"""Typerkennung am Inhalt — nie an der Dateiendung.

Warum nicht an der Endung: Eine Endung ist eine Behauptung des Dateisystems, kein
Befund ueber den Inhalt. In einem gewachsenen Dokumentenbestand liegen `.pdf`, die
JPEG sind, und `.doc`, die in Wahrheit DOCX sind. Wer der Endung glaubt, schickt
eine Datei los, die die Engine nicht lesen kann, und bezahlt fuer den Fehlschlag.

Die Erkennung ist eine GROBE VORERKENNUNG (Mineroo#17; Mineroo#1, Paket,
„Dateitypen"): Welcher Typ eine Datei ist, entscheidet maßgeblich MinerUs eigene
Erkennung im Worker (DocVortex 0.5.16, `document/detection.py`,
`guess_suffix_by_bytes`). Das Paket laesst nur durch, was die Kennung eines Formats
traegt, das MinerU 4 liest, damit offensichtlich Fremdes (Text, CSV, ein
beliebiges ZIP) nicht an RunPod geht. Weist der Worker eine so durchgelassene
Datei dennoch ab, endet sie `input_error` (`errors.ist_dateiabweisung`).

Durch laesst sie, neben PDF und den Bildern:

* jeden OLE2-Container (darunter DOC, XLS, PPT), an seinen acht Kennbytes;
* ZIP-Container mit dem Aufbau von OOXML (DOCX, XLSX, PPTX), OpenDocument (ODT,
  ODS, ODP), EPUB oder OFD;
* RTF und MHTML an ihrem Kopf, HTML an seinem Anfang (`<!DOCTYPE html` oder
  `<html`).

ZIP braucht Zusatzarbeit: Alle diese Container tragen dieselben vier Magic Bytes
wie jede beliebige andere ZIP-Datei (`PK\\x03\\x04`). Ein ZIP ohne den Aufbau
eines dieser Formate muss abgewiesen werden, statt es durchzureichen und zu
hoffen — deshalb schaut die Erkennung in diesem Fall in das Archiv hinein.

CSV und TSV laesst sie nicht durch: Sie tragen keine Kennung im Inhalt (Mineroo#1,
Out of Scope).
"""

from __future__ import annotations

import codecs
import email.parser
import email.policy
import enum
import io
import re
import zipfile
import zlib
from collections.abc import Callable, Iterator
from typing import BinaryIO, Final
from xml.etree import ElementTree

from .errors import CorruptDocumentError, UnsupportedTypeError

KOPF_BYTES: Final = 65536
"""So viele Bytes liest die Erkennung am Dateianfang: so viele, wie die Kennungen
brauchen. MHTML ist der laengste Fall: Sein Kopf nennt `multipart/related` oft erst
nach vielen Kopfzeilen, und DocVortex liest dafuer bis 64 KiB
(`document/filetypes.py`, `has_mhtml_header`). RTF darf nach bis zu 64
Leerzeichen beginnen und wird in den ersten 128 Bytes gesucht (`RTF_KOPF_BYTES`),
PDF und HTML koennen nach Leerraum beginnen, PDF auch nach mehreren UTF-8-BOMs.
Die festen Bildkennungen stehen in den ersten zwoelf Bytes (WebP: `RIFF`, vier
Bytes Groesse, `WEBP`). Die Grenze gilt auch fuer den PDF-Rettungshinweis."""

RTF_KOPF_BYTES: Final = 128
"""In so vielen Bytes am Anfang sucht die Erkennung den Kopf einer RTF-Datei, wie
DocVortex (`detection.py`, `guess_suffix_by_bytes`: `file_bytes[:128]`)."""

MELDUNG_BYTES: Final = 16
"""So viele Bytes vom Anfang nennt die Meldung bei einem unbekannten Typ,
ungekuerzt: die Kennbytes, an denen man einen Typ von Hand nachschlaegt. Nicht den
ganzen gelesenen Kopf, der bis zu `KOPF_BYTES` gross ist und in `error` und auf
stderr landete."""

MP4_ANFANGSBOXEN: Final = (b"ftyp", b"free", b"skip")
"""MP4 kann vor ftyp freie Boxen tragen; der Inhalt wird danach geprueft (#76)."""

_OLE2_KENNUNG: Final = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
"""Die acht Kennbytes eines OLE2-Containers (Compound File Binary, MS-CFB 2.2).
DocVortex unterscheidet darin DOC, XLS und PPT an den Namen der Datenstroeme; die
Vorerkennung laesst jeden Container durch, und was MinerU darin nicht liest, weist
der Worker ab."""

_RTF_KOPF: Final = re.compile(
    rb"\A(?:\xef\xbb\xbf)?[ \t\r\n]{0,64}\{\\rtf(?=[0-9])", re.IGNORECASE
)
"""Der Kopf einer RTF-Datei, woertlich wie DocVortex 0.5.16
(`document/filetypes.py`, `_RTF_HEADER_RE`): eine BOM, bis zu 64 Leerzeichen,
dann `{\\rtf` mit einer Versionsnummer. Eine Datei, die mit anderem Text beginnt,
ist kein RTF."""

