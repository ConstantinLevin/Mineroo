"""PDF lesen und schneiden — die eine Stelle mit pypdfium2."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.skip(reason="Vertrag steht, Koerper folgt in Phase B")


def test_seitenzahl_stimmt() -> None:
    """`page_count` liefert die tatsaechliche Seitenzahl."""


def test_schnitt_ist_null_basiert_und_einschliessend() -> None:
    """`extract(0, 0)` liefert genau die erste Seite; `extract(0, 2)` genau drei.

    Die Falle dahinter: pypdfium2 nimmt Seitenangaben in zwei Formen entgegen,
    die VERSCHIEDEN zaehlen — als Liste null-basiert, als Zeichenkette
    eins-basiert. Wer sie verwechselt, verschiebt jedes Dokument um eine Seite,
    und am Einzelergebnis sieht man das nicht.
    """


def test_geschnittenes_pdf_hat_die_erwartete_seitenzahl() -> None:
    """Die Nachzaehlung im Schnitt greift: Ein Ausschnitt mit weniger Seiten als
    angefordert ergibt CorruptDocumentError.

    Das ist die Antwort auf die geerbte Falle — MinerUs eigener Schnitt
    ueberspringt kaputte Seiten STILL. Wir werden laut.
    """


def test_ungueltiger_bereich_ist_programmierfehler() -> None:
    """`extract(5, 2)` und `extract(0, 999)` ergeben ValueError, keinen
    InputError — es liegt dann nicht an der Datei, sondern am Aufrufer."""


def test_schnitt_blaeht_nicht_auf() -> None:
    """Die Summe aller Einzelseiten bleibt in der Groessenordnung des Originals.

    Gemessen am 17-MB-Bescheid: Faktor 1,00. Der Test haelt die MESSUNG fest,
    nicht eine Eigenschaft des Formats — deshalb prueft er eine grosszuegige
    Schranke und nicht die exakte Zahl.
    """


def test_kaputtes_pdf_ergibt_corrupt_document_error() -> None:
    """Beschaedigte, verschluesselte und passwortgeschuetzte Dateien sind fuer
    uns dasselbe: nicht verarbeitbar, endgueltig, ohne RunPod-Kontakt."""


def test_reader_ist_nach_dem_schliessen_unbrauchbar() -> None:
    """Zugriff nach dem Verlassen des Kontext-Managers wirft."""
