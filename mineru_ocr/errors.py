"""Ausnahmen und die EINE Stelle, an der ueber Wiederholen oder Aufgeben entschieden wird.

Warum dieses Modul zuerst kommt und warum es keine zweite Entscheidungsstelle
geben darf: Der Plan (4.2) legt fest, dass die Frage „ist dieser Fehler
wiederholbar oder endgueltig?" von ZWEI Seiten gestellt wird — von Procrastinates
Retry-Strategie (die entscheidet, ob der Job noch einmal vorgelegt wird) und vom
Exception-Handler des Tasks (der entscheidet, ob er vor dem Werfen `status='failed'`
in die Zeile schreibt). Beantworten die beiden Seiten die Frage verschieden,
entsteht der schlimmste Zustand dieses Systems: ein Job, der noch einmal laeuft,
waehrend die Zeile schon `failed` sagt — oder ein Dokument, das ohne Ergebnis bleibt,
waehrend der Job endgueltig aufgegeben hat. Deshalb gibt es `classify_failure` genau einmal,
und beide Seiten rufen sie.

Die drei moeglichen Antworten sind nicht zwei, sondern drei, und das ist eine
Festlegung des Plans (5.1, Retry-Politik): Ein leeres Konto ist WEDER ein
wiederholbarer Fehler NOCH ein endgueltiger. Wuerde man es als wiederholbar
behandeln, verbrauchte jede Wartezeit einen der drei echten Versuche und der
Versuchszaehler waere kein ehrliches Mass fuer Fehlversuche mehr; wuerde man es
als endgueltig behandeln, waere ein Dokument fuer immer verloren, nur weil das
Konto fuer eine Viertelstunde leer war. Es ist eine dritte Sache: warten.
"""

from __future__ import annotations

import asyncio
import enum
from typing import Final

import httpx
import psycopg


class MineruOcrError(Exception):
    """Wurzel aller Ausnahmen dieses Pakets.

    Alles, was dieses Paket wirft und was ein Aufrufer sinnvoll fangen kann,
    haengt hier drunter. Ausnahmen fremder Bibliotheken (httpx, psycopg,
    procrastinate, pypdfium2) werden NICHT pauschal eingepackt — sie erreichen
    den Aufrufer roh, weil ihre eigene Typinformation mehr wert ist als eine
    zusaetzliche Huelle. `classify_failure` kennt sie trotzdem (siehe dort).
    """


# --------------------------------------------------------------------------
# Konfiguration
# --------------------------------------------------------------------------


class ConfigurationError(MineruOcrError):
    """Die lokale Einrichtung stimmt nicht — ohne Zutun des Menschen geht nichts.

    Faelle: kein API-Schluessel an der erwarteten Stelle, keine Datenbank
    erreichbar. Immer endgueltig, immer VOR
    jedem RunPod-Kontakt, nie ein Grund fuer einen Retry.
    """


class ConfigDriftError(ConfigurationError):
    """Der Endpoint steht anders da, als das Betriebshandbuch ihn festlegt.

    Wird nur im strengen Preflight geworfen (`OCRClient(strict=True)`); im
    nachsichtigen Modus wird derselbe Sachverhalt als Warnung gemeldet. Der
    Preflight MUTIERT NIE — er stellt fest und meldet, er richtet nichts (Plan 8,
    Regel 10: bezahlte Ressourcen werden nie eigenmaechtig angelegt oder
    geaendert).
    """


# --------------------------------------------------------------------------
# Eingabefehler — endgueltig OHNE jeden RunPod-Kontakt
# --------------------------------------------------------------------------


class InputError(MineruOcrError):
    """Mit dieser Datei kann nicht gearbeitet werden; RunPod wird gar nicht erst gefragt.

    Alles unter dieser Klasse fuehrt zum Zeilen-Status `input_error` (Plan 4.1).
    Der Unterschied zu `failed` ist nicht die Schwere, sondern der Ort:
    `input_error` heisst „vor dem Absenden aussortiert", `failed` heisst „die
    Engine oder der Transport hat es nicht geschafft".

    In aller Regel kostet das nichts — mit EINER Ausnahme, die man kennen muss,
    sonst rechnet man die Ausgaben zu niedrig: `TooLargeError` aus dem Zerlegen
    kann fallen, nachdem schon Teilstuecke bezahlt wurden (siehe dort).
    """


class UnsupportedTypeError(InputError):
    """Der Dateityp gehoert nicht zu den unterstuetzten.

    Sonderfall im Ablauf: Diese Ausnahme erreicht die Datenbank NIE als
    `input_error`, weil sie schon beim Abschicken fliegt, bevor eine Zeile
    angelegt wird (Plan 4.1, `OCRClient.ocr_start` wirft sofort — Jurgens
    Vorgabe 2.1: „rejected falls nicht unterstuetzter type"), und zwar bevor
    irgendein Dokument derselben Sendung eingereiht ist. Nur die Kommandozeile
    faengt sie in einem Ordner, zaehlt sie und geht weiter, statt an der ersten
    `.DS_Store` zu sterben.
    """