_HTML_ANFANG: Final = re.compile(
    rb"\A(?:\xef\xbb\xbf)?\s*<(?:!doctype\s+html|html)(?=[\s>])", re.IGNORECASE
)
"""Der Anfang einer HTML-Datei: `<!DOCTYPE html` oder `<html`, gross oder klein,
nach einer BOM und Leerraum. MinerU erkennt HTML an keiner Kennung, sondern mit
seinem Inhaltsklassifizierer (Magika); das Paket laesst nur durch, was so
beginnt, und kein Text, in dem irgendwo ein Tag steht."""

_PDF_ANFANG: Final = re.compile(rb"\A(?:[ \t\r\n\f\v]|\xef\xbb\xbf)*%PDF-")
"""Nur ASCII-Leerraum und vollstaendige UTF-8-BOMs duerfen vor PDF stehen (#56).
Kein Suchen nach der Kennung hinter fremden Bytes; der Kopf bleibt begrenzt."""

_OOXML_VERZEICHNISSE: Final = {
    "word/": "DOCX",
    "xl/": "XLSX",
    "ppt/": "PPTX",
}
"""Woran ein OOXML-Archiv von einem beliebigen ZIP zu unterscheiden ist.

Gesucht wird im ZENTRALVERZEICHNIS des Archivs, nicht in den ersten Bytes. Der
Unterschied ist nicht theoretisch: Von LibreOffice geschriebene Office-Dateien
ordnen ihre Eintraege anders an, und wer nur die ersten paar lokalen Kopfbereiche
ansieht — wie es verbreitete Erkennungs-Bibliotheken tun —, weist ein gueltiges
DOCX als nicht unterstuetzt ab. Das Zentralverzeichnis kennt alle Eintraege,
unabhaengig von ihrer Reihenfolge."""

_OPENDOCUMENT_TYPEN: Final = {
    "application/vnd.oasis.opendocument.text": "ODT",
    "application/vnd.oasis.opendocument.spreadsheet": "ODS",
    "application/vnd.oasis.opendocument.presentation": "ODP",
}
"""Die OpenDocument-Typen, die MinerU 4 liest, nach dem Medientyp des Pakets
(DocVortex 0.5.16, `detection.py`, `ODF_MIMETYPE_SUFFIXES`). Eine Zeichnung oder
Formel hat den Aufbau von OpenDocument, liest MinerU aber nicht."""

_EPUB_TYP: Final = "application/epub+zip"
_CONTAINER_NS: Final = "urn:oasis:names:tc:opendocument:xmlns:container"
_MANIFEST_NS: Final = "urn:oasis:names:tc:opendocument:xmlns:manifest:1.0"

_KLEINER_EINTRAG: Final = 256
"""So gross darf die Datei `mimetype` eines Pakets hoechstens sein, damit die
Erkennung sie liest (DocVortex: 256). Sie traegt nur den Medientyp; eine grosse
waere kein Paket, sondern womoeglich eine ZIP-Bombe."""

_GROSSER_EINTRAG: Final = 1024 * 1024
"""So gross darf ein XML-Eintrag (OpenDocument-Manifest oder EPUB-Container)
hoechstens sein, damit die Erkennung ihn liest (DocVortex: 1 MiB)."""

_ZIP_LESEFEHLER: Final = (
    zipfile.BadZipFile,
    zlib.error,
    EOFError,
    NotImplementedError,
    RuntimeError,
    ValueError,
    LookupError,
    ElementTree.ParseError,
)
"""Was `zipfile` beim Oeffnen eines Archivs und beim Lesen eines Eintrags werfen
kann, wenn der INHALT nicht stimmt: ein beschaedigtes oder verschluesseltes
Archiv, eine Version oder ein Kompressionsverfahren, das `zipfile` nicht kennt
(`NotImplementedError`), ein als UTF-8 markierter Name, der keines ist
(`UnicodeDecodeError`, ein `ValueError`), unlesbares XML, auch eines mit einer
Kodierung, die es nicht gibt (`LookupError`). Fuer die Vorerkennung
heisst jedes davon nur: Dieses Merkmal traegt das Archiv nicht, oder es ist kein
lesbares ZIP. `OSError` steht bewusst nicht hier: Beim Lesen von der Platte ist
er ein Problem der Datei, kein Typproblem (`sniff_file`)."""



