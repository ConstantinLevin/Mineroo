"""Typerkennung an den Magic Bytes."""

from __future__ import annotations

import io
import struct
import time
import zipfile
from pathlib import Path

import pytest

from mineru_ocr.errors import UnsupportedTypeError
from mineru_ocr.mime import KOPF_BYTES, DocumentType, sniff_bytes, sniff_file

from ganzdokumente import (
    GANZDOKUMENTE,
    MHTML,
    OLE2_KOPF,
    gewoehnliches_zip,
    office_zip,
    zip_aus,
)


def sniff_path(pfad: Path) -> DocumentType:
    """Die Datei oeffnen und `sniff_file` reichen, wie es die Kommandozeile tut."""
    with pfad.open("rb") as datei:
        return sniff_file(datei)


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
    daten = office_zip(verzeichnis)
    assert sniff_bytes(daten) is erwartet

    datei = tmp_path / "probe.zip"
    datei.write_bytes(daten)
    assert sniff_path(datei) is erwartet


@pytest.mark.parametrize("name", list(GANZDOKUMENTE))
def test_ganzdokument_formate_werden_am_inhalt_erkannt(name: str, tmp_path: Path) -> None:
    """Die Ganzdokument-Formate, die MinerU 4 liest, laesst die Vorerkennung durch
    (Mineroo#17): jeden OLE2-Container, ZIP-Container mit dem Aufbau von
    OpenDocument, EPUB oder OFD, RTF und MHTML an ihrem Kopf, HTML an seinem
    Anfang — aus Bytes und von der Platte."""
    daten, erwartet = GANZDOKUMENTE[name]
    assert sniff_bytes(daten) is erwartet

    datei = tmp_path / "probe"
    datei.write_bytes(daten)
    assert sniff_path(datei) is erwartet


@pytest.mark.parametrize(
    "mimetype",
    [
        "application/vnd.makemusic.notation", "application/vnd.recordare.musicxml", "",
        "application/epub+zip" + " " * 256,
    ],
    ids=["finale", "musicxml", "leer", "zu-gross"],
)
def test_fremder_mimetype_ist_kein_epub_trotz_container(
    mimetype: str, tmp_path: Path
) -> None:
    """Ein vorhandener fremder Medientyp entscheidet auch gegen einen
    Container mit OPF-Verweis (Mineroo#50)."""
    daten = zip_aus({
        "mimetype": mimetype,
        "META-INF/container.xml": (
            '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="buch.opf" '
            'media-type="application/oebps-package+xml"/></rootfiles></container>'
        ),
        "buch.opf": "<package/>",
    })
    with pytest.raises(UnsupportedTypeError):
        sniff_bytes(daten)

    datei = tmp_path / "probe.epub"
    datei.write_bytes(daten)
    with pytest.raises(UnsupportedTypeError):
        sniff_path(datei)


@pytest.mark.parametrize("container", [None, "<kaputt"])
def test_epub_mimetype_genuegt_auch_ohne_lesbaren_container(
    container: str | None, tmp_path: Path
) -> None:
    """Der EPUB-Medientyp reicht; der Container ist nur der Ersatzweg."""
    eintraege = {"mimetype": "application/epub+zip"}
    if container is not None:
        eintraege["META-INF/container.xml"] = container
    daten = zip_aus(eintraege)
    assert sniff_bytes(daten) is DocumentType.EPUB

    datei = tmp_path / "probe"
    datei.write_bytes(daten)
    assert sniff_path(datei) is DocumentType.EPUB


