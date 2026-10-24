"""Typerkennung an den Magic Bytes — nie an der Dateiendung.

Warum nicht an der Endung: Eine Endung ist eine Behauptung des Dateisystems, kein
Befund ueber den Inhalt. In einem gewachsenen Dokumentenbestand liegen `.pdf`, die
JPEG sind, und `.doc`, die in Wahrheit DOCX sind. Wer der Endung glaubt, schickt
eine Datei los, die die Engine nicht lesen kann, und bezahlt fuer den Fehlschlag.

Die eine Stelle, an der es nicht ohne Zusatzarbeit geht, ist ZIP: DOCX, PPTX und
XLSX tragen alle dieselben vier Magic Bytes wie jede beliebige andere ZIP-Datei
(`PK\\x03\\x04`). Ein ZIP ohne Office-Struktur muss abgewiesen werden, statt es
durchzureichen und zu hoffen — deshalb schaut die Erkennung in diesem einen Fall
in das Archiv hinein und sucht die Verzeichnisse `word/`, `xl/` oder `ppt/`
(Plan 6).
"""

from __future__ import annotations

import enum
import io
import zipfile
from pathlib import Path
from typing import Final

from .errors import UnsupportedTypeError

KOPF_BYTES: Final = 16
"""So viele Bytes braucht die Erkennung am Dateianfang. WebP ist der laengste
Fall: vier Bytes `RIFF`, vier Bytes Groesse, dann `WEBP` — zwoelf. Sechzehn sind
Puffer."""

_OFFICE_VERZEICHNISSE: Final = {
    "word/": "DOCX",
    "xl/": "XLSX",
    "ppt/": "PPTX",
}
"""Woran ein Office-Archiv von einem beliebigen ZIP zu unterscheiden ist.

Gesucht wird im ZENTRALVERZEICHNIS des Archivs, nicht in den ersten Bytes. Der
Unterschied ist nicht theoretisch: Von LibreOffice geschriebene Office-Dateien
ordnen ihre Eintraege anders an, und wer nur die ersten paar lokalen Kopfbereiche
ansieht — wie es verbreitete Erkennungs-Bibliotheken tun —, weist ein gueltiges
DOCX als nicht unterstuetzt ab. Das Zentralverzeichnis kennt alle Eintraege,
unabhaengig von ihrer Reihenfolge."""


class DocumentType(enum.Enum):
    """Die unterstuetzten Eingabearten. Was hier nicht steht, wird abgewiesen.

    Die Liste ist die des Plans (7, `OCRWrapper`-Init) und nicht laenger: Sie
    deckt, was MinerU als Erstklass-Eingabe nimmt. Ein Typ dazuzunehmen heisst,
    ihn vorher an der echten Engine geprueft zu haben — nicht, hier eine Zeile
    einzufuegen.
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

    @property
    def is_pdf(self) -> bool:
        """Ob dieser Typ seitenweise zerlegt werden kann.

        Nur PDF kann das. Bilder sind einseitig, Office-Formate sind nicht
        seitenweise zerlegbar — beide gehen ungeteilt hinaus oder gar nicht
        (Plan 6).

        Der Name sagt „ist PDF", die Frage lautet „ist zerlegbar". Heute ist das
        dasselbe. Kaeme je ein zweites zerlegbares Format dazu, ist DIESE Stelle
        zu aendern und nicht die Aufrufer.
        """
        return self is DocumentType.PDF

    @property
    def mime(self) -> str:
        """Die MIME-Zeichenkette, wie sie in `documents.mime` landet."""
        return _TABELLE[self][0]

    @property
    def suffix(self) -> str:
        """Die Endung mit fuehrendem Punkt."""
        return _TABELLE[self][1]


_TABELLE: Final[dict[DocumentType, tuple[str, str]]] = {
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
}
"""MIME-Zeichenkette und Endung je Typ. DIESE WERTE SIND DAUERHAFT.

Warum das keine Kleinigkeit ist: Die MIME-Zeichenkette landet unveraenderlich in
`documents.mime`, und der Job findet ueber sie den Typ zur Zeile wieder
(`executor._typ_aus_mime`). Wer hier spaeter eine MIME-Zeichenkette aendert,
laesst alle bestehenden Zeilen ins Leere zeigen, ohne dass etwas auffaellt.

