"""Die Procrastinate-Seite: Apps, Tasks, Wiederholungs-Strategie, Einreihen.

Jeder Prozess baut seine App EINMAL und behaelt sie; keine App liegt auf
Modulebene, und nirgends wird ein Zugang zur Laufzeit umgehaengt. Wer welche App
haelt:

    Der Daemon haelt genau eine — `build_daemon_app`: asynchroner Zugang, die
    angemeldeten Tasks samt Wiederholungs-Strategie, ein eigener Pool.

    Der `OCRClient` haelt eine eigene — `build_client_app`: synchroner Zugang, KEINE
    angemeldeten Tasks. Er reiht per NAMEN ein (`enqueue_document`) und fragt
    die Job-Verwaltung ab.

Beide Seiten muessen dieselben Task-NAMEN und dieselbe Queue kennen
(Feature-Verifikation Punkt 13). Kennen sie sie nicht, passiert etwas
Unangenehmeres als ein Fehler — es passiert NICHTS. Der Client reiht ein, der
Daemon sieht nichts, und beide melden Erfolg. Deshalb stehen Namen und Queues als
Konstanten in `config.py`; der Daemon meldet seine Tasks unter ihnen an, der
Client reiht unter ihnen ein, und beide lesen dieselbe Konstante.

Eine zweite App im selben Prozess ist erlaubt (die Tests tun es), kostet aber
etwas: Procrastinates eingebauter Task `remove_old_jobs` wird bei jeder weiteren
App umbenannt. Der Daemon-Prozess baut deshalb keine weitere; dieses Paket
benutzt `remove_old_jobs` ohnehin nicht (erfolgreiche Jobs loescht der Worker,
`delete_jobs="successful"`).

Die Tasks hier sind duenn. Die Arbeit steht in `executor.py`, das nichts von
Warteschlangen weiss; was hier hinzukommt, ist ausschliesslich die Anbindung.
"""

from __future__ import annotations

import math
from typing import Any, Final

import procrastinate
from procrastinate import RetryDecision
from procrastinate.jobs import DeleteJobCondition, Status
from psycopg_pool import AsyncConnectionPool

from . import config, executor
from .config import Settings
from .errors import TransientRunPodError, should_run_again
from .runpod import RunPodClient


