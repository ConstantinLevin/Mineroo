"""Audio-Vertrag (#70), ausschliesslich erfundene Bytes und Antworten."""
from __future__ import annotations

import errno
import io
from unittest.mock import MagicMock, Mock

import pytest

from mineru_ocr import core, mime
from mineru_ocr.config import Settings


@pytest.mark.parametrize(('daten', 'name'), [
    (b'ID3\x04\x00\x00\x00\x00\x00\x00', 'MP3'),
    (b'\xff\xfb\x90\x64' + bytes(20), 'MP3'),
    (b'\x00\x00\x00\x18ftypM4A \x00\x00\x00\x00M4A isom', 'M4A'),
    (b'\xff\xf1\x50\x80\x01\x7f\xfc', 'AAC'),
    (b'RIFF' + bytes(4) + b'WAVEfmt ', 'WAV'),
    (b'fLaC' + bytes(20), 'FLAC'),
    (b'OggS' + bytes(22) + b'\x01\x08OpusHead', 'OGG'),
    (b'FORM' + bytes(4) + b'AIFF', 'AIFF'),
    (b'FORM' + bytes(4) + b'AIFC', 'AIFF'),
])
def test_audio_wird_am_inhalt_erkannt(daten: bytes, name: str, settings: Settings) -> None:
    typ = mime.sniff_bytes(daten)
    assert typ.name == name
    assert typ.is_audio
    assert not typ.is_whole_document
    assert mime.sniff_file(io.BytesIO(daten)) is typ
    assert core.prepare(daten, settings).doc_type is typ


def test_audio_hat_nur_die_datenbankgrenze(settings: Settings) -> None:
    import dataclasses
    daten = b'fLaC' + bytes(1000)
    assert core.prepare(daten, dataclasses.replace(settings, max_payload_bytes=10)).size_bytes == len(daten)


def test_audio_hat_eigenen_task_und_worker(settings: Settings) -> None:
    from procrastinate import testing
    import procrastinate
    from mineru_ocr import tasks, daemon
    app = procrastinate.App(connector=testing.InMemoryConnector())
    kennung = tasks.enqueue_document(app, object(), 'a' * 64, doc_type=mime.DocumentType.FLAC)
    job = app.job_manager.list_jobs(id=kennung)[0]
    assert (job.queue, job.task_name, job.lock, job.task_kwargs) == (
        'audio', 'mineru_ocr.audio_document', 'a' * 64, {'sha256': 'a' * 64},
    )
    anmeldung = tasks.build_daemon_app(settings).tasks['mineru_ocr.audio_document']
    assert anmeldung.queue == 'audio'
    worker = daemon._worker_optionen(settings, None, {})
    audio = [w for w in worker if w['queues'] == ['audio']]
    assert len(audio) == 1
    assert audio[0]['concurrency'] == settings.audio_concurrency < 8


