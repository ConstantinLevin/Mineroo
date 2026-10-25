"""Die eigene Tabelle: Datenvertrag, Zugriff, Wecksignal — und sonst nichts.

Dieses Modul ist der einzige Ort im Paket, an dem SQL auf `documents` steht. Der
Grund ist nicht Ordnungsliebe: Der Vertrag, den diese Tabelle darstellt, wird von
ZWEI Prozessen benutzt, die einander nie sehen — der Daemon schreibt, die Fassade
liest, und dazwischen steht kein Compiler, der eine Unstimmigkeit bemerken wuerde.
Ein zweiter Ort mit eigenem SQL waere eine zweite Lesart derselben Tabelle, und
die faellt erst auf, wenn die Daten schon falsch sind.

Was NICHT hier steht: SQL auf Procrastinates eigene Tabellen. Die werden ueber
den `JobManager` gelesen, nie von Hand — sie sind die Innereien einer fremden
Bibliothek, ihre Form ist versionsabhaengig, und die Bibliothek liefert eine
dokumentierte Lese-Schnittstelle mit. Die EINE Ausnahme steht am Ende dieser
Datei, ist einzeln benannt und traegt ihre Warnung mit sich.

Die tragende Festlegung des ganzen Datenmodells stammt vom Auftraggeber und
lautet: EIN Buch, nicht zwei. Jede Tatsache steht an genau einer Stelle: das
Ergebnis in `documents`, die offene Eingabe in `inputs`, was wartet oder laeuft
in Procrastinates Job-Tabelle. „man sagt procrastinate es soll einen job
erstelelen, und die zeile anlegen ist teil davon nicht was wir machen" (Spec
Mineroo#1): Die Ergebniszeile legt der Job an, wenn er endet, und sie kennt
deshalb nur Endzustaende. Es gibt keinen Zustand `new` und keinen `processing`.
Offen ist ein Dokument, solange seine Eingabe in `inputs` steht und es kein
Ergebnis hat; „laeuft gerade" steht in der Job-Tabelle und wird nirgends
gespiegelt. Es gibt keinen Abgleich-Waechter, weil es nichts abzugleichen gibt.
"""

from __future__ import annotations

import asyncio
import enum
import math
import time
from collections.abc import AsyncGenerator, Generator, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, fields
from datetime import datetime
from typing import Any, Final

import psycopg
import psycopg.conninfo
import psycopg.sql
from procrastinate.jobs import Job
from psycopg.rows import class_row, dict_row
from psycopg.types.json import Jsonb

from .config import DAEMON_HEARTBEAT_MAX_AGE_SECONDS, DEFAULT_WAIT_POLL_SECONDS

from .errors import (
    ConfigurationError,
    DatabaseError,
    DocumentNotFound,
    MineruOcrError,
    TerminalOverwriteError,
    WaitTimeoutError,
)

DONE_CHANNEL: Final = "doc_done"
"""Der Kanal, auf dem das Terminal-Schreiben seine Warter weckt.

Eigenbau, und das ist kein Versaeumnis: Procrastinate hat kein eingebautes
„Job fertig"-Signal an externe Warter — die einzigen dokumentierten
NOTIFY-Verwendungen sind neue Jobs und Abbruch-Anforderungen, und die einzige
Lausch-Schnittstelle (`listen_for_jobs`) hoert ausdruecklich auf
Einreih-Vorgaenge (Feature-Verifikation Punkt 10). Wer auf ein DOKUMENT wartet,
braucht deshalb einen eigenen Kanal."""

_TERMINALE_JOB_ZUSTAENDE: Final = frozenset(
    {"succeeded", "failed", "cancelled", "aborted"}
)
"""Procrastinates Job-Endzustaende, in seinen eigenen Worten.

Die Aufzaehlung stammt aus dem Zustandsmodell der Doku (todo, doing, succeeded,
failed, cancelled, aborted). Sie steht hier als Menge und nicht als Bedingung
verstreut, weil `derive_status` die einzige Stelle ist, die sie braucht — und
weil ein vergessener Zustand dort still zu „laeuft noch" wuerde."""

# --------------------------------------------------------------------------
# Der Datenvertrag
# --------------------------------------------------------------------------

SCHEMA_SQL: Final = """
CREATE TABLE IF NOT EXISTS documents (
    sha256        text PRIMARY KEY,
    size_bytes    bigint,
    mime          text,
    page_count    integer,
    status        text NOT NULL
        CHECK (status IN ('done', 'empty', 'failed', 'input_error')),
    raw           jsonb,
    error         text,
    endpoint_id   text NOT NULL,
    image_tag_at_submit text,
    notes         jsonb NOT NULL DEFAULT '[]',
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS documents_status_idx ON documents (status);

CREATE TABLE IF NOT EXISTS inputs (
    sha256        text PRIMARY KEY,
    content       bytea NOT NULL,
    job_id        bigint
);
"""
"""Das vollstaendige Schema: `documents` fuer die Ergebnisse, `inputs` fuer die
Eingaben, die noch kein Ergebnis haben.

Der CHECK auf `status` steht im Code, nicht nur im Kommentar: Die Aufzaehlung der
gueltigen Zustaende ist der Kern dieses Datenmodells, und eine Aufzaehlung, die
nur ein Kommentar ist, wird irgendwann von einem falsch geschriebenen Zustand
verletzt, den niemand bemerkt. Die Datenbank bemerkt ihn.

Der Index folgt der Abfrage, die es tatsaechlich gibt: die Zaehlung nach Zustand
fuer `status()`. Mehr Indizes waeren geraten; dieser ist hergeleitet.

Eine dritte Abfrage gibt es, und sie hat bewusst KEINEN Index: die offenen
Aufruest-Faelle in `status()` — `status = 'input_error'` mit einem
entsprechenden Eintrag in `notes`. Sie laeuft ueber eine jsonb-Filterung, aber
erst NACH dem Zustandsfilter, und `input_error` ist der mit Abstand seltenste
Zustand: Bei dreissigtausend Dokumenten bleiben davon eine Handvoll uebrig,
ueber die dann gefiltert wird. Ein Teil-Index dafuer waere Aufwand gegen ein
Problem, das die Groessenordnung nicht hergibt. Sollte sich das aendern, ist die
Stelle hier benannt.

Zu den einzelnen Spalten — die Begruendungen, ohne die niemand sie richtig
benutzt:

`sha256` ist der ROHE Byte-Hash der Quelldatei und der Primaerschluessel.
Ausdruecklich OHNE Normalisierung der Metadaten: Ein Normalisierungsfehler wuerde
falsche Treffer bei der Dubletten-Erkennung erzeugen — „gilt als fertig, wurde
nie erkannt" —, und das ist der unsichtbarste aller Fehler. Die eigentliche
Dubletten-Erkennung ist Sache des Aufrufers.

`inputs` haelt die Bytes der Eingabe, solange das Dokument kein Endergebnis hat:
`sha256` ist der Schluessel, `content` die vollstaendigen Bytes als `bytea` (nicht
base64: verlustfrei wie dieses, aber auf der Platte das 1,03-Fache statt das
1,37-Fache). Der Job traegt nur den Hash und liest die Eingabe ueber ihn. Mit dem
Endergebnis loescht der Job die Eingabe in derselben Transaktion, bei `done`,
`empty` und `input_error`; bei `failed` bleibt sie, damit `retry` etwas zu lesen
hat. Pfade gibt es in der Datenbank nicht. Die Tabelle ist zugleich das
Verzeichnis der offenen Dokumente: Abschicken legt nur die Eingabe an, und ihr
Primaerschluessel ist die Sperre gegen ein zweites Einreihen.

`status` kennt nur Endzustaende, kein `new` und kein `processing`: Die Zeile
legt der Job an, wenn er endet (siehe Modul-Vorspann). Deshalb hat die Spalte
keine Voreinstellung.

`size_bytes` und `mime` bestimmt der Job aus den Bytes der Eingabe. Leer bleiben
sie nur, wo sie sich nicht bestimmen liessen: `mime` bei einem Typ, den das Paket
nicht kennt, beide, wenn die Eingabe fehlte oder ihr Hash nicht stimmte. Ein
erfundener Wert waere dort eine Angabe, die stimmt aussieht und nicht stimmt.

`raw` ist Jurgens „raw section" — und sie steht auf DOKUMENT-Ebene, nicht auf
Chunk-Ebene. Das ist die tragende Festlegung und der haeufigste Denkfehler beim
Lesen dieses Schemas, deshalb ausgeschrieben: Eingabe ist das ganze Dokument,
Ausgabe ist das ganze Dokument in erkannter Form. Dass die Engine unterwegs
mehrfach angesprochen wurde, weil das Dokument fuer eine einzelne Anfrage zu
gross war, ist eine Eigenschaft des TRANSPORTS und geht die Datenbank nichts an.
„DIE CHUNKS SIND ABSTRAHIERT ES LANDET IN POSTGRES NUR DAS FERTIG
ZUSAMMENGESETZTE UND DADURCH GEHT KEINE INFORMATION VERLOREN WEIL INPUT IST DAS
GANZE DOKUMENT UND OUTPUT IST DAS GANZE DOKUMENT IN OCR, das es dazwischen
chunks gab in der arbeit is irrelevant" (Jurgen, 07.08.2026).

Was also darin steht: der zusammengesetzte Text, die zusammengesetzte
Strukturliste mit den Seitenzahlen DES ORIGINALS, die vereinigten Bilder, dazu
alles Uebrige, was die Antworten trugen — `middle`, die Layout- und Span-PDFs,
die `debug`-Bloecke, die Ausfuehrungszeiten. Zusammengesetzt heisst NICHT
reduziert: Weggelassen wird nichts, und nichts wird auf eine Auswahl
eingedampft. Zusammengesetzt heisst nur, dass die Stuecke wieder in der
Reihenfolge und mit der Zaehlung stehen, die sie vor dem Schnitt hatten.

Zusammengesetzt wird im EXECUTOR, vor dem Schreiben — nicht spaeter in den
Gettern. Der Grund ist zwingend und war der Fehler einer frueheren Fassung: Nur
der Executor weiss, welcher Teil welche Seiten trug. Wer das Zusammensetzen auf
die Leseseite verschiebt, muss dieses Wissen mitschleppen — also doch
Chunk-Angaben in der Datenbank fuehren, gegen die Festlegung oben.

Geschrieben wird `raw` NUR beim Endschreiben eines Durchlaufs, das die Zeile
anlegt, und ueberschrieben nur dann, wenn ein gescheitertes Dokument erneut
verarbeitet wird.
Mengengeruest: rund 0,05 bis 0,1 MB je Seite; bei 30.000 Dokumenten sind das
Gigabyte, die Postgres auslagert. Der Plattenplatz gehoert vor dem
grossen Lauf geprueft (Plan 12.5). Warum die Ergebnisse in die Datenbank gehoeren
und nicht als Pfad daneben: Sie sind ab dem Schreiben die EINZIGE dauerhafte
Kopie — auf der Gegenseite liegt ein fertiges Ergebnis nur rund dreissig Minuten
zum Abholen bereit. Fuer die EINGABE gilt das Umgekehrte: Sie liegt in `inputs`
und verschwindet mit dem Endergebnis.

`image_tag_at_submit` ist Herkunft. Ohne diese Angabe an der Zeile laesst sich
eine spaetere Qualitaetsfrage nicht mehr beantworten.

`notes` ist eine Ereignisliste, kein Freitext: angehaengt wird, nie ersetzt.
Darin landen Aufruest-Hinweise und Auffaelligkeiten.

`updated_at` wird im Anwendungscode gepflegt, nicht per Trigger. Ein Trigger
waere versteckter Zustand in der Datenbank; die Tabelle soll ohne Wissen ueber
verborgene Mechanik lesbar sein.
"""


class DocumentStatus(enum.StrEnum):
    """Die vier Zustaende einer Ergebniszeile. Alle sind endgueltig.

    Eine Zeile gibt es erst, wenn der Job endet; „noch kein Ergebnis" ist deshalb
    kein Zustand der Zeile, sondern ihr Fehlen bei stehender Eingabe (siehe
    Modul-Vorspann).

    Warum `empty` und `input_error` eigene Zustaende sind und nicht Sonderfaelle
    von `done` bzw. `failed`: Ein leeres Ergebnis als Erfolg zu verbuchen ist
    unsichtbarer Verlust, es als Fehler zu verbuchen ist endlose Wiedervorlage
    mit doppelten Kosten — beide Fehler sind in den Ad-hoc-Skripten je einmal
    wirklich passiert. Und `input_error` von `failed` zu trennen beantwortet die
    Frage „wofuer haben wir bezahlt?": `input_error` hat RunPod nie gesehen.
    """

    DONE = "done"
    """Ergebnis vollstaendig da, mindestens ein Chunk mit Inhalt."""

    EMPTY = "empty"
    """Alles leer — gueltig, aber am Bild zu pruefen."""

    FAILED = "failed"
    """Endgueltig gescheitert: nicht wiederholbar oder Versuche erschoepft."""

    INPUT_ERROR = "input_error"
    """Vor jedem RunPod-Kontakt aussortiert. Hat nichts gekostet."""