@pytest.mark.parametrize(
    "container",
    [
        '<container><rootfiles><rootfile full-path="partitur.xml" '
        'media-type="application/vnd.recordare.musicxml+xml"/></rootfiles></container>',
        '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        '<rootfiles><rootfile full-path="partitur.xml" '
        'media-type="application/vnd.recordare.musicxml+xml"/></rootfiles></container>',
        '<container xmlns="urn:beispiel:notation"><rootfiles>'
        '<rootfile full-path="score.dat"/></rootfiles></container>',
        '<container xmlns="urn:beispiel:notation"><rootfiles>'
        '<rootfile full-path="buch.opf" media-type="application/oebps-package+xml"/>'
        '</rootfiles></container>',
        '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        '<rootfiles><rootfile media-type="application/oebps-package+xml"/>'
        '</rootfiles></container>',
        '<container/>',
        '<container',
    ],
    ids=[
        "musicxml", "musicxml-mit-namensraum", "notation", "fremder-namensraum",
        "opf-ohne-pfad", "leer", "kaputt",
    ],
)
def test_container_ohne_opf_verweis_ist_kein_epub(
    container: str, tmp_path: Path
) -> None:
    """Ohne `mimetype` reicht der Name `container.xml` nicht (Mineroo#50)."""
    daten = zip_aus({"META-INF/container.xml": container, "partitur.xml": "<score/>"})
    with pytest.raises(UnsupportedTypeError):
        sniff_bytes(daten)

    datei = tmp_path / "probe.epub"
    datei.write_bytes(daten)
    with pytest.raises(UnsupportedTypeError):
        sniff_path(datei)


@pytest.mark.parametrize(
    "daten",
    [
        # Text und Daten ohne Kennung: CSV und TSV liest das Paket nicht (Mineroo#1,
        # Out of Scope).
        b"Gebaeude;Heizbeginn;Heizende\nHalle 1;06:00;18:00\n",
        b"Gebaeude\tHeizbeginn\nHalle 1\t06:00\n",
        b'{"gebaeude": "Halle 1", "heizbeginn": "06:00"}',
        b'<?xml version="1.0"?><heizplan><halle>1</halle></heizplan>',
        b"# Heizzeiten\n\nAb dem 1. November gelten neue Zeiten.\n",
        # HTML nicht an seinem Anfang, sondern irgendwo im Text.
        b"Notiz zur Seite: <html> fehlt noch\n",
        # Ein Tag, der nur mit `html` beginnt.
        b"<htmlish>Heizzeiten</htmlish>",
        # RTF nicht an seinem Kopf, oder ohne Versionsnummer.
        b"Hallo {\\rtf1 Heizzeiten}",
        b"{\\rtf Heizzeiten}",
        b" " * 65 + b"{\\rtf1 Heizzeiten}",
        # Eine E-Mail ohne `multipart/related` ist kein MHTML.
        b"From: hausverwaltung@beispiel.invalid\r\nMIME-Version: 1.0\r\n"
        b'Content-Type: multipart/mixed; boundary="x"\r\n\r\n--x\r\n\r\nText\r\n--x--\r\n',
        # `multipart/related` ohne Grenze traegt keine Teile.
        b"MIME-Version: 1.0\r\nContent-Type: multipart/related\r\n\r\n<html></html>",
        # Die acht Kennbytes von OLE2, abgeschnitten.
        OLE2_KOPF[:7],
    ],
    ids=[
        "csv", "tsv", "json", "xml", "markdown", "html-mitten-im-text", "htmlish",
        "rtf-nicht-am-kopf", "rtf-ohne-version", "rtf-nach-zu-viel-leerraum",
        "mail-multipart-mixed", "mhtml-ohne-grenze", "ole2-abgeschnitten",
    ],
)
def test_offensichtlich_fremdes_wird_zurueckgehalten(daten: bytes, tmp_path: Path) -> None:
    """Was keine Kennung eines Formats traegt, das MinerU liest, geht nicht an
    RunPod (Mineroo#17)."""
    with pytest.raises(UnsupportedTypeError):
        sniff_bytes(daten)

    datei = tmp_path / "probe"
    datei.write_bytes(daten)
    with pytest.raises(UnsupportedTypeError):
        sniff_path(datei)


