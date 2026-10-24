"""Warteschlangen-Anbindung — offline, mit Procrastinates eigenem Testwerkzeug.

Diese Tests brauchen KEINE Datenbank. Der Daemon baut seine App mit
`tasks.build_daemon_app`, der Client seine mit `tasks.build_client_app`; beide
Bauer oeffnen nichts, und fuer das Einreihen setzt der Test Procrastinates
speicherinternen Zugang (`testing.InMemoryConnector`) ein. Das Einreihen mit
`connection=` prueft hier ein Zugang, der die uebergebene Verbindung MITSCHREIBT,
denn der speicherinterne uebergeht sie; gegen echtes Postgres steht es in
`test_integration.py`.
"""

from __future__ import annotations

import dataclasses
import inspect
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import procrastinate
import pytest
from procrastinate import testing
from procrastinate.jobs import Job
from psycopg_pool import AsyncConnectionPool

from mineru_ocr import client, config, daemon, tasks
from mineru_ocr.config import Settings
from mineru_ocr.errors import (
    CorruptDocumentError,
    InsufficientBalanceError,
    TerminalRunPodError,
    TransientRunPodError,
)

HASH = "a" * 64


class MerkenderSpeicher(testing.InMemoryConnector):
    """Der speicherinterne Zugang, der die uebergebene Verbindung festhaelt.

    Procrastinates `InMemoryConnector` nimmt `connection=` entgegen und tut
    nichts damit. Fuer die Zusicherung „das Einreihen laeuft ueber die
    Verbindung des Aufrufers" genuegt das nicht; dieser Zugang schreibt mit,
    welche Verbindung ankam.
    """

    def __init__(self) -> None:
        super().__init__()
        self.verbindungen: list[Any] = []

    def execute_query_all_with_connection(
        self, connection: Any, query: str, **arguments: Any
    ) -> list[dict[str, Any]]:
        self.verbindungen.append(connection)
        return super().execute_query_all_with_connection(
            connection, query, **arguments
        )


@pytest.fixture
def app(settings: Settings) -> procrastinate.App:
    """Die App des Daemons — frisch gebaut, ohne Zugang zu einer Datenbank."""
    return tasks.build_daemon_app(settings)


@pytest.fixture
def einreihen_app(settings: Settings) -> Iterator[procrastinate.App]:
    """Die App des Clients mit einem merkenden speicherinternen Zugang.

    Procrastinates dokumentiertes Testmuster: `replace_connector` haengt den
    speicherinternen Zugang ein und tauscht ihn am Ende zurueck. Nur der Test
    tut das; im Betrieb wird nirgends ein Zugang umgeschaltet.
    """
    with tasks.build_client_app(settings).replace_connector(
        MerkenderSpeicher()
    ) as a:
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


def test_jeder_aufruf_baut_eine_eigene_app(settings: Settings) -> None:
    """`build_daemon_app` hat keinen Zustand ausserhalb der App, die es liefert.

    Zwei Aufrufe mit verschiedenen Einstellungen ergeben zwei unabhaengige Apps,
    jede mit den Werten IHRER Einstellung. Die frueheren Fassungen teilten sich
    eine App auf Modulebene und mussten die Strategie beim zweiten Aufruf
    nachziehen — daran ist es einmal still gescheitert (der Job wartete dreissig
    Sekunden statt der eingestellten fuenfzig Millisekunden).
    """
    knapp = dataclasses.replace(settings, max_attempts=1)
    grosszuegig = dataclasses.replace(settings, max_attempts=9)
    a = tasks.build_daemon_app(knapp)
    b = tasks.build_daemon_app(grosszuegig)

    assert a is not b
    assert a.tasks[config.TASK_OCR_DOCUMENT] is not b.tasks[config.TASK_OCR_DOCUMENT]
    fehler = TransientRunPodError("x")
    assert (
        a.tasks[config.TASK_OCR_DOCUMENT].retry_strategy.get_retry_decision(  # type: ignore[union-attr]
            exception=fehler, job=_job()
        )
        is None
    )
    assert (
        b.tasks[config.TASK_OCR_DOCUMENT].retry_strategy.get_retry_decision(  # type: ignore[union-attr]
            exception=fehler, job=_job()
        )
        is not None
    )