@pytest.mark.db
async def test_audio_vom_abschicken_bis_zum_cache(db_conn: object, settings: Settings) -> None:
    import dataclasses
    from typing import Any, cast
    import httpx
    from psycopg_pool import AsyncConnectionPool
    from mineru_ocr import db
    from mineru_ocr.client import OCRClient, AsyncOCRClient
    from mineru_ocr.audio_executor import process_audio
    from mineru_ocr.wrapper import OCRWrapper

    conn = cast(Any, db_conn)
    einstellung = dataclasses.replace(settings, elevenlabs_api_key='erfunden-audio')
    daten = b'fLaC' + bytes(200)
    antwort = {
        'text': 'Hallo Welt. [lacht] Guten Tag.', 'language_code': 'deu',
        'language_probability': 0.99, 'audio_duration_secs': 14.0,
        'transcription_id': 'erfunden-70', 'words': [
            {'type': 'word', 'text': 'Hallo', 'speaker_id': 'speaker_7', 'start': 0.0, 'end': 1.0},
            {'type': 'spacing', 'text': ' ', 'speaker_id': 'speaker_7'},
            {'type': 'word', 'text': 'Welt.', 'speaker_id': 'speaker_7'},
            {'type': 'spacing', 'text': ' ', 'speaker_id': 'speaker_7'},
            {'type': 'audio_event', 'text': '[lacht]', 'speaker_id': None},
            {'type': 'word', 'text': 'Guten Tag.', 'speaker_id': 'speaker_2'},
            {'type': 'word', 'text': 'Weiter.', 'speaker_id': 'speaker_7'},
        ], 'zusaetzliches_feld': {'bleibt': True},
    }
    kontakte = []
    def antworten(request: httpx.Request) -> httpx.Response:
        kontakte.append(request)
        assert str(request.url) == 'https://api.elevenlabs.io/v1/speech-to-text'
        assert request.headers['xi-api-key'] == 'erfunden-audio'
        assert b'name="model_id"\r\n\r\nscribe_v2' in request.content
        assert b'name="diarize"\r\n\r\ntrue' in request.content
        assert b'name="tag_audio_events"\r\n\r\ntrue' in request.content
        assert daten in request.content
        return httpx.Response(200, json=antwort)

    with OCRClient(db_dsn=settings.db_dsn, preflight=False) as klient:
        sha = klient.ocr_start(daten)
        job = conn.execute('SELECT queue_name, task_name FROM procrastinate_jobs').fetchone()
        assert job == ('audio', 'mineru_ocr.audio_document')
        assert klient.ocr_start(daten).queued is False
    async with AsyncConnectionPool(settings.db_dsn, min_size=1, max_size=2, open=False) as pool:
        async with httpx.AsyncClient(transport=httpx.MockTransport(antworten)) as http:
            await process_audio(str(sha), settings=einstellung, pool=pool, http=http)
            await process_audio(str(sha), settings=einstellung, pool=pool, http=http)
    zeile = db.get_document(conn, str(sha))
    assert zeile is not None
    assert zeile.status is db.DocumentStatus.DONE
    assert zeile.endpoint_id == 'elevenlabs'
    assert zeile.mime == 'audio/flac' and zeile.size_bytes == len(daten)
    assert (zeile.mineru_version, zeile.read_settings, zeile.image_tag_at_submit, zeile.page_count) == (None, None, None, None)
    huelle = OCRWrapper.from_row(zeile, conn)
    assert huelle.raw['response'] == antwort
    assert huelle.raw['parameters'] == {'model_id': 'scribe_v2', 'diarize': 'true', 'tag_audio_events': 'true'}
    assert huelle.rendered_markdown == 'Sprecher 1: Hallo Welt. [lacht]\n\nSprecher 2: Guten Tag.\n\nSprecher 1: Weiter.'
    assert huelle.readable_markdown == huelle.rendered_markdown
    assert conn.execute('SELECT count(*) FROM inputs').fetchone() == (0,)
    with OCRClient(db_dsn=settings.db_dsn, preflight=False) as klient:
        assert klient.ocr_start(daten).status is db.DocumentStatus.DONE
        assert klient.get(sha).rendered_markdown == huelle.rendered_markdown
    async with AsyncOCRClient(db_dsn=settings.db_dsn) as klient_async:
        assert (await klient_async.ocr_start(daten)).queued is False
        assert (await klient_async.get(sha)).readable_markdown == huelle.rendered_markdown
    assert len(kontakte) == 1


def test_geraeusch_vor_dem_ersten_wort_erfindet_keinen_sprecher() -> None:
    from mineru_ocr.elevenlabs import transcript
    raw = {'response': {'words': [
        {'type': 'audio_event', 'text': '[Musik]', 'speaker_id': None},
        {'type': 'spacing', 'text': ' '},
        {'type': 'word', 'text': 'Hallo.', 'speaker_id': 'speaker_4'},
    ]}}
    assert transcript(raw) == 'Sprecher 1: [Musik] Hallo.'


