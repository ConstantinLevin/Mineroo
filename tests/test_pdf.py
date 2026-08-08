"""PDF lesen und schneiden — die eine Stelle mit pypdfium2."""

from __future__ import annotations

import io
from pathlib import Path

import pypdfium2
import pytest

from mineru_ocr.errors import CorruptDocumentError
from mineru_ocr.pdf import PdfReader


def _pdf_mit_seiten(seiten: int, breite: float = 200.0, hoehe: float = 300.0) -> bytes:
    """Baut ein PDF mit der gewuenschten Seitenzahl. Rein synthetisch."""
    doc = pypdfium2.PdfDocument.new()
    try:
        for _ in range(seiten):
            doc.new_page(breite, hoehe)
        puffer = io.BytesIO()
        doc.save(puffer)
        return puffer.getvalue()
    finally:
        doc.close()


@pytest.fixture
def pdf_zehn(tmp_path: Path) -> Path:
    datei = tmp_path / "zehn_seiten.pdf"
    datei.write_bytes(_pdf_mit_seiten(10))
    return datei


# --- Lesen ----------------------------------------------------------------


def test_seitenzahl_stimmt(pdf_zehn: Path) -> None:
    """`page_count` liefert die tatsaechliche Seitenzahl."""
    with PdfReader(pdf_zehn) as leser:
        assert leser.page_count == 10


def test_kaputtes_pdf_ergibt_corrupt_document_error(tmp_path: Path) -> None:
    """Beschaedigte, verschluesselte und passwortgeschuetzte Dateien sind fuer
    uns dasselbe: nicht verarbeitbar, endgueltig, ohne RunPod-Kontakt."""
    datei = tmp_path / "kaputt.pdf"
    datei.write_bytes(b"%PDF-1.7\nund dann nur noch Unsinn\n")
    with pytest.raises(CorruptDocumentError):
        PdfReader(datei)


def test_reader_ist_nach_dem_schliessen_unbrauchbar(pdf_zehn: Path) -> None:
    """Zugriff nach dem Verlassen des Kontext-Managers wirft."""
    with PdfReader(pdf_zehn) as leser:
        assert leser.page_count == 10
    with pytest.raises(Exception):
        leser.page_count


# --- Schneiden ------------------------------------------------------------


def test_schnitt_ist_null_basiert_und_einschliessend(pdf_zehn: Path) -> None:
    """`extract(0, 0)` liefert genau die erste Seite; `extract(0, 2)` genau drei.

    Die Falle dahinter: pypdfium2 nimmt Seitenangaben in zwei Formen entgegen,
    die VERSCHIEDEN zaehlen — als Liste null-basiert, als Zeichenkette
    eins-basiert. Wer sie verwechselt, verschiebt jedes Dokument um eine Seite,
    und am Einzelergebnis sieht man das nicht.
    """
    with PdfReader(pdf_zehn) as leser:
        for erste, letzte, erwartet in [(0, 0, 1), (0, 2, 3), (3, 3, 1), (0, 9, 10), (7, 9, 3)]:
            ausschnitt = leser.extract(erste, letzte)
            geschnitten = pypdfium2.PdfDocument(ausschnitt)
            try:
                assert len(geschnitten) == erwartet, (erste, letzte)
            finally:
                geschnitten.close()


def test_ausschnitt_ist_ein_lesbares_pdf(pdf_zehn: Path) -> None:
    """Das Ergebnis ist ein vollstaendiges, fuer sich lesbares Dokument — nicht
    ein Bruchstueck, das nur im Zusammenhang funktioniert."""
    with PdfReader(pdf_zehn) as leser:
        ausschnitt = leser.extract(2, 4)
    assert ausschnitt.startswith(b"%PDF-")
    geoeffnet = pypdfium2.PdfDocument(ausschnitt)
    try:
        assert len(geoeffnet) == 3
    finally:
        geoeffnet.close()