class Phase(enum.Enum):
    """Wo ein Dokument gerade steht — ABGELEITET, nirgends gespeichert.

    Das ist die Antwort auf „ein Buch, nicht zwei": Der Fortschritt eines
    Dokuments wird aus Ergebniszeile, Eingabe und dem Job der Eingabe
    GELESEN, nie gespiegelt. Diese Aufzaehlung existiert nur im Speicher. Ein
    unbekanntes Dokument hat keine Phase; wer danach fragt, bekommt
    `DocumentNotFound`.
    """

    TERMINAL = "terminal"
    """Es gibt ein Ergebnis. Welches, sagt `row_status`."""

    WAITING = "waiting"
    """Offen, sein Job wartet (`todo`)."""

    RUNNING = "running"
    """Offen, sein Job laeuft (`doing`)."""

    ENDED_WITHOUT_RESULT = "ended_without_result"
    """Offen, aber kein Job ist mehr unterwegs: Sein Job ist endgueltig zu
    Ende, ohne ein Ergebnis zu schreiben, oder es gibt keinen.

    Der seltene Fall, denn jeder Job, der nicht neu eingereiht wird, schreibt
    ein Ergebnis (Mineroo#27): ein Abbruch ueber die API, ein von Hand
    abgesagter Job, ein unbekannter Task-Name, ein Endschreiben, das im letzten
    Versuch an der Datenbank scheitert. Er ist KEIN
    Datenfehler, sondern genau der Zustand, den das Ein-Buch-Modell erwartet.
    Erneutes Abschicken heilt ihn NICHT: Die Eingabe steht, das Dokument gilt als
    unterwegs. Der Weg zurueck ist `retry`: Es reiht einen neuen Job ein. „es wird
    dann ncohmal und das wars wenns nicht fertig ist ists gefailed" (Plan 2.4).
    """


@dataclass(frozen=True)
class DocumentRow:
    """Eine Ergebniszeile, so wie sie in der Datenbank steht. Reine Daten."""

    sha256: str
    size_bytes: int | None
    """Die Groesse der Eingabe, vom Job bestimmt. None nur, wenn die Eingabe
    fehlte oder beschaedigt war (siehe SCHEMA_SQL)."""

    mime: str | None
    """Der Typ der Eingabe, vom Job aus den Bytes bestimmt. None, wenn das Paket
    den Typ nicht kennt oder die Eingabe fehlte oder beschaedigt war."""

    page_count: int | None
    status: DocumentStatus
    raw: dict[str, Any] | None
    """Das zusammengesetzte Gesamtergebnis auf Dokument-Ebene, oder None, solange
    es nicht gelesen wurde. EIN Objekt, keine Liste — eine Liste waere die
    Chunk-Struktur, und die gibt es hier nicht (siehe SCHEMA_SQL)."""

    error: str | None
    endpoint_id: str
    image_tag_at_submit: str | None
    notes: list[dict[str, Any]]
    created_at: datetime
    updated_at: datetime

    raw_loaded: bool = True
    """Ob `raw` wirklich gelesen wurde. `list_documents` setzt es auf False und
    laesst `raw` None — ohne diese Unterscheidung waere „nicht geladen" von „ist
    leer" nicht zu trennen, und eine Huelle aus einer gelisteten Zeile lieferte
    fuer ein fertiges Dokument stillschweigend nichts."""

    def __post_init__(self) -> None:
        """Macht aus der gelesenen Zeichenkette einen `DocumentStatus`.

        Warum das noetig ist: Die Spalte ist `text` mit einer CHECK-Bedingung und
        kein Postgres-Aufzaehlungstyp, also reicht `class_row` die rohe
        Zeichenkette durch. `DocumentStatus` ist zwar ein `StrEnum` und
        vergliche sich mit ihr richtig — aber nur beim Vergleich. `row.status is
        DocumentStatus.DONE` waere an einer Zeichenkette falsch, und der Fehler
        kaeme erst dort, wo jemand so vergleicht, nicht dort, wo die falsche
        Zeile entstand.

        `object.__setattr__`, weil die Klasse eingefroren ist. Das ist der
        vorgesehene Weg fuer genau diesen Fall und keine Umgehung: Eingefroren
        heisst „nach der Erzeugung unveraenderlich", und `__post_init__` gehoert
        noch zur Erzeugung.

        Ohne Fallunterscheidung, und das ist Absicht: `DocumentStatus(x)` gibt
        fuer ein bereits richtiges Mitglied dasselbe Mitglied zurueck.
        """
        object.__setattr__(self, "status", DocumentStatus(self.status))

    def angaben(self) -> dict[str, Any]:
        """Die Angaben der Zeile als einfache Abbildung, ohne `raw`.

        Fuer jede Ausgabe mit `--json`, die nur den Stand braucht: die Huelle
        (`OCRWrapper.to_dict`) und `DerivedStatus.to_dict` stellen dieselben
        Angaben mit denselben Schluesseln dar, aus dieser einen Stelle.
        """
        return {
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "mime": self.mime,
            "page_count": self.page_count,
            "status": self.status.value,
            "error": self.error,
            "endpoint_id": self.endpoint_id,
            "image_tag_at_submit": self.image_tag_at_submit,
            "notes": self.notes,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


@dataclass(frozen=True)
class DocumentState:
    """Was die Datenbank in EINEM Augenblick ueber ein Dokument weiss.

    Ergebnis da heisst fertig, Eingabe da heisst offen, keins von beiden heisst
    unbekannt. Beides kommt aus EINER Anweisung (`read_states`), nie aus zweien:
    Erst `documents`, dann `inputs` zu lesen, meldete ein Dokument, dessen Job
    genau dazwischen fertig wird, faelschlich als unbekannt.
    """

    sha256: str
    row: DocumentRow | None
    """Die Ergebniszeile ohne `raw` (`raw_loaded` False), None ohne Ergebnis."""

    has_input: bool
    """Ob die Eingabe in `inputs` steht. Bei `failed` bleibt sie stehen; dann
    gibt es beides, und das Ergebnis entscheidet."""

    job_id: int | None = None
    """Die Kennung des Jobs, die an der Eingabe steht (`inputs.job_id`), None ohne
    Eingabe. Ueber sie finden `document()` und `retry` den Job."""

    @property
    def is_terminal(self) -> bool:
        """Es gibt ein Ergebnis, gleich welches."""
        return self.row is not None

    @property
    def is_open(self) -> bool:
        """Kein Ergebnis, aber eine Eingabe: unterwegs oder liegengeblieben."""
        return self.row is None and self.has_input

    @property
    def is_unknown(self) -> bool:
        """Weder Ergebnis noch Eingabe: nie abgeschickt."""
        return self.row is None and not self.has_input


@dataclass(frozen=True)
class DerivedStatus:
    """Der zusammengesetzte Stand aus Ergebnis, Eingabe und dem Job der Eingabe.

    Wird bei jeder Abfrage neu gebildet und nirgends abgelegt. Wer ihn ablegte,
    haette die zweite Buchfuehrung wieder eingefuehrt, gegen die das ganze
    Datenmodell steht.
    """

    sha256: str
    phase: Phase

    row: DocumentRow | None
    """Die Ergebniszeile ohne `raw`, None solange das Dokument offen ist."""

    job_id: int | None
    """Kennung des Jobs, die an der Eingabe steht (`inputs.job_id`), None wenn es
    keinen gibt (auch, wenn er nicht mehr da ist)."""

    job_status: str | None
    """Dessen Zustand in Procrastinates Worten (`todo`, `doing`, `succeeded`,
    `failed`, `cancelled`, `aborted`), unuebersetzt weitergereicht — wer in der
    Datenbank nachsieht, soll dasselbe Wort finden."""

    attempts: int | None
    """Wie oft der Job bisher vorgelegt wurde. Achtung auf die Reichweite: Das
    zaehlt JEDE Wiedervorlage — Strategie-Retry, stalled-Wiederaufnahme und Retry
    von Hand laufen durch dieselbe SQL-Funktion und erhoehen den Zaehler alle drei
    (Feature-Verifikation Punkt 19)."""

    @property
    def row_status(self) -> DocumentStatus | None:
        """Der Endzustand, None solange das Dokument offen ist."""
        return self.row.status if self.row is not None else None

    def to_dict(self) -> dict[str, Any]:
        """Der Stand als einfache Abbildung, ohne `raw` — fuer `show --json`.

        Bei einem Ergebnis stehen dessen Angaben darin (`DocumentRow.angaben`),
        bei einem offenen Dokument nur Hash, Phase und Job; `status` ist dann
        None.
        """
        abbildung: dict[str, Any] = (
            self.row.angaben() if self.row is not None else {"sha256": self.sha256, "status": None}
        )
        abbildung["phase"] = self.phase.value
        abbildung["job"] = {
            "id": self.job_id,
            "status": self.job_status,
            "attempts": self.attempts,
        }
        return abbildung


# --------------------------------------------------------------------------
# Einrichtung
# --------------------------------------------------------------------------


def setup(dsn: str) -> None:
    """Richtet Datenbank und Schema ein. Mehrfach aufrufbar. KEINE Migrationen.

    Vier Schritte: Datenbank anlegen, falls sie fehlt; Procrastinates eigenes
    Schema anwenden, falls es fehlt (ueber `app.schema_manager.apply_schema()`,
    die eingebaute Schnittstelle — NICHT das Kommando `procrastinate schema
    --apply`, das waere ein Unterprozess, der die Verbindungsangaben ein zweites
    Mal braeuchte); `SCHEMA_SQL` anwenden; nachsehen, ob die Tabelle auf der
    Platte zu `DocumentRow` passt.

    Das „falls es fehlt" im zweiten Schritt ist keine Sparsamkeit, sondern
    Bedingung fuer die Zusicherung unten: `apply_schema()` selbst ist NICHT
    mehrfach aufrufbar und bricht beim zweiten Mal ab. Begruendung und Beleg
    stehen bei `_procrastinate_schema_anwenden_falls_noetig`.

    Der vierte Schritt ist die Antwort auf die einzige Doppelung dieses Moduls:
    `SCHEMA_SQL` und `DocumentRow` beschreiben dieselbe Tabelle. Ein
    Migrationswerkzeug loeste das auf, waere hier aber Ballast — dies ist ein
    Werkzeug fuer einen Menschen auf einer Maschine, und Migrationen werden nicht
    gefahren. Stattdessen wird die Doppelung LAUT: Weichen die tatsaechlichen
    Spalten von den Feldern der Zeilenklasse ab, sagt `setup` das samt der
    einzigen sinnvollen Abhilfe — Datenbank wegwerfen und neu anlegen. Ein
    Auseinanderlaufen, das keiner bemerkt, gibt es damit nicht, und ein
    Migrationssystem, das niemand braucht, auch nicht.

    Args:
        dsn: Verbindung zur ZIEL-Datenbank. Zum Anlegen wird kurz auf die
            Wartungsdatenbank derselben Instanz verbunden — `CREATE DATABASE`
            geht nicht aus der Datenbank heraus, die angelegt werden soll.

    Zusicherung:
        Mehrfach aufrufbar ohne Wirkung beim zweiten Mal. Zerstoert NICHTS:
        keine Tabelle wird geloescht, keine Spalte veraendert, keine Zeile
        angefasst — auch dann nicht, wenn Schritt vier eine Abweichung findet.
        Ein `setup` auf eine gefuellte Datenbank ist ein Nichts-Tun.

    Raises:
        ConfigurationError: Die Instanz ist nicht erreichbar oder die Rechte
            reichen nicht.
        DatabaseError: Die vorhandene Tabelle passt nicht zur Zeilenklasse. Die
            Meldung nennt die abweichenden Spalten und den Weg (wegwerfen, neu
            anlegen) — nicht bloss „Schema-Abweichung".

    Kosten: keine.
    """
    _datenbank_anlegen_falls_noetig(dsn)
    _procrastinate_schema_anwenden_falls_noetig(dsn)

    with connect(dsn) as conn:
        conn.execute(SCHEMA_SQL)
        check_schema(conn)


def _procrastinate_schema_anwenden_falls_noetig(dsn: str) -> None:
    """Wendet Procrastinates Schema an — aber nur, wenn es noch nicht da ist.

    WARUM DIE VORPRUEFUNG, gemessen am Objekt (08.08.2026, Fassung 3.9.0):
    `schema_manager.apply_schema()` ist NICHT mehrfach aufrufbar. Beim zweiten
    Aufruf gegen dieselbe Datenbank bricht es ab mit
    `DuplicateObject: type "procrastinate_job_status" already exists`, verpackt in
    einen `ConnectorException`. Der Grund liegt in Postgres: `CREATE TYPE` kennt
    kein `IF NOT EXISTS`, und Procrastinates `schema.sql` legt den Aufzaehlungstyp
    fuer den Job-Zustand unbedingt an. Ein `SchemaManager` bietet dagegen nichts
    an — er hat genau vier Methoden (`apply_schema`, `apply_schema_async`,
    `get_schema`, `get_migrations_path`), keine davon beantwortet „ist es schon
    da?".

    Ohne diese Vorpruefung waere die Zusicherung von `setup` — mehrfach
    aufrufbar — schlicht falsch, und ein zweites `mineru-ocr setup` endete mit
    einem Absturz, dessen Meldung von einem Aufzaehlungstyp handelt, mit dem der
    Benutzer nie zu tun hatte.

    Geprueft wird auf die Tabelle `procrastinate_jobs`, und das ist verlaesslich:
    Der Konnektor fuehrt die ganze `schema.sql` in EINEM `conn.execute` innerhalb
    von `pool.connection()` aus — also in einer einzigen Transaktion, die bei
    einem Fehler vollstaendig zurueckgerollt wird (am Quelltext geprueft). Ein
    halb angewandtes Schema kann es damit nicht geben; entweder ist alles da oder
    nichts.
    """
    import procrastinate

    with connect(dsn) as conn:
        vorhanden = conn.execute(
            "SELECT to_regclass('public.procrastinate_jobs')"
        ).fetchone()
        if vorhanden is not None and vorhanden[0] is not None:
            return

    app = procrastinate.App(connector=procrastinate.SyncPsycopgConnector(conninfo=dsn))
    with app.open():
        app.schema_manager.apply_schema()


def _datenbank_anlegen_falls_noetig(dsn: str) -> None:
    """Legt die Ziel-Datenbank an, wenn sie fehlt.

    `CREATE DATABASE` laesst sich nicht aus der Datenbank heraus ausfuehren, die
    angelegt werden soll — deshalb der Umweg ueber die Wartungsdatenbank
    derselben Instanz.
    """
    angaben = psycopg.conninfo.conninfo_to_dict(dsn)
    zielname = angaben.get("dbname")
    if not isinstance(zielname, str) or not zielname:
        raise ConfigurationError(
            f"Die Verbindungszeichenkette nennt keine Datenbank (`dbname`): {dsn!r}"
        )

    try:
        # `dbname` als Schluesselwort UEBERSCHREIBT die Angabe aus der
        # Verbindungszeichenkette — psycopg fuehrt beides zusammen. Der Umweg
        # ueber `conninfo_to_dict` und `make_conninfo` waere derselbe Vorgang von
        # Hand, mit dem Zusatzrisiko, dass beim Zerlegen und Wiederzusammenbauen
        # eine Angabe verlorengeht.
        with psycopg.connect(dsn, dbname="postgres", autocommit=True) as conn:
            vorhanden = conn.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (zielname,)
            ).fetchone()
            if vorhanden is None:
                # Der Name kommt aus der Verbindungszeichenkette des Benutzers und
                # laesst sich nicht als Parameter binden — CREATE DATABASE nimmt
                # keine. `psycopg.sql.Identifier` maskiert ihn richtig; eine
                # Zeichenketten-Verkettung waere hier die klassische Luecke.
                conn.execute(
                    psycopg.sql.SQL("CREATE DATABASE {}").format(
                        psycopg.sql.Identifier(zielname)
                    )
                )
    except psycopg.Error as fehler:
        raise _verbindungsfehler_uebersetzen(
            fehler, f"Datenbank {zielname!r} anlegen"
        ) from fehler