class DocumentType(enum.Enum):
    """Die unterstuetzten Eingabearten. Was hier nicht steht, wird abgewiesen.

    Die Liste deckt, was MinerU 4 liest (Job-Vertrag des Forks, `api.mdx`; Mineroo#1,
    Worker, „Dateitypen"), ausser JP2 und dem, was keine Kennung im Inhalt traegt
    (CSV, TSV). Ein Typ dazuzunehmen heisst, ihn vorher an der echten Engine
    geprueft zu haben — nicht, hier eine Zeile einzufuegen.
    """

    PDF = "pdf"
    PNG = "png"
    JPEG = "jpeg"
    GIF = "gif"
    BMP = "bmp"
    TIFF = "tiff"
    WEBP = "webp"
    DOCX = "docx"
    PPTX = "pptx"
    XLSX = "xlsx"
    OLE2 = "ole2"
    """Ein OLE2-Container: DOC, XLS oder PPT. Welches davon, entscheidet der Worker."""
    ODT = "odt"
    ODS = "ods"
    ODP = "odp"
    RTF = "rtf"
    EPUB = "epub"
    OFD = "ofd"
    HTML = "html"
    MHTML = "mhtml"
    MP3 = "mp3"
    M4A = "m4a"
    AAC = "aac"
    WAV = "wav"
    FLAC = "flac"
    OGG = "ogg"
    AIFF = "aiff"

    @property
    def is_audio(self) -> bool:
        """Ob die Datei ungeteilt an ElevenLabs statt an MinerU geht (#70)."""
        return self in _AUDIO

    @property
    def is_pdf(self) -> bool:
        """Ob dieser Typ seitenweise zerlegt werden kann.

        Nur PDF kann das. Bilder gehen ungeteilt hinaus (ein Bild je Datei; die
        mit mehreren Bildern prueft `can_hold_several_images`), Ganzdokument-Formate
        (`is_whole_document`) sind nicht seitenweise zerlegbar — beide gehen
        ungeteilt hinaus oder gar nicht (Plan 6).

        Der Name sagt „ist PDF", die Frage lautet „ist zerlegbar". Heute ist das
        dasselbe. Kaeme je ein zweites zerlegbares Format dazu, ist DIESE Stelle
        zu aendern und nicht die Aufrufer.
        """
        return self is DocumentType.PDF

    @property
    def is_image(self) -> bool:
        """Ob dieser Typ eine Bilddatei ist.

        Ein Bild zaehlt als eine Seite: Nach seinem Teilstueck prueft das Paket,
        ob MinerU genau eine Seite verarbeitet hat (Mineroo#12). Mehrbildige
        Dateien kommen gar nicht so weit (`can_hold_several_images`).
        """
        return self in _BILDER

    @property
    def is_whole_document(self) -> bool:
        """Ob MinerU 4 diesen Typ als ganzes Dokument liest, ohne Seiten.

        Die OCR-Formate ausser PDF und Bildern (Mineroo#1, „Dateitypen": „local Flash
        and whole-document parsing"). Fuer diese Ganzdokument-Formate gelten die
        Ausnahmen aus Mineroo#17: Sie gehen ungeteilt als ein Stueck hinaus, unter
        der Groessengrenze einer Anfrage; der Seitenvergleich entfaellt, denn sie
        haben keine Seitenzahl; und die Lese-Einstellungen wirken nicht, der
        Worker liest sie immer in `flash`. Gespeichert werden fuer sie deshalb
        keine, und es gibt keinen Vermerk ueber abweichende.
        """
        return not (self.is_pdf or self.is_image or self.is_audio)

    @property
    def can_hold_several_images(self) -> bool:
        """Ob dieser Typ eine Bilddatei ist, die eine Folge von Seiten tragen kann.

        TIFF (mehrseitiges Fax), GIF, WebP und PNG (animiert). JPEG und BMP
        gehoeren nicht dazu: Ein JPEG mit einem zweiten Bild (Multi-Picture-Format,
        von Kameras und Telefonen fuer Vorschau oder Tiefenkarte geschrieben) ist
        ein Foto, keine Seitenfolge, und Pillow zaehlte dort trotzdem zwei Frames.
        Ganzdokument-Formate gehoeren auch nicht dazu, auch wenn sie Bilder
        enthalten: Sie sind ein Dokument, keine Folge von Bildern.
        """
        return self in _BILDFOLGEN

    @property
    def mime(self) -> str:
        """Die MIME-Zeichenkette, wie sie in `documents.mime` landet."""
        return _TABELLE[self][0]

    @property
    def suffix(self) -> str:
        """Die Endung mit fuehrendem Punkt."""
        return _TABELLE[self][1]


_AUDIO: Final = frozenset({
    DocumentType.MP3, DocumentType.M4A, DocumentType.AAC, DocumentType.WAV,
    DocumentType.FLAC, DocumentType.OGG, DocumentType.AIFF,
})

_BILDER: Final[frozenset[DocumentType]] = frozenset(
    {
        DocumentType.PNG,
        DocumentType.JPEG,
        DocumentType.GIF,
        DocumentType.BMP,
        DocumentType.TIFF,
        DocumentType.WEBP,
    }
)

_BILDFOLGEN: Final[frozenset[DocumentType]] = frozenset(
    {
        DocumentType.PNG,
        DocumentType.GIF,
        DocumentType.TIFF,
        DocumentType.WEBP,
    }
)