def build_daemon_app(settings: Settings) -> procrastinate.App:
    """Baut die App des Daemons: asynchroner Zugang, Tasks angemeldet.

    Wird einmal je Daemon-Prozess gerufen. Die App ist danach NICHT geoeffnet —
    das tut der Daemon mit `open_async(pool=…)` und seinem eigenen Pool; der Bau
    selbst baut keine Verbindung auf, kann also auch laufen, wenn Postgres gerade
    nicht erreichbar ist.

    Die Anmeldung steht hier und nicht als Dekorator ueber den Task-Funktionen,
    weil beide Tasks Werte aus `Settings` brauchen, die es beim Import noch nicht
    gibt — die Wiederholungs-Strategie ihre Wartezeiten und ihr Versuchsbudget,
    der Waechter seinen Zeitplan. Ein Dekorator liefe beim Import, also vor
    `Settings.load`.

    Angemeldet wird:

        `ocr_document` unter dem Namen `config.TASK_OCR_DOCUMENT`, in der Queue
        `config.QUEUE_OCR`, mit `retry=OCRRetryStrategy(settings)` und
        `pass_context=True`.

        `retry_stalled` unter dem Namen `config.TASK_RETRY_STALLED`, in der Queue
        `config.QUEUE_MAINTENANCE`, mit `queueing_lock="retry_stalled"`,
        `pass_context=True` und periodisch nach `settings.stalled_check_cron`.

    Der NAME wird ausdruecklich gesetzt und nicht aus dem Modulpfad abgeleitet.
    Der Grund steht in `config.py`: Kennen Client und Daemon verschiedene Namen,
    gibt es keinen Fehler und keine Meldung — die Jobs bleiben liegen.

    Zusicherung:
        Jeder Aufruf liefert eine eigene, unabhaengige App mit den Werten der
        uebergebenen Einstellung. Es gibt keinen Zustand ausserhalb der App; die
        fruehere Fassung teilte sich eine App auf Modulebene und musste Strategie
        und Zeitplan beim zweiten Aufruf nachziehen — daran ist es einmal still
        gescheitert (der Job wartete dreissig Sekunden statt der eingestellten
        fuenfzig Millisekunden).

    Kosten: keine.
    """
    app = procrastinate.App(
        connector=procrastinate.PsycopgConnector(conninfo=settings.db_dsn)
    )

    # In Dekorator-Form aufgerufen und nicht mit der Funktion als erstem
    # Argument: `app.task` nimmt beides, aber das erste Argument NUR allein —
    # `task(func)` ohne weitere Angaben, oder `task(name=…, …)(func)`. Beides
    # zugleich gibt es nicht, und die Typpruefung sagt das auch.
    #
    # Eine eigene Strategie ist die dokumentierte Form („class
    # procrastinate.BaseRetryStrategy — If you want to implement your own retry
    # strategy, you can inherit from this class"). Bis 3.9.0 kannte die
    # Typangabe `RetryValue` die Basisklasse nicht, und hier stand eine
    # Unterdrueckung; 3.10.0 hat sie behoben, die Unterdrueckung ist mit dem
    # Upgrade entfallen.
    app.task(
        name=config.TASK_OCR_DOCUMENT,
        queue=config.QUEUE_OCR,
        retry=OCRRetryStrategy(settings),
        pass_context=True,
    )(ocr_document)

    waechter = app.task(
        name=config.TASK_RETRY_STALLED,
        queue=config.QUEUE_MAINTENANCE,
        queueing_lock="retry_stalled",
        pass_context=True,
    )(retry_stalled)
    # Der periodische Zeitplan haengt UEBER dem Task, nicht statt seiner:
    # `@app.periodic` wird in der Doku ueber `@app.task` gestapelt, und ohne den
    # Task darunter gaebe es nichts zu wiederholen.
    app.periodic(cron=settings.stalled_check_cron)(waechter)
    return app


def build_client_app(settings: Settings) -> procrastinate.App:
    """Baut die App des `OCRClient`: synchroner Zugang, keine angemeldeten Tasks.

    Der Client reiht per NAMEN ein (`enqueue_document`) und fragt die
    Job-Verwaltung ab; die Task-Funktionen kennt er nicht und braucht sie nicht.
    Ein synchroner Zugang kann einreihen, abfragen und Jobs wiederholen
    (`retry_job_by_id`); nur einen Worker kann er nicht tragen. Ein Client, der
    versehentlich einen Worker startet, waere ein zweiter Exekutor, von dem
    niemand weiss; mit ihm scheitert das.

    Die App kommt UNGEOEFFNET zurueck. Der Client oeffnet sie mit einem Pool, den
    er selbst anlegt und schliesst (`app.open(pool)`): Procrastinates
    `app.close()` schliesst einen uebergebenen Pool nicht, und ein Pool mit
    stehenden Verbindungen waere genau die Last, die der Client sparen soll — je
    Agent eine Verbindung statt fuenf.

    Die App entsteht hier, in einem Modul des Pakets. Eine App im Hauptmodul
    eines Skripts bekaeme einen Modulpfad, der nicht der importierbare ist.

    Kosten: keine.
    """
    return procrastinate.App(
        connector=procrastinate.SyncPsycopgConnector(conninfo=settings.db_dsn)
    )


