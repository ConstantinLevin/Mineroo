"""Die Procrastinate-Seite: App, Tasks, Wiederholungs-Strategie, Einreihen.

Dieses Modul wird von BEIDEN Prozessen importiert — vom Daemon, der die Tasks
ausfuehrt, und von jedem Produzenten, der Jobs einreiht. Das ist keine Bequemlichkeit,
sondern Notwendigkeit: Beide Seiten muessen dieselben Task-NAMEN kennen
(Feature-Verifikation Punkt 13). Kennen sie sie nicht, passiert etwas
Unangenehmeres als ein Fehler — es passiert NICHTS. Der Produzent reiht ein, der
Daemon sieht nichts, und beide melden Erfolg. Deshalb stehen die Namen als
Konstanten in `config.py` und werden hier ausdruecklich gesetzt, statt aus dem
Modulpfad zu entstehen.

Die Tasks hier sind duenn. Die Arbeit steht in `executor.py`, das nichts von
Warteschlangen weiss; was hier hinzukommt, ist ausschliesslich die Anbindung.
"""

from __future__ import annotations

from typing import Any

import procrastinate
from procrastinate import RetryDecision, testing

from .config import Settings

app = procrastinate.App(connector=testing.InMemoryConnector())
"""Die App auf Modulebene — die von Procrastinate dokumentierte Form.

Sie entsteht beim Import mit einem speicherinternen Zugang, der NICHTS mit einer
Datenbank zu tun hat. Das ist kein Platzhalter aus Verlegenheit, sondern hat drei
Gruende: Ein Import darf keine Verbindung aufbauen; die Zwei-Prozess-Lage
braucht ohnehin zwei verschiedene Zugaenge (der Produzent einen synchronen, der
nur einreihen darf, der Daemon einen asynchronen, weil nur damit ein Worker
laeuft); und Procrastinates eigenes Testmuster tauscht den Zugang gegen genau
diesen speicherinternen aus, was nur geht, wenn die App nicht schon am Import an
einer echten Datenbank haengt.

`configure` haengt den richtigen Zugang ein. Wer das vergisst, schreibt seine
Jobs in den Arbeitsspeicher und wundert sich, dass nichts passiert — deshalb
prueft `defer_document` es und wirft.
"""


def register_tasks() -> None:
    """Meldet die beiden Tasks an. Wird von `configure` gerufen, nicht von Hand.

    Die Anmeldung steht als Funktion und nicht als Dekorator ueber den
    Task-Funktionen, weil beide Tasks Werte aus `Settings` brauchen, die es beim
    Import noch nicht gibt — die Wiederholungs-Strategie ihre Wartezeiten und ihr
    Versuchsbudget, der Waechter seinen Zeitplan. Ein Dekorator liefe beim Import,
    also vor `Settings.load`.

    Angemeldet wird:

        `ocr_document` unter dem Namen `config.TASK_OCR_DOCUMENT`, in der Queue
        `config.QUEUE_OCR`, mit `retry=OCRRetryStrategy(settings)` und
        `pass_context=True`.

        `retry_stalled` unter dem Namen `config.TASK_RETRY_STALLED`, in der Queue
        `config.QUEUE_MAINTENANCE`, mit `queueing_lock="retry_stalled"`,
        `pass_context=True` und periodisch nach `settings.stalled_check_cron`.

    Der NAME wird ausdruecklich gesetzt und nicht aus dem Modulpfad abgeleitet.
    Der Grund steht in `config.py`: Kennen Produzent und Daemon verschiedene
    Namen, gibt es keinen Fehler und keine Meldung — die Jobs bleiben liegen.

    Zusicherung:
        Mehrfach aufrufbar; die zweite Anmeldung desselben Namens ist ein
        Nichts-Tun. Baut keine Verbindung auf.

    Kosten: keine.
    """
    raise NotImplementedError


