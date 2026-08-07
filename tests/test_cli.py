"""Die Kommandozeile — Exit-Codes und die Regel, dass sie nur übersetzt.

Die Exit-Codes sind im Vertrag ausdrücklich Zusage, weil in einer Shell darauf
verzweigt wird. Eine Zusage ohne Test ist eine Absichtserklärung.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.skip(reason="Vertrag steht, Koerper folgt in Phase B")


# --- Exit-Codes -----------------------------------------------------------


def test_erfolg_ist_null() -> None:
    """Jedes Kommando gibt bei Erfolg 0."""


def test_fertiges_dokument_gibt_null() -> None:
    """`wait` und `markdown` geben 0 bei `done` und bei `empty`."""


def test_gescheitertes_dokument_gibt_drei() -> None:
    """`wait` und `markdown` geben 3 bei `failed` und `input_error`.

    Ein eigener Code, weil das kein Fehler DES WERKZEUGS ist. Wer beides auf 1
    wirft, kann in einer Schleife nicht mehr zwischen „kaputtes Dokument" und
    „kaputte Einrichtung" unterscheiden.
    """


def test_betriebsfehler_gibt_eins() -> None:
    """Endpoint nicht erreichbar, Datenbank weg: 1."""


def test_falsches_argument_gibt_zwei() -> None:
    """Unbekannter Pfad, fehlendes Argument: 2 (von typer gesetzt)."""


def test_markdown_gibt_bei_misserfolg_nichts_aus() -> None:
    """Kein halber Text auf der Standardausgabe — er saehe vollstaendig aus."""


# --- Keine zweite Wahrheit ------------------------------------------------


def test_strict_ist_ueberall_gleich_voreingestellt() -> None:
    """`mineru-ocr preflight` und `OCRClient()` verhalten sich gleich.

    Der Test gegen genau das, was der Vorspann der Datei verbietet: dieselbe
    Pruefung ueber die Kommandozeile nachsichtiger als aus Python heraus.
    """


def test_kein_kommando_rechnet_selbst() -> None:
    """Jedes Kommando ruft genau eine oeffentliche Funktion und formatiert deren
    Rueckgabe."""


def test_es_gibt_kein_kommando_das_mutiert() -> None:
    """Keines legt eine bezahlte Ressource an oder aendert eine — nicht
    abgeschaltet, sondern nicht vorhanden."""


# --- Ausgabe --------------------------------------------------------------


def test_json_ist_maschinenlesbar() -> None:
    """`--json` gibt gueltiges JSON und sonst nichts auf die Standardausgabe."""


def test_json_traegt_kein_raw() -> None:
    """Auch hier nicht — zweistellige Megabyte je Dokument."""


def test_schluessel_erscheint_in_keiner_ausgabe() -> None:
    """Weder im Bericht, noch in `--json`, noch in einer Fehlermeldung."""


def test_traceback_ist_schlicht() -> None:
    """`pretty_exceptions_enable=False`: kein reich formatierter Traceback mit
    lokalen Variablen, unter denen eine Einstellung stehen koennte."""


# --- Namen ----------------------------------------------------------------


def test_kommandonamen_folgen_der_python_flaeche() -> None:
    """`ocr`, `status`, `wait` heissen wie ihre Methoden. Zwei Vokabulare fuer
    dieselbe Sache waeren dieselbe Doppelung wie zwei Wahrheiten im Verhalten."""


def test_hash_und_pfad_werden_gleich_aufgeloest() -> None:
    """Vierundsechzig Zeichen aus `[0-9a-f]` sind ein Hash, alles andere ein
    Pfad — in jedem Kommando dieselbe Regel."""
