"""Warteschlangen-Anbindung — offline, mit Procrastinates eigenem Testwerkzeug.

Diese Tests brauchen KEINE Datenbank. Procrastinate bringt für genau diesen
Zweck einen speicherinternen Zugang mit (`testing.InMemoryConnector`), und der
Plan hatte ihn ausdrücklich in die kostenlose Offline-Stufe gelegt. Ohne diese
Datei wäre die gesamte Anbindung nur mit laufender Instanz prüfbar.
"""

from __future__ import annotations

import dataclasses
import inspect
from collections.abc import Iterator
from datetime import UTC, datetime

import procrastinate
import pytest
from procrastinate import testing
from procrastinate.jobs import Job

from mineru_ocr import config, tasks
from mineru_ocr.config import Settings
from mineru_ocr.errors import (
    ConfigurationError,
    CorruptDocumentError,
    InsufficientBalanceError,
    TerminalRunPodError,
    TransientRunPodError,
)

HASH = "a" * 64


@pytest.fixture
def app(settings: Settings) -> Iterator[procrastinate.App]:
    """Die App mit einem EIGENEN speicherinternen Zugang.

    Procrastinates dokumentiertes Testmuster: `replace_connector` haengt einen
    `InMemoryConnector` ein und tauscht ihn am Ende zurueck. Ein eigener und
    nicht der Zugang vom Import — genau daran unterscheidet `defer_document`,
    ob jemand `configure` gerufen hat.
    """
    speicher = testing.InMemoryConnector()
    with tasks.app.replace_connector(speicher) as a:
        tasks.register_tasks(settings)
        yield a


def _job(attempts: int = 0) -> Job:
    return Job(
        id=1,
        status="doing",
        queue=config.QUEUE_OCR,
        lock=HASH,
        queueing_lock=HASH,
        task_name=config.TASK_OCR_DOCUMENT,
        attempts=attempts,
    )


# --- Anmeldung ------------------------------------------------------------


def test_tasks_sind_unter_den_festgelegten_namen_angemeldet(
    app: procrastinate.App,
) -> None:
    """Nach `configure` tragen die Tasks genau `config.TASK_OCR_DOCUMENT` und
    `config.TASK_RETRY_STALLED`.

    Der Fall, gegen den das steht, meldet keinen Fehler: Kennen Produzent und
    Daemon verschiedene Namen, bleiben die Jobs liegen, und beide Seiten melden
    Erfolg.
    """
    assert config.TASK_OCR_DOCUMENT in app.tasks
    assert config.TASK_RETRY_STALLED in app.tasks
    # Und die Namen sind ausdruecklich gesetzt, nicht aus dem Modulpfad
    # entstanden — sonst haetten sie den Import-Pfad als Praefix.
    assert app.tasks[config.TASK_OCR_DOCUMENT].name == "mineru_ocr.ocr_document"


def test_queues_sind_getrennt(app: procrastinate.App) -> None:
    """Dokument-Task in `ocr`, Waechter in `maintenance`.

    Die Trennung ist keine Ordnungsfrage: Periodische Tasks sind gewoehnliche
    Jobs und teilen den Slot-Pool ihres Workers. Haengen alle
    vierundzwanzig OCR-Slots in Balance-Warteschleifen, kaeme ein Waechter auf
    derselben Queue nie dran.
    """
    assert app.tasks[config.TASK_OCR_DOCUMENT].queue == config.QUEUE_OCR
    assert app.tasks[config.TASK_RETRY_STALLED].queue == config.QUEUE_MAINTENANCE


def test_waechter_ist_periodisch_und_gegen_sich_selbst_gesperrt(
    app: procrastinate.App, settings: Settings
) -> None:
    """Der Waechter laeuft nach Zeitplan und hat einen eigenen `queueing_lock`,
    damit er sich nicht selbst ueberholt."""
    eintraege = list(app.periodic_registry.periodic_tasks.values())
    namen = [e.task.name for e in eintraege]
    assert config.TASK_RETRY_STALLED in namen
    assert app.tasks[config.TASK_RETRY_STALLED].queueing_lock == "retry_stalled"


def test_anmelden_ist_mehrfach_aufrufbar(
    app: procrastinate.App, settings: Settings
) -> None:
    """Ein zweiter Aufruf ist ein Nichts-Tun, kein Fehler."""
    vorher = len(app.tasks)
    tasks.register_tasks(settings)
    tasks.register_tasks(settings)
    assert len(app.tasks) == vorher


def test_produzenten_zugang_kann_nur_einreihen(settings: Settings) -> None:
    """Mit dem synchronen Zugang schlaegt jeder andere Vorgang fehl — ein
    Produzent, der versehentlich einen Worker startet, waere ein zweiter Exekutor,
    von dem niemand weiss.

    Geprueft am TYP des eingehaengten Zugangs, ohne eine Verbindung zu
    oeffnen: `configure` baut ausdruecklich keine auf, und ein Test, der eine
    aufbaut, pruefte etwas anderes als die Zusicherung.
    """
    urspruenglich = tasks.app.connector
    try:
        produzent = tasks.configure(settings, for_worker=False)
        assert isinstance(produzent.connector, procrastinate.SyncPsycopgConnector)

        daemon = tasks.configure(settings, for_worker=True)
        assert isinstance(daemon.connector, procrastinate.PsycopgConnector)

        # UND der job_manager zeigt auf denselben Zugang. Wer nur
        # `app.connector` setzt, bekommt leere Job-Listen, obwohl Jobs da sind.
        assert daemon.job_manager.connector is daemon.connector
    finally:
        tasks.app.connector = urspruenglich
        tasks.app.job_manager.connector = urspruenglich