def configure(settings: Settings, *, for_worker: bool) -> procrastinate.App:
    """Haengt der App einen Zugang zur Datenbank an und gibt sie zurueck.

    Args:
        settings: Traegt die Verbindungszeichenkette.
        for_worker: True fuer den Daemon — asynchroner Zugang, kann alles.
            False fuer Produzenten — synchroner Zugang, kann NUR einreihen.
            Jeder andere Vorgang laeuft mit einem synchronen Zugang in einen
            Fehler, und das ist gut so: Ein Produzent, der versehentlich einen
            Worker startet, waere ein zweiter Exekutor, von dem niemand weiss.

    Returns:
        Dieselbe App auf Modulebene, jetzt benutzbar.

    Zusicherung:
        Mehrfach aufrufbar; der zuletzt eingehaengte Zugang gilt. Baut KEINE
        Verbindung auf — das geschieht beim ersten Gebrauch. `configure` kann
        deshalb auch dann laufen, wenn Postgres gerade nicht erreichbar ist.

    Kosten: keine.
    """
    raise NotImplementedError


class OCRRetryStrategy(procrastinate.BaseRetryStrategy):
    """Entscheidet, ob ein gescheiterter Job noch einmal vorgelegt wird.

    Diese Klasse entscheidet NICHT selbst, was wiederholbar ist — sie fragt
    `errors.classify_failure`. Das ist die halbe Miete dieses Aufbaus: Dieselbe
    Frage wird auch im Ausnahme-Pfad des Executors gestellt, und beide Antworten
    MUESSEN uebereinstimmen. Waeren es zwei Regeln, entstuende der schlimmste
    Zustand des Systems — ein Job, der noch einmal laeuft, waehrend die Zeile
    schon `failed` sagt.

    Wartezeiten: exponentiell mit `settings.retry_base_seconds` und
    `settings.retry_factor`, also rund dreissig Sekunden, zwei Minuten, acht
    Minuten. Grosszuegig, weil die typischen wiederholbaren Fehler (Warteschlange
    voll, Worker startet kalt) Minuten und nicht Sekunden brauchen.
    """

    def __init__(self, settings: Settings) -> None:
        """Nimmt die Einstellung entgegen, aus der Wartezeiten und Budget kommen.

        Die Strategie braucht `retry_base_seconds`, `retry_factor` und
        `max_attempts`. Sie holt sie sich nicht selbst — es gibt keine globale
        Einstellung, und das ist eine ausdrueckliche Regel (`config.py`). Gebaut
        wird sie in `register_tasks`, das die Einstellung ohnehin hat.
        """
        raise NotImplementedError

    def get_retry_decision(
        self, *, exception: BaseException, job: procrastinate.jobs.Job
    ) -> RetryDecision | None:
        """Sagt, wann der Job erneut vorgelegt wird — oder dass er es nicht wird.

        Args:
            exception: Was den Job hat scheitern lassen.
            job: Der Job, wegen `job.attempts`.

        Returns:
            Eine `RetryDecision` mit dem naechsten Termin, oder None fuer
            „nicht wiederholen".

            Ausdruecklich None und NICHT `RetryDecision(should_retry=False)`:
            Dieser Parameter steht nur in einem Beispiel der Anleitung, waehrend
            die Referenz-Signatur ihn nicht kennt. `None` ist der in beiden
            Quellen uebereinstimmend dokumentierte Weg (Feature-Verifikation
            Punkt 8). Auf einem undokumentierten Parameter aufzubauen, waere ein
            stiller Bruch beim naechsten Upgrade — und einer, der als „wird
            trotzdem wiederholt" auftraete, also als Geld.

        Zusicherung:
            Gibt None zurueck, sobald `classify_failure` etwas anderes als
            `RETRYABLE` sagt ODER `job.attempts` das Budget aus
            `settings.max_attempts` erreicht hat.

            Zum Budget gehoert eine Eigenheit, die man kennen muss, sonst haelt
            man sie fuer einen Fehler: `attempts` zaehlt JEDE Wiedervorlage, nicht
            nur die aus dieser Strategie. Die SQL-Funktion
            `procrastinate_retry_job_v2` erhoeht den Zaehler bei jedem
            Zuruecksetzen auf `todo`, und sowohl der stalled-Waechter als auch ein
            Retry von Hand laufen durch genau diese Funktion (Feature-Verifikation
            Punkt 19, am Objekt der installierten 3.9.0 belegt). Ein Retry von
            Hand auf einen erschoepften Job ist deshalb genau EIN weiterer
            Versuch, keine neue Dreier-Runde — und eine stalled-Wiederaufnahme
            zehrt am selben Budget.

        Kosten: keine.
        """
        raise NotImplementedError