INPUT_SPALTEN: Final = frozenset({"sha256", "content", "job_id"})
"""Die Spalten von `inputs`. Die Tabelle hat keine Zeilenklasse (der Executor
liest nur `content`), also steht die Erwartung hier, und `_schema_bewerten`
vergleicht gegen sie. Die Form der Spalten (Typ, NOT NULL) und den
Primaerschluessel prueft `_schema_bewerten` dort selbst."""

MAX_INPUT_BYTES: Final = 1_000_000_000
"""Die groesste Eingabe, die sich ablegen laesst. Postgres begrenzt ein Feld und
eine Nachricht auf knapp 1 GiB: Gemessen gingen 1.070.000.008 Byte noch hinein,
1.101.004.808 nicht (der Server meldet dann „invalid message length" und trennt
die Verbindung). Die Grenze liegt darunter, damit die Ablehnung eine Meldung ist
und kein Verbindungsabbruch. Sie gilt fuer jede Eingabe, auch fuer PDFs, die der
Job danach in Stuecke schneidet."""


def check_schema(conn: psycopg.Connection[Any]) -> None:
    """Prueft rein lesend, ob die Tabellen da sind und zum Code passen.

    Fuer jeden Zugang: Daemon und Client rufen das beim Start (der
    `AsyncOCRClient` ueber `check_schema_async`), `setup` nach dem Anlegen. Nach einem Paket-Update ohne `setup` steht sonst die alte
    Tabelle mit `new`-Zeilen da, und der erste Lesezugriff scheiterte an
    `DocumentStatus('new')` mit einem `ValueError` — beim Daemon in jedem Job
    vor dem Fehlerzweig, sodass das Dokument ohne Ergebnis endete, bei der
    Kommandozeile als ungefangener Ausnahmetext.

    Gelesen wird der Katalog in EINER Anweisung (`_KATALOG_SQL`), bewertet ohne
    Ein- und Ausgabe (`_schema_bewerten`).

    Raises:
        DatabaseError: Eine der Tabellen fehlt (nie `setup` gelaufen) oder passt
            nicht; die Meldung nennt den Weg.

    Kosten: keine. Eine Katalogabfrage.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        katalog = cur.execute(_KATALOG_SQL).fetchone()
    _schema_bewerten(katalog)


async def check_schema_async(conn: psycopg.AsyncConnection[Any]) -> None:
    """Dasselbe wie `check_schema`, asynchron: fuer den `AsyncOCRClient`."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_KATALOG_SQL)
        katalog = await cur.fetchone()
    _schema_bewerten(katalog)


def _form_sql(tabelle: str) -> str:
    """Spalten einer Tabelle als JSON-Liste von [Name, Typ, NOT NULL].

    Aus `pg_attribute` der Tabelle, die `search_path` aufloest
    (`to_regclass`), nicht aus `information_schema.columns` nach Namen: Das
    fasste gleichnamige Tabellen anderer Schemas dazu und meldete eine gesunde
    Datenbank als unpassend. Fehlt die Tabelle, ist die Liste leer.
    """
    return (
        "(SELECT coalesce(json_agg(json_build_array(a.attname, "
        "format_type(a.atttypid, a.atttypmod), a.attnotnull)), '[]'::json) "
        f"FROM pg_attribute a WHERE a.attrelid = to_regclass('{tabelle}') "
        "AND a.attnum > 0 AND NOT a.attisdropped)"
    )


_KATALOG_SQL: Final = (
    "SELECT to_regclass('documents') IS NOT NULL AS documents_da, "
    "to_regclass('inputs') IS NOT NULL AS inputs_da, "
    f"{_form_sql('documents')} AS documents_form, "
    f"{_form_sql('inputs')} AS inputs_form, "
    "(SELECT coalesce(string_agg(pg_get_constraintdef(oid), ' '), '') "
    "FROM pg_constraint WHERE conrelid = to_regclass('documents') "
    "AND contype = 'c') AS pruefungen, "
    "(SELECT pg_get_expr(d.adbin, d.adrelid) FROM pg_attrdef d "
    "JOIN pg_attribute a ON a.attrelid = d.adrelid AND a.attnum = d.adnum "
    "WHERE d.adrelid = to_regclass('documents') AND a.attname = 'status') "
    "AS voreinstellung, "
    "(SELECT coalesce(json_agg(a.attname), '[]'::json) FROM pg_index i "
    "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY (i.indkey) "
    "WHERE i.indrelid = to_regclass('inputs') AND i.indisprimary) AS schluessel"
)
"""Was `check_schema` ueber beide Tabellen wissen
muessen, in EINER Anweisung: ob sie da sind, ihre Spalten, die Pruefungen und
die Voreinstellung von `documents.status`, der Primaerschluessel von `inputs`."""


def _schema_bewerten(katalog: dict[str, Any] | None) -> None:
    """Prueft die Tabellen auf der Platte gegen ihre Zeilenklassen. Ohne Ein- und
    Ausgabe: `katalog` ist die Zeile von `_KATALOG_SQL`.

    Die Antwort auf die einzige Doppelung dieses Moduls. Ein Migrationswerkzeug
    loeste sie auf, waere hier aber Ballast — dies ist ein Werkzeug fuer einen
    Menschen auf einer Maschine. Stattdessen wird die Doppelung LAUT.

    Geprueft werden beide Tabellen, `documents` gegen die Felder von
    `DocumentRow` und `inputs` gegen `INPUT_SPALTEN`; gemeldet wird alles auf
    einmal, damit die Abhilfe nicht Tabelle fuer Tabelle gesucht werden muss.
    """
    assert katalog is not None  # Ein SELECT ohne FROM liefert genau eine Zeile.
    fehlend = [t for t in ("documents", "inputs") if not katalog[f"{t}_da"]]
    if fehlend:
        raise DatabaseError(
            f"Die Tabellen {fehlend} fehlen in dieser Datenbank. Erst "
            "`mineru-ocr setup` ausfuehren."
        )
    # `raw_loaded` ist ein Merkmal des gelesenen Objekts, keine Spalte — es sagt,
    # ob `raw` mitgelesen wurde, und das kann die Tabelle nicht wissen.
    erwartet = {
        "documents": (
            {feld.name for feld in fields(DocumentRow)} - {"raw_loaded"},
            "DocumentRow",
        ),
        "inputs": (set(INPUT_SPALTEN), "INPUT_SPALTEN"),
    }
    abweichungen: list[str] = []
    for tabelle, (soll, gegen) in erwartet.items():
        form: dict[str, tuple[str, bool]] = {
            name: (typ, nicht_null)
            for name, typ, nicht_null in katalog[f"{tabelle}_form"]
        }
        ist = set(form)
        fehlend_spalten = soll - ist
        ueberzaehlig = ist - soll
        if fehlend_spalten or ueberzaehlig:
            abweichungen.append(
                f"Die Tabelle `{tabelle}` passt nicht zu {gegen}. "
                f"Im Code, aber nicht in der Tabelle: {sorted(fehlend_spalten)}. "
                f"In der Tabelle, aber nicht im Code: {sorted(ueberzaehlig)}."
            )
        elif tabelle == "documents":
            # Die Namen stimmen; ob die Tabelle zum Modell passt, sagen sie
            # nicht. Eine Tabelle aus der Zeit, als das Abschicken die Zeile
            # anlegte, kennt `new`, setzt es als Voreinstellung und verlangt
            # `mime` und `size_bytes` — und der Job, der die Zeile jetzt erst am
            # Ende anlegt, scheiterte daran bei der ersten Eingabe, die fehlt.
            pruefungen = katalog["pruefungen"]
            voreinstellung = katalog["voreinstellung"]
            nullbar_erwartet = {"size_bytes", "mime"}
            falsch_nicht_null = sorted(
                name for name in nullbar_erwartet if form[name][1]
            )
            if "'new'" in pruefungen or voreinstellung is not None or falsch_nicht_null:
                abweichungen.append(
                    "Die Tabelle `documents` hat die erwarteten Spalten, aber die "
                    "Form aus der Zeit, als das Abschicken die Zeile anlegte: "
                    "erwartet `status` ohne Voreinstellung und nur mit den "
                    "Endzustaenden ('done', 'empty', 'failed', 'input_error'), "
                    "`mime` und `size_bytes` ohne NOT NULL; gefunden die "
                    f"Pruefungen {pruefungen or '(keine)'}, Voreinstellung "
                    f"{voreinstellung or '(keine)'}, "
                    f"NOT NULL an {falsch_nicht_null or '(keiner)'}."
                )
        elif tabelle == "inputs":
            # Die Namen stimmen; ob die Form stimmt, sagen sie nicht. Ohne
            # Primaerschluessel scheitert `ON CONFLICT (sha256)` beim ersten
            # Abschicken, und `setup` haette vorher Erfolg gemeldet.
            schluessel = set(katalog["schluessel"])
            if (
                form["sha256"][0] != "text"
                or form["content"] != ("bytea", True)
                # Leer erlaubt: Das Abschicken legt die Eingabe ohne Kennung an
                # und schreibt sie erst, wenn `defer` sie zurueckgab.
                or form["job_id"] != ("bigint", False)
                or schluessel != {"sha256"}
            ):
                abweichungen.append(
                    "Die Tabelle `inputs` hat die erwarteten Spalten, aber nicht "
                    "ihre Form: erwartet `sha256 text PRIMARY KEY`, "
                    "`content bytea NOT NULL` und `job_id bigint` (ohne NOT "
                    "NULL), gefunden "
                    f"{form} mit Primaerschluessel {sorted(schluessel)}."
                )
    if not abweichungen:
        return
    raise DatabaseError(
        " ".join(abweichungen)
        + " Es gibt in diesem Werkzeug bewusst keine Migrationen — der Weg ist: "
        "Datenbank wegwerfen und neu anlegen (DROP DATABASE, dann `mineru-ocr "
        "setup`). Wenn darin bezahlte Ergebnisse stehen, vorher sichern."
    )


def _verbindungsfehler_uebersetzen(
    fehler: psycopg.Error, was: str
) -> ConfigurationError | DatabaseError:
    """Entscheidet, ob ein Postgres-Fehler wiederholbar ist oder nicht.

    DIESE UNTERSCHEIDUNG ENTSCHEIDET UEBER GELD, und deshalb steht sie an einer
    Stelle statt verstreut: Eine Verbindungsstoerung ist wiederholbar, ein
    falscher Zugang ist es nicht. Fiele bei einem einsekuendigen Postgres-
    Aussetzer im Terminal-Schreiben ein `ConfigurationError`, machte er aus einem
    bezahlten Dokument ein endgueltig gescheitertes.

    Unterschieden wird am SQLSTATE, nicht am Text der Meldung: Klasse 28
    (`invalid_authorization_specification`, also auch das falsche Passwort) und
    `3D000` (`invalid_catalog_name`, es gibt die Datenbank nicht) sind Zugangs-
    und Konfigurationsfehler. Alles Uebrige — insbesondere ein Fehler ohne
    SQLSTATE, wie ihn eine abgelehnte Verbindung erzeugt — gilt als wiederholbar.
    Im Zweifel also wiederholbar, denn ein faelschlich wiederholter Versuch
    kostet einen Anlauf, ein faelschlich endgueltiger ein Dokument.

    Beim VERBINDEN gibt es diesen SQLSTATE nicht: psycopg (gemessen an 3.3.3)
    baut den Fehler eines gescheiterten Verbindungsaufbaus nur aus dem Text der
    Meldung, ohne SQLSTATE — auch fuer „database … does not exist", „role … does
    not exist" und ein falsches Passwort. Ein Verbinden, das scheitert, ist hier
    deshalb immer `DatabaseError`, also wiederholbar. Am Text der Meldung wird
    bewusst nicht unterschieden (Entscheidung des Eigentuemers zu Mineroo#28):
    Er haengt an Version und Sprache des Servers. `ConfigurationError` kommt nur
    fuer einen Fehler mit SQLSTATE, also auf einer schon offenen Verbindung.
    """
    zustand = getattr(fehler, "sqlstate", None)
    if isinstance(zustand, str) and (zustand.startswith("28") or zustand == "3D000"):
        return ConfigurationError(f"{was}: Zugang taugt nicht ({zustand}): {fehler}")
    return DatabaseError(f"{was}: Datenbank antwortet nicht ({zustand}): {fehler}")