def test_audio_konfiguration_und_geheimnis(tmp_path: object) -> None:
    from pathlib import Path
    from typing import cast
    from mineru_ocr.errors import ConfigurationError
    datei = cast(Path, tmp_path) / 'audio.toml'
    datei.write_text('audio_concurrency = 3\naudio_region = "us"\n')
    einstellung = Settings.load(db_dsn='dbname=mineru_ocr_test', config_path=datei,
                               environ={'ELEVENLABS_API_KEY': 'erfundenes-geheimnis'})
    assert einstellung.audio_concurrency == 3
    assert einstellung.audio_region == 'us'
    assert einstellung.require_elevenlabs_api_key() == 'erfundenes-geheimnis'
    assert 'erfundenes-geheimnis' not in repr(einstellung)
    umgebung = Settings.load(db_dsn='dbname=mineru_ocr_test', config_path=datei,
                            environ={'MINERU_OCR_AUDIO_CONCURRENCY': '4'})
    assert umgebung.audio_concurrency == 4
    with pytest.raises(ConfigurationError, match='ELEVENLABS_API_KEY'):
        umgebung.require_elevenlabs_api_key()
    datei.write_text('elevenlabs_api_key = "darf-nicht-in-toml"\n')
    with pytest.raises(ConfigurationError) as fehler:
        Settings.load(db_dsn='dbname=mineru_ocr_test', config_path=datei, environ={})
    assert 'darf-nicht-in-toml' not in str(fehler.value)


@pytest.mark.db
@pytest.mark.parametrize('woerter', [[], [{'type': 'audio_event', 'text': '[Musik]'}]])
async def test_audio_ohne_wort_ist_empty(
    db_conn: object, settings: Settings, woerter: list[dict[str, str]],
) -> None:
    import dataclasses
    from typing import Any, cast
    import httpx
    from psycopg_pool import AsyncConnectionPool
    from mineru_ocr import db
    from mineru_ocr.audio_executor import process_audio
    from mineru_ocr.client import OCRClient
    conn = cast(Any, db_conn)
    with OCRClient(db_dsn=settings.db_dsn, preflight=False) as klient:
        sha = klient.ocr_start(b'fLaC' + bytes(40))
    async with AsyncConnectionPool(settings.db_dsn, min_size=1, max_size=1, open=False) as pool:
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={'text': '[Musik]', 'words': woerter}),
        )) as http:
            await process_audio(str(sha), settings=dataclasses.replace(
                settings, elevenlabs_api_key='erfundener-schluessel',
            ), pool=pool, http=http)
    with OCRClient(db_dsn=settings.db_dsn, preflight=False) as klient:
        huelle = klient.get(sha)
        assert huelle.status is db.DocumentStatus.EMPTY
        assert huelle.rendered_markdown == ''
        assert huelle.raw['response']['words'] == woerter
    assert conn.execute('SELECT count(*) FROM inputs').fetchone() == (0,)


@pytest.mark.db
async def test_audio_async_abschicken_und_retry_waehlen_audio(db_conn: object, settings: Settings) -> None:
    from typing import Any, cast
    from mineru_ocr.client import OCRClient, AsyncOCRClient
    from mineru_ocr.core import RetryOutcome
    conn = cast(Any, db_conn)
    async with AsyncOCRClient(db_dsn=settings.db_dsn) as klient:
        sha = await klient.ocr_start(b'fLaC' + bytes(41))
    assert conn.execute('SELECT queue_name FROM procrastinate_jobs').fetchone() == ('audio',)
    conn.execute('DELETE FROM procrastinate_jobs')
    with OCRClient(db_dsn=settings.db_dsn, preflight=False) as sync:
        assert sync.retry(sha) is RetryOutcome.REQUEUED
        assert sync.retry(sha) is RetryOutcome.UNDERWAY
    assert conn.execute('SELECT queue_name, task_name FROM procrastinate_jobs').fetchall() == [
        ('audio', 'mineru_ocr.audio_document'),
    ]