class CorruptDocumentError(InputError):
    """Die Datei gibt sich als PDF aus, laesst sich aber nicht verlaesslich lesen oder schneiden.

    Der wichtigste Fall dahinter ist keine kaputte Datei, sondern eine geerbte
    Falle: MinerUs eigene Schneidefunktion ueberspringt kaputte Seiten STILL
    („Skipped broken PDF pages"). Wir benutzen sie deshalb nicht und rechnen
    nach jedem Schnitt die Summe der Chunk-Seitenzahlen gegen die Gesamtseitenzahl
    (Plan 6). Stimmen sie nicht ueberein, fliegt diese Ausnahme — laut statt
    still, weil eine fehlende Seite in einem 30.000-Dokument-Lauf sonst niemandem
    auffaellt.
    """


class MultiImageError(InputError):
    """Die Bilddatei enthaelt mehr als ein Bild (etwa ein mehrseitiges Fax als TIFF).

    Die Engine liest von einer Bilddatei nur das erste Bild; alle weiteren gingen
    still verloren, und das Dokument endete als einseitiges `done`. Deshalb endet
    die Datei vor jedem RunPod-Kontakt als `input_error`, mit dem Hinweis, sie als
    PDF einzureichen. Mehrseitige Bilder selbst in eine PDF umzuwandeln ist
    ausdruecklich nicht Aufgabe des Pakets (Mineroo#1, Out of Scope).
    """


class WorkerRejectedError(InputError):
    """Der Worker hat die Datei als nicht lesbar abgewiesen; der Grund steht in seiner Antwort.

    Die Ausnahme von der Regel der Oberklasse, „RunPod wird gar nicht erst
    gefragt": Hier hat RunPod geantwortet, und zwar, dass MinerU mit dieser
    Datei nichts anfangen kann. Das ist eine Aussage ueber die Eingabe, nicht
    ueber die Engine, und endet deshalb wie die anderen Eingabefehler als
    `input_error` (Mineroo#11); ein weiterer Versuch kaufte dieselbe Abweisung.
    Ob eine `FAILED`-Antwort eine solche Abweisung ist, entscheidet
    `ist_dateiabweisung`. Die Meldung traegt den Grund aus der Antwort.
    """


DATEIABWEISUNG_KENNUNGEN: Final = (
    "ValueError: input is not a file type MinerU",
    "InvalidRequestError:",
    "UnidentifiedImageError: cannot identify image file",
    "OSError: Truncated File Read",
    "OSError: image file is truncated",
    "OSError: broken data stream when reading image file",
    # DocVortex 0.5.16 fuer Ganzdokument-Formate (Mineroo#17).
    "LegacyOfficeError:",
    "LegacyOfficeMalformedError:",
    "LegacyOfficeMissingPartError:",
    "LegacyOfficeEncryptedError:",
    "LegacyOfficeResourceLimitError:",
    "OdfParseError:",
    "OdfEncryptedError:",
    "OdfResourceLimitError:",
    "EpubError:",
    "EpubParseError:",
    "EpubEncryptedError:",
    "EpubResourceLimitError:",
    "OfdParseError:",
    "OfdEncryptedError:",
    "OfdResourceLimitError:",
    "HtmlParseError:",
    "HtmlResourceLimitError:",
)
"""Die Anfaenge des `error` einer `FAILED`-Antwort, an denen der Worker eine Datei
als nicht lesbar abweist.

AUS DEN QUELLEN des MinerU-4-Workers (Fork `ConstantinLevin/mineru-runpod`) und
von MinerU 4.0.12, nicht geraten:

    `ValueError: input is not a file type MinerU …` ist die eigene Meldung des
    Workers fuer eine Datei, deren Typ MinerU nicht liest (`worker/parse.py`,
    `run_mineru`).

    `InvalidRequestError: …` ist MinerUs eigene Abweisung; der Worker behaelt
    MinerUs Fehlertyp (Job-Vertrag `api.mdx`, „Validation behaviour", letzter
    Absatz). Im Lesepfad von MinerU 4.0.12 entsteht sie an der Seitenauswahl,
    etwa bei einer PDF ohne Seite („document has no available pages"); einen
    Seitenbereich schickt das Paket nicht.

    `UnidentifiedImageError: cannot identify image file …`,
    `OSError: Truncated File Read`, `OSError: image file is truncated …` und
    `OSError: broken data stream when reading image file` sind die Meldungen
    von Pillow fuer Bilddaten, die sich nicht dekodieren lassen (Pillow
    `Image.open` und `ImageFile`; die letzte ist der Dekoderstatus -2, „broken
    data stream"); MinerU 4.0.12 laesst sie aus `_prepare_input` durch, und der
    Worker behaelt den Fehlertyp. Am Endpoint beobachtet fuer eine PNG-, GIF-
    und JPEG-Signatur vor Muell, fuer ein in der Mitte abgeschnittenes PNG und
    JPEG und fuer ein PNG mit intaktem Kopf und kaputten Pixeldaten
    (`artifacts/2026-10-26-mineru-4.0.12-endpoint/`, `kaputte_pixel`): Der Typ
    wird erkannt, gelesen werden kann die Datei nicht, und jeder weitere Lauf
    kaufte dieselbe Meldung. Gelistet sind nur diese Meldungen, nicht `OSError`
    allgemein: Ein voller Datentraeger ist auch ein `OSError` und sagt nichts
    ueber die Datei.

    Die Fehlertypen, mit denen DocVortex 0.5.16 (in MinerU 4.0.12) ein
    Ganzdokument als nicht lesbar abweist, je Format aus
    `analyzers/native/{office,office/odf,epub,ofd,html}/errors.py`: ein Container
    oder Datensatz, der kein gueltiges Dokument ergibt (`…MalformedError`,
    `…ParseError`), ein fehlender Teil (`LegacyOfficeMissingPartError`, „missing
    required OLE stream"), eine Verschluesselung, die der reine Python-Weg nicht
    liest (`…EncryptedError`), und eine Eingabe ueber den festen
    Sicherheitsgrenzen (`…ResourceLimitError`); dazu die Oberklassen
    `LegacyOfficeError` und `EpubError`. Alle erben von `ValueError`, und der
    Worker behaelt den Typnamen. Am Endpoint beobachtet fuer einen
    OLE2-Container ohne den Strom `WordDocument`
    (`artifacts/2026-10-27-ganzdokumente/`, `ole2_ohne_word_daten`): Die
    Vorerkennung des Pakets laesst ihn durch, MinerU liest ihn als DOC und
    scheitert. Jeder weitere Lauf kaufte dieselbe Meldung. Gemeint ist immer der
    ganze Typname mit Doppelpunkt; ein Typ, der nur so beginnt, ist keiner davon.

Was NICHT darunter faellt und `failed` bleibt: die Pruefungen der Anfrage durch
den Worker (`ValueError: input validation failed: …` und die uebrigen
`ValueError` seiner Schema-Pruefung) und jeder andere Fehler der Engine, auch
einer, den ein Ganzdokument mit einem anderen Typ ausloest (etwa ein OOXML-Paket,
an dem eine Bibliothek unter DocVortex mit einem allgemeinen Fehler scheitert)."""


