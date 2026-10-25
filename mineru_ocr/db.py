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
lautet: EIN Buch, nicht zwei. „DAS IST POSTRGRES DER JOB LINKED AUF DAS DOKUMENT
DAS ER PROCESSED UND WENN MAN DEN STATUS EINES DOKUMENTS HABEN WILL QUERIED MAN
NACH DE LETZTEN JOB DER AUF DAS DOKUMENT LINKT" (Plan 2.4). Daraus folgt alles
Uebrige: Die Zeile kennt nur Endzustaende. Es gibt keinen Zustand `processing`,
weil „laeuft gerade" bereits in Procrastinates Job-Tabelle steht und ein zweites
Mal gefuehrt werden muesste. Es gibt keinen Abgleich-Waechter, weil es nichts
abzugleichen gibt. Zwischen der Anlage einer Zeile und ihrem Endzustand fasst
NIEMAND sie an.
"""

from __future__ import annotations

import enum
import time
from collections.abc import Sequence
from dataclasses import dataclass, fields
from datetime import datetime
from typing import Any, Final

import psycopg
from procrastinate.jobs import Job
from psycopg.rows import class_row
from psycopg.types.json import Jsonb

from .config import DAEMON_HEARTBEAT_MAX_AGE_SECONDS, DEFAULT_WAIT_POLL_SECONDS

from .errors import (
    ConfigurationError,
    DatabaseError,
    DocumentNotFound,
    TerminalOverwriteError,
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
    size_bytes    bigint NOT NULL,
    mime          text NOT NULL,
    page_count    integer,
    status        text NOT NULL DEFAULT 'new'
        CHECK (status IN ('new', 'done', 'empty', 'failed', 'input_error')),
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
    content       bytea NOT NULL
);
"""
"""Das vollstaendige Schema: `documents` fuer die Ergebnisse, `inputs` fuer die
Eingaben, die noch kein Ergebnis haben.

Der CHECK auf `status` steht im Code, nicht nur im Kommentar: Die Aufzaehlung der
gueltigen Zustaende ist der Kern dieses Datenmodells, und eine Aufzaehlung, die
nur ein Kommentar ist, wird irgendwann von einem falsch geschriebenen Zustand
verletzt, den niemand bemerkt. Die Datenbank bemerkt ihn.

Der Index folgt der Abfrage, die es tatsaechlich gibt: die Zaehlung nach Zustand
fuer `status()`/`report()`. Mehr Indizes waeren geraten; dieser ist hergeleitet.

Eine dritte Abfrage gibt es, und sie hat bewusst KEINEN Index: die offenen
Aufruest-Faelle in `status()`/`report()` — `status = 'input_error'` mit einem
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
hat. Pfade gibt es in der Datenbank nicht.

`status` kennt KEIN `processing`. Siehe Modul-Vorspann.

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

Geschrieben wird `raw` NUR beim Terminal-Schreiben eines Durchlaufs und
ueberschrieben nur dann, wenn ein gescheitertes Dokument erneut verarbeitet wird.
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
    """Die fuenf Zustaende einer Zeile. Vier davon sind endgueltig.

    Warum `empty` und `input_error` eigene Zustaende sind und nicht Sonderfaelle
    von `done` bzw. `failed`: Ein leeres Ergebnis als Erfolg zu verbuchen ist
    unsichtbarer Verlust, es als Fehler zu verbuchen ist endlose Wiedervorlage
    mit doppelten Kosten — beide Fehler sind in den Ad-hoc-Skripten je einmal
    wirklich passiert. Und `input_error` von `failed` zu trennen beantwortet die
    Frage „wofuer haben wir bezahlt?": `input_error` hat RunPod nie gesehen.
    """

    NEW = "new"
    """Angelegt, noch kein Ergebnis. Der EINZIGE nicht endgueltige Zustand."""

    DONE = "done"
    """Ergebnis vollstaendig da, mindestens ein Chunk mit Inhalt."""

    EMPTY = "empty"
    """Alles leer — gueltig, aber am Bild zu pruefen."""

    FAILED = "failed"
    """Endgueltig gescheitert: nicht wiederholbar oder Versuche erschoepft."""

    INPUT_ERROR = "input_error"
    """Vor jedem RunPod-Kontakt aussortiert. Hat nichts gekostet."""

    @property
    def is_terminal(self) -> bool:
        """Alles ausser `NEW`."""
        return self is not DocumentStatus.NEW


class Phase(enum.Enum):
    """Wo ein Dokument gerade steht — ABGELEITET, nirgends gespeichert.

    Das ist die Antwort auf „ein Buch, nicht zwei": Der Fortschritt eines
    Dokuments wird aus der Zeile UND dem juengsten verlinkten Job GELESEN, nie in
    die Zeile gespiegelt. Diese Aufzaehlung existiert nur im Speicher.
    """

    TERMINAL = "terminal"
    """Die Zeile ist endgueltig. Welcher Endzustand, sagt `row_status`."""

    REGISTERED = "registered"
    """Zeile angelegt, aber nie ein Job dazu eingereiht."""

    WAITING = "waiting"
    """Juengster Job wartet (`todo`)."""

    RUNNING = "running"
    """Juengster Job laeuft (`doing`)."""

    ENDED_WITHOUT_RESULT = "ended_without_result"
    """Juengster Job ist endgueltig zu Ende, die Zeile steht aber noch auf `new`.

    Der seltene Fall: harter Abschuss im Schreibfenster, oder Abbruch beim
    Herunterfahren (`aborted`). Er ist KEIN Datenfehler, sondern genau der
    Zustand, den das Ein-Buch-Modell erwartet — und er heilt von selbst: Beim
    naechsten Einreihen laeuft das Dokument schlicht von vorn. „es wird dann
    ncohmal und das wars wenns nicht fertig ist ists gefailed" (Plan 2.4).
    """


@dataclass(frozen=True)
class DocumentRow:
    """Eine Zeile, so wie sie in der Datenbank steht. Reine Daten, keine Logik."""

    sha256: str
    size_bytes: int
    mime: str
    page_count: int | None
    status: DocumentStatus
    raw: dict[str, Any] | None
    """Das zusammengesetzte Gesamtergebnis auf Dokument-Ebene, oder None, solange
    es keines gibt. EIN Objekt, keine Liste — eine Liste waere die Chunk-Struktur,
    und die gibt es hier nicht (siehe SCHEMA_SQL)."""

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
        vergliche sich mit ihr richtig — aber nur beim Vergleich. `row.status.
        is_terminal` gaebe es an einer Zeichenkette nicht, und der Fehler kaeme
        erst dort, wo jemand die Eigenschaft benutzt, nicht dort, wo die falsche
        Zeile entstand.

        `object.__setattr__`, weil die Klasse eingefroren ist. Das ist der
        vorgesehene Weg fuer genau diesen Fall und keine Umgehung: Eingefroren
        heisst „nach der Erzeugung unveraenderlich", und `__post_init__` gehoert
        noch zur Erzeugung.

        Ohne Fallunterscheidung, und das ist Absicht: `DocumentStatus(x)` gibt
        fuer ein bereits richtiges Mitglied dasselbe Mitglied zurueck. Eine
        vorgeschaltete `isinstance`-Pruefung waere fuer die Typpruefung toter
        Code — sie sieht das Feld als `DocumentStatus` deklariert und den
        Gegenzweig damit als unerreichbar. Genau das ist die Lage: Nach der
        Erzeugung IST es einer, waehrend der Erzeugung kann es eine Zeichenkette
        aus der Datenbank sein.
        """
        object.__setattr__(self, "status", DocumentStatus(self.status))