def connect(dsn: str) -> psycopg.Connection[Any]:
    """Oeffnet eine synchrone Verbindung fuer die Fassaden-Seite.

    Besitz: Der Aufrufer besitzt und schliesst sie.

    Zusicherung:
        `autocommit` ist AN. Das ist keine Bequemlichkeit: Jede Anweisung sieht
        so den neuesten Stand, und keine Lesung haelt eine Transaktion offen, die
        das naechste Lesen auf einem alten Schnappschuss festhielte. Wer eine
        Transaktion braucht, oeffnet sie ausdruecklich. Gewartet wird NICHT auf
        dieser Verbindung, sondern auf einer eigenen (`_warte_verbindung`).

    Raises:
        DatabaseError: Das Verbinden scheiterte — die Instanz antwortet nicht,
            aber ebenso eine fehlende Datenbank, eine fehlende Rolle oder ein
            falsches Passwort: psycopg gibt einem gescheiterten Verbindungsaufbau
            keinen SQLSTATE (`_verbindungsfehler_uebersetzen`). Ausdruecklich
            NICHT `ConfigurationError` fuer eine Stoerung — der Unterschied
            entscheidet ueber Geld: Wuerde hier `ConfigurationError` fliegen,
            machte ein einsekuendiger Postgres-Aussetzer beim Terminal-Schreiben
            aus einem bezahlten Dokument ein endgueltig gescheitertes.
    """
    return _verbinden(dsn, "Verbindung oeffnen")


async def connect_async(dsn: str) -> psycopg.AsyncConnection[Any]:
    """Oeffnet eine asynchrone Verbindung fuer die Daemon-Seite.

    Sonst wie `connect`, einschliesslich `autocommit`.
    """
    return await _verbinden_async(dsn, "Verbindung oeffnen")


def _verbinden(dsn: str, was: str, **angaben: Any) -> psycopg.Connection[Any]:
    """Das eine Verbinden hinter `connect` und `_warte_verbindung`: `autocommit`
    an, die uebrigen Angaben an psycopg, ein Fehler uebersetzt."""
    try:
        return psycopg.connect(dsn, autocommit=True, **angaben)
    except psycopg.Error as fehler:
        raise _verbindungsfehler_uebersetzen(fehler, was) from fehler


async def _verbinden_async(
    dsn: str, was: str, **angaben: Any
) -> psycopg.AsyncConnection[Any]:
    """Dasselbe wie `_verbinden`, asynchron."""
    try:
        return await psycopg.AsyncConnection.connect(dsn, autocommit=True, **angaben)
    except psycopg.Error as fehler:
        raise _verbindungsfehler_uebersetzen(fehler, was) from fehler


# --------------------------------------------------------------------------
# Zeilen anlegen und lesen — synchron, fuer die Fassade
# --------------------------------------------------------------------------

_SPALTEN_VOLL: Final = (
    "sha256, size_bytes, mime, page_count, status, "
    "raw, error, endpoint_id, image_tag_at_submit, notes, created_at, updated_at, "
    "true AS raw_loaded"
)
"""Die Spaltenliste fuer ein VOLLSTAENDIGES Lesen, inklusive `raw`.

Ausgeschrieben statt `SELECT *`, aus einem Grund, der genau hier greift:
`class_row(DocumentRow)` ordnet ueber die Spaltennamen zu, und ein `*` liefert
die Spalten in der Reihenfolge der Tabelle samt allem, was jemand spaeter
hinzufuegt. Eine unbekannte Spalte waere dann ein unerwartetes Schluesselwort
beim Erzeugen der Zeilenklasse — ein Fehler zur Laufzeit, an einer Stelle, die
mit der eigentlichen Aenderung nichts zu tun hat. Die ausgeschriebene Liste
bricht stattdessen dort, wo sie gepflegt gehoert, und `_schema_bewerten` sagt
es beim `setup` ohnehin vorher.

`true AS raw_loaded` faellt auf, ist aber der Kern: Es fuellt das gleichnamige
Feld direkt aus der Abfrage, statt es nachtraeglich zu setzen. Damit gibt es
EINE Zeilenklasse fuer beide Lesearten — die schmale Fassung unten sagt an
derselben Stelle `false`, und keine Aufrufstelle muss den Unterschied kennen."""

_SPALTEN_OHNE_RAW: Final = (
    "sha256, size_bytes, mime, page_count, status, "
    "NULL::jsonb AS raw, error, endpoint_id, image_tag_at_submit, notes, "
    "created_at, updated_at, false AS raw_loaded"
)
"""Dieselbe Liste ohne den teuren Teil — `raw` bleibt ungelesen und leer.

`NULL::jsonb AS raw` mit der Typangabe, damit die Spalte auch dann `jsonb`
heisst, wenn Postgres sonst `unknown` daraus machte; `false AS raw_loaded` ist
das Unterscheidungsmerkmal, ohne das „nicht geladen" und „ist leer" dasselbe
waeren."""


_EINGABE_SQL: Final = (
    "INSERT INTO inputs (sha256, content) VALUES (%s, %b) "
    "ON CONFLICT (sha256) DO NOTHING RETURNING sha256"
)
"""Die Anweisung von `insert_input` und `insert_input_async`."""

_KENNUNG_SQL: Final = "UPDATE inputs SET job_id = %s WHERE sha256 = %s"
"""Die Anweisung von `set_input_job` und `set_input_job_async`."""


def insert_input(
    conn: psycopg.Connection[Any], *, sha256: str, content: bytes
) -> bool:
    """Legt die Eingabe eines Dokuments ab, falls es zu diesem Hash noch keine gibt.

    Die Bytes liegen als `bytea` in `inputs`, bis das Dokument ein Endergebnis
    hat: verlustfrei wie base64, aber ohne das zusaetzliche Drittel auf der
    Platte (1,03-fach statt 1,37-fach). base64 war nur noetig, solange die Bytes
    im JSON der Job-Argumente stehen sollten; der Job traegt nur noch den Hash.

    Der Platzhalter ist `%b`, nicht `%s`: Er schickt den Wert im Binaerformat.
    Ohne ihn ginge ein grosses PDF als escapter Text ueber die Leitung.

    Das ist das ganze Abschicken auf Seiten der Datenbank: Eine Ergebniszeile
    legt erst der Job an. `ON CONFLICT (sha256) DO NOTHING` macht die Tabelle zur
    Sperre gegen ein zweites Einreihen: Schicken zwei Aufrufer gleichzeitig
    dieselben Bytes, laesst Postgres den zweiten am Primaerschluessel auf den
    ersten warten, und danach fuegt er nichts ein. Eine Pruefung im Programm
    haette zwischen Pruefen und Einfuegen ein Zeitfenster; die Datenbank hat
    keines.

    Args:
        conn: Offene Verbindung, gehoert dem Aufrufer.
        sha256: Roher Byte-Hash von `content`.
        content: Die vollstaendigen Bytes der Eingabe.

    Returns:
        True, wenn die Eingabe neu abgelegt wurde; False, wenn es zu diesem Hash
        schon eine gibt: Das Dokument ist dann unterwegs (oder gescheitert, und
        seine Eingabe steht fuer `retry`), und die vorhandene bleibt unberuehrt.

    Kosten: keine.
    """
    ergebnis = conn.execute(_EINGABE_SQL, (sha256, content)).fetchone()
    return ergebnis is not None


async def insert_input_async(
    conn: psycopg.AsyncConnection[Any], *, sha256: str, content: bytes
) -> bool:
    """Dasselbe wie `insert_input`, asynchron: fuer den `AsyncOCRClient`."""
    cur = await conn.execute(_EINGABE_SQL, (sha256, content))
    return await cur.fetchone() is not None


async def set_input_job_async(
    conn: psycopg.AsyncConnection[Any], *, sha256: str, job_id: int
) -> None:
    """Dasselbe wie `set_input_job`, asynchron: fuer den `AsyncOCRClient`."""
    await conn.execute(_KENNUNG_SQL, (job_id, sha256))


def set_input_job(conn: psycopg.Connection[Any], *, sha256: str, job_id: int) -> None:
    """Merkt an der Eingabe, welcher Procrastinate-Job das Dokument verarbeitet.

    Die Verbindung Dokument–Job: Abschicken schreibt die Kennung, die `defer`
    zurueckgibt, in derselben Transaktion wie Eingabe und Job; `retry` schreibt
    die Kennung des neuen Jobs, wenn es einen anlegt. Ueber sie findet
    `document()` den Job eines offenen Dokuments und `retry` den, den es
    wiederholt. Die Kennung steht nur hier, nicht am Job (der traegt nur den Hash
    als `lock`).

    Kosten: keine.
    """
    conn.execute(_KENNUNG_SQL, (job_id, sha256))


def lock_input(conn: psycopg.Connection[Any], sha256: str) -> bool:
    """Sperrt die Eingabe eines Dokuments bis zum Ende der Transaktion.

    Fuer `retry`, bevor es einen Job zuruecksetzt oder neu einreiht: Steht die
    Eingabe noch, kann sie bis zum Commit niemand loeschen (das Endschreiben
    eines Jobs wartet). Ist sie weg, wurde der Job fertig, seit `retry` las, und
    sein Ergebnis loeschte sie; zu wiederholen gibt es dann nichts.

    Returns:
        True, wenn die Eingabe steht und gesperrt ist; False, wenn es keine gibt.

    Kosten: keine.
    """
    return (
        conn.execute(
            "SELECT 1 FROM inputs WHERE sha256 = %s FOR UPDATE", (sha256,)
        ).fetchone()
        is not None
    )


RETRY_SPERRKLASSE: Final = 26
"""Die erste Haelfte des Schluessels der Advisory-Sperre von `retry_lock`; sie
trennt diese Sperren von allen anderen auf derselben Datenbank."""


@contextmanager
def retry_lock(conn: psycopg.Connection[Any], sha256: str) -> Iterator[None]:
    """Serialisiert `retry` je Dokument: eine Advisory-Sperre der Sitzung.

    Zwei gleichzeitige `retry` auf dasselbe Dokument laesen sonst beide denselben
    Stand (etwa „kein Job") und reihten beide ein; zwei Jobs, und scheiterte der
    erste, bezahlte der zweite noch einmal. Unter der Sperre liest der zweite erst,
    wenn der erste fertig ist, und sieht dessen Job.

    Eine Sperre der SITZUNG und keine der Transaktion: `retry` nimmt die
    `failed`-Zeile in einer eigenen Transaktion weg und setzt den Job danach auf
    einer anderen Verbindung zurueck (`retry_job_by_id`); die Sperre muss beide
    Schritte umfassen. Stirbt der Prozess, schliesst die Verbindung und gibt sie
    frei. Der Schluessel ist (`RETRY_SPERRKLASSE`, `hashtext(sha256)`); eine
    Kollision zweier Hashes laesst zwei Dokumente nur nacheinander laufen.

    Kosten: keine.
    """
    conn.execute(
        "SELECT pg_advisory_lock(%s, hashtext(%s))", (RETRY_SPERRKLASSE, sha256)
    )
    try:
        yield
    finally:
        conn.execute(
            "SELECT pg_advisory_unlock(%s, hashtext(%s))", (RETRY_SPERRKLASSE, sha256)
        )


def input_hashes(conn: psycopg.Connection[Any]) -> list[str]:
    """Die Hashes aller stehenden Eingaben, geordnet: jedes Dokument ohne
    gueltiges Ergebnis (gescheitert oder offen). Ohne `content`.

    Kosten: keine.
    """
    return [
        zeile[0]
        for zeile in conn.execute("SELECT sha256 FROM inputs ORDER BY sha256").fetchall()
    ]


def delete_failed_result(conn: psycopg.Connection[Any], sha256: str) -> None:
    """Nimmt ein Ergebnis `failed` weg, und nur dieses — fuer `retry`.

    Danach ist das Dokument wieder offen (Eingabe da, kein Ergebnis), und jeder,
    der wartet, wartet auf den neuen Lauf, in jedem Prozess. Ein gueltiges
    Ergebnis (`done`, `empty`, `input_error`) bleibt unberuehrt, auch wenn es
    zwischen Nachsehen und Wegnehmen entstanden ist.

    Kosten: keine.
    """
    conn.execute(
        "DELETE FROM documents WHERE sha256 = %s AND status = 'failed'", (sha256,)
    )


async def read_input_async(dsn: str, sha256: str) -> bytes | None:
    """Liest die Eingabe eines Dokuments auf einer EIGENEN, kurzlebigen Verbindung.

    Warum nicht auf einer Verbindung aus dem Pool: libpq behaelt seine Puffer in
    der Groesse des groessten je gelesenen Werts, bis die Verbindung schliesst
    (gemessen: 64 MiB nach einem Wert von 50 MB). Im Pool des Daemons mit 24
    Verbindungen bande das ein Gigabyte. Eine eigene Verbindung kostet rund 5 ms
    und wird nach dem Lesen geschlossen.

    Gelesen wird mit `binary=True`. Ohne geht der Wert als Hex ueber die Leitung,
    doppelt so gross, und ab rund 536 MB scheitert der Server.

    Returns:
        Die Bytes — immer `bytes`, nie ein `memoryview`, denn pypdfium2 nimmt
        ein `memoryview` nicht an. `None`, wenn es zu diesem Hash keine Eingabe
        gibt; was das heisst, entscheidet der Aufrufer.

    Raises:
        DatabaseError: Die Instanz antwortet nicht, oder das Verbinden scheiterte
            aus anderem Grund (ohne SQLSTATE, `_verbindungsfehler_uebersetzen`).
        ConfigurationError: Ein Zugangsfehler mit SQLSTATE beim Lesen.

    Kosten: keine.
    """
    conn = await connect_async(dsn)
    try:
        try:
            cur = await conn.execute(
                "SELECT content FROM inputs WHERE sha256 = %s",
                (sha256,),
                binary=True,
            )
            zeile = await cur.fetchone()
        except psycopg.Error as fehler:
            raise _verbindungsfehler_uebersetzen(fehler, "Eingabe lesen") from fehler
    finally:
        await conn.close()
    if zeile is None:
        return None
    inhalt = zeile[0]
    return inhalt if type(inhalt) is bytes else bytes(inhalt)


