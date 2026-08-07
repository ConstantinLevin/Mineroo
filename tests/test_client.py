"""Die Fassade — Preflight, Einreihen, Warten, Nachsehen."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.skip(reason="Vertrag steht, Koerper folgt in Phase B")


# --- Preflight ------------------------------------------------------------


def test_preflight_mutiert_nichts() -> None:
    """Kein Schreibzugriff auf den Endpoint, kein Anlegen, kein Aendern."""


def test_nicht_pruefbar_ist_nicht_in_ordnung() -> None:
    """Laeuft kein Worker, sind dessen Umgebungsvariablen NICHT pruefbar — und
    der Befund sagt das, statt stillschweigend „in Ordnung" zu melden. Genau
    darin liegt der Wert dieser Pruefung."""


def test_altes_image_ist_warnung_kein_abbruch() -> None:
    """Mit altem Image kann man arbeiten, man soll es nur wissen."""


def test_rechenzentren_werden_gemeldet() -> None:
    """Die Konfiguration wird berichtet; der Client richtet kein Verhalten danach
    — das ist eine Entscheidung des Menschen und kein Schalter im Programm."""


def test_toter_daemon_wird_erkannt() -> None:
    """Das haeufigste Betriebs-Missverstaendnis: Alles sieht normal aus, nur
    arbeitet niemand. Der Client sagt es."""


def test_ocr_start_stirbt_bei_totem_daemon() -> None:
    """Einreihen ohne laufenden Exekutor bricht ab, statt Jobs anzuhaeufen, die
    niemand abholt."""


# --- Arbeit ---------------------------------------------------------------


def test_ocr_start_blockiert_nicht() -> None:
    """Es reiht ein und kehrt zurueck — „das losschicken ist einfach einen job
    erstellen"."""


def test_ocr_start_reiht_alles_ohne_endzustand_ein() -> None:
    """Auch Zeilen, deren juengster Job endgueltig endete, ohne etwas zu
    schreiben: „es wird dann ncohmal"."""


def test_einreihen_ist_unterbrechbar() -> None:
    """Mittendrin abgebrochen: Was eingereiht ist, bleibt es; beim naechsten
    Aufruf laeuft nur der Rest."""


def test_wait_gibt_bei_jedem_endzustand_zurueck() -> None:
    """Auch bei `failed` — das Scheitern ist ein Ergebnis, kein Fehler des
    Wartens. Der Aufrufer prueft `.success`."""


def test_wait_auf_unbekannten_hash_wirft_sofort() -> None:
    """Nicht erst nach Ablauf der Wartezeit. Auf etwas zu warten, das nie
    eingereiht wurde, ist immer ein Irrtum des Aufrufers."""


def test_get_braucht_keinen_pfad() -> None:
    """`get("<hash>")` liefert die Huelle samt Ergebnis, ohne die Quelldatei
    anzufassen — sie kann laengst weg sein."""


def test_retry_failed_nimmt_nur_failed() -> None:
    """`input_error` wird uebersprungen und nicht mitgezaehlt: Ein so
    wiedervorgelegter Job taete nichts, verbrauchte aber einen Versuch."""


def test_retry_failed_ueberspringt_aborted() -> None:
    """Das Tooling weist `aborted` ab; der Heilungsweg ist erneutes Einreihen.
    Uebersprungen und gemeldet, nicht mit einer Ausnahme aufgehalten."""


# --- Nachsehen ------------------------------------------------------------


def test_status_zaehlt_in_summen() -> None:
    """Zwei Abfragen, unabhaengig von der Bestandsgroesse — nicht eine je
    Dokument.

    Bei zehntausend Dokumenten waere die Einzelaufloesung zehntausend Abfragen
    fuer eine Uebersicht, die niemand Dokument fuer Dokument liest.
    """


def test_credit_wird_live_geholt() -> None:
    """Kein zwischengespeicherter Wert: Ein alter Kontostand ist genau dann
    falsch, wenn es darauf ankommt."""


def test_workers_liefert_einzeln() -> None:
    """Nicht die zusammengefassten Zaehler des Endpoints — die luegen."""


def test_settings_maskiert_den_schluessel() -> None:
    """Auch ueber `client.settings`."""