@pytest.mark.db
async def test_cli_mischt_audio_und_pdf_und_exportiert_ohne_neuen_lauf(
    db_conn: object, settings: Settings, tmp_path: object, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import dataclasses
    import json
    import os
    from pathlib import Path
    import subprocess
    import sys
    from typing import Any, cast
    import httpx
    import ersatz_antworten as ersatz
    from test_pdf import _pdf_mit_seiten
    from test_integration import ProtokollRunPod
    from mineru_ocr import daemon, tasks

    conn = cast(Any, db_conn)
    pfad = cast(Path, tmp_path)
    ordner = pfad / 'eingang'
    ordner.mkdir()
    # Die Endungen luegen absichtlich: Entscheidend ist der Inhalt.
    (ordner / 'a.pdf').write_bytes(b'fLaC' + bytes(42))
    (ordner / 'b.m4a').write_bytes(_pdf_mit_seiten(1))
    config = pfad / 'config.toml'
    config.write_text(f'db_dsn = "{settings.db_dsn}"\nendpoint_id = "testendpoint"\n')
    fake_key = pfad / 'runpod.toml'
    fake_key.write_text('apikey = "erfunden-kein-runpod-zugang"\n')
    einstellung = dataclasses.replace(settings, config_path=config, elevenlabs_api_key='erfunden-audio')
    runpod = ProtokollRunPod(lambda nr: ersatz.fertige_antwort('PDF erfunden.'))
    monkeypatch.setattr(tasks, 'RunPodClient', lambda _: runpod)
    original_http = httpx.AsyncClient
    kontakte: list[str] = []
    def antworten(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == 'https://api.elevenlabs.io/v1/speech-to-text'
        kontakte.append(str(request.url))
        return httpx.Response(200, json={
            'text': '<b>Hallo</b>.', 'words': [
                {'type': 'word', 'text': '<b>Hallo</b>.', 'speaker_id': 'speaker_0'},
            ],
        })
    def http_bauen(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        return original_http(*args, **{**kwargs, 'transport': httpx.MockTransport(antworten)})
    monkeypatch.setattr(httpx, 'AsyncClient', http_bauen)
    env = {**os.environ, 'MINERU_OCR_CONFIG': str(config), 'MINERU_OCR_DB_DSN': settings.db_dsn}
    einstieg = (
        'import sys; from pathlib import Path; from mineru_ocr import config; '
        f'config.RUNPOD_CONFIG_PATH = Path({str(fake_key)!r}); '
        'from mineru_ocr.cli import main; main()'
    )
    def aufruf(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, '-c', einstieg, *args], env=env,
                              capture_output=True, text=True, timeout=15)
    lauf = asyncio.create_task(daemon.run(einstellung))
    try:
        async with asyncio.timeout(10):
            while conn.execute('SELECT count(*) FROM procrastinate_workers').fetchone()[0] != 3:
                if lauf.done():
                    await lauf
                await asyncio.sleep(0.01)
        ausgabe = await asyncio.to_thread(aufruf, str(ordner), '--timeout', '10')
        assert ausgabe.returncode == 0, ausgabe.stderr
        assert ausgabe.stdout.index('Sprecher 1: <b>Hallo</b>.') < ausgabe.stdout.index('PDF erfunden.')
    finally:
        lauf.cancel()
        try:
            await lauf
        except asyncio.CancelledError:
            pass
    assert conn.execute('SELECT count(*) FROM procrastinate_workers').fetchone() == (0,)
    assert len(kontakte) == runpod.aufrufe == 1
    for optionen in ([], ['--re'], ['-t'], ['-o', str(pfad / 'text.md')],
                     ['--ra', '-o', str(pfad / 'roh.json')]):
        ausgabe = await asyncio.to_thread(aufruf, str(ordner / 'a.pdf'), *optionen)
        assert ausgabe.returncode == 0, ausgabe.stderr
        if '--ra' in optionen:
            roh = json.loads(Path(ausgabe.stdout.strip()).read_text())
            assert roh['raw']['response']['text'] == '<b>Hallo</b>.'
        elif '-t' in optionen or '-o' in optionen:
            ziel = Path(ausgabe.stdout.strip())
            assert ziel.read_text() == 'Sprecher 1: <b>Hallo</b>.'
            if '-t' in optionen:
                ziel.unlink()
        else:
            assert ausgabe.stdout.strip() == 'Sprecher 1: <b>Hallo</b>.'
    assert len(kontakte) == runpod.aufrufe == 1


@pytest.mark.db
@pytest.mark.parametrize(('http_status', 'antwort', 'fehlername', 'endzustand'), [
    (401, {'detail': 'erfundenes-audio-geheimnis'}, 'ConfigurationError', 'failed'),
    (422, {'detail': 'erfundenes-audio-geheimnis'}, 'WorkerRejectedError', 'input_error'),
    (200, {'text': 'Antwort ohne Wortdaten'}, 'IncompleteResultError', 'failed'),
    (200, {'words': [{'type': 'word', 'text': '\x00', 'speaker_id': 's'}]}, 'UnstorableResultError', 'failed'),
])
async def test_audio_schreibt_endzustand_auch_bei_unverwertbarer_antwort(
    db_conn: object, settings: Settings, http_status: int, antwort: dict[str, object],
    fehlername: str, endzustand: str,
) -> None:
    import dataclasses
    from typing import Any, cast
    import httpx
    from psycopg_pool import AsyncConnectionPool
    from mineru_ocr import db, errors
    from mineru_ocr.client import OCRClient
    from mineru_ocr.audio_executor import process_audio
    conn = cast(Any, db_conn)
    daten = b'fLaC' + bytes(43)
    einstellung = dataclasses.replace(settings, elevenlabs_api_key='erfundenes-audio-geheimnis')
    with OCRClient(db_dsn=settings.db_dsn, preflight=False) as klient:
        sha = klient.ocr_start(daten)
    async with AsyncConnectionPool(settings.db_dsn, min_size=1, max_size=1, open=False) as pool:
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(http_status, json=antwort),
        )) as http:
            with pytest.raises(getattr(errors, fehlername)):
                await process_audio(str(sha), settings=einstellung, pool=pool, http=http)
    zeile = db.get_document(conn, str(sha))
    assert zeile is not None and zeile.status.value == endzustand
    assert zeile.endpoint_id == 'elevenlabs' and zeile.read_settings is None
    assert 'erfundenes-audio-geheimnis' not in repr(zeile)
    assert conn.execute('SELECT count(*) FROM inputs').fetchone() == ((0 if endzustand == 'input_error' else 1),)
    with OCRClient(db_dsn=settings.db_dsn, preflight=False) as klient:
        assert klient.ocr_start(daten).status is zeile.status