# --- Einreihen ------------------------------------------------------------


def test_einreihen_erzeugt_einen_job(app: procrastinate.App) -> None:
    """Der Normalfall: ein Job, mit dem Hash als einzigem Argument."""
    assert tasks.defer_document(HASH) is True
    jobs = list(app.connector.jobs.values())  # type: ignore[attr-defined]
    assert len(jobs) == 1
    assert jobs[0]["task_name"] == config.TASK_OCR_DOCUMENT
    # Der Hash ist das EINZIGE Argument. Alles Weitere steht in der Zeile — ein
    # Job-Argument waere eine Kopie, und eine Kopie kann abweichen.
    assert jobs[0]["args"] == {"sha256": HASH}


def test_zweites_einreihen_meldet_laeuft_schon(app: procrastinate.App) -> None:
    """`AlreadyEnqueued` wird gefangen und zu False — der Normalfall beim
    wiederholten Durchlaufen eines Ordners, kein Fehler."""
    assert tasks.defer_document(HASH) is True
    assert tasks.defer_document(HASH) is False
    assert len(app.connector.jobs) == 1  # type: ignore[attr-defined]


def test_beide_sperren_werden_gesetzt(app: procrastinate.App) -> None:
    """`queueing_lock` UND `lock`, beide auf den Hash.

    Ohne den zweiten wuerde ein waehrend der Verarbeitung nachgeschobenes
    Duplikat angenommen und liefe parallel: dasselbe Dokument zweimal bezahlt.
    Der `queueing_lock` allein reicht nicht — er bindet nur den Zustand `todo`
    und ist frei, sobald der Job laeuft.
    """
    tasks.defer_document(HASH)
    job = next(iter(app.connector.jobs.values()))  # type: ignore[attr-defined]
    assert job["queueing_lock"] == HASH
    assert job["lock"] == HASH


def test_kein_stapel_einreihen() -> None:
    """Es wird einzeln eingereiht. Ein Stapel mit einem einzigen Konflikt wuerde
    vollstaendig zurueckgerollt — bei dreissigtausend Dokumenten genuegte ein
    bereits wartendes, damit gar nichts passiert.

    Geprueft am Quelltext: `batch_defer` darf im Modul nicht vorkommen. Der
    Vorspann von `defer_document` erklaert, warum — und ein Test, der nur die
    Wirkung prueft, saehe den Unterschied nicht.
    """
    quelle = inspect.getsource(tasks)
    assert "batch_defer" not in quelle.replace("`batch_defer`", "")


def test_einreihen_ohne_konfigurierten_zugang_wirft() -> None:
    """Wer `configure` vergisst, schreibt sonst in den Arbeitsspeicher und
    wundert sich, dass nichts geschieht.

    Kein Fehler, keine Meldung, nur nichts — die unangenehmste aller
    Fehlerarten. Deshalb wird sie hier laut.
    """
    urspruenglich = tasks.app.connector
    try:
        tasks.app.connector = tasks._ZUGANG_BEIM_IMPORT
        with pytest.raises(ConfigurationError, match="configure"):
            tasks.defer_document(HASH)
    finally:
        tasks.app.connector = urspruenglich


# --- Wiederholungs-Strategie ---------------------------------------------


def test_strategie_fragt_die_gemeinsame_funktion(settings: Settings) -> None:
    """Die Entscheidung kommt aus `errors.classify_failure`, nicht aus einer
    zweiten Regel — sonst laeuft ein Job noch einmal, waehrend die Zeile schon
    `failed` sagt.

    Geprueft, indem je ein Vertreter jeder Fehlerart durchgereicht wird: Was
    `classify_failure` als wiederholbar einstuft, wird wiederholt; alles andere
    nicht.
    """
    strategie = tasks.OCRRetryStrategy(settings)

    wiederholbar: list[Exception] = [
        TransientRunPodError("Gegenseite hustet"),
        # Ein leeres Konto ist kein Fehler des Dokuments — es wird gewartet,
        # nicht aufgegeben.
        InsufficientBalanceError("402"),
    ]
    for fehler in wiederholbar:
        assert (
            strategie.get_retry_decision(exception=fehler, job=_job()) is not None
        ), type(fehler).__name__

    endgueltig: list[Exception] = [
        TerminalRunPodError("Modell abgestuerzt"),
        CorruptDocumentError("PDF kaputt"),
    ]
    for fehler in endgueltig:
        assert (
            strategie.get_retry_decision(exception=fehler, job=_job()) is None
        ), type(fehler).__name__