def get_document(
    conn: psycopg.Connection[Any], sha256: str, *, with_raw: bool = True
) -> DocumentRow | None:
    """Liest eine Zeile, standardmaessig mit `raw`. None, wenn es sie nicht gibt.

    Mit `with_raw=False` bleibt `raw` ungelesen (`raw_loaded = False`), wie bei
    `list_documents`. Das ist der Weg fuer alles, was nur den Zustand braucht:
    Das Abschicken eines schon fertigen Dokuments soll nicht dessen ganze
    Antwort lesen, und ein erneuter Lauf ueber 30.000 fertige Dokumente zoege
    sonst rund 34 GB. Wer das Ergebnis dann doch will, bekommt es von der Huelle
    nachgeladen.

    Die Zuordnung Spalte-zu-Feld laeuft ueber `psycopg.rows.class_row(DocumentRow)`
    — ueber die SPALTENNAMEN aus der Cursor-Beschreibung, nicht ueber die
    Position. Bei vierzehn Feldern ist ein Positionsfehler nach einer eingefuegten
    Spalte sonst unvermeidlich und still: Er verschoebe `error` nach
    `endpoint_id` und saehe auf den ersten Blick plausibel aus. Nebenbei bekommt
    die Typpruefung damit einen getippten Cursor statt einer Umtypung je
    Lesefunktion.

    Der `status` bleibt dabei Handarbeit: Die Spalte ist `text` mit einer
    CHECK-Bedingung, kein Postgres-Aufzaehlungstyp — `class_row` reicht die
    Zeichenkette durch, und `DocumentStatus` entsteht daraus in
    `DocumentRow.__post_init__`.
    """
    with conn.cursor(row_factory=class_row(DocumentRow)) as cur:
        return cur.execute(_zeile_sql(with_raw), (sha256,)).fetchone()


def _zeile_sql(with_raw: bool) -> str:
    """Die Anweisung von `get_document` und `get_document_async`."""
    spalten = _SPALTEN_VOLL if with_raw else _SPALTEN_OHNE_RAW
    return f"SELECT {spalten} FROM documents WHERE sha256 = %s"


async def get_document_async(
    conn: psycopg.AsyncConnection[Any], sha256: str, *, with_raw: bool = True
) -> DocumentRow | None:
    """Dasselbe wie `get_document`, asynchron: fuer den `AsyncOCRClient`.

    Raises:
        DatabaseError: Die Verbindung taugt nicht mehr (etwa nach einem Neustart
            von Postgres) — uebersetzt wie jeder Verbindungsfehler
            (`_verbindungsfehler_uebersetzen`), nicht als roher psycopg-Fehler.
    """
    try:
        async with conn.cursor(row_factory=class_row(DocumentRow)) as cur:
            await cur.execute(_zeile_sql(with_raw), (sha256,))
            return await cur.fetchone()
    except psycopg.Error as fehler:
        raise _verbindungsfehler_uebersetzen(fehler, "Ergebnis lesen") from fehler


def list_documents(
    conn: psycopg.Connection[Any],
    *,
    status: DocumentStatus | None = None,
    limit: int | None = None,
) -> list[DocumentRow]:
    """Listet Zeilen, wahlweise nach Zustand gefiltert.

    Zusicherung:
        `raw` wird NICHT mitgeladen. Bei dreissigtausend Dokumenten waeren das
        Gigabyte, und keine Liste braucht sie.

        Damit „nicht geladen" nicht mit „ist leer" verwechselt werden kann,
        traegt jede so gelesene Zeile `raw_loaded = False`. Ohne diese
        Unterscheidung liefert eine Huelle aus einer gelisteten Zeile fuer ein
        FERTIGES Dokument einen leeren Text — und das ist der Fehler, den niemand
        bemerkt, weil er wie ein leeres Ergebnis aussieht. Die Ergebnis-Getter der
        Huelle laden bei `raw_loaded is False` die Zeile einzeln nach, statt zu
        raten.

        Gelesen wird dafuer mit derselben Zeilenklasse, aber einer schmalen
        Spaltenliste: `NULL::jsonb AS raw, false AS raw_loaded` fuellt die beiden
        Felder direkt aus der Abfrage. Der Vertrag sah hier urspruenglich eine
        eigene zweite Zeilenklasse vor — die waere eine zweite Lesart derselben
        Tabelle gewesen, genau das, wogegen der Modul-Vorspann steht, und sie
        muesste bei jeder Spaltenaenderung mitgepflegt werden.
    """
    bedingungen: list[str] = []
    werte: list[Any] = []
    if status is not None:
        bedingungen.append("status = %s")
        werte.append(status.value)

    sql = f"SELECT {_SPALTEN_OHNE_RAW} FROM documents"
    if bedingungen:
        sql += " WHERE " + " AND ".join(bedingungen)
    sql += " ORDER BY created_at"
    if limit is not None:
        sql += " LIMIT %s"
        werte.append(limit)

    with conn.cursor(row_factory=class_row(DocumentRow)) as cur:
        return cur.execute(sql, tuple(werte)).fetchall()


def counts_by_status(conn: psycopg.Connection[Any]) -> dict[DocumentStatus, int]:
    """Zaehlt die Ergebniszeilen je Endzustand. Grundlage von `status()`.

    Zusicherung:
        Jeder Zustand kommt im Ergebnis vor, auch mit Null. Ein fehlender
        Schluessel und eine Null sind fuer einen Leser dasselbe, fuer den Code
        aber nicht — und ein `KeyError` in einer Statusausgabe ist besonders
        aergerlich, weil er genau dann kommt, wenn alles gut lief.
    """
    zaehlung = {zustand: 0 for zustand in DocumentStatus}
    for name, anzahl in conn.execute(
        "SELECT status, count(*) FROM documents GROUP BY status"
    ).fetchall():
        zaehlung[DocumentStatus(name)] = anzahl
    return zaehlung


def count_open(conn: psycopg.Connection[Any]) -> int:
    """Zaehlt die offenen Dokumente: Eingabe da, kein Ergebnis.

    Nicht einfach die Zeilen von `inputs`: Bei `failed` bleibt die Eingabe
    stehen, damit `retry` etwas zu lesen hat, und ein gescheitertes Dokument hat
    ein Ergebnis. Es zaehlt unter `failed`, nicht zweimal.

    Kosten: keine.
    """
    zeile = conn.execute(
        "SELECT count(*) FROM inputs i "
        "WHERE NOT EXISTS (SELECT 1 FROM documents d WHERE d.sha256 = i.sha256)"
    ).fetchone()
    return int(zeile[0]) if zeile is not None else 0


_ZEILENFELDER: Final = tuple(feld.name for feld in fields(DocumentRow) if feld.name != "sha256")
"""Die Felder von `DocumentRow` ausser dem Hash, den `read_states` aus der
gefragten Liste nimmt: Bei einem offenen Dokument sind alle anderen NULL."""

_STAND_SQL: Final = (
    "SELECT h.sha256, (i.sha256 IS NOT NULL) AS has_input, "
    "i.job_id AS input_job_id, "
    + ", ".join(
        "NULL::jsonb AS raw" if name == "raw"
        else "false AS raw_loaded" if name == "raw_loaded"
        else f"d.{name}"
        for name in _ZEILENFELDER
    )
    + " FROM unnest(%s::text[]) WITH ORDINALITY AS h(sha256, nr) "
    "LEFT JOIN documents d ON d.sha256 = h.sha256 "
    "LEFT JOIN inputs i ON i.sha256 = h.sha256 "
    "ORDER BY h.nr"
)
"""Die EINE Anweisung, die `documents` und `inputs` zugleich liest — fuer
`read_states` und `read_states_async`, also fuer Nachsehen und Warten, sync wie
async. Ohne `raw` und ohne `content`: Aus `inputs` kommen nur, ob es die Zeile
gibt, und die Kennung ihres Jobs (`inputs.job_id`)."""


def _stand_aus_zeilen(zeilen: Sequence[dict[str, Any]]) -> list[DocumentState]:
    """Macht aus den Zeilen von `_STAND_SQL` je Hash einen `DocumentState`."""
    stand: list[DocumentState] = []
    for zeile in zeilen:
        ergebnis = (
            None
            if zeile["status"] is None
            else DocumentRow(
                sha256=zeile["sha256"], **{name: zeile[name] for name in _ZEILENFELDER}
            )
        )
        stand.append(
            DocumentState(
                sha256=zeile["sha256"],
                row=ergebnis,
                has_input=zeile["has_input"],
                job_id=zeile["input_job_id"],
            )
        )
    return stand


def read_states(
    conn: psycopg.Connection[Any], hashes: Sequence[str]
) -> list[DocumentState]:
    """Was die Datenbank ueber diese Dokumente weiss, in EINER Anweisung.

    `documents` und `inputs` werden in derselben Anweisung gelesen, nicht
    nacheinander. Zwei Abfragen in der Reihenfolge erst `documents`, dann
    `inputs` meldeten ein Dokument, dessen Job genau dazwischen fertig wird,
    faelschlich als unbekannt: Die erste saehe noch kein Ergebnis, die zweite
    keine Eingabe mehr. Eine Anweisung liest beide Tabellen in demselben
    Schnappschuss.

    Ohne `raw` und ohne `content`: Wer wartet oder nachsieht, braucht den Stand,
    nicht die Antwort und nicht die Bytes. Procrastinate wird nicht gefragt.

    Args:
        conn: Offene Verbindung, gehoert dem Aufrufer.
        hashes: Die gefragten Hashes, in beliebiger Zahl.

    Returns:
        Je gefragtem Hash ein `DocumentState`, in der Reihenfolge der Frage.

    Kosten: keine.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        zeilen = cur.execute(_STAND_SQL, (list(hashes),)).fetchall()
    return _stand_aus_zeilen(zeilen)


async def read_states_async(
    conn: psycopg.AsyncConnection[Any], hashes: Sequence[str]
) -> list[DocumentState]:
    """Dasselbe wie `read_states`, asynchron: dieselbe EINE Anweisung."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_STAND_SQL, (list(hashes),))
        zeilen = await cur.fetchall()
    return _stand_aus_zeilen(zeilen)


# --------------------------------------------------------------------------
# Lesen und schreiben — asynchron, ausschliesslich der Task
# --------------------------------------------------------------------------


async def get_status_async(
    conn: psycopg.AsyncConnection[Any], sha256: str
) -> DocumentStatus | None:
    """Der Endzustand eines Dokuments fuer den Executor, None ohne Ergebnis.

    Nur der Zustand, nie `raw`: Der Executor braucht ihn fuer das Nachsehen am
    Anfang, und `raw` ist bei einem verarbeiteten Dokument zweistellige Megabyte
    gross. Es mitzuladen hiesse, bei jedem Wiederlauf eines fertigen Dokuments
    das ganze Ergebnis durch die Leitung zu ziehen, um es sofort wegzuwerfen.

    Warum es diese Funktion ueberhaupt gibt und der Executor nicht selbst fragt:
    Dieses Modul ist der einzige Ort im Paket, an dem SQL auf `documents` steht
    (siehe Modul-Vorspann).

    Kosten: keine.
    """
    zeile = await (
        await conn.execute("SELECT status FROM documents WHERE sha256 = %s", (sha256,))
    ).fetchone()
    return None if zeile is None else DocumentStatus(zeile[0])


async def delete_input_async(conn: psycopg.AsyncConnection[Any], sha256: str) -> None:
    """Loescht die Eingabe eines Dokuments, falls sie noch da ist.

    Fuer den Job, der ein Ergebnis `done`, `empty` oder `input_error` vorfindet:
    Dann ist die Eingabe zu nichts mehr gut. Das geschieht, wenn ein Abschicken
    die Eingabe genau in dem Augenblick neu anlegte, in dem ein Job das Ergebnis
    schrieb; der neue Job raeumt sie weg, statt ein zweites Mal zu bezahlen.

    Kosten: keine.
    """
    await conn.execute("DELETE FROM inputs WHERE sha256 = %s", (sha256,))


