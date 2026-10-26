"""Der Ablauf an einem Dokument — ohne Warteschlange, ohne Netz.

Dass es diese Datei gibt, ist der Existenzgrund von `executor.py`: Das Modul
weiß nichts von Procrastinate, damit genau diese Tests ohne laufende
Warteschlange möglich sind. Fehlten sie, wäre die Trennung umsonst gewesen.
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import hashlib
import io
import re
import threading
from pathlib import Path
from typing import Any

import psycopg
import pypdfium2
import pytest
from PIL import Image
from psycopg_pool import AsyncConnectionPool

import ersatz_antworten as ersatz
import ganzdokumente
from mineru_ocr import db as db_modul
from mineru_ocr.config import ReadSettings, Settings
from mineru_ocr.db import DocumentStatus, get_document, insert_input, write_terminal_async
from mineru_ocr.errors import (
    ConfigurationError,
    CorruptDocumentError,
    InputError,
    InsufficientBalanceError,
    JobAufgegebenError,
    MultiImageError,
    ResultLostError,
    TerminalRunPodError,
    TransientRunPodError,
    UnstorableResultError,
    UnsupportedTypeError,
)
from mineru_ocr.executor import (
    process_document,
    wait_for_balance,
    write_failure_if_no_result,
)
from mineru_ocr.mime import DocumentType, sniff_bytes

pytestmark = pytest.mark.db

PNG_KOPF = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"


class FalscherRunPod:
    """Ein RunPod-Zugang ohne Netz, der mitschreibt, was er tat.

    Er bildet die drei Methoden nach, die der Executor benutzt, und zaehlt
    dabei mit, WANN abgesendet und WANN abgeholt wurde. Genau daran haengt die
    wichtigste Zusicherung des Executors: dass je Dokument nie zwei Ergebnisse
    gleichzeitig unabgeholt sind.
    """

    def __init__(
        self,
        antworten: list[dict[str, Any]] | None = None,
        *,
        marke: str | None = ":17d4955b2",
        kontostand: float = 5.0,
    ) -> None:
        self.antworten = antworten or []
        self.marke = marke
        self.kontostand = kontostand
        self.abgesendet: list[str] = []
        self.abgeholt: list[str] = []
        self.abgebrochen: list[str] = []
        self.verlauf: list[str] = []
        # JEDER Aufruf, auch die lesenden (Kontostand, Herkunft des Images): wer
        # „vor jedem RunPod-Kontakt" prueft, darf keinen davon uebersehen.
        self.kontakte: list[str] = []
        # Je Absenden die Lese-Einstellungen (`tier`, `ocr_mode`,
        # `image_analysis`), mit denen der Executor RunPod gerufen hat.
        self.lese_einstellungen: list[tuple[str, str, bool]] = []
        self._nummer = 0

    async def submit(
        self,
        *,
        file_b64: str,
        tier: str,
        ocr_mode: str,
        image_analysis: bool,
        transport: str,
        **_: Any,
    ) -> str:
        job = f"job-{len(self.abgesendet)}"
        self.kontakte.append("submit")
        self.abgesendet.append(file_b64)
        self.lese_einstellungen.append((tier, ocr_mode, image_analysis))
        self.verlauf.append(f"submit:{job}")
        return job

    async def await_result(self, job_id: str) -> dict[str, Any]:
        self.kontakte.append("await_result")
        self.abgeholt.append(job_id)
        self.verlauf.append(f"holen:{job_id}")
        nummer = min(self._nummer, len(self.antworten) - 1)
        self._nummer += 1
        return self.antworten[nummer]

    async def cancel(self, job_id: str) -> dict[str, Any]:
        self.kontakte.append("cancel")
        self.abgebrochen.append(job_id)
        self.verlauf.append(f"abbrechen:{job_id}")
        return {"id": job_id, "status": "CANCELLED"}

    async def image_tag(self) -> str | None:
        self.kontakte.append("image_tag")
        return self.marke

    async def balance(self) -> float:
        self.kontakte.append("balance")
        return self.kontostand


def _pdf(seiten: int) -> bytes:
    doc = pypdfium2.PdfDocument.new()
    try:
        for _ in range(seiten):
            doc.new_page(200.0, 300.0)
        puffer = io.BytesIO()
        doc.save(puffer)
        return puffer.getvalue()
    finally:
        doc.close()


_KOPF: dict[DocumentType, bytes] = {
    DocumentType.PDF: b"%PDF-",
    DocumentType.PNG: b"\x89PNG\r\n\x1a\n",
}
"""Der Dateikopf, an dem der Job den Typ erkennt (`mime.sniff_bytes`)."""


def _als(typ: DocumentType, inhalt: bytes) -> bytes:
    """Die Bytes einer Eingabe vom Typ `typ`: der Inhalt hinter dem Dateikopf,
    falls er ihn noch nicht traegt. Der Job bestimmt den Typ aus den Bytes.

    Erkennt `sniff_bytes` den Typ schon (ein echtes Bild aus Pillow, eine PDF),
    bleibt der Inhalt, wie er ist; nur sonst kommt der Kopf aus `_KOPF` davor.
    """
    try:
        if sniff_bytes(inhalt) is typ:
            return inhalt
    except UnsupportedTypeError:
        pass
    return _KOPF[typ] + inhalt


def _anlegen(
    conn: psycopg.Connection[Any],
    tmp_path: Path,
    inhalt: bytes,
    *,
    name: str = "x.pdf",
    typ: DocumentType = DocumentType.PDF,
    lesen: ReadSettings = ReadSettings(),
) -> tuple[str, Path]:
    """Legt die Eingabe an, wie das Abschicken es tut (keine Ergebniszeile), und
    gibt Hash und einen Pfad zurueck, den es NICHT gibt.

    Der Inhalt bekommt den Dateikopf seines Typs (`_als`), denn der Job erkennt
    den Typ an den Bytes. Der Pfad ist ein Merkzeichen fuer Tests: Auf der Platte
    liegt nichts. Liest der Job die Eingabe von dort statt aus `inputs`, findet
    er nichts, und der Test faellt.
    """
    pfad = tmp_path / name
    daten = _als(typ, inhalt)
    sha = hashlib.sha256(daten).hexdigest()
    insert_input(conn, sha256=sha, content=daten, read_settings=lesen)
    return sha, pfad


def _eingabe_steht(conn: psycopg.Connection[Any], sha: str) -> bool:
    return (
        conn.execute("SELECT 1 FROM inputs WHERE sha256 = %s", (sha,)).fetchone()
        is not None
    )


@pytest.fixture
async def pool(_test_datenbank: str) -> Any:
    """Ein echter Verbindungspool auf die Test-Datenbank.

    Ein echter und kein nachgebauter: Der Executor gibt seine Verbindung
    ausdruecklich zwischen Lesen und Schreiben ZURUECK, und ob er das wirklich
    tut, laesst sich nur an einem Pool mit begrenzter Groesse pruefen.
    """
    p = AsyncConnectionPool(_test_datenbank, min_size=1, max_size=2, open=False)
    await p.open(wait=True)
    try:
        yield p
    finally:
        await p.close()


@pytest.fixture
def exec_settings(settings: Settings) -> Settings:
    return dataclasses.replace(
        settings,
        endpoint_id="testendpoint",
        poll_interval_seconds=0.01,
        balance_recheck_seconds=0.01,
    )


# --- Der Guard ------------------------------------------------------------


async def test_guard_haelt_fertige_dokumente_an(
    db_conn: psycopg.Connection[Any],
    async_conn: psycopg.AsyncConnection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Bei `done`, `empty` und `input_error` kehrt der Ablauf sofort zurueck und
    schickt NICHTS ab — sonst waere ein Wiederlauf nach dem finalen Schreiben ein
    vollstaendig neu bezahltes Dokument. Eine Eingabe, die neben dem Ergebnis
    wieder steht (ein Abschicken genau im Augenblick des Endschreibens), loescht
    er dabei."""
    for nr, status in enumerate(
        [DocumentStatus.DONE, DocumentStatus.EMPTY, DocumentStatus.INPUT_ERROR]
    ):
        sha, _ = _anlegen(
            db_conn, tmp_path, f"inhalt-{nr}".encode(), name=f"{nr}.png",
            typ=DocumentType.PNG,
        )
        await write_terminal_async(
            async_conn, sha256=sha, status=status, raw={"erste": nr},
            endpoint_id="testendpoint",
        )
        insert_input(db_conn, sha256=sha, content=b"wieder abgelegt", read_settings=ReadSettings())

        runpod = FalscherRunPod([ersatz.fertige_antwort()])
        await process_document(
            sha, settings=exec_settings, runpod=runpod, pool=pool
        )

        assert runpod.abgesendet == [], status
        zeile = get_document(db_conn, sha)
        assert zeile is not None
        assert zeile.raw == {"erste": nr}, status
        assert not _eingabe_steht(db_conn, sha), status


