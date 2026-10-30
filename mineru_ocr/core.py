"""Der Kern des Abschickens: Hash, Typpruefung und Groessenpruefung, ohne Ein- und Ausgabe.

Die Bibliothek ist so gebaut, wie httpx es tut: ein Kern, der nichts liest und
nichts schreibt, und duenne Schalen darueber, die die Verbindung halten. Heute gibt
es die synchrone Schale (`OCRClient` ueber psycopgs `Connection`); der asynchrone
Client legt spaeter eine zweite ueber `AsyncConnection`. Beide pruefen ein Dokument
mit denselben Funktionen und kommen deshalb zu demselben Urteil, welche Bytes
abgeschickt werden und welche nicht.

Was in den Schalen steht und hier bewusst nicht: Verbindungen, Transaktionen,
Warten, Procrastinate. Was hier steht und in keiner Schale doppelt: wie der Hash
gebildet wird, woran ein Typ erkannt wird, was zu gross ist.

Hier gibt es keinen Pfad. Wer Dateien liest, ist die Kommandozeile; die Bibliothek
bekommt Bytes.
"""

from __future__ import annotations

import enum
import hashlib
from dataclasses import dataclass
from typing import Any

from . import db
from .chunking import check_fits_unsplit
from .config import Settings
from .db import DocumentStatus
from .errors import TooLargeError
from .mime import DocumentType, sniff_bytes


def sha256_of(data: bytes) -> str:
    """Der Hash eines Dokuments, der Griff, unter dem es ueberall weitergeht.

    Roher Byte-Hash, kein Metadaten-normalisierter: Zwei PDFs, die sich nur im
    Titel unterscheiden, sind zwei Dokumente. Es ist derselbe Hash, den der Job
    nach dem Lesen der Eingabe nachprueft.
    """
    return hashlib.sha256(data).hexdigest()


def check_size(size_bytes: int, was: str) -> None:
    """Weist eine Eingabe ab, die nicht in `inputs` passt — mit einer Meldung.

    Ohne diese Pruefung scheitert das Ablegen ab etwa 1 GiB mit einem
    Verbindungsabbruch des Servers, und in einem Ordnerlauf bricht das das ganze
    Einreihen ab.

    Args:
        size_bytes: Die Groesse der Eingabe.
        was: Wie die Meldung sie nennt (ein Dateiname, oder „die Bytes").

    Raises:
        TooLargeError: `size_bytes` liegt ueber `db.MAX_INPUT_BYTES`.
    """
    if size_bytes > db.MAX_INPUT_BYTES:
        raise TooLargeError(
            f"{was}: {size_bytes} Byte sind mehr, als sich in der Datenbank "
            f"ablegen laesst ({db.MAX_INPUT_BYTES} Byte). Die Eingabe liegt als "
            f"`bytea` in der Datenbank, und Postgres begrenzt ein Feld auf knapp "
            f"1 GiB."
        )


@dataclass(frozen=True)
class Prepared:
    """Ein geprueftes Dokument, bereit zum Ablegen. Reine Daten."""

    sha256: str
    doc_type: DocumentType
    size_bytes: int


def prepare(data: bytes, settings: Settings) -> Prepared:
    """Prueft ein Dokument und bildet seinen Hash. Legt nichts ab.

    Abgewiesen wird, woraus nichts Neues entstehen kann („nur abschicken wenn
    etwas neues entstehen soll"):

    * ein Dateityp, den das Paket nicht liest;
    * eine Eingabe, die sich nicht in der Datenbank ablegen laesst;
    * ein Ganzdokument-Format oder Bild ueber der Groessengrenze einer Anfrage
      (`max_payload_bytes`): Es laesst sich nicht zerlegen, also kann es nie
      hinausgehen. Eine einzelne PDF-Seite ueber der Grenze findet erst das
      Schneiden im Job, denn das Abschicken kennt die Seiten nicht.

    Raises:
        UnsupportedTypeError: Der Typ wird nicht unterstuetzt.
        TooLargeError: Die Eingabe ist zu gross, siehe oben.

    Kosten: keine.
    """
    typ = sniff_bytes(data)
    check_size(len(data), "die Bytes")
    if not (typ.is_pdf or typ.is_audio):
        check_fits_unsplit(size_bytes=len(data), doc_type=typ, settings=settings)
    return Prepared(sha256=sha256_of(data), doc_type=typ, size_bytes=len(data))


class Submitted(str):
    """Der Hash eines abgeschickten Dokuments — und was das Abschicken daraus machte.

    IST der Hash: Er vergleicht sich, hasht und serialisiert sich wie die
    Zeichenkette, die er ist, und taugt als Schluessel und als Argument fuer `wait`,
    `get` und `document`. Die zwei Angaben daran sagen, was beim Abschicken
    geschah; ohne sie muesste, wer es wissen will, vorher und nachher nachsehen.

    Attributes:
        queued: Ob dieser Aufruf das Dokument eingereiht hat. False bei einem
            Treffer (es gibt ein Ergebnis) und bei einem Dokument, das schon
            unterwegs ist.
        status: Der Endzustand des Dokuments im Augenblick des Abschickens, oder
            None, wenn es offen ist (eingereiht oder schon unterwegs). Einen
            Zustand fuer „noch kein Ergebnis" gibt es nicht: Die Ergebniszeile
            legt erst der Job an.
    """

    queued: bool
    status: DocumentStatus | None

    @property
    def is_open(self) -> bool:
        """Das Dokument hat noch kein Ergebnis: eingereiht oder schon unterwegs."""
        return self.status is None

    def __new__(
        cls, sha256: str, *, queued: bool, status: DocumentStatus | None
    ) -> Submitted:
        gleich = super().__new__(cls, sha256)
        gleich.queued = queued
        gleich.status = status
        return gleich

    def __getnewargs_ex__(self) -> tuple[tuple[str], dict[str, Any]]:
        """Damit Kopieren und Pickeln den Hash MIT seinen Angaben wiederherstellen.

        Ohne das riefe `copy` `__new__` nur mit der Zeichenkette auf und stuerzte
        ueber die fehlenden Schluesselwoerter ab.
        """
        return (str(self),), {"queued": self.queued, "status": self.status}


class RetryOutcome(enum.Enum):
    """Was `retry` mit einem Dokument getan hat. Nur die letzten beiden kosten."""

    HAS_RESULT = "has_result"
    """Nichts: Es steht ein gueltiges Ergebnis da (`done`, `empty`,
    `input_error`)."""

    UNDERWAY = "underway"
    """Nichts: Der Job wartet oder laeuft."""

    RESET = "reset"
    """Die `failed`-Zeile ist weg, und derselbe gescheiterte Job ist mit
    `retry_job_by_id` zurueckgesetzt."""

    REQUEUED = "requeued"
    """Ein neuer Job ist eingereiht (der alte war abgebrochen, abgesagt oder weg),
    eine `failed`-Zeile ist weg, und die neue Kennung steht an der Eingabe."""