def test_ausschnitt_laesst_sich_erneut_oeffnen(pdf_zehn: Path, tmp_path: Path) -> None:
    """Der Ausschnitt laesst sich von der Platte erneut oeffnen und hat dieselbe
    Seitenzahl — der Weg, den die Engine mit ihm gehen wird."""
    with PdfReader(pdf_zehn) as leser:
        ausschnitt = leser.extract(2, 4)
    ziel = tmp_path / "ausschnitt.pdf"
    ziel.write_bytes(ausschnitt)
    with PdfReader(ziel) as erneut:
        assert erneut.page_count == 3


def test_geschnittenes_pdf_hat_die_erwartete_seitenzahl(
    pdf_zehn: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Die Nachzaehlung im Schnitt greift: Ein Ausschnitt mit weniger Seiten als
    angefordert ergibt CorruptDocumentError.

    Das ist die Antwort auf die geerbte Falle — MinerUs eigener Schnitt
    ueberspringt kaputte Seiten STILL. Wir werden laut.

    Geprueft wird, indem das Uebernehmen der Seiten voruebergehend zu wenige
    liefert. Ein echtes PDF mit einer kaputten Seite waere das ehrlichere
    Material, aber eines herzustellen hiesse, die Beschaedigung zu erfinden, die
    pypdfium2 gerade ueberspringt — und ob man dieselbe trifft, weiss man nicht.
    Der Zaehl-Vergleich ist die Zusicherung, und genau der wird hier geprueft.
    """
    echtes_import = pypdfium2.PdfDocument.import_pages

    def zu_wenige(selbst, quelle, pages=None, index=None):  # type: ignore[no-untyped-def]
        gekuerzt = list(pages)[:-1] if pages else pages
        return echtes_import(selbst, quelle, pages=gekuerzt, index=index)

    monkeypatch.setattr(pypdfium2.PdfDocument, "import_pages", zu_wenige)

    with PdfReader(pdf_zehn) as leser:
        with pytest.raises(CorruptDocumentError, match="statt"):
            leser.extract(0, 2)


def test_ungueltiger_bereich_ist_programmierfehler(pdf_zehn: Path) -> None:
    """`extract(5, 2)` und `extract(0, 999)` ergeben ValueError, keinen
    InputError — es liegt dann nicht an der Datei, sondern am Aufrufer."""
    with PdfReader(pdf_zehn) as leser:
        for erste, letzte in [(5, 2), (0, 999), (-1, 3), (10, 10)]:
            with pytest.raises(ValueError):
                leser.extract(erste, letzte)


def test_meldung_nennt_die_zaehlweise(pdf_zehn: Path) -> None:
    """Die Fehlermeldung sagt, wie gezaehlt wird — sonst raet der naechste Leser,
    ob er sich um eins vertan hat oder um die Grenze."""
    with PdfReader(pdf_zehn) as leser:
        with pytest.raises(ValueError) as fehler:
            leser.extract(0, 10)
    text = str(fehler.value)
    assert "null-basiert" in text
    assert "einschliessend" in text


def test_schnitt_blaeht_nicht_auf(pdf_zehn: Path) -> None:
    """Die Summe aller Einzelseiten bleibt in der Groessenordnung des Originals.

    Gemessen am 17-MB-Bescheid: Faktor 1,00. Der Test haelt die MESSUNG fest,
    nicht eine Eigenschaft des Formats — deshalb prueft er eine grosszuegige
    Schranke und nicht die exakte Zahl. Er faengt den Fall, gegen den pypdf im
    ganzen Projekt verboten ist: dort wurden aus 360 MB Quelle 4,1 GB
    Einzelseiten.
    """
    original = pdf_zehn.stat().st_size
    with PdfReader(pdf_zehn) as leser:
        summe = sum(len(leser.extract(seite, seite)) for seite in range(leser.page_count))
    assert summe < original * 4, (
        f"Einzelseiten-Summe {summe} gegen Original {original} — der Faktor "
        f"{summe / original:.2f} deutet auf inkrementelles Speichern hin."
    )