def ist_dateiabweisung(fehlertext: str) -> bool:
    """Ob das `error` einer `FAILED`-Antwort eine Abweisung der Datei ist.

    Die eine Stelle, an der das entschieden wird; der Executor uebersetzt eine
    solche Antwort in `WorkerRejectedError`, und `classify_failure` stuft die wie
    jede `InputError` als endgueltig ein. Kosten: keine.
    """
    return fehlertext.startswith(DATEIABWEISUNG_KENNUNGEN)


class TooLargeError(InputError):
    """Die Datei passt nicht unter eine Grenze und laesst sich nicht zerlegen.

    Gemeint sind die Payload-Grenze einer Anfrage und, beim Abschicken, die
    Grenze, die Postgres einem Feld setzt (`db.MAX_INPUT_BYTES`).

    Drei Faelle: eine einzelne PDF-Seite ueber der Grenze, eine Nicht-PDF-Datei
    ueber der Grenze (DOCX, TIFF — nicht seitenweise zerlegbar), und beim
    Abschicken eine Eingabe ueber `db.MAX_INPUT_BYTES`, die sich nicht in
    `inputs` ablegen laesst (dann entsteht keine Zeile; die Kommandozeile zaehlt
    sie in einem Ordner als uebersprungen). Eine zu grosse PDF-Einzelseite wird
    zuerst mit 200 dpi und hoechstens 3500 Pixeln an der laengsten Kante gerendert
    (Mineroo#49); erst wenn auch dieses Bild nicht passt, wird geworfen.

    Der Hinweis landet als Eintrag in `notes` der Zeile. Den Aufgaben-Eintrag legt
    NICHT der Daemon an — er wird ueber `status()` sichtbar, und der Mensch
    bzw. die Sitzung, die den Bericht liest, legt ihn an (Plan 4.1). Ein
    Hintergrundprozess, der Tickets erzeugt, ist ein Seiteneffekt, den niemand
    erwartet.

    DIE EINE AUSNAHME von der Kostenlos-Regel der Oberklasse: Der Seiten-Fall
    faellt erst beim Zerlegen auf, moeglicherweise NACHDEM schon Teilstuecke
    verschickt und bezahlt wurden. Eine `input_error`-Zeile kann deshalb sehr
    wohl bezahlte Ergebnisse in `raw` tragen. Wer die Ausgaben auswertet und
    `input_error` pauschal als kostenlos zaehlt, rechnet zu niedrig.
    """


# --------------------------------------------------------------------------
# RunPod-Seite
# --------------------------------------------------------------------------


class RunPodError(MineruOcrError):
    """Oberklasse fuer alles, was von der Engine-Seite kommt."""


class TransientRunPodError(RunPodError):
    """Vorruebergehend — derselbe Aufruf kann spaeter gelingen.

    Faelle: Transportfehler (Verbindung, Zeitueberschreitung), HTTP 429/5xx und der
    Job-Endzustand `TIMED_OUT`. Fuehrt zu einem Retry nach der Strategie in
    `tasks.py`; nach Erschoepfung der Versuche wird daraus `failed`.
    """


class TerminalRunPodError(RunPodError):
    """Endgueltig — ein weiterer Versuch wuerde dasselbe Ergebnis kaufen.

    Faelle: Job-Endzustand `FAILED` oder `CANCELLED`, Antworten, die nicht der
    erwarteten Form entsprechen (Schema-/Validierungsfehler), HTTP 4xx ausser den
    eigens behandelten (401/403 sind Konfiguration, 402 ist Guthaben, 404 auf
    /status ist Retention, 429 ist ein voruebergehendes Rate-Limit).
    """


