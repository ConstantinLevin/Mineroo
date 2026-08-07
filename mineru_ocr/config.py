"""Alle Vorgaben an EINER Stelle — samt der Herleitung, aus der jede Zahl stammt.

Warum das ein eigenes Modul ist und nicht ueber die Module verstreut: Eine Zahl,
die an zwei Stellen steht, wird beim naechsten Mal an einer Stelle geaendert. Und
eine Zahl ohne ihre Herleitung ist beim naechsten Lesen nicht mehr von einem
Griff ins Blaue zu unterscheiden — dann traut sich niemand, sie anzufassen, und
niemand kann sagen, ob sie noch stimmt. Deshalb steht an jedem Feld, WORAUS es
folgt und WAS es kippen wuerde.

Die Schwellenwerte hier sind zum groesseren Teil hergeleitet und zum kleineren
Teil gesetzt. Wo etwas gesetzt und nicht gemessen ist, steht es dabei — Plan 12
fuehrt diese Punkte als offene Nachmessungen.
"""

from __future__ import annotations

import dataclasses
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from .errors import ConfigurationError

DEFAULT_DB_DSN: Final = "host=localhost port=5437 dbname=mineru_ocr user=jurgen"
"""Die laufende Postgres-App-Instanz, eigene Datenbank (Plan 9)."""

DEFAULT_ENDPOINT_ID: Final = "l79lebhinxaqjz"
"""Der zweifach in Produktion bewaehrte queue-basierte Endpoint."""

DEFAULT_SPOOL_DIR: Final = Path.home() / ".mineru_ocr" / "spool"
"""Ablage fuer Byte-Eingaben ohne eigenen Pfad."""

RUNPOD_CONFIG_PATH: Final = Path.home() / ".runpod" / "config.toml"
"""Der im Projekt bewaehrte Ort des API-Schluessels, Feld `apikey`. Wird
gelesen, nie geschrieben."""

# --------------------------------------------------------------------------
# Namen, die zwischen Prozessen verabredet sind
# --------------------------------------------------------------------------
# Diese Zeichenketten sind Vertrag zwischen Prozessen, die einander nie sehen:
# der Produzent reiht unter einem Task-Namen ein, der Daemon holt unter demselben
# ab. Stimmen sie nicht ueberein, passiert NICHTS — kein Fehler, keine Meldung,
# die Jobs bleiben schlicht liegen. Deshalb stehen sie hier und werden von
# beiden Seiten importiert, statt an zwei Stellen getippt zu werden
# (Feature-Verifikation Punkt 13: Producer- und Daemon-App muessen dieselben
# Task-Namen kennen).

TASK_OCR_DOCUMENT = "mineru_ocr.ocr_document"
"""Voller Name des Dokument-Tasks. Wird explizit gesetzt statt aus dem Modulpfad
abgeleitet, damit ein spaeteres Verschieben der Datei die laufende Warteschlange
nicht entwertet."""

TASK_RETRY_STALLED = "mineru_ocr.retry_stalled"
"""Voller Name des Waechter-Tasks (Plan 5.2)."""

QUEUE_OCR = "ocr"
"""Queue der Dokument-Jobs."""

QUEUE_MAINTENANCE = "maintenance"
"""Queue des Waechters. Eigene Queue, weil periodische Tasks sonst um dieselben
Concurrency-Slots konkurrieren wie die OCR-Jobs und bei vollem Pool verhungern —
die Procrastinate-Doku empfiehlt fuer genau dieses Problem die Queue-Trennung
(Feature-Verifikation Punkt 2)."""

NOTIFY_CHANNEL = "doc_done"
"""Kanal, auf dem der Task jedes Terminal-Schreiben meldet; Nutzlast ist der
sha256 des Dokuments. EIGENBAU: Procrastinate hat kein eingebautes
„Job fertig"-Signal an externe Warter (Feature-Verifikation Punkt 10)."""