async def ocr_document(context: procrastinate.JobContext, sha256: str) -> None:
    """Der Dokument-Task. Duenner Adapter ueber `executor.process_document`.

    Angemeldet unter `config.TASK_OCR_DOCUMENT` in der Queue `config.QUEUE_OCR`,
    mit `OCRRetryStrategy` und `pass_context=True`.

    Was dieser Adapter selbst tut: Er holt sich den geteilten RunPod-Zugang und
    den Verbindungspool aus dem Kontext des Workers und reicht sie weiter. Mehr
    nicht — jede Zeile Ablauflogik, die hier statt im Executor stuende, waere
    eine Zeile, die sich nur mit laufender Warteschlange pruefen laesst.

    Args:
        context: Von Procrastinet gestellt; traegt die Betriebsmittel des Workers.
        sha256: Das einzige Argument des Jobs. Alles Weitere steht in der Zeile —
            und das ist Absicht: Ein Job-Argument ist eine Kopie, und eine Kopie
            kann von der Zeile abweichen. Ein Hash kann das nicht.

    Idempotenz:
        Ja, ueber den Terminal-Guard im Executor — mit der gewollten Ausnahme
        `failed`.

    Kosten: JA.
    """
    raise NotImplementedError


async def retry_stalled(context: procrastinate.JobContext, timestamp: int) -> None:
    """Legt Jobs erneut vor, deren Worker gestorben ist. Woertlich das Doku-Muster.

    Angemeldet unter `config.TASK_RETRY_STALLED` in der Queue
    `config.QUEUE_MAINTENANCE`, periodisch nach `settings.stalled_check_cron`,
    mit einem eigenen `queueing_lock`, damit sich diese Aufgabe nicht selbst
    ueberholt.

    Der Koerper sind vier Zeilen: `get_stalled_jobs()` holen, ueber die Liste
    laufen, `retry_job()` rufen. Nichts Eigenes. Das ist Procrastinates eigene,
    dokumentierte Antwort auf „Daemon hart gestorben, was nun?" (Feature-
    Verifikation Punkt 6), und Eigenbau an dieser Stelle waere die schlechtere
    Fassung eines geloesten Problems.

    Was daraus fuer das Dokument folgt: Es wird von vorn verarbeitet. Der
    haengengebliebene Job wird auf `todo` zurueckgesetzt, sein Versuchszaehler
    steigt, und beim naechsten Anlauf beginnt der Executor bei Schritt 0 — die
    Zeile stand ja noch auf `new`, also greift der Guard nicht.

    Einen ZEILEN-Abgleich gibt es hier ausdruecklich nicht (Plan 2.4, ein Buch):
    Weil die Zeile zwischen Anlage und Endzustand nie angefasst wird, kann sie
    mit nichts auseinanderlaufen. Es gibt nichts abzugleichen.

    Args:
        context: Von Procrastinate gestellt.
        timestamp: Von Procrastinate gestellter Zeitstempel des periodischen
            Laufs. Wird nicht benutzt, gehoert aber zur Signatur periodischer
            Tasks.

    Zusicherung:
        Ein haengengebliebener Job haelt bis zu diesem Zuruecksetzen seinen
        `lock` — er ist noch nicht „beendet" (Feature-Verifikation Punkt 18).
        Ein nachgereihtes Duplikat desselben Dokuments wartet also genau so
        lange, wie das Dokument ohnehin nicht doppelt laufen darf. Das ist
        gewollt und keine Blockade.

    Kosten: keine unmittelbar — mittelbar loest jede Wiedervorlage einen neuen
    Verarbeitungslauf aus, und der kostet.
    """
    raise NotImplementedError


