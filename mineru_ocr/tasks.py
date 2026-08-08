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

import math
from typing import Any

import attr
import procrastinate
from procrastinate import RetryDecision, testing
from psycopg_pool import AsyncConnectionPool

from . import config, executor
from .config import Settings
from .errors import ConfigurationError, should_run_again
from .runpod import RunPodClient

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

_ZUGANG_BEIM_IMPORT = app.connector
"""Der speicherinterne Zugang, mit dem die App entsteht — als IDENTITAET gemerkt.

Wozu: `defer_document` soll den Fall abfangen, dass niemand `configure` gerufen
hat. Zu pruefen, ob der Zugang ein `InMemoryConnector` IST, waere dafuer das
falsche Merkmal — Procrastinates eigenes Testmuster haengt absichtlich einen
solchen ein, und dieser Test wuerde dann jeden Test unmoeglich machen. Geprueft
wird deshalb auf DIESES EINE Objekt: Ist es noch da, hat niemand konfiguriert.
Ein eigens eingehaengter Zugang, auch ein speicherinterner, ist eine bewusste
Entscheidung und geht durch."""


def register_tasks(settings: Settings) -> None:
    """Meldet die beiden Tasks an. Wird von `configure` gerufen, nicht von Hand.

    Die Anmeldung steht als Funktion und nicht als Dekorator ueber den
    Task-Funktionen, weil beide Tasks Werte aus `Settings` brauchen, die es beim
    Import noch nicht gibt — die Wiederholungs-Strategie ihre Wartezeiten und ihr
    Versuchsbudget, der Waechter seinen Zeitplan. Ein Dekorator liefe beim Import,
    also vor `Settings.load`.

    (Die Signatur stand in Phase A ohne Argument. Das konnte nicht stimmen: Der
    Absatz darueber sagt, dass die Anmeldung Werte aus `Settings` braucht, und
    ohne Argument haette sie sie sich nur aus einer globalen Einstellung holen
    koennen — die es ausdruecklich nicht gibt.)

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
        Mehrfach aufrufbar. Beim zweiten Aufruf werden die Tasks nicht erneut
        angemeldet — wohl aber ihre EINSTELLUNGEN nachgezogen: die
        Wiederholungs-Strategie und der Zeitplan des Waechters.

        Warum das Nachziehen und nicht bloss ein Nichts-Tun: Genau daran ist es
        beim Bauen gescheitert, und zwar still. `configure(settings)` sichert zu,
        dass die uebergebene Einstellung gilt. Wer sie mit anderen Werten ein
        zweites Mal ruft — kuerzere Wartezeiten, ein kleineres Versuchsbudget —
        und dabei nur die Anmeldung ueberspringt, behaelt die Werte des ERSTEN
        Aufrufs. Der Job wartete dann dreissig Sekunden statt der eingestellten
        fuenfzig Millisekunden, und niemand sah, warum: Die Einstellung war ja
        sichtbar gesetzt. Aufgefallen ist es erst daran, dass zwei
        Integrationstests einzeln liefen und im vollstaendigen Lauf in den
        Zeitablauf gerieten.

        Baut keine Verbindung auf.

    Kosten: keine.
    """
    if config.TASK_OCR_DOCUMENT in app.tasks:
        # Die Einstellungen nachziehen — siehe die Begruendung oben.
        app.tasks[config.TASK_OCR_DOCUMENT].retry_strategy = OCRRetryStrategy(settings)

        # Der Zeitplan des Waechters wird ERSETZT und nicht geaendert:
        # `PeriodicTask.cron` ist schreibgeschuetzt (am Objekt festgestellt —
        # ein Schreibversuch bricht zur Laufzeit ab). Ein neuer Eintrag an
        # derselben Stelle im Verzeichnis erreicht dasselbe und geht mit der
        # Bauart der fremden Klasse statt gegen sie.
        for schluessel, eintrag in list(app.periodic_registry.periodic_tasks.items()):
            if eintrag.task.name == config.TASK_RETRY_STALLED:
                app.periodic_registry.periodic_tasks[schluessel] = attr.evolve(
                    eintrag, cron=settings.stalled_check_cron
                )
        return

    # In Dekorator-Form aufgerufen und nicht mit der Funktion als erstem
    # Argument: `app.task` nimmt beides, aber das erste Argument NUR allein —
    # `task(func)` ohne weitere Angaben, oder `task(name=…, …)(func)`. Beides
    # zugleich gibt es nicht, und die Typpruefung sagt das auch.
    # Die Unterdrueckung an der naechsten Zeile verdeckt KEINEN Fehler von uns,
    # sondern haelt einen Widerspruch in Procrastinate selbst fest. Er ist am
    # Objekt der installierten 3.9.0 geprueft, in beide Richtungen:
    #
    #   Die Doku sagt: „class procrastinate.BaseRetryStrategy — If you want to
    #   implement your own retry strategy, you can inherit from this class.
    #   Child classes only need to implement get_retry_decision."
    #
    #   Die Typangabe sagt etwas Engeres: `RetryValue: TypeAlias = bool | int |
    #   RetryStrategy` (procrastinate/retry.py) — `BaseRetryStrategy` steht dort
    #   NICHT, obwohl `RetryStrategy` von ihr erbt.
    #
    #   Zur Laufzeit gilt die Doku: `get_retry_strategy` gibt alles, was nicht
    #   falsch, `True` oder eine ganze Zahl ist, unveraendert zurueck (am
    #   Quelltext gelesen). Eine eigene Strategie funktioniert also.
    #
    # Der Ausweg „von `RetryStrategy` erben statt von `BaseRetryStrategy`" waere
    # schlechter: Diese Klasse bringt eigene Felder und eine eigene
    # Entscheidungslogik mit, die wir vollstaendig ersetzen — wir erbten Ballast,
    # um eine Typangabe zufriedenzustellen.
    #
    # Bei einem Procrastinate-Upgrade gehoert diese Zeile geprueft: Faellt die
    # Unterdrueckung als „unbenutzt" auf, ist der Widerspruch behoben und sie
    # kann weg. Genau diese Meldung gibt mypy von sich aus aus.
    app.task(  # type: ignore[call-overload]
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
    zugang: procrastinate.BaseConnector
    if for_worker:
        zugang = procrastinate.PsycopgConnector(conninfo=settings.db_dsn)
    else:
        zugang = procrastinate.SyncPsycopgConnector(conninfo=settings.db_dsn)

    # BEIDE Felder, und das ist kein Abschreiben aus Vorsicht: Procrastinates
    # eigenes `replace_connector` setzt genau diese zwei, und `job_manager` haelt
    # eine EIGENE Referenz. Wer nur `app.connector` setzt, bekommt einen
    # Zustand, den man nicht vermutet und schwer findet — das Einreihen liefe
    # gegen die Datenbank, jede Abfrage ueber den `job_manager` aber weiter gegen
    # den speicherinternen Zugang. Also: leere Job-Listen, obwohl Jobs da sind.
    #
    # Warum nicht `with_connector`: Das ist seit 2.14.0 abgekuendigt, und die
    # Abkuendigung nennt selbst den Grund — es baut eine ZWEITE App, die
    # dieselben Tasks referenziert, und die Tasks zeigen auf die erste zurueck.
    # Warum nicht `replace_connector`: Das ist ein Kontext-Manager, der am Ende
    # zuruecktauscht; er ist fuer den voruebergehenden Tausch im Test gedacht,
    # nicht fuer die dauerhafte Einrichtung.
    app.connector = zugang
    app.job_manager.connector = zugang

    register_tasks(settings)
    return app


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
            Punkt 19, am Objekt der installierten 3.9.0 belegt). Ein Retry von
            Hand auf einen erschoepften Job ist deshalb genau EIN weiterer
            Versuch, keine neue Dreier-Runde — und eine stalled-Wiederaufnahme
            zehrt am selben Budget.

        Kosten: keine.
        """
        # `should_run_again` und nicht `classify_failure` unmittelbar: Die
        # Einstufung hat DREI Werte, und aus dreien muessen diese Stelle und der
        # Ausnahme-Pfad des Executors dieselben zwei Gruppen bilden. Genau daran
        # ist es beim Bauen schon einmal auseinandergelaufen — Begruendung in
        # voller Laenge an `errors.should_run_again`.
        if not should_run_again(exception):
            return None

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
        if job.attempts + 1 >= self._settings.max_attempts:
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
        sha256: Das einzige Argument des Jobs. Alles Weitere steht in der Zeile —
            und das ist Absicht: Ein Job-Argument ist eine Kopie, und eine Kopie
            kann von der Zeile abweichen. Ein Hash kann das nicht.

    Idempotenz:
        Ja, ueber den Terminal-Guard im Executor — mit der gewollten Ausnahme
        `failed`.

    Kosten: JA.
    """
    mittel = context.additional_context
    await executor.process_document(
        sha256,
        settings=mittel["settings"],
        runpod=mittel["runpod"],
        pool=mittel["pool"],
    )


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
    verwalter = context.app.job_manager
    for job in await verwalter.get_stalled_jobs():
        await verwalter.retry_job(job)


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
    if app.connector is _ZUGANG_BEIM_IMPORT:
        raise ConfigurationError(
            "Es wurde eingereiht, ohne dass `configure` gelaufen ist. Der Job "
            "waere in den Arbeitsspeicher dieses Prozesses gegangen und mit ihm "
            "verschwunden — kein Fehler, keine Meldung, nur nichts. Erst "
            "`tasks.configure(settings, for_worker=False)` rufen."
        )

    aufgabe = app.tasks[config.TASK_OCR_DOCUMENT]

    def _einreihen() -> None:
        aufgabe.configure(lock=sha256, queueing_lock=sha256).defer(sha256=sha256)

    try:
        _einreihen()
    except procrastinate.exceptions.AppNotOpen:
        # Die App war noch nicht geoeffnet. Das ist eine Luecke zwischen zwei
        # Zusicherungen, die beim Bauen aufgefallen ist: `configure` sichert
        # ausdruecklich zu, KEINE Verbindung aufzubauen — damit es auch laufen
        # kann, wenn Postgres gerade weg ist —, und `defer_document` ist die
        # erste Stelle, die wirklich eine braucht.
        #
        # Nachgeholt wird es hier und nicht beim Aufrufer, und erst NACH dem
        # ersten Versuch: So kostet der Normalfall (App laengst offen) keinen
        # zusaetzlichen Aufruf, und der Sonderfall heilt sich selbst.
        #
        # Das Oeffnen ist mehrfach ausfuehrbar — der Konnektor legt den
        # Verbindungspool nur an, wenn es noch keinen gibt (`if self._pool:
        # return`, am Quelltext gelesen). Ihn stattdessen JE EINREIHUNG zu
        # oeffnen und zu schliessen waeren dreissigtausend Pool-Aufbauten im
        # Bestandslauf. Der Pool des Produzenten lebt danach fuer die Dauer des
        # Prozesses; das ist das dokumentierte Grundmuster fuer den synchronen
        # Produzenten und kein Leck.
        try:
            app.open()
        except NotImplementedError as fehler:
            # Der eingehaengte Zugang ist ein ASYNCHRONER, und der kennt kein
            # synchrones Oeffnen. Das passiert, wenn im selben Prozess ein
            # Daemon konfiguriert wurde — dort gehoert die App ihm, und er
            # oeffnet sie mit `open_async`. Von aussen dazwischenzugreifen waere
            # falsch, deshalb wird hier gesagt, was zu tun ist, statt zu raten.
            raise ConfigurationError(
                "Die App traegt einen asynchronen Zugang und ist nicht "
                "geoeffnet. Aus synchronem Code laesst sie sich so nicht "
                "oeffnen. Entweder `tasks.configure(settings, "
                "for_worker=False)` fuer den Produzenten benutzen, oder die "
                "App vom Besitzer oeffnen lassen "
                "(`async with app.open_async(): …`)."
            ) from fehler
        try:
            _einreihen()
        except procrastinate.exceptions.AlreadyEnqueued:
            return False
    except procrastinate.exceptions.AlreadyEnqueued:
        # „Laeuft schon" — der Normalfall beim erneuten Durchlaufen eines
        # Ordners und ausdruecklich kein Fehler. Genau darauf beruht Jurgens
        # Ordner-Szenario: „dann später kommt zum ordner noch ein pfad hinzu den
        # tut man einfach dazu und lässt nochmal durchlaufen weil eh nur läuft
        # was noch fehlt".
        return False
    return True


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
        ),
    }