_TABELLE: Final[dict[DocumentType, tuple[str, str]]] = {
    DocumentType.MP3: ("audio/mpeg", ".mp3"),
    DocumentType.M4A: ("audio/mp4", ".m4a"),
    DocumentType.AAC: ("audio/aac", ".aac"),
    DocumentType.WAV: ("audio/wav", ".wav"),
    DocumentType.FLAC: ("audio/flac", ".flac"),
    DocumentType.OGG: ("audio/ogg", ".ogg"),
    DocumentType.AIFF: ("audio/aiff", ".aiff"),
    DocumentType.PDF: ("application/pdf", ".pdf"),
    DocumentType.PNG: ("image/png", ".png"),
    DocumentType.JPEG: ("image/jpeg", ".jpg"),
    DocumentType.GIF: ("image/gif", ".gif"),
    DocumentType.BMP: ("image/bmp", ".bmp"),
    DocumentType.TIFF: ("image/tiff", ".tif"),
    DocumentType.WEBP: ("image/webp", ".webp"),
    DocumentType.DOCX: (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".docx",
    ),
    DocumentType.PPTX: (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".pptx",
    ),
    DocumentType.XLSX: (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xlsx",
    ),
    DocumentType.OLE2: ("application/x-ole-storage", ".cfb"),
    DocumentType.ODT: ("application/vnd.oasis.opendocument.text", ".odt"),
    DocumentType.ODS: ("application/vnd.oasis.opendocument.spreadsheet", ".ods"),
    DocumentType.ODP: ("application/vnd.oasis.opendocument.presentation", ".odp"),
    DocumentType.RTF: ("application/rtf", ".rtf"),
    DocumentType.EPUB: ("application/epub+zip", ".epub"),
    DocumentType.OFD: ("application/ofd", ".ofd"),
    DocumentType.HTML: ("text/html", ".html"),
    DocumentType.MHTML: ("multipart/related", ".mhtml"),
}
"""MIME-Zeichenkette und Endung je Typ. DIESE WERTE SIND DAUERHAFT.

Warum das keine Kleinigkeit ist: Die MIME-Zeichenkette landet unveraenderlich in
`documents.mime`; der Job bestimmt sie aus den Bytes (`sniff_bytes`) und schreibt
sie mit dem Endergebnis. Wer hier spaeter eine MIME-Zeichenkette aendert, laesst
alte und neue Zeilen denselben Typ verschieden nennen, ohne dass etwas auffaellt.

Wo es zwei uebliche Schreibweisen gibt, ist die kuerzere gewaehlt (`.jpg`,
`.tif`) — die Wahl ist beliebig, ihre Bestaendigkeit nicht. Die Zeichenketten der
Ganzdokument-Formate sind die von DocVortex 0.5.16 (`document/filetypes.py`,
`MIME_TYPE_BY_EXTENSION`). Fuer OLE2 nicht: Das Paket weiss nur, dass es ein
OLE2-Container ist, und nennt ihn so (`application/x-ole-storage`, `.cfb` fuer
Compound File Binary), statt DOC, XLS oder PPT zu raten."""


_KLEINE_VERFAHREN: Final = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})
"""Die Kompressionsverfahren, deren Eintraege die Erkennung liest. Ein OpenDocument-
und ein EPUB-Paket legen `mimetype` ungepackt ab (ODF 1.2, Teil 3, 3.3; EPUB OCF
3.3, 4.2.5), DocVortex liest `OFD.xml` nur ungepackt oder mit deflate
(`detect_ofd_path`). bzip2 und LZMA laeuft die Erkennung deshalb nicht an: Ihre
Dekoder werfen bei kaputten Daten `OSError` und `LZMAError`, und ein `OSError`
hiesse in `sniff_file` sonst, die Datei sei nicht lesbar (Code-Review zu
Mineroo#17)."""


def _klein_lesen(archiv: zipfile.ZipFile, name: str, grenze: int) -> bytes | None:
    """Ein Eintrag des Archivs, wenn es ihn gibt, er hoechstens `grenze` Bytes hat
    und ungepackt oder mit deflate gepackt ist (`_KLEINE_VERFAHREN`); sonst None.
    Gelesen wird hoechstens `grenze + 1` Bytes, auch wenn das Verzeichnis eine
    falsche Groesse angibt."""
    try:
        eintrag = archiv.getinfo(name)
    except KeyError:
        return None
    if eintrag.file_size > grenze or eintrag.compress_type not in _KLEINE_VERFAHREN:
        return None
    with archiv.open(eintrag) as strom:
        daten = strom.read(grenze + 1)
    return daten if len(daten) <= grenze else None


def _ist_ofd(archiv: zipfile.ZipFile) -> bool:
    """OFD: `OFD.xml` an der Wurzel des Archivs (GB/T 33190, 7.1; DocVortex
    `analyzers/native/ofd/package.py`, `detect_ofd_path`)."""
    return "OFD.xml" in archiv.namelist()