@pytest.mark.parametrize(
    "eintraege",
    [
        # OpenDocument-Zeichnung und -Formel: Aufbau von OpenDocument, aber kein
        # Typ, den MinerU liest (DocVortex 0.5.16 kennt nur ODT, ODS und ODP).
        {"mimetype": "application/vnd.oasis.opendocument.graphics"},
        {"mimetype": "application/vnd.oasis.opendocument.formula"},
        # Eine Datei namens `mimetype` mit fremdem Inhalt.
        {"mimetype": "text/plain"},
        # Verwaltungsdateien, die nur so heissen wie die eines Formats.
        {"META-INF/manifest.xml": "<kein-manifest/>"},
        {"daten/OFD.xml": "<OFD/>"},
    ],
    ids=["odg", "odf-formel", "mimetype-fremd", "manifest-ohne-wurzel", "ofd-nicht-an-der-wurzel"],
)
def test_zip_ohne_den_aufbau_eines_formats_wird_zurueckgehalten(
    eintraege: dict[str, str],
) -> None:
    """Ein ZIP geht nur durch, wenn es den Aufbau von OOXML, OpenDocument, EPUB
    oder OFD hat. Ein Teil davon genuegt nicht."""
    with pytest.raises(UnsupportedTypeError):
        sniff_bytes(zip_aus(eintraege))


def _verdorbener_eintrag(
    eintraege: dict[str, str], verdorben: str, verfahren: int = zipfile.ZIP_DEFLATED
) -> bytes:
    """Ein ZIP, in dem die gepackten Daten des Eintrags `verdorben` (mit
    `verfahren` gepackt) mit 0xFF ueberschrieben sind; Verzeichnis und Groessen
    bleiben, wie sie waren."""
    puffer = io.BytesIO()
    with zipfile.ZipFile(puffer, "w", zipfile.ZIP_DEFLATED) as archiv:
        for name, inhalt in eintraege.items():
            archiv.writestr(
                name, inhalt, compress_type=verfahren if name == verdorben else None
            )
    daten = bytearray(puffer.getvalue())
    with zipfile.ZipFile(io.BytesIO(bytes(daten))) as archiv:
        info = archiv.getinfo(verdorben)
    kopf = info.header_offset
    namenslaenge, extralaenge = struct.unpack("<HH", daten[kopf + 26 : kopf + 30])
    anfang = kopf + 30 + namenslaenge + extralaenge
    daten[anfang : anfang + info.compress_size] = b"\xff" * info.compress_size
    return bytes(daten)


def _zentralverzeichnis_aendern(daten: bytes, versatz: int, wert: bytes) -> bytes:
    """Ueberschreibt im ersten Eintrag des Zentralverzeichnisses (`PK\\x01\\x02`) die
    Bytes ab `versatz` (APPNOTE 4.3.12)."""
    anfang = daten.index(b"PK\x01\x02") + versatz
    return daten[:anfang] + wert + daten[anfang + len(wert) :]


