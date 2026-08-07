"""Warteschlangen-Anbindung — offline, mit Procrastinates eigenem Testwerkzeug.

Diese Tests brauchen KEINE Datenbank. Procrastinate bringt für genau diesen
Zweck einen speicherinternen Zugang mit (`testing.InMemoryConnector`), und der
Plan hatte ihn ausdrücklich in die kostenlose Offline-Stufe gelegt. Ohne diese
Datei wäre die gesamte Anbindung nur mit laufender Instanz prüfbar.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.skip(reason="Vertrag steht, Koerper folgt in Phase B")


# --- Anmeldung ------------------------------------------------------------


def test_tasks_sind_unter_den_festgelegten_namen_angemeldet() -> None:
    """Nach `configure` tragen die Tasks genau `config.TASK_OCR_DOCUMENT` und
    `config.TASK_RETRY_STALLED`.

    Der Fall, gegen den das steht, meldet keinen Fehler: Kennen Produzent und
    Daemon verschiedene Namen, bleiben die Jobs liegen, und beide Seiten melden
    Erfolg.
    """


def test_queues_sind_getrennt() -> None:
    """Dokument-Task in `ocr`, Waechter in `maintenance`."""


def test_anmelden_ist_mehrfach_aufrufbar() -> None:
    """Ein zweiter Aufruf ist ein Nichts-Tun, kein Fehler."""


def test_produzenten_zugang_kann_nur_einreihen() -> None:
    """Mit dem synchronen Zugang schlaegt jeder andere Vorgang fehl — ein
    Produzent, der versehentlich einen Worker startet, waere ein zweiter Exekutor,
    von dem niemand weiss."""


# --- Einreihen ------------------------------------------------------------


def test_zweites_einreihen_meldet_laeuft_schon() -> None:
    """`AlreadyEnqueued` wird gefangen und zu False — der Normalfall beim
    wiederholten Durchlaufen eines Ordners, kein Fehler."""


def test_beide_sperren_werden_gesetzt() -> None:
    """`queueing_lock` UND `lock`, beide auf den Hash.

    Ohne den zweiten wuerde ein waehrend der Verarbeitung nachgeschobenes
    Duplikat angenommen und liefe parallel: dasselbe Dokument zweimal bezahlt.
    """


def test_kein_stapel_einreihen() -> None:
    """Es wird einzeln eingereiht. Ein Stapel mit einem einzigen Konflikt wuerde
    vollstaendig zurueckgerollt — bei dreissigtausend Dokumenten genuegte ein
    bereits wartendes, damit gar nichts passiert."""


def test_einreihen_ohne_konfigurierten_zugang_wirft() -> None:
    """Wer `configure` vergisst, schreibt sonst in den Arbeitsspeicher und
    wundert sich, dass nichts geschieht."""


# --- Wiederholungs-Strategie ---------------------------------------------


def test_strategie_fragt_die_gemeinsame_funktion() -> None:
    """Die Entscheidung kommt aus `errors.classify_failure`, nicht aus einer
    zweiten Regel — sonst laeuft ein Job noch einmal, waehrend die Zeile schon
    `failed` sagt."""


def test_kein_retry_ergibt_none() -> None:
    """Verweigert wird mit `return None`, nicht mit `RetryDecision(
    should_retry=False)`.

    Der Parameter steht nur in einem Beispiel der Anleitung, nicht in der
    Referenz-Signatur. Auf ihm aufzubauen braeche beim naechsten Upgrade — und
    zwar als „wird trotzdem wiederholt", also als Geld.
    """


def test_wartezeiten_wachsen_exponentiell() -> None:
    """Rund dreissig Sekunden, zwei Minuten, acht Minuten."""


def test_budget_kommt_aus_der_einstellung() -> None:
    """Die Strategie benutzt `settings.max_attempts` und nicht eine eigene Zahl."""


def test_erschoepftes_budget_ergibt_none() -> None:
    """Ab `job.attempts >= max_attempts` wird nicht mehr wiederholt."""