async def write_terminal_async(
    conn: psycopg.AsyncConnection[Any],
    *,
    sha256: str,
    status: DocumentStatus,
    raw: dict[str, Any],
    endpoint_id: str,
    mime: str | None = None,
    size_bytes: int | None = None,
    image_tag_at_submit: str | None = None,
    error: str | None = None,
    notes: Sequence[dict[str, Any]] | None = None,
    page_count: int | None = None,
) -> None:
    """Legt das Ergebnis eines Durchlaufs an und weckt alle Warter.

    Das ist der EINZIGE Schreibvorgang an `documents`. Die Ergebniszeile
    entsteht hier, am Ende des Jobs, und mit ihr in EINER Transaktion: `raw`,
    `status`, `error`, die Angaben zur Eingabe (`mime`, `size_bytes`,
    `page_count`), `notes`, das Loeschen der Eingabe aus `inputs` (ausser bei
    `failed`, damit `retry` etwas zu lesen hat) und das Wecksignal. Es gibt
    keinen Zwischenzustand, in dem das Ergebnis steht und die Eingabe noch da
    ist, und keinen, in dem keins von beiden da ist.

    Args:
        conn: Offene asynchrone Verbindung, gehoert dem Aufrufer.
        sha256: Primaerschluessel.
        status: Der Endzustand.
        raw: Das zusammengesetzte Gesamtergebnis auf Dokument-Ebene, wie der
            Executor es gebildet hat (siehe SCHEMA_SQL). Bei `failed` und
            `input_error` dasselbe Objekt mit dem, was bis dahin ankam, samt
            Fehler-Umschlag — auch eine Fehlerantwort ist bezahlt und gehoert
            aufgehoben. Geschrieben wird es als `psycopg.types.json.Jsonb(raw)`:
            Ohne diesen Wrapper wird eine Abbildung nicht als `jsonb` angepasst,
            sondern als Verbund- oder Feldtyp — und das ist die Spalte mit dem
            bezahlten Ergebnis, nicht die Stelle zum Ausprobieren.
        endpoint_id: An welchem Endpoint gelesen wurde (aus der Einstellung des
            Daemons).
        mime: Der Typ, den der Job aus den Bytes bestimmt hat; None, wenn er
            sich nicht bestimmen liess.
        size_bytes: Die Groesse der Eingabe; None, wenn sie fehlte oder
            beschaedigt war.
        image_tag_at_submit: Welches Worker-Image lief. Der Executor liest es
            einmal je Lauf und reicht es hier durch. Ohne diesen Weg bliebe die
            Spalte fuer immer leer, und die Frage, fuer die sie da ist („war das
            noch das alte Image?"), waere nach Wochen nicht mehr entscheidbar.
        error: Kurzform bei `failed`/`input_error`, sonst None.
        notes: Anzuhaengende Ereignisse. None haengt nichts an; die Liste wird
            ANGEHAENGT, nie ersetzt.
        page_count: Die Seitenzahl, die der Job aus den Bytes bestimmt hat (nur
            bei PDF).

    Zusicherung:
        Nach der Rueckkehr steht ein Ergebnis da, und ein `NOTIFY` auf dem Kanal
        `doc_done` mit dem sha256 als Nutzlast ist abgesetzt — auch bei
        `failed` und `input_error`. Das ist wichtiger, als es aussieht: Weckte
        das Signal nur bei Erfolg, liefe jeder Warter auf ein gescheitertes
        Dokument bis zum Zeitablauf, und der Aufrufer wuesste am Ende nicht, ob
        es scheiterte oder nur lange dauert.

        NICHT zugesichert ist, dass ein Warter das Signal empfaengt — seine
        Verbindung kann in diesem Augenblick abgerissen sein, oder er lauscht
        noch nicht. Deshalb lauscht `wait_for_terminal` zuerst und liest danach,
        liest nach jedem Neuverbinden alles Offene neu und im Takt ohnehin.

    Idempotenz und die eine erlaubte Ueberschreibung:
        Gibt es noch keine Zeile, wird sie angelegt.

        Steht sie auf `failed`, wird sie UEBERSCHRIEBEN, und das ist gewollt: Ein
        per `retry_job_by_id` wieder vorgelegter Job verarbeitet das Dokument
        wirklich neu und schreibt sein Ergebnis (Plan 4.2). Genau deshalb deckt
        das Nachsehen am Anfang des Executors `failed` ausdruecklich nicht ab.
        `created_at` bleibt dabei, `updated_at` wird neu; `mime` und `size_bytes`
        bleiben, wo dieser Lauf sie nicht bestimmen konnte.

        Steht sie auf `done`, `empty` oder `input_error`, entscheidet der
        ZUSTAND, und nur er: Ist der uebergebene Zustand derselbe, bleibt das
        Ergebnis, wie es ist — insbesondere wird `raw` NICHT ersetzt, der erste
        Schreibvorgang gewinnt; angehaengt werden nur die Vermerke, und die
        Eingabe geht. Ist er ein anderer, fliegt `TerminalOverwriteError`.

        Warum der Zustand und nicht der Inhalt der Vergleichsmassstab ist: `raw`
        traegt Job-Kennungen und Laufzeiten, ist also bei zwei Laeufen nie gleich —
        ein Inhaltsvergleich waere immer „abweichend" und wuerde den harmlosen
        Wiederlauf nach einem Absturz im Schreibfenster zum Fehler machen. Und er
        waere teuer: zweistellige Megabyte je Dokument.

        Ein fertiges Dokument wird also nie ueberschrieben, auf keinem Weg. Die
        Regel steht an dieser einen Stelle, weil sie eine Eigenschaft der TABELLE
        ist und keine des Aufrufers — ein zweiter Schreibweg mit anderer Regel
        waere die Luecke, durch die ein bezahltes Ergebnis verschwindet.

    Nebenlaeufigkeit:
        Angelegt wird mit `INSERT … ON CONFLICT DO NOTHING`; gibt es die Zeile
        schon, wird sie gesperrt (`SELECT … FOR UPDATE`). Zwei Tasks auf
        demselben Hash koennen sich so nicht verschraenken: Der zweite wartet am
        Primaerschluessel auf den ersten und sieht danach dessen Ergebnis. Es
        SOLLTE ohnehin nie zwei geben (`lock=sha256` beim Einreihen) — die
        Sperre ist der Guertel zum Hosentraeger.

    Raises:
        TerminalOverwriteError: siehe Idempotenz.

    Kosten: keine.
    """
    vermerke = Jsonb(list(notes) if notes else [])
    async with conn.transaction():
        angelegt = await (
            await conn.execute(
                """
                INSERT INTO documents (
                    sha256, size_bytes, mime, page_count, status, raw, error,
                    endpoint_id, image_tag_at_submit, notes
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (sha256) DO NOTHING
                RETURNING sha256
                """,
                (
                    sha256,
                    size_bytes,
                    mime,
                    page_count,
                    status.value,
                    Jsonb(raw),
                    error,
                    endpoint_id,
                    image_tag_at_submit,
                    vermerke,
                ),
            )
        ).fetchone()

        if angelegt is None:
            # Es gibt die Zeile schon. `FOR UPDATE` sperrt sie bis zum Ende dieser
            # Transaktion; der Zustand, den diese Anweisung liest, ist der
            # festgeschriebene (READ COMMITTED liest je Anweisung neu).
            zeile = await (
                await conn.execute(
                    "SELECT status FROM documents WHERE sha256 = %s FOR UPDATE",
                    (sha256,),
                )
            ).fetchone()
            vorher = DocumentStatus(zeile[0]) if zeile is not None else None
            if vorher is DocumentStatus.FAILED:
                await conn.execute(
                    """
                    UPDATE documents
                       SET status = %s,
                           raw = %s,
                           error = %s,
                           endpoint_id = %s,
                           mime = COALESCE(%s, mime),
                           size_bytes = COALESCE(%s, size_bytes),
                           image_tag_at_submit = COALESCE(%s, image_tag_at_submit),
                           page_count = COALESCE(%s, page_count),
                           notes = notes || %s::jsonb,
                           updated_at = now()
                     WHERE sha256 = %s
                    """,
                    (
                        status.value,
                        Jsonb(raw),
                        error,
                        endpoint_id,
                        mime,
                        size_bytes,
                        image_tag_at_submit,
                        page_count,
                        vermerke,
                        sha256,
                    ),
                )
            elif vorher is status:
                # Derselbe Endzustand ein zweites Mal: Der erste Schreibvorgang
                # gewinnt, `raw`, `status` und `updated_at` bleiben. Die Vermerke
                # werden trotzdem angehaengt: Genau in diesem Fall ist die
                # Beobachtung, DASS es einen zweiten Lauf gab, die einzige Spur
                # davon, und unsichtbare Wiederlaeufe sind bezahlte Wiederlaeufe.
                if notes:
                    await conn.execute(
                        "UPDATE documents SET notes = notes || %s::jsonb "
                        "WHERE sha256 = %s",
                        (vermerke, sha256),
                    )
            else:
                raise TerminalOverwriteError(
                    f"{sha256} steht bereits endgueltig auf "
                    f"{vorher.value if vorher else '(geloescht)'!r} und soll auf "
                    f"{status.value!r} geaendert werden. Ein fertiges Dokument "
                    f"wird nie ueberschrieben. Der einzige Zustand, der neu "
                    f"verarbeitet werden darf, ist `failed` — dafuer gibt es "
                    f"retry_job_by_id."
                )

        # Die Eingabe geht mit dem Ergebnis, in DERSELBEN Transaktion: Beides
        # gilt oder beides nicht. Bei `failed` bleibt sie stehen, damit `retry`
        # etwas zu lesen hat. Entfernt wird per `DELETE`; fuer den Platz ist das
        # gleich wie ein Leeren des Felds, und Autovacuum gibt ihn frei.
        if status is not DocumentStatus.FAILED:
            await conn.execute("DELETE FROM inputs WHERE sha256 = %s", (sha256,))
        # Das Wecksignal gehoert in DIESELBE Transaktion: Wuerde es davor
        # abgesetzt, koennte ein Warter aufwachen und noch kein Ergebnis
        # vorfinden; danach abgesetzt, faellt es bei einem Abbruch zwischen
        # beidem ganz aus. Postgres stellt NOTIFY ohnehin erst beim Commit zu.
        await conn.execute("SELECT pg_notify(%s, %s)", (DONE_CHANNEL, sha256))


# --------------------------------------------------------------------------
# Warten
# --------------------------------------------------------------------------


WAIT_APPLICATION_NAME: Final = "mineru-ocr wait"
"""Der `application_name` jeder Warte-Verbindung.

Damit sieht man in `pg_stat_activity`, wie viele Warter gerade an der Datenbank
haengen und welche Verbindung das ist — je Warte-Aufruf genau eine, neben der
Verbindung des Clients."""

NEUVERBINDEN_PAUSE_SEKUNDEN: Final = 0.5
"""Wie lange die Schleife vor jedem neuen Verbindungsversuch wartet: nach einem
Abbruch der Verbindung ebenso wie nach einem gescheiterten Neuverbinden (die
Instanz startet gerade neu). Auch nach einem Abbruch, weil eine Verbindung, die
gleich bei ihrer ersten Lesung wieder abbricht (etwa an einem
`statement_timeout`), sonst Hunderte Verbindungen je Sekunde oeffnete. Signale
aus der Pause heilt das erneute Lesen. Die Frist kuerzt sie, und ist sie
abgelaufen, endet das Warten statt des naechsten Versuchs."""


def check_wait_timeout(timeout: float | None) -> None:
    """Prueft eine Frist fuers Warten, an EINER Stelle fuer Schleife, Client und
    CLI.

    Raises:
        ValueError: Die Frist ist `nan` (sie liefe nie ab, denn jeder Vergleich
            damit ist falsch) oder negativ (ein Irrtum des Aufrufers). None heisst
            unbegrenzt, `inf` ebenso.
    """
    if timeout is not None and not timeout >= 0:
        raise ValueError(
            f"Die Frist muss eine Zahl von Sekunden ab null sein oder None "
            f"(unbegrenzt), nicht {timeout!r}."
        )


