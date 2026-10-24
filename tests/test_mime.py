"""Typerkennung an den Magic Bytes."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from mineru_ocr.errors import UnsupportedTypeError
from mineru_ocr.mime import DocumentType, sniff_bytes, sniff_path

# Kopfbytes je Typ, so kurz wie moeglich und so lang wie noetig. Sie stammen aus
# den Formatspezifikationen, nicht aus echten Dateien — eine echte Datei waere
# fuer diese Frage nur unnoetiger Ballast.
KOEPFE: dict[DocumentType, bytes] = {
    DocumentType.PDF: b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n",
    DocumentType.PNG: b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR",
    DocumentType.JPEG: b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01",
    DocumentType.GIF: b"GIF89a\x01\x00\x01\x00\x00\x00\x00",
    DocumentType.BMP: b"BM\x8a\x00\x00\x00\x00\x00\x00\x00\x8a\x00",
    DocumentType.TIFF: b"II*\x00\x08\x00\x00\x00\x10\x00\x00\x00",
    DocumentType.WEBP: b"RIFF\x24\x00\x00\x00WEBPVP8 ",
}


def _office_zip(verzeichnis: str) -> bytes:
    """Baut ein winziges, gueltiges Office-Archiv mit dem gegebenen Verzeichnis.

    Die Eintragsreihenfolge ist absichtlich UNGUENSTIG: Erst kommen die
    Verwaltungsdateien, das kennzeichnende Verzeichnis kommt zuletzt. Genau so
    schreibt LibreOffice, und genau daran scheitern Erkennungen, die nur die
    ersten Eintraege ansehen.
    """
    puffer = io.BytesIO()
    with zipfile.ZipFile(puffer, "w") as archiv:
        archiv.writestr("[Content_Types].xml", "<Types/>")
        archiv.writestr("_rels/.rels", "<Relationships/>")
        archiv.writestr("docProps/core.xml", "<coreProperties/>")
        archiv.writestr(f"{verzeichnis}document.xml", "<document/>")
    return puffer.getvalue()


def _gewoehnliches_zip() -> bytes:
    puffer = io.BytesIO()
    with zipfile.ZipFile(puffer, "w") as archiv:
        archiv.writestr("notizen.txt", "nichts von Belang")
    return puffer.getvalue()


# --- Erkennung ------------------------------------------------------------


@pytest.mark.parametrize("typ", list(KOEPFE))
def test_jeder_unterstuetzte_typ_wird_erkannt(typ: DocumentType, tmp_path: Path) -> None:
    """Je ein Kopf-Fixture ergibt den richtigen Typ — aus Bytes und von der
    Platte."""
    daten = KOEPFE[typ]
    assert sniff_bytes(daten) is typ

    datei = tmp_path / "probe"
    datei.write_bytes(daten)
    assert sniff_path(datei) is typ


@pytest.mark.parametrize(
    "verzeichnis, erwartet",
    [("word/", DocumentType.DOCX), ("xl/", DocumentType.XLSX), ("ppt/", DocumentType.PPTX)],
)
def test_office_archive_werden_unterschieden(
    verzeichnis: str, erwartet: DocumentType, tmp_path: Path
) -> None:
    """DOCX, XLSX und PPTX tragen dieselben vier Magic Bytes und werden am
    Zentralverzeichnis auseinandergehalten — auch wenn das kennzeichnende
    Verzeichnis der LETZTE Eintrag ist."""
    daten = _office_zip(verzeichnis)
    assert sniff_bytes(daten) is erwartet

    datei = tmp_path / "probe.zip"
    datei.write_bytes(daten)
    assert sniff_path(datei) is erwartet


def test_endung_wird_ignoriert(tmp_path: Path) -> None:
    """Eine JPEG-Datei mit der Endung .pdf wird als JPEG erkannt.

    Der Fall, gegen den die ganze Magic-Byte-Erkennung steht: In einem
    gewachsenen Bestand luegen Endungen, und wer ihnen glaubt, bezahlt fuer den
    Fehlschlag.
    """
    datei = tmp_path / "gescannter_bescheid.pdf"
    datei.write_bytes(KOEPFE[DocumentType.JPEG])
    assert sniff_path(datei) is DocumentType.JPEG


# --- Abweisung ------------------------------------------------------------


def test_zip_ohne_office_struktur_wird_abgewiesen(tmp_path: Path) -> None:
    """Ein gewoehnliches ZIP hat dieselben vier Magic Bytes wie DOCX und muss
    trotzdem UnsupportedTypeError ergeben."""
    daten = _gewoehnliches_zip()
    with pytest.raises(UnsupportedTypeError, match="Office"):
        sniff_bytes(daten)

    datei = tmp_path / "irgendwas.zip"
    datei.write_bytes(daten)
    with pytest.raises(UnsupportedTypeError):
        sniff_path(datei)


def test_leere_datei_wird_abgewiesen(tmp_path: Path) -> None:
    """Null Bytes ergeben UnsupportedTypeError, keinen Absturz."""
    datei = tmp_path / "leer"
    datei.write_bytes(b"")
    with pytest.raises(UnsupportedTypeError):
        sniff_path(datei)
    with pytest.raises(UnsupportedTypeError):
        sniff_bytes(b"")


def test_zu_kurze_datei_wird_abgewiesen(tmp_path: Path) -> None:
    """Weniger Bytes als die Magic Bytes verlangen ergibt UnsupportedTypeError."""
    datei = tmp_path / "kurz"
    datei.write_bytes(b"%P")
    with pytest.raises(UnsupportedTypeError):
        sniff_path(datei)


def test_unbekannte_magic_bytes_werden_abgewiesen() -> None:
    """Ein Textdokument ist kein unterstuetzter Typ."""
    with pytest.raises(UnsupportedTypeError):
        sniff_bytes(b"Sehr geehrte Damen und Herren,\n")


def test_meldung_traegt_die_vollen_kopfbytes() -> None:
    """Die Fehlermeldung zeigt die gelesenen Bytes UNGEKUERZT.

    Wer sie abschneidet, nimmt genau die Angabe weg, mit der man den Typ von
    Hand nachschlaegt.
    """
    daten = b"\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0a\x0b\x0c\x0d\x0e\x0f\x10"
    with pytest.raises(UnsupportedTypeError) as fehler:
        sniff_bytes(daten)
    assert repr(daten) in str(fehler.value)


def test_sniff_bytes_braucht_die_ganzen_bytes_beim_zip() -> None:
    """Ein abgeschnittener DOCX-Puffer ergibt UnsupportedTypeError statt eines
    falschen Treffers — das Archivverzeichnis steht am ENDE der Datei.

    Und die Meldung sagt es dazu, sonst sucht man den Fehler im Archiv statt
    beim Aufrufer.
    """
    ganz = _office_zip("word/")
    haelfte = ganz[: len(ganz) // 2]
    with pytest.raises(UnsupportedTypeError, match="unvollstaendig"):
        sniff_bytes(haelfte)


def test_leserechte_werden_nicht_als_typfehler_ausgegeben(tmp_path: Path) -> None:
    """Eine nicht vorhandene Datei ergibt OSError, nicht UnsupportedTypeError.

    Die Verwechslung wuerde die Fehlersuche in die falsche Richtung schicken.
    """
    with pytest.raises(OSError):
        sniff_path(tmp_path / "gibtesnicht.pdf")


# --- Die Tabelle ----------------------------------------------------------


def test_nur_pdf_ist_zerlegbar() -> None:
    """`is_pdf` ist genau fuer PDF wahr. Alles andere geht ungeteilt hinaus
    oder gar nicht."""
    for typ in DocumentType:
        assert typ.is_pdf is (typ is DocumentType.PDF)


def test_jeder_typ_hat_mime_und_endung() -> None:
    """Die Tabelle ist vollstaendig — ein Typ ohne Eintrag flaege erst beim
    Anlegen einer Zeile auf."""
    for typ in DocumentType:
        assert typ.mime and "/" in typ.mime
        assert typ.suffix.startswith(".")


def test_die_tabelle_ist_festgeschrieben() -> None:
    """Die Werte sind dauerhaft und werden hier woertlich festgehalten.

    Die MIME-Zeichenkette landet unveraenderlich in `documents.mime`, und der Job
    findet ueber sie den Typ zur Zeile wieder. Wer sie spaeter aendert, laesst
    alle bestehenden Zeilen ins Leere zeigen — dieser Test macht die Aenderung
    sichtbar, statt sie durchrutschen zu lassen.
    """
    assert (DocumentType.PDF.mime, DocumentType.PDF.suffix) == ("application/pdf", ".pdf")
    assert (DocumentType.PNG.mime, DocumentType.PNG.suffix) == ("image/png", ".png")
    assert (DocumentType.JPEG.mime, DocumentType.JPEG.suffix) == ("image/jpeg", ".jpg")
    assert (DocumentType.GIF.mime, DocumentType.GIF.suffix) == ("image/gif", ".gif")
    assert (DocumentType.BMP.mime, DocumentType.BMP.suffix) == ("image/bmp", ".bmp")
    assert (DocumentType.TIFF.mime, DocumentType.TIFF.suffix) == ("image/tiff", ".tif")
    assert (DocumentType.WEBP.mime, DocumentType.WEBP.suffix) == ("image/webp", ".webp")
    assert DocumentType.DOCX.suffix == ".docx"
    assert DocumentType.XLSX.suffix == ".xlsx"
    assert DocumentType.PPTX.suffix == ".pptx"


def test_endungen_sind_eindeutig() -> None:
    """Keine zwei Typen teilen sich eine Endung — sonst waere sie kein Merkmal
    des Typs mehr."""
    endungen = [typ.suffix for typ in DocumentType]
    assert len(endungen) == len(set(endungen))