class IncompleteResponseError(TerminalRunPodError):
    """Die Antwort eines Teilstuecks ist erfolgreich, aber unvollstaendig.

    Der Worker meldet `COMPLETED`, und trotzdem fehlt etwas, das das Paket
    braucht: Seiten, die MinerU nicht verarbeitet hat (`MissingPagesError`),
    oder eine Datei, ohne die das Ergebnis nicht darstellbar ist
    (`MissingArtifactError`). Geprueft wird nach jedem Teilstueck
    (`vollstaendigkeit.teilstueck_pruefen`, Mineroo#12).

    ENDGUELTIG ueber die Oberklasse, also ueber `classify_failure` und nicht
    ueber eine eigene Fallunterscheidung: Ein weiterer Versuch kaufte mit hoher
    Wahrscheinlichkeit dieselbe Antwort (eine Seite, die MinerU verwirft,
    verwirft es wieder). Das Dokument endet `failed`, nicht `input_error`:
    Gefehlt hat etwas in der Antwort der Engine, und ob es an der Datei lag,
    sagt die Antwort nicht. Die bezahlte Antwort steht dabei vollstaendig in
    `raw`, und `retry` liest das Dokument neu, wenn jemand es noch einmal will.

    Ohne diese Pruefung endete ein solches Dokument `done`: Fehlende Seiten
    fielen niemandem auf, und ohne `middle_json.json` scheiterte jede
    Darstellung danach dauerhaft, ohne dass `retry` oder erneutes Abschicken es
    zurueckholte (Code-Review zu Mineroo#11, Befund A1).
    """


class MissingPagesError(IncompleteResponseError):
    """MinerU hat andere Seiten verarbeitet, als das Paket geschickt hat.

    Verglichen werden die Seiten des Middle-JSON eines Teilstuecks mit dem
    Seitenbereich, den das Paket geschickt hat, bei PDFs; ein Bild zaehlt als
    eine Seite. Ein Dokument wird nur `done`, wenn MinerU so viele Seiten
    verarbeitet hat, wie das Paket geschickt hat (Mineroo#1, Story 13).
    """


class MissingArtifactError(IncompleteResponseError):
    """Der Antwort fehlt eine Datei, die das Paket braucht, oder der Worker meldet sie verloren.

    Welche Dateien das Paket braucht, steht an einer Stelle,
    `vollstaendigkeit.BENOETIGTE_DATEIEN`. Fehlt eine davon im Archiv, oder
    nennt der Worker sie in `degraded` (Job-Vertrag, „Incomplete responses":
    eine Datei, die er nicht lesen konnte, fehlt dann oder kommt als leerer
    Ersatzwert), endet das Dokument terminal. Eine andere Datei in `degraded`
    wird nur vermerkt (Mineroo#1, Story 14).
    """


class JobAufgegebenError(TransientRunPodError):
    """Das Paket hat einen Job aufgegeben und `/cancel` dafuer bereits versucht.

    Wiederholbar wie jeder `TransientRunPodError`; `classify_failure` ordnet sie
    dort ein. Eigene Klasse, damit der Executor am Typ erkennt, dass der Abbruch
    schon versucht wurde: Er schickt ihn fuer jede andere Ausnahme, mit der das
    Warten auf einen womoeglich noch laufenden Job endet, und dieselbe Kennung
    nicht ein zweites Mal, ausser wenn der Versuch misslang.

    Attributes:
        abgebrochen: `True`, wenn RunPod den Abbruch angenommen hat. `False`,
            wenn er misslang (etwa weil RunPod nicht zu erreichen war): Dann
            laeuft der Job womoeglich weiter, und der Executor versucht den
            Abbruch noch einmal, bevor die Wiederholung dasselbe Teilstueck ein
            zweites Mal absendet und bezahlt.
    """

    def __init__(self, nachricht: str, *, abgebrochen: bool = True) -> None:
        super().__init__(nachricht)
        self.abgebrochen = abgebrochen


class ResultLostError(TransientRunPodError):
    """`GET /status/<id>` antwortet 404: das Ergebnis ist nicht mehr abrufbar.

    Ein fertiges Ergebnis liegt auf der Gegenseite rund dreissig Minuten zum
    Abholen bereit; danach ist es weg — und zwar weg, BEVOR es je bei uns war.
    Was einmal in unserer Datenbank steht, bleibt dort; dieses Fenster betrifft
    ausschliesslich die Spanne zwischen Absenden und Abholen.

    WIEDERHOLBAR, nicht endgueltig. Weiterpollen kann zwar nie mehr etwas
    liefern, aber ein neuer Versuch schickt einen NEUEN Job und bekommt ein neues
    Ergebnis — das ist kein „dasselbe Ergebnis zweimal kaufen", sondern genau die
    Doppelzahlung, die dieses Projekt an jeder vergleichbaren Stelle bewusst
    hinnimmt: „es wird dann ncohmal und das wars" (Plan 2.4). Ein verpasstes
    Abholfenster ist kein Grund, ein Dokument endgueltig aufzugeben — und es ist
    nicht einmal ein Fehler der Gegenseite.

    Dass der Fall ueberhaupt eintritt, ist ein Fehler UNSERER Orchestrierung, und
    dagegen ist der Executor gebaut (Schritt 2a dort): Es wird nie erst alles
    abgesendet und spaeter eingesammelt; je Dokument ist hoechstens ein Ergebnis
    offen und wird innerhalb eines Abfrageintervalls abgeholt. Solange der Prozess
    lebt, kann dieser Zustand nicht entstehen. Er bleibt als eigene Klasse
    stehen, weil er nach einem Absturz oder einer langen Zwangspause doch
    auftreten kann — und weil er dann als das benannt werden soll, was er ist,
    statt als anonymer HTTP-Fehler.
    """