def _ist_epub(archiv: zipfile.ZipFile) -> bool:
    """EPUB am Medientyp; nur ohne `mimetype` am OPF-Verweis im Container.

    Ein fremder Medientyp schliesst EPUB aus: Auch Notensatz-Archive tragen
    `META-INF/container.xml` (Mineroo#50; EPUB OCF 3.3, 4.2).
    """
    mimetype = _klein_lesen(archiv, "mimetype", _KLEINER_EINTRAG)
    if "mimetype" in archiv.namelist():
        return mimetype is not None and mimetype.strip() == _EPUB_TYP.encode("ascii")
    container = _klein_lesen(archiv, "META-INF/container.xml", _GROSSER_EINTRAG)
    if container is None:
        return False
    return any(
        eintrag.get("media-type") == "application/oebps-package+xml"
        and bool(eintrag.get("full-path"))
        for eintrag in ElementTree.fromstring(container).iter(f"{{{_CONTAINER_NS}}}rootfile")
    )


def _opendocument_typ(archiv: zipfile.ZipFile) -> DocumentType | None:
    """ODT, ODS oder ODP am Medientyp des Pakets: in `mimetype` oder, fehlt die, im
    Manifest am Eintrag fuer die Wurzel (ODF 1.2, Teil 3, 3.3 und 4.3; DocVortex
    `detection.py`, `_guess_odf_suffix_from_zip`)."""
    mimetype = _klein_lesen(archiv, "mimetype", _KLEINER_EINTRAG)
    if mimetype is not None:
        typ = _OPENDOCUMENT_TYPEN.get(mimetype.decode("ascii", errors="replace").strip())
        return DocumentType[typ] if typ is not None else None
    manifest = _klein_lesen(archiv, "META-INF/manifest.xml", _GROSSER_EINTRAG)
    if manifest is None:
        return None
    for eintrag in ElementTree.fromstring(manifest).iter(f"{{{_MANIFEST_NS}}}file-entry"):
        if eintrag.get(f"{{{_MANIFEST_NS}}}full-path") == "/":
            typ = _OPENDOCUMENT_TYPEN.get(
                eintrag.get(f"{{{_MANIFEST_NS}}}media-type", "").strip()
            )
            return DocumentType[typ] if typ is not None else None
    return None


def _merkmal[T](pruefung: Callable[[zipfile.ZipFile], T], archiv: zipfile.ZipFile) -> T | None:
    """Eine Pruefung am Archiv; ein Lesefehler heisst: Merkmal nicht vorhanden."""
    try:
        return pruefung(archiv)
    except _ZIP_LESEFEHLER:
        return None


def _typ_aus_zip(archiv: zipfile.ZipFile) -> DocumentType:
    """Entscheidet, welches Format ein ZIP traegt — oder weist es ab.

    In der Reihenfolge von DocVortex (`guess_suffix_by_bytes`): OFD, EPUB, OOXML,
    OpenDocument.
    """
    if _merkmal(_ist_ofd, archiv):
        return DocumentType.OFD
    if _merkmal(_ist_epub, archiv):
        return DocumentType.EPUB
    for name in archiv.namelist():
        for praefix, typ in _OOXML_VERZEICHNISSE.items():
            if name.startswith(praefix):
                return DocumentType[typ]
    opendocument = _merkmal(_opendocument_typ, archiv)
    if opendocument is not None:
        return opendocument
    raise UnsupportedTypeError(
        "ZIP-Archiv ohne den Aufbau eines Formats, das MinerU liest: weder Office "
        "(word/, xl/ oder ppt/ im Zentralverzeichnis) noch OpenDocument (Text, "
        "Tabelle, Praesentation), EPUB oder OFD — nicht unterstuetzt."
    )


def _ist_mhtml(kopf: bytes) -> bool:
    """MHTML: ein Kopf nach RFC 5322 mit `multipart/related` und einer Grenze
    (RFC 2557), in den ersten `KOPF_BYTES`. Wie DocVortex 0.5.16
    (`document/filetypes.py`, `has_mhtml_header`); eine E-Mail mit anderen Teilen
    ist kein MHTML."""
    if b"multipart/related" not in kopf.lower():
        return False
    try:
        # `compat32` und nicht `email.policy.default`: Dessen Kopf-Parser braucht
        # fuer einen `Content-Type`, der die 64 KiB mit Semikola, Kommentaren
        # oder Parametern fuellt, quadratische Zeit (gemessen fast dreissig
        # Sekunden), und die Vorerkennung laeuft im Daemon synchron (Code-Review
        # zur Spec #1). `compat32` liest Typ und Grenze in linearer Zeit.
        nachricht = email.parser.BytesHeaderParser(policy=email.policy.compat32).parsebytes(
            kopf
        )
        return (
            nachricht.get_content_type() == "multipart/related"
            and bool(nachricht.get_boundary())
        )
    except Exception:  # noqa: BLE001 — jeder Fehler des Parsers heisst: kein MHTML
        # Der Header-Parser der Standardbibliothek wirft bei kaputten Koepfen
        # nicht nur seinen `HeaderParseError`: Mit `email.policy.default` lief
        # CPython 3.12 bei einem RFC-2231-Parameter, der mit `*` endet, in einen
        # `IndexError`, bei tausend verschachtelten Kommentaren in einen
        # `RecursionError` (QA und Code-Review zu Mineroo#17); `compat32` liest
        # beides, aber kein Parser verspricht, nie anders zu werfen. Der Aufruf
        # liest nur `kopf` im Speicher, es gibt keine Ein- und Ausgabe, deren
        # Fehler hier verschluckt wuerde.
        return False