# --------------------------------------------------------------------------
# Die Einstellungen
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Settings:
    """Vollstaendige Betriebseinstellung. Unveraenderlich, damit sie waehrend eines
    Laufs nicht unter der Hand kippt.

    Wird einmal beim Start gebildet (`Settings.load`) und dann durchgereicht.
    Es gibt bewusst KEINE globale Instanz und keinen Zugriff auf Umgebungsvariablen
    ausserhalb von `load` — sonst haengt das Verhalten eines Tests davon ab, was
    zufaellig in der Shell steht.
    """

    # -- Verbindungen ------------------------------------------------------

    db_dsn: str
    """Verbindung zur eigenen Datenbank. Voreinstellung siehe `load`: die
    laufende Postgres-App-Instanz auf Port 5437, Datenbank `mineru_ocr`. Eigene
    Datenbank statt eigenem Schema in einer fremden, damit Sicherung, Loeschen
    und Migrationen sich nie mit den gbrain-Daten beruehren (Plan 9)."""

    endpoint_id: str
    """Der RunPod-Serverless-Endpoint. Voreinstellung `l79lebhinxaqjz` — der
    zweifach in Produktion bewiesene queue-basierte Endpoint."""

    api_key: str
    """RunPod-API-Schluessel. Gehoert NIE in eine URL und NIE in eine Ausgabe;
    er reist ausschliesslich im Authorization-Header als Bearer-Token
    (Plan 8, Regel 12). `__repr__` dieser Klasse maskiert ihn (siehe unten)."""

    spool_dir: Path
    """Ablage fuer Eingaben, die als reine Bytes hereinkamen und keinen Pfad
    haben. Voreinstellung `~/.mineru_ocr/spool/`. Wird NIE automatisch geleert:
    `documents.source_path` zeigt dorthin, und ein Aufraeumen wuerde die
    Nachvollziehbarkeit eines fertigen Dokuments zerstoeren."""

    # -- Job-Contract ------------------------------------------------------

    backend: str = "vlm-auto-engine"
    """Das gemessene Backend. `hybrid-auto-engine` liess bei der Messung 13 von
    41 Formeln fallen und verlor stillschweigend Indizes, bei 2,2-facher Laufzeit.
    Der Wert ist ein Parameter und kein festverdrahteter Wert, weil die
    Vergleichsmessung fuer andere Materialarten aussteht — aber die Voreinstellung
    ist das Gemessene, nicht das Bequeme (Plan 11)."""

    transport: str = "tarball_b64"
    """Fest. Bei diesem Transport ist ein `formats`-Feld wirkungslos: es kommt
    immer alles zurueck. Damit ist die Abruf-Regel („alles holen, alles
    persistieren, nie filtern") Mechanik statt Disziplin — an Disziplin ist sie
    schon einmal gescheitert. Zusaetzlich traegt der Tarball die Layout- und
    Span-PDFs, die der Inline-Transport gar nicht liefert (Plan 6)."""

    # -- Zerlegung grosser Dokumente --------------------------------------

    max_payload_bytes: int = 7_000_000
    """Obergrenze fuer die ROHE Groesse eines Chunks vor der Base64-Kodierung.
    Hergeleitet: Das Gateway nimmt auf `/run` hoechstens 10 MB an; Base64 blaeht
    um Faktor 1,33; der Rest ist Puffer fuer den Umschlag ringsum. 7,0 MB roh
    ergeben rund 9,3 MB kodiert."""

    max_pages_per_chunk: int = 20
    """Obergrenze fuer die Seitenzahl eines Chunks. Hergeleitet aus dem
    Antwort-Cap von rund 20 MB: gemessene Einzelseiten-Antworten liegen inline bei
    23 bis 152 KB, der Tarball traegt zusaetzlich Layout- und Span-PDFs;
    20 Seiten mal konservativ 0,75 MB sind rund 15 MB und damit unter dem Cap.
    KONSERVATIV GESCHAETZT, nicht am Tarball gemessen — die Nachmessung steht
    als offener Punkt (Plan 12.2), und beim ersten echten Riss wird gemessen und
    nicht geraten erhoeht."""

    # -- Zeitverhalten -----------------------------------------------------

    http_connect_timeout: float = 15.0
    """Wie lange auf das Zustandekommen einer Verbindung gewartet wird."""

    http_read_timeout: float = 60.0
    """Wie lange auf eine Antwort gewartet wird, nachdem die Anfrage draussen ist.
    Grosszuegig, weil `/status` gelegentlich traege antwortet und ein Abbruch hier
    einen laufenden, bezahlten Job zu einem Fehlversuch machen wuerde."""

    http_write_timeout: float = 300.0
    """Wie lange das HOCHLADEN dauern darf. Der wichtigste der vier Werte, und der
    Grund, warum es sie ueberhaupt einzeln gibt: httpx setzt ohne Zutun EINE
    Schranke von fuenf Sekunden fuer Verbindungsaufbau, Lesen, Schreiben und Pool
    zusammen (`httpx/_config.py`, `DEFAULT_TIMEOUT_CONFIG = Timeout(timeout=5.0)`).
    Ein Teilstueck geht mit bis zu 9,3 MB Base64 hinaus — gegen fuenf Sekunden ist
    das bei maessigem Upstream nicht zu schaffen. Und der Fehlschlag waere teuer:
    Ein Schreib-Abbruch gilt als wiederholbar, der Chunk geht noch einmal raus,
    waehrend der erste Job auf der Gegenseite moeglicherweise schon angenommen und
    abgerechnet ist — und dessen Kennung kennt dann niemand. Fuenf Minuten sind
    grosszuegig gesetzt: Hochladen kostet nichts, ein doppelt bezahlter Parse
    schon."""

    http_pool_timeout: float = 30.0
    """Wie lange auf einen freien Platz im Verbindungspool gewartet wird."""

    http_max_keepalive: int = 32
    """Wie viele Verbindungen offen gehalten werden. Muss mindestens so gross sein
    wie `ocr_concurrency`, sonst baut ein Teil der gleichzeitig betreuten
    Dokumente bei JEDER Abfrage die TLS-Verbindung neu auf — httpx haelt ohne
    Zutun nur zwanzig offen (`httpx/_config.py`, `DEFAULT_LIMITS`), bei
    vierundzwanzig Dokumenten also zu wenige. Der Abstand nach oben ist Puffer
    fuer die GraphQL-Aufrufe."""

    poll_interval_seconds: float = 10.0
    """Abstand zwischen zwei `/status`-Abfragen eines laufenden Jobs.

    Der Sicherheitsabstand, um den es geht: Das Abholfenster der Gegenseite ist
    rund dreissig Minuten, also 1800 Sekunden. Bei zehn Sekunden Takt liegt die
    Verzoegerung zwischen „fertig" und „abgeholt" im Mittel bei fuenf Sekunden —
    das Hundertachtzigfache an Reserve. Enger zu takten kauft davon nichts
    Messbares und macht das Log unlesbar: Vierundzwanzig gleichzeitig betreute
    Dokumente ergeben bei zehn Sekunden 2,4 kleine Anfragen pro Sekunde, bei
    einer Sekunde waeren es vierundzwanzig.

    Es gibt auf dieser Schnittstelle nichts Besseres als Abfragen, und das ist
    geprueft, nicht angenommen: `/runsync` kann zwar bis zu fuenf Minuten warten,
    verkuerzt aber die Aufbewahrung von dreissig Minuten auf EINE und deckt den
    Kaltstart von achteinhalb Minuten ohnehin nicht. `/stream` ist keine offen
    gehaltene Verbindung — der Hersteller-SDK setzt es selbst als Schleife mit
    einer Sekunde Pause um, der Server antwortet also sofort. Ein Webhook waere
    echtes Push, braucht aber eine von aussen erreichbare Adresse. Bleibt
    `/status`, und dessen Antwort traegt im Moment des Fertigwerdens gleich das
    ganze Ergebnis — Abfrage und Abholung sind derselbe Aufruf."""

    job_timeout_seconds: float = 1_500.0
    """Obergrenze fuer die Betreuung EINES Chunk-Jobs. 25 Minuten: deckt den
    Kaltstart von rund 8,5 Minuten plus Warteschlange plus Parse und bleibt unter
    der 30-Minuten-Aufbewahrung. Wird sie gerissen, ist das ein wiederholbarer
    Fehler, kein endgueltiger."""

    ocr_concurrency: int = 24
    """Wie viele Dokumente der Daemon gleichzeitig betreut. GESETZT, nicht
    gemessen — nachzumessen am ersten echten Stapel (Plan 12.1).

    Die Zahl hat genau EINE sinnvolle Bezugsgroesse, und das ist wichtiger als
    ihr Wert: die tatsaechliche Parallelitaet auf der Gegenseite, naemlich
    `workersMax` mal `MINERU_MAX_CONCURRENCY`. Bei uns steht
    `MINERU_MAX_CONCURRENCY` auf 1 und die Worker-Quote bei 10 — es werden also
    zu keinem Zeitpunkt mehr als ZEHN Teilstuecke wirklich geparst. Mehr
    Dokumente gleichzeitig zu betreuen macht keinen einzigen Job schneller, es
    verlaengert nur die Warteschlange auf der Gegenseite.

    Warum trotzdem mehr als zehn: Ein Dokument traegt nur dann zur Warteschlange
    bei, wenn gerade ein Teilstueck von ihm draussen ist. Zwischen „Teilstueck
    fertig" und „naechstes abgeschickt" liegt bis zu ein Abfragetakt, und in
    dieser Luecke steuert es nichts bei. Bei rund dreissig Sekunden Parse-Zeit
    und zehn Sekunden Luecke ist etwa ein Viertel der Zeit tot; man braucht also
    gut dreizehn, um zehn Worker satt zu halten. Vierundzwanzig ist der Puffer
    darueber.

    DARAUS FOLGT DIE PFLEGEREGEL: Steigt die Worker-Quote, muss diese Zahl
    mitwachsen — sonst laufen die neuen Worker leer. Sinkt sie, ist ein zu hoher
    Wert nicht falsch, sondern nur wirkungslos (die Ueberzaehligen warten in der
    Warteschlange der Gegenseite). Wer diesen Wert anfasst, ohne die Quote zu
    kennen, raet."""

    maintenance_concurrency: int = 1
    """Der Waechter braucht genau einen Slot; mehr waere sinnlos, weil sein
    `queueing_lock` ohnehin nur einen wartenden Job zulaesst."""

    stalled_check_cron: str = "*/10 * * * *"
    """Wie oft der Waechter nach haengengebliebenen Jobs sieht. Der Wert stammt
    woertlich aus dem Doku-Muster von Procrastinate (Feature-Verifikation
    Punkt 6) — kein eigener Einfall."""

    shutdown_graceful_timeout: float = 30.0
    """Wie lange beim Beenden auf laufende Dokumente gewartet wird, bevor sie
    abgebrochen werden. Ein abgebrochener Job endet als `aborted`, und dafuer
    gibt es KEIN Retry-Bordmittel (Feature-Verifikation Punkt 19); sein
    Heilungsweg ist das erneute Einreihen. Deshalb ist der Wert eher grosszuegig:
    dreissig Sekunden reichen, damit ein laufender Chunk seine Antwort noch
    abholt."""

    # -- Geld --------------------------------------------------------------

    min_balance_start_usd: float = 1.00
    """Untergrenze im Preflight: unterhalb dessen beginnt eine Arbeit gar nicht
    erst. Ein Dollar ist Puffer, nicht Bedarf — Arbeit mit leerem Konto zu
    beginnen heisst, mitten im Bestand liegenzubleiben."""

    min_balance_pause_usd: float = 0.50
    """Untergrenze im Lauf: darunter geht der Task in die Warteschleife statt zu
    scheitern. Frueher als der historische Abbruch der Ad-hoc-Skripte bei
    fuenfzehn Cent, aus zwei Gruenden: Pausieren kostet nichts, und der Rest muss
    ausreichen, die bereits laufenden Chunks noch zu Ende zu bezahlen."""

    wait_poll_seconds: float = 5.0
    """Abstand der Rueckfallabfrage in `wait()`. Das Wecksignal ist der Normalweg;
    dieser Wert deckt den Fall, dass es nicht ankommt (abgerissene Verbindung,
    Schreibvorgang von aussen). Steht hier und nicht als Zahl in zwei Docstrings,
    weil eine Zahl an zwei Stellen beim naechsten Mal an einer geaendert wird."""

    balance_recheck_seconds: float = 900.0
    """Abstand der Kontostands-Pruefungen in der Warteschleife. Eine Viertelstunde
    ist keine Messung, sondern die Abwaegung zwischen „merkt das Aufladen schnell"
    und „belaestigt die API nicht ohne Not"."""

    # -- Betrieb -----------------------------------------------------------

    strict: bool = True
    """Ob eine Abweichung der Endpoint-Konfiguration den Preflight abbricht
    (`ConfigDriftError`) oder nur gemeldet wird."""

    delete_jobs: str = "never"
    """Job-Zeilen bleiben in der Datenbank stehen. Das ist zugleich Procrastinates
    Voreinstellung und Jurgens Festlegung („tidiness = full legibility"): Der
    abgeleitete Dokument-Status LIEST aus den Job-Zeilen; wer sie loescht, loescht
    die Beobachtbarkeit mit (Plan 5.3)."""

    max_attempts: int = 3
    """Wie oft ein Dokument bei wiederholbaren Fehlern insgesamt versucht wird.
    Achtung auf die Reichweite: Dieses Budget wird nicht nur von Strategie-Retries
    verbraucht, sondern von JEDER Wiedervorlage — auch von der stalled-Wiederaufnahme
    des Waechters und von einem Retry von Hand (Feature-Verifikation Punkt 19)."""

    retry_base_seconds: float = 30.0
    """Grundwartezeit des exponentiellen Rueckzugs."""

    retry_factor: float = 4.0
    """Faktor des Rueckzugs: rund 30 Sekunden, 2 Minuten, 8 Minuten."""

    def __repr__(self) -> str:
        """Wie die Voreinstellung, aber mit maskiertem API-Schluessel.

        Diese Methode ist kein Komfort, sondern eine Sicherheitszusage: Eine
        Einstellung landet frueher oder spaeter in einem Log, einer Fehlermeldung
        oder einer Debugger-Ausgabe. `api_key` erscheint dort NIE im Klartext —
        stattdessen die Zeichenkette `***`. Wer den Schluessel wirklich braucht,
        liest ihn aus dem Feld, und das ist dann eine bewusste Handlung.

        WIE WEIT DIE ZUSAGE REICHT, damit sich niemand zu viel darauf verlaesst:
        Sie deckt `repr()` und darueber auch `str()`. Sie deckt NICHT
        `dataclasses.asdict(settings)` und nichts, was daraus JSON macht — dieser
        Weg geht an der Methode vorbei und schreibt den Schluessel im Klartext.
        Wer die Einstellung serialisiert, muss das Feld selbst entfernen. Es gibt
        im Paket keine Stelle, die das tut, und es soll auch keine geben.

        (Dass eine Dataclass diese Methode nicht ueberschreibt, ist geprueft:
        `dataclasses` setzt `__repr__` nur, wenn im Klassenkoerper keines steht.)
        """
        raise NotImplementedError

    @classmethod
    def load(
        cls,
        *,
        db_dsn: str | None = None,
        endpoint_id: str | None = None,
        api_key: str | None = None,
        spool_dir: Path | None = None,
        strict: bool = True,
        backend: str | None = None,
        min_balance_start_usd: float | None = None,
        ocr_concurrency: int | None = None,
    ) -> Settings:
        """Bildet die Einstellung aus ausdruecklichen Angaben und Voreinstellungen.

        Zwei Quellen, mehr nicht: Was hier uebergeben wurde, schlaegt die
        Voreinstellung. Keine Umgebungsvariablen — bei einem Werkzeug fuer eine
        Person auf einer Maschine waeren sie eine dritte Stelle, an der ein Wert
        stehen kann, und die Fehlersuche „warum nimmt er den falschen Endpoint?"
        beginnt dann in der Shell statt im Code.

        Der API-Schluessel hat eine eigene Voreinstellung: `~/.runpod/config.toml`,
        Feld `apikey` — der im Projekt bewaehrte Ort, an dem er ohnehin schon
        liegt. Er wird gelesen, nie geschrieben.

        Args:
            db_dsn: Verbindungszeichenkette. Voreinstellung
                `host=localhost port=5437 dbname=mineru_ocr user=jurgen`.
            endpoint_id: Voreinstellung `l79lebhinxaqjz`.
            api_key: Voreinstellung siehe oben.
            spool_dir: Voreinstellung `~/.mineru_ocr/spool/`.
            strict: Ob Konfigurations-Abweichungen im Preflight abbrechen.
            backend: Voreinstellung `vlm-auto-engine`. Steht hier, weil
                `OCRClient` es als Konstruktor-Parameter anbietet — ein
                oeffentlicher Parameter, der keinen Weg in die Einstellung hat,
                ist ein stiller Nichts-Tuer: Wer `backend="pipeline"` schreibt,
                bekaeme keinen Fehler und trotzdem das alte Backend.
            min_balance_start_usd: Untergrenze im Preflight, aus demselben Grund.
            ocr_concurrency: Gleichzeitig betreute Dokumente, aus demselben Grund
                (`mineru-ocr daemon --concurrency`).

        Returns:
            Die vollstaendige, unveraenderliche Einstellung.

        Zusicherung:
            Wirft, statt mit einer halben Einstellung weiterzulaufen. Legt NICHTS
            an — insbesondere wird `spool_dir` hier weder erzeugt noch geprueft
            (das tut, wer ihn zuerst braucht); `load` ist frei von Seiteneffekten
            ausser dem Lesen der Schluesseldatei.

        Raises:
            ConfigurationError: Kein API-Schluessel auffindbar, oder
                `~/.runpod/config.toml` existiert und ist unlesbar bzw. hat kein
                Feld `apikey`.

        Kosten: keine. Spricht weder mit RunPod noch mit der Datenbank —
        insbesondere prueft `load` NICHT, ob die Datenbank erreichbar oder der
        Schluessel gueltig ist. Das ist Sache des Preflights, und die Trennung ist
        Absicht: Eine Einstellung zu bilden muss auch offline gehen.
        """
        raise NotImplementedError