@pytest.mark.parametrize(
    "daten",
    [
        # QA zu Mineroo#17: Der Header-Parser von CPython wirft bei einem
        # RFC-2231-Parameter, der mit `*` endet, einen `IndexError`.
        b"Content-Type:multipart/related;\xff*",
        b"MIME-Version: 1.0\r\nContent-Type: multipart/related; boundary*\r\n\r\n"
        b"<html></html>\r\n",
        # Ein Zentralverzeichnis, das `zipfile` schon beim Oeffnen nicht liest:
        # eine Version, die es nicht kennt (`NotImplementedError`), und ein als
        # UTF-8 markierter Name, der keines ist (`UnicodeDecodeError`).
        _zentralverzeichnis_aendern(office_zip("word/"), 6, b"\x63\x00"),
        _zentralverzeichnis_aendern(
            zip_aus({"a.xml": "<x/>"}).replace(b"a.xml", b"\xff.xml"), 8, b"\x00\x08"
        ),
        # Code-Review zu Mineroo#17: ein `mimetype`, dessen LZMA- oder
        # bzip2-Daten kaputt sind, und ein Manifest mit einer Kodierung, die es
        # nicht gibt (`LookupError`).
        _verdorbener_eintrag(
            {"mimetype": "application/epub+zip"}, "mimetype", zipfile.ZIP_LZMA
        ),
        _verdorbener_eintrag(
            {"mimetype": "application/epub+zip"}, "mimetype", zipfile.ZIP_BZIP2
        ),
        zip_aus(
            {"META-INF/manifest.xml": '<?xml version="1.0" encoding="gibt-es-nicht"?><m/>'}
        ),
    ],
    ids=[
        "mhtml-parameter-kurz", "mhtml-parameter", "zip-version", "zip-name-kein-utf8",
        "mimetype-lzma-kaputt", "mimetype-bzip2-kaputt", "manifest-kodierung-unbekannt",
    ],
)
def test_kaputter_kopf_ist_ein_unbekannter_typ_kein_absturz(
    daten: bytes, tmp_path: Path
) -> None:
    """Was der Header-Parser oder `zipfile` nicht lesen, ist kein Typ, den das
    Paket kennt: `UnsupportedTypeError`, sonst bricht die Kommandozeile einen
    Ordnerlauf ab und `ocr_start` eine ganze Folge."""
    with pytest.raises(UnsupportedTypeError):
        sniff_bytes(daten)
    datei = tmp_path / "probe"
    datei.write_bytes(daten)
    with pytest.raises(UnsupportedTypeError):
        sniff_path(datei)


def test_verschachtelte_kommentare_hinter_der_grenze_brechen_die_vorerkennung_nicht_ab(
    tmp_path: Path,
) -> None:
    """Code-Review zu Mineroo#17: Tausend verschachtelte Kommentare hinter der
    Grenze liessen den Kopf-Parser mit `email.policy.default` in einen
    `RecursionError` laufen. Gelesen mit `compat32` ist es ein Kopf mit Typ
    `multipart/related` und Grenze, also MHTML; ob MinerU die Datei liest,
    entscheidet der Worker (Mineroo#17)."""
    daten = (
        b"MIME-Version: 1.0\r\nContent-Type: multipart/related; boundary=x "
        + b"(" * 1100
        + b"\r\n\r\n<html></html>"
    )
    assert sniff_bytes(daten) is DocumentType.MHTML
    datei = tmp_path / "probe"
    datei.write_bytes(daten)
    assert sniff_path(datei) is DocumentType.MHTML


def _schnellste_laufzeit(daten: bytes, laeufe: int = 3) -> float:
    """Die kuerzeste von `laeufe` Laufzeiten der Vorerkennung, in Sekunden; ein
    unbekannter Typ zaehlt wie ein erkannter. Die kuerzeste, weil Last auf der
    Maschine einzelne Laeufe verlaengert, aber keinen verkuerzt."""
    zeiten = []
    for _ in range(laeufe):
        beginn = time.perf_counter()
        try:
            sniff_bytes(daten)
        except UnsupportedTypeError:
            pass
        zeiten.append(time.perf_counter() - beginn)
    return min(zeiten)