class InsufficientBalanceError(RunPodError):
    """Das Konto reicht nicht — WEDER Fehler NOCH Retry, sondern Grund zu warten.

    Ausgeloest von HTTP 402, ElevenLabs' `quota_exceeded` oder vom Kontostand
    unter der Pausenschwelle. Der
    Task faengt diese Ausnahme selbst und geht in eine Warteschleife (Plan 5.1);
    sie erreicht die Retry-Strategie im Normalfall gar nicht. Sie steht trotzdem
    in `classify_failure`, damit die Antwort auch dann stimmt, wenn sie doch
    einmal durchrutscht.

    Zwei Schwellen, zwei verschiedene Zwecke (Plan 5.1): Der Preflight verlangt
    mindestens einen Dollar, damit eine Arbeit gar nicht erst mit leerem Puffer
    beginnt. Die Lauf-Pause greift bei fuenfzig Cent — frueher als der historische
    Abbruch der Ad-hoc-Skripte bei fuenfzehn Cent, weil Pausieren nichts kostet
    und der verbleibende Puffer die schon laufenden Chunks noch zu Ende bezahlen
    koennen muss.
    """


# --------------------------------------------------------------------------
# Datenbank
# --------------------------------------------------------------------------


class DatabaseError(MineruOcrError):
    """Oberklasse fuer Fehler an der eigenen `documents`-Tabelle."""


class DocumentNotFound(DatabaseError):
    """Zu diesem Hash gibt es nicht, was gefragt ist.

    Beim `wait` und bei `document` heisst das: weder Ergebnis noch Eingabe. Der
    Aufrufer wartet auf etwas, das nie abgeschickt wurde — und bekommt das
    sofort gesagt, statt bis zum Timeout zu warten. Bei `get` und an einer Huelle
    heisst es: Es gibt (noch) kein Ergebnis; die Meldung sagt, ob das Dokument
    offen oder unbekannt ist.
    """


class UnstorableResultError(DatabaseError):
    """Postgres weist das Ergebnis eines Jobs ab, weil sein Inhalt nicht speicherbar ist.

    Faelle: ein U+0000 im Text, den `jsonb` nicht tragen kann (SQLSTATE 22P05),
    eine Groesse ueber der Grenze von `jsonb` (54000), allgemein ein Datenfehler
    (Klasse 22) oder eine Programmgrenze (Klasse 54) beim Endschreiben, dazu ein
    `raw`, das groesser ist als eine Nachricht an Postgres (`db.MAX_RAW_JSON_BYTES`,
    vor dem Senden geprueft, ohne SQLSTATE). Geworfen
    nur von `db.write_terminal_async`, das den rohen psycopg-Fehler darin
    einpackt; `sqlstate` traegt dessen Code (None beim Fehler, den psycopg selbst
    vor dem Senden wirft).

    Anders als jeder andere `DatabaseError` ENDGUELTIG: Ein weiterer Lauf liefert
    denselben Inhalt, und Postgres weist ihn wieder ab. Wiederholt hiesse, das
    Dokument dreimal zu bezahlen und es am Ende ohne Ergebnis liegen zu lassen
    (Mineroo#27). Der Executor schreibt dann `failed` ohne den Inhalt, mit einem
    Vermerk, der den Grund nennt; den Inhalt veraendert er nicht, um ihn
    speicherbar zu machen.
    """

    def __init__(self, nachricht: str, *, sqlstate: str | None = None) -> None:
        super().__init__(nachricht)
        self.sqlstate = sqlstate


class WaitTimeoutError(MineruOcrError, TimeoutError):
    """Die Frist eines Warte-Aufrufs ist abgelaufen, und Dokumente sind noch offen.

    Zugleich ein `TimeoutError`: Wer allgemein auf einen Zeitablauf faengt, faengt
    auch diesen. Die Frist gilt fuer den ganzen Aufruf, nicht je Dokument, und sie
    beendet nur das Warten: Kein Job und kein Dokument wird angefasst, die offenen
    koennen danach noch fertig werden. Der Zeitablauf sagt etwas ueber die Geduld
    des Wartenden, nichts ueber die Dokumente.
    """

    def __init__(
        self,
        nachricht: str,
        *,
        pending: tuple[str, ...] = (),
        timeout: float | None = None,
    ) -> None:
        super().__init__(nachricht)
        self.pending = pending
        """Die Hashes, die bei Ablauf noch offen waren, in der Reihenfolge der
        Frage. Mit ihnen laesst sich spaeter erneut warten."""
        self.timeout = timeout