@dataclass(frozen=True)
class DerivedStatus:
    """Der zusammengesetzte Zustand aus Zeile und juengstem Job.

    Wird bei jeder Abfrage neu gebildet und nirgends abgelegt. Wer ihn ablegte,
    haette die zweite Buchfuehrung wieder eingefuehrt, gegen die das ganze
    Datenmodell steht.
    """

    sha256: str
    row_status: DocumentStatus
    phase: Phase

    job_id: int | None
    """Kennung des juengsten verlinkten Jobs, None wenn es keinen gibt."""

    job_status: str | None
    """Dessen Zustand in Procrastinates Worten (`todo`, `doing`, `succeeded`,
    `failed`, `cancelled`, `aborted`), unuebersetzt weitergereicht — wer in der
    Datenbank nachsieht, soll dasselbe Wort finden."""

    attempts: int | None
    """Wie oft der juengste Job bisher vorgelegt wurde. Achtung auf die
    Reichweite: Das zaehlt JEDE Wiedervorlage — Strategie-Retry,
    stalled-Wiederaufnahme und Retry von Hand laufen durch dieselbe SQL-Funktion
    und erhoehen den Zaehler alle drei (Feature-Verifikation Punkt 19)."""

    job_count: int
    """Wie viele Jobs es insgesamt zu diesem Dokument gab. Groesser als eins bei
    jedem Wiederanlauf; sichtbar, weil Job-Zeilen nie geloescht werden."""


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
        _schema_abgleichen(conn)


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


INPUT_SPALTEN: Final = frozenset({"sha256", "content"})
"""Die Spalten von `inputs`. Die Tabelle hat keine Zeilenklasse (der Executor
liest nur `content`), also steht die Erwartung hier, und `_schema_abgleichen`
vergleicht gegen sie. Typ, NOT NULL und Primaerschluessel stehen dort selbst."""

MAX_INPUT_BYTES: Final = 1_000_000_000
"""Die groesste Eingabe, die sich ablegen laesst. Postgres begrenzt ein Feld und
eine Nachricht auf knapp 1 GiB: Gemessen gingen 1.070.000.008 Byte noch hinein,
1.101.004.808 nicht (der Server meldet dann „invalid message length" und trennt
die Verbindung). Die Grenze liegt darunter, damit die Ablehnung eine Meldung ist
und kein Verbindungsabbruch. Sie gilt fuer jede Eingabe, auch fuer PDFs, die der
Job danach in Stuecke schneidet."""


