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
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

import psycopg
from procrastinate.jobs import Job

# --------------------------------------------------------------------------
# Der Datenvertrag
# --------------------------------------------------------------------------

SCHEMA_SQL: Final = """
CREATE TABLE IF NOT EXISTS documents (
    sha256        text PRIMARY KEY,
    source_path   text NOT NULL,
    source_name   text NOT NULL,
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
CREATE INDEX IF NOT EXISTS documents_source_path_idx ON documents (source_path);
"""
"""Das vollstaendige Schema. Wortgleich zu Plan 4.1, mit zwei Zusaetzen.

Der CHECK auf `status` steht im Code, nicht nur im Kommentar: Die Aufzaehlung der
gueltigen Zustaende ist der Kern dieses Datenmodells, und eine Aufzaehlung, die
nur ein Kommentar ist, wird irgendwann von einem falsch geschriebenen Zustand
verletzt, den niemand bemerkt. Die Datenbank bemerkt ihn.

Die beiden Indizes folgen den Abfragen, die es tatsaechlich gibt: die Zaehlung
nach Zustand fuer `status()`/`report()`, und das Nachschlagen ueber den Pfad fuer
`mineru-ocr show PFAD`. Mehr Indizes waeren geraten; diese beiden sind
hergeleitet.

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
Dubletten-Erkennung ist ohnehin im DEVONthink-Vorlauf erledigt.

`source_path` zeigt auf das Original; bei Byte-Eingaben auf die Spool-Datei.
Liegt dieselbe Datei unter zwei Pfaden, gewinnt der zuerst gesehene, und der
zweite wird als Eintrag in `notes` vermerkt statt die Zeile zu ueberschreiben.

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
Mengengeruest: rund 0,5 bis 1 MB je Seite; bei 30.000 Dokumenten sind das
zweistellige Gigabyte, die Postgres auslagert. Der Plattenplatz gehoert vor dem
grossen Lauf geprueft (Plan 12.5). Warum die Ergebnisse in die Datenbank gehoeren
und nicht als Pfad daneben: Sie sind ab dem Schreiben die EINZIGE dauerhafte
Kopie — auf der Gegenseite liegt ein fertiges Ergebnis nur rund dreissig Minuten
zum Abholen bereit. Fuer die EINGABE-Bytes gilt das Umgekehrte, und deshalb steht
dort ein Pfad.

`image_tag_at_submit` ist Herkunft. Ohne diese Angabe an der Zeile laesst sich
eine spaetere Qualitaetsfrage nicht mehr beantworten.

`notes` ist eine Ereignisliste, kein Freitext: angehaengt wird, nie ersetzt.
Darin landen Aufruest-Hinweise, zweite Pfade, Auffaelligkeiten.

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
        raise NotImplementedError


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
    source_path: str
    source_name: str
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
    Schema ueber `app.schema_manager.apply_schema()` anwenden (die eingebaute
    Schnittstelle — NICHT das Kommando `procrastinate schema --apply`, das waere
    ein Unterprozess, der die Verbindungsangaben ein zweites Mal braeuchte);
    `SCHEMA_SQL` anwenden; nachsehen, ob die Tabelle auf der Platte zu
    `DocumentRow` passt.

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
    raise NotImplementedError


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
    raise NotImplementedError


async def connect_async(dsn: str) -> psycopg.AsyncConnection[Any]:
    """Oeffnet eine asynchrone Verbindung fuer die Daemon-Seite.

    Sonst wie `connect`, einschliesslich `autocommit`.
    """
    raise NotImplementedError


# --------------------------------------------------------------------------
# Zeilen anlegen und lesen — synchron, fuer die Fassade
# --------------------------------------------------------------------------


def insert_document(
    conn: psycopg.Connection[Any],
    *,
    sha256: str,
    source_path: str,
    source_name: str,
    size_bytes: int,
    mime: str,
    page_count: int | None,
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
        source_path: Absoluter Pfad zum Original oder zur Spool-Datei.
        source_name: Anzeigename, ueblicherweise der Dateiname.
        size_bytes: Rohe Groesse.
        mime: Die MIME-Zeichenkette aus `mime.DocumentType`.
        page_count: Seitenzahl bei PDF, sonst None.
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
    raise NotImplementedError


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
    raise NotImplementedError


def get_document_by_path(
    conn: psycopg.Connection[Any], source_path: str
) -> DocumentRow | None:
    """Liest eine Zeile ueber ihren Pfad — fuer `mineru-ocr show PFAD`.

    Returns:
        Die Zeile, oder None. Gibt es mehrere Zeilen zu demselben Pfad, kann es
        sie nicht geben: Der Pfad ist nicht eindeutig, der Hash ist es. Diese
        Funktion gibt dann die zuletzt angelegte zurueck — der Fall entsteht,
        wenn eine Datei am selben Pfad durch eine andere ersetzt wurde.
    """
    raise NotImplementedError


def list_documents(
    conn: psycopg.Connection[Any],
    *,
    status: DocumentStatus | None = None,
    limit: int | None = None,
) -> list[DocumentRow]:
    """Listet Zeilen, wahlweise nach Zustand gefiltert.

    Zusicherung:
        `raw` wird NICHT mitgeladen. Bei dreissigtausend Dokumenten waeren das
        zweistellige Gigabyte, und keine Liste braucht sie.

        Damit „nicht geladen" nicht mit „ist leer" verwechselt werden kann,
        traegt jede so gelesene Zeile `raw_loaded = False`. Ohne diese
        Unterscheidung liefert eine Huelle aus einer gelisteten Zeile fuer ein
        FERTIGES Dokument einen leeren Text — und das ist der Fehler, den niemand
        bemerkt, weil er wie ein leeres Ergebnis aussieht. Die Ergebnis-Getter der
        Huelle laden bei `raw_loaded is False` die Zeile einzeln nach, statt zu
        raten.

        Gelesen wird dafuer mit einer eigenen, schmalen Zeilenklasse statt mit
        `class_row(DocumentRow)` — dem fehlte sonst das Schluesselwort `raw`.
    """
    raise NotImplementedError


def counts_by_status(conn: psycopg.Connection[Any]) -> dict[DocumentStatus, int]:
    """Zaehlt die Zeilen je Zustand. Grundlage von `status()` und `report()`.

    Zusicherung:
        Jeder Zustand kommt im Ergebnis vor, auch mit Null. Ein fehlender
        Schluessel und eine Null sind fuer einen Leser dasselbe, fuer den Code
        aber nicht — und ein `KeyError` in einer Statusausgabe ist besonders
        aergerlich, weil er genau dann kommt, wenn alles gut lief.
    """
    raise NotImplementedError


# --------------------------------------------------------------------------
# Endergebnis schreiben — asynchron, ausschliesslich der Task
# --------------------------------------------------------------------------


async def write_terminal_async(
    conn: psycopg.AsyncConnection[Any],
    *,
    sha256: str,
    status: DocumentStatus,
    raw: dict[str, Any],
    image_tag_at_submit: str | None = None,
    error: str | None = None,
    notes: Sequence[dict[str, Any]] | None = None,
) -> None:
    """Schreibt das Endergebnis eines Durchlaufs und weckt alle Warter.

    Das ist der EINZIGE Schreibvorgang an einer Zeile zwischen ihrer Anlage und
    ihrem Endzustand. Er umfasst `raw`, `status`, `error`, angehaengte `notes`,
    `updated_at` und das Wecksignal in EINER Transaktion — es gibt keinen
    Zwischenzustand, in dem `raw` geschrieben ist und der Status noch `new` sagt.

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
    raise NotImplementedError


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
    """
    raise NotImplementedError


# --------------------------------------------------------------------------
# Warten
# --------------------------------------------------------------------------


def wait_for_terminal(
    conn: psycopg.Connection[Any],
    sha256: str,
    timeout: float | None = None,
    poll_seconds: float = 5.0,
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
    raise NotImplementedError


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
    raise NotImplementedError


# --------------------------------------------------------------------------
# Die eine undokumentierte Flaeche
# --------------------------------------------------------------------------


def worker_heartbeats(conn: psycopg.Connection[Any]) -> list[tuple[str, datetime]]:
    """Liest Procrastinates Worker-Lebenszeichen. UNDOKUMENTIERT — mit Vorsicht.

    Diese Funktion greift auf eine Tabelle zu, die Procrastinate in der
    stabilen Dokumentation NICHT als Schnittstelle fuehrt. Belegt ist nur, DASS
    es die Lebenszeichen in der Datenbank gibt und dass ein Django-Modell namens
    `ProcrastinateWorker` mit den Feldern `id` und `last_heartbeat` sie
    ausliefert; ein SQL-Tabellenname wird nirgends genannt, und eine Lese-
    Schnittstelle im `JobManager` gibt es nicht (Feature-Verifikation Punkt 12).

    Deshalb ist der Zugriff hier eingesperrt: EINE Funktion, ein Aufrufer (der
    Preflight), eine Warnung. Bei jedem Procrastinate-Upgrade gehoert sie erneut
    geprueft; bricht sie, bricht genau eine Stelle und nicht der Betrieb.

    Wozu ueberhaupt: Das haeufigste Betriebs-Missverstaendnis dieser Architektur
    ist „es passiert nichts" bei totem Daemon. Producer legen Zeilen an, Jobs
    stapeln sich, und alles sieht normal aus — nur arbeitet niemand. Der Client
    soll das SAGEN, statt es einen suchen zu lassen.

    Returns:
        Ein Eintrag je lebendem Worker mit Kennung und letztem Lebenszeichen.
        Leere Liste heisst: kein Worker laeuft. Das ist eine Feststellung, kein
        Fehler — im Ruhezustand ist genau das richtig.

    Raises:
        DatabaseError: Die Tabelle gibt es nicht (mehr) oder sie sieht anders aus
            als erwartet. Die Meldung sagt ausdruecklich, dass es sich um eine
            undokumentierte Flaeche handelt und wo nachzusehen ist — sonst sucht
            der naechste Leser den Fehler bei sich.

    Kosten: keine.
    """
    raise NotImplementedError
