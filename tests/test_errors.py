"""Die eine Entscheidung: wiederholen, aufgeben oder warten.

Diese Tests sind die wichtigsten des Pakets, weil an dieser Funktion zwei
Aufrufer hängen, die niemals verschieden antworten dürfen — sonst läuft ein
Job noch einmal, während die Zeile schon `failed` sagt.
"""

from __future__ import annotations

import asyncio

import httpx
import psycopg
import pytest

from mineru_ocr.errors import (
    ConfigDriftError,
    ConfigurationError,
    CorruptDocumentError,
    DatabaseError,
    DocumentNotFound,
    FailureKind,
    IncompleteResultError,
    InputError,
    InsufficientBalanceError,
    JobAufgegebenError,
    MineruOcrError,
    ResultLostError,
    TerminalOverwriteError,
    TerminalRunPodError,
    TooLargeError,
    TransientRunPodError,
    UnstorableResultError,
    UnsupportedTypeError,
    classify_failure,
)


def _alle_unterklassen(wurzel: type) -> list[type]:
    """Alle Nachfahren einer Klasse, die Wurzel eingeschlossen."""
    gefunden = [wurzel]
    for kind in wurzel.__subclasses__():
        gefunden.extend(_alle_unterklassen(kind))
    return gefunden


def test_transportfehler_ist_wiederholbar() -> None:
    """httpx-Zeitueberschreitung und Transportfehler ergeben RETRYABLE."""
    assert classify_failure(httpx.ReadTimeout("zu lange")) is FailureKind.RETRYABLE
    assert classify_failure(httpx.ConnectError("weg")) is FailureKind.RETRYABLE
    assert classify_failure(httpx.WriteTimeout("upload")) is FailureKind.RETRYABLE


def test_serverfehler_ist_wiederholbar() -> None:
    """HTTP 5xx, verpackt als TransientRunPodError, ergibt RETRYABLE."""
    assert classify_failure(TransientRunPodError("502")) is FailureKind.RETRYABLE


def test_zeitueberschreitung_des_jobs_ist_wiederholbar() -> None:
    """Der Job-Endzustand TIMED_OUT ergibt RETRYABLE — es kann an der
    Warteschlange gelegen haben."""
    assert classify_failure(TransientRunPodError("TIMED_OUT")) is FailureKind.RETRYABLE


def test_verlorenes_ergebnis_ist_wiederholbar() -> None:
    """ResultLostError (404 nach Ablauf der Aufbewahrung) ergibt RETRYABLE.

    Ein verpasstes Abholfenster ist kein Grund, ein Dokument endgueltig
    aufzugeben — es wird neu geschickt. Dass der Fall ueberhaupt eintritt, ist ein
    Fehler unserer Orchestrierung, und dagegen ist der Executor gebaut, nicht die
    Klassifikation.
    """
    assert classify_failure(ResultLostError("404")) is FailureKind.RETRYABLE


def test_eingabefehler_ist_endgueltig() -> None:
    """Jede InputError-Unterklasse ergibt TERMINAL."""
    for fehler in (
        InputError("allgemein"),
        UnsupportedTypeError("kein PDF"),
        CorruptDocumentError("kaputt"),
        TooLargeError("zu gross"),
    ):
        assert classify_failure(fehler) is FailureKind.TERMINAL, type(fehler)


def test_schluesselfehler_ist_endgueltig() -> None:
    """ConfigurationError ergibt TERMINAL — Wiederholen macht einen ungueltigen
    Schluessel nicht gueltig."""
    assert classify_failure(ConfigurationError("kein Schluessel")) is FailureKind.TERMINAL
    assert classify_failure(ConfigDriftError("Drift")) is FailureKind.TERMINAL


def test_leeres_konto_ist_weder_noch() -> None:
    """InsufficientBalanceError ergibt BALANCE_PAUSE, nicht RETRYABLE.

    Waere es RETRYABLE, verbrauchte jede Wartezeit einen der drei echten
    Versuche und der Versuchszaehler waere kein ehrliches Mass mehr.
    """
    assert classify_failure(InsufficientBalanceError("402")) is FailureKind.BALANCE_PAUSE


def test_kontostand_schlaegt_die_oberklasse() -> None:
    """InsufficientBalanceError IST ein RunPodError — die Reihenfolge der
    Pruefungen muss den Sonderfall zuerst nehmen.

    Der Test steht da, weil das Umstellen zweier Zeilen die Antwort aendert,
    ohne dass eine Bedingung sich aendert: Aus „warten" wuerde „aufgeben".
    """
    from mineru_ocr.errors import RunPodError

    assert issubclass(InsufficientBalanceError, RunPodError)
    assert classify_failure(InsufficientBalanceError("402")) is FailureKind.BALANCE_PAUSE


