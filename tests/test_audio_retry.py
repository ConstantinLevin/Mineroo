"""Audio-Fehler und Wiederholung (#71), nur erfundene Antworten und Test-DB."""
from __future__ import annotations

import asyncio
import io
import wave
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest

from mineru_ocr import daemon, db
from mineru_ocr.client import OCRClient
from mineru_ocr.config import Settings
from mineru_ocr.core import RetryOutcome
from mineru_ocr.executor import ABBRUCH_IM_LETZTEN_VERSUCH

pytestmark = pytest.mark.db
DATEN = b'fLaC' + bytes(45)
ANTWORT = {'words': [{'type': 'word', 'text': 'Erfunden.', 'speaker_id': 's'}]}


@pytest.fixture
def audio_settings(settings: Settings, tmp_path: Path) -> Settings:
    """Jeder gestartete Test-Daemon hat eine ausdrueckliche Scratch-Konfiguration."""
    datei = tmp_path / 'audio.toml'
    datei.write_text(f'db_dsn = "{settings.db_dsn}"\nendpoint_id = "testendpoint"\n')
    return replace(settings, config_path=datei, elevenlabs_api_key='erfunden-audio',
                   audio_concurrency=1, ocr_concurrency=2,
                   shutdown_graceful_timeout=0.01, retry_base_seconds=0.01,
                   retry_factor=1, balance_recheck_seconds=0.03)