@pytest.mark.parametrize(('code', 'detail', 'klasse', 'art', 'grund'), [
    (401, {'status': 'invalid_api_key', 'message': 'falscher Schluessel'}, 'ConfigurationError', 'terminal', None),
    (403, 'Zugriff verweigert', 'ConfigurationError', 'terminal', None),
    (422, {'status': 'invalid_audio', 'message': 'Audiospur fehlt: erfundenes-geheimnis'}, 'WorkerRejectedError', 'terminal', 'Audiospur fehlt'),
    (422, [{'loc': ['body', 'file'], 'msg': 'Datei ist abgeschnitten', 'type': 'value_error'}], 'WorkerRejectedError', 'terminal', 'Datei ist abgeschnitten'),
    (429, {'status': 'too_many_concurrent_requests'}, 'HTTPStatusError', 'retryable', None),
    (500, 'interner Fehler', 'HTTPStatusError', 'retryable', None),
    (503, 'nicht erreichbar', 'HTTPStatusError', 'retryable', None),
    (401, {'status': 'quota_exceeded', 'message': 'Kontingent erschoepft'}, 'InsufficientBalanceError', 'balance_pause', None),
    (400, {'status': 'quota_exceeded'}, 'InsufficientBalanceError', 'balance_pause', None),
    (402, 'Kontingent erschoepft', 'InsufficientBalanceError', 'balance_pause', None),
    (400, 'andere Ablehnung', 'HTTPStatusError', 'terminal', None),
])
async def test_audio_fehler_geht_durch_die_zentrale_einordnung(
    settings: Settings, code: int, detail: object, klasse: str, art: str, grund: str | None,
) -> None:
    """429/5xx duerfen nicht endgueltig werden; 422 behaelt seinen Grund."""
    import dataclasses
    import httpx
    from mineru_ocr import errors
    from mineru_ocr.elevenlabs import transcribe
    from mineru_ocr.mime import DocumentType
    schluessel = 'erfundenes-geheimnis'
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(code, json={'detail': detail}),
    )) as http:
        with pytest.raises(Exception) as gefangen:
            await transcribe(b'fLaC' + bytes(43), DocumentType.FLAC,
                             settings=dataclasses.replace(settings, elevenlabs_api_key=schluessel), http=http)
    fehler = gefangen.value
    assert type(fehler).__name__ == klasse
    assert errors.classify_failure(fehler).value == art
    assert errors.should_run_again(fehler) is (art != 'terminal')
    assert schluessel not in str(fehler)
    if grund is not None:
        assert grund in str(fehler)


