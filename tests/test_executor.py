"""Der Ablauf an einem Dokument — ohne Warteschlange, ohne Netz.

Dass es diese Datei gibt, ist der Existenzgrund von `executor.py`: Das Modul
weiß nichts von Procrastinate, damit genau diese Tests ohne laufende
Warteschlange möglich sind. Fehlten sie, wäre die Trennung umsonst gewesen.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.skip(reason="Vertrag steht, Koerper folgt in Phase B")


# --- Der Guard ------------------------------------------------------------


def test_guard_haelt_fertige_dokumente_an() -> None:
    """Bei `done`, `empty` und `input_error` kehrt der Ablauf sofort zurueck und
    schickt NICHTS ab — sonst waere ein Wiederlauf nach dem finalen Schreiben ein
    vollstaendig neu bezahltes Dokument."""


def test_guard_laesst_failed_durch() -> None:
    """Bei `failed` wird wirklich neu verarbeitet.

    Der Gegentest zum vorigen, und der wichtigere: Stuende `failed` im Guard,
    waere jeder Retry von Hand ein Nichts-Tun, das als Erfolg verbucht wird — und
    ein gescheitertes Dokument koennte nie wieder laufen.
    """


# --- Eingang --------------------------------------------------------------


def test_veraenderte_datei_wird_erkannt() -> None:
    """Weicht der Hash beim Verarbeiten ab, endet das Dokument `input_error` mit
    Vermerk — und es wird nichts abgeschickt."""


def test_nicht_pdf_laeuft_als_ein_teilstueck() -> None:
    """Ein Bild ergibt genau einen Chunk `(0, 0)`; es gibt keinen Sonderfall
    „war nicht geschnitten"."""


# --- Absenden und Abholen -------------------------------------------------


def test_hoechstens_ein_ergebnis_offen() -> None:
    """Zu keinem Zeitpunkt sind je Dokument zwei Ergebnisse unabgeholt.

    Die Zusicherung gegen das verpasste Abholfenster: nie absenden-und-spaeter-
    einsammeln. Genau das machten die alten Ad-hoc-Skripte anders.
    """


def test_ergebnis_wird_sofort_abgeholt() -> None:
    """Zwischen terminalem Zustand und Abholung liegt hoechstens ein
    Abfragetakt."""


def test_failed_wird_in_terminalen_fehler_uebersetzt() -> None:
    """Ein Chunk-Endzustand `FAILED` ergibt `TerminalRunPodError`."""


def test_cancelled_wird_in_terminalen_fehler_uebersetzt() -> None:
    """`CANCELLED` ebenso — endgueltig, nicht wiederholbar."""


def test_timed_out_ist_wiederholbar() -> None:
    """`TIMED_OUT` ergibt `TransientRunPodError`: Es kann an der Warteschlange
    gelegen haben."""


def test_ausgepackt_wird_nur_erfolgreiches() -> None:
    """`unpack_response` sieht ausschliesslich `COMPLETED`-Antworten — die
    Uebersetzung liegt davor."""


# --- Schreiben ------------------------------------------------------------


def test_seitenabdeckung_wird_nachgerechnet() -> None:
    """Vor dem Schreiben laeuft `verify_page_coverage` ueber die TATSAECHLICH
    gelieferten Teilstuecke; bei Abweichung `input_error`.

    Nicht ueber den Plan: `iter_chunks` darf unterwegs halbieren, geliefert wird
    also moeglicherweise etwas anderes als geplant.
    """


def test_genau_ein_schreibvorgang() -> None:
    """Zwischen Anlage und Endzustand wird die Zeile genau einmal beschrieben."""


def test_ergebnis_ist_zusammengesetzt() -> None:
    """Was geschrieben wird, ist EIN Dokument: Text in Reihenfolge, Seitenzahlen
    des Originals, Bilder vereinigt. Am Ergebnis ist nicht ablesbar, ob
    geschnitten wurde."""


def test_image_tag_wird_mitgeschrieben() -> None:
    """`image_tag_at_submit` ist nach dem Lauf gesetzt.

    Ohne diesen Weg bliebe die Spalte fuer immer leer — und die Frage, fuer die
    sie da ist, waere nach Wochen nicht mehr entscheidbar.
    """


def test_wiederholbarer_fehler_laesst_die_zeile_unberuehrt() -> None:
    """Geworfen, nichts geschrieben — die Warteschlange legt neu vor."""


def test_endgueltiger_fehler_schreibt_vor_dem_werfen() -> None:
    """Zeile `failed` samt dem, was bis dahin ankam, DANN die Ausnahme. So tragen
    Job-Endzustand und Zeile dieselbe Wahrheit."""


# --- Geld -----------------------------------------------------------------


def test_leeres_konto_wartet_ohne_zu_zaehlen() -> None:
    """Der Versuchszaehler bleibt unberuehrt; nach dem Aufladen laeuft es
    weiter."""


def test_gescheiterte_kontoabfrage_beendet_das_warten_nicht() -> None:
    """Ein Netzwackeln ist kein „Konto in Ordnung" — sonst liefe man in eine Wand
    aus Ablehnungen, jede ein bezahlter Fehlversuch."""


# --- Verbindungen ---------------------------------------------------------


def test_keine_verbindung_waehrend_des_wartens() -> None:
    """Waehrend der Abfrageschleife haelt der Ablauf keine Datenbankverbindung.

    Sonst blockierten vierundzwanzig gleichzeitig betreute Dokumente den Pool bis
    zu fuenfundzwanzig Minuten, waehrend keines von ihnen ihn benutzt.
    """


def test_abbruch_wird_durchgereicht() -> None:
    """`CancelledError` wird nicht gefangen: Der Job endet `aborted`, die Zeile
    bleibt `new`, das Dokument laeuft beim naechsten Einreihen von vorn."""