def _mp4_boxen(file: BinaryIO, anfang: int, ende: int) -> Iterator[tuple[bytes, int, int]]:
    """ISO-BMFF-Boxen lesen, Nutzdaten ueberspringen; Grenzen gelten je Elternbox.

    0 bedeutet bis Dateiende, 1 kuendigt eine 64-Bit-Groesse an (ISO/IEC
    14496-12, Box). Keine Suche nach Kennungen innerhalb beliebiger Nutzdaten.
    """
    while anfang < ende:
        file.seek(anfang)
        kopf = file.read(8)
        if len(kopf) != 8:
            raise UnsupportedTypeError("MP4: unvollstaendiger Boxkopf.")
        groesse = int.from_bytes(kopf[:4], "big")
        nutzanfang = anfang + 8
        if groesse == 1:
            erweitert = file.read(8)
            if len(erweitert) != 8:
                raise UnsupportedTypeError("MP4: unvollstaendige 64-Bit-Groesse.")
            groesse = int.from_bytes(erweitert, "big")
            nutzanfang += 8
        elif groesse == 0:
            groesse = ende - anfang
        boxende = anfang + groesse
        if boxende < nutzanfang or boxende > ende:
            raise UnsupportedTypeError("MP4: Boxgroesse liegt ausserhalb ihres Containers.")
        yield kopf[4:8], nutzanfang, boxende
        anfang = boxende


def _m4a_typ(file: BinaryIO) -> DocumentType:
    """Nur Tonspuren zulassen, unabhaengig von ftyp-Marke und Dateiname (#76).

    moov/trak/mdia/hdlr beschreibt jede Spur, auch wenn moov hinter mdat liegt.
    Der Handler steht nach Version/Flags und pre_defined (je vier Bytes).
    Quelle: https://developer.apple.com/documentation/quicktime-file-format/handler_reference_atom
    Dekodieren bleibt Aufgabe von ElevenLabs; hier werden nur Spuren bestimmt.
    """
    file.seek(0, io.SEEK_END)
    ende = file.tell()
    spuren = 0
    dateityp = False
    for art, anfang, boxende in _mp4_boxen(file, 0, ende):
        if art == b"ftyp":
            dateityp = True
        if art != b"moov":
            continue
        for art, anfang, boxende in _mp4_boxen(file, anfang, boxende):
            if art != b"trak":
                continue
            handler: list[bytes] = []
            for art, anfang, boxende in _mp4_boxen(file, anfang, boxende):
                if art != b"mdia":
                    continue
                for art, anfang, boxende in _mp4_boxen(file, anfang, boxende):
                    if art == b"hdlr":
                        if boxende - anfang < 12:
                            raise UnsupportedTypeError("MP4: unvollstaendiger Medienhandler.")
                        file.seek(anfang + 8)
                        handler.append(file.read(4))
            if handler != [b"soun"]:
                raise UnsupportedTypeError("MP4 enthaelt keine reine Audiospur; Video ist nicht unterstuetzt.")
            spuren += 1
    if not dateityp or not spuren:
        raise UnsupportedTypeError("MP4 enthaelt keine erkennbare Audiospur.")
    return DocumentType.M4A