async def test_audio_frist_waechst_mit_dauer_und_timeout_bleibt_wiederholbar(
    settings: Settings, audio_dauerprobe: Mock,
) -> None:
    """Gleich grosse synthetische WAVs mit 14 s und 1 h brauchen andere Fristen."""
    import dataclasses
    import subprocess
    import wave
    import httpx
    from mineru_ocr.elevenlabs import transcribe
    from mineru_ocr.errors import classify_failure, FailureKind
    audio_dauerprobe.side_effect = [
        subprocess.CompletedProcess(args=[], returncode=0, stdout=dauer)
        for dauer in (b'14\n', b'3600\n')
    ]
    fristen: list[float] = []
    def zeitueberschreitung(request: httpx.Request) -> httpx.Response:
        fristen.append(request.extensions['timeout']['read'])
        raise httpx.ReadTimeout('erfundenes-geheimnis')
    async with httpx.AsyncClient(transport=httpx.MockTransport(zeitueberschreitung)) as http:
        for frequenz in (3600, 14):
            ausgabe = io.BytesIO()
            with wave.open(ausgabe, 'wb') as wav:
                wav.setnchannels(1)
                wav.setsampwidth(1)
                wav.setframerate(frequenz)
                wav.writeframes(bytes(50400))
            with pytest.raises(httpx.TransportError) as gefangen:
                await transcribe(ausgabe.getvalue(), mime.DocumentType.WAV,
                                 settings=dataclasses.replace(settings, elevenlabs_api_key='erfundenes-geheimnis'), http=http)
            assert classify_failure(gefangen.value) is FailureKind.RETRYABLE
            assert 'erfundenes-geheimnis' not in str(gefangen.value)
    assert fristen == [301.4, 660.0]
    assert audio_dauerprobe.call_count == 2


def test_audio_frist_ohne_ffprobe_schaetzt_nach_bytezahl(monkeypatch: pytest.MonkeyPatch) -> None:
    from mineru_ocr.elevenlabs import _anfragefrist
    monkeypatch.setattr('mineru_ocr.elevenlabs.shutil.which', lambda name: None)
    probe = Mock(side_effect=AssertionError('Ohne ffprobe darf keine Probe starten.'))
    monkeypatch.setattr('mineru_ocr.elevenlabs.subprocess.run', probe)
    assert _anfragefrist(bytes(50444), mime.DocumentType.WAV) == 305.0444
    assert _anfragefrist(bytes(100000), mime.DocumentType.WAV) == 310.0
    probe.assert_not_called()


