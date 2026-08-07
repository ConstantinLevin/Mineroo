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
from pathlib import Path


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
        """
        raise NotImplementedError

    @property
    def mime(self) -> str:
        """Die MIME-Zeichenkette, wie sie in `documents.mime` landet."""
        raise NotImplementedError

    @property
    def suffix(self) -> str:
        """Die Endung mit fuehrendem Punkt, fuer den Namen der Spool-Datei."""
        raise NotImplementedError


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
    raise NotImplementedError


def sniff_bytes(data: bytes) -> DocumentType:
    """Bestimmt den Typ eines Byte-Puffers im Speicher.

    Fuer den Weg ueber `OCRWrapper.from_bytes`, bei dem noch keine Datei
    existiert. Verhaelt sich sonst genau wie `sniff_path`.

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
    raise NotImplementedError