def build_async_client_app(settings: Settings) -> procrastinate.App:
    """Baut die App des `AsyncOCRClient`: asynchroner Zugang, keine Tasks.

    Dasselbe wie `build_client_app`, nur mit `PsycopgConnector`: Procrastinate
    paart den asynchronen Konnektor mit `psycopg.AsyncConnection`, und mit ihr
    reiht der Async-Client ein (`enqueue_document_async`). Fuer sync und async im
    selben Prozess haelt Procrastinates eigener Abnahmetest genau zwei solche Apps;
    ein Prozess mit beiden Clients hat sie ebenso.

    Die App kommt UNGEOEFFNET zurueck. Der Client oeffnet sie mit einem Pool, den
    er selbst anlegt und in derselben Ereignisschleife wieder schliesst
    (`app.open_async(pool)`).

    Kosten: keine.
    """
    return procrastinate.App(
        connector=procrastinate.PsycopgConnector(conninfo=settings.db_dsn)
    )


def wird_erneut_vorgelegt(
    exception: BaseException, job: procrastinate.jobs.Job, settings: Settings
) -> bool:
    """Die EINE Antwort auf „legt die Warteschlange diesen Job noch einmal vor?"

    Sie wird an ZWEI Stellen gebraucht, und beide muessen sie gleich beantworten:

        `OCRRetryStrategy.get_retry_decision` — lege ich ihn wieder vor?
        `ocr_document` — ist dies der letzte Versuch, muss der Executor also
            schreiben statt zu werfen?

    Warum das eine gemeinsame Funktion ist und nicht zweimal dieselbe Bedingung:
    Genau dieselbe Konstruktion gibt es schon eine Ebene tiefer bei
    `errors.should_run_again`, und sie steht dort, weil die Frage „wiederholbar?"
    einmal an zwei Stellen unterschiedlich beantwortet wurde. Der Fehler ist
    hier derselbe, nur teurer: Faende `ocr_document` „letzter Versuch" und die
    Strategie legte trotzdem vor, liefe ein Job noch einmal auf einer Zeile, die
    schon `failed` sagt — und der Guard laesst `failed` ausdruecklich durch, das
    Dokument wuerde also vollstaendig neu bezahlt.

    Zusicherung:
        True genau dann, wenn der Fehler wiederholbar ist UND das Budget aus
        `settings.max_attempts` noch nicht erschoepft ist.

    Kosten: keine.
    """
    if not should_run_again(exception):
        return False
    # `attempts` ist die Zahl der BISHERIGEN Versuche und beginnt bei 0. Die
    # ausfuehrliche Begruendung samt der Plan-Unschaerfe steht an der
    # Aufrufstelle in `get_retry_decision`.
    return job.attempts + 1 < settings.max_attempts


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
        wird sie in `build_daemon_app`, das die Einstellung ohnehin hat.
        """
        self._settings = settings

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
            Punkt 19, am Objekt von 3.9.0 belegt, fuer 3.10.0 unveraendert). Ein Retry von
            Hand auf einen erschoepften Job ist deshalb genau EIN weiterer
            Versuch, keine neue Dreier-Runde — und eine stalled-Wiederaufnahme
            zehrt am selben Budget.

        Kosten: keine.
        """
        # DIE ENTSCHEIDUNG SELBST steht in `wird_erneut_vorgelegt`, weil
        # `ocr_document` sie ebenfalls braucht — und beide Stellen sie gleich
        # beantworten muessen. Die Begruendung steht dort.
        #
        # Was in dieser Antwort steckt, ausgeschrieben, damit es niemand
        # nachschlagen muss:
        #
        # `should_run_again` und nicht `classify_failure` unmittelbar: Die
        # Einstufung hat DREI Werte, und aus dreien muessen diese Stelle und der
        # Ausnahme-Pfad des Executors dieselben zwei Gruppen bilden. Genau daran
        # ist es beim Bauen schon einmal auseinandergelaufen — Begruendung in
        # voller Laenge an `errors.should_run_again`.
        #
        # `attempts` ist die Zahl der BISHERIGEN Versuche und beginnt bei 0
        # (Referenz: „The first time a job is run, attempts will be 0"). Die Zahl
        # der bisherigen Laeufe ist also `attempts + 1`, und `max_attempts` sagt,
        # wie oft ein Dokument INSGESAMT versucht wird — nicht, wie oft
        # wiederholt wird.
        #
        # ANMERKUNG ZU EINER UNSCHAERFE IM PLAN, damit sie niemand fuer einen
        # Fehler haelt: Plan 5.1 nennt „bis 3 Versuche" UND die Folge
        # „~30 s/2 min/8 min" — drei Wartezeiten. Beides zusammen geht nicht auf:
        # Bei drei Laeufen insgesamt gibt es zwei Wartezeiten (30 s und 2 min),
        # die dritte waere erst bei einem vierten Lauf faellig. Massgeblich ist
        # hier die genauere der beiden Angaben, naemlich die an
        # `Settings.max_attempts`: „wie oft ein Dokument insgesamt versucht
        # wird". Wer drei Wartezeiten will, setzt `max_attempts=4` — der Wert ist
        # ein Parameter, die Rechnung bleibt dieselbe.
        if not wird_erneut_vorgelegt(exception, job, self._settings):
            return None

        # `retry_in` nimmt ganze Sekunden — `TimeDeltaParams` ist eine
        # TypedDict mit `int`-Feldern. Aufgerundet und nicht abgerundet: Bei
        # einem Rueckzug ist zu frueh schlechter als zu spaet, und mindestens
        # eine Sekunde muss es sein, sonst kaeme der Wiederlauf sofort.
        wartezeit = self._settings.retry_base_seconds * (
            self._settings.retry_factor**job.attempts
        )
        return RetryDecision(retry_in={"seconds": max(1, math.ceil(wartezeit))})


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
        sha256: Das einzige Argument des Jobs. Die Bytes stehen in `inputs` —
            und das ist Absicht: Ein Job-Argument ist eine Kopie, und eine Kopie
            kann von der Datenbank abweichen. Ein Hash kann das nicht.

    Idempotenz:
        Ja, ueber das Nachsehen im Executor — mit der gewollten Ausnahme
        `failed`.

    Kosten: JA.
    """
    mittel = context.additional_context
    einstellungen = mittel["settings"]

    # OB DIES DER LETZTE VERSUCH IST, weiss nur diese Ebene: Der Versuchszaehler
    # gehoert der Warteschlange, und der Executor weiss ausdruecklich nichts von
    # ihr. Also wird die Frage hier beantwortet und die Antwort weitergereicht.
    #
    # Wozu der Executor sie braucht: Bei einem wiederholbaren Fehler wirft er
    # normalerweise, ohne etwas zu schreiben — die Warteschlange legt ja neu
    # vor. Kommt aber kein Versuch mehr, faende dieses „neu vorlegen" nie statt,
    # und das Dokument bliebe fuer immer ohne Ergebnis. Genau so war es gebaut
    # (damals mit einer Zeile `new`): Ein
    # Dokument, das dreimal transient scheiterte, endete mit Job `failed` und
    # Zeile `new`, tauchte nie im `failed`-Zaehler auf und zaehlte fuer immer als
    # „pending" — obwohl `errors.py` und `db.py` beide zusichern, aus
    # erschoepften Versuchen werde `failed`.
    #
    # Geschrieben wird im EXECUTOR und nicht hier, obwohl die Erkenntnis hier
    # entsteht: Dort liegen die bis dahin abgeholten Teilstuecke. Sie sind
    # bezahlt und gehoeren in `raw`; ein Schreibvorgang von dieser Ebene aus
    # haette sie nicht und wuerde eine leere `failed`-Zeile hinterlassen.
    #
    # Die Bedingung ist NICHT nachgebaut, sondern dieselbe Funktion, die auch die
    # Wiederholungs-Strategie fragt. Zwei Formulierungen derselben Regel waren in
    # diesem Paket schon einmal der teuerste Fehler.
    #
    # Ein Fehler, der GAR NICHT wiederholbar ist, faellt hier nicht auf: Fuer ihn
    # schreibt der Executor ohnehin, unabhaengig von dieser Angabe. `letzter`
    # ist deshalb nur fuer den wiederholbaren Fall interessant.
    letzter = not wird_erneut_vorgelegt(
        _ERSCHOEPFUNGS_SONDE, context.job, einstellungen
    )

    await executor.process_document(
        sha256,
        settings=einstellungen,
        runpod=mittel["runpod"],
        pool=mittel["pool"],
        letzter_versuch=letzter,
    )


_ERSCHOEPFUNGS_SONDE: Final = TransientRunPodError(
    "Sonde: nur zur Frage, ob das Versuchsbudget noch reicht"
)
"""Ein Stellvertreter-Fehler, um `wird_erneut_vorgelegt` nach dem BUDGET zu
fragen, ohne einen echten Fehler zu haben.