class _Warteplan:
    """Der Kern der Warteschleife, ohne Ein- und Ausgabe.

    Er weiss, welche Dokumente noch offen sind, was als Naechstes gelesen wird
    und wie lange auf Signale gewartet wird. Die beiden Schalen
    (`wait_for_terminal` und `wait_for_terminal_async`) tun nur noch das, was
    sync und async verschieden ist: verbinden, lauschen, lesen, schlafen. So kann
    keine der beiden still anders entscheiden als die andere.
    """

    def __init__(
        self, hashes: Sequence[str], timeout: float | None, poll_seconds: float
    ) -> None:
        if isinstance(hashes, str):
            # Ein Hash ist selbst eine Folge von Zeichen; als Folge von Hashes
            # gelesen, wuerde auf vierundsechzig einzelne Zeichen gewartet.
            raise TypeError(
                "Erwartet wird eine Folge von Hashes, nicht ein einzelner Hash als "
                "Zeichenkette: `[sha256]` statt `sha256`."
            )
        check_wait_timeout(timeout)
        self.offen: dict[str, None] = dict.fromkeys(hashes)
        """Die offenen Hashes in der Reihenfolge der Frage, jeder einmal."""
        self.frist = timeout
        self.ende = None if timeout is None else time.monotonic() + timeout
        self.takt = poll_seconds
        self.naechste_vollabfrage = 0.0
        self.angefangen = False
        """Ob schon einmal gelesen wurde. Davor ist ein Verbindungsfehler ein
        Betriebsfehler des Aufrufs (Postgres fehlt), danach ein Abbruch, der
        durch Neuverbinden heilt."""

    def alle(self) -> list[str]:
        """Alle offenen, zum Lesen; der Takt beginnt von vorn."""
        self.naechste_vollabfrage = time.monotonic() + self.takt
        return list(self.offen)

    def gemeldet(self, nutzlasten: Iterable[str]) -> list[str]:
        """Die gemeldeten Hashes, die noch offen sind. Die Signale ALLER
        Dokumente kommen auf demselben Kanal; die fremden fallen hier heraus."""
        gemeldet = set(nutzlasten)
        return [sha256 for sha256 in self.offen if sha256 in gemeldet]

    def auswerten(self, stand: Sequence[DocumentState]) -> list[DocumentRow]:
        """Nimmt die fertigen aus den offenen und gibt ihre Zeilen zurueck.

        Raises:
            DocumentNotFound: Ein gelesenes Dokument hat weder Ergebnis noch
                Eingabe.
        """
        self.angefangen = True
        unbekannt = [s.sha256 for s in stand if s.is_unknown]
        if unbekannt:
            raise DocumentNotFound(
                f"Weder Ergebnis noch Eingabe zu {', '.join(unbekannt)}. Auf ein "
                f"Dokument zu warten, das nie abgeschickt wurde, ist immer ein "
                f"Irrtum des Aufrufers — deshalb kommt die Antwort jetzt und nicht "
                f"nach Ablauf der Wartezeit."
            )
        fertig: list[DocumentRow] = []
        for s in stand:
            if s.row is not None and s.sha256 in self.offen:
                del self.offen[s.sha256]
                fertig.append(s.row)
        return fertig

    def abgelaufen(self) -> bool:
        """Ob die Frist abgelaufen ist. Dann lesen die Schalen ein letztes Mal alle
        offenen und rufen `frist_melden`: Was inzwischen fertig ist, ist ein
        Ergebnis und kein offenes Dokument — auch wenn der Aufrufer sich beim
        vorigen so lange aufhielt, dass es ohne Signal-Lesung fertig wurde."""
        return self.ende is not None and time.monotonic() >= self.ende

    def frist_melden(self) -> None:
        """Wirft den Zeitablauf, wenn noch Dokumente offen sind.

        Raises:
            WaitTimeoutError: Er nennt die offenen in der Reihenfolge der Frage.
        """
        if not self.offen:
            return
        offen = tuple(self.offen)
        raise WaitTimeoutError(
            f"{len(offen)} Dokument(e) hatten innerhalb von {self.frist} "
            f"Sekunden kein Ergebnis: {', '.join(offen)}. Sie koennen trotzdem "
            f"noch fertig werden — der Zeitablauf sagt etwas ueber die Geduld "
            f"des Wartenden, nichts ueber die Dokumente.",
            pending=offen,
            timeout=self.frist,
        )

    def wartezeit(self) -> float | None:
        """Wie lange auf Signale gewartet wird: bis zum naechsten Takt, hoechstens
        bis zur Frist. None heisst: Der Takt ist faellig, alle offenen lesen. Ob
        die Frist schon ablief, fragt vorher `abgelaufen`."""
        jetzt = time.monotonic()
        bis_takt = self.naechste_vollabfrage - jetzt
        if bis_takt <= 0:
            return None
        if self.ende is None:
            return bis_takt
        return max(0.0, min(bis_takt, self.ende - jetzt))

    def verbindungsfrist(self) -> int | None:
        """Der `connect_timeout` fuer den naechsten Verbindungsversuch: der Rest
        der Frist in ganzen Sekunden, oder None ohne Frist (dann gilt, was die
        Verbindungszeichenkette sagt). Ohne sie haenge ein Verbinden zu einem
        Rechner, der gar nicht antwortet, bis zur Voreinstellung des
        Betriebssystems, gleich welche Frist der Aufrufer setzte. psycopg hebt
        Werte unter zwei Sekunden auf zwei an. Eine unendliche Frist ist keine
        Grenze und gibt deshalb auch None."""
        if self.ende is None or math.isinf(self.ende):
            return None
        return max(1, math.ceil(self.ende - time.monotonic()))

    def pause(self) -> float:
        """Die Pause vor dem naechsten Verbindungsversuch, von der Frist gekuerzt.

        Raises:
            WaitTimeoutError: Die Frist ist abgelaufen. Dann wird nicht mehr
                verbunden; gelesen werden kann ohne Verbindung nichts mehr.
        """
        if self.abgelaufen():
            self.frist_melden()
        if self.ende is None:
            return NEUVERBINDEN_PAUSE_SEKUNDEN
        return max(0.0, min(NEUVERBINDEN_PAUSE_SEKUNDEN, self.ende - time.monotonic()))


def _lauschen_sql() -> psycopg.sql.Composed:
    """`LISTEN doc_done`, mit dem Kanal als Bezeichner gequotet."""
    return psycopg.sql.SQL("LISTEN {}").format(psycopg.sql.Identifier(DONE_CHANNEL))


def _warte_verbindung(
    dsn: str, *, connect_timeout: int | None = None
) -> psycopg.Connection[Any]:
    """Oeffnet die EIGENE Verbindung eines sync-Warte-Aufrufs.

    `autocommit`, sonst kaemen die Signale erst nach einer Transaktion an. Nie aus
    einem Pool, denn ein Pool hebt kein `LISTEN` auf; nie die Verbindung des
    Clients, denn dort wuechse der Rueckstau an Signalen ueber einen ganzen Lauf.
    `connect_timeout` ist der Rest der Frist (`_Warteplan.verbindungsfrist`).

    Raises:
        DatabaseError: Das Verbinden scheiterte. Das schliesst eine fehlende
            Datenbank oder Rolle ein, denn psycopg gibt dem gescheiterten
            Verbindungsaufbau keinen SQLSTATE (`_verbindungsfehler_uebersetzen`).
    """
    return _verbinden(
        dsn, "Warte-Verbindung oeffnen", **_warte_angaben(dsn, connect_timeout)
    )


async def _warte_verbindung_async(
    dsn: str, *, connect_timeout: int | None = None
) -> psycopg.AsyncConnection[Any]:
    """Dasselbe wie `_warte_verbindung`, asynchron."""
    return await _verbinden_async(
        dsn, "Warte-Verbindung oeffnen", **_warte_angaben(dsn, connect_timeout)
    )


def _warte_angaben(dsn: str, connect_timeout: int | None) -> dict[str, Any]:
    """Was eine Warte-Verbindung von anderen unterscheidet: ihr Name in
    `pg_stat_activity` und, mit Frist, die Grenze fuers Verbinden.

    Nennt die Verbindungszeichenkette schon einen kuerzeren `connect_timeout`,
    bleibt der: Eine Angabe neben der Zeichenkette ginge bei psycopg vor, und
    eine lange Frist machte sonst aus fuenf Sekunden je Versuch zehn Minuten.
    """
    angaben: dict[str, Any] = {"application_name": WAIT_APPLICATION_NAME}
    if connect_timeout is None:
        return angaben
    try:
        vorgabe = int(psycopg.conninfo.conninfo_to_dict(dsn).get("connect_timeout") or 0)
    except (psycopg.Error, ValueError, TypeError):
        # Eine Zeichenkette, die sich nicht lesen laesst, meldet das Verbinden
        # selbst; hier bleibt es beim Rest der Frist.
        vorgabe = 0
    angaben["connect_timeout"] = min(connect_timeout, vorgabe) if vorgabe > 0 else connect_timeout
    return angaben


def _abbruch_einordnen(fehler: psycopg.Error, plan: _Warteplan) -> MineruOcrError | None:
    """Entscheidet, ob nach einem Fehler auf der Warte-Verbindung neu verbunden wird.

    None heisst: neu verbinden und neu anfangen. Das gilt nur fuer einen
    `OperationalError`, der wiederholbar ist (kein Zugangsfehler), und erst, wenn
    schon einmal gelesen wurde; sonst kommt der uebersetzte Fehler zurueck, und
    der Aufrufer wirft ihn.
    """
    uebersetzt = _verbindungsfehler_uebersetzen(fehler, "Warten")
    if (
        isinstance(fehler, psycopg.OperationalError)
        and isinstance(uebersetzt, DatabaseError)
        and plan.angefangen
    ):
        return None
    return uebersetzt


def wait_for_terminal(
    dsn: str,
    hashes: Sequence[str],
    *,
    timeout: float | None = None,
    poll_seconds: float = DEFAULT_WAIT_POLL_SECONDS,
) -> Generator[DocumentRow]:
    """Wartet auf das Ergebnis vieler Dokumente und liefert jedes, sobald es da ist.

    Das ist Jurgens „awaiten des resultats" als eigene Funktion (Plan 2.3), und
    es ist der Grund, warum das Versprechen dieses Systems eine Datenbank-Zeile
    ist und kein Objekt im Speicher: Auf eine Zeile kann man von ueberall warten,
    jederzeit, auch Tage spaeter, auch aus einer anderen Sitzung. „das warten ist
    kuenstlich man wartet auf die datenbank nicht direkt auf den worker der ist ja
    unabhaengig" — ob ein Daemon laeuft, aendert am Warten nichts, und
    Procrastinate wird nicht gefragt; ob ein Job steckt, sagt `OCRClient.document`.

    Der Ablauf, auf einer EIGENEN `autocommit`-Verbindung je Aufruf (nicht je
    Dokument; `_warte_verbindung`):
        1. `LISTEN doc_done`, BEVOR zum ersten Mal gelesen wird. Andersherum gaebe
           es ein Fenster zwischen Lesen und Lauschen, in dem ein Signal verloren
           ginge.
        2. Dann alle offenen Hashes lesen, mit `read_states`: `documents` und
           `inputs` in EINER Anweisung, ohne `raw` und ohne `content`. Ergebnis da
           heisst fertig, Eingabe da heisst offen, keins von beiden unbekannt — und
           das wird sofort gemeldet. Zwei Anweisungen (erst `documents`, dann
           `inputs`) hielten ein Dokument, dessen Job genau dazwischen endet, fuer
           unbekannt.
        3. Dann auf Signale warten, mit `notifies(timeout=min(Takt, Frist),
           stop_after=1)`, und den Generator der Signale bis zum Ende laufen
           lassen, OHNE `break`: Ein `break` verwirft den Rest eines Pakets an
           Signalen (gemessen: 4 von 5 verloren). Nach einem Signal werden nur die
           gemeldeten Hashes gelesen, die noch offen sind; im Takt
           (`poll_seconds`) zusaetzlich alle offenen — gegen Schreibvorgaenge von
           Hand und alles, was ein Signal sonst verschlucken kann.
        4. Bei einem wiederholbaren `OperationalError` (die Verbindung riss ab)
           wird nach einer kurzen Pause (`NEUVERBINDEN_PAUSE_SEKUNDEN`) neu
           verbunden und neu angefangen: wieder lauschen, wieder alles Offene
           lesen. Signale aus der Luecke heilt das erneute Lesen. Bei einem
           Zugangsfehler mit SQLSTATE nicht: Der endet das Warten. Ein
           Neuverbinden, das scheitert, traegt aber keinen SQLSTATE (auch nicht,
           wenn Datenbank oder Rolle fehlen; `_verbindungsfehler_uebersetzen`)
           und wird deshalb wiederholt: Ohne Frist wartet die Schleife, bis die
           Datenbank zurueck ist. Wer das nicht will, setzt eine Frist.
        5. Laeuft die Frist ab, wird ein letztes Mal alles Offene gelesen; was
           inzwischen fertig ist, kommt als Ergebnis, und nur, was dann noch
           offen ist, nennt der `WaitTimeoutError`. Auch das Verbinden ist durch
           den Rest der Frist begrenzt (`connect_timeout`).

    NICHT zusaetzlich `add_notify_handler` auf der Verbindung: psycopg warnt zur
    Laufzeit, dass beides zusammen nicht verlaesslich ist.

    Args:
        dsn: Die Verbindungszeichenkette; die Schleife oeffnet und schliesst ihre
            Verbindung selbst.
        hashes: Auf welche Dokumente gewartet wird. Doppelte werden einmal
            geliefert.
        timeout: Sekunden fuer den GANZEN Aufruf, oder None fuer unbegrenzt.
        poll_seconds: Der Takt, in dem alle offenen gelesen werden.

    Yields:
        Je Dokument einmal die Ergebniszeile OHNE `raw` (`raw_loaded` False),
        sobald es endgueltig ist — bei JEDEM Endzustand, auch `failed` und
        `input_error`. Ein gescheitertes Dokument beendet das Warten auf die
        uebrigen nicht; das Scheitern ist ein Ergebnis, kein Fehler des Wartens.
        Bereits fertige kommen sofort.

    Raises:
        DocumentNotFound: Weder Ergebnis noch Eingabe zu einem Hash. Kommt SOFORT,
            vor allem anderen, und nennt jeden unbekannten.
        WaitTimeoutError: Die Frist lief ab; er nennt die noch offenen Hashes.
        DatabaseError: Schon das erste Verbinden scheiterte (Instanz weg,
            Datenbank oder Rolle fehlt, falsches Passwort).
        ConfigurationError: Ein Zugangsfehler mit SQLSTATE auf der offenen
            Warte-Verbindung.
        ValueError: Die Frist ist `nan` oder negativ (`check_wait_timeout`).
        TypeError: `hashes` ist ein einzelner Hash.

    Eine Frist und Strg-C beenden nur das Warten: Die Schleife liest nur und fasst
    nie einen Job oder ein Dokument an. Strg-C kommt nach Bruchteilen einer
    Sekunde an, weil psycopg die Wartezeit in kurze Abschnitte zerlegt; die
    Warte-Verbindung schliesst dabei.

    Kosten: keine.
    """
    plan = _Warteplan(hashes, timeout, poll_seconds)
    while plan.offen:
        if plan.angefangen and plan.abgelaufen():
            # Nach einer Pause bis zur Frist kein neuer Versuch: Er hielte das
            # Warten bis zu seinem `connect_timeout` ueber die Frist hinaus.
            plan.frist_melden()
        try:
            conn = _warte_verbindung(dsn, connect_timeout=plan.verbindungsfrist())
        except DatabaseError:
            if not plan.angefangen:
                raise
            time.sleep(plan.pause())
            continue
        try:
            conn.execute(_lauschen_sql())
            zu_lesen = plan.alle()
            while True:
                if zu_lesen:
                    yield from plan.auswerten(read_states(conn, zu_lesen))
                if not plan.offen:
                    return
                if plan.abgelaufen():
                    yield from plan.auswerten(read_states(conn, plan.alle()))
                    plan.frist_melden()
                    return
                warte = plan.wartezeit()
                if warte is None:
                    zu_lesen = plan.alle()
                    continue
                zu_lesen = plan.gemeldet(
                    n.payload for n in conn.notifies(timeout=warte, stop_after=1)
                )
        except psycopg.Error as fehler:
            weiter = _abbruch_einordnen(fehler, plan)
            if weiter is not None:
                raise weiter from fehler
        finally:
            conn.close()
        time.sleep(plan.pause())