def _schema_abgleichen(conn: psycopg.Connection[Any]) -> None:
    """Prueft die Tabellen auf der Platte gegen ihre Zeilenklassen.

    Die Antwort auf die einzige Doppelung dieses Moduls. Ein Migrationswerkzeug
    loeste sie auf, waere hier aber Ballast — dies ist ein Werkzeug fuer einen
    Menschen auf einer Maschine. Stattdessen wird die Doppelung LAUT.

    Geprueft werden beide Tabellen, `documents` gegen die Felder von
    `DocumentRow` und `inputs` gegen `INPUT_SPALTEN`; gemeldet wird alles auf
    einmal, damit die Abhilfe nicht Tabelle fuer Tabelle gesucht werden muss.
    """
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
        # Aus `pg_attribute` der Tabelle, die `search_path` aufloest, nicht aus
        # `information_schema.columns` nach Namen: Das fasste gleichnamige
        # Tabellen anderer Schemas dazu und meldete eine gesunde Datenbank als
        # unpassend.
        form = {
            name: (typ, nicht_null)
            for name, typ, nicht_null in conn.execute(
                "SELECT a.attname, format_type(a.atttypid, a.atttypmod), "
                "a.attnotnull FROM pg_attribute a "
                "WHERE a.attrelid = %s::regclass AND a.attnum > 0 "
                "AND NOT a.attisdropped",
                (tabelle,),
            ).fetchall()
        }
        ist = set(form)
        fehlend = soll - ist
        ueberzaehlig = ist - soll
        if fehlend or ueberzaehlig:
            abweichungen.append(
                f"Die Tabelle `{tabelle}` passt nicht zu {gegen}. "
                f"Im Code, aber nicht in der Tabelle: {sorted(fehlend)}. "
                f"In der Tabelle, aber nicht im Code: {sorted(ueberzaehlig)}."
            )
        elif tabelle == "inputs":
            # Die Namen stimmen; ob die Form stimmt, sagen sie nicht. Ohne
            # Primaerschluessel scheitert `ON CONFLICT (sha256)` beim ersten
            # Abschicken, und `setup` haette vorher Erfolg gemeldet.
            schluessel = {
                zeile[0]
                for zeile in conn.execute(
                    "SELECT a.attname FROM pg_index i "
                    "JOIN pg_attribute a ON a.attrelid = i.indrelid "
                    "AND a.attnum = ANY (i.indkey) "
                    "WHERE i.indrelid = 'inputs'::regclass AND i.indisprimary"
                ).fetchall()
            }
            if (
                form["sha256"][0] != "text"
                or form["content"] != ("bytea", True)
                or schluessel != {"sha256"}
            ):
                abweichungen.append(
                    "Die Tabelle `inputs` hat die erwarteten Spalten, aber nicht "
                    "ihre Form: erwartet `sha256 text PRIMARY KEY` und "
                    "`content bytea NOT NULL`, gefunden "
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
    """
    zustand = getattr(fehler, "sqlstate", None)
    if isinstance(zustand, str) and (zustand.startswith("28") or zustand == "3D000"):
        return ConfigurationError(f"{was}: Zugang taugt nicht ({zustand}): {fehler}")
    return DatabaseError(f"{was}: Datenbank antwortet nicht ({zustand}): {fehler}")


def connect(dsn: str) -> psycopg.Connection[Any]:
    """Oeffnet eine synchrone Verbindung fuer die Fassaden-Seite.

    Besitz: Der Aufrufer besitzt und schliesst sie.

    Zusicherung:
        `autocommit` ist AN. Das ist keine Bequemlichkeit, sondern Bedingung
        dafuer, dass Benachrichtigungen zeitnah ankommen: Eine Verbindung in
        einer offenen Transaktion sieht `NOTIFY` erst nach deren Ende — `wait()`
        wuerde dann bis zum Zeitablauf haengen, obwohl das Ergebnis laengst da
        ist. Wer eine Transaktion braucht, oeffnet sie ausdruecklich.

    Raises:
        DatabaseError: Die Instanz antwortet nicht. Ausdruecklich NICHT
            `ConfigurationError` — der Unterschied entscheidet ueber Geld: Eine
            Verbindungsstoerung ist wiederholbar, ein falscher Zugang ist es
            nicht. Wuerde hier `ConfigurationError` fliegen, machte ein
            einsekuendiger Postgres-Aussetzer beim Terminal-Schreiben aus einem
            bezahlten Dokument ein endgueltig gescheitertes.
        ConfigurationError: Der Zugang taugt nicht — falsche
            Verbindungszeichenkette, fehlende Rechte, keine solche Datenbank.
    """
    try:
        return psycopg.connect(dsn, autocommit=True)
    except psycopg.Error as fehler:
        raise _verbindungsfehler_uebersetzen(fehler, "Verbindung oeffnen") from fehler


async def connect_async(dsn: str) -> psycopg.AsyncConnection[Any]:
    """Oeffnet eine asynchrone Verbindung fuer die Daemon-Seite.

    Sonst wie `connect`, einschliesslich `autocommit`.
    """
    try:
        return await psycopg.AsyncConnection.connect(dsn, autocommit=True)
    except psycopg.Error as fehler:
        raise _verbindungsfehler_uebersetzen(fehler, "Verbindung oeffnen") from fehler


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
bricht stattdessen dort, wo sie gepflegt gehoert, und `_schema_abgleichen` sagt
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


def insert_document(
    conn: psycopg.Connection[Any],
    *,
    sha256: str,
    size_bytes: int,
    mime: str,
    endpoint_id: str,
) -> bool:
    """Legt eine Zeile an, falls es sie noch nicht gibt.

    Setzt Jurgens Dubletten-Vorgabe auf Datenbank-Ebene um: `INSERT ... ON
    CONFLICT (sha256) DO NOTHING`. Dieselbe Datei kann nie zweimal existieren,
    egal wie viele Produzenten sie gleichzeitig einreihen — die Datenbank
    entscheidet, nicht eine Pruefung im Programm, die zwischen Pruefen und
    Einfuegen ein Zeitfenster hat.

    Args:
        conn: Offene Verbindung, gehoert dem Aufrufer.
        sha256: Roher Byte-Hash.
        size_bytes: Rohe Groesse.
        mime: Die MIME-Zeichenkette aus `mime.DocumentType`.
        endpoint_id: Fuer welchen Endpoint die Zeile gilt.

    Returns:
        True, wenn eine Zeile angelegt wurde. False, wenn es sie schon gab —
        DAS IST DER NORMALFALL beim wiederholten Durchlaufen eines Ordners und
        kein Fehler. Genau darauf beruht Jurgens Ordner-Szenario: „dann später
        kommt zum ordner noch ein pfad hinzu den tut man einfach dazu und lässt
        nochmal durchlaufen weil eh nur läuft was noch fehlt" (Plan 2.1).

    Zusicherung:
        Beruehrt eine BESTEHENDE Zeile in keiner Weise — kein Feld, auch nicht
        `updated_at`. Ein zweites Einreihen darf ein fertiges Ergebnis nicht
        anfassen.

    Kosten: keine.
    """
    ergebnis = conn.execute(
        """
        INSERT INTO documents (
            sha256, size_bytes, mime, endpoint_id
        )
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (sha256) DO NOTHING
        RETURNING sha256
        """,
        (sha256, size_bytes, mime, endpoint_id),
    ).fetchone()
    # `RETURNING` liefert nur dann eine Zeile, wenn wirklich eingefuegt wurde —
    # bei `DO NOTHING` kommt nichts. Das ist die verlaessliche Unterscheidung;
    # `rowcount` haette sie auch, aber `RETURNING` sagt es an der Abfrage selbst.
    return ergebnis is not None


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

    Args:
        conn: Offene Verbindung, gehoert dem Aufrufer.
        sha256: Roher Byte-Hash von `content`.
        content: Die vollstaendigen Bytes der Eingabe.

    Returns:
        True, wenn die Eingabe neu abgelegt wurde; False, wenn es zu diesem Hash
        schon eine gibt. Dann bleibt die vorhandene unberuehrt.

    Kosten: keine.
    """
    ergebnis = conn.execute(
        "INSERT INTO inputs (sha256, content) VALUES (%s, %b) "
        "ON CONFLICT (sha256) DO NOTHING RETURNING sha256",
        (sha256, content),
    ).fetchone()
    return ergebnis is not None


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
        DatabaseError: Die Instanz antwortet nicht. Wiederholbar.
        ConfigurationError: Der Zugang taugt nicht.

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


def get_document(conn: psycopg.Connection[Any], sha256: str) -> DocumentRow | None:
    """Liest eine Zeile vollstaendig, einschliesslich `raw`. None, wenn es sie nicht gibt.

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
        return cur.execute(
            f"SELECT {_SPALTEN_VOLL} FROM documents WHERE sha256 = %s", (sha256,)
        ).fetchone()


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
    """Zaehlt die Zeilen je Zustand. Grundlage von `status()` und `report()`.

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


# --------------------------------------------------------------------------
# Lesen und schreiben — asynchron, ausschliesslich der Task
# --------------------------------------------------------------------------


async def get_document_async(
    conn: psycopg.AsyncConnection[Any], sha256: str
) -> DocumentRow:
    """Liest eine Zeile fuer den Executor — OHNE `raw`.

    Warum ohne: Der Executor braucht Typ und Zustand. `raw` ist bei einem
    bereits verarbeiteten Dokument zweistellige Megabyte gross, und der einzige
    Grund, es zu lesen, waere der Terminal-Guard — der aber nur den Zustand
    braucht. Es mitzuladen hiesse, bei jedem Wiederlauf eines fertigen Dokuments
    das ganze Ergebnis durch die Leitung zu ziehen, um es sofort wegzuwerfen.

    Warum es diese Funktion ueberhaupt gibt und der Executor nicht selbst fragt:
    Dieses Modul ist der einzige Ort im Paket, an dem SQL auf `documents` steht
    (siehe Modul-Vorspann). Eine zweite Stelle waere eine zweite Lesart derselben
    Tabelle — und die faellt erst auf, wenn die Daten schon falsch sind.

    Args:
        conn: Offene asynchrone Verbindung, gehoert dem Aufrufer.
        sha256: Primaerschluessel.

    Returns:
        Die Zeile mit `raw_loaded = False`.

    Raises:
        DocumentNotFound: Kein Eintrag. Anders als `get_document` gibt diese
            Funktion NICHT None zurueck: Ein Job, der auf ein Dokument verweist,
            das es nicht gibt, ist keine leere Auskunft, sondern ein Widerspruch
            — die Zeile entsteht beim Einreihen, also vor dem Job.

    Kosten: keine.
    """
    async with conn.cursor(row_factory=class_row(DocumentRow)) as cur:
        await cur.execute(
            f"SELECT {_SPALTEN_OHNE_RAW} FROM documents WHERE sha256 = %s",
            (sha256,),
        )
        zeile = await cur.fetchone()
    if zeile is None:
        raise DocumentNotFound(
            f"Kein Eintrag zu {sha256}. Der Job verweist auf ein Dokument, das "
            f"es nicht gibt — die Zeile entsteht beim Einreihen, vor dem Job."
        )
    return zeile


async def write_terminal_async(
    conn: psycopg.AsyncConnection[Any],
    *,
    sha256: str,
    status: DocumentStatus,
    raw: dict[str, Any],
    image_tag_at_submit: str | None = None,
    error: str | None = None,
    notes: Sequence[dict[str, Any]] | None = None,
    page_count: int | None = None,
) -> None:
    """Schreibt das Endergebnis eines Durchlaufs und weckt alle Warter.

    Das ist der EINZIGE Schreibvorgang an einer Zeile zwischen ihrer Anlage und
    ihrem Endzustand. Er umfasst `raw`, `status`, `error`, angehaengte `notes`,
    `page_count`, `updated_at`, das Loeschen der Eingabe aus `inputs` (ausser bei
    `failed`, damit `retry` etwas zu lesen hat) und das Wecksignal in EINER
    Transaktion — es gibt keinen Zwischenzustand, in dem `raw` geschrieben ist
    und der Status noch `new` sagt, und keinen, in dem das Ergebnis steht und die
    Eingabe noch da ist.

    Args:
        conn: Offene asynchrone Verbindung, gehoert dem Aufrufer.
        sha256: Primaerschluessel. Die Zeile muss existieren.
        status: Ein endgueltiger Zustand. `NEW` ist ein Programmierfehler.
        raw: Das zusammengesetzte Gesamtergebnis auf Dokument-Ebene, wie der
            Executor es gebildet hat (siehe SCHEMA_SQL). Bei `failed` und
            `input_error` dasselbe Objekt mit dem, was bis dahin ankam, samt
            Fehler-Umschlag — auch eine Fehlerantwort ist bezahlt und gehoert
            aufgehoben. Geschrieben wird es als `psycopg.types.json.Jsonb(raw)`:
            Ohne diesen Wrapper wird eine Abbildung nicht als `jsonb` angepasst,
            sondern als Verbund- oder Feldtyp — und das ist die Spalte mit dem
            bezahlten Ergebnis, nicht die Stelle zum Ausprobieren.
        image_tag_at_submit: Welches Worker-Image lief. Der Executor liest es
            einmal je Lauf und reicht es hier durch. Ohne diesen Weg bliebe die
            Spalte fuer immer leer, und die Frage, fuer die sie da ist („war das
            noch das alte Image?"), waere nach Wochen nicht mehr entscheidbar.
        error: Kurzform bei `failed`/`input_error`, sonst None.
        notes: Anzuhaengende Ereignisse. None haengt nichts an; die Liste wird
            ANGEHAENGT, nie ersetzt.
        page_count: Die Seitenzahl, die der Job aus den Bytes bestimmt hat (nur
            bei PDF). None laesst eine vorhandene Angabe stehen.

    Zusicherung:
        Nach der Rueckkehr ist die Zeile endgueltig und ein `NOTIFY` auf dem
        Kanal `doc_done` mit dem sha256 als Nutzlast ist abgesetzt — auch bei
        `failed` und `input_error`. Das ist wichtiger, als es aussieht: Weckte
        das Signal nur bei Erfolg, liefe jeder Warter auf ein gescheitertes
        Dokument bis zum Zeitablauf, und der Aufrufer wuesste am Ende nicht, ob
        es scheiterte oder nur lange dauert.

        NICHT zugesichert ist, dass ein Warter das Signal empfaengt — er kann
        genau zwischen seiner ersten Abfrage und dem Beginn des Lauschens stehen.
        Deshalb hat `wait_for_terminal` zusaetzlich eine Erstabfrage und eine
        Rueckfallabfrage.

    Idempotenz und die eine erlaubte Ueberschreibung:
        Zweimal derselbe Aufruf ist unschaedlich.

        Steht die Zeile bereits auf `failed`, wird sie UEBERSCHRIEBEN, und das
        ist gewollt: Ein per `retry_job_by_id` wieder vorgelegter Job verarbeitet
        das Dokument wirklich neu und schreibt sein Ergebnis (Plan 4.2). Genau
        deshalb deckt der Terminal-Guard im Executor `failed` ausdruecklich nicht
        ab — waere `failed` im Guard, waere jeder Retry von Hand ein Nichts-Tun,
        das als Erfolg verbucht wuerde, und ein gescheitertes Dokument koennte
        nie wieder laufen.

        Steht sie auf `done`, `empty` oder `input_error`, entscheidet der
        ZUSTAND, und nur er: Ist der uebergebene Zustand derselbe, kehrt die
        Funktion zurueck, ohne etwas zu tun — insbesondere wird `raw` NICHT
        ersetzt, der erste Schreibvorgang gewinnt. Ist er ein anderer, fliegt
        `TerminalOverwriteError`.

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
        Die Zeile wird gesperrt (`SELECT ... FOR UPDATE`), zwei Tasks auf
        demselben Hash koennen sich nicht verschraenken. Es SOLLTE ohnehin nie
        zwei geben (`lock=sha256` beim Einreihen) — die Sperre ist der Guertel
        zum Hosentraeger.

    Raises:
        DocumentNotFound: Kein Eintrag zu diesem Hash.
        TerminalOverwriteError: siehe Idempotenz.

    Kosten: keine.
    """
    if not status.is_terminal:
        raise ValueError(
            f"write_terminal_async wurde mit dem Zustand {status.value!r} gerufen. "
            f"Diese Funktion schreibt ENDzustaende; `new` ist keiner."
        )

    async with conn.transaction():
        zeile = await (
            await conn.execute(
                "SELECT status FROM documents WHERE sha256 = %s FOR UPDATE",
                (sha256,),
            )
        ).fetchone()
        if zeile is None:
            raise DocumentNotFound(
                f"Kein Eintrag zu {sha256}. Ein Endergebnis ohne Zeile gibt es "
                f"nicht — die Zeile entsteht beim Einreihen, nicht hier."
            )

        vorher = DocumentStatus(zeile[0])
        if vorher.is_terminal and vorher is not DocumentStatus.FAILED:
            if vorher is status:
                # Derselbe Endzustand ein zweites Mal: Der erste Schreibvorgang
                # gewinnt, `raw` wird NICHT ersetzt. Das ist der harmlose
                # Wiederlauf nach einem Absturz im Schreibfenster.
                #
                # DIE VERMERKE WERDEN TROTZDEM ANGEHAENGT. Frueher kehrte diese
                # Stelle vor dem gesamten UPDATE zurueck, und damit fielen auch
                # `notes` und `image_tag_at_submit` des zweiten Laufs weg — obwohl
                # der Docstring nur `raw` ausnimmt („insbesondere wird `raw` NICHT
                # ersetzt") und die Zusicherung am `notes`-Parameter das Gegenteil
                # sagt: „die Liste wird ANGEHAENGT, nie ersetzt".
                #
                # Das ist kein Wortklauben: Genau in diesem Fall — ein zweiter
                # Lauf, der dasselbe Ergebnis noch einmal erzeugt hat — ist die
                # Beobachtung, DASS es ihn gab, die einzige Spur davon. Sie
                # wegzuwerfen macht den harmlosen Wiederlauf unsichtbar, und
                # unsichtbare Wiederlaeufe sind bezahlte Wiederlaeufe.
                #
                # `raw` und `status` bleiben unberuehrt, `updated_at` ebenfalls:
                # Der Zeitstempel gehoert zum Ergebnis, und das Ergebnis ist das
                # des ERSTEN Laufs.
                if notes:
                    await conn.execute(
                        "UPDATE documents SET notes = notes || %s::jsonb "
                        "WHERE sha256 = %s",
                        (Jsonb(list(notes)), sha256),
                    )
                return
            raise TerminalOverwriteError(
                f"{sha256} steht bereits endgueltig auf {vorher.value!r} und "
                f"soll auf {status.value!r} geaendert werden. Ein fertiges "
                f"Dokument wird nie ueberschrieben. Der einzige Zustand, der "
                f"neu verarbeitet werden darf, ist `failed` — dafuer gibt es "
                f"retry_job_by_id."
            )

        await conn.execute(
            """
            UPDATE documents
               SET status = %s,
                   raw = %s,
                   error = %s,
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
                image_tag_at_submit,
                page_count,
                Jsonb(list(notes) if notes else []),
                sha256,
            ),
        )
        # Die Eingabe geht mit dem Endergebnis, in DERSELBEN Transaktion: Beides
        # gilt oder beides nicht. Bei `failed` bleibt sie stehen, damit `retry`
        # etwas zu lesen hat. Entfernt wird per `DELETE`; fuer den Platz ist das
        # gleich wie ein Leeren des Felds, und Autovacuum gibt ihn frei.
        if status is not DocumentStatus.FAILED:
            await conn.execute("DELETE FROM inputs WHERE sha256 = %s", (sha256,))
        # Das Wecksignal gehoert in DIESELBE Transaktion: Wuerde es davor
        # abgesetzt, koennte ein Warter aufwachen und die Zeile noch alt
        # vorfinden; danach abgesetzt, faellt es bei einem Abbruch zwischen
        # beidem ganz aus. Postgres stellt NOTIFY ohnehin erst beim Commit zu.
        await conn.execute("SELECT pg_notify(%s, %s)", (DONE_CHANNEL, sha256))


async def append_note_async(
    conn: psycopg.AsyncConnection[Any], sha256: str, note: dict[str, Any]
) -> None:
    """Haengt ein Ereignis an `notes` an, ohne sonst etwas zu beruehren.

    Der eine Schreibvorgang, der auch an einer noch nicht endgueltigen Zeile
    erlaubt ist — und die Ausnahme ist eng: `notes` ist ausdruecklich KEIN
    Zustand, sondern eine Beobachtung. Angehaengt wird immer, ersetzt wird nie.

    Args:
        note: Sollte mindestens `kind` und `at` tragen, damit die Liste spaeter
            noch lesbar ist. Die Form wird nicht erzwungen — eine erzwungene Form
            waere ein zweites Datenmodell, und `notes` ist gerade der Ort fuer
            das, was noch keines hat.

    Raises:
        DocumentNotFound: Kein Eintrag zu diesem Hash. Still nichts zu tun waere
            hier schlechter als zu werfen: Eine Beobachtung, die ins Leere
            geschrieben wird, faellt sonst nie auf.
    """
    ergebnis = await (
        await conn.execute(
            "UPDATE documents SET notes = notes || %s::jsonb "
            "WHERE sha256 = %s RETURNING sha256",
            (Jsonb([note]), sha256),
        )
    ).fetchone()
    if ergebnis is None:
        raise DocumentNotFound(f"Kein Eintrag zu {sha256}, Notiz nicht angehaengt.")


# --------------------------------------------------------------------------
# Warten
# --------------------------------------------------------------------------


def wait_for_terminal(
    conn: psycopg.Connection[Any],
    sha256: str,
    timeout: float | None = None,
    poll_seconds: float = DEFAULT_WAIT_POLL_SECONDS,
) -> DocumentRow:
    """Wartet, bis ein Dokument endgueltig ist, und gibt die Zeile zurueck.

    Das ist Jurgens „awaiten des resultats" als eigene Funktion (Plan 2.3), und
    es ist der Grund, warum das Versprechen dieses Systems eine Datenbank-Zeile
    ist und kein Objekt im Speicher: Auf ein Objekt im Speicher kann man nur
    warten, solange der Prozess lebt. Auf eine Zeile kann man von ueberall
    warten, jederzeit, auch Tage spaeter, auch aus einer anderen Sitzung.

    Der Ablauf hat drei Teile, und jeder deckt eine Luecke der anderen ab:
        1. `LISTEN doc_done`, BEVOR zum ersten Mal gelesen wird.
        2. Dann die Erstabfrage — denn das Dokument kann bereits fertig sein,
           und auf ein Signal zu warten, das schon vorbei ist, waere ein Warten
           bis zum Zeitablauf.
        3. Dann die Benachrichtigungen ueber `conn.notifies(timeout=…,
           stop_after=1)` — die Bordmittel von psycopg ab 3.2, nicht eine eigene
           Warteschleife auf dem Socket. Zwei Gruende: Die Zeitschranke wird
           intern in kurze Abschnitte zerlegt, damit ein Signal (Strg-C) sofort
           ankommt statt erst nach Ablauf; und sie laeuft ueber die monotone Uhr,
           sodass eine Zeitumstellung das Warten nicht verlaengert. `stop_after=1`
           sagt „ein Signal genuegt", statt es mit einem Abbruch nachzubauen.

           Dazu eine Rueckfallabfrage alle `settings.wait_poll_seconds`. Die ist
           nicht ueberfluessig, auch wenn Schritt 1 vor Schritt 2 steht: Eine
           abgerissene Verbindung, ein verpasstes Signal, ein Schreibvorgang von
           aussen an der Datenbank — gegen all das hilft nur, gelegentlich
           einfach nachzusehen. psycopg liefert dafuer nichts; das ist echte
           Ergaenzung, keine Wiedererfindung.

           NICHT zusaetzlich `add_notify_handler` auf derselben Verbindung
           benutzen: psycopg warnt zur Laufzeit, dass beides zusammen nicht
           verlaesslich ist.

    Args:
        conn: Verbindung mit `autocommit` (sonst kommen die Signale erst nach
            der Transaktion an — siehe `connect`).
        sha256: Auf welches Dokument gewartet wird.
        timeout: Sekunden, oder None fuer unbegrenzt.
        poll_seconds: Abstand der Rueckfallabfrage.

    Returns:
        Die Zeile in ihrem Endzustand — bei JEDEM Endzustand, auch bei `failed`.
        Der Aufrufer prueft, welcher es ist. Diese Funktion wirft nicht, weil ein
        Dokument scheiterte; das Scheitern ist ein Ergebnis, kein Fehler des
        Wartens.

    Raises:
        DocumentNotFound: Es gibt keine solche Zeile. Kommt SOFORT, nicht nach
            dem Zeitablauf — auf etwas zu warten, das nie eingereiht wurde, ist
            immer ein Irrtum des Aufrufers, und ihn zwei Stunden warten zu
            lassen, bevor man es ihm sagt, waere unfreundlich.
        TimeoutError: Die Zeit lief ab. Das Dokument kann trotzdem noch fertig
            werden — der Zeitablauf sagt etwas ueber die Geduld des Wartenden,
            nichts ueber das Dokument.

    Kosten: keine.
    """
    # Schritt 1: lauschen, BEVOR zum ersten Mal gelesen wird. Andersherum gaebe
    # es ein Fenster zwischen Lesen und Lauschen, in dem ein Signal verloren
    # ginge — und dann wartet der Aufrufer auf etwas, das schon vorbei ist.
    conn.execute(
        psycopg.sql.SQL("LISTEN {}").format(psycopg.sql.Identifier(DONE_CHANNEL))
    )

    # Schritt 2: die Erstabfrage. Sie beantwortet zugleich die Frage, ob es die
    # Zeile ueberhaupt gibt — und die wird SOFORT beantwortet, nicht nach dem
    # Zeitablauf.
    zeile = get_document(conn, sha256)
    if zeile is None:
        raise DocumentNotFound(
            f"Kein Eintrag zu {sha256}. Auf ein Dokument zu warten, das nie "
            f"eingereiht wurde, ist immer ein Irrtum des Aufrufers — deshalb "
            f"kommt die Antwort jetzt und nicht nach Ablauf der Wartezeit."
        )
    if zeile.status.is_terminal:
        return zeile

    ende = None if timeout is None else time.monotonic() + timeout
    while True:
        rest = None if ende is None else ende - time.monotonic()
        if rest is not None and rest <= 0:
            raise TimeoutError(
                f"{sha256} wurde innerhalb von {timeout} Sekunden nicht "
                f"endgueltig. Das Dokument kann trotzdem noch fertig werden — "
                f"der Zeitablauf sagt etwas ueber die Geduld des Wartenden, "
                f"nichts ueber das Dokument."
            )

        # Schritt 3: auf ein Signal warten, hoechstens aber bis zur naechsten
        # Rueckfallabfrage. `stop_after=1` heisst „ein Signal genuegt"; die
        # Zeitschranke zerlegt psycopg intern in kurze Abschnitte, damit Strg-C
        # sofort ankommt statt erst nach Ablauf.
        wartezeit = poll_seconds if rest is None else min(poll_seconds, rest)
        for _ in conn.notifies(timeout=wartezeit, stop_after=1):
            break

        # Nachgesehen wird IMMER, ob ein Signal kam oder nicht. Das ist die
        # Rueckfallabfrage: gegen abgerissene Verbindungen, verpasste Signale
        # und Schreibvorgaenge von aussen. Und es ist zugleich die Pruefung, ob
        # das Signal ueberhaupt zu UNSEREM Dokument gehoerte — auf dem Kanal
        # liegen die Signale aller Dokumente.
        zeile = get_document(conn, sha256)
        if zeile is not None and zeile.status.is_terminal:
            return zeile


# --------------------------------------------------------------------------
# Der abgeleitete Zustand
# --------------------------------------------------------------------------


def derive_status(row: DocumentRow, jobs: Sequence[Job]) -> DerivedStatus:
    """Setzt Zeile und Job-Liste zu einem Gesamtbild zusammen. Rein, ohne I/O.

    Die Regel, in der Reihenfolge, in der sie angewandt wird:
        1. Ist die Zeile endgueltig, IST das der Zustand. Es wird dann gar nicht
           mehr auf die Jobs gesehen — ein fertiges Dokument bleibt fertig, auch
           wenn spaeter noch ein Job dazu auftaucht.
        2. Sonst zaehlt der JUENGSTE Job (hoechste Kennung). „Juengster" und nicht
           „der Job", weil Job-Zeilen nie geloescht werden und es ueber die Zeit
           mehrere je Dokument gibt; der aelteste sagt nichts ueber jetzt.
        3. Gibt es keinen Job, ist das Dokument angelegt und nie eingereiht.
        4. Ist der juengste Job endgueltig, waehrend die Zeile `new` sagt, ist
           das `ENDED_WITHOUT_RESULT` — siehe dort.

    Warum das eine reine Funktion mit hereingereichter Job-Liste ist und nicht
    selbst abfragt: So laesst sich die Regel gegen erfundene Job-Listen
    durchspielen, ohne dass eine Datenbank laeuft. Die Regel ist der heikle Teil,
    die Abfrage ist Handwerk.

    Args:
        row: Die Zeile.
        jobs: Alle Jobs mit `queueing_lock == row.sha256`, in beliebiger
            Reihenfolge — sortiert wird hier.

    Returns:
        Das Gesamtbild.

    Kosten: keine.
    """
    anzahl = len(jobs)

    # Regel 1: Ist die Zeile endgueltig, IST das der Zustand. Auf die Jobs wird
    # dann gar nicht mehr gesehen — mit EINER Ausnahme, die keine Regel bricht:
    # Die Job-Angaben werden trotzdem mitgeliefert, weil sie beim Nachsehen
    # helfen („welcher Job hat das geschrieben, im wievielten Versuch?"). Sie
    # ENTSCHEIDEN nur nichts mehr.
    juengster = max(jobs, key=lambda job: job.id or 0) if jobs else None

    if row.status.is_terminal:
        phase = Phase.TERMINAL
    elif juengster is None:
        phase = Phase.REGISTERED
    elif juengster.status == "todo":
        phase = Phase.WAITING
    elif juengster.status == "doing":
        phase = Phase.RUNNING
    elif juengster.status in _TERMINALE_JOB_ZUSTAENDE:
        # Der Job ist fertig, die Zeile steht aber noch auf `new`: harter
        # Abschuss im Schreibfenster oder Abbruch beim Herunterfahren. Kein
        # Datenfehler — der Zustand, den das Ein-Buch-Modell erwartet.
        phase = Phase.ENDED_WITHOUT_RESULT
    else:
        # Ein Job-Zustand, den Procrastinate erst spaeter eingefuehrt hat. Ihn
        # als „laeuft noch" zu verbuchen waere die bequeme Luege; `WAITING` ist
        # die vorsichtige Wahl, weil sie das Dokument im Blick behaelt, statt es
        # faelschlich als abgeschlossen auszuweisen.
        phase = Phase.WAITING

    return DerivedStatus(
        sha256=row.sha256,
        row_status=row.status,
        phase=phase,
        job_id=juengster.id if juengster else None,
        job_status=juengster.status if juengster else None,
        attempts=juengster.attempts if juengster else None,
        job_count=anzahl,
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
    (`clock_timestamp()` gegen
    `last_heartbeat`, beides ihre Uhr): Der Client kann eine andere Zeit oder
    Zeitzone haben, und ein Vergleich mit seiner Uhr wuerde einen laufenden
    Daemon fuer tot erklaeren oder einen toten fuer lebendig. `clock_timestamp()`
    und nicht `now()`, weil `now()` innerhalb einer Transaktion auf ihren Beginn
    eingefroren ist.

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
        return [
            (kennung, gesehen)
            for kennung, gesehen in conn.execute(
                "SELECT id, last_heartbeat FROM procrastinate_workers "
                "WHERE last_heartbeat > "
                "clock_timestamp() - make_interval(secs => %s::double precision) "
                "ORDER BY last_heartbeat DESC",
                (DAEMON_HEARTBEAT_MAX_AGE_SECONDS,),
            ).fetchall()
        ]
    except psycopg.Error as fehler:
        raise DatabaseError(
            f"Die Worker-Lebenszeichen liessen sich nicht lesen: {fehler}\n"
            f"ACHTUNG, UNDOKUMENTIERTE FLAECHE: `procrastinate_workers` wird in "
            f"Procrastinates stabiler Dokumentation nicht als Schnittstelle "
            f"gefuehrt; Name und Spalten sind am Objekt der installierten "
            f"Fassung festgestellt (Feature-Verifikation Punkt 12). Wahrscheinlich"
            f" hat ein Procrastinate-Upgrade sie geaendert. Nachzusehen in "
            f"`procrastinate/sql/schema.sql` der installierten Fassung; die "
            f"Abfrage steht ausschliesslich in dieser Funktion. Der Fehler liegt "
            f"NICHT beim Aufrufer."
        ) from fehler