@asynccontextmanager
async def audio_daemon(
    settings: Settings, monkeypatch: pytest.MonkeyPatch,
    antworten: Callable[[httpx.Request], Any],
) -> AsyncIterator[asyncio.Task[None]]:
    """Echter Daemon; ausschliesslich die HTTP-Gegenstelle wird ersetzt.

    Kein ausgehender Netzverkehr, auch nicht zu RunPod. Nur Audio wird bedient.
    Abbruch und Pool-Aufraeumen bleiben die unveraenderten Produktionspfade.
    """
    original = httpx.AsyncClient
    async def transport(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == 'https://api.elevenlabs.io/v1/speech-to-text'
        ergebnis = antworten(request)
        if asyncio.iscoroutine(ergebnis):
            return await ergebnis  # type: ignore[no-any-return]
        assert isinstance(ergebnis, httpx.Response)
        return ergebnis
    def bauen(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        return original(*args, **{**kwargs, 'transport': httpx.MockTransport(transport)})
    with monkeypatch.context() as patch:
        patch.setattr(httpx, 'AsyncClient', bauen)
        lauf = asyncio.create_task(daemon.run(settings, queues=['audio']))
        try:
            yield lauf
        finally:
            lauf.cancel()
            try:
                await asyncio.wait_for(lauf, 5)
            except asyncio.CancelledError:
                pass
        assert lauf.done()


async def bis_ergebnis(
    klient: OCRClient, sha: str, lauf: asyncio.Task[None],
) -> None:
    # Vorgemerkte Wiederholungen holt Procrastinate im 5-s-Abfragetakt.
    async with asyncio.timeout(20):
        while db.get_document(klient.conn, sha) is None:
            if lauf.done():
                await lauf
                raise AssertionError('Daemon endete ohne Ergebnis.')
            await asyncio.sleep(0.01)


@pytest.mark.parametrize(('fehlerart', 'zustand', 'versuche'), [
    ('schluessel', 'failed', 1), ('fehlender_schluessel', 'failed', 0),
    ('datei', 'input_error', 1), ('429', 'failed', 3), ('503', 'failed', 3),
    ('timeout', 'failed', 3), ('verbindung', 'failed', 3),
])
async def test_audio_job_schreibt_genau_dann_terminal_wenn_er_nicht_wiederholt_wird(
    db_conn: psycopg.Connection[Any], audio_settings: Settings, monkeypatch: pytest.MonkeyPatch,
    fehlerart: str, zustand: str, versuche: int,
) -> None:
    daten = DATEN
    if fehlerart == 'timeout':
        ausgabe = io.BytesIO()
        with wave.open(ausgabe, 'wb') as wav:
            wav.setnchannels(1)
            wav.setsampwidth(1)
            wav.setframerate(14)
            wav.writeframes(bytes(50400))  # Eine Stunde Stille, ohne echte Aufnahme.
        daten = ausgabe.getvalue()
    kontakte = 0
    def antworten(request: httpx.Request) -> httpx.Response:
        nonlocal kontakte
        kontakte += 1
        assert db.get_document(db_conn, str(sha)) is None
        if fehlerart == 'timeout':
            assert request.extensions['timeout']['read'] >= 600
            raise httpx.ReadTimeout('erfundener Timeout')
        if fehlerart == 'verbindung':
            raise httpx.RemoteProtocolError('erfundener Verbindungsabbruch')
        code = {'schluessel': 401, 'datei': 422, '429': 429, '503': 503}[fehlerart]
        return httpx.Response(code, json={'detail': {'status': 'invalid_audio', 'message': 'Audiospur fehlt'}})
    einstellung = replace(audio_settings, elevenlabs_api_key=None) if fehlerart == 'fehlender_schluessel' else audio_settings
    with OCRClient(db_dsn=einstellung.db_dsn, preflight=False) as klient:
        sha = klient.ocr_start(daten)
        async with audio_daemon(einstellung, monkeypatch, antworten) as lauf:
            await bis_ergebnis(klient, sha, lauf)
        huelle = klient.get(sha)
        assert huelle.status.value == zustand
        assert huelle.raw['failure']['type']
        if fehlerart == 'datei':
            assert 'Audiospur fehlt' in (huelle.row.error or '')
        assert huelle.to_dict()['endpoint_id'] == 'elevenlabs'
        assert kontakte == versuche
        assert db_conn.execute('SELECT queue_name, status, attempts FROM procrastinate_jobs WHERE queue_name = \'audio\'').fetchone() == (
            'audio', 'failed', max(1, versuche),
        )
        assert klient.retry(sha) is (RetryOutcome.HAS_RESULT if zustand == 'input_error' else RetryOutcome.RESET)
    assert db_conn.execute('SELECT count(*) FROM procrastinate_workers').fetchone() == (0,)


@pytest.mark.parametrize('wiederholen', ['retry', 'retry_all', 'retry_all_ohne_liste'])
@pytest.mark.parametrize('job_bleibt', [True, False])
async def test_audio_retry_liest_failed_neu_ueber_die_audio_queue(
    db_conn: psycopg.Connection[Any], audio_settings: Settings, monkeypatch: pytest.MonkeyPatch,
    wiederholen: str, job_bleibt: bool,
) -> None:
    with OCRClient(db_dsn=audio_settings.db_dsn, preflight=False) as klient:
        sha = klient.ocr_start(DATEN)
        async with audio_daemon(audio_settings, monkeypatch, lambda r: httpx.Response(401)) as lauf:
            await bis_ergebnis(klient, sha, lauf)
        assert klient.get(sha).status is db.DocumentStatus.FAILED
        if not job_bleibt:
            db_conn.execute('DELETE FROM procrastinate_jobs')
        if wiederholen == 'retry':
            ausgang = klient.retry(sha)
        else:
            ausgaenge = klient.retry_all([sha] if wiederholen == 'retry_all' else None)
            ausgang = ausgaenge[sha]
        assert ausgang is (RetryOutcome.RESET if job_bleibt else RetryOutcome.REQUEUED)
        assert db_conn.execute('SELECT queue_name, task_name FROM procrastinate_jobs WHERE queue_name = \'audio\'').fetchone() == (
            'audio', 'mineru_ocr.audio_document',
        )
        kontakte = 0
        def antworten(request: httpx.Request) -> httpx.Response:
            nonlocal kontakte
            kontakte += 1
            assert DATEN in request.content
            return httpx.Response(200, json=ANTWORT)
        async with audio_daemon(audio_settings, monkeypatch, antworten) as lauf:
            await bis_ergebnis(klient, sha, lauf)
        assert klient.get(sha).rendered_markdown == 'Sprecher 1: Erfunden.'
        assert kontakte == 1
        assert klient.ocr_start(DATEN).queued is False
    assert db_conn.execute('SELECT count(*) FROM procrastinate_workers').fetchone() == (0,)


@pytest.mark.parametrize('letzter', [False, True])
@pytest.mark.parametrize('pause', [False, True])
async def test_audio_herunterfahren_waehrend_anfrage_oder_kontingentpause(
    db_conn: psycopg.Connection[Any], audio_settings: Settings, monkeypatch: pytest.MonkeyPatch,
    letzter: bool, pause: bool,
) -> None:
    erreicht = asyncio.Event()
    async def antworten(request: httpx.Request) -> httpx.Response:
        erreicht.set()
        if pause:
            return httpx.Response(401, json={'detail': {'status': 'quota_exceeded'}})
        await asyncio.Event().wait()
        raise AssertionError('Die Anfrage sollte abgebrochen werden.')
    einstellung = replace(audio_settings, max_attempts=1 if letzter else 3, balance_recheck_seconds=900)
    with OCRClient(db_dsn=einstellung.db_dsn, preflight=False) as klient:
        sha = klient.ocr_start(DATEN)
        async with audio_daemon(einstellung, monkeypatch, antworten):
            await asyncio.wait_for(erreicht.wait(), 5)
            # Bei Kontingentpause muss die Antwort den wartenden Executor erreicht haben.
            await asyncio.sleep(0.03)
        stand = db_conn.execute('SELECT status, attempts FROM procrastinate_jobs WHERE queue_name = \'audio\'').fetchone()
        if letzter:
            assert stand == ('aborted', 1)
            huelle = klient.get(sha)
            assert huelle.status is db.DocumentStatus.FAILED
            assert ABBRUCH_IM_LETZTEN_VERSUCH in (huelle.row.error or '')
        else:
            assert stand == ('todo', 1)
            assert db.get_document(db_conn, sha) is None
            async with audio_daemon(einstellung, monkeypatch, lambda r: httpx.Response(200, json=ANTWORT)) as lauf:
                await bis_ergebnis(klient, sha, lauf)
            assert klient.get(sha).rendered_markdown == 'Sprecher 1: Erfunden.'
    assert db_conn.execute('SELECT count(*) FROM procrastinate_workers').fetchone() == (0,)