def defer_document(sha256: str) -> bool:
    """Reiht ein Dokument ein. EINZELN, nie im Stapel.

    Setzt beim Einreihen ZWEI Sperren, und beide werden gebraucht:

    `queueing_lock = sha256` verhindert ein zweites WARTENDES Duplikat. Es ist
    aber schon frei, sobald der Job laeuft — „queueing_lock allows a single job in
    todo status. Meanwhile, it allows multiple jobs to be in doing status"
    (Feature-Verifikation Punkt 5). Ein waehrend der Verarbeitung nachgeschobenes
    Duplikat wuerde also angenommen und liefe parallel: doppelte Bezahlung
    desselben Dokuments.

    `lock = sha256` schliesst diese Luecke. Es serialisiert Jobs gleichen Locks —
    das Duplikat startet erst NACH dem laufenden Job. Endet der laufende
    erfolgreich, endet das Duplikat sofort am Terminal-Guard, ohne etwas zu
    kosten. Endet er `failed`, verarbeitet das Duplikat das Dokument neu, und
    auch das ist gewollt: Es ist genau der eine weitere Versuch. Der Lock wird
    bei JEDEM Job-Ende frei, auch bei einem gescheiterten — „the second task
    cannot run before the first one has ended (successfully or not)"
    (Feature-Verifikation Punkt 18) —, eine Kette kann also nie dauerhaft
    verklemmen.

    Warum ausdruecklich EINZELN und nie ueber das Stapel-Einreihen: Beides
    zusammen ist untersagt, und die Folge waere schlimm. Enthaelt ein Stapel auch
    nur einen Konflikt, wird der GESAMTE Stapel zurueckgerollt — dreissigtausend
    Dokumente scheitern an einem einzigen bereits wartenden (Feature-Verifikation
    Punkt 9). Einzelne Einreihungen sind billig; dieser Fehler waere es nicht.

    Args:
        sha256: Welches Dokument.

    Returns:
        True, wenn ein Job entstanden ist. False, wenn schon einer wartet —
        das ist der NORMALFALL beim erneuten Durchlaufen eines Ordners und heisst
        schlicht „laeuft schon" (die Ausnahme `AlreadyEnqueued` wird gefangen und
        in dieses False uebersetzt).

    Zusicherung:
        Reiht ein und wartet nicht. Wer warten will, nimmt `wait`. Das trennt
        Jurgens „losschicken ist einfach einen job erstellen" von „das warten auf
        eine antwort […] eine seperate funktion" (Plan 2.3).

    Kosten: keine unmittelbar — mittelbar loest der Job Verarbeitung aus.
    """
    raise NotImplementedError


def worker_context_resources(settings: Settings) -> dict[str, Any]:
    """Baut die Betriebsmittel, die alle Tasks eines Workers teilen.

    Ein RunPod-Zugang und ein Verbindungspool fuer den ganzen Prozess, nicht je
    Job. Vierundzwanzig eigene HTTP-Clients waeren vierundzwanzig eigene
    Verbindungspools, die einander nichts nuetzen; ein geteilter Client haelt
    seine Verbindungen offen, und genau davon lebt eine Abfrageschleife, die alle
    zehn Sekunden dieselbe Gegenstelle fragt.

    Args:
        settings: Wird an beide Betriebsmittel durchgereicht.

    Returns:
        Die Abbildung, die als `additional_context` an `run_worker_async`
        uebergeben wird. Die Tasks lesen sie als
        `context.additional_context["runpod"]` bzw. `["pool"]`.

        Der Name steht hier ausdruecklich, weil er die ganze Frage beantwortet:
        Ohne ihn baut ein Implementierer wahrscheinlich eine Modul-Variable oder
        eine Kontextvariable — und beides umgeht die Besitzregel unten, weil dann
        niemand mehr sagen kann, wer schliesst.

    Der Verbindungspool:
        Er wird per `App.open_async(pool=…)` an Procrastinate DURCHGEREICHT,
        statt dass beide je einen eigenen aufmachen. Sonst laufen im
        Daemon-Prozess zwei unabhaengige Obergrenzen fuer Verbindungen zu
        derselben Datenbank, von denen keine in `Settings` steht — das bemerkt
        man erst, wenn Postgres „too many connections" sagt, und dann mitten im
        Bestandslauf. Procrastinates LISTEN-Verbindung bleibt davon unberuehrt,
        sie zaehlt ohnehin nicht in den Pool.

        Geoeffnet wird er mit `await pool.open()` bzw. als Kontext-Manager, NICHT
        im Konstruktor: Das ist in psycopg_pool 3.3.0 abgekuendigt und schreibt
        eine Warnung in genau die Konsole, in der der erste Bestandslauf
        beobachtet werden soll.

    Besitz und Lebensdauer: Der Daemon besitzt beides und schliesst beides beim
    Herunterfahren, NACH den Workern. Die Tasks benutzen nur.

    Kosten: keine.
    """
    raise NotImplementedError
