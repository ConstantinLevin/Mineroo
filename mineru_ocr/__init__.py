"""mineru_ocr — Dokumente rein, Text und Struktur raus.

Die lokale Seite der MinerU-Strecke: Ein Daemon arbeitet Dokumente ueber einen
RunPod-Endpoint ab, eine Postgres-Tabelle haelt die Ergebnisse, und eine Fassade
macht beides bequem benutzbar. Die Bibliothek nimmt BYTES und gibt HASHES; Pfade
kennt nur die Kommandozeile. Der uebliche Weg:

    from pathlib import Path
    from mineru_ocr import OCRClient

    with OCRClient() as client:
        h = client.ocr_start(Path("scan.pdf").read_bytes())
        # kehrt SOFORT zurueck; ein laufender Daemon verarbeitet den Job.
        # `h` ist der Dokumenthash, mit dem es in jedem Prozess weitergeht.
        print(client.status())          # Uebersicht, jederzeit

        # Das Dokument abwarten und ansehen:
        fertig = client.wait(h)
        print(fertig.rendered_markdown)

        # Auf viele warten: alle in Reihenfolge, oder jedes, sobald es fertig ist.
        hashes = client.ocr_start([Path(p).read_bytes() for p in ("a.pdf", "b.pdf")])
        alle = client.wait_all(hashes, timeout=600)
        for huelle in client.as_completed(hashes):
            print(huelle.sha256, huelle.status)

        # Oder spaeter, ohne zu warten — auch aus einer anderen Sitzung:
        print(client.get(h).rendered_markdown)

        # Abschicken und warten in einem Zug, mit der vollstaendigen Antwort:
        antwort = client.ocr(Path("andere.pdf").read_bytes()).raw

Dasselbe asynchron, mit denselben Namen und `await` (`status`, `preflight`,
`document` und `retry` gibt es nur synchron):

    from mineru_ocr import AsyncOCRClient

    async with AsyncOCRClient() as client:
        hashes = await client.ocr_start([pdf_a, pdf_b])
        async for huelle in client.as_completed(hashes, timeout=600):
            print(huelle.sha256, huelle.status)
        fertig = await client.wait(hashes[0])   # die Huelle traegt `raw` geladen

Und derselbe Dienst ueber die Kommandozeile, die Dateien und Ordner liest:

    mineru-ocr setup
    mineru-ocr daemon start 1           # Exekutor im Hintergrund
    mineru-ocr scans/ --timeout 600      # abschicken, warten, Volltext ausgeben
    mineru-ocr daemon list              # lokale Daemons und Warteschlange
    mineru-ocr daemon stop 1            # bis zu einen Daemon ordentlich beenden

Was hier steht, ist die zugesicherte Flaeche. Alles andere in den Untermodulen
ist Innenleben und kann sich aendern.

Der Daemon ist ein EIGENER PROZESS. Wer nur einreiht, startet keinen —
`ocr_start` legt Jobs an, und wenn niemand sie abholt, passiert nichts. Genau das
ist die haeufigste Ueberraschung dieses Aufbaus, und deshalb warnt `ocr_start`
(und sagt der Preflight es einem), statt es einen suchen zu lassen.

Der ganze Aufbau steht samt Begruendungen in `plans/2026-08-05-ocr-service-plan.md`;
jede Procrastinate-Stuetze ist einzeln belegt in
`plans/2026-08-06-procrastinate-feature-verifikation.md`.
"""

from __future__ import annotations

from .client import AsyncOCRClient, OCRClient
from .config import ReadSettings, Settings
from .core import RetryOutcome, Submitted
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
    MultiImageError,
    ResultLostError,
    RunPodError,
    TerminalOverwriteError,
    TerminalRunPodError,
    TooLargeError,
    TransientRunPodError,
    UnsupportedTypeError,
    WaitTimeoutError,
)
from .mime import DocumentType
from .wrapper import OCRWrapper

__all__ = [
    # Der uebliche Einstieg
    "OCRClient",
    "AsyncOCRClient",
    "OCRWrapper",
    "Submitted",
    "RetryOutcome",
    # Einstellung und Zustaende
    "Settings",
    "ReadSettings",
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
    "MultiImageError",
    "TooLargeError",
    "RunPodError",
    "TransientRunPodError",
    "TerminalRunPodError",
    "ResultLostError",
    "InsufficientBalanceError",
    "DatabaseError",
    "DocumentNotFound",
    "TerminalOverwriteError",
    "WaitTimeoutError",
    "IncompleteResultError",
    "FailureKind",
]

__version__ = "1.0"
