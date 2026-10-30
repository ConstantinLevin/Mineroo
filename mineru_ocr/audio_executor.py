"""Audio ohne Teilstuecke, mit derselben Endschreib-Regel wie OCR (#70)."""
from __future__ import annotations

import asyncio
from typing import Any

import httpx
from psycopg_pool import AsyncConnectionPool

from . import core, db, elevenlabs, progress
from .abbruch import abwarten_bis_zum_zweiten_abbruch
from .config import Settings
from .errors import CorruptDocumentError, InputError, UnstorableResultError, should_run_again
from .executor import ABBRUCH_IM_LETZTEN_VERSUCH, _text_speicherbar
from .mime import sniff_bytes


async def process_audio(
    sha256: str, *, settings: Settings, pool: AsyncConnectionPool,
    letzter_versuch: bool = False, http: httpx.AsyncClient | None = None,
) -> None:
    """Liest eine Eingabe, transkribiert einmal und schreibt nur am Ende.

    Fertige Ergebnisse sind kostenfreie Treffer. Wiederholbare Fehler werden
    ohne Ergebnis weitergeworfen, im letzten Versuch steht vorher `failed`.
    Ein bezahltes Ergebnis wird beim Herunterfahren abgeschirmt gespeichert.
    """
    roh: dict[str, Any] = {"provider": "elevenlabs"}
    art: str | None = None
    groesse: int | None = None

    async def schreiben(status: db.DocumentStatus, error: str | None = None) -> None:
        async with pool.connection() as conn:
            await db.write_terminal_async(
                conn, sha256=sha256, status=status, raw=roh, endpoint_id="elevenlabs",
                mime=art, size_bytes=groesse, error=error, store_read_settings=False,
            )

    try:
        async with pool.connection() as conn:
            if await db.get_status_async(conn, sha256) in (
                db.DocumentStatus.DONE, db.DocumentStatus.EMPTY, db.DocumentStatus.INPUT_ERROR,
            ):
                await db.delete_input_async(conn, sha256)
                return
        eingabe = await db.read_input_async(settings.db_dsn, sha256)
        if eingabe is None or core.sha256_of(eingabe.content) != sha256:
            raise CorruptDocumentError("Audio-Eingabe fehlt oder ihr Hash stimmt nicht.")
        typ = sniff_bytes(eingabe.content)
        if not typ.is_audio:
            raise CorruptDocumentError("Audio-Task erhielt eine Eingabe ohne Audiokennung.")
        art, groesse = typ.mime, len(eingabe.content)
        core.check_size(groesse, "die Audio-Eingabe")
        progress.melden("ElevenLabs verarbeitet")
        if http is None:
            async with httpx.AsyncClient() as zugang:
                roh = await elevenlabs.transcribe(eingabe.content, typ, settings=settings, http=zugang)
        else:
            roh = await elevenlabs.transcribe(eingabe.content, typ, settings=settings, http=http)
        progress.melden("Antwort verarbeiten")
        text = elevenlabs.transcript(roh)
        status = db.DocumentStatus.DONE if text else db.DocumentStatus.EMPTY
        progress.melden("Ergebnis speichern")
        await abwarten_bis_zum_zweiten_abbruch(schreiben(status))
    except (Exception, asyncio.CancelledError) as fehler:
        if should_run_again(fehler) and not letzter_versuch:
            raise
        beschreibung = _text_speicherbar(
            ABBRUCH_IM_LETZTEN_VERSUCH if isinstance(fehler, asyncio.CancelledError)
            else f"{type(fehler).__name__}: {fehler}"
        )
        roh["failure"] = {"type": type(fehler).__name__, "message": beschreibung}
        status = db.DocumentStatus.INPUT_ERROR if isinstance(fehler, InputError) else db.DocumentStatus.FAILED

        async def fehler_schreiben() -> None:
            nonlocal roh
            try:
                if isinstance(fehler, UnstorableResultError):
                    raise fehler
                await schreiben(status, beschreibung)
            except UnstorableResultError:
                # Unveraenderte Rohantwort bleibt nicht speicherbar; wie OCR
                # einen ausdruecklichen Fehlschlag ohne Inhalt hinterlassen.
                roh = {"provider": "elevenlabs", "failure": roh["failure"],
                       "ergebnis_nicht_speicherbar": True}
                await schreiben(db.DocumentStatus.FAILED, beschreibung)

        await abwarten_bis_zum_zweiten_abbruch(fehler_schreiben())
        raise
