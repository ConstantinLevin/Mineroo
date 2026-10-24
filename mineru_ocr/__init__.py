"""mineru_ocr — Dokumente rein, Text und Struktur raus.

Die lokale Seite der MinerU-Strecke: Ein Daemon arbeitet Dokumente ueber einen
RunPod-Endpoint ab, eine Postgres-Tabelle haelt die Ergebnisse, und eine Fassade
macht beides bequem benutzbar. Der uebliche Weg:

    from pathlib import Path
    from mineru_ocr import OCRClient, OCRMegaWrapper

    with OCRClient() as client:
        mega = OCRMegaWrapper.from_dir(
            Path("~/Scans").expanduser(), client.conn, client.settings
        )
        client.ocr_start(mega)          # kehrt SOFORT zurueck, es laeuft noch nichts
        print(client.status())          # Uebersicht, jederzeit

        # Ein einzelnes Dokument abwarten und ansehen:
        fertig = client.wait("<sha256>")
        print(fertig.rendered_markdown)

        # Oder spaeter, ohne zu warten — auch aus einer anderen Sitzung:
        print(client.get("<sha256>").rendered_markdown)

Und derselbe Dienst ueber die Kommandozeile:

    mineru-ocr setup
    mineru-ocr daemon                   # in einem eigenen Fenster: der Exekutor
    mineru-ocr ocr ~/Scans
    mineru-ocr status

Was hier steht, ist die zugesicherte Flaeche. Alles andere in den Untermodulen
ist Innenleben und kann sich aendern.

Der Daemon ist ein EIGENER PROZESS. Wer nur einreiht, startet keinen —
`ocr_start` legt Jobs an, und wenn niemand sie abholt, passiert nichts. Genau das
ist die haeufigste Ueberraschung dieses Aufbaus, und deshalb sagt der Preflight
es einem, statt es einen suchen zu lassen.

Der ganze Aufbau steht samt Begruendungen in `plans/2026-08-05-ocr-service-plan.md`;
jede Procrastinate-Stuetze ist einzeln belegt in
`plans/2026-08-06-procrastinate-feature-verifikation.md`.
"""

from __future__ import annotations

from .client import OCRClient
from .config import Settings
from .db import DerivedStatus, DocumentRow, DocumentStatus, Phase
from .errors import (
    ConfigDriftError,
    ConfigurationError,
    CorruptDocumentError,
    DatabaseError,
    DocumentNotFound,
    FailureKind,
    IncompleteResultError,
    InputError,
    InsufficientBalanceError,
    MineruOcrError,
    ResultLostError,
    RunPodError,
    TerminalOverwriteError,
    TerminalRunPodError,
    TooLargeError,
    TransientRunPodError,
    UnsupportedTypeError,
)
from .mime import DocumentType
from .wrapper import OCRMegaWrapper, OCRWrapper

__all__ = [
    # Der uebliche Einstieg
    "OCRClient",
    "OCRWrapper",
    "OCRMegaWrapper",
    # Einstellung und Zustaende
    "Settings",
    "DocumentStatus",
    "DocumentRow",
    "DerivedStatus",
    "Phase",
    "DocumentType",
    # `Chunk` und `ChunkArtifacts` stehen hier ABSICHTLICH nicht: Sie leben nur
    # waehrend eines Laufs im Executor, keine oeffentliche Methode gibt sie je
    # heraus, und ein Export waere eine Stabilitaets-Zusage ueber etwas, das es
    # von aussen gar nicht gibt.
    # Ausnahmen — vollstaendig, damit ein Aufrufer gezielt fangen kann
    "MineruOcrError",
    "ConfigurationError",
    "ConfigDriftError",
    "InputError",
    "UnsupportedTypeError",
    "CorruptDocumentError",
    "TooLargeError",
    "RunPodError",
    "TransientRunPodError",
    "TerminalRunPodError",
    "ResultLostError",
    "InsufficientBalanceError",
    "DatabaseError",
    "DocumentNotFound",
    "TerminalOverwriteError",
    "IncompleteResultError",
    "FailureKind",
]

__version__ = "0.1.0"