@pytest.mark.parametrize('stelle', ['anlegen', 'schreiben', 'flush'])
@pytest.mark.parametrize('fehlernummer', [errno.ENOSPC, errno.EACCES], ids=['ENOSPC', 'EACCES'])
async def test_audio_temporaerfehler_verhindert_keine_transkription(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, audio_dauerprobe: Mock,
    stelle: str, fehlernummer: int,
) -> None:
    """Optionale Metadaten duerfen gueltige Audio-Bytes nicht endgueltig ablehnen."""
    import dataclasses
    import httpx
    from mineru_ocr.elevenlabs import transcribe
    datei = MagicMock()
    temporaer = Mock(return_value=datei)
    fehler = OSError(fehlernummer, 'erfundener Speicherfehler')
    if stelle == 'anlegen':
        temporaer.side_effect = fehler
    else:
        getattr(datei.__enter__.return_value, 'write' if stelle == 'schreiben' else 'flush').side_effect = fehler
    monkeypatch.setattr('mineru_ocr.elevenlabs.tempfile.NamedTemporaryFile', temporaer)
    daten = b'fLaC' + bytes(50440)
    kontakte: list[httpx.Request] = []
    antwort = {'words': [{'type': 'word', 'text': 'Erfunden.', 'speaker_id': 's'}]}
    def antworten(request: httpx.Request) -> httpx.Response:
        kontakte.append(request)
        assert daten in request.content
        assert request.extensions['timeout']['read'] == 305.0444
        return httpx.Response(200, json=antwort)
    async with httpx.AsyncClient(transport=httpx.MockTransport(antworten)) as http:
        roh = await transcribe(daten, mime.DocumentType.FLAC, settings=dataclasses.replace(
            settings, elevenlabs_api_key='erfunden-audio',
        ), http=http)
    assert roh['response'] == antwort
    assert len(kontakte) == 1
    audio_dauerprobe.assert_not_called()
    if stelle != 'anlegen':
        datei.__exit__.assert_called_once()


@pytest.mark.db
async def test_audio_kontingentpause_verbraucht_auch_im_letzten_versuch_keinen_retry(
    db_conn: object, settings: Settings,
) -> None:
    import asyncio
    import dataclasses
    from typing import Any, cast
    import httpx
    from psycopg_pool import AsyncConnectionPool
    from mineru_ocr import db
    from mineru_ocr.audio_executor import process_audio
    from mineru_ocr.client import OCRClient
    conn = cast(Any, db_conn)
    daten = b'fLaC' + bytes(44)
    with OCRClient(db_dsn=settings.db_dsn, preflight=False) as klient:
        sha = klient.ocr_start(daten)
    kontakte: list[float] = []
    def antworten(request: httpx.Request) -> httpx.Response:
        kontakte.append(asyncio.get_running_loop().time())
        assert db.get_document(conn, str(sha)) is None
        if len(kontakte) <= 3:
            return httpx.Response(401, json={'detail': {'status': 'quota_exceeded', 'message': 'Keine Credits'}})
        return httpx.Response(200, json={'words': [{'type': 'word', 'text': 'Erfunden.', 'speaker_id': 's'}]})
    async with AsyncConnectionPool(settings.db_dsn, min_size=1, max_size=1, open=False) as pool:
        async with httpx.AsyncClient(transport=httpx.MockTransport(antworten)) as http:
            async with asyncio.timeout(5):
                await process_audio(str(sha), settings=dataclasses.replace(
                    settings, elevenlabs_api_key='erfunden', balance_recheck_seconds=0.03,
                ), pool=pool, http=http, letzter_versuch=True)
    assert len(kontakte) == 4
    assert kontakte[-1] - kontakte[0] >= 0.09
    with OCRClient(db_dsn=settings.db_dsn, preflight=False) as klient:
        assert klient.get(sha).rendered_markdown == 'Sprecher 1: Erfunden.'
    assert conn.execute('SELECT attempts FROM procrastinate_jobs').fetchone() == (0,)