Wo es zwei uebliche Schreibweisen gibt, ist die kuerzere gewaehlt (`.jpg`,
`.tif`) — die Wahl ist beliebig, ihre Bestaendigkeit nicht."""


def _typ_aus_zip(archiv: zipfile.ZipFile) -> DocumentType:
    """Entscheidet, welches Office-Format ein ZIP traegt — oder weist es ab."""
    for name in archiv.namelist():
        for praefix, typ in _OFFICE_VERZEICHNISSE.items():
            if name.startswith(praefix):
                return DocumentType[typ]
    raise UnsupportedTypeError(
        "ZIP-Archiv ohne Office-Struktur (weder word/, xl/ noch ppt/ im "
        "Zentralverzeichnis) — nicht unterstuetzt."
    )


def _typ_aus_kopf(kopf: bytes) -> DocumentType | None:
    """Erkennt alles ausser ZIP an den ersten Bytes. None heisst: kein Treffer."""
    if kopf.startswith(b"%PDF-"):
        return DocumentType.PDF
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
    return None


def _ist_zip(kopf: bytes) -> bool:
    """DOCX, PPTX, XLSX und jedes andere ZIP tragen dieselben vier Magic Bytes."""
    return kopf.startswith(b"PK\x03\x04")


def sniff_path(path: Path) -> DocumentType:
    """Bestimmt den Typ einer Datei auf der Platte.

    Der kanonische Weg. Liest so wenig wie noetig: fuer alle Typen ausser ZIP
    genuegen die ersten Bytes; bei ZIP wird zusaetzlich das Archivverzeichnis
    gelesen (nicht das Archiv ausgepackt).

    Args:
        path: Vorhandene, lesbare Datei.

    Returns:
        Der erkannte Typ.

    Zusicherung:
        Reiner Lesezugriff, keine Aenderung an der Datei. Gibt niemals einen Typ
        zurueck, den `DocumentType` nicht kennt — im Zweifel wird geworfen.

    Raises:
        UnsupportedTypeError: Die Magic Bytes gehoeren zu keinem unterstuetzten
            Typ, ODER die Datei ist ein ZIP ohne Office-Struktur, ODER sie ist
            kuerzer als die noetigen Magic Bytes (auch eine leere Datei ist kein
            unterstuetzter Typ, sondern ein Eingabefehler).
        OSError: Die Datei ist nicht lesbar. Wird bewusst NICHT eingepackt: Ein
            Rechteproblem ist kein Typproblem, und die Verwechslung der beiden
            wuerde die Fehlersuche in die falsche Richtung schicken.

    Kosten: keine.
    """
    with path.open("rb") as datei:
        kopf = datei.read(KOPF_BYTES)

    treffer = _typ_aus_kopf(kopf)
    if treffer is not None:
        return treffer

    if _ist_zip(kopf):
        try:
            with zipfile.ZipFile(path) as archiv:
                return _typ_aus_zip(archiv)
        except zipfile.BadZipFile as fehler:
            raise UnsupportedTypeError(
                f"{path.name} beginnt wie ein ZIP, ist aber keines: {fehler}"
            ) from fehler

    # Die gelesenen Kopfbytes stehen VOLLSTAENDIG in der Meldung. Wer sie
    # abschneidet, nimmt genau die Angabe weg, mit der man den Typ von Hand
    # nachschlaegt.
    raise UnsupportedTypeError(
        f"{path.name}: kein unterstuetzter Typ "
        f"(gelesene Kopfbytes: {kopf!r}, {len(kopf)} Byte)."
    )


def sniff_bytes(data: bytes) -> DocumentType:
    """Bestimmt den Typ eines Byte-Puffers im Speicher.

    Fuer den Weg ueber `OCRWrapper.from_bytes`, bei dem es keine Datei gibt.
    Verhaelt sich sonst genau wie `sniff_path`.

    Args:
        data: Die VOLLSTAENDIGEN Bytes. Nicht nur der Anfang: Bei einem ZIP
            braucht die Erkennung das Archivverzeichnis, und das steht am ENDE
            der Datei. Ein abgeschnittener Puffer wuerde ein gueltiges DOCX als
            nicht unterstuetzt abweisen.

    Returns:
        Der erkannte Typ.

    Raises:
        UnsupportedTypeError: wie bei `sniff_path`.

    Kosten: keine.
    """
    kopf = data[:KOPF_BYTES]

    treffer = _typ_aus_kopf(kopf)
    if treffer is not None:
        return treffer

    if _ist_zip(kopf):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archiv:
                return _typ_aus_zip(archiv)
        except zipfile.BadZipFile as fehler:
            # Der haeufigste Grund hierfuer ist ein ABGESCHNITTENER Puffer: Das
            # Zentralverzeichnis steht am Ende der Datei. Deshalb sagt die
            # Meldung es dazu — sonst sucht man den Fehler im Archiv statt beim
            # Aufrufer.
            raise UnsupportedTypeError(
                f"Beginnt wie ein ZIP, ist aber keines lesbares: {fehler}. "
                f"Haeufigste Ursache: Der uebergebene Puffer ist unvollstaendig "
                f"({len(data)} Byte) — das Zentralverzeichnis steht am Dateiende."
            ) from fehler

    raise UnsupportedTypeError(
        f"Kein unterstuetzter Typ (gelesene Kopfbytes: {kopf!r}, "
        f"Puffergroesse {len(data)} Byte)."
    )