def test_retry_strategie_und_zeitplan_haengen_an_der_app(
    app: procrastinate.App, settings: Settings
) -> None:
    """Die App des Daemons traegt die Wiederholungs-Strategie des Dokument-Tasks
    und den Zeitplan des Waechters, beide aus der uebergebenen Einstellung."""
    strategie = app.tasks[config.TASK_OCR_DOCUMENT].retry_strategy
    assert isinstance(strategie, tasks.OCRRetryStrategy)

    eintrag = next(
        e
        for e in app.periodic_registry.periodic_tasks.values()
        if e.task.name == config.TASK_RETRY_STALLED
    )
    assert eintrag.cron == settings.stalled_check_cron


def test_daemon_app_traegt_den_asynchronen_zugang(app: procrastinate.App) -> None:
    """Nur ein asynchroner Zugang kann einen Worker tragen. Der `job_manager`
    haelt dabei keine eigene, abweichende Referenz."""
    assert isinstance(app.connector, procrastinate.PsycopgConnector)
    assert app.job_manager.connector is app.connector


def test_client_app_traegt_den_synchronen_zugang(settings: Settings) -> None:
    """Der Client reiht ein und fragt ab — mit einem synchronen Zugang und ohne
    angemeldete Tasks: Er reiht per Namen ein und kennt die Funktionen nicht."""
    client_app = tasks.build_client_app(settings)
    assert isinstance(client_app.connector, procrastinate.SyncPsycopgConnector)
    assert client_app.job_manager.connector is client_app.connector
    assert config.TASK_OCR_DOCUMENT not in client_app.tasks


def test_es_gibt_weder_modulweite_app_noch_umschalten() -> None:
    """Modulweite App, `configure`, `defer_document` und der Platzhalter-Zugang
    sind weg, und nirgends im Paket wird ein Zugang zur Laufzeit getauscht.

    Geprueft an den Namen und am Quelltext der drei Module, die mit
    Procrastinate sprechen: Ein Zugang, der nachtraeglich umgehaengt wird, ist
    genau das, was dieses Paket nicht mehr tut — jeder Prozess baut seine App
    einmal und behaelt sie.
    """
    for name in ("app", "configure", "defer_document", "register_tasks"):
        assert not hasattr(tasks, name), name
    assert not hasattr(tasks, "_ZUGANG_BEIM_IMPORT")

    for modul in (tasks, client, daemon):
        quelle = inspect.getsource(modul)
        assert "replace_connector" not in quelle, modul.__name__
        assert "with_connector" not in quelle, modul.__name__
        assert ".connector =" not in quelle, modul.__name__
        assert "InMemoryConnector" not in quelle, modul.__name__


# --- Einreihen ------------------------------------------------------------


def test_einreihen_erzeugt_einen_job(einreihen_app: procrastinate.App) -> None:
    """Der Normalfall: ein Job, mit dem Hash als einzigem Argument."""
    assert tasks.enqueue_document(einreihen_app, object(), HASH) is True
    jobs = list(einreihen_app.connector.jobs.values())  # type: ignore[attr-defined]
    assert len(jobs) == 1
    assert jobs[0]["task_name"] == config.TASK_OCR_DOCUMENT
    # Der Hash ist das EINZIGE Argument. Alles Weitere steht in der Zeile — ein
    # Job-Argument waere eine Kopie, und eine Kopie kann abweichen.
    assert jobs[0]["args"] == {"sha256": HASH}