Der Grund fuer den Umweg: Ob dies der letzte Versuch ist, muss VOR dem Lauf
feststehen — der Executor bekommt es als Parameter. Einen Fehler gibt es zu
diesem Zeitpunkt noch nicht. `wird_erneut_vorgelegt` beantwortet aber zwei
Fragen in einer: „ist der Fehler wiederholbar" UND „reicht das Budget". Mit
einem ausdruecklich wiederholbaren Stellvertreter faellt die erste Frage weg,
und uebrig bleibt genau die zweite.

Warum kein zweiter Zaehler-Vergleich an dieser Stelle: Er waere eine zweite
Formulierung derselben Regel, und genau daran ist dieses Paket schon einmal
auseinandergelaufen. Ein Stellvertreter ist haesslicher als ein `if`, aber er
kann nicht von der Strategie abweichen."""


WORKER_ABGESTUERZT: Final = "Worker wiederholt abgestuerzt"
"""Der Grund am `failed`-Ergebnis eines Dokuments, dessen Job der Waechter bei
erschoepftem Budget beendet: Jeder Versuch endete mit einem toten Worker."""


async def retry_stalled(context: procrastinate.JobContext, timestamp: int) -> None:
    """Legt Jobs erneut vor, deren Worker gestorben ist, solange Versuche uebrig sind.

    Angemeldet unter `config.TASK_RETRY_STALLED` in der Queue
    `config.QUEUE_MAINTENANCE`, periodisch nach `settings.stalled_check_cron`,
    mit einem eigenen `queueing_lock`, damit sich diese Aufgabe nicht selbst
    ueberholt.

    Der Grundstock ist Procrastinates dokumentiertes Rezept: `get_stalled_jobs()`
    holen, ueber die Liste laufen, `retry_job()` rufen (Feature-Verifikation
    Punkt 6). Das Rezept laesst eigene Regeln ausdruecklich zu, und eine kommt
    hinzu: das Budget (Mineroo#27). Ohne es lief ein Dokument, das den Worker
    abstuerzen laesst, endlos, und jede Runde bezahlte die bis zu 23 anderen
    laufenden Dokumente neu.

    Je haengendem Job:

        Versuche uebrig (dieselbe Frage wie an der Retry-Strategie,
        `wird_erneut_vorgelegt` mit der Sonde): `retry_job`. Der Job steht
        wieder auf `todo`, sein Versuchszaehler steigt, und beim naechsten
        Anlauf beginnt der Executor bei Schritt 0.

        Budget erschoepft: Bei einem Dokument-Job schreibt der Executor das
        `failed`-Ergebnis `WORKER_ABGESTUERZT`, aber nur, wenn noch keins da
        ist (`executor.write_failure_if_no_result`); dann beendet
        `JobManager.finish_job` den Job als `failed`, mit derselben Loeschregel
        wie der Worker (`settings.delete_jobs`). Ein Job eines anderen Tasks
        hat kein Dokument und wird nur beendet.

    Die Regel, die das Modell traegt: Jeder Job, der nicht neu eingereiht wird,
    schreibt ein Ergebnis.

    Einen ZEILEN-Abgleich gibt es hier ausdruecklich nicht (Plan 2.4, ein Buch):
    Weil die Ergebniszeile erst am Ende des Jobs entsteht, kann sie mit nichts
    auseinanderlaufen. Es gibt nichts abzugleichen.

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
    verwalter = context.app.job_manager
    mittel = context.additional_context
    einstellungen: Settings = mittel["settings"]
    for job in await verwalter.get_stalled_jobs():
        if wird_erneut_vorgelegt(_ERSCHOEPFUNGS_SONDE, job, einstellungen):
            await verwalter.retry_job(job)
            continue
        if job.task_name == config.TASK_OCR_DOCUMENT:
            await executor.write_failure_if_no_result(
                str(job.task_kwargs["sha256"]),
                settings=einstellungen,
                pool=mittel["pool"],
                grund=WORKER_ABGESTUERZT,
            )
        await verwalter.finish_job(
            job,
            status=Status.FAILED,
            delete_job=DeleteJobCondition(einstellungen.delete_jobs)
            is DeleteJobCondition.ALWAYS,
        )


def enqueue_document(
    app: procrastinate.App, connection: Any, sha256: str
) -> int:
    """Reiht ein Dokument ein, EINZELN, und gibt die Kennung des Jobs.

    Eingereiht wird per NAMEN und mit ausdruecklicher Queue, beide aus denselben
    Konstanten, unter denen der Daemon den Task anmeldet. Ohne `queue=` landete
    der Job in `default`, die der Daemon nie liest. Die Verbindung ist die des
    Aufrufers (`connection=`): Das Einreihen holt sich keine aus dem Pool, laeuft
    in der Transaktion des Aufrufers, und das Signal, das die Worker weckt, geht
    erst bei deren Commit hinaus.

    Am Job steht nur `lock = sha256`, kein `queueing_lock`. Die Sperre gegen ein
    zweites Einreihen ist die Eingabe-Tabelle (ihr Primaerschluessel): Wer
    einreiht, hat die Eingabe gerade neu angelegt oder legt fuer eine stehende
    Eingabe ausdruecklich einen neuen Job an (`retry`). Ein `queueing_lock`
    daneben loeste Procrastinate-Issue #1446 aus: Ein zweiter Job, eingereiht
    waehrend der erste laeuft, blieb fuer immer auf „laeuft" stehen, wenn der erste
    wiederholbar scheiterte. `lock` serialisiert Jobs desselben Dokuments; er wird
    bei JEDEM Job-Ende frei, auch bei einem gescheiterten — „the second task
    cannot run before the first one has ended (successfully or not)"
    (Feature-Verifikation Punkt 18) —, eine Kette kann also nie verklemmen.

    Warum ausdruecklich EINZELN und nie ueber das Stapel-Einreihen: Jedes Dokument
    hat seine eigene Transaktion mit seiner Eingabe, und ein Stapel, der an einem
    Element scheitert, rollte alle zurueck (Feature-Verifikation Punkt 9).

    Args:
        app: Die App des Clients (`build_client_app`), geoeffnet oder nicht — mit
            `connection=` braucht das Einreihen keinen Pool.
        connection: Die Verbindung des Aufrufers, in deren Transaktion der Job
            entsteht.
        sha256: Welches Dokument.

    Returns:
        Die Kennung des Jobs, die `defer` zurueckgibt. Der Aufrufer schreibt sie
        in `inputs.job_id`: Ueber sie findet `document()` den Job und `retry` den,
        den es wiederholt.

    Zusicherung:
        Reiht ein und wartet nicht. Wer warten will, nimmt `wait`. Das trennt
        Jurgens „losschicken ist einfach einen job erstellen" von „das warten auf
        eine antwort […] eine seperate funktion" (Plan 2.3).

    Kosten: keine unmittelbar — mittelbar loest der Job Verarbeitung aus.
    """
    return _ocr_task(app, connection, sha256).defer(sha256=sha256)


async def enqueue_document_async(
    app: procrastinate.App, connection: Any, sha256: str
) -> int:
    """Dasselbe wie `enqueue_document`, asynchron, fuer den `AsyncOCRClient`:
    Name, Queue und Sperre gleich, eingereiht mit `defer_async` auf der
    `AsyncConnection` des Aufrufers, in dessen Transaktion.

    Args:
        app: Die App des Async-Clients (`build_async_client_app`).
        connection: Die `psycopg.AsyncConnection` des Aufrufers.
        sha256: Welches Dokument.

    Returns:
        Die Kennung des Jobs, fuer `inputs.job_id`.

    Kosten: keine unmittelbar — mittelbar loest der Job Verarbeitung aus.
    """
    return await _ocr_task(app, connection, sha256).defer_async(sha256=sha256)


def _ocr_task(
    app: procrastinate.App, connection: Any, sha256: str
) -> procrastinate.jobs.JobDeferrer:
    """Der Task des Daemons, per Namen, mit Queue, Sperre und Verbindung — die
    eine Konfiguration hinter `enqueue_document` und `enqueue_document_async`."""
    return app.configure_task(
        name=config.TASK_OCR_DOCUMENT,
        queue=config.QUEUE_OCR,
        lock=sha256,
        connection=connection,
    )


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

    Der Pool kommt UNGEOEFFNET zurueck. Das ist kein Versehen: Er wird mit
    `await pool.open()` geoeffnet, und diese Funktion ist synchron. Im
    Konstruktor zu oeffnen ist in psycopg_pool 3.3.0 abgekuendigt und schreibt
    eine Warnung in genau die Konsole, in der der erste Bestandslauf beobachtet
    werden soll. Der Daemon oeffnet ihn, weil er ihn ohnehin auch schliesst —
    Oeffnen und Schliessen gehoeren demselben.

    Kosten: keine.
    """
    return {
        # Die Einstellung wandert mit. Der Vertrag nannte in Phase A nur
        # `runpod` und `pool` — der Task braucht aber auch die Schwellen, und
        # sie sich aus einer globalen Einstellung zu holen ist ausdruecklich
        # verboten (es gibt keine). Sie hier mitzugeben ist der einzige Weg, der
        # ohne globalen Zustand auskommt.
        "settings": settings,
        "runpod": RunPodClient(settings),
        "pool": AsyncConnectionPool(
            settings.db_dsn,
            min_size=1,
            max_size=settings.ocr_concurrency,
            open=False,
            # Prueft jede Verbindung beim Ausleihen. Einen UEBERGEBENEN Pool
            # prueft Procrastinate nicht selbst (nur den, den es selbst anlegt);
            # ohne diese Zeile schriebe der Daemon nach einem Postgres-Neustart
            # ueber eine tote Verbindung — und zwar im Schreibvorgang, der das
            # bezahlte Ergebnis ablegt.
            #
            # EINE EIGENHEIT, die man kennen muss (psycopg_pool 3.3.0,
            # `pool_async.py`: `CLIENT_EXCEPTIONS = (Exception,
            # asyncio.CancelledError)`): Trifft ein Abbruch einen Task WAEHREND
            # der Pruefung, verschluckt der Pool ihn und versucht es erneut — der
            # Task laeuft weiter, als waere nichts gewesen. Fuer den Daemon heisst
            # das: Faellt SIGTERM in dieses Zeitfenster (Millisekunden, bei jeder
            # Ausleihe), laeuft ein Worker weiter, und das Herunterfahren haengt,
            # bis ein zweites Signal kommt (das bricht ausdruecklich sofort ab,
            # siehe `daemon.run`). Procrastinates eigener Standard-Pool traegt
            # dasselbe `check` und damit dieselbe Eigenheit. Belegt mit einem
            # Task, der waehrend einer langsamen Pruefung abgebrochen wird: Er
            # laeuft mit `check` normal zu Ende, ohne wird er abgebrochen.
            check=AsyncConnectionPool.check_connection,
        ),
    }