async def wait_for_terminal_async(
    dsn: str,
    hashes: Sequence[str],
    *,
    timeout: float | None = None,
    poll_seconds: float = DEFAULT_WAIT_POLL_SECONDS,
) -> AsyncGenerator[DocumentRow]:
    """Dasselbe wie `wait_for_terminal`, als async-Generator.

    Derselbe Kern (`_Warteplan`), dieselbe Anweisung, dieselben Fehler. Das
    Gegenstueck zu Strg-C ist hier `cancel()`: Es kommt sofort an, und die
    Warte-Verbindung schliesst.
    """
    plan = _Warteplan(hashes, timeout, poll_seconds)
    while plan.offen:
        if plan.angefangen and plan.abgelaufen():
            plan.frist_melden()
        try:
            conn = await _warte_verbindung_async(
                dsn, connect_timeout=plan.verbindungsfrist()
            )
        except DatabaseError:
            if not plan.angefangen:
                raise
            await asyncio.sleep(plan.pause())
            continue
        try:
            await conn.execute(_lauschen_sql())
            zu_lesen = plan.alle()
            while True:
                if zu_lesen:
                    for zeile in plan.auswerten(await read_states_async(conn, zu_lesen)):
                        yield zeile
                if not plan.offen:
                    return
                if plan.abgelaufen():
                    for zeile in plan.auswerten(
                        await read_states_async(conn, plan.alle())
                    ):
                        yield zeile
                    plan.frist_melden()
                    return
                warte = plan.wartezeit()
                if warte is None:
                    zu_lesen = plan.alle()
                    continue
                zu_lesen = plan.gemeldet(
                    [n.payload async for n in conn.notifies(timeout=warte, stop_after=1)]
                )
        except psycopg.Error as fehler:
            weiter = _abbruch_einordnen(fehler, plan)
            if weiter is not None:
                raise weiter from fehler
        finally:
            await conn.close()
        await asyncio.sleep(plan.pause())


# --------------------------------------------------------------------------
# Der abgeleitete Zustand
# --------------------------------------------------------------------------


def derive_status(
    sha256: str, row: DocumentRow | None, job: Job | None
) -> DerivedStatus:
    """Setzt Ergebnis und Job eines BEKANNTEN Dokuments zu einem Gesamtbild
    zusammen. Rein, ohne I/O.

    Bekannt heisst: Es hat ein Ergebnis oder eine Eingabe. Ein unbekanntes
    Dokument hat keine Phase; das stellt der Aufrufer vorher fest
    (`DocumentState.is_unknown`) und meldet es.

    Der Job ist der, dessen Kennung an der Eingabe steht (`inputs.job_id`), nicht
    irgendeiner mit demselben Hash: Abschicken und `retry` schreiben dort die
    Kennung des Jobs, der das Dokument verarbeitet.

    Die Regel, in der Reihenfolge, in der sie angewandt wird:
        1. Gibt es ein Ergebnis, IST das der Zustand. Es wird dann gar nicht
           mehr auf den Job gesehen.
        2. Sonst ist es offen, und es zaehlt der Zustand des Jobs.
        3. Ist der Job endgueltig, oder gibt es keinen (mehr), ist nichts mehr
           unterwegs: `ENDED_WITHOUT_RESULT` — siehe dort.

    Warum das eine reine Funktion mit hereingereichtem Job ist und nicht selbst
    abfragt: So laesst sich die Regel gegen erfundene Jobs durchspielen, ohne dass
    eine Datenbank laeuft. Die Regel ist der heikle Teil, die Abfrage ist Handwerk.

    Args:
        sha256: Der Hash des Dokuments.
        row: Die Ergebniszeile, None solange das Dokument offen ist.
        job: Der Job der Eingabe, None wenn es keinen gibt.

    Returns:
        Das Gesamtbild.

    Kosten: keine.
    """
    # Regel 1: Gibt es ein Ergebnis, IST das der Zustand. Die Job-Angaben werden
    # trotzdem mitgeliefert, weil sie beim Nachsehen helfen („welcher Job hat
    # das geschrieben, im wievielten Versuch?"). Sie ENTSCHEIDEN nur nichts mehr.
    if row is not None:
        phase = Phase.TERMINAL
    elif job is None:
        # Offen, aber ohne Job: Eingabe und Job entstehen in einer Transaktion,
        # es gab also einen, und er ist weg. Unterwegs ist nichts mehr.
        phase = Phase.ENDED_WITHOUT_RESULT
    elif job.status == "todo":
        phase = Phase.WAITING
    elif job.status == "doing":
        phase = Phase.RUNNING
    elif job.status in _TERMINALE_JOB_ZUSTAENDE:
        # Der Job ist fertig, ein Ergebnis gibt es aber nicht: einer der
        # seltenen Faelle an `Phase.ENDED_WITHOUT_RESULT`. Kein Datenfehler —
        # der Zustand, den das Ein-Buch-Modell erwartet; `retry` bringt ihn
        # wieder in Gang.
        phase = Phase.ENDED_WITHOUT_RESULT
    else:
        # Ein Job-Zustand, den Procrastinate erst spaeter eingefuehrt hat. Ihn
        # als „laeuft noch" zu verbuchen waere die bequeme Luege; `WAITING` ist
        # die vorsichtige Wahl, weil sie das Dokument im Blick behaelt, statt es
        # faelschlich als abgeschlossen auszuweisen.
        phase = Phase.WAITING

    return DerivedStatus(
        sha256=sha256,
        phase=phase,
        row=row,
        job_id=job.id if job else None,
        job_status=job.status if job else None,
        attempts=job.attempts if job else None,
    )


# --------------------------------------------------------------------------
# Die eine undokumentierte Flaeche
# --------------------------------------------------------------------------


def worker_heartbeats(conn: psycopg.Connection[Any]) -> list[tuple[int, datetime]]:
    """Liest Procrastinates Worker-Lebenszeichen. UNDOKUMENTIERT — mit Vorsicht.

    Diese Funktion greift auf eine Tabelle zu, die Procrastinate in der
    stabilen Dokumentation NICHT als Schnittstelle fuehrt. Belegt ist nur, DASS
    es die Lebenszeichen in der Datenbank gibt und dass ein Django-Modell namens
    `ProcrastinateWorker` mit den Feldern `id` und `last_heartbeat` sie
    ausliefert; ein SQL-Tabellenname wird nirgends genannt, und eine Lese-
    Schnittstelle im `JobManager` gibt es nicht (Feature-Verifikation Punkt 12).

    AM OBJEKT FESTGESTELLT (08.08.2026, `procrastinate/sql/schema.sql` der
    installierten Fassung 3.9.0). Die Feature-Verifikation hatte an dieser Stelle
    eine VORHERSAGE stehen — „mit hoher Sicherheit … eine Tabelle namens
    `procrastinate_workers` mit mindestens `id` und `last_heartbeat`; das ist
    eine Vorhersage, kein Befund". Sie ist eingetroffen, und zwar genau:

        CREATE TABLE procrastinate_workers(
            id bigint PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
            last_heartbeat timestamp with time zone NOT NULL DEFAULT NOW()
        );

    Eine Abweichung von der Vorhersage gibt es doch, und sie stand im Vertrag
    falsch: `id` ist `bigint`, nicht Text. Der Rueckgabetyp dieser Funktion war
    entsprechend `list[tuple[str, datetime]]` und ist auf `int` berichtigt.
    Das ist der Grund, warum eine Vorhersage als Vorhersage gekennzeichnet
    gehoert — man prueft sie sonst nie nach und baut auf ihrem ungeprueften Teil
    weiter.

    Die Tabelle traegt uebrigens KEINE Worker-Namen und keine Queue-Angabe. Wer
    wissen will, WELCHER Worker lebt, bekommt es hier nicht; er bekommt nur, wie
    viele leben und wie frisch ihr letztes Lebenszeichen ist. Fuer die Frage,
    fuer die der Preflight sie stellt („arbeitet ueberhaupt jemand?"), genuegt
    das.

    Deshalb ist der Zugriff hier eingesperrt: EINE Funktion, eine Warnung. Bei
    jedem Procrastinate-Upgrade gehoert sie erneut geprueft; bricht sie, bricht
    genau eine Stelle und nicht der Betrieb.

    NUR FRISCHE HERZSCHLAEGE ZAEHLEN. Nach einem Absturz bleibt die Worker-Zeile
    bis zum naechsten Start stehen — abgeraeumt wird sie dann von Procrastinate
    selbst (`Worker.run` ruft `prune_stalled_workers`) —, und ihr Alter ist das
    Merkmal: Eine 67 Tage alte Zeile liess frueher „der Daemon laeuft" sagen. Als
    Lebenszeichen gilt deshalb nur ein Herzschlag, der juenger ist als
    `config.DAEMON_HEARTBEAT_MAX_AGE_SECONDS`. Das Alter rechnet DIE DATENBANK
    (`clock_timestamp()` gegen `last_heartbeat`, beides ihre Uhr): Der Client
    kann eine andere Zeit oder Zeitzone haben, und ein Vergleich mit seiner Uhr
    wuerde einen laufenden Daemon fuer tot erklaeren oder einen toten fuer
    lebendig. `clock_timestamp()` und nicht `now()`, weil `now()` innerhalb einer
    Transaktion auf ihren Beginn eingefroren ist.

    Wozu ueberhaupt: Das haeufigste Betriebs-Missverstaendnis dieser Architektur
    ist „es passiert nichts" bei totem Daemon. Producer legen Zeilen an, Jobs
    stapeln sich, und alles sieht normal aus — nur arbeitet niemand. Der Client
    soll das SAGEN, statt es einen suchen zu lassen.

    Returns:
        Ein Eintrag je lebendem Worker mit Kennung und letztem Lebenszeichen,
        juengstes zuerst. Leere Liste heisst: kein Worker laeuft (oder nur
        Zeilen abgestuerzter Worker stehen noch da). Das ist eine Feststellung,
        kein Fehler — im Ruhezustand ist genau das richtig.

    Raises:
        DatabaseError: Die Tabelle gibt es nicht (mehr) oder sie sieht anders aus
            als erwartet. Die Meldung sagt ausdruecklich, dass es sich um eine
            undokumentierte Flaeche handelt und wo nachzusehen ist — sonst sucht
            der naechste Leser den Fehler bei sich.

    Kosten: keine.
    """
    try:
        # In einem Sicherungspunkt: Haelt der Aufrufer eine eigene Transaktion
        # offen (`client.conn.transaction()`), bricht eine gescheiterte Abfrage
        # sonst die ganze Transaktion ab, und der gefangene Fehler laesst eine
        # Verbindung zurueck, auf der nichts mehr geht. Unter `autocommit` ist es
        # eine kurze eigene Transaktion.
        with conn.transaction():
            zeilen = conn.execute(
                _HERZSCHLAG_SQL, (DAEMON_HEARTBEAT_MAX_AGE_SECONDS,)
            ).fetchall()
        return [(kennung, gesehen) for kennung, gesehen in zeilen]
    except psycopg.Error as fehler:
        raise _herzschlag_unlesbar(fehler) from fehler


async def worker_heartbeats_async(
    conn: psycopg.AsyncConnection[Any],
) -> list[tuple[int, datetime]]:
    """Dasselbe wie `worker_heartbeats`, asynchron: fuer den `AsyncOCRClient`.
    Dieselbe Anweisung, derselbe Sicherungspunkt, derselbe Fehler."""
    try:
        async with conn.transaction():
            cur = await conn.execute(
                _HERZSCHLAG_SQL, (DAEMON_HEARTBEAT_MAX_AGE_SECONDS,)
            )
            zeilen = await cur.fetchall()
        return [(kennung, gesehen) for kennung, gesehen in zeilen]
    except psycopg.Error as fehler:
        raise _herzschlag_unlesbar(fehler) from fehler


_HERZSCHLAG_SQL: Final = (
    "SELECT id, last_heartbeat FROM procrastinate_workers "
    "WHERE last_heartbeat > "
    "clock_timestamp() - make_interval(secs => %s::double precision) "
    "ORDER BY last_heartbeat DESC"
)
"""Die Anweisung von `worker_heartbeats` und `worker_heartbeats_async` — die
einzige Stelle, an der die undokumentierte Worker-Tabelle gelesen wird."""


def _herzschlag_unlesbar(fehler: psycopg.Error) -> DatabaseError:
    """Der Fehler, wenn sich die Lebenszeichen nicht lesen lassen."""
    return DatabaseError(
        f"Die Worker-Lebenszeichen liessen sich nicht lesen: {fehler}\n"
        f"ACHTUNG, UNDOKUMENTIERTE FLAECHE: `procrastinate_workers` wird in "
        f"Procrastinates stabiler Dokumentation nicht als Schnittstelle "
        f"gefuehrt; Name und Spalten sind am Objekt der installierten "
        f"Fassung festgestellt (Feature-Verifikation Punkt 12). Wahrscheinlich"
        f" hat ein Procrastinate-Upgrade sie geaendert. Nachzusehen in "
        f"`procrastinate/sql/schema.sql` der installierten Fassung; die "
        f"Abfrage steht ausschliesslich in `_HERZSCHLAG_SQL`. Der Fehler liegt "
        f"NICHT beim Aufrufer."
    )