def test_job_landet_in_der_queue_die_der_daemon_liest(
    einreihen_app: procrastinate.App, app: procrastinate.App
) -> None:
    """Name und Queue des eingereihten Jobs sind die, unter denen der Daemon den
    Task angemeldet hat.

    Ohne ausdrueckliche Queue landete der Job in `default`, die der Daemon nie
    liest: kein Fehler, keine Meldung, die Jobs blieben einfach liegen. Verglichen
    wird deshalb gegen die App des Daemons und nicht gegen dieselbe Konstante ein
    zweites Mal.
    """
    tasks.enqueue_document(einreihen_app, object(), HASH)
    job = next(iter(einreihen_app.connector.jobs.values()))  # type: ignore[attr-defined]

    assert job["queue_name"] != "default"
    aufgabe = app.tasks[job["task_name"]]
    assert aufgabe.queue == job["queue_name"]
    assert job["queue_name"] == config.QUEUE_OCR


def test_einreihen_laeuft_ueber_die_uebergebene_verbindung(
    einreihen_app: procrastinate.App,
) -> None:
    """Das Einreihen benutzt die Verbindung des Aufrufers und holt sich keine aus
    dem Pool — so bleibt es bei der einen Verbindung je Client."""
    verbindung = object()
    tasks.enqueue_document(einreihen_app, verbindung, HASH)

    speicher = einreihen_app.connector
    assert isinstance(speicher, MerkenderSpeicher)
    assert speicher.verbindungen == [verbindung]


def test_zweites_einreihen_meldet_laeuft_schon(
    einreihen_app: procrastinate.App,
) -> None:
    """`AlreadyEnqueued` wird gefangen und zu False — der Normalfall beim
    wiederholten Durchlaufen eines Ordners, kein Fehler."""
    assert tasks.enqueue_document(einreihen_app, object(), HASH) is True
    assert tasks.enqueue_document(einreihen_app, object(), HASH) is False
    assert len(einreihen_app.connector.jobs) == 1  # type: ignore[attr-defined]


def test_beide_sperren_werden_gesetzt(einreihen_app: procrastinate.App) -> None:
    """`queueing_lock` UND `lock`, beide auf den Hash.

    Ohne den zweiten wuerde ein waehrend der Verarbeitung nachgeschobenes
    Duplikat angenommen und liefe parallel: dasselbe Dokument zweimal bezahlt.
    Der `queueing_lock` allein reicht nicht — er bindet nur den Zustand `todo`
    und ist frei, sobald der Job laeuft.
    """
    tasks.enqueue_document(einreihen_app, object(), HASH)
    job = next(iter(einreihen_app.connector.jobs.values()))  # type: ignore[attr-defined]
    assert job["queueing_lock"] == HASH
    assert job["lock"] == HASH


def test_kein_stapel_einreihen() -> None:
    """Es wird einzeln eingereiht. Ein Stapel mit einem einzigen Konflikt wuerde
    vollstaendig zurueckgerollt — bei dreissigtausend Dokumenten genuegte ein
    bereits wartendes, damit gar nichts passiert.

    Geprueft am Quelltext: `batch_defer` darf im Modul nicht vorkommen. Der
    Vorspann von `enqueue_document` erklaert, warum — und ein Test, der nur die
    Wirkung prueft, saehe den Unterschied nicht.
    """
    quelle = inspect.getsource(tasks)
    assert "batch_defer" not in quelle.replace("`batch_defer`", "")


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


def test_pool_prueft_verbindungen_beim_ausleihen(settings: Settings) -> None:
    """Der Pool des Daemons prueft jede Verbindung, bevor er sie herausgibt.

    Einen UEBERGEBENEN Pool prueft Procrastinate nicht selbst; ohne die Pruefung
    schriebe der Daemon nach einem Postgres-Neustart ueber eine tote Verbindung —
    und zwar mitten in dem Schreibvorgang, der das bezahlte Ergebnis ablegt. Das
    Verhalten gegen eine wirklich beendete Verbindung steht in
    `test_integration.py`.
    """
    mittel = tasks.worker_context_resources(settings)
    assert mittel["pool"]._check is AsyncConnectionPool.check_connection