class TerminalOverwriteError(DatabaseError):
    """Es wurde versucht, ein bereits geschriebenes Endergebnis zu ueberschreiben.

    Das ist ein Programmierfehler, kein Betriebsfall: Das Nachsehen des Tasks
    (Plan 5.1, Schritt 0) haelt jeden Wiederlauf ueber einem fertigen Dokument auf,
    bevor es dazu kommen kann. Die Ausnahme ist das Netz darunter — sie faengt
    genau den Fall, dass das Nachsehen einmal falsch gesetzt wird.

    Der EINE erlaubte Ueberschreibungsweg ist der Wiederanlauf eines gescheiterten
    Dokuments (Plan 4.2): Ein per `retry_job_by_id` wieder vorgelegter Job MUSS
    das Dokument neu verarbeiten und sein `raw` beim Terminal-Schreiben
    ueberschreiben duerfen. Dafuer gibt es in `db.py` einen eigenen, benannten
    Weg — nicht ein Flag an dieser Ausnahme vorbei.
    """


# --------------------------------------------------------------------------
# Ergebnis-Abruf
# --------------------------------------------------------------------------


class IncompleteResultError(MineruOcrError):
    """Ein Gesamtergebnis wurde abgefragt und kann nicht ganz geliefert werden.

    Die Getter der Fassade werfen lieber, als einen Teil zu liefern. Grund: Ein
    aeusserlich vollstaendiges Ergebnis, dem in der Mitte zwanzig Seiten fehlen,
    ist der Fehler, den niemand bemerkt — genau der, an dem die Ad-hoc-Skripte
    einmal vorbeigelaufen sind. Wer Teile sehen will, greift auf die Rohantworten
    zu; die stehen offen.

    DREI ANLAESSE, und der zweite stand hier lange nicht, obwohl der Code ihn
    schon warf:

        Die Zeile ist noch nicht so weit — abgefragt, bevor alle Chunks
        erfolgreich terminal sind. Es gibt schlicht noch nichts.

        Die Zeile ist BESCHAEDIGT — ein Feld des gespeicherten Ergebnisses hat
        einen anderen Typ als zugesichert (der haeufigste Fall ist JSON-`null`),
        ein Eintrag von `middle_json` traegt keine Seiten, oder das Ergebnis
        traegt weder `markdown_full` noch `markdown` noch `middle_json` und ist
        damit gar kein zusammengesetztes Ergebnis. Geworfen wird auch hier,
        statt einen leeren Wert zu liefern: Ein leeres Feld sieht aus wie ein
        leeres Dokument, und genau daran ist die Beschaedigung sonst nicht mehr
        zu erkennen. Zustaendig sind `OCRWrapper._feld` und
        `assembly._pflichtfeld`.

        Es gibt KEINE GANZE VOLLFASSUNG — einem Teilstueck fehlte
        `markdown_full.md`, `assemble` hat `markdown_full` deshalb auf None
        gesetzt, und `assembly.rendered_markdown` liefert keinen Text aus Teilen.
        Beschaedigt ist dabei nichts; die Meldung nennt, wo das Bezahlte liegt.
    """


# --------------------------------------------------------------------------
# Die eine Entscheidung
# --------------------------------------------------------------------------


def elevenlabs_failure(response: httpx.Response, *, api_key: str) -> Exception:
    """Uebersetzt die HTTP-Abweisung in die gemeinsamen Fehlertypen.

    ElevenLabs meldet `quota_exceeded` auch mit HTTP 400/401; die Kennung hat
    deshalb Vorrang vor dem HTTP-Status. Quelle: ElevenLabs, Technical FAQ,
    `api-error-code-400-or-401`. Fremde Header und Antwortkoerper bleiben aus
    der Ausnahme heraus; nur eine Datei-Abweisung behaelt den maskierten Grund.
    """
    try:
        antwort = response.json()
    except ValueError:
        antwort = None
    detail = antwort.get("detail") if isinstance(antwort, dict) else None
    kennung = detail.get("status") if isinstance(detail, dict) else None
    code = response.status_code
    if kennung == "quota_exceeded" or code == 402:
        return InsufficientBalanceError("ElevenLabs: Kontingent erschoepft.")
    if code in (401, 403):
        return ConfigurationError("ElevenLabs: Schluessel ungueltig oder Zugriff verweigert.")
    if code == 422:
        grund = "Audiodatei nicht lesbar"
        if isinstance(detail, str):
            grund = detail
        elif isinstance(detail, dict) and isinstance(detail.get("message"), str):
            grund = detail["message"]
        elif isinstance(detail, list):
            meldungen = [eintrag["msg"] for eintrag in detail
                         if isinstance(eintrag, dict) and isinstance(eintrag.get("msg"), str)]
            if meldungen:
                grund = "; ".join(meldungen)
        if api_key:
            grund = grund.replace(api_key, "[maskiert]")
        grund = grund.replace("\x00", "[NUL]")
        return WorkerRejectedError(f"ElevenLabs: {grund} (HTTP 422).")
    # Auch response.request darf keinen xi-api-key aus der echten Anfrage tragen.
    anfrage = httpx.Request("POST", "https://api.elevenlabs.io/v1/speech-to-text")
    return httpx.HTTPStatusError(
        f"ElevenLabs: HTTP {code} bei der Transkription.", request=anfrage,
        response=httpx.Response(code, request=anfrage),
    )


