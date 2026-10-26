"""Erfundene Muster der Ganzdokument-Formate (Mineroo#17), so klein, wie die
Vorerkennung sie braucht.

Fuer `test_mime.py` (die Erkennung) und `test_executor.py` (der Weg durch den
Job). Woran MinerU die Formate erkennt, steht in DocVortex 0.5.16,
`document/detection.py` und `document/filetypes.py`. Nichts davon stammt aus
echten Dateien.
"""

from __future__ import annotations

import io
import zipfile

from mineru_ocr.mime import DocumentType


def office_zip(verzeichnis: str) -> bytes:
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


def gewoehnliches_zip() -> bytes:
    puffer = io.BytesIO()
    with zipfile.ZipFile(puffer, "w") as archiv:
        archiv.writestr("notizen.txt", "nichts von Belang")
    return puffer.getvalue()


def zip_aus(eintraege: dict[str, str]) -> bytes:
    """Ein ZIP mit genau diesen Eintraegen, in dieser Reihenfolge."""
    puffer = io.BytesIO()
    with zipfile.ZipFile(puffer, "w", zipfile.ZIP_DEFLATED) as archiv:
        for name, inhalt in eintraege.items():
            archiv.writestr(name, inhalt)
    return puffer.getvalue()


def _opendocument(mimetype: str) -> bytes:
    """Ein OpenDocument-Paket, wie die Spezifikation (ODF 1.2, Teil 3, 3.3) es
    verlangt: `mimetype` als erster Eintrag, ungepackt."""
    puffer = io.BytesIO()
    with zipfile.ZipFile(puffer, "w") as archiv:
        archiv.writestr("mimetype", mimetype, compress_type=zipfile.ZIP_STORED)
        archiv.writestr("content.xml", "<office:document-content/>")
        archiv.writestr("META-INF/manifest.xml", "<manifest:manifest/>")
    return puffer.getvalue()


_MANIFEST = (
    '<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:'
    'manifest:1.0"><manifest:file-entry manifest:full-path="/" '
    'manifest:media-type="{typ}"/></manifest:manifest>'
)

_CONTAINER = (
    '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" '
    'version="1.0"><rootfiles><rootfile full-path="OEBPS/inhalt.opf" '
    'media-type="application/oebps-package+xml"/></rootfiles></container>'
)

OLE2_KOPF = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 504
"""Der Kopfsektor eines OLE2-Containers (Compound File Binary, MS-CFB 2.2): die
acht Kennbytes, der Rest leer. Mehr braucht die Vorerkennung nicht."""

MHTML = (
    b"From: <Gespeichert von einem Browser>\r\n"
    b"Snapshot-Content-Location: https://beispiel.invalid/hausmitteilung\r\n"
    b"Subject: Hausmitteilung\r\n"
    b"MIME-Version: 1.0\r\n"
    b'Content-Type: multipart/related;\r\n\ttype="text/html";\r\n'
    b'\tboundary="----Grenze-1234"\r\n\r\n'
    b"------Grenze-1234\r\nContent-Type: text/html\r\n\r\n"
    b"<html><body><p>Heizzeiten</p></body></html>\r\n"
    b"------Grenze-1234--\r\n"
)
"""Eine MHTML-Datei, wie ein Browser sie speichert (RFC 2557): ein Kopf nach
RFC 5322 mit `multipart/related` und einer Grenze."""

GANZDOKUMENTE: dict[str, tuple[bytes, DocumentType]] = {
    "ole2": (OLE2_KOPF, DocumentType.OLE2),
    "odt": (_opendocument("application/vnd.oasis.opendocument.text"), DocumentType.ODT),
    "ods": (
        _opendocument("application/vnd.oasis.opendocument.spreadsheet"),
        DocumentType.ODS,
    ),
    "odp": (
        _opendocument("application/vnd.oasis.opendocument.presentation"),
        DocumentType.ODP,
    ),
    "odt-nur-manifest": (
        zip_aus(
            {
                "content.xml": "<office:document-content/>",
                "META-INF/manifest.xml": _MANIFEST.format(
                    typ="application/vnd.oasis.opendocument.text"
                ),
            }
        ),
        DocumentType.ODT,
    ),
    "epub": (
        zip_aus(
            {
                "mimetype": "application/epub+zip",
                "META-INF/container.xml": _CONTAINER,
                "OEBPS/inhalt.opf": "<package/>",
            }
        ),
        DocumentType.EPUB,
    ),
    "epub-nur-container": (
        zip_aus({"META-INF/container.xml": _CONTAINER, "OEBPS/inhalt.opf": "<package/>"}),
        DocumentType.EPUB,
    ),
    "ofd": (
        zip_aus({"OFD.xml": '<ofd:OFD xmlns:ofd="http://www.ofdspec.org/2016"/>'}),
        DocumentType.OFD,
    ),
    "rtf": (b"{\\rtf1\\ansi\\deff0 {\\fonttbl{\\f0 Arial;}}Heizzeiten\\par}", DocumentType.RTF),
    "rtf-mit-bom-und-leerraum": (
        b"\xef\xbb\xbf" + b" \r\n" * 21 + b"\t{\\rtf1 Heizzeiten}",
        DocumentType.RTF,
    ),
    "mhtml": (MHTML, DocumentType.MHTML),
    "html-doctype": (
        b'<!DOCTYPE html>\n<html lang="de"><body>Heizzeiten</body></html>',
        DocumentType.HTML,
    ),
    "html-doctype-klein": (b"<!doctype HTML><p>Heizzeiten</p>", DocumentType.HTML),
    "html-ohne-doctype": (b"<html><body>Heizzeiten</body></html>", DocumentType.HTML),
    "html-mit-bom-und-leerraum": (
        b"\xef\xbb\xbf\n\n  <HTML lang=de><body>Heizzeiten</body></HTML>",
        DocumentType.HTML,
    ),
}
"""Je Ganzdokument-Format ein erfundenes Muster, so klein, wie die Vorerkennung es
braucht. Woran MinerU sie erkennt, steht in DocVortex 0.5.16,
`document/detection.py` und `document/filetypes.py`."""


JE_TYP: dict[DocumentType, bytes] = {
    DocumentType.DOCX: office_zip("word/"),
    DocumentType.XLSX: office_zip("xl/"),
    DocumentType.PPTX: office_zip("ppt/"),
    **{typ: daten for daten, typ in GANZDOKUMENTE.values()},
}
"""Je Ganzdokument-Format ein Muster (das letzte aus `GANZDOKUMENTE` je Typ),
fuer Tests, die jeden Typ einmal durch den Job schicken."""