async def test_guard_laesst_failed_durch(
    db_conn: psycopg.Connection[Any],
    async_conn: psycopg.AsyncConnection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Bei `failed` wird wirklich neu verarbeitet.

    Der Gegentest zum vorigen, und der wichtigere: Stuende `failed` im Guard,
    waere jeder Retry von Hand ein Nichts-Tun, das als Erfolg verbucht wird — und
    ein gescheitertes Dokument koennte nie wieder laufen.
    """
    sha, _ = _anlegen(
        db_conn, tmp_path, b"bild", name="a.png", typ=DocumentType.PNG
    )
    await write_terminal_async(
        async_conn,
        sha256=sha,
        status=DocumentStatus.FAILED,
        raw={"versuch": 1},
        error="damals gescheitert",
        endpoint_id="testendpoint",
    )

    runpod = FalscherRunPod([ersatz.fertige_antwort("Jetzt geklappt")])
    await process_document(sha, settings=exec_settings, runpod=runpod, pool=pool)

    assert len(runpod.abgesendet) == 1
    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.DONE
    assert zeile.raw is not None
    assert zeile.raw["markdown"] == "Jetzt geklappt"


async def test_der_job_schickt_die_lese_einstellungen_seiner_eingabe(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Je Dokument, nicht je Daemon (Mineroo#14): Stufe, OCR-Modus und Bildanalyse
    kommen aus der Eingabe-Zeile, fuer jedes Teilstueck; was in der Einstellung
    des Daemons steht, wirkt auf keinen Job."""
    daemon = dataclasses.replace(
        exec_settings, tier="advanced", ocr_mode="ocr", image_analysis=True,
        max_pages_per_chunk=2,
    )
    bild, _ = _anlegen(
        db_conn, tmp_path, b"ein bild", name="a.png", typ=DocumentType.PNG,
        lesen=ReadSettings("basic", "txt", False),
    )
    pdf, _ = _anlegen(
        db_conn, tmp_path, _pdf(3), lesen=ReadSettings("advanced", "auto", False)
    )

    # Bild, dann die zwei Teilstuecke des PDFs (Seiten 0-1 und 2): Seit Mineroo#12
    # muss jede Antwort so viele Seiten tragen, wie geschickt wurden.
    runpod = FalscherRunPod(
        [
            ersatz.fertige_antwort("Text"),
            ersatz.fertige_antwort("Text", seiten=2),
            ersatz.fertige_antwort("Text"),
        ]
    )
    await process_document(bild, settings=daemon, runpod=runpod, pool=pool)
    await process_document(pdf, settings=daemon, runpod=runpod, pool=pool)

    assert runpod.lese_einstellungen == [
        ("basic", "txt", False),
        ("advanced", "auto", False),
        ("advanced", "auto", False),
    ]


# --- Eingang --------------------------------------------------------------


async def test_eingabe_kommt_aus_inputs_nicht_von_einem_pfad(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Die Bytes, die RunPod bekommt, sind die aus `inputs`. Auf der Platte liegt
    nichts (siehe `_anlegen`), und der Job liest trotzdem."""
    sha, pfad = _anlegen(
        db_conn, tmp_path, b"aus der datenbank", name="a.png", typ=DocumentType.PNG
    )
    assert not pfad.exists()

    runpod = FalscherRunPod([ersatz.fertige_antwort()])
    await process_document(sha, settings=exec_settings, runpod=runpod, pool=pool)

    assert [base64.b64decode(b) for b in runpod.abgesendet] == [
        _als(DocumentType.PNG, b"aus der datenbank")
    ]


async def test_eingabe_mit_falschem_hash_wird_erkannt(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Passen die gelesenen Bytes nicht zum Hash, endet das Dokument
    `input_error` mit Vermerk — und es wird nichts abgeschickt.

    Das kann nur eine beschaedigte Zeile sein: Der Job liest genau die Bytes, aus
    denen der Hash gebildet wurde. Wird es doch einmal falsch, darf das Ergebnis
    nicht unter einem fremden Hash abgelegt werden.
    """
    sha, _ = _anlegen(
        db_conn, tmp_path, b"urspruenglich", name="a.png", typ=DocumentType.PNG
    )
    db_conn.execute(
        "UPDATE inputs SET content = %b WHERE sha256 = %s",
        (b"inzwischen etwas ganz anderes", sha),
    )

    runpod = FalscherRunPod([ersatz.fertige_antwort()])
    with pytest.raises(CorruptDocumentError):
        await process_document(
            sha, settings=exec_settings, runpod=runpod, pool=pool
        )

    assert runpod.abgesendet == []
    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.INPUT_ERROR
    assert zeile.error is not None
    assert any(v["kind"] == "gescheitert" for v in zeile.notes)


async def test_kaputte_pdf_endet_input_error_auch_bei_leerem_konto(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Eine PDF, die sich nicht oeffnen laesst, endet `input_error`, ohne auf Geld
    zu warten.

    Das Einreihen oeffnet keine PDF mehr, die Pruefung geschieht erst im Job. Stuende
    die Kontostandsabfrage davor, haette eine kaputte PDF bei leerem Konto einen
    Platz des Daemons belegt, bis wieder Geld da ist, obwohl RunPod nie gefragt wird.
    """
    sha, _ = _anlegen(db_conn, tmp_path, b"%PDF-1.7\nkeine PDF, nur ein Kopf")

    runpod = FalscherRunPod([ersatz.fertige_antwort()], kontostand=0.0)
    with pytest.raises(CorruptDocumentError):
        await asyncio.wait_for(
            process_document(sha, settings=exec_settings, runpod=runpod, pool=pool),
            timeout=5,
        )

    assert runpod.kontakte == []
    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.INPUT_ERROR


def _passwortgeschuetzte_pdf() -> bytes:
    """Eine PDF mit `/Encrypt`, deren Passwort pdfium nicht kennt.

    Von Hand gebaut, weil pypdfium2 nicht verschluesselt und pypdf im Projekt
    verboten ist. `/O` und `/U` sind Fuellwerte: Das Passwort der leeren
    Zeichenkette passt nicht dazu, pdfium meldet „Incorrect password error".
    """
    objekte = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 300] >>",
        b"<< /Filter /Standard /V 1 /R 2 /P -4 /O <"
        + b"11" * 32
        + b"> /U <"
        + b"22" * 32
        + b"> >>",
    ]
    teile = [b"%PDF-1.4\n"]
    versaetze = []
    for nr, inhalt in enumerate(objekte, start=1):
        versaetze.append(sum(len(t) for t in teile))
        teile.append(b"%d 0 obj\n" % nr + inhalt + b"\nendobj\n")
    xref = sum(len(t) for t in teile)
    teile.append(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objekte) + 1))
    teile.extend(b"%010d 00000 n \n" % v for v in versaetze)
    teile.append(
        b"trailer\n<< /Size %d /Root 1 0 R /Encrypt 4 0 R /ID [<%s> <%s>] >>\n"
        % (len(objekte) + 1, b"aa" * 16, b"aa" * 16)
    )
    teile.append(b"startxref\n%d\n%%%%EOF\n" % xref)
    return b"".join(teile)


async def test_verschluesselte_pdf_endet_input_error_ohne_runpod_kontakt(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Eine passwortgeschuetzte PDF endet `input_error`, auch bei leerem Konto, und
    RunPod hoert nichts davon."""
    sha, _ = _anlegen(db_conn, tmp_path, _passwortgeschuetzte_pdf())

    runpod = FalscherRunPod([ersatz.fertige_antwort()], kontostand=0.0)
    with pytest.raises(CorruptDocumentError, match="PDF"):
        await asyncio.wait_for(
            process_document(sha, settings=exec_settings, runpod=runpod, pool=pool),
            timeout=5,
        )

    assert runpod.kontakte == []
    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.INPUT_ERROR
    assert zeile.error is not None


def _bild(dateiformat: str, anzahl: int) -> bytes:
    """Eine Bilddatei mit `anzahl` Bildern (Frames), in dem Format."""
    bilder = [Image.new("RGB", (8, 8), farbe) for farbe in ("red", "blue", "green")]
    puffer = io.BytesIO()
    # JPEG und BMP kennen kein `save_all`; sie tragen auch nur ein Bild.
    folge: dict[str, Any] = (
        {"save_all": True, "append_images": bilder[1:anzahl]}
        if dateiformat not in ("JPEG", "BMP")
        else {}
    )
    bilder[0].save(
        puffer,
        format=dateiformat,
        **folge,
        **({"lossless": True} if dateiformat == "WEBP" else {}),
    )
    return puffer.getvalue()


_BILDFORMATE = [
    ("TIFF", DocumentType.TIFF),
    ("GIF", DocumentType.GIF),
    ("WEBP", DocumentType.WEBP),
    ("PNG", DocumentType.PNG),
]


@pytest.mark.parametrize(("dateiformat", "typ"), _BILDFORMATE)
async def test_mehrbildige_bilddatei_endet_input_error_vor_jedem_runpod_kontakt(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
    dateiformat: str,
    typ: DocumentType,
) -> None:
    """Ein mehrseitiges Fax als TIFF (oder ein anderes Bild mit mehreren Bildern)
    endet `input_error` mit dem Hinweis, es als PDF einzureichen.

    Bei leerem Konto und mit Zeitgrenze: Die Pruefung geschieht VOR der
    Kontostandsabfrage, sonst wartete der Job auf Geld fuer eine Datei, die RunPod
    nie sieht. Ohne die Pruefung ginge nur das erste Bild hinaus und das Dokument
    endete als einseitiges `done`.
    """
    sha, _ = _anlegen(db_conn, tmp_path, _bild(dateiformat, 3), typ=typ)

    runpod = FalscherRunPod([ersatz.fertige_antwort()], kontostand=0.0)
    with pytest.raises(MultiImageError, match="PDF"):
        await asyncio.wait_for(
            process_document(sha, settings=exec_settings, runpod=runpod, pool=pool),
            timeout=5,
        )

    assert runpod.kontakte == []
    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.INPUT_ERROR
    assert zeile.error is not None and "PDF" in zeile.error


# Formate, die keine Folge von Seiten tragen koennen, gehoeren zur Gegenprobe: Sie
# duerfen nie als mehrbildig abgewiesen werden.
_EINBILD_FORMATE = [
    *_BILDFORMATE,
    ("JPEG", DocumentType.JPEG),
    ("BMP", DocumentType.BMP),
]


@pytest.mark.parametrize(("dateiformat", "typ"), _EINBILD_FORMATE)
async def test_einbildige_bilddatei_geht_unveraendert_hinaus(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
    dateiformat: str,
    typ: DocumentType,
) -> None:
    """Das Gegenstueck: Ein Bild mit einem Bild wird gelesen."""
    inhalt = _bild(dateiformat, 1)
    sha, _ = _anlegen(db_conn, tmp_path, inhalt, typ=typ)

    runpod = FalscherRunPod([ersatz.fertige_antwort()])
    await process_document(sha, settings=exec_settings, runpod=runpod, pool=pool)

    assert [base64.b64decode(b) for b in runpod.abgesendet] == [inhalt]
    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.DONE


async def test_jpeg_mit_zweitem_bild_geht_hinaus_denn_es_ist_keine_seitenfolge(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Eine JPEG-Datei mit einem zweiten eingebetteten Bild (Multi-Picture-Format,
    wie es Kameras und Telefone fuer Vorschau oder Tiefenkarte schreiben) ist ein
    Foto, kein mehrseitiges Dokument: Pillow zaehlt dort 2 Frames, die Datei wird
    aber gelesen und nicht abgewiesen."""
    inhalt = _bild("MPO", 2)
    sha, _ = _anlegen(db_conn, tmp_path, inhalt, typ=DocumentType.JPEG)

    runpod = FalscherRunPod([ersatz.fertige_antwort()])
    await process_document(sha, settings=exec_settings, runpod=runpod, pool=pool)

    assert [base64.b64decode(b) for b in runpod.abgesendet] == [inhalt]
    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.DONE


async def test_bild_das_pillow_nicht_lesen_kann_geht_trotzdem_hinaus(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Gezaehlt wird, was sich zaehlen laesst. Eine Bilddatei, die Pillow nicht
    oeffnet (die Endung stimmt, der Inhalt nicht), beweist keine mehreren Bilder;
    ob sie lesbar ist, entscheidet die Engine, nicht diese Pruefung."""
    sha, _ = _anlegen(
        db_conn, tmp_path, PNG_KOPF + b"abgeschnitten", typ=DocumentType.PNG
    )

    runpod = FalscherRunPod([ersatz.fertige_antwort()])
    await process_document(sha, settings=exec_settings, runpod=runpod, pool=pool)

    assert len(runpod.abgesendet) == 1


async def test_job_bestimmt_die_seitenzahl_und_schreibt_sie_mit_dem_ergebnis(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Vor dem Ende gibt es keine Zeile; der Job zaehlt die Seiten aus den Bytes
    und schreibt sie mit dem Endergebnis, dazu Typ, Groesse und Endpoint."""
    inhalt = _pdf(3)
    sha, _ = _anlegen(db_conn, tmp_path, inhalt)
    assert get_document(db_conn, sha) is None

    await process_document(
        sha,
        settings=exec_settings,
        runpod=FalscherRunPod([ersatz.fertige_antwort(seiten=3)]),
        pool=pool,
    )

    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.DONE
    assert zeile.page_count == 3
    assert zeile.mime == "application/pdf"
    assert zeile.size_bytes == len(inhalt)
    assert zeile.endpoint_id == "testendpoint"


async def test_seitenzahl_steht_auch_bei_failed(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Scheitert das Dokument nach dem Oeffnen, ist die Seitenzahl bekannt und
    wird mitgeschrieben."""
    sha, _ = _anlegen(db_conn, tmp_path, _pdf(2))
    runpod = FalscherRunPod([{"id": "j", "status": "FAILED", "error": "grund"}])
    with pytest.raises(TerminalRunPodError):
        await process_document(sha, settings=exec_settings, runpod=runpod, pool=pool)

    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.FAILED
    assert zeile.page_count == 2


@pytest.mark.parametrize(
    "antwort, erwartet",
    [
        (ersatz.fertige_antwort("Text"), DocumentStatus.DONE),
        (ersatz.fertige_antwort(""), DocumentStatus.EMPTY),
    ],
)
async def test_eingabe_wird_mit_dem_ergebnis_geloescht(
    antwort: dict[str, Any],
    erwartet: DocumentStatus,
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Nach `done` und `empty` ist die Eingabe weg."""
    sha, _ = _anlegen(
        db_conn, tmp_path, b"bild", name="a.png", typ=DocumentType.PNG
    )
    await process_document(
        sha, settings=exec_settings, runpod=FalscherRunPod([antwort]), pool=pool
    )

    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is erwartet
    assert db_conn.execute(
        "SELECT 1 FROM inputs WHERE sha256 = %s", (sha,)
    ).fetchone() is None


async def test_eingabe_ist_nach_input_error_geloescht(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Auch ein `input_error` ist ein Endergebnis: Ein Wiederlauf aenderte an der
    Eingabe nichts, also bleibt sie nicht liegen."""
    sha, _ = _anlegen(
        db_conn, tmp_path, b"kein pdf", name="kaputt.pdf", typ=DocumentType.PDF
    )
    with pytest.raises(CorruptDocumentError):
        await process_document(
            sha,
            settings=exec_settings,
            runpod=FalscherRunPod([ersatz.fertige_antwort()]),
            pool=pool,
        )

    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.INPUT_ERROR
    assert db_conn.execute(
        "SELECT 1 FROM inputs WHERE sha256 = %s", (sha,)
    ).fetchone() is None


async def test_eingabe_bleibt_nach_failed_stehen(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Nach `failed` steht die Eingabe noch: `retry` braucht etwas zu lesen."""
    sha, _ = _anlegen(
        db_conn, tmp_path, b"bild", name="a.png", typ=DocumentType.PNG
    )
    runpod = FalscherRunPod([{"id": "j", "status": "FAILED", "error": "grund"}])
    with pytest.raises(TerminalRunPodError):
        await process_document(sha, settings=exec_settings, runpod=runpod, pool=pool)

    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.FAILED
    gelesen = db_conn.execute(
        "SELECT content FROM inputs WHERE sha256 = %s", (sha,)
    ).fetchone()
    assert gelesen is not None
    assert bytes(gelesen[0]) == _als(DocumentType.PNG, b"bild")


async def test_wiederholbarer_fehler_laesst_die_eingabe_liegen(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Wird der Job neu vorgelegt, ohne dass ein Ergebnis geschrieben wurde, muss
    der naechste Lauf seine Eingabe noch finden."""
    sha, _ = _anlegen(
        db_conn, tmp_path, b"bild", name="a.png", typ=DocumentType.PNG
    )
    runpod = FalscherRunPod([{"id": "j", "status": "TIMED_OUT"}])
    with pytest.raises(TransientRunPodError):
        await process_document(sha, settings=exec_settings, runpod=runpod, pool=pool)

    assert db_conn.execute(
        "SELECT 1 FROM inputs WHERE sha256 = %s", (sha,)
    ).fetchone() is not None


async def test_nicht_pdf_laeuft_als_ein_teilstueck(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Ein Bild ergibt genau einen Chunk `(0, 0)`; es gibt keinen Sonderfall
    „war nicht geschnitten"."""
    sha, _ = _anlegen(
        db_conn, tmp_path, b"bilddaten", name="a.png", typ=DocumentType.PNG
    )
    runpod = FalscherRunPod([ersatz.fertige_antwort("Bildtext")])
    await process_document(sha, settings=exec_settings, runpod=runpod, pool=pool)

    assert len(runpod.abgesendet) == 1
    assert base64.b64decode(runpod.abgesendet[0]) == _als(DocumentType.PNG, b"bilddaten")
    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.DONE


async def test_kleines_pdf_geht_ungeschnitten_hinaus(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Ein PDF unter beiden Schwellen wird NICHT neu geschrieben.

    Es gehen die ORIGINAL-Bytes hinaus. Ein Ausschnitt waere byte-gleich im
    Inhalt, aber ein neu geschriebenes Dokument — unnoetige Arbeit an genau der
    Stelle, an der jede Umformung ein Risiko ist.
    """
    inhalt = _pdf(3)
    sha, _ = _anlegen(db_conn, tmp_path, inhalt)
    runpod = FalscherRunPod([ersatz.fertige_antwort(seiten=3)])
    await process_document(sha, settings=exec_settings, runpod=runpod, pool=pool)

    assert len(runpod.abgesendet) == 1
    assert base64.b64decode(runpod.abgesendet[0]) == inhalt


async def test_grosses_pdf_wird_geschnitten(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Ueber der Seiten-Schwelle wird zerlegt, und die Teilstuecke decken das
    Dokument vollstaendig ab."""
    eng = dataclasses.replace(exec_settings, max_pages_per_chunk=2)
    inhalt = _pdf(5)
    sha, _ = _anlegen(db_conn, tmp_path, inhalt)
    runpod = FalscherRunPod(
        [
            ersatz.fertige_antwort("Stueck", seiten=2),
            ersatz.fertige_antwort("Stueck", seiten=2),
            ersatz.fertige_antwort("Stueck"),
        ]
    )
    await process_document(sha, settings=eng, runpod=runpod, pool=pool)

    # 5 Seiten bei Schwelle 2 → 2 + 2 + 1.
    assert len(runpod.abgesendet) == 3
    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.DONE


# --- Absenden und Abholen -------------------------------------------------


async def test_hoechstens_ein_ergebnis_offen(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Zu keinem Zeitpunkt sind je Dokument zwei Ergebnisse unabgeholt.

    Die Zusicherung gegen das verpasste Abholfenster: nie absenden-und-spaeter-
    einsammeln. Genau das machten die alten Ad-hoc-Skripte anders.

    Geprueft am VERLAUF: Auf jedes `submit` muss unmittelbar sein `holen` folgen,
    bevor das naechste `submit` kommt. Ein Verlauf wie submit/submit/holen/holen
    waere die alte Bauart und wuerde hier auffallen.
    """
    eng = dataclasses.replace(exec_settings, max_pages_per_chunk=1)
    sha, _ = _anlegen(db_conn, tmp_path, _pdf(4))
    runpod = FalscherRunPod([ersatz.fertige_antwort()])
    await process_document(sha, settings=eng, runpod=runpod, pool=pool)

    assert runpod.verlauf == [
        "submit:job-0",
        "holen:job-0",
        "submit:job-1",
        "holen:job-1",
        "submit:job-2",
        "holen:job-2",
        "submit:job-3",
        "holen:job-3",
    ]


@pytest.mark.parametrize(
    "zustand, erwartet",
    [
        ("FAILED", TerminalRunPodError),
        ("CANCELLED", TerminalRunPodError),
        ("TIMED_OUT", TransientRunPodError),
    ],
)
async def test_endzustaende_werden_uebersetzt(
    zustand: str,
    erwartet: type[Exception],
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Nicht-COMPLETED-Endzustaende werden in die richtige Ausnahme uebersetzt.

    `FAILED` und `CANCELLED` sind endgueltig; `TIMED_OUT` ist wiederholbar, denn
    es kann an der Warteschlange gelegen haben. Der Unterschied entscheidet, ob
    das Dokument aufgegeben oder noch zweimal bezahlt wird.
    """
    sha, _ = _anlegen(
        db_conn, tmp_path, zustand.encode(), name=f"{zustand}.png",
        typ=DocumentType.PNG,
    )
    runpod = FalscherRunPod([{"id": "j", "status": zustand, "error": "grund"}])
    with pytest.raises(erwartet):
        await process_document(
            sha, settings=exec_settings, runpod=runpod, pool=pool
        )


async def test_ausgepackt_wird_nur_erfolgreiches(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """`unpack_response` sieht ausschliesslich `COMPLETED`-Antworten — die
    Uebersetzung liegt davor.

    Geprueft daran, dass eine FAILED-Antwort OHNE `output` nicht am Auspacken
    scheitert (was ein anderer Fehlertyp waere), sondern an der Uebersetzung.
    """
    sha, _ = _anlegen(
        db_conn, tmp_path, b"x", name="a.png", typ=DocumentType.PNG
    )
    runpod = FalscherRunPod([{"id": "j", "status": "FAILED"}])
    with pytest.raises(TerminalRunPodError) as fehler:
        await process_document(
            sha, settings=exec_settings, runpod=runpod, pool=pool
        )
    assert "FAILED" in str(fehler.value)
    assert "output" not in str(fehler.value).split("Volle Antwort")[0]


# --- Schreiben ------------------------------------------------------------


async def test_genau_ein_schreibvorgang(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Die Ergebniszeile wird GENAU EINMAL geschrieben: angelegt am Ende.

    Das ist die Zusicherung, an der das ganze Ein-Buch-Modell haengt: kein
    Zwischenstand, kein „laeuft gerade", keine Chunk-Zustaende in der Zeile.

    DIESER TEST HAT SIE NICHT GEPRUEFT. Er verglich `updated_at` vorher und
    nachher und verlangte „groesser" — das ist „MINDESTENS einmal". Zwei, drei
    oder zehn Schreibvorgaenge haetten ihn genauso bestanden, und die
    Begruendung im Docstring („Ein Zwischen-Update haette sie ein zweites Mal
    bewegt") stimmt schlicht nicht: Ein zweites Update bewegt denselben
    Zeitstempel ein zweites Mal weiter, und „weiter" bleibt „weiter".

    Gezaehlt wird jetzt wirklich, mit einem Zaehl-Ausloeser auf der Tabelle. Das
    ist die einzige Art, „genau einmal" zu pruefen, ohne dem Code zu glauben,
    den man prueft.
    """
    eng = dataclasses.replace(exec_settings, max_pages_per_chunk=1)
    sha, _ = _anlegen(db_conn, tmp_path, _pdf(3))
    assert get_document(db_conn, sha) is None

    # Ein Zaehler, der JEDES Anlegen und Aendern dieser Zeile mitschreibt —
    # unabhaengig davon, welche Spalten es anfasst.
    db_conn.execute("CREATE TABLE IF NOT EXISTS _schreibzaehler (sha256 text)")
    db_conn.execute("TRUNCATE _schreibzaehler")
    db_conn.execute(
        "CREATE OR REPLACE FUNCTION _zaehle_schreibvorgang() RETURNS trigger AS $$ "
        "BEGIN INSERT INTO _schreibzaehler VALUES (NEW.sha256); RETURN NEW; END; "
        "$$ LANGUAGE plpgsql"
    )
    db_conn.execute(
        "CREATE OR REPLACE TRIGGER _zaehl_trigger AFTER INSERT OR UPDATE ON documents "
        "FOR EACH ROW EXECUTE FUNCTION _zaehle_schreibvorgang()"
    )
    db_conn.commit()
    try:
        runpod = FalscherRunPod([ersatz.fertige_antwort()])
        await process_document(sha, settings=eng, runpod=runpod, pool=pool)

        zeile = db_conn.execute(
            "SELECT count(*) FROM _schreibzaehler WHERE sha256 = %s", (sha,)
        ).fetchone()
        assert zeile is not None
        # DREI Teilstuecke sind gelaufen — und trotzdem genau EIN Schreibvorgang.
        assert zeile[0] == 1, f"{zeile[0]} Schreibvorgaenge statt genau einem"
    finally:
        db_conn.execute("DROP TRIGGER IF EXISTS _zaehl_trigger ON documents")
        db_conn.commit()

    nachher = get_document(db_conn, sha)
    assert nachher is not None
    assert nachher.status is DocumentStatus.DONE


async def test_ergebnis_ist_zusammengesetzt(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Was geschrieben wird, ist EIN Dokument: Text in Reihenfolge, die Seiten
    des Middle-JSON mit den Seitenzahlen des Originals."""
    eng = dataclasses.replace(exec_settings, max_pages_per_chunk=1)
    sha, _ = _anlegen(db_conn, tmp_path, _pdf(3))

    # Drei Teilstuecke, jedes mit teilstueck-lokalem page_idx 0.
    runpod = FalscherRunPod(
        [
            ersatz.fertige_antwort("Seite A"),
            ersatz.fertige_antwort("Seite B"),
            ersatz.fertige_antwort("Seite C"),
        ]
    )
    await process_document(sha, settings=eng, runpod=runpod, pool=pool)

    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.raw is not None
    assert zeile.raw["markdown"] == "Seite A\n\nSeite B\n\nSeite C"
    # DIE Korrektur, um die es geht: aus dreimal „Seite 0" werden 0, 1, 2.
    assert [
        [s["page_idx"] for s in middle["pages"]] for middle in zeile.raw["middle_json"]
    ] == [[0], [1], [2]]
    # Und es gibt kein Feld, das die Teilung noch verriete.
    assert "chunks" not in zeile.raw
    assert "chunk_plan" not in zeile.raw


async def test_echte_mineru4_antworten_ergeben_ein_dokument(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Der zwoelfseitige Bericht geht als PDF hinein, der Job schneidet ihn in
    drei Teilstuecke (0-4, 5-9, 10-11), und RunPod antwortet mit den drei ECHTEN
    Antworten des MinerU-4-Endpoints genau dieser Teilstuecke, in `advanced` mit
    OCR (`artifacts/2026-10-26-mineru-4.0.12-endpoint/`). Heraus kommt EIN
    Dokument: zwoelf Seiten mit den Seitenzahlen des Originals und alle zwoelf
    Bilder, obwohl zwei Teilstuecke je fuenf gleich benannte tragen."""
    pdf = (ersatz.ECHT_ENDPOINT / "material" / "mehrteilig.pdf").read_bytes()
    eng = dataclasses.replace(exec_settings, max_pages_per_chunk=5)
    sha, _ = _anlegen(db_conn, tmp_path, pdf)
    runpod = FalscherRunPod([ersatz.echte_antwort(name) for name, _ in ersatz.echte_teilstuecke()])
    await process_document(sha, settings=eng, runpod=runpod, pool=pool)

    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.DONE
    assert zeile.raw is not None
    assert len(runpod.abgesendet) == 3
    assert [s["page_idx"] for m in zeile.raw["middle_json"] for s in m["pages"]] == list(range(12))
    assert len(zeile.raw["images"]) == 12
    for verweis in re.findall(r"!\[[^\]]*\]\(([^)]+)\)", zeile.raw["markdown"]):
        assert verweis in zeile.raw["images"]
    # Die echten Antworten melden MinerU 4.0.12 und genau die bestellten
    # Einstellungen: kein Vermerk (Mineroo#14).
    assert zeile.mineru_version == "4.0.12"
    assert zeile.read_settings == ReadSettings("advanced", "ocr", True)
    arten = {v["kind"] for v in zeile.notes}
    assert not arten & {"lese_einstellungen_abweichend", "mineru_versionen_verschieden"}


def _vermerk(zeile: Any, art: str) -> dict[str, Any]:
    (vermerk,) = [v for v in zeile.notes if v["kind"] == art]
    return dict(vermerk)


async def test_nicht_passende_gemeldete_einstellungen_ergeben_einen_vermerk(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Meldet der Worker Einstellungen, die nicht zur Bestellung passen, kommt ein
    Vermerk an die Zeile, und das Ergebnis gilt trotzdem (Mineroo#14). Der Vermerk
    nennt Bestellung und je abweichendem Teilstueck, was gemeldet wurde."""
    eng = dataclasses.replace(exec_settings, max_pages_per_chunk=2)
    sha, _ = _anlegen(db_conn, tmp_path, _pdf(3), lesen=ReadSettings("advanced", "ocr", True))
    runpod = FalscherRunPod(
        [
            ersatz.fertige_antwort("eins", seiten=2),
            ersatz.fertige_antwort("zwei", tier="flash", ocr_mode="txt", image_analysis=False),
        ]
    )
    await process_document(sha, settings=eng, runpod=runpod, pool=pool)

    zeile = get_document(db_conn, sha)
    assert zeile is not None and zeile.status is DocumentStatus.DONE
    vermerk = _vermerk(zeile, "lese_einstellungen_abweichend")
    assert vermerk["bestellt"] == {"tier": "advanced", "ocr_mode": "ocr", "image_analysis": True}
    assert vermerk["abweichend"] == [
        {
            "teilstueck": 1,
            "seiten": [2, 2],
            "gemeldet": {"tier": "flash", "ocr_mode": "txt", "image_analysis": False},
        }
    ]


async def test_auto_passt_zu_txt_wie_zu_ocr(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Bei `auto` waehlt MinerU den Modus je Datei und meldet `txt` oder `ocr`; beides
    passt zur Bestellung. Eine Meldung ganz ohne Einstellungen passt nicht."""
    eng = dataclasses.replace(exec_settings, max_pages_per_chunk=1)
    sha, _ = _anlegen(db_conn, tmp_path, _pdf(2), lesen=ReadSettings("advanced", "auto", True))
    runpod = FalscherRunPod(
        [ersatz.fertige_antwort("eins", ocr_mode="txt"), ersatz.fertige_antwort("zwei")]
    )
    await process_document(sha, settings=eng, runpod=runpod, pool=pool)
    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert all(v["kind"] != "lese_einstellungen_abweichend" for v in zeile.notes)

    ohne = ersatz.fertige_antwort("ohne")
    for feld in ("tier", "ocr_mode", "image_analysis"):
        del ohne["output"][feld]
    stumm, _ = _anlegen(db_conn, tmp_path, b"s", name="s.png", typ=DocumentType.PNG)
    await process_document(
        stumm, settings=exec_settings, runpod=FalscherRunPod([ohne]), pool=pool
    )
    zeile = get_document(db_conn, stumm)
    assert zeile is not None and zeile.status is DocumentStatus.DONE
    assert _vermerk(zeile, "lese_einstellungen_abweichend")["abweichend"][0]["gemeldet"] == {
        "tier": None,
        "ocr_mode": None,
        "image_analysis": None,
    }


async def test_verschiedene_versionen_in_den_teilstuecken_ergeben_einen_vermerk(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Gaben die Teilstuecke verschiedene MinerU-Versionen an, nennt ein Vermerk
    alle, und die Spalte traegt die niedrigste, nach PEP 440 verglichen: 4.0.9
    liegt vor 4.0.12 (Mineroo#14)."""
    eng = dataclasses.replace(exec_settings, max_pages_per_chunk=1)
    sha, _ = _anlegen(db_conn, tmp_path, _pdf(3))
    runpod = FalscherRunPod(
        [
            ersatz.fertige_antwort("a", mineru_version="4.0.12"),
            ersatz.fertige_antwort("b", mineru_version="4.0.9"),
            ersatz.fertige_antwort("c", mineru_version="4.0.12"),
        ]
    )
    await process_document(sha, settings=eng, runpod=runpod, pool=pool)

    zeile = get_document(db_conn, sha)
    assert zeile is not None and zeile.status is DocumentStatus.DONE
    assert zeile.mineru_version == "4.0.9"
    vermerk = _vermerk(zeile, "mineru_versionen_verschieden")
    assert vermerk["versionen"] == ["4.0.12", "4.0.9", "4.0.12"]
    assert vermerk["gespeichert"] == "4.0.9"


async def test_eine_gleiche_version_in_allen_teilstuecken_ergibt_keinen_vermerk(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    eng = dataclasses.replace(exec_settings, max_pages_per_chunk=1)
    sha, _ = _anlegen(db_conn, tmp_path, _pdf(2))
    runpod = FalscherRunPod([ersatz.fertige_antwort("a"), ersatz.fertige_antwort("b")])
    await process_document(sha, settings=eng, runpod=runpod, pool=pool)
    zeile = get_document(db_conn, sha)
    assert zeile is not None and zeile.mineru_version == ersatz.MINERU_VERSION
    assert all(v["kind"] != "mineru_versionen_verschieden" for v in zeile.notes)


@pytest.mark.parametrize(
    ("bestellt", "gemeldet"),
    [
        # Je Feld allein: Stufe, OCR-Modus, Bildanalyse.
        (ReadSettings("advanced", "ocr", True), {"tier": "standard"}),
        (ReadSettings("advanced", "ocr", True), {"ocr_mode": "txt"}),
        (ReadSettings("advanced", "txt", True), {"ocr_mode": "ocr"}),
        (ReadSettings("advanced", "ocr", True), {"image_analysis": False}),
        # Ein Text ist kein Wahrheitswert, auch wenn er wie einer aussieht.
        (ReadSettings("advanced", "ocr", True), {"image_analysis": "true"}),
        (ReadSettings("basic", "ocr", False), {"image_analysis": 0}),
    ],
)
async def test_schon_ein_abweichendes_feld_ergibt_den_vermerk(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
    bestellt: ReadSettings,
    gemeldet: dict[str, Any],
) -> None:
    """Jedes der drei Felder zaehlt fuer sich (Mineroo#14): Weicht nur eines ab,
    kommt der Vermerk, und die uebrigen gemeldet wie bestellt aendern daran nichts."""
    sha, _ = _anlegen(
        db_conn, tmp_path, b"x", name="a.png", typ=DocumentType.PNG, lesen=bestellt
    )
    wie_bestellt = {
        "tier": bestellt.tier,
        "ocr_mode": "ocr" if bestellt.ocr_mode == "auto" else bestellt.ocr_mode,
        "image_analysis": bestellt.image_analysis,
    }
    antwort = ersatz.fertige_antwort("Text", **{**wie_bestellt, **gemeldet})
    await process_document(
        sha, settings=exec_settings, runpod=FalscherRunPod([antwort]), pool=pool
    )

    zeile = get_document(db_conn, sha)
    assert zeile is not None and zeile.status is DocumentStatus.DONE
    (abweichend,) = _vermerk(zeile, "lese_einstellungen_abweichend")["abweichend"]
    assert abweichend["gemeldet"] == {**wie_bestellt, **gemeldet}


async def test_eine_unverwertbare_antwort_mit_anderen_einstellungen_ergibt_den_vermerk(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Auch eine bezahlte Antwort, die sich nicht auspacken liess, meldet, womit
    der Worker las (Mineroo#14); weicht das von der Bestellung ab, steht der
    Vermerk an der Zeile wie bei einem ausgepackten Teilstueck."""
    sha, _ = _anlegen(
        db_conn, tmp_path, b"x", name="a.png", typ=DocumentType.PNG,
        lesen=ReadSettings("advanced", "ocr", True),
    )
    dateien = ersatz.dokument_dateien(ersatz.middle_json([ersatz.seite(0, [])]))
    del dateien["markdown.md"]
    antwort = ersatz.tarball_antwort(
        dateien, tier="basic", ocr_mode="txt", image_analysis=False
    )
    with pytest.raises(TerminalRunPodError):
        await process_document(
            sha, settings=exec_settings, runpod=FalscherRunPod([antwort]), pool=pool
        )

    zeile = get_document(db_conn, sha)
    assert zeile is not None and zeile.status is DocumentStatus.FAILED
    vermerk = _vermerk(zeile, "lese_einstellungen_abweichend")
    assert vermerk["abweichend"] == [
        {
            "unverwertbar": 0,
            "seiten": [0, 0],
            "gemeldet": {"tier": "basic", "ocr_mode": "txt", "image_analysis": False},
        }
    ]
    assert zeile.notes[-1]["kind"] == "gescheitert"


async def test_eine_fehlerantwort_ohne_einstellungen_ergibt_keinen_vermerk(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
    echte_fehlerantwort: dict[str, Any],
) -> None:
    """Eine Fehlerantwort meldet keine Lese-Einstellungen (die echte des Endpoints
    nicht); daran ist nichts zu vergleichen, also kein Vermerk."""
    sha, _ = _anlegen(db_conn, tmp_path, b"x", name="a.png", typ=DocumentType.PNG)
    with pytest.raises(TerminalRunPodError):
        await process_document(
            sha, settings=exec_settings, runpod=FalscherRunPod([echte_fehlerantwort]),
            pool=pool,
        )
    zeile = get_document(db_conn, sha)
    assert zeile is not None and zeile.status is DocumentStatus.FAILED
    assert all(v["kind"] != "lese_einstellungen_abweichend" for v in zeile.notes)


async def test_eine_unlesbare_version_gilt_als_die_niedrigste(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Eine Angabe, die keine Versionsnummer nach PEP 440 ist, zaehlt als aelter als
    jede (Mineroo#14), und eine fehlende Meldung neben einer gemeldeten ergibt den
    Vermerk, ohne die Spalte zu leeren."""
    eng = dataclasses.replace(exec_settings, max_pages_per_chunk=1)
    unlesbar, _ = _anlegen(db_conn, tmp_path, _pdf(2))
    runpod = FalscherRunPod(
        [
            ersatz.fertige_antwort("a", mineru_version="4.0.12"),
            ersatz.fertige_antwort("b", mineru_version="vier-null"),
        ]
    )
    await process_document(unlesbar, settings=eng, runpod=runpod, pool=pool)
    zeile = get_document(db_conn, unlesbar)
    assert zeile is not None and zeile.mineru_version == "vier-null"
    assert _vermerk(zeile, "mineru_versionen_verschieden")["versionen"] == [
        "4.0.12", "vier-null"
    ]

    ohne = ersatz.fertige_antwort("d")
    del ohne["output"]["mineru_version"]
    fehlend, _ = _anlegen(db_conn, tmp_path, _pdf(3))
    runpod = FalscherRunPod(
        [ersatz.fertige_antwort("c", mineru_version="4.0.12"), ohne, ersatz.fertige_antwort("e")]
    )
    await process_document(fehlend, settings=eng, runpod=runpod, pool=pool)
    zeile = get_document(db_conn, fehlend)
    assert zeile is not None and zeile.mineru_version == "4.0.12"
    assert _vermerk(zeile, "mineru_versionen_verschieden")["versionen"] == [
        "4.0.12", None, "4.0.12"
    ]


async def test_eine_riesige_versionsangabe_haelt_das_ergebnis_nicht_auf(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Eine Ziffernfolge ueber Pythons Grenze fuer Ganzzahl-Umwandlung laesst
    `packaging` mit `ValueError` scheitern statt mit `InvalidVersion`; sie gilt
    als unlesbare Angabe, und das Dokument endet wie jedes andere."""
    riesig = "4." + "9" * 5000
    eng = dataclasses.replace(exec_settings, max_pages_per_chunk=1)
    sha, _ = _anlegen(db_conn, tmp_path, _pdf(2))
    runpod = FalscherRunPod(
        [
            ersatz.fertige_antwort("a", mineru_version="4.0.12"),
            ersatz.fertige_antwort("b", mineru_version=riesig),
        ]
    )
    await process_document(sha, settings=eng, runpod=runpod, pool=pool)
    zeile = get_document(db_conn, sha)
    assert zeile is not None and zeile.status is DocumentStatus.DONE
    assert zeile.mineru_version == riesig


async def test_ein_nullzeichen_in_den_meldungen_verhindert_das_ergebnis_nicht(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Meldet der Worker Version oder Einstellungen mit einem U+0000, kann Postgres
    weder `raw` noch den Text so speichern. Das Dokument endet trotzdem: `failed`
    ohne den Inhalt (Mineroo#27), und Spalte und Vermerk tragen das Gemeldete mit
    dem Nullzeichen sichtbar als `\\u0000`."""
    sha, _ = _anlegen(
        db_conn, tmp_path, b"x", name="a.png", typ=DocumentType.PNG,
        lesen=ReadSettings("advanced", "ocr", True),
    )
    antwort = ersatz.fertige_antwort(
        "Text", mineru_version="4.0.12\x00", tier="adv\x00anced"
    )
    with pytest.raises(UnstorableResultError):
        await process_document(
            sha, settings=exec_settings, runpod=FalscherRunPod([antwort]), pool=pool
        )

    zeile = get_document(db_conn, sha)
    assert zeile is not None and zeile.status is DocumentStatus.FAILED
    assert zeile.mineru_version == "4.0.12\\u0000"
    (abweichend,) = _vermerk(zeile, "lese_einstellungen_abweichend")["abweichend"]
    assert abweichend["gemeldet"]["tier"] == "adv\\u0000anced"
    assert any(v["kind"] == "ergebnis_nicht_speicherbar" for v in zeile.notes)


async def test_ein_nullzeichen_im_abweisungsgrund_verhindert_das_ergebnis_nicht(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Traegt der Grund, mit dem der Worker eine Datei abweist, ein U+0000, geht
    er in `error`, in den Fehler-Umschlag und in den Vermerk. Das Dokument endet
    trotzdem `input_error`, ohne den Inhalt (Mineroo#27), und der Grund steht mit
    dem Nullzeichen als `\\u0000` da; sonst scheiterte auch das Schreiben ohne
    Inhalt, und das Dokument bliebe ohne Ergebnis (Code-Review zur Spec #1)."""
    grund = "HtmlParseError: Steuerzeichen \x00 im Kopf"
    sha, _ = _anlegen(
        db_conn, tmp_path, ganzdokumente.JE_TYP[DocumentType.HTML], name="d.html",
        typ=DocumentType.HTML,
    )
    with pytest.raises(InputError):
        await process_document(
            sha,
            settings=exec_settings,
            runpod=FalscherRunPod([ersatz.fehlerantwort(grund)]),
            pool=pool,
        )

    zeile = get_document(db_conn, sha)
    assert zeile is not None and zeile.status is DocumentStatus.INPUT_ERROR
    assert zeile.error is not None and "Steuerzeichen \\u0000 im Kopf" in zeile.error
    assert zeile.raw is not None
    assert "\\u0000" in zeile.raw["failure"]["message"]
    assert any(v["kind"] == "ergebnis_nicht_speicherbar" for v in zeile.notes)


async def test_ein_nullzeichen_im_degraded_verhindert_das_ergebnis_nicht(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Ein `degraded`, das ein U+0000 traegt, geht in den Vermerk
    `unvollstaendige_antwort` (Mineroo#12). Das Dokument endet trotzdem, `failed`
    ohne den Inhalt, und der Vermerk traegt das Nullzeichen als `\\u0000`."""
    degraded = {
        "count": 1,
        "items": [
            {
                "artifact": "structured_content",
                "file": "structured_content.json",
                "reason": "un\x00readable",
                "error_type": "JSONDecodeError",
            }
        ],
    }
    sha, _ = _anlegen(db_conn, tmp_path, _echtes_material("normale_seite.pdf"))
    with pytest.raises(UnstorableResultError):
        await process_document(
            sha,
            settings=exec_settings,
            runpod=FalscherRunPod(
                [
                    ersatz.abgeleitete_antwort(
                        "normale_seite", ohne=("structured_content.json",), degraded=degraded
                    )
                ]
            ),
            pool=pool,
        )

    zeile = get_document(db_conn, sha)
    assert zeile is not None and zeile.status is DocumentStatus.FAILED
    vermerk = _vermerk(zeile, "unvollstaendige_antwort")
    assert vermerk["degraded"]["items"][0]["reason"] == "un\\u0000readable"


async def test_ein_einzelnes_surrogat_in_der_version_verhindert_das_ergebnis_nicht(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Ein einzelnes Surrogat (`"\\ud800"` im JSON der Antwort) ist kein Zeichen,
    das sich als UTF-8 schreiben laesst: psycopg weist es in der Spalte ab, Postgres
    in `jsonb`. Das Dokument endet trotzdem, `failed` ohne den Inhalt, und die
    Spalte traegt das Surrogat sichtbar als `\\ud800` (Code-Review zur Spec #1)."""
    sha, _ = _anlegen(
        db_conn, tmp_path, b"surrogat", name="s.png", typ=DocumentType.PNG
    )
    antwort = ersatz.fertige_antwort("Text", mineru_version="4.0.12\ud800")
    with pytest.raises(UnstorableResultError):
        await process_document(
            sha, settings=exec_settings, runpod=FalscherRunPod([antwort]), pool=pool
        )

    zeile = get_document(db_conn, sha)
    assert zeile is not None and zeile.status is DocumentStatus.FAILED
    assert zeile.mineru_version == "4.0.12\\ud800"
    assert any(v["kind"] == "ergebnis_nicht_speicherbar" for v in zeile.notes)


async def test_scheitert_das_endschreiben_stehen_die_lese_vermerke_einmal(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Der Erfolgsweg hat Version und Vermerke schon bestimmt, wenn das
    Endschreiben scheitert; der Fehlerzweig nimmt sie, statt sie ein zweites Mal
    anzuhaengen."""
    sha, _ = _anlegen(
        db_conn, tmp_path, b"x", name="a.png", typ=DocumentType.PNG,
        lesen=ReadSettings("advanced", "ocr", True),
    )
    antwort = ersatz.fertige_antwort("vor\x00nach", tier="flash", mineru_version="4.0.11")
    with pytest.raises(UnstorableResultError):
        await process_document(
            sha, settings=exec_settings, runpod=FalscherRunPod([antwort]), pool=pool
        )

    zeile = get_document(db_conn, sha)
    assert zeile is not None and zeile.status is DocumentStatus.FAILED
    arten = [v["kind"] for v in zeile.notes]
    assert arten.count("lese_einstellungen_abweichend") == 1
    assert zeile.mineru_version == "4.0.11"


# --- Vollstaendigkeit: Seiten und degraded (Mineroo#12) ------------------------

_DEGRADED_MIDDLE_JSON: dict[str, Any] = {
    "count": 1,
    "items": [
        {
            "artifact": "middle_json",
            "file": "middle_json.json",
            "reason": "unreadable",
            "error_type": "JSONDecodeError",
        }
    ],
}

_DEGRADED_STRUCTURED_CONTENT: dict[str, Any] = {
    "count": 1,
    "items": [
        {
            "artifact": "structured_content",
            "file": "structured_content.json",
            "reason": "unreadable",
            "error_type": "JSONDecodeError",
        }
    ],
}


@pytest.fixture
def degraded_middle_json() -> dict[str, Any]:
    """Eine Antwort, in der der Worker `middle_json.json` verloren meldet.

    ABGELEITET aus der echten Antwort `normale_seite`
    (`artifacts/2026-10-26-mineru-4.0.12-endpoint/antworten/`, Job
    `c7c9eae9-…-e2`, Eingabe `material/normale_seite.pdf`): Das Mitglied
    `middle_json.json` ist aus dem Archiv entfernt, und im Ergebnis-Eintrag ist
    das Feld `degraded` gesetzt, auf `_DEGRADED_MIDDLE_JSON`. Alles andere ist
    die echte Antwort. So liefert der Worker eine Datei, die er nicht lesen
    konnte, beim Transport `tarball_b64` (Job-Vertrag des Forks, `api.mdx`,
    „Incomplete responses": das Archiv ohne das Mitglied, `degraded` mit
    `count` und `items`). Abgeleitet, weil der Endpoint `degraded` nicht gezielt
    liefert: Die echten Antworten tragen keins, und erzwingen liesse es sich nur
    mit einer Datei, die beim Schreiben im Worker kaputtgeht."""
    return ersatz.abgeleitete_antwort(
        "normale_seite", ohne=("middle_json.json",), degraded=_DEGRADED_MIDDLE_JSON
    )


@pytest.fixture
def degraded_structured_content() -> dict[str, Any]:
    """Eine Antwort, in der der Worker eine Datei verloren meldet, die das Paket nicht braucht.

    ABGELEITET aus der echten Antwort `normale_seite` wie `degraded_middle_json`:
    Das Mitglied `structured_content.json` ist aus dem Archiv entfernt, und im
    Ergebnis-Eintrag ist das Feld `degraded` gesetzt, auf
    `_DEGRADED_STRUCTURED_CONTENT`. Alles andere ist die echte Antwort."""
    return ersatz.abgeleitete_antwort(
        "normale_seite",
        ohne=("structured_content.json",),
        degraded=_DEGRADED_STRUCTURED_CONTENT,
    )


def _echtes_material(name: str) -> bytes:
    return (ersatz.ECHT_ENDPOINT / "material" / name).read_bytes()


async def test_degraded_benoetigte_datei_endet_failed_mit_der_antwort_in_raw(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
    degraded_middle_json: dict[str, Any],
) -> None:
    """Der Fall aus dem Code-Review zu Mineroo#11 (A1): Der Worker meldet
    `middle_json.json` verloren. Frueher endete das Dokument `done`, und jede
    Darstellung scheiterte danach dauerhaft. Jetzt endet es `failed`, die
    bezahlte Antwort steht in `raw` (Text, `degraded`), und die Eingabe bleibt
    stehen: `retry` kann das Dokument neu lesen."""
    from mineru_ocr.errors import MissingArtifactError

    sha, _ = _anlegen(db_conn, tmp_path, _echtes_material("normale_seite.pdf"))
    with pytest.raises(MissingArtifactError):
        await process_document(
            sha,
            settings=exec_settings,
            runpod=FalscherRunPod([degraded_middle_json]),
            pool=pool,
        )

    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.FAILED
    assert zeile.error is not None and "MissingArtifactError" in zeile.error
    assert "middle_json.json" in zeile.error
    assert zeile.raw is not None
    assert "Geschaeftsverlauf" in zeile.raw["markdown"]
    assert len(zeile.raw["images"]) == 2
    assert zeile.raw["middle_json"] == [None]
    assert zeile.raw["extras"][0]["degraded"] == _DEGRADED_MIDDLE_JSON
    assert zeile.raw["failure"]["type"] == "MissingArtifactError"
    assert _eingabe_steht(db_conn, sha)


async def test_degraded_andere_datei_endet_done_mit_vermerk(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
    degraded_structured_content: dict[str, Any],
) -> None:
    """Eine Datei, die das Paket nicht braucht, in `degraded`: Das Dokument wird
    `done`, und `notes` vermerkt das `degraded` samt Teilstueck und Job, damit
    der Verlust nicht nur tief in `raw` steht."""
    sha, _ = _anlegen(db_conn, tmp_path, _echtes_material("normale_seite.pdf"))
    await process_document(
        sha,
        settings=exec_settings,
        runpod=FalscherRunPod([degraded_structured_content]),
        pool=pool,
    )

    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.DONE
    vermerke = [v for v in zeile.notes if v.get("kind") == "unvollstaendige_antwort"]
    assert len(vermerke) == 1
    assert vermerke[0]["degraded"] == _DEGRADED_STRUCTURED_CONTENT
    assert vermerke[0]["teilstueck"] == {"first_page": 0, "last_page": 0}
    assert vermerke[0]["job_id"] == "job-0"
    assert zeile.raw is not None
    assert zeile.raw["structured_content"] == [None]


async def test_intakte_antwort_hat_keinen_vermerk(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """`degraded` fehlt in einer intakten Antwort, also gibt es nichts zu
    vermerken: Die echte normale Seite endet `done` ohne Vermerk."""
    sha, _ = _anlegen(db_conn, tmp_path, _echtes_material("normale_seite.pdf"))
    await process_document(
        sha,
        settings=exec_settings,
        runpod=FalscherRunPod([ersatz.echte_antwort("normale_seite")]),
        pool=pool,
    )

    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.DONE
    assert not [v for v in zeile.notes if v.get("kind") == "unvollstaendige_antwort"]


async def test_verworfene_seite_im_mehrteiler_endet_failed_ohne_weitere_kosten(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Der zwoelfseitige Bericht in drei Teilstuecken (0-4, 5-9, 10-11), und im
    zweiten verarbeitet MinerU nur vier von fuenf Seiten (abgeleitet aus der
    echten Antwort `mehrteilig_1_seiten_5-9`: die Seite mit `page_idx` 2 aus dem
    Middle-JSON entfernt). Das Dokument endet `failed`, sobald das zweite
    Teilstueck da ist: Das dritte wird nicht mehr bezahlt. Die zwei bezahlten
    Antworten stehen zusammengesetzt in `raw`, mit den Seitenzahlen des
    Originals; Seite 7 fehlt, und die Meldung nennt sie."""
    from mineru_ocr.errors import MissingPagesError

    def ohne_seite_2(middle: dict[str, Any]) -> dict[str, Any]:
        middle["pages"] = [s for s in middle["pages"] if s["page_idx"] != 2]
        return middle

    eng = dataclasses.replace(exec_settings, max_pages_per_chunk=5)
    sha, _ = _anlegen(db_conn, tmp_path, _echtes_material("mehrteilig.pdf"))
    runpod = FalscherRunPod(
        [
            ersatz.echte_antwort("mehrteilig_0_seiten_0-4"),
            ersatz.abgeleitete_antwort(
                "mehrteilig_1_seiten_5-9", middle_json_aendern=ohne_seite_2
            ),
            ersatz.echte_antwort("mehrteilig_2_seiten_10-11"),
        ]
    )
    with pytest.raises(MissingPagesError):
        await process_document(sha, settings=eng, runpod=runpod, pool=pool)

    assert len(runpod.abgesendet) == 2
    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.FAILED
    assert zeile.page_count == 12
    assert zeile.error is not None and "[7]" in zeile.error
    assert zeile.raw is not None
    assert [[s["page_idx"] for s in m["pages"]] for m in zeile.raw["middle_json"]] == [
        [0, 1, 2, 3, 4],
        [5, 6, 8, 9],
    ]
    assert _eingabe_steht(db_conn, sha)


async def test_ungeschnittene_pdf_mit_fehlender_seite_endet_failed(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Auch ungeschnitten ist die PDF ein Teilstueck: drei Seiten geschickt, zwei
    verarbeitet, also `failed` statt `done`."""
    from mineru_ocr.errors import MissingPagesError

    sha, _ = _anlegen(db_conn, tmp_path, _pdf(3))
    with pytest.raises(MissingPagesError):
        await process_document(
            sha,
            settings=exec_settings,
            runpod=FalscherRunPod([ersatz.fertige_antwort(seiten=2)]),
            pool=pool,
        )

    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.FAILED


async def test_bild_zaehlt_als_eine_seite(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Ein Bild ist eine Seite. Die echte leere Seite als PNG (eine Seite ohne
    Bloecke) endet `empty`; eine Antwort ohne jede Seite endet `failed`, denn
    dann hat MinerU das Bild nicht verarbeitet."""
    from mineru_ocr.errors import MissingPagesError

    sha, _ = _anlegen(
        db_conn, tmp_path, _echtes_material("leere_seite.png"), name="a.png",
        typ=DocumentType.PNG,
    )
    await process_document(
        sha,
        settings=exec_settings,
        runpod=FalscherRunPod([ersatz.echte_antwort("leere_seite_bild")]),
        pool=pool,
    )
    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.EMPTY

    def ohne_seiten(middle: dict[str, Any]) -> dict[str, Any]:
        middle["pages"] = []
        return middle

    sha, _ = _anlegen(
        db_conn, tmp_path, b"anderes bild", name="b.png", typ=DocumentType.PNG
    )
    with pytest.raises(MissingPagesError):
        await process_document(
            sha,
            settings=exec_settings,
            runpod=FalscherRunPod(
                [
                    ersatz.abgeleitete_antwort(
                        "leere_seite_bild", middle_json_aendern=ohne_seiten
                    )
                ]
            ),
            pool=pool,
        )
    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.FAILED


async def test_ganzdokument_format_hat_keine_seitenzahl(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Ein DOCX liest MinerU als ganzes Dokument; eine Seitenzahl kennt das Paket
    dafuer nicht, und der Vergleich entfaellt. Die echte DOCX-Antwort endet
    `done`."""
    sha, _ = _anlegen(
        db_conn, tmp_path, _echtes_material("dokument.docx"), name="d.docx",
        typ=DocumentType.DOCX,
    )
    await process_document(
        sha,
        settings=exec_settings,
        runpod=FalscherRunPod([ersatz.echte_antwort("dokument_docx")]),
        pool=pool,
    )
    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.DONE


@pytest.mark.parametrize(
    "material, aufnahme, typ",
    [
        ("dokument.docx", "dokument_docx", DocumentType.DOCX),
        ("seite.html", "seite_html", DocumentType.HTML),
    ],
)
async def test_echte_ganzdokument_antworten_ohne_einstellungen_und_vermerk(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
    material: str,
    aufnahme: str,
    typ: DocumentType,
) -> None:
    """Die aufgenommenen DOCX- und HTML-Antworten (Mineroo#17): Der Worker liest
    sie als `flash` mit `txt`, gleich was bestellt war. Die Lese-Einstellungen
    wirken bei Ganzdokument-Formaten nicht; gespeichert werden keine, und es gibt
    keinen Vermerk. Seitenzahl gibt es keine, die Version schon."""
    sha, _ = _anlegen(
        db_conn, tmp_path, _echtes_material(material), name=material, typ=typ,
        lesen=ReadSettings("advanced", "ocr", True),
    )
    await process_document(
        sha,
        settings=exec_settings,
        runpod=FalscherRunPod([ersatz.echte_antwort(aufnahme)]),
        pool=pool,
    )
    zeile = get_document(db_conn, sha)
    assert zeile is not None and zeile.status is DocumentStatus.DONE
    assert (zeile.tier, zeile.ocr_mode, zeile.image_analysis) == (None, None, None)
    assert zeile.read_settings is None
    assert zeile.page_count is None
    assert zeile.mime == typ.mime
    assert zeile.mineru_version == "4.0.12"
    assert all(v["kind"] != "lese_einstellungen_abweichend" for v in zeile.notes)


@pytest.mark.parametrize("typ", list(ganzdokumente.JE_TYP), ids=lambda t: t.value)
async def test_ganzdokument_formate_gehen_ungeteilt_als_ein_stueck(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
    typ: DocumentType,
) -> None:
    """Jedes Ganzdokument-Format geht ungeteilt als EIN Stueck an den Worker, mit
    genau den Bytes der Eingabe (Mineroo#17). Die Antwort ist die aufgenommene
    DOCX-Antwort: MinerU liest alle Ganzdokument-Formate denselben Weg, ganz und
    in `flash`. Gespeichert werden keine Lese-Einstellungen, auch nicht die
    gesendeten, und es gibt keinen Vermerk."""
    daten = ganzdokumente.JE_TYP[typ]
    sha, _ = _anlegen(
        db_conn, tmp_path, daten, name="d", typ=typ, lesen=ReadSettings("basic", "txt", False)
    )
    runpod = FalscherRunPod([ersatz.echte_antwort("dokument_docx")])
    await process_document(sha, settings=exec_settings, runpod=runpod, pool=pool)

    assert [base64.b64decode(gesendet) for gesendet in runpod.abgesendet] == [daten]
    zeile = get_document(db_conn, sha)
    assert zeile is not None and zeile.status is DocumentStatus.DONE
    assert zeile.read_settings is None
    assert (zeile.tier, zeile.ocr_mode, zeile.image_analysis) == (None, None, None)
    assert zeile.mime == typ.mime and zeile.page_count is None
    assert all(v["kind"] != "lese_einstellungen_abweichend" for v in zeile.notes)


async def test_bei_ganzdokument_formaten_entfaellt_der_seitenvergleich(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Ganzdokument-Formate haben keine Seitenzahl, der Vergleich der Seiten
    entfaellt (Mineroo#17). Abgeleitet aus der echten DOCX-Antwort: ihr
    Middle-JSON traegt drei logische Seiten mit Luecke (0, 3, 7). Bei einem Bild
    endete das `failed`; das DOCX endet `done`."""

    def seiten_mit_luecke(middle: dict[str, Any]) -> dict[str, Any]:
        (seite,) = middle["pages"]
        middle["pages"] = [{**seite, "page_idx": nummer} for nummer in (0, 3, 7)]
        return middle

    sha, _ = _anlegen(
        db_conn, tmp_path, _echtes_material("dokument.docx"), name="d.docx",
        typ=DocumentType.DOCX,
    )
    await process_document(
        sha,
        settings=exec_settings,
        runpod=FalscherRunPod(
            [ersatz.abgeleitete_antwort("dokument_docx", middle_json_aendern=seiten_mit_luecke)]
        ),
        pool=pool,
    )
    zeile = get_document(db_conn, sha)
    assert zeile is not None and zeile.status is DocumentStatus.DONE


@pytest.mark.parametrize("typ", [DocumentType.OLE2, DocumentType.ODT, DocumentType.HTML])
async def test_vom_worker_abgewiesenes_ganzdokument_endet_input_error_mit_seinem_grund(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
    typ: DocumentType,
) -> None:
    """Die Vorerkennung laesst grob durch; was MinerU dann doch nicht liest (ein
    OLE2-Container ohne Office-Daten etwa), weist der Worker ab. Die Datei endet
    `input_error` mit dem Grund aus der Antwort, ueber die eine Entscheidung in
    `errors` (Mineroo#17), und ohne gespeicherte Lese-Einstellungen."""
    grund = (
        "ValueError: input is not a file type MinerU 4.0.12 reads (detected "
        "'unknown'). It reads PDF, images, Office, OpenDocument, RTF, EPUB, OFD, "
        "HTML and MHTML files."
    )
    sha, _ = _anlegen(db_conn, tmp_path, ganzdokumente.JE_TYP[typ], name="d", typ=typ)
    with pytest.raises(InputError):
        await process_document(
            sha,
            settings=exec_settings,
            runpod=FalscherRunPod([ersatz.fehlerantwort(grund)]),
            pool=pool,
        )
    zeile = get_document(db_conn, sha)
    assert zeile is not None and zeile.status is DocumentStatus.INPUT_ERROR
    assert zeile.error is not None and grund in zeile.error
    assert zeile.read_settings is None
    assert zeile.mime == typ.mime


async def test_echte_abweisung_eines_ole2_containers_ohne_word_daten_endet_input_error(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Am Endpoint aufgenommen (Mineroo#17,
    `artifacts/2026-10-27-ganzdokumente/`): Ein OLE2-Container, dem der Strom
    `WordDocument` fehlt, geht durch die Vorerkennung, MinerU versucht ihn als
    DOC zu lesen und scheitert mit DocVortex' `LegacyOfficeMissingPartError`.
    Das ist eine Abweisung der Datei, kein Fehler des Laufs: Das Dokument endet
    `input_error` mit dem Grund aus der Antwort, und die Antwort steht in `raw`."""
    material = (ersatz.ECHT_GANZDOKUMENTE / "material" / "ohne_word_daten.cfb").read_bytes()
    antwort = ersatz.echte_antwort("ole2_ohne_word_daten", ersatz.ECHT_GANZDOKUMENTE)
    sha, _ = _anlegen(db_conn, tmp_path, material, name="d.cfb", typ=DocumentType.OLE2)
    with pytest.raises(InputError):
        await process_document(
            sha, settings=exec_settings, runpod=FalscherRunPod([antwort]), pool=pool
        )
    zeile = get_document(db_conn, sha)
    assert zeile is not None and zeile.status is DocumentStatus.INPUT_ERROR
    assert zeile.error is not None
    assert "missing required OLE stream: WordDocument" in zeile.error
    assert zeile.raw is not None
    assert zeile.raw["unverwertbare_antworten"][0]["antwort"] == antwort
    assert zeile.read_settings is None


async def test_der_waechter_speichert_bei_ganzdokument_formaten_keine_einstellungen(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Auch das `failed` des Waechters (`write_failure_if_no_result`) traegt fuer
    ein Ganzdokument-Format keine Lese-Einstellungen (Mineroo#17); fuer ein Bild
    die bestellten, wie bisher."""
    bestellt = ReadSettings("basic", "txt", False)
    ganz, _ = _anlegen(
        db_conn, tmp_path, ganzdokumente.JE_TYP[DocumentType.RTF], name="d.rtf",
        typ=DocumentType.RTF, lesen=bestellt,
    )
    bild, _ = _anlegen(
        db_conn, tmp_path, b"b", name="b.png", typ=DocumentType.PNG, lesen=bestellt
    )
    for sha in (ganz, bild):
        assert await write_failure_if_no_result(
            sha, settings=exec_settings, pool=pool, grund="Worker wiederholt abgestuerzt"
        )
    zeile = get_document(db_conn, ganz)
    assert zeile is not None and zeile.status is DocumentStatus.FAILED
    assert zeile.read_settings is None
    zeile = get_document(db_conn, bild)
    assert zeile is not None and zeile.read_settings == bestellt


async def test_ganzdokument_format_ohne_middle_json_endet_failed(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Die benoetigten Dateien gelten fuer jedes Format: Fehlt der echten
    DOCX-Antwort `middle_json.json` (abgeleitet: das Mitglied aus dem Archiv
    entfernt, ohne `degraded`), endet auch sie `failed`."""
    from mineru_ocr.errors import MissingArtifactError

    sha, _ = _anlegen(
        db_conn, tmp_path, _echtes_material("dokument.docx"), name="d.docx",
        typ=DocumentType.DOCX,
    )
    with pytest.raises(MissingArtifactError):
        await process_document(
            sha,
            settings=exec_settings,
            runpod=FalscherRunPod(
                [ersatz.abgeleitete_antwort("dokument_docx", ohne=("middle_json.json",))]
            ),
            pool=pool,
        )
    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.FAILED


async def test_unlesbarer_degraded_eintrag_verdeckt_keine_benoetigte_datei(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Ein Eintrag von `degraded.items`, der kein Objekt ist, darf den gueltigen
    daneben nicht verdecken (Code-Review zu Mineroo#12). Abgeleitet aus der
    echten Antwort `normale_seite`: `middle_json.json` bleibt im Archiv (wie der
    leere Ersatzwert beim Inline-Transport), und `degraded` nennt es neben einem
    kaputten Eintrag verloren. Das Dokument endet `failed`, nicht `done`."""
    from mineru_ocr.errors import MissingArtifactError

    gemischt = {"count": 2, "items": ["kaputt", *_DEGRADED_MIDDLE_JSON["items"]]}
    sha, _ = _anlegen(db_conn, tmp_path, _echtes_material("normale_seite.pdf"))
    with pytest.raises(MissingArtifactError):
        await process_document(
            sha,
            settings=exec_settings,
            runpod=FalscherRunPod(
                [ersatz.abgeleitete_antwort("normale_seite", degraded=gemischt)]
            ),
            pool=pool,
        )
    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.FAILED


def _ohne_seitenliste(middle: dict[str, Any]) -> dict[str, Any]:
    del middle["pages"]
    return middle


def _seite_ohne_blockliste(middle: dict[str, Any]) -> dict[str, Any]:
    middle["pages"][0]["blocks"] = None
    return middle


@pytest.mark.parametrize(
    ("material", "antwort", "typ", "aendern"),
    [
        ("dokument.docx", "dokument_docx", DocumentType.DOCX, _ohne_seitenliste),
        ("normale_seite.pdf", "normale_seite", DocumentType.PDF, _seite_ohne_blockliste),
    ],
    ids=["docx-ohne-seitenliste", "pdf-seite-ohne-blockliste"],
)
async def test_nicht_darstellbares_middle_json_endet_failed(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
    material: str,
    antwort: str,
    typ: DocumentType,
    aendern: Any,
) -> None:
    """Da ist nicht genug: Das Middle-JSON muss die Form haben, die die
    Darstellung liest (Code-Review zu Mineroo#12). Ein DOCX ohne Seitenliste und
    eine PDF-Seite mit `blocks: null` (abgeleitet aus den echten Antworten)
    endeten sonst `done`, und `rendered_markdown` scheiterte danach dauerhaft."""
    from mineru_ocr.errors import MissingArtifactError

    sha, _ = _anlegen(
        db_conn, tmp_path, _echtes_material(material), name=f"d.{typ.name.lower()}",
        typ=typ,
    )
    with pytest.raises(MissingArtifactError):
        await process_document(
            sha,
            settings=exec_settings,
            runpod=FalscherRunPod(
                [ersatz.abgeleitete_antwort(antwort, middle_json_aendern=aendern)]
            ),
            pool=pool,
        )
    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.FAILED
    assert zeile.raw is not None
    assert zeile.raw["failure"]["type"] == "MissingArtifactError"


async def test_leeres_ergebnis_wird_empty(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Ein Dokument ohne jeden Inhalt endet `empty`, nicht `done` und nicht
    `failed`.

    Beide Verwechslungen sind in den Ad-hoc-Skripten je einmal wirklich
    passiert: als Erfolg getarnt ist es unsichtbarer Verlust, als Fehler getarnt
    endlose Wiedervorlage mit Doppelkosten.
    """
    sha, _ = _anlegen(
        db_conn, tmp_path, b"leerbild", name="a.png", typ=DocumentType.PNG
    )
    runpod = FalscherRunPod([ersatz.fertige_antwort("")])
    await process_document(sha, settings=exec_settings, runpod=runpod, pool=pool)

    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.EMPTY


async def test_image_tag_wird_mitgeschrieben(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """`image_tag_at_submit` ist nach dem Lauf gesetzt.

    Ohne diesen Weg bliebe die Spalte fuer immer leer — und die Frage, fuer die
    sie da ist, waere nach Wochen nicht mehr entscheidbar.
    """
    sha, _ = _anlegen(
        db_conn, tmp_path, b"x", name="a.png", typ=DocumentType.PNG
    )
    runpod = FalscherRunPod([ersatz.fertige_antwort()], marke=":abc12345")
    await process_document(sha, settings=exec_settings, runpod=runpod, pool=pool)

    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.image_tag_at_submit == ":abc12345"


async def test_die_zeile_traegt_mineru_version_und_bestellte_lese_einstellungen(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Zu jedem Ergebnis die MinerU-Version, die der Worker meldet, und die
    Lese-Einstellungen, die das Abschicken bestellt hat (Mineroo#14), neben dem
    Image-Tag wie bisher. Was MinerU tatsaechlich nahm (bei `auto` den Modus, den
    es fuer die Datei waehlte), steht im Rohergebnis."""
    bestellt = ReadSettings("advanced", "auto", True)
    sha, _ = _anlegen(
        db_conn, tmp_path, b"x", name="a.png", typ=DocumentType.PNG, lesen=bestellt
    )
    runpod = FalscherRunPod(
        [ersatz.fertige_antwort(ocr_mode="txt", mineru_version="4.0.12")],
        marke=":abc12345",
    )
    await process_document(sha, settings=exec_settings, runpod=runpod, pool=pool)

    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.mineru_version == "4.0.12"
    assert (zeile.tier, zeile.ocr_mode, zeile.image_analysis) == ("advanced", "auto", True)
    assert zeile.read_settings == bestellt
    assert zeile.image_tag_at_submit == ":abc12345"
    assert zeile.raw is not None
    assert zeile.raw["envelopes"][0]["output_meta"]["ocr_mode"] == "txt"


async def test_auch_ein_gescheitertes_dokument_traegt_version_und_bestellte_einstellungen(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """„Zu jedem Ergebnis": auch zu `input_error` und `failed`, ohne Lauf (der
    Waechter) und mit einem. Die Version ist die, die der Worker meldete, auch in
    einer Fehlerantwort; ohne Antwort gibt es keine."""
    bestellt = ReadSettings("basic", "txt", False)
    kaputt, _ = _anlegen(db_conn, tmp_path, b"%PDF-1.7\nnur ein Kopf", lesen=bestellt)
    with pytest.raises(CorruptDocumentError):
        await process_document(
            kaputt, settings=exec_settings, runpod=FalscherRunPod([]), pool=pool
        )
    zeile = get_document(db_conn, kaputt)
    assert zeile is not None and zeile.status is DocumentStatus.INPUT_ERROR
    assert zeile.read_settings == bestellt
    assert zeile.mineru_version is None

    liegen, _ = _anlegen(
        db_conn, tmp_path, b"l", name="l.png", typ=DocumentType.PNG, lesen=bestellt
    )
    await write_failure_if_no_result(
        liegen, settings=exec_settings, pool=pool, grund="Worker wiederholt abgestuerzt"
    )
    zeile = get_document(db_conn, liegen)
    assert zeile is not None and zeile.read_settings == bestellt

    abgewiesen, _ = _anlegen(
        db_conn, tmp_path, b"f", name="f.png", typ=DocumentType.PNG, lesen=bestellt
    )
    runpod = FalscherRunPod([ersatz.fehlerantwort("RuntimeError: kaputt im Worker")])
    with pytest.raises(TerminalRunPodError):
        await process_document(abgewiesen, settings=exec_settings, runpod=runpod, pool=pool)
    zeile = get_document(db_conn, abgewiesen)
    assert zeile is not None and zeile.status is DocumentStatus.FAILED
    assert zeile.read_settings == bestellt
    assert zeile.mineru_version == ersatz.MINERU_VERSION


async def test_image_tag_wird_auch_bei_einer_pdf_mitgeschrieben(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Die PDF-Strecke liest die Herkunft des Images an ihrer eigenen Stelle; ohne
    diesen Test liesse sich der Aufruf dort loeschen, ohne dass einer scheitert."""
    sha, _ = _anlegen(db_conn, tmp_path, _pdf(1))
    runpod = FalscherRunPod([ersatz.fertige_antwort()], marke=":pdf12345")
    await process_document(sha, settings=exec_settings, runpod=runpod, pool=pool)

    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.image_tag_at_submit == ":pdf12345"
    # Zuerst die Herkunft, dann der Kontostand, dann erst bezahlt.
    assert runpod.kontakte[0] == "image_tag"
    assert runpod.kontakte[1] == "balance"


async def test_die_bildfrage_laeuft_nicht_auf_dem_thread_der_ereignisschleife(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pillow liest die Bytes eines Bildes in einem Thread: Ein boesartig gebautes
    GIF (Kommentarbloecke bis zur Groessengrenze) brauchte Sekunden bis Minuten und
    hielt sonst die Ereignisschleife des Daemons mit allen anderen Dokumenten an."""
    from mineru_ocr import images

    threads: list[int] = []

    def _merken(daten: bytes) -> bool:
        threads.append(threading.get_ident())
        return False

    monkeypatch.setattr(images, "has_several_images", _merken)
    sha, _ = _anlegen(db_conn, tmp_path, _bild("TIFF", 1), typ=DocumentType.TIFF)

    runpod = FalscherRunPod([ersatz.fertige_antwort()])
    await process_document(sha, settings=exec_settings, runpod=runpod, pool=pool)

    assert len(threads) == 1
    assert threads[0] != threading.get_ident()


async def test_zu_grosses_bild_wird_abgelehnt_bevor_pillow_es_liest(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Die Groessenpruefung ist billig, das Lesen mit Pillow nicht (ein GIF mit
    Kommentarbloecken brauchte Sekunden bis Minuten): Die Groesse kommt zuerst."""
    from mineru_ocr import images
    from mineru_ocr.errors import TooLargeError

    def _nicht_lesen(daten: bytes) -> bool:
        pytest.fail("Pillow liest ein Bild, das ohnehin zu gross ist")

    monkeypatch.setattr(images, "has_several_images", _nicht_lesen)
    zu_gross = b"GIF89a" + b"x" * (exec_settings.max_payload_bytes + 1000)
    sha, _ = _anlegen(
        db_conn, tmp_path, zu_gross, name="riesig.gif", typ=DocumentType.GIF
    )

    runpod = FalscherRunPod([ersatz.fertige_antwort()])
    with pytest.raises(TooLargeError):
        await process_document(sha, settings=exec_settings, runpod=runpod, pool=pool)

    assert runpod.kontakte == []


async def test_unlesbarer_image_tag_kostet_kein_dokument(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Scheitert das Lesen der Herkunft, laeuft das Dokument trotzdem durch.

    Die Angabe ist wertvoll, aber sie ist nicht das Ergebnis. Ein Netzwackeln
    beim Lesen einer Nebenangabe darf keinen bezahlten Parse kosten — der
    Vermerk sagt dann, warum sie fehlt.
    """

    class OhneMarke(FalscherRunPod):
        async def image_tag(self) -> str | None:
            raise TransientRunPodError("GraphQL antwortet nicht")

    sha, _ = _anlegen(
        db_conn, tmp_path, b"x", name="a.png", typ=DocumentType.PNG
    )
    runpod = OhneMarke([ersatz.fertige_antwort()])
    await process_document(sha, settings=exec_settings, runpod=runpod, pool=pool)

    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.DONE
    assert zeile.image_tag_at_submit is None
    assert any(v["kind"] == "image_tag_nicht_lesbar" for v in zeile.notes)


async def test_wiederholbarer_fehler_laesst_die_zeile_unberuehrt(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Geworfen, nichts geschrieben — die Warteschlange legt neu vor. Es gibt
    kein Ergebnis, und die Eingabe steht: Das Dokument bleibt offen."""
    sha, _ = _anlegen(
        db_conn, tmp_path, b"x", name="a.png", typ=DocumentType.PNG
    )

    runpod = FalscherRunPod([{"id": "j", "status": "TIMED_OUT"}])
    with pytest.raises(TransientRunPodError):
        await process_document(
            sha, settings=exec_settings, runpod=runpod, pool=pool
        )

    assert get_document(db_conn, sha) is None
    assert _eingabe_steht(db_conn, sha)


async def test_endgueltiger_fehler_schreibt_vor_dem_werfen(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Zeile `failed` samt dem, was bis dahin ankam, DANN die Ausnahme. So tragen
    Job-Endzustand und Zeile dieselbe Wahrheit.

    Und das Bezahlte geht NICHT verloren: Das erste Teilstueck kam durch, das
    zweite scheiterte — im `raw` steht trotzdem, was das erste lieferte.
    """
    eng = dataclasses.replace(exec_settings, max_pages_per_chunk=1)
    sha, _ = _anlegen(db_conn, tmp_path, _pdf(2))

    runpod = FalscherRunPod(
        [
            ersatz.fertige_antwort("Erstes Stueck kam an"),
            {"id": "j", "status": "FAILED", "error": "Modellfehler"},
        ]
    )
    with pytest.raises(TerminalRunPodError):
        await process_document(sha, settings=eng, runpod=runpod, pool=pool)

    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.FAILED
    assert zeile.raw is not None
    # Das Bezahlte ist da.
    assert "Erstes Stueck kam an" in zeile.raw["markdown"]
    # Und daneben steht, WORAN es scheiterte — sonst saehe die Zeile aus wie ein
    # unvollstaendiges Ergebnis ohne Grund.
    assert zeile.raw["failure"]["type"] == "TerminalRunPodError"
    assert any(v["kind"] == "gescheitert" for v in zeile.notes)
    assert zeile.notes[-1]["teilstuecke_bezahlt"] == 1


async def test_nicht_speicherbares_ergebnis_endet_failed_im_ersten_versuch(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Weist Postgres das Ergebnis wegen seines Inhalts ab, endet das Dokument
    sofort `failed`, mit einem Vermerk, der den Grund nennt (Mineroo#27).

    Ein U+0000 im Text kann jsonb nicht tragen, und jeder weitere Lauf liefert
    denselben Inhalt. Als Datenbank-Aussetzer eingestuft, liefe das Dokument
    dreimal, bezahlte dreimal und bliebe am Ende ohne Ergebnis, denn auch das
    `failed` mit demselben Inhalt waere nicht speicherbar. Deshalb schon im
    ersten Versuch (`letzter_versuch` steht hier auf False) endgueltig, und das
    `failed` traegt den Inhalt nicht: Er ist so nicht speicherbar, und
    veraendert wird er nicht.
    """
    sha, _ = _anlegen(
        db_conn, tmp_path, b"nul im text", name="n.png", typ=DocumentType.PNG
    )
    runpod = FalscherRunPod([ersatz.fertige_antwort("vor\x00nach")])

    with pytest.raises(UnstorableResultError):
        await process_document(sha, settings=exec_settings, runpod=runpod, pool=pool)

    assert len(runpod.abgesendet) == 1
    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.FAILED
    assert zeile.error is not None and "22P05" in zeile.error
    assert zeile.raw is not None
    assert zeile.raw["failure"]["type"] == "UnstorableResultError"
    assert "markdown" not in zeile.raw
    assert "22P05" in zeile.raw["ergebnis_nicht_speicherbar"]
    vermerk = next(v for v in zeile.notes if v["kind"] == "ergebnis_nicht_speicherbar")
    assert "22P05" in vermerk["detail"]
    assert any(v["kind"] == "gescheitert" for v in zeile.notes)
    # `failed` laesst die Eingabe stehen, damit `retry` etwas zu lesen hat.
    assert _eingabe_steht(db_conn, sha)


async def test_zu_grosses_ergebnis_endet_failed_im_ersten_versuch(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ein Ergebnis ueber der Nachrichtengrenze von Postgres endet ebenso sofort
    `failed`, ohne den Inhalt und mit dem Grund (Mineroo#27). Die Grenze ist
    herabgesetzt."""
    monkeypatch.setattr(db_modul, "MAX_RAW_JSON_BYTES", 2000)
    sha, _ = _anlegen(
        db_conn, tmp_path, b"gross", name="g.png", typ=DocumentType.PNG
    )
    runpod = FalscherRunPod([ersatz.fertige_antwort("x" * 5000)])

    with pytest.raises(UnstorableResultError):
        await process_document(sha, settings=exec_settings, runpod=runpod, pool=pool)

    assert len(runpod.abgesendet) == 1
    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.FAILED
    assert zeile.raw is not None
    assert "markdown" not in zeile.raw
    assert "2000" in zeile.raw["ergebnis_nicht_speicherbar"]


async def test_nicht_speicherbare_teilstuecke_im_fehlerpfad_verhindern_das_failed_nicht(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Scheitert ein Dokument an etwas anderem und traegt das bis dahin Bezahlte
    einen nicht speicherbaren Inhalt, wird `failed` trotzdem geschrieben: ohne
    den Inhalt, mit dem urspruenglichen Fehler in `error` und einem Vermerk,
    warum das Bezahlte fehlt.

    Ohne das liefe das `failed`-Schreiben im Fehlerzweig an demselben Inhalt
    auf, der Fehler verliesse den Zweig, und das Dokument bliebe ohne Ergebnis
    (Mineroo#27).
    """
    eng = dataclasses.replace(exec_settings, max_pages_per_chunk=1)
    sha, _ = _anlegen(db_conn, tmp_path, _pdf(2))
    runpod = FalscherRunPod(
        [
            ersatz.fertige_antwort("erstes\x00stueck"),
            {"id": "j", "status": "FAILED", "error": "Modellfehler"},
        ]
    )

    with pytest.raises(TerminalRunPodError):
        await process_document(sha, settings=eng, runpod=runpod, pool=pool)

    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.FAILED
    assert zeile.error is not None and zeile.error.startswith("TerminalRunPodError")
    assert zeile.raw is not None
    assert zeile.raw["failure"]["type"] == "TerminalRunPodError"
    assert "markdown" not in zeile.raw
    assert "22P05" in zeile.raw["ergebnis_nicht_speicherbar"]
    assert any(v["kind"] == "ergebnis_nicht_speicherbar" for v in zeile.notes)


# --- Geld -----------------------------------------------------------------


async def test_leeres_konto_wartet(exec_settings: Settings) -> None:
    """`wait_for_balance` kehrt erst zurueck, wenn der Stand wieder reicht.

    Und es WIRFT nicht: Wuerde es werfen, verbrauchte die Wartezeit die drei
    echten Versuche, und ein Bestand liefe bei kurzzeitig leerem Konto
    reihenweise in `failed`.
    """

    class Aufladend(FalscherRunPod):
        def __init__(self) -> None:
            super().__init__()
            self.abfragen = 0

        async def balance(self) -> float:
            self.abfragen += 1
            return 0.0 if self.abfragen < 3 else 5.0

    runpod = Aufladend()
    await wait_for_balance(runpod, exec_settings)
    assert runpod.abfragen == 3


async def test_gescheiterte_kontoabfrage_beendet_das_warten_nicht(
    exec_settings: Settings,
) -> None:
    """Ein Netzwackeln ist kein „Konto in Ordnung" — sonst liefe man in eine Wand
    aus Ablehnungen, jede ein bezahlter Fehlversuch."""

    class Wackelnd(FalscherRunPod):
        def __init__(self) -> None:
            super().__init__()
            self.abfragen = 0

        async def balance(self) -> float:
            self.abfragen += 1
            if self.abfragen < 3:
                raise TransientRunPodError("GraphQL antwortet nicht")
            return 5.0

    runpod = Wackelnd()
    await wait_for_balance(runpod, exec_settings)
    assert runpod.abfragen == 3


async def test_endgueltiger_fehler_der_kontoabfrage_beendet_das_warten(
    exec_settings: Settings,
) -> None:
    """Ein widerrufener Schluessel laesst das Warten NICHT endlos weiterlaufen.

    Der Gegenfall zum Test darueber, und der teurere von beiden: Frueher fing die
    Schleife jede Ausnahme. Ein `ConfigurationError` (HTTP 401/403 — laut
    `errors.py` „immer endgueltig, nie ein Grund fuer einen Retry") liess damit
    JEDES Dokument vor seinem ersten Teilstueck haengen, endlos und ohne jede
    Meldung. Der Lauf stand still und war dabei von „laeuft" nicht zu
    unterscheiden.
    """

    class TotesKonto(FalscherRunPod):
        def __init__(self) -> None:
            super().__init__()
            self.abfragen = 0

        async def balance(self) -> float:
            self.abfragen += 1
            raise ConfigurationError("HTTP 401: Schluessel widerrufen")

    runpod = TotesKonto()
    with pytest.raises(ConfigurationError):
        await wait_for_balance(runpod, exec_settings)
    # EINMAL gefragt, nicht endlos: Der Fehler wird weitergereicht, nicht
    # ausgesessen.
    assert runpod.abfragen == 1


async def test_402_beim_absenden_pausiert_statt_zu_werfen(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Regel 8 am ganzen Ablauf: Ein leeres Konto MITTEN im Lauf pausiert.

    Das Szenario, fuer das die Regel geschrieben wurde: Ein mehrteiliges Dokument
    laeuft, das Konto leert sich zwischen zwei Teilstuecken. Vorher lief der 402
    ungefangen nach oben, wurde als wiederholbar eingestuft und die Warteschlange
    legte den Job neu vor — was zweierlei kostete: Das bereits bezahlte erste
    Teilstueck war verworfen und wurde neu bezahlt, und die Wiedervorlage zehrte
    am Budget aus `max_attempts`.

    Geprueft wird deshalb beides: dass die Ausnahme NICHT nach oben geht, und
    dass das erste Teilstueck erhalten bleibt.
    """
    eng = dataclasses.replace(exec_settings, max_pages_per_chunk=1)
    sha, _ = _anlegen(db_conn, tmp_path, _pdf(2))

    class KontoLeertSich(FalscherRunPod):
        def __init__(self) -> None:
            super().__init__(
                [
                    ersatz.fertige_antwort("Erstes Stueck"),
                    ersatz.fertige_antwort("Zweites Stueck"),
                ]
            )
            self.leer = True

        async def submit(self, *, file_b64: str, **rest: Any) -> str:
            if len(self.abgesendet) == 1 and self.leer:
                # Beim ZWEITEN Absenden ist das Konto leer. Danach ist es
                # aufgeladen — genau so, wie eine Nachzahlung wirkt.
                self.leer = False
                raise InsufficientBalanceError("HTTP 402: Konto leer")
            return await super().submit(file_b64=file_b64, **rest)

    runpod = KontoLeertSich()
    await process_document(sha, settings=eng, runpod=runpod, pool=pool)

    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.DONE
    assert zeile.raw is not None
    # Beide Teilstuecke sind da — das erste wurde durch die Pause nicht verworfen.
    assert "Erstes Stueck" in zeile.raw["markdown"]
    assert "Zweites Stueck" in zeile.raw["markdown"]
    # Und es wurde genau zweimal wirklich abgesendet, nicht dreimal: Die Pause
    # wiederholt das gescheiterte Absenden, sie sendet nichts zusaetzlich.
    assert len(runpod.abgesendet) == 2


async def test_402_beim_abholen_sendet_den_job_nicht_neu_ab(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Ein 402 beim Abholen fuehrt NICHT zu einem zweiten Absenden.

    Der Grund, warum Absenden und Abholen getrennt pausieren: Ist der Job schon
    abgesendet, ist er bezahlt. Ihn nach einer Pause erneut abzuschicken zahlte
    ihn ein zweites Mal UND liesse das erste Ergebnis in seinem
    Dreissig-Minuten-Fenster verfallen — der teuerste denkbare Ausgang einer
    Massnahme, die Geld sparen soll.
    """
    sha, _ = _anlegen(
        db_conn, tmp_path, b"ein bild", name="b.png", typ=DocumentType.PNG
    )

    class LeerBeimAbholen(FalscherRunPod):
        def __init__(self) -> None:
            super().__init__([ersatz.fertige_antwort("Fertig geworden")])
            self.versuche = 0

        async def await_result(self, job_id: str) -> dict[str, Any]:
            self.versuche += 1
            if self.versuche == 1:
                raise InsufficientBalanceError("HTTP 402 beim Abfragen")
            return await super().await_result(job_id)

    runpod = LeerBeimAbholen()
    await process_document(sha, settings=exec_settings, runpod=runpod, pool=pool)

    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.DONE
    # DAS ist die Zusicherung: einmal abgesendet, zweimal abgeholt.
    assert len(runpod.abgesendet) == 1
    assert runpod.versuche == 2


async def test_402_trotz_gedecktem_konto_wird_als_widerspruch_benannt(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Sagt der Kontostand „gedeckt" und der Endpoint trotzdem 402, wird der
    Widerspruch benannt statt ausgesessen.

    Die Pause wartet, so lange das Konto leer ist — das ist gewollt und
    unbegrenzt. Aber `wait_for_balance` kehrt nur zurueck, wenn der Stand ueber
    der Schwelle liegt; kommt DANN erneut ein 402, widersprechen sich zwei
    Quellen. Endlos darauf zu warten hiesse, auf eine Bedingung zu warten, die
    nach der einen Quelle schon erfuellt ist — genau der stille Stillstand, gegen
    den auch die Behandlung endgueltiger Fehler in `wait_for_balance` steht.
    """
    sha, _ = _anlegen(
        db_conn, tmp_path, b"noch ein bild", name="c.png", typ=DocumentType.PNG
    )

    class ImmerLeer(FalscherRunPod):
        """Konto meldet 5,00 USD, der Endpoint lehnt trotzdem jedes Mal ab."""

        async def submit(self, *, file_b64: str, **_: Any) -> str:
            raise InsufficientBalanceError("HTTP 402: Konto leer")

    runpod = ImmerLeer()
    with pytest.raises(TerminalRunPodError) as gefangen:
        await process_document(sha, settings=exec_settings, runpod=runpod, pool=pool)

    assert "widersprechen einander" in str(gefangen.value)
    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.FAILED


async def test_fehlerpfad_schreibt_auch_wenn_das_zusammensetzen_scheitert(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Der Endzustand wird auch dann geschrieben, wenn `assemble` SELBST wirft.

    Der teuerste Fehler, den dieses Modul hatte: Der Fehlerpfad baute das zu
    schreibende `raw` mit demselben `assembly.assemble`, das im Erfolgspfad
    geworfen haben kann. War der urspruengliche Fehler genau dieser Wurf, warf
    der Aufruf im Fehlerpfad sofort wieder, die Ausnahme verliess den Handler,
    und geschrieben wurde NIE.

    Die Folge war nicht nur der Verlust der bezahlten Teilstuecke: Der Job endete
    `failed`, die Zeile blieb `new`, das Dokument zaehlte fuer immer als
    `pending` — und weil ein Ordner-Durchlauf genau die `pending` anfasst,
    bezahlte JEDER weitere Durchlauf das ganze Dokument neu und scheiterte
    deterministisch wieder an derselben Stelle.

    Ausgeloest wird der Wurf hier ueber ein `page_idx`, das keine ganze Zahl ist
    — `assemble` kann den Seitenversatz dann nicht berechnen und wirft, wie es
    soll.
    """
    sha, _ = _anlegen(
        db_conn, tmp_path, b"kaputte antwort", name="d.png", typ=DocumentType.PNG
    )

    runpod = FalscherRunPod(
        [
            ersatz.fertige_antwort("Bezahlt und angekommen", page_idx=1.5)
        ]
    )
    with pytest.raises(TerminalRunPodError):
        await process_document(sha, settings=exec_settings, runpod=runpod, pool=pool)

    zeile = get_document(db_conn, sha)
    assert zeile is not None
    # ZUERST geschrieben, DANN geworfen — auch hier.
    assert zeile.status is DocumentStatus.FAILED
    assert zeile.raw is not None
    # Das Bezahlte ist in der Notfassung erhalten, unbearbeitet.
    assert "assembly_failed" in zeile.raw
    assert zeile.raw["chunks"][0]["markdown"] == "Bezahlt und angekommen"
    assert zeile.raw["chunks"][0]["middle_json"]["pages"][0]["page_idx"] == 1.5
    # Und der Leser sieht, dass er die Notfassung vor sich hat.
    assert any(v["kind"] == "zusammensetzen_gescheitert" for v in zeile.notes)


async def test_bezahlte_antwort_mit_formfehler_wird_aufgehoben(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Eine BEZAHLTE `COMPLETED`-Antwort, die sich nicht auspacken laesst, landet
    trotzdem vollstaendig in `raw`.

    Vorher ging sie verloren: `unpack_response` warf bei einer Formabweichung,
    die Ausnahme trug nur die Schluesselnamen in ihrer Meldung, und die Antwort
    selbst wanderte weder in `teile` noch in `raw`. Der Job war zu diesem
    Zeitpunkt schon bezahlt, und dreissig Minuten spaeter ist eine Antwort auf
    der Gegenseite nicht mehr abrufbar — bei jeder kuenftigen Fassung der Engine,
    die ein Feld anders benennt, waere das Ergebnis also unwiederbringlich weg
    gewesen. Genau der Verlust, gegen den die Abruf-Regel steht: alles holen,
    alles persistieren, nie filtern.

    Hier antwortet die Gegenseite mit `COMPLETED`, aber mit einer `output`-Form,
    die es so nicht geben duerfte — zwei Ergebnisse statt einem.
    """
    sha, _ = _anlegen(
        db_conn, tmp_path, b"formfehler", name="e.png", typ=DocumentType.PNG
    )

    kaputte_antwort = {
        "id": "job-formfehler",
        "status": "COMPLETED",
        "workerId": "wrk-7",
        "executionTime": 4242,
        "output": {
            "ok": True,
            "results": [
                {"markdown": "Erstes Ergebnis"},
                {"markdown": "Zweites Ergebnis"},
            ],
        },
    }
    runpod = FalscherRunPod([kaputte_antwort])

    with pytest.raises(TerminalRunPodError):
        await process_document(sha, settings=exec_settings, runpod=runpod, pool=pool)

    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.FAILED
    assert zeile.raw is not None

    # DIE ZUSICHERUNG: die ganze Antwort steht da, nicht nur eine Fehlermeldung
    # ueber sie.
    aufgehoben = zeile.raw["unverwertbare_antworten"]
    assert len(aufgehoben) == 1
    assert aufgehoben[0]["antwort"] == kaputte_antwort
    assert aufgehoben[0]["job_id"] == "job-0"
    assert aufgehoben[0]["chunk"] == {"first_page": 0, "last_page": 0}
    # Und der Grund steht daneben, sonst saehe die Zeile aus wie ein
    # unvollstaendiges Ergebnis ohne Anlass.
    assert "TerminalRunPodError" in aufgehoben[0]["fehler"]


async def test_echte_fehlerantwort_landet_vollstaendig_in_raw(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
    echte_fehlerantwort: dict[str, Any],
) -> None:
    """Die ECHTE Fehlerantwort des MinerU-4-Endpoints auf eine unbekannte Stufe
    (`input validation failed: tier must be one of …`) ist eine Pruefung der
    Anfrage, keine Abweisung der Datei: `failed`, und die Antwort steht danach
    als abfragbares JSON in `raw`.

    Sie traegt weit mehr als „es ging schief": `error` auf oberster Ebene, ein
    volles `output` mit `ok: false`, den `debug`-Block mit GPU,
    Modellverzeichnis und Phasenzeiten — und einen kompletten `traceback` aus
    dem Worker (Herkunft bei der Vorrichtung `echte_fehlerantwort`).

    Frueher stand `_endzustand_uebersetzen` VOR dem Schutzblock, der bezahlte
    Antworten aufhebt. Der Traceback ueberlebte damit nur als Python-
    Textdarstellung, eingebettet in die Fehlermeldung — vorhanden, aber weder
    abfragbar noch zuverlaessig lesbar. Fuer die beste Diagnose, die es zu einem
    gescheiterten Dokument gibt, ist das zu wenig.
    """
    sha, _ = _anlegen(
        db_conn, tmp_path, b"loest fehler aus", name="f.png", typ=DocumentType.PNG
    )
    runpod = FalscherRunPod([echte_fehlerantwort])

    with pytest.raises(TerminalRunPodError):
        await process_document(sha, settings=exec_settings, runpod=runpod, pool=pool)

    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.FAILED
    assert zeile.raw is not None

    aufgehoben = zeile.raw["unverwertbare_antworten"]
    assert len(aufgehoben) == 1
    # Die GANZE Antwort, unveraendert.
    assert aufgehoben[0]["antwort"] == echte_fehlerantwort
    # Und damit insbesondere das, wofuer man sie aufhebt.
    ausgabe = aufgehoben[0]["antwort"]["output"]
    assert ausgabe["ok"] is False
    assert "Traceback" in ausgabe["traceback"]
    assert ausgabe["debug"]["gpu"]["name"].startswith("NVIDIA")


@pytest.mark.parametrize(
    "fehler",
    [
        # Worker, `worker/parse.py` (`run_mineru`): die Meldung fuer einen Typ,
        # den MinerU nicht liest.
        "ValueError: input is not a file type MinerU 4.0.12 reads (detected "
        "'txt'). It reads PDF, images, Office, OpenDocument, RTF, EPUB, OFD, "
        "HTML and MHTML files.",
        # Job-Vertrag (`api.mdx`, „Validation behaviour", Punkt 10): MinerUs
        # eigene Abweisung einer PDF ohne Seite, mit MinerUs Fehlertyp.
        "InvalidRequestError: Invalid page range '': document has no available pages",
        # Am Endpoint beobachtet (QA zu Mineroo#11): PNG- und GIF-Signatur vor
        # Muell. MinerU erkennt den Typ und scheitert am Dekodieren.
        "OSError: Truncated File Read",
        "UnidentifiedImageError: cannot identify image file <_io.BytesIO object at 0x741910c9c900>",
    ],
)
async def test_vom_worker_abgewiesene_datei_endet_input_error_mit_seinem_grund(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
    fehler: str,
) -> None:
    """Weist der Worker eine Datei als nicht lesbar ab, endet sie als
    `input_error`, und der Grund ist der aus seiner Antwort (Mineroo#11). Die
    bezahlte Antwort bleibt ganz in `raw`."""
    sha, _ = _anlegen(db_conn, tmp_path, b"nicht lesbar", name="a.png", typ=DocumentType.PNG)
    antwort = ersatz.fehlerantwort(fehler)
    runpod = FalscherRunPod([antwort])

    with pytest.raises(InputError):
        await process_document(sha, settings=exec_settings, runpod=runpod, pool=pool)

    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.INPUT_ERROR
    assert zeile.error is not None and fehler in zeile.error
    assert zeile.raw is not None
    assert zeile.raw["unverwertbare_antworten"][0]["antwort"] == antwort


async def test_echte_abweisung_eines_bildes_mit_kaputten_pixeln_endet_input_error(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Die ECHTE Antwort des Endpoints auf ein PNG, dessen Kopf stimmt und dessen
    Pixeldaten kaputt sind (`material/kaputte_pixel.png`): MinerU erkennt das
    Bild und scheitert erst beim Dekodieren, mit Pillows `OSError: broken data
    stream when reading image file`. Das ist eine Aussage ueber die Datei, also
    `input_error` mit diesem Grund; ein weiterer Lauf kaufte dieselbe Meldung."""
    png = (ersatz.ECHT_ENDPOINT / "material" / "kaputte_pixel.png").read_bytes()
    sha, _ = _anlegen(db_conn, tmp_path, png, name="a.png", typ=DocumentType.PNG)
    antwort = ersatz.echte_antwort("kaputte_pixel")
    runpod = FalscherRunPod([antwort])

    with pytest.raises(InputError):
        await process_document(sha, settings=exec_settings, runpod=runpod, pool=pool)

    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.INPUT_ERROR
    assert zeile.error is not None
    assert "OSError: broken data stream when reading image file" in zeile.error
    assert zeile.raw is not None
    assert zeile.raw["unverwertbare_antworten"][0]["antwort"] == antwort


async def test_ein_anderer_fehler_des_workers_bleibt_failed(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Scheitert der Worker an etwas anderem als der Datei — eine abgewiesene
    Anfrage (`ValueError: input validation failed: …`), die Engine selbst —,
    ist das `failed`: Die Datei kann nichts dafuer."""
    sha, _ = _anlegen(db_conn, tmp_path, b"engine scheitert", name="a.png", typ=DocumentType.PNG)
    runpod = FalscherRunPod(
        [ersatz.fehlerantwort("RuntimeError: CUDA error: out of memory")]
    )

    with pytest.raises(TerminalRunPodError):
        await process_document(sha, settings=exec_settings, runpod=runpod, pool=pool)

    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.FAILED


async def test_eingabeprobleme_enden_input_error_nicht_failed(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Was VOR dem ersten bezahlten Aufruf schiefgeht, ist `input_error`.

    `db.py` trennt die beiden Endzustaende genau daran: `failed` beantwortet
    „wofuer haben wir bezahlt?", `input_error` sagt „mit dieser Eingabe konnten
    wir nichts anfangen". Zwei Faelle landeten trotzdem auf `failed`, beide
    ueber die Regel „Unbekanntes ist endgueltig":

        eine MIME-Zeichenkette, zu der es keinen bekannten Typ gibt,
        und eine Eingabe, die in `inputs` fehlt, obwohl das Dokument noch
        kein Endergebnis hat.

    Bezahlt ist in beiden Faellen nichts. Und der Unterschied ist nicht bloss
    Etikett: Bei `input_error` kehrt das Nachsehen des Executors zurueck,
    `failed` laeuft ausdruecklich neu — ein Retry von Hand liesse ein Dokument
    mit unbekanntem Typ also erneut anlaufen, ohne dass sich daran etwas aendern
    koennte. Was sich nicht bestimmen liess, bleibt in der Zeile leer.
    """
    # 1. Bytes eines Typs, den das Paket nicht kennt (der Job erkennt den Typ
    # am Inhalt, wie das Abschicken).
    unbekannt = b"kein bekannter dateikopf"
    sha_mime = hashlib.sha256(unbekannt).hexdigest()
    insert_input(db_conn, sha256=sha_mime, content=unbekannt, read_settings=ReadSettings())
    runpod = FalscherRunPod([ersatz.fertige_antwort()])
    with pytest.raises(UnsupportedTypeError):
        await process_document(
            sha_mime, settings=exec_settings, runpod=runpod, pool=pool
        )
    zeile = get_document(db_conn, sha_mime)
    assert zeile is not None
    assert zeile.status is DocumentStatus.INPUT_ERROR
    assert zeile.mime is None and zeile.size_bytes == len(unbekannt)
    assert not _eingabe_steht(db_conn, sha_mime)
    # Und es wurde NICHTS abgesendet.
    assert runpod.abgesendet == []

    # 2. Zum Job gibt es keine Eingabe (beschaedigter Bestand).
    sha_weg, _ = _anlegen(
        db_conn, tmp_path, b"gleich weg", name="w.png", typ=DocumentType.PNG
    )
    db_conn.execute("DELETE FROM inputs WHERE sha256 = %s", (sha_weg,))
    runpod2 = FalscherRunPod([ersatz.fertige_antwort()])
    with pytest.raises(CorruptDocumentError):
        await process_document(
            sha_weg, settings=exec_settings, runpod=runpod2, pool=pool
        )
    zeile = get_document(db_conn, sha_weg)
    assert zeile is not None
    assert zeile.status is DocumentStatus.INPUT_ERROR
    assert zeile.mime is None and zeile.size_bytes is None
    assert runpod2.abgesendet == []


async def test_zu_grosses_bild_wird_abgelehnt_ohne_zu_bezahlen(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Der Aufruest-Pfad, end-to-end: abgelehnt, `input_error`, NICHTS abgesendet.

    Ein Bild ueber der Nutzlast-Grenze laesst sich nicht seitenweise zerlegen —
    nur PDFs koennen das. Der Fall ist bewusst NICHT gebaut (Rastern und
    verkleinert versenden), weil er im gesamten geprueften Bestand nicht vorkam;
    die groesste Einzelseite mass 1,27 MB gegen eine Grenze von 7 MB.

    Geprueft wurde bisher die Entscheidung selbst (`test_chunking`) und ihr
    Auftauchen im Bericht (`test_wrapper`) — aber nicht die Frage, an der Geld
    haengt: Kommt die Ablehnung wirklich VOR dem ersten bezahlten Aufruf?

    Und der zweite Teil: Der Endzustand ist `input_error`, nicht `failed`. Er
    gehoert damit zu den Ergebnissen, die das Nachsehen nicht wieder anfasst — ein
    Wiederlauf koennte an der Groesse nichts aendern und wuerde nur kosten.
    """
    from mineru_ocr.errors import TooLargeError

    zu_gross = b"\x89PNG\r\n\x1a\n" + b"x" * (exec_settings.max_payload_bytes + 1000)
    sha, _ = _anlegen(
        db_conn, tmp_path, zu_gross, name="riesig.png", typ=DocumentType.PNG
    )
    # Leeres Konto: Die Ablehnung darf nicht erst nach dem Warten auf Geld kommen.
    runpod = FalscherRunPod([ersatz.fertige_antwort()], kontostand=0.0)

    with pytest.raises(TooLargeError) as gefangen:
        await asyncio.wait_for(
            process_document(sha, settings=exec_settings, runpod=runpod, pool=pool),
            timeout=5,
        )

    # DIE ZUSICHERUNG, an der Geld haengt: kein einziger Aufruf, auch kein lesender.
    assert runpod.kontakte == []

    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.INPUT_ERROR
    # Der Marker, an dem der Bericht diese Faelle findet.
    assert "AUFRUESTBAR" in str(gefangen.value)
    assert zeile.error is not None and "AUFRUESTBAR" in zeile.error


def test_ungueltige_lese_einstellung_faellt_lokal_auf(settings: Settings) -> None:
    """Ein Tippfehler in Stufe oder OCR-Modus kostet keinen Kaltstart.

    Die gueltigen Werte stehen im Job-Vertrag des Forks (`api.mdx`,
    „Reading settings"): `tier` ist eines von `flash`, `basic`, `standard`,
    `advanced`, `ocr_mode` eines von `auto`, `txt`, `ocr`. Der Worker prueft
    sie erst, wenn ein Worker hochgefahren ist; ein Tippfehler waere also nur
    gegen einen bezahlten Kaltstart je Dokument zu erfahren.
    """
    import dataclasses

    from mineru_ocr.config import GUELTIGE_OCR_MODI, GUELTIGE_STUFEN
    from mineru_ocr.errors import ConfigurationError

    # Die Voreinstellungen sind die des Vertrags.
    assert (settings.tier, settings.ocr_mode, settings.image_analysis) == (
        "advanced",
        "ocr",
        True,
    )

    with pytest.raises(ConfigurationError, match="kennt der Worker nicht"):
        dataclasses.replace(settings, tier="advance")
    with pytest.raises(ConfigurationError, match="kennt der Worker nicht"):
        dataclasses.replace(settings, ocr_mode="ocr ")

    for stufe in ("flash", "basic", "standard", "advanced"):
        assert dataclasses.replace(settings, tier=stufe).tier == stufe
    for modus in ("auto", "txt", "ocr"):
        assert dataclasses.replace(settings, ocr_mode=modus).ocr_mode == modus
    assert set(GUELTIGE_STUFEN) == {"flash", "basic", "standard", "advanced"}
    assert set(GUELTIGE_OCR_MODI) == {"auto", "txt", "ocr"}


# --- Verbindungen und Abbruch ---------------------------------------------


async def test_keine_verbindung_waehrend_des_wartens(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Waehrend der Abfrageschleife haelt der Ablauf keine Datenbankverbindung.

    Sonst blockierten vierundzwanzig gleichzeitig betreute Dokumente den Pool,
    solange sie warten, waehrend keines von ihnen ihn benutzt.

    Geprueft mit einem Pool der Groesse EINS: Waehrend das Dokument auf RunPod
    wartet, muss eine ZWEITE Abfrage durch denselben Pool gehen koennen. Haelt
    der Executor seine Verbindung, laeuft dieser Test in den Zeitablauf.
    """
    kleiner = AsyncConnectionPool(
        exec_settings.db_dsn, min_size=1, max_size=1, open=False
    )
    await kleiner.open(wait=True)
    try:
        sha, _ = _anlegen(
            db_conn, tmp_path, b"x", name="a.png", typ=DocumentType.PNG
        )

        durchgekommen = asyncio.Event()

        class Wartend(FalscherRunPod):
            async def await_result(self, job_id: str) -> dict[str, Any]:
                # Genau HIER — mitten im Warten auf RunPod — muss der Pool frei
                # sein.
                async with kleiner.connection() as zweite:
                    await zweite.execute("SELECT 1")
                durchgekommen.set()
                return await super().await_result(job_id)

        runpod = Wartend([ersatz.fertige_antwort()])
        await process_document(
            sha, settings=exec_settings, runpod=runpod, pool=kleiner
        )
        assert durchgekommen.is_set()
    finally:
        await kleiner.close()


async def test_abbruch_wird_durchgereicht(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Ein Abbruch, nach dem noch ein Versuch kommt, schreibt nichts: Die
    Warteschlange reiht den Job neu ein (Mineroo#27), es entsteht kein Ergebnis,
    und die Eingabe steht.

    Der Test faengt einen echten Fehler ab, den man leicht macht: Wuerde der
    Abbruch hier wie ein endgueltiger Fehler verbucht, machte jedes Strg-C
    reihenweise `failed`.
    """
    sha, _ = _anlegen(
        db_conn, tmp_path, b"x", name="a.png", typ=DocumentType.PNG
    )

    class Abbrechend(FalscherRunPod):
        async def await_result(self, job_id: str) -> dict[str, Any]:
            raise asyncio.CancelledError()

    runpod = Abbrechend([ersatz.fertige_antwort()])
    with pytest.raises(asyncio.CancelledError):
        await process_document(
            sha, settings=exec_settings, runpod=runpod, pool=pool
        )

    # Kein Ergebnis — kein `failed` —, und die Eingabe steht noch.
    assert get_document(db_conn, sha) is None
    assert _eingabe_steht(db_conn, sha)


async def test_abbruch_im_letzten_versuch_schreibt_failed_und_wirft_weiter(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Bricht das Herunterfahren den letzten Versuch ab, schreibt der Executor
    `failed` mit dem Vermerk und wirft den Abbruch DANN weiter (Mineroo#27).

    Ohne das bliebe das Dokument ohne Ergebnis liegen: Die Warteschlange reiht
    nach dem letzten Versuch nicht mehr ein, und ein Daemon, der waehrend eines
    langen Dokuments dreimal neu startet, liesse es offen zurueck. Der Job
    endet danach `aborted`; der RunPod-Job ist abgebrochen, und die Eingabe
    bleibt fuer `retry` stehen.
    """
    sha, _ = _anlegen(
        db_conn, tmp_path, b"x", name="a.png", typ=DocumentType.PNG
    )

    class Abbrechend(FalscherRunPod):
        async def await_result(self, job_id: str) -> dict[str, Any]:
            raise asyncio.CancelledError()

    runpod = Abbrechend([ersatz.fertige_antwort()])
    with pytest.raises(asyncio.CancelledError):
        await process_document(
            sha,
            settings=exec_settings,
            runpod=runpod,
            pool=pool,
            letzter_versuch=True,
        )

    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.FAILED
    assert zeile.error is not None
    assert (
        "im letzten Versuch abgebrochen (Herunterfahren oder Abbruch ueber die API)"
        in zeile.error
    )
    assert zeile.raw is not None
    assert zeile.raw["failure"]["type"] == "CancelledError"
    assert (
        "im letzten Versuch abgebrochen (Herunterfahren oder Abbruch ueber die API)"
        in zeile.raw["failure"]["message"]
    )
    assert zeile.notes[-1]["kind"] == "gescheitert"
    assert runpod.abgebrochen == ["job-0"]
    assert _eingabe_steht(db_conn, sha)


@pytest.mark.parametrize("fall", ["endgueltig", "letzter_versuch"])
async def test_abbruch_waehrend_des_failed_schreibens_unterbricht_es_nicht(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fall: str,
) -> None:
    """Kommt der Abbruch des Herunterfahrens, waehrend der Fehlerzweig gerade
    `failed` schreibt, wird das Schreiben trotzdem fertig, und weitergeworfen
    wird der Fehler, der in den Fehlerzweig fuehrte (Mineroo#27).

    Sonst bliebe im letzten Versuch das Dokument ohne Ergebnis (die Regel, die
    das Modell traegt), und bei einem endgueltigen Fehler kaeme statt seiner
    der Abbruch heraus: Procrastinate reihte den Job neu ein, und das Dokument
    liefe und kostete noch einmal, obwohl `failed` gemeint war.
    """
    sha, _ = _anlegen(
        db_conn, tmp_path, b"x", name="a.png", typ=DocumentType.PNG
    )
    erwartet: type[BaseException]
    if fall == "endgueltig":

        class Endgueltig(FalscherRunPod):
            async def await_result(self, job_id: str) -> dict[str, Any]:
                raise TerminalRunPodError("FAILED")

        runpod: FalscherRunPod = Endgueltig([ersatz.fertige_antwort()])
        erwartet = TerminalRunPodError
    else:

        async def aussetzer(*_a: Any, **_k: Any) -> Any:
            raise psycopg.OperationalError("Postgres kurz weg")

        monkeypatch.setattr(db_modul, "get_status_async", aussetzer)
        runpod = FalscherRunPod([ersatz.fertige_antwort()])
        erwartet = psycopg.OperationalError

    echtes_schreiben = db_modul.write_terminal_async
    schreiben_begonnen = asyncio.Event()
    weiter = asyncio.Event()

    async def langsames_schreiben(*a: Any, **k: Any) -> Any:
        schreiben_begonnen.set()
        await weiter.wait()
        return await echtes_schreiben(*a, **k)

    monkeypatch.setattr(db_modul, "write_terminal_async", langsames_schreiben)
    lauf = asyncio.ensure_future(
        process_document(
            sha,
            settings=exec_settings,
            runpod=runpod,
            pool=pool,
            letzter_versuch=fall == "letzter_versuch",
        )
    )
    await asyncio.wait_for(schreiben_begonnen.wait(), timeout=10)
    lauf.cancel()
    await asyncio.sleep(0)
    weiter.set()
    with pytest.raises(erwartet):
        await lauf

    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.FAILED
    assert _eingabe_steht(db_conn, sha)


async def test_zweiter_abbruch_waehrend_des_failed_schreibens_gewinnt(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Haengt das Schreiben von `failed` (eine blockierte Datenbank), beendet ein
    zweiter Abbruch es doch: Das zweite Signal an den Daemon bricht sofort ab
    (`daemon.run`). Sonst hielte eine haengende Datenbank den Daemon fest, bis
    nur noch `kill -9` hilft. Es entsteht kein Ergebnis: Mit Restbudget reiht
    Procrastinate den Job neu ein, im letzten Versuch ist `retry` der Weg
    zurueck."""
    sha, _ = _anlegen(
        db_conn, tmp_path, b"x", name="a.png", typ=DocumentType.PNG
    )

    class Endgueltig(FalscherRunPod):
        async def await_result(self, job_id: str) -> dict[str, Any]:
            raise TerminalRunPodError("FAILED")

    schreiben_begonnen = asyncio.Event()

    async def haengendes_schreiben(*_a: Any, **_k: Any) -> Any:
        schreiben_begonnen.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(db_modul, "write_terminal_async", haengendes_schreiben)
    lauf = asyncio.ensure_future(
        process_document(
            sha,
            settings=exec_settings,
            runpod=Endgueltig([ersatz.fertige_antwort()]),
            pool=pool,
        )
    )
    await asyncio.wait_for(schreiben_begonnen.wait(), timeout=10)
    lauf.cancel()
    await asyncio.sleep(0)
    lauf.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(lauf, timeout=10)

    assert get_document(db_conn, sha) is None
    assert _eingabe_steht(db_conn, sha)


@pytest.mark.parametrize("letzter_versuch", [False, True])
async def test_abbruch_waehrend_des_done_schreibens_behaelt_das_bezahlte_ergebnis(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    letzter_versuch: bool,
) -> None:
    """Bricht das Herunterfahren den Job genau waehrend des Schreibens von
    `done` ab, laeuft das Schreiben zu Ende, und der Job endet ohne Fehler
    (Mineroo#27).

    Sonst ging das bezahlte Ergebnis verloren: Mit Restbudget reihte
    Procrastinate den Job neu ein, und das Dokument liefe und kostete noch
    einmal; im letzten Versuch stuende statt `done` ein `failed` da.
    """
    sha, _ = _anlegen(
        db_conn, tmp_path, b"x", name="a.png", typ=DocumentType.PNG
    )
    echtes_schreiben = db_modul.write_terminal_async
    schreiben_begonnen = asyncio.Event()
    weiter = asyncio.Event()

    async def langsames_schreiben(*a: Any, **k: Any) -> Any:
        schreiben_begonnen.set()
        await weiter.wait()
        return await echtes_schreiben(*a, **k)

    monkeypatch.setattr(db_modul, "write_terminal_async", langsames_schreiben)
    lauf = asyncio.ensure_future(
        process_document(
            sha,
            settings=exec_settings,
            runpod=FalscherRunPod([ersatz.fertige_antwort()]),
            pool=pool,
            letzter_versuch=letzter_versuch,
        )
    )
    await asyncio.wait_for(schreiben_begonnen.wait(), timeout=10)
    lauf.cancel()
    await asyncio.sleep(0)
    weiter.set()
    await asyncio.wait_for(lauf, timeout=10)

    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.DONE
    assert not _eingabe_steht(db_conn, sha)


async def test_zweiter_abbruch_waehrend_des_done_schreibens_gewinnt(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Haengt das Schreiben von `done`, beendet ein zweiter Abbruch es doch,
    wie beim Schreiben von `failed`: Eine blockierte Datenbank haelt den Daemon
    gegen das zweite Signal nicht fest. Es entsteht kein Ergebnis, und die
    Eingabe bleibt stehen; mit Restbudget reiht Procrastinate den Job neu ein."""
    sha, _ = _anlegen(
        db_conn, tmp_path, b"x", name="a.png", typ=DocumentType.PNG
    )
    schreiben_begonnen = asyncio.Event()

    async def haengendes_schreiben(*_a: Any, **_k: Any) -> Any:
        schreiben_begonnen.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(db_modul, "write_terminal_async", haengendes_schreiben)
    lauf = asyncio.ensure_future(
        process_document(
            sha,
            settings=exec_settings,
            runpod=FalscherRunPod([ersatz.fertige_antwort()]),
            pool=pool,
        )
    )
    await asyncio.wait_for(schreiben_begonnen.wait(), timeout=10)
    lauf.cancel()
    await asyncio.sleep(0)
    lauf.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(lauf, timeout=10)

    assert get_document(db_conn, sha) is None
    assert _eingabe_steht(db_conn, sha)


@pytest.mark.parametrize("schritt", ["get_status_async", "read_input_async"])
async def test_datenbankfehler_vor_dem_lesen_im_letzten_versuch_schreibt_failed(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    schritt: str,
) -> None:
    """Das Nachsehen und das Lesen der Eingabe stehen im Fehlerzweig: Scheitern
    sie im letzten Versuch an der Datenbank, steht danach `failed` da
    (Mineroo#27). Vorher stand das Nachsehen vor dem Fehlerzweig, und ein
    Aussetzer dort liess das Dokument im letzten Versuch ohne Ergebnis zurueck.

    Gestoert wird nur der eine Aufruf; das Schreiben danach laeuft gegen die
    echte Datenbank, denn ein Aussetzer ist meist vorbei, wenn geschrieben wird.
    """
    sha, _ = _anlegen(
        db_conn, tmp_path, b"x", name="a.png", typ=DocumentType.PNG
    )

    async def aussetzer(*_a: Any, **_k: Any) -> Any:
        raise psycopg.OperationalError("Postgres kurz weg")

    monkeypatch.setattr(db_modul, schritt, aussetzer)
    runpod = FalscherRunPod([ersatz.fertige_antwort()])
    with pytest.raises(psycopg.OperationalError):
        await process_document(
            sha,
            settings=exec_settings,
            runpod=runpod,
            pool=pool,
            letzter_versuch=True,
        )

    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.FAILED
    assert zeile.error is not None and "OperationalError" in zeile.error
    # Vor jedem RunPod-Kontakt gescheitert: bezahlt ist nichts.
    assert runpod.kontakte == []
    assert _eingabe_steht(db_conn, sha)


@pytest.mark.parametrize("schritt", ["get_status_async", "read_input_async"])
async def test_datenbankfehler_vor_dem_lesen_mit_restbudget_schreibt_nichts(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    schritt: str,
) -> None:
    """Mit Restbudget bleibt es beim Werfen: Der Aussetzer ist wiederholbar,
    die Warteschlange legt neu vor, und es entsteht kein Ergebnis."""
    sha, _ = _anlegen(
        db_conn, tmp_path, b"x", name="a.png", typ=DocumentType.PNG
    )

    async def aussetzer(*_a: Any, **_k: Any) -> Any:
        raise psycopg.OperationalError("Postgres kurz weg")

    monkeypatch.setattr(db_modul, schritt, aussetzer)
    with pytest.raises(psycopg.OperationalError):
        await process_document(
            sha,
            settings=exec_settings,
            runpod=FalscherRunPod([ersatz.fertige_antwort()]),
            pool=pool,
        )

    assert get_document(db_conn, sha) is None
    assert _eingabe_steht(db_conn, sha)


async def test_failed_ohne_lauf_schreibt_nur_wo_noch_kein_ergebnis_ist(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """`write_failure_if_no_result` legt ein `failed`-Ergebnis an, wenn das
    Dokument noch keins hat, und laesst ein vorhandenes stehen, auch ein
    `failed` mit bezahltem `raw` (Mineroo#27, der Waechter bei erschoepftem
    Budget). Die Eingabe bleibt, damit `retry` etwas zu lesen hat."""
    offen, _ = _anlegen(db_conn, tmp_path, b"o", name="o.png", typ=DocumentType.PNG)
    assert await write_failure_if_no_result(
        offen, settings=exec_settings, pool=pool, grund="Worker wiederholt abgestuerzt"
    )
    zeile = get_document(db_conn, offen)
    assert zeile is not None
    assert zeile.status is DocumentStatus.FAILED
    assert zeile.error is not None and "Worker wiederholt abgestuerzt" in zeile.error
    assert zeile.raw is not None
    assert "Worker wiederholt abgestuerzt" in zeile.raw["failure"]["message"]
    assert zeile.notes[-1]["kind"] == "gescheitert"
    assert _eingabe_steht(db_conn, offen)

    for status in (DocumentStatus.FAILED, DocumentStatus.DONE):
        sha, _ = _anlegen(
            db_conn, tmp_path, status.value.encode(), name="v.png", typ=DocumentType.PNG
        )
        async with pool.connection() as conn:
            await write_terminal_async(
                conn,
                sha256=sha,
                status=status,
                raw={"bezahlt": True},
                endpoint_id="testendpoint",
            )
        assert not await write_failure_if_no_result(
            sha, settings=exec_settings, pool=pool, grund="Worker wiederholt abgestuerzt"
        )
        zeile = get_document(db_conn, sha)
        assert zeile is not None
        assert zeile.status is status
        assert zeile.raw == {"bezahlt": True}


async def test_abbruch_beim_warten_bricht_den_runpod_job_ab(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Bricht der Daemon beim Herunterfahren die Arbeit ab, geht `/cancel` fuer
    den RunPod-Job hinaus, auf den gerade gewartet wurde — und der Abbruch wird
    weitergeworfen, nicht verschluckt.

    Sonst liefe der Job bei RunPod weiter und kostete Geld, ohne dass ihn noch
    jemand abholt. Ein Ergebnis entsteht nicht; ob und wie neu eingereiht wird,
    regelt die Warteschlange und nicht dieser Zweig.
    """
    sha, _ = _anlegen(
        db_conn, tmp_path, b"x", name="a.png", typ=DocumentType.PNG
    )

    class Abbrechend(FalscherRunPod):
        async def await_result(self, job_id: str) -> dict[str, Any]:
            raise asyncio.CancelledError()

    runpod = Abbrechend([ersatz.fertige_antwort()])
    with pytest.raises(asyncio.CancelledError):
        await process_document(
            sha, settings=exec_settings, runpod=runpod, pool=pool
        )

    assert runpod.abgebrochen == ["job-0"]
    # Erst abgesendet, dann abgebrochen, in dieser Reihenfolge.
    assert runpod.verlauf == ["submit:job-0", "abbrechen:job-0"]
    assert get_document(db_conn, sha) is None


async def test_misslungener_abbruch_verdeckt_das_herunterfahren_nicht(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Scheitert das `/cancel` beim Herunterfahren, endet die Arbeit trotzdem mit
    `CancelledError`.

    Kaeme stattdessen der Fehler des Abbruchs heraus, verbuchte Procrastinate den
    Job als gescheitert statt als abgebrochen, und jedes Strg-C bei erreichbarem
    Netz-Problem machte reihenweise `failed`.
    """
    sha, _ = _anlegen(
        db_conn, tmp_path, b"x", name="a.png", typ=DocumentType.PNG
    )

    class Kaputt(FalscherRunPod):
        async def await_result(self, job_id: str) -> dict[str, Any]:
            raise asyncio.CancelledError()

        async def cancel(self, job_id: str) -> dict[str, Any]:
            raise TransientRunPodError("Netz weg")

    with pytest.raises(asyncio.CancelledError):
        await process_document(
            sha,
            settings=exec_settings,
            runpod=Kaputt([ersatz.fertige_antwort()]),
            pool=pool,
        )

    assert get_document(db_conn, sha) is None


async def test_erfolgreicher_job_wird_nicht_abgebrochen(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Ein Job, der sein Ergebnis geliefert hat, bekommt kein `/cancel`."""
    sha, _ = _anlegen(
        db_conn, tmp_path, b"x", name="a.png", typ=DocumentType.PNG
    )
    runpod = FalscherRunPod([ersatz.fertige_antwort()])
    await process_document(
        sha, settings=exec_settings, runpod=runpod, pool=pool
    )
    assert runpod.abgebrochen == []


async def test_widerspruechliches_402_beim_abholen_bricht_den_job_ab(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Gibt die Kontopause beim Abholen auf, geht `/cancel` fuer den Job hinaus.

    Nach der dritten widerspruechlichen 402 wirft `_mit_kontopause`, und das
    Paket gibt den Job auf, der bei RunPod aber laeuft und weiter kostet.
    """
    sha, _ = _anlegen(
        db_conn, tmp_path, b"ein bild", name="b.png", typ=DocumentType.PNG
    )

    class ImmerLeerBeimAbholen(FalscherRunPod):
        async def await_result(self, job_id: str) -> dict[str, Any]:
            raise InsufficientBalanceError("HTTP 402 beim Abfragen")

    runpod = ImmerLeerBeimAbholen()
    with pytest.raises(TerminalRunPodError, match="widersprechen einander"):
        await process_document(sha, settings=exec_settings, runpod=runpod, pool=pool)

    assert runpod.abgebrochen == ["job-0"]
    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.FAILED


async def test_endgueltiger_fehler_beim_abfragen_bricht_den_job_ab(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Endet das Warten mit einem Fehler, der den Job nicht beendet hat (etwa
    HTTP 429 auf `/status`), geht `/cancel` hinaus; der Fehler bleibt der Grund."""
    sha, _ = _anlegen(
        db_conn, tmp_path, b"ein bild", name="b.png", typ=DocumentType.PNG
    )

    class Gedrosselt(FalscherRunPod):
        async def await_result(self, job_id: str) -> dict[str, Any]:
            raise TerminalRunPodError("HTTP 429 beim Abfragen")

    runpod = Gedrosselt()
    with pytest.raises(TerminalRunPodError, match="429"):
        await process_document(sha, settings=exec_settings, runpod=runpod, pool=pool)

    assert runpod.abgebrochen == ["job-0"]


async def test_aufgegebener_job_wird_vom_executor_nicht_zweimal_abgebrochen(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """`JobAufgegebenError` mit `abgebrochen=True` (der Vorgabe) heisst: `/cancel`
    ging im Client schon hinaus."""
    sha, _ = _anlegen(
        db_conn, tmp_path, b"ein bild", name="b.png", typ=DocumentType.PNG
    )

    class Aufgegeben(FalscherRunPod):
        async def await_result(self, job_id: str) -> dict[str, Any]:
            raise JobAufgegebenError("Zeitbudget abgelaufen")

    runpod = Aufgegeben()
    with pytest.raises(JobAufgegebenError):
        await process_document(sha, settings=exec_settings, runpod=runpod, pool=pool)

    assert runpod.abgebrochen == []


async def test_verlorenes_ergebnis_wird_nicht_abgebrochen(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Bei HTTP 404 kennt RunPod den Job nicht mehr; ein `/cancel` liefe ins Leere."""
    sha, _ = _anlegen(
        db_conn, tmp_path, b"ein bild", name="b.png", typ=DocumentType.PNG
    )

    class Verloren(FalscherRunPod):
        async def await_result(self, job_id: str) -> dict[str, Any]:
            raise ResultLostError("HTTP 404")

    runpod = Verloren()
    with pytest.raises(ResultLostError):
        await process_document(sha, settings=exec_settings, runpod=runpod, pool=pool)

    assert runpod.abgebrochen == []


async def test_aufgegebener_job_mit_misslungenem_abbruch_wird_vom_executor_abgebrochen(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Ging das `/cancel` des Clients nicht hinaus (etwa weil RunPod nicht zu
    erreichen war), versucht der Executor es noch einmal.

    Sonst liefe der Job weiter, und die Wiederholung sendete dasselbe Teilstueck
    ein zweites Mal ab: genau die Doppelzahlung, die der Abbruch verhindern soll.
    """
    sha, _ = _anlegen(
        db_conn, tmp_path, b"ein bild", name="b.png", typ=DocumentType.PNG
    )

    class AbbruchMisslang(FalscherRunPod):
        async def await_result(self, job_id: str) -> dict[str, Any]:
            raise JobAufgegebenError("Abbruch misslang", abgebrochen=False)

    runpod = AbbruchMisslang()
    with pytest.raises(JobAufgegebenError):
        await process_document(sha, settings=exec_settings, runpod=runpod, pool=pool)

    assert runpod.abgebrochen == ["job-0"]


@pytest.mark.parametrize(
    "fehler",
    [
        TerminalRunPodError("HTTP 429 beim Abfragen"),
        JobAufgegebenError("Abbruch misslang", abgebrochen=False),
    ],
    ids=["anderer_fehler", "aufgegeben_ohne_abbruch"],
)
async def test_herunterfahren_waehrend_des_abbruchs_nach_einem_fehler_gibt_ihn_nicht_auf(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
    fehler: Exception,
) -> None:
    """Trifft das erste Signal zum Herunterfahren das `/cancel`, das nach einem
    Fehler hinausgehen soll, wird es noch einmal geschickt, und das
    `CancelledError` wird weitergeworfen.

    Der Handler des Fehlers faengt das Signal nicht von selbst: Es kaeme mitten im
    Abbruch an, und der Job liefe weiter.
    """
    sha, _ = _anlegen(
        db_conn, tmp_path, b"ein bild", name="b.png", typ=DocumentType.PNG
    )

    class Gedrosselt(FalscherRunPod):
        def __init__(self) -> None:
            super().__init__()
            self.versuche = 0

        async def await_result(self, job_id: str) -> dict[str, Any]:
            raise fehler

        async def cancel(self, job_id: str) -> dict[str, Any]:
            self.versuche += 1
            if self.versuche == 1:
                raise asyncio.CancelledError()
            return await super().cancel(job_id)

    runpod = Gedrosselt()
    with pytest.raises(asyncio.CancelledError):
        await process_document(sha, settings=exec_settings, runpod=runpod, pool=pool)

    assert runpod.abgebrochen == ["job-0"]