@pytest.mark.parametrize("einheit", [b";", b" (a)", b"; a=b"], ids=["semikola", "kommentare", "parameter"])
def test_ein_entgleister_mhtml_kopf_haelt_die_vorerkennung_nicht_auf(einheit: bytes) -> None:
    """Ein Kopf, dessen `Content-Type` die gelesenen 64 KiB mit Semikola,
    Kommentaren oder Parametern fuellt, wird in linearer Zeit gelesen
    (Code-Review zur Spec #1). Die Vorerkennung laeuft im Daemon synchron; der
    Kopf-Parser mit `email.policy.default` brauchte dafuer quadratische Zeit, fast
    dreissig Sekunden, und hielt alle Jobs und den Herzschlag an.

    Gemessen gegen einen gleich langen harmlosen MHTML-Kopf auf derselben
    Maschine: Eine feste Sekundengrenze hinge an Maschine und Last. Linear lagen
    die Semikola beim 26-fachen des harmlosen Kopfs, die quadratische Zeit beim
    rund 10 000-fachen."""
    kopf = b"Content-Type: multipart/related; boundary=x"
    entgleist = kopf + einheit * ((KOPF_BYTES - len(kopf)) // len(einheit))
    zeile = b"X-Kopfzeile: w\r\n"
    harmlos = kopf + b"\r\n" + zeile * ((KOPF_BYTES - len(kopf) - 2) // len(zeile))
    assert len(entgleist) > KOPF_BYTES - len(einheit) and len(harmlos) > KOPF_BYTES - len(zeile)
    assert _schnellste_laufzeit(entgleist) < 100 * _schnellste_laufzeit(harmlos)


def test_ein_unlesbares_merkmal_verdeckt_kein_anderes() -> None:
    """Laesst sich ein Eintrag nicht lesen (hier ein `mimetype` mit kaputten
    deflate-Daten), fehlt nur dieses Merkmal: Ein OOXML-Archiv mit `word/` bleibt
    ein DOCX."""
    daten = _verdorbener_eintrag(
        {"mimetype": "application/epub+zip", "word/document.xml": "<document/>"},
        "mimetype",
    )
    assert sniff_bytes(daten) is DocumentType.DOCX


def test_ganzdokument_formate_werden_nicht_am_namen_erkannt(tmp_path: Path) -> None:
    """Der Name einer Datei sagt nichts (Mineroo#17): Ein DOCX namens `.txt` ist
    ein DOCX, ein Text namens `.rtf`, `.html` oder `.odt` ist kein Dokument."""
    datei = tmp_path / "notiz.txt"
    datei.write_bytes(office_zip("word/"))
    assert sniff_path(datei) is DocumentType.DOCX

    for name in ("bericht.rtf", "seite.html", "seite.mhtml", "text.odt", "alt.doc"):
        datei = tmp_path / name
        datei.write_bytes(b"Sehr geehrte Damen und Herren,\n")
        with pytest.raises(UnsupportedTypeError):
            sniff_path(datei)


def test_die_vorerkennung_liest_so_viel_wie_die_kennungen_brauchen(tmp_path: Path) -> None:
    """Statt der frueheren 16 Bytes liest `sniff_file` so viel vom Anfang, wie die
    Kennungen brauchen: Der MHTML-Kopf nennt `multipart/related` oft erst nach
    hunderten Bytes (DocVortex liest bis 64 KiB), und RTF darf nach bis zu 64
    Leerzeichen beginnen (DocVortex prueft 128 Bytes)."""
    kopfzeilen = b"".join(
        b"X-Kopfzeile-%d: %s\r\n" % (n, b"w" * 900) for n in range(60)
    )
    weit = kopfzeilen + MHTML
    assert len(weit.split(b"multipart/related")[0]) > 50_000
    datei = tmp_path / "weit.mhtml"
    datei.write_bytes(weit)
    assert sniff_path(datei) is DocumentType.MHTML
    assert sniff_bytes(weit) is DocumentType.MHTML

    rtf = b"\xef\xbb\xbf" + b" " * 64 + b"{\\rtf1 Heizzeiten}"
    datei = tmp_path / "weit.rtf"
    datei.write_bytes(rtf)
    assert sniff_path(datei) is DocumentType.RTF


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
    daten = gewoehnliches_zip()
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


def test_meldung_nennt_die_kennbytes_nicht_den_ganzen_gelesenen_kopf(tmp_path: Path) -> None:
    """Gelesen werden bis zu 64 KiB; die Meldung nennt davon die ersten 16 Bytes
    ungekuerzt, die Kennbytes, an denen man den Typ von Hand nachschlaegt. Der
    ganze Kopf stuende sonst in `error` und auf stderr."""
    daten = b"Sehr geehrte Damen und Herren, " * 4000
    datei = tmp_path / "brief"
    datei.write_bytes(daten)
    for von_der_platte in (False, True):
        with pytest.raises(UnsupportedTypeError) as fehler:
            if von_der_platte:
                sniff_path(datei)
            else:
                sniff_bytes(daten)
        assert repr(daten[:16]) in str(fehler.value)
        assert len(str(fehler.value)) < 500


def test_sniff_bytes_braucht_die_ganzen_bytes_beim_zip() -> None:
    """Ein abgeschnittener DOCX-Puffer ergibt UnsupportedTypeError statt eines
    falschen Treffers — das Archivverzeichnis steht am ENDE der Datei.

    Und die Meldung sagt es dazu, sonst sucht man den Fehler im Archiv statt
    beim Aufrufer.
    """
    ganz = office_zip("word/")
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


def test_bilder_sind_genau_die_bildtypen() -> None:
    """`is_image` ist fuer die sechs Bildtypen wahr: Ein Bild zaehlt als eine
    Seite (Mineroo#12). PDF und die Office-Formate sind keine Bilder."""
    bilder = {
        DocumentType.PNG,
        DocumentType.JPEG,
        DocumentType.GIF,
        DocumentType.BMP,
        DocumentType.TIFF,
        DocumentType.WEBP,
    }
    for typ in DocumentType:
        assert typ.is_image is (typ in bilder), typ


def test_ganzdokument_formate_sind_alles_ausser_pdf_und_bildern() -> None:
    """MinerU 4 liest alles ausser PDF und Bildern als ganzes Dokument, ohne
    Seiten (Mineroo#1, Worker, „Dateitypen"): Office, OpenDocument, RTF, EPUB,
    OFD, HTML und MHTML."""
    for typ in DocumentType:
        assert typ.is_whole_document is not (typ.is_pdf or typ.is_image), typ
    assert {typ for typ in DocumentType if typ.is_whole_document} == {
        DocumentType.DOCX,
        DocumentType.PPTX,
        DocumentType.XLSX,
        DocumentType.OLE2,
        DocumentType.ODT,
        DocumentType.ODS,
        DocumentType.ODP,
        DocumentType.RTF,
        DocumentType.EPUB,
        DocumentType.OFD,
        DocumentType.HTML,
        DocumentType.MHTML,
    }


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
    # Die Ganzdokument-Formate aus Mineroo#17; die Zeichenketten sind die von
    # DocVortex 0.5.16 (`document/filetypes.py`, `MIME_TYPE_BY_EXTENSION`), ausser
    # fuer OLE2, das das Paket nicht weiter unterscheidet.
    assert (DocumentType.OLE2.mime, DocumentType.OLE2.suffix) == (
        "application/x-ole-storage",
        ".cfb",
    )
    assert (DocumentType.ODT.mime, DocumentType.ODT.suffix) == (
        "application/vnd.oasis.opendocument.text",
        ".odt",
    )
    assert (DocumentType.ODS.mime, DocumentType.ODS.suffix) == (
        "application/vnd.oasis.opendocument.spreadsheet",
        ".ods",
    )
    assert (DocumentType.ODP.mime, DocumentType.ODP.suffix) == (
        "application/vnd.oasis.opendocument.presentation",
        ".odp",
    )
    assert (DocumentType.RTF.mime, DocumentType.RTF.suffix) == ("application/rtf", ".rtf")
    assert (DocumentType.EPUB.mime, DocumentType.EPUB.suffix) == (
        "application/epub+zip",
        ".epub",
    )
    assert (DocumentType.OFD.mime, DocumentType.OFD.suffix) == ("application/ofd", ".ofd")
    assert (DocumentType.HTML.mime, DocumentType.HTML.suffix) == ("text/html", ".html")
    assert (DocumentType.MHTML.mime, DocumentType.MHTML.suffix) == (
        "multipart/related",
        ".mhtml",
    )


def test_endungen_sind_eindeutig() -> None:
    """Keine zwei Typen teilen sich eine Endung — sonst waere sie kein Merkmal
    des Typs mehr."""
    endungen = [typ.suffix for typ in DocumentType]
    assert len(endungen) == len(set(endungen))