def test_kein_retry_ergibt_none(settings: Settings) -> None:
    """Verweigert wird mit `return None`, nicht mit `RetryDecision(
    should_retry=False)`.

    Der Parameter steht nur in einem Beispiel der Anleitung, nicht in der
    Referenz-Signatur. Auf ihm aufzubauen braeche beim naechsten Upgrade — und
    zwar als „wird trotzdem wiederholt", also als Geld.
    """
    strategie = tasks.OCRRetryStrategy(settings)
    assert (
        strategie.get_retry_decision(
            exception=TerminalRunPodError("aus"), job=_job()
        )
        is None
    )
    # Und der Parameter kommt im Modul ueberhaupt nicht vor.
    assert "should_retry" not in inspect.getsource(tasks).replace(
        "`should_retry=False`", ""
    ).replace("should_retry=False)", "")


def test_wartezeiten_wachsen_exponentiell(settings: Settings) -> None:
    """Grundwert mal Faktor hoch Versuchszahl: 30 s, 2 min, 8 min.

    Grosszuegig, weil die typischen wiederholbaren Fehler — Warteschlange voll,
    Worker startet kalt — Minuten und nicht Sekunden brauchen.
    """
    grosszuegig = dataclasses.replace(settings, max_attempts=99)
    strategie = tasks.OCRRetryStrategy(grosszuegig)
    erwartet = [30, 120, 480]
    for versuche, sekunden in enumerate(erwartet):
        vorher = datetime.now(UTC)
        entscheidung = strategie.get_retry_decision(
            exception=TransientRunPodError("x"), job=_job(attempts=versuche)
        )
        assert entscheidung is not None
        # `RetryDecision` speichert KEIN `retry_in`: Der Konstruktor rechnet es
        # sofort in ein `retry_at` um (am Quelltext gelesen). Geprueft wird also
        # der Abstand, nicht das uebergebene Feld — und das ist ohnehin das
        # Ehrlichere, denn der Abstand ist es, worauf es ankommt.
        assert entscheidung.retry_at is not None
        abstand = (entscheidung.retry_at - vorher).total_seconds()
        assert sekunden - 1 <= abstand <= sekunden + 1, (versuche, abstand)


def test_budget_kommt_aus_der_einstellung(settings: Settings) -> None:
    """Die Strategie benutzt `settings.max_attempts` und nicht eine eigene Zahl."""
    eng = dataclasses.replace(settings, max_attempts=1)
    strategie = tasks.OCRRetryStrategy(eng)
    # Bei einem einzigen erlaubten Versuch gibt es keine Wiederholung.
    assert (
        strategie.get_retry_decision(exception=TransientRunPodError("x"), job=_job())
        is None
    )


def test_erschoepftes_budget_ergibt_none(settings: Settings) -> None:
    """Ist das Budget aufgebraucht, wird nicht mehr wiederholt.

    `max_attempts` sagt, wie oft ein Dokument INSGESAMT versucht wird — die Zahl
    der bisherigen Laeufe ist `attempts + 1`. Bei drei erlaubten Versuchen gibt
    es also zwei Wiederholungen, nicht drei.

    Zur Reichweite des Zaehlers: Er zaehlt JEDE Wiedervorlage, auch die des
    stalled-Waechters und einen Retry von Hand — alle drei laufen durch dieselbe
    SQL-Funktion (Feature-Verifikation Punkt 19). Ein Retry von Hand auf einen
    erschoepften Job ist deshalb genau EIN weiterer Versuch, keine neue Runde.
    """
    strategie = tasks.OCRRetryStrategy(settings)  # max_attempts = 3
    assert settings.max_attempts == 3

    for versuche in (0, 1):
        assert (
            strategie.get_retry_decision(
                exception=TransientRunPodError("x"), job=_job(attempts=versuche)
            )
            is not None
        ), versuche

    for versuche in (2, 3, 7):
        assert (
            strategie.get_retry_decision(
                exception=TransientRunPodError("x"), job=_job(attempts=versuche)
            )
            is None
        ), versuche


# --- Die Betriebsmittel des Workers --------------------------------------


def test_betriebsmittel_kommen_ungeoeffnet(settings: Settings) -> None:
    """Der Verbindungspool kommt UNGEOEFFNET zurueck.

    Im Konstruktor zu oeffnen ist in psycopg_pool 3.3.0 abgekuendigt und
    schriebe eine Warnung in genau die Konsole, in der der erste Bestandslauf
    beobachtet werden soll. Oeffnen und Schliessen gehoeren demselben — dem
    Daemon.
    """
    mittel = tasks.worker_context_resources(settings)
    assert set(mittel) == {"settings", "runpod", "pool"}
    assert mittel["pool"].closed is True
    assert mittel["settings"] is settings


def test_pool_ist_so_gross_wie_die_gleichzeitigkeit(settings: Settings) -> None:
    """Der Pool fasst so viele Verbindungen, wie Dokumente gleichzeitig betreut
    werden. Ein kleinerer Pool waere die Obergrenze, die dann niemand sucht."""
    eng = dataclasses.replace(settings, ocr_concurrency=7)
    mittel = tasks.worker_context_resources(eng)
    assert mittel["pool"].max_size == 7