def test_unbekannte_ausnahme_ist_endgueltig() -> None:
    """Eine voellig fremde Ausnahme ergibt TERMINAL, nicht RETRYABLE.

    Unbekannt heisst mit hoher Wahrscheinlichkeit „unser eigener Fehler", und
    den dreimal an jedem Dokument des Bestands zu bezahlen waere die teuerste
    denkbare Voreinstellung.
    """
    assert classify_failure(ValueError("Tippfehler")) is FailureKind.TERMINAL
    assert classify_failure(KeyError("falscher Feldname")) is FailureKind.TERMINAL
    assert classify_failure(AttributeError("None")) is FailureKind.TERMINAL


def test_abbruch_beim_herunterfahren_ist_wiederholbar() -> None:
    """Ein `asyncio.CancelledError` ergibt RETRYABLE, nicht „Unbekanntes".

    Procrastinate fragt die Retry-Strategie auch dann, wenn das Herunterfahren
    einen Job abbricht, und reiht ihn wieder ein, wenn sie ja sagt. Das ist die
    Regel „jeder Job, der nicht neu eingereiht wird, schreibt ein Ergebnis"
    (Mineroo#27): Ohne diese Einstufung endete jeder beim Herunterfahren
    abgebrochene Job `aborted`, und sein Dokument bliebe ohne Ergebnis liegen.
    `CancelledError` erbt von `BaseException`; die Einstufung haengt also nicht
    an `Exception`.
    """
    from mineru_ocr.errors import should_run_again

    assert not issubclass(asyncio.CancelledError, Exception)
    assert classify_failure(asyncio.CancelledError()) is FailureKind.RETRYABLE
    assert should_run_again(asyncio.CancelledError())


def test_datenbankfehler_ist_wiederholbar() -> None:
    """psycopg-Fehler und DatabaseError ergeben RETRYABLE — die Datenbank ist
    lokal, ein Aussetzer ist fast immer voruebergehend."""
    assert classify_failure(DatabaseError("Aussetzer")) is FailureKind.RETRYABLE
    assert classify_failure(psycopg.OperationalError("weg")) is FailureKind.RETRYABLE


def test_geloeschte_zeile_ist_endgueltig() -> None:
    """DocumentNotFound ist ein DatabaseError und trotzdem TERMINAL: Dreimal
    nachzusehen bringt eine geloeschte Zeile nicht zurueck."""
    assert issubclass(DocumentNotFound, DatabaseError)
    assert classify_failure(DocumentNotFound("weg")) is FailureKind.TERMINAL


def test_ueberschreibversuch_ist_endgueltig() -> None:
    """TerminalOverwriteError ebenso — ein Programmierfehler wird durch
    Wiederholen nur teurer."""
    assert issubclass(TerminalOverwriteError, DatabaseError)
    assert classify_failure(TerminalOverwriteError("fertig")) is FailureKind.TERMINAL


def test_nicht_speicherbares_ergebnis_ist_endgueltig() -> None:
    """UnstorableResultError ist ein DatabaseError und trotzdem TERMINAL.

    Postgres hat das Ergebnis wegen seines Inhalts abgewiesen (etwa ein U+0000
    im Text oder eine Groesse ueber der Grenze von jsonb). Derselbe Inhalt wird
    beim naechsten Lauf wieder abgewiesen; wiederholt hiesse nur, ihn dreimal zu
    bezahlen und am Ende ohne Ergebnis dazustehen (Mineroo#27).
    """
    from mineru_ocr.errors import should_run_again

    assert issubclass(UnstorableResultError, DatabaseError)
    assert classify_failure(UnstorableResultError("U+0000")) is FailureKind.TERMINAL
    assert not should_run_again(UnstorableResultError("U+0000"))


def test_unvollstaendiges_ergebnis_ist_endgueltig() -> None:
    """IncompleteResultError ist ebenfalls ein Programmierfehler."""
    assert classify_failure(IncompleteResultError("halb")) is FailureKind.TERMINAL


def test_terminaler_engine_fehler_ist_endgueltig() -> None:
    """TerminalRunPodError ergibt TERMINAL."""
    assert classify_failure(TerminalRunPodError("FAILED")) is FailureKind.TERMINAL