class FailureKind(enum.Enum):
    """Wie mit einem Fehlschlag umzugehen ist. Drei Antworten, mehr gibt es nicht."""

    RETRYABLE = "retryable"
    """Noch einmal versuchen. Der Versuchszaehler des Jobs zaehlt hoch.

    Achtung auf die Reichweite dieses Zaehlers: `attempts` steigt bei JEDER
    Wiedervorlage, nicht nur bei Strategie-Retries. Die stalled-Wiederaufnahme
    des Waechters und ein von Hand ausgeloester Retry laufen durch dieselbe
    SQL-Funktion `procrastinate_retry_job_v2` und erhoehen ihn ebenso
    (Feature-Verifikation Punkt 19, am Objekt von 3.9.0 belegt, fuer 3.10.0 unveraendert).
    Ein Retry von Hand auf einen erschoepften Job ist deshalb genau EIN weiterer
    Versuch, keine neue Dreier-Runde.
    """

    TERMINAL = "terminal"
    """Aufgeben. Die Zeile wird `failed` (oder `input_error`), bevor geworfen wird."""

    BALANCE_PAUSE = "balance_pause"
    """Warten, nicht zaehlen. Der Task haelt selbst an und prueft den Kontostand erneut."""


def classify_failure(exc: BaseException) -> FailureKind:
    """Beantwortet fuer JEDE Ausnahme, wie mit ihr umzugehen ist.

    Dies ist die einzige Stelle im Paket, an der diese Frage beantwortet wird.
    Beide Aufrufer — die Retry-Strategie in `tasks.py` und der Exception-Handler
    in `executor.py` — rufen ausschliesslich hier an. Wer eine zweite
    Entscheidungsstelle einbaut, hat den Fehler eingebaut, gegen den dieses Modul
    steht.

    Args:
        exc: Die aufgetretene Ausnahme. Die vollstaendige Zuordnung, damit kein
            Fall offenbleibt und der Vollstaendigkeits-Test einen Sollwert hat:

            WIEDERHOLBAR — `TransientRunPodError` samt `ResultLostError`;
            `httpx.TimeoutException` und `httpx.TransportError`,
            `httpx.HTTPStatusError` bei HTTP 429/5xx; `DatabaseError`
            und seine Unterklassen ausser `DocumentNotFound`,
            `TerminalOverwriteError` und `UnstorableResultError` (die Datenbank ist
            lokal, ein Aussetzer ist fast immer voruebergehend); rohe
            `psycopg`-Fehler aus demselben Grund; `asyncio.CancelledError`,
            der Abbruch beim Herunterfahren (siehe unten).

            ENDGUELTIG — `TerminalRunPodError`, auch die unvollstaendige
            Antwort (`IncompleteResponseError`: fehlende Seiten, eine fehlende
            Datei, die das Paket braucht); jede `InputError`;
            `ConfigurationError` samt `ConfigDriftError` (Wiederholen macht einen
            untauglichen Schluessel nicht gueltig); `DocumentNotFound` (die Zeile
            ist geloescht, dreimal nachsehen aendert daran nichts);
            `UnstorableResultError` (Postgres weist das Ergebnis wegen seines
            Inhalts ab, und ein weiterer Lauf liefert denselben Inhalt);
            `TerminalOverwriteError` und `IncompleteResultError` (beides
            Programmierfehler — wiederholt werden sie nur teurer); und alles
            Unbekannte.

            WARTEN — `InsufficientBalanceError`.

    Returns:
        Die Behandlungsart.

    Zusicherung:
        Vollstaendig — fuer jede Eingabe kommt genau eine der drei Antworten
        heraus, es gibt keinen Pfad ohne Rueckgabe. Rein: keine Seiteneffekte,
        kein I/O, kein Zustand. Deterministisch: dieselbe Ausnahme ergibt immer
        dieselbe Antwort, damit Strategie und Handler nie auseinanderlaufen.

    Warum Unbekanntes ENDGUELTIG ist und nicht wiederholbar:
        Ein unbekannter Fehler ist mit hoher Wahrscheinlichkeit ein Fehler in
        unserem eigenen Code — ein Tippfehler, ein falscher Feldname, eine
        Annahme, die nicht traegt. So etwas wird durch Wiederholen nicht besser,
        sondern nur dreimal so teuer, und zwar an jedem Dokument des Bestands.
        Die Voreinstellung „aufgeben und sichtbar scheitern" macht den Fehler in
        `status()` sofort sichtbar, statt ihn in einem stillen Retry-Karussell zu
        verstecken.

    Warum der Abbruch WIEDERHOLBAR ist:
        Faehrt der Daemon herunter, bricht Procrastinate die noch laufenden Jobs
        mit `asyncio.CancelledError` ab und fragt dann die Retry-Strategie;
        sagt sie ja, reiht es den Job wieder ein, und die Wiedervorlage zaehlt
        als Versuch (gewollt und durch Procrastinates eigene Abnahmetests
        abgesichert, PR #1215, steht aber nicht in seiner Doku zum
        Herunterfahren). Als „Unbekanntes" eingestuft, endete jeder solche Job
        `aborted`, und sein Dokument bliebe ohne Ergebnis liegen. Die Regel, die
        das Modell traegt: Jeder Job, der nicht neu eingereiht wird, schreibt
        ein Ergebnis (Mineroo#27). Im letzten Versuch schreibt der Executor
        deshalb `failed` und wirft dann weiter; der Job endet `aborted`.

        Ein Abbruch ueber die API (`cancel_job_by_id(…, abort=True)`) kommt
        beim Task als derselbe `CancelledError` an und ist an der Ausnahme nicht
        zu unterscheiden. Neu eingereiht wird er trotzdem nicht:
        `procrastinate_retry_job_v2` setzt einen Job mit `abort_requested` auf
        `failed` statt auf `todo` (Procrastinate 3.10.0).

    Kosten: keine. Beruehrt weder Netz noch Datenbank.
    """
    # Die Reihenfolge ist bindend, weil die Klassen sich ueberdecken. Wer sie
    # umstellt, aendert Antworten, ohne eine Zeile Bedingung anzufassen:
    #
    #   InsufficientBalanceError IST ein RunPodError — stuende die RunPod-Zeile
    #   davor, wuerde aus „warten" ein „aufgeben".
    #   DocumentNotFound, TerminalOverwriteError und UnstorableResultError SIND
    #   DatabaseError — stuende die DatabaseError-Zeile davor, liefe ein
    #   geloeschtes Dokument dreimal, und ein nicht speicherbares Ergebnis wuerde
    #   dreimal bezahlt und bliebe doch ohne Ergebnis.
    #
    # Deshalb: die Sonderfaelle vor ihren Oberklassen.

    if isinstance(exc, InsufficientBalanceError):
        return FailureKind.BALANCE_PAUSE

    if isinstance(
        exc, (DocumentNotFound, TerminalOverwriteError, UnstorableResultError)
    ):
        return FailureKind.TERMINAL

    if isinstance(exc, DatabaseError):
        return FailureKind.RETRYABLE

    if isinstance(exc, TransientRunPodError):
        return FailureKind.RETRYABLE

    if isinstance(exc, (TerminalRunPodError, InputError, ConfigurationError)):
        return FailureKind.TERMINAL

    if isinstance(exc, IncompleteResultError):
        return FailureKind.TERMINAL

    # Fremd-Ausnahmen: Netz und Datenbank sind voruebergehend, sonst nichts.
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return FailureKind.RETRYABLE

    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return (FailureKind.RETRYABLE if code == 429 or 500 <= code < 600
                else FailureKind.TERMINAL)

    if isinstance(exc, psycopg.Error):
        return FailureKind.RETRYABLE

    # Der Abbruch beim Herunterfahren. Er erbt von `BaseException`, nicht von
    # `Exception`; Begruendung im Docstring.
    if isinstance(exc, asyncio.CancelledError):
        return FailureKind.RETRYABLE

    # Alles Unbekannte ist mit hoher Wahrscheinlichkeit unser eigener Fehler.
    # Den dreimal an jedem Dokument des Bestands zu bezahlen waere die teuerste
    # denkbare Voreinstellung — siehe die Begruendung im Docstring.
    return FailureKind.TERMINAL