def _typ_aus_kopf(kopf: bytes) -> DocumentType | None:
    """Erkennt alles ausser ZIP und MP4 am Anfang. None heisst: kein Treffer.

    Die festen Kennbytes zuerst, dann die Textformate in der Reihenfolge von
    DocVortex (RTF vor MHTML); HTML zuletzt, denn ein MHTML-Teil ist HTML.
    """
    if _PDF_ANFANG.match(kopf):
        return DocumentType.PDF
    if kopf.startswith(b"RIFF") and kopf[8:12] == b"WAVE":
        return DocumentType.WAV
    if kopf.startswith(b"FORM") and kopf[8:12] in (b"AIFF", b"AIFC"):
        return DocumentType.AIFF
    if kopf.startswith(b"fLaC"):
        return DocumentType.FLAC
    if kopf.startswith(b"OggS") and any(
        kennung in kopf for kennung in (b"OpusHead", b"\x01vorbis", b"Speex   ", b"\x7fFLAC")
    ):
        return DocumentType.OGG
    if len(kopf) >= 7 and kopf[0] == 0xff and kopf[1] & 0xf6 == 0xf0:
        return DocumentType.AAC  # ADTS, kein MPEG-Audio-Layer
    if kopf.startswith(b"ADIF"):
        return DocumentType.AAC
    if kopf.startswith(b"ID3") or (
        len(kopf) >= 4 and kopf[0] == 0xff and kopf[1] & 0xe0 == 0xe0
        and kopf[1] & 0x18 != 0x08 and kopf[1] & 0x06 == 0x02
        and kopf[2] >> 4 not in (0, 15) and kopf[2] & 0x0c != 0x0c
    ):
        return DocumentType.MP3
    if kopf.startswith(b"\x89PNG\r\n\x1a\n"):
        return DocumentType.PNG
    if kopf.startswith(b"\xff\xd8\xff"):
        return DocumentType.JPEG
    if kopf.startswith((b"GIF87a", b"GIF89a")):
        return DocumentType.GIF
    if kopf.startswith(b"BM"):
        return DocumentType.BMP
    # TIFF gibt es in beiden Byte-Reihenfolgen: Intel (little) und Motorola (big).
    if kopf.startswith((b"II*\x00", b"MM\x00*")):
        return DocumentType.TIFF
    # WebP ist ein RIFF-Behaelter; die vier Bytes dazwischen sind die Groesse.
    if kopf.startswith(b"RIFF") and kopf[8:12] == b"WEBP":
        return DocumentType.WEBP
    if kopf.startswith(_OLE2_KENNUNG):
        return DocumentType.OLE2
    if _RTF_KOPF.match(kopf[:RTF_KOPF_BYTES]):
        return DocumentType.RTF
    if _ist_mhtml(kopf):
        return DocumentType.MHTML
    if _HTML_ANFANG.match(kopf) or _html_utf16_kodierung(kopf) is not None:
        return DocumentType.HTML
    return None


def _html_utf16_kodierung(kopf: bytes) -> str | None:
    """UTF-16 nur mit HTML-Kennung am Anfang, mit oder ohne BOM (#56).

    Inkrementell, weil die 64-KiB-Grenze mitten in einem Surrogatpaar oder
    Codepunkt liegen kann. Der ganze Inhalt wird erst fuer den Worker dekodiert.
    """
    for kodierung in ("utf-16",) if kopf.startswith((b"\xff\xfe", b"\xfe\xff")) else (
        "utf-16-le", "utf-16-be",
    ):
        try:
            text = codecs.getincrementaldecoder(kodierung)().decode(kopf, final=False)
        except UnicodeDecodeError:
            continue
        if _HTML_ANFANG.match(text.encode("utf-8")):
            return kodierung
    return None


def _ist_zip(kopf: bytes) -> bool:
    """DOCX, ODT, EPUB, OFD und jedes andere ZIP tragen dieselben vier Magic Bytes."""
    return kopf.startswith(b"PK\x03\x04")


def worker_bytes(data: bytes, doc_type: DocumentType) -> bytes:
    """Bereitet erkannte Bytes nur fuer den Worker auf, nie fuer Hash oder DB.

    PDF verliert ausschliesslich den von der Erkennung erlaubten Vorspann.
    HTML in UTF-16 wird UTF-8 mit BOM, damit beim Worker die BOM vor alten
    Kodierungsangaben im HTML gilt; andere Bytes bleiben unveraendert (#56).
    """
    if doc_type.is_pdf:
        anfang = _PDF_ANFANG.match(data[:KOPF_BYTES])
        if anfang is not None:
            return data[anfang.end() - len(b"%PDF-"):]
    if doc_type is DocumentType.HTML:
        kodierung = _html_utf16_kodierung(data[:KOPF_BYTES])
        if kodierung is not None:
            try:
                return data.decode(kodierung).encode("utf-8-sig")
            except UnicodeDecodeError as fehler:
                raise CorruptDocumentError(
                    f"UTF-16-HTML ist beschaedigt (Byte {fehler.start}, null-basiert)."
                ) from fehler
    return data


def _unbekannt(kopf: bytes, groesse: str) -> UnsupportedTypeError:
    """Die Meldung fuer einen Typ ohne Treffer. Die Kennbytes stehen UNGEKUERZT
    darin (`MELDUNG_BYTES`): Wer sie abschneidet, nimmt genau die Angabe weg, mit
    der man den Typ von Hand nachschlaegt."""
    versatz = kopf.find(b"%PDF-")
    hinweis = (
        f" Ab Byte {versatz} (null-basiert) beginnt %PDF-, aber davor stehen "
        "fremde Bytes. Die Datei muss von Hand geprueft werden."
        if versatz > 0 else ""
    )
    return UnsupportedTypeError(
        f"Kein unterstuetzter Typ (die ersten {MELDUNG_BYTES} Byte: "
        f"{kopf[:MELDUNG_BYTES]!r}; {groesse}).{hinweis}"
    )