def test_klassifikation_ist_total() -> None:
    """Fuer JEDE Ausnahme des Pakets kommt genau eine der drei Antworten heraus.

    Laeuft ueber alle Unterklassen von MineruOcrError. Ein Pfad ohne Rueckgabe
    waere ein Loch, das genau dann auffaellt, wenn schon etwas schiefgegangen
    ist.
    """
    klassen = _alle_unterklassen(MineruOcrError)
    assert len(klassen) >= 16, f"Erwartet mindestens 16 Klassen, gefunden {len(klassen)}"
    for klasse in klassen:
        antwort = classify_failure(klasse("Probe"))
        assert isinstance(antwort, FailureKind), klasse


def test_klassifikation_ist_deterministisch() -> None:
    """Dieselbe Ausnahme ergibt bei wiederholtem Aufruf dieselbe Antwort."""
    fehler = TransientRunPodError("502")
    antworten = {classify_failure(fehler) for _ in range(50)}
    assert antworten == {FailureKind.RETRYABLE}


def test_klassifikation_hat_keine_seiteneffekte() -> None:
    """Rein: kein I/O, kein Zustand — dieselbe Antwort auch fuer frische
    Ausnahmen derselben Klasse."""
    assert classify_failure(TooLargeError("a")) is classify_failure(TooLargeError("b"))


@pytest.mark.parametrize(
    "fehler, erwartet",
    [
        (ResultLostError("404"), FailureKind.RETRYABLE),
        (JobAufgegebenError("Zeitbudget"), FailureKind.RETRYABLE),
        (InsufficientBalanceError("402"), FailureKind.BALANCE_PAUSE),
        (DocumentNotFound("weg"), FailureKind.TERMINAL),
        (UnstorableResultError("U+0000"), FailureKind.TERMINAL),
        (UnsupportedTypeError("zip"), FailureKind.TERMINAL),
        (httpx.ConnectTimeout("weg"), FailureKind.RETRYABLE),
        (asyncio.CancelledError(), FailureKind.RETRYABLE),
    ],
)
def test_die_heiklen_faelle_einzeln(fehler: BaseException, erwartet: FailureKind) -> None:
    """Die fuenf Faelle, bei denen eine Verwechslung unmittelbar Geld kostet."""
    assert classify_failure(fehler) is erwartet


def test_zwei_gruppen_aus_drei_einstufungen() -> None:
    """`should_run_again` bildet die EINE Gruppierung fuer beide Aufrufer.

    Der Fall, gegen den das steht, ist waehrend des Bauens wirklich passiert:
    Der Ausnahme-Pfad des Executors behandelte `BALANCE_PAUSE` als „werfen,
    nichts schreiben"; die Wiederholungs-Strategie prueft anfangs nur auf
    `RETRYABLE` und haette denselben Fehler als endgueltig verbucht. Ergebnis
    waere ein Dokument gewesen, das wegen eines LEEREN KONTOS aufgegeben wird,
    obwohl mit ihm nichts ist — und dessen Zeile dabei auf `new` stehen bleibt,
    weil der Executor nichts geschrieben hatte.

    Die Einstufung war an beiden Stellen dieselbe. Die Gruppierung war es nicht.
    Deshalb gibt es fuer die Gruppierung eine eigene Funktion und diesen Test.
    """
    from mineru_ocr.errors import should_run_again

    noch_einmal: list[BaseException] = [
        TransientRunPodError("Gegenseite hustet"),
        ResultLostError("30 Minuten vorbei"),
        JobAufgegebenError("Zeitbudget abgelaufen, Abbruch ging hinaus"),
        InsufficientBalanceError("402"),
        DatabaseError("Postgres kurz weg"),
        asyncio.CancelledError(),
    ]
    for fehler in noch_einmal:
        assert should_run_again(fehler), type(fehler).__name__

    aufgeben: list[Exception] = [
        TerminalRunPodError("Modell abgestuerzt"),
        CorruptDocumentError("PDF kaputt"),
        TooLargeError("Seite zu gross"),
        ConfigurationError("Schluessel taugt nicht"),
        DocumentNotFound("keine Zeile"),
        IncompleteResultError("Teile fehlen"),
    ]
    for fehler in aufgeben:
        assert not should_run_again(fehler), type(fehler).__name__


def test_gruppierung_deckt_jede_einstufung_ab() -> None:
    """Jede der drei Einstufungen faellt in genau eine Gruppe.

    Kaeme eine vierte Einstufung dazu, faellt sie hier auf — statt still in die
    Gruppe „noch einmal" zu rutschen, weil sie eben nicht `TERMINAL` ist. Ein
    neuer Fehlerfall, der dreimal an jedem Dokument des Bestands bezahlt wird,
    ist die teuerste Art, das zu bemerken.
    """
    assert set(FailureKind) == {
        FailureKind.RETRYABLE,
        FailureKind.TERMINAL,
        FailureKind.BALANCE_PAUSE,
    }