def should_run_again(exc: BaseException) -> bool:
    """Sagt, ob der Job nach diesem Fehler noch einmal laufen soll.

    Die eine Antwort fuer BEIDE Stellen, an denen sie gebraucht wird: die
    Wiederholungs-Strategie in `tasks.py` entscheidet damit, ob sie den Job
    erneut vorlegt, und der Ausnahme-Pfad in `executor.py` entscheidet damit, ob
    er die Zeile unberuehrt laesst.

    WARUM ES DIESE FUNKTION GIBT, und nicht nur `classify_failure`: Die
    Einstufung allein genuegt nicht — sie hat DREI Werte, und aus dreien muessen
    beide Stellen dieselben zwei Gruppen bilden. Genau daran ist es beim Bauen
    schon einmal auseinandergelaufen: Der Executor behandelte
    `BALANCE_PAUSE` als „werfen, nichts schreiben", die Strategie aber als „nicht
    wiederholen", weil sie nur auf `RETRYABLE` prüfte. Das Ergebnis waere ein
    Dokument gewesen, das wegen eines LEEREN KONTOS endgueltig aufgegeben wird,
    obwohl mit ihm nichts ist — und das dabei ohne Ergebnis liegen bleibt,
    weil der Executor ja nichts geschrieben hat. Die Einstufung war an beiden
    Stellen dieselbe; die Gruppierung war es nicht. Also steht auch sie hier.

    Die Gruppierung: Nur `TERMINAL` heisst aufgeben. `RETRYABLE` und
    `BALANCE_PAUSE` heissen beide „noch einmal" — beim Konto allerdings mit der
    Einschraenkung, dass es dazu im Normalfall gar nicht kommt: Der Executor
    haelt bei leerem Konto selbst an (`wait_for_balance`), damit das Warten
    keinen der drei echten Versuche verbraucht. Erreicht ein 402 trotzdem diese
    Stelle — etwa weil das Konto MITTEN im Lauf leer wurde —, ist „noch einmal"
    die richtige Antwort: Das Dokument ist in Ordnung, nur das Konto nicht.
    """
    return classify_failure(exc) is not FailureKind.TERMINAL