def sniff_file(file: BinaryIO) -> DocumentType:
    """Bestimmt den Typ einer geoeffneten Datei, ohne sie ganz zu lesen.

    Der Weg fuer die Kommandozeile, die Dateien liest: Eine grosse Datei fremden
    Typs soll nicht erst ganz in den Speicher, um dann abgewiesen zu werden. Liest
    so wenig wie noetig: fuer alle Typen ausser ZIP und MP4 genuegt der Anfang
    (`KOPF_BYTES`); bei ZIP wird zusaetzlich das Archivverzeichnis gelesen und,
    wo das Format es verlangt, ein kleiner Eintrag (nicht das Archiv ausgepackt).
    Bei MP4 werden Boxkoepfe und Medienhandler gelesen, Mediendaten uebersprungen.
    Die Bibliothek selbst kennt keine Pfade — wer eine Datei hat, oeffnet sie und
    reicht das Dateiobjekt herein.

    Args:
        file: Eine zum Lesen geoeffnete, durchsuchbare Datei (`open(..., "rb")`),
            positioniert am Anfang. Sie bleibt offen; schliessen tut der Aufrufer.

    Returns:
        Der erkannte Typ.

    Zusicherung:
        Reiner Lesezugriff. Gibt niemals einen Typ zurueck, den `DocumentType`
        nicht kennt — im Zweifel wird geworfen.

    Raises:
        UnsupportedTypeError: Der Anfang gehoert zu keinem unterstuetzten Typ,
            ODER die Datei ist ein ZIP ohne den Aufbau eines unterstuetzten
            Formats, ODER sie ist kuerzer als die noetigen Kennbytes (auch eine
            leere Datei ist kein unterstuetzter Typ, sondern ein Eingabefehler).
        OSError: Die Datei ist nicht lesbar. Wird bewusst NICHT eingepackt: Ein
            Rechteproblem ist kein Typproblem, und die Verwechslung der beiden
            wuerde die Fehlersuche in die falsche Richtung schicken.

    Kosten: keine.
    """
    kopf = file.read(KOPF_BYTES)

    treffer = _typ_aus_kopf(kopf)
    if treffer is not None:
        return treffer

    if kopf[4:8] in MP4_ANFANGSBOXEN:
        return _m4a_typ(file)

    if _ist_zip(kopf):
        try:
            with zipfile.ZipFile(file) as archiv:
                return _typ_aus_zip(archiv)
        except _ZIP_LESEFEHLER as fehler:
            raise UnsupportedTypeError(
                f"Beginnt wie ein ZIP, ist aber keines: {type(fehler).__name__}: {fehler}."
            ) from fehler

    raise _unbekannt(kopf, f"gelesen {len(kopf)} Byte")


def sniff_bytes(data: bytes) -> DocumentType:
    """Bestimmt den Typ eines Byte-Puffers im Speicher.

    Der Weg des Abschickens (`OCRClient.ocr_start`, `core.prepare`) und des Jobs,
    die nur Bytes kennen und keine Datei. Verhaelt sich sonst genau wie
    `sniff_file`, das die Kommandozeile vor dem Lesen einer Datei benutzt.

    Args:
        data: Die VOLLSTAENDIGEN Bytes. Nicht nur der Anfang: Das ZIP-Verzeichnis
            und die MP4-Spurbeschreibung koennen am Dateiende stehen. Ein
            abgeschnittener Puffer wuerde gueltige Dateien abweisen.

    Returns:
        Der erkannte Typ.

    Raises:
        UnsupportedTypeError: wie bei `sniff_file`.

    Kosten: keine.
    """
    kopf = data[:KOPF_BYTES]

    treffer = _typ_aus_kopf(kopf)
    if treffer is not None:
        return treffer

    if kopf[4:8] in MP4_ANFANGSBOXEN:
        return _m4a_typ(io.BytesIO(data))

    if _ist_zip(kopf):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archiv:
                return _typ_aus_zip(archiv)
        except _ZIP_LESEFEHLER as fehler:
            # Ohne Zentralverzeichnis (`BadZipFile`) ist der haeufigste Grund ein
            # ABGESCHNITTENER Puffer: Das Verzeichnis steht am Ende der Datei.
            # Deshalb sagt die Meldung es dazu — sonst sucht man den Fehler im
            # Archiv statt beim Aufrufer. Ein Verzeichnis, das `zipfile` liest,
            # aber nicht versteht, ist ein Fehler des Archivs.
            hinweis = (
                f" Haeufigste Ursache: Der uebergebene Puffer ist unvollstaendig "
                f"({len(data)} Byte) — das Zentralverzeichnis steht am Dateiende."
                if isinstance(fehler, zipfile.BadZipFile)
                else ""
            )
            raise UnsupportedTypeError(
                f"Beginnt wie ein ZIP, ist aber keines lesbares: "
                f"{type(fehler).__name__}: {fehler}.{hinweis}"
            ) from fehler

    raise _unbekannt(kopf, f"Puffergroesse {len(data)} Byte")
