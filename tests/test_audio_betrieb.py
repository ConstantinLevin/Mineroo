"""Audio im Betrieb (#72), nur erfundene Antworten und Testdatenbank."""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from typer.testing import CliRunner

from mineru_ocr.cli import app
from mineru_ocr.client import OCRClient
from mineru_ocr.wait_display import Anzeige
from conftest import TEST_DSN


@pytest.mark.db
def test_start_ohne_audio_schluessel_startet_keinen_prozess(
    db_conn: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = tmp_path / "runpod.toml"
    key.write_text('apikey = "erfundener-runpod-schluessel"\n')
    monkeypatch.setattr("mineru_ocr.config.RUNPOD_CONFIG_PATH", key)
    starten = MagicMock(side_effect=AssertionError("Kein Kind erlaubt"))
    monkeypatch.setattr("mineru_ocr.daemons.subprocess.Popen", starten)
    lauf = CliRunner().invoke(app, ["daemon", "start", "2", "--dsn", TEST_DSN])
    assert lauf.exit_code == 1
    assert "ELEVENLABS_API_KEY" in lauf.stderr
    starten.assert_not_called()


@pytest.mark.db
@pytest.mark.parametrize("fehler", [401, 403, 429, 503, "timeout", "transport"])
def test_start_mit_ungueltigem_audio_schluessel_startet_keinen_prozess(
    db_conn: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fehler: int | str,
) -> None:
    key = tmp_path / "runpod.toml"
    key.write_text('apikey = "erfundener-runpod-schluessel"\n')
    monkeypatch.setattr("mineru_ocr.config.RUNPOD_CONFIG_PATH", key)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "erfunden-audio-geheim")
    kontakte: list[httpx.Request] = []

    def antworten(self: Any, request: httpx.Request) -> httpx.Response:
        kontakte.append(request)
        if fehler == "timeout":
            raise httpx.ReadTimeout("erfunden-audio-geheim", request=request)
        if fehler == "transport":
            raise httpx.ConnectError("erfunden-audio-geheim", request=request)
        assert isinstance(fehler, int)
        return httpx.Response(fehler, json={"detail": "erfunden-audio-geheim"})

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", antworten)
    starten = MagicMock(side_effect=AssertionError("Kein Kind erlaubt"))
    monkeypatch.setattr("mineru_ocr.daemons.subprocess.Popen", starten)
    lauf = CliRunner().invoke(app, ["daemon", "start", "2", "--dsn", TEST_DSN])
    assert lauf.exit_code == 1
    assert "ElevenLabs" in lauf.stderr
    erwartet = ("ungueltig" if fehler in (401, 403) else "Zeitfrist" if fehler == "timeout"
                else "Transportfehler" if fehler == "transport" else f"HTTP {fehler}")
    assert erwartet in lauf.stderr
    assert "erfunden-audio-geheim" not in lauf.output
    starten.assert_not_called()
    assert len(kontakte) == (2 if fehler == "timeout" else 1)
    assert kontakte[0].method == "GET"
    assert str(kontakte[0].url) == "https://api.elevenlabs.io/v1/user"
    assert kontakte[0].headers["xi-api-key"] == "erfunden-audio-geheim"


@pytest.mark.db
@pytest.mark.parametrize("tty", [False, True])
def test_audio_anzeige_kennt_die_lane_auch_ohne_lokalen_fortschritt(
    db_conn: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tty: bool,
) -> None:
    from io import StringIO
    import os

    class Ausgabe(StringIO):
        def isatty(self) -> bool:
            return tty

    monkeypatch.setattr("mineru_ocr.wait_display.shutil.get_terminal_size",
                        lambda _: os.terminal_size((100, 24)))
    # Irrefuehrender Name: Die Queue bestimmt die Lane, niemals die Endung.
    pfad = tmp_path / "synthetisch.pdf"
    pfad.write_bytes(b"fLaC" + bytes(50))
    ausgabe = Ausgabe()
    with OCRClient(preflight=False) as klient:
        sha = str(klient.ocr_start(pfad.read_bytes()))
        anzeige = Anzeige(klient, stream=ausgabe)
        anzeige.hinzufuegen(pfad, sha)
        anzeige.gesucht = True
        anzeige.refresh()
        eingereiht = ausgabe.getvalue()
        assert "Daemon-Platz" in eingereiht
        assert "RunPod" not in eingereiht and "RP:" not in eingereiht
        job, worker = db_conn.execute(
            "SELECT id, (SELECT id FROM procrastinate_workers LIMIT 1) "
            "FROM procrastinate_jobs WHERE queue_name='audio'"
        ).fetchone()
        db_conn.execute("UPDATE procrastinate_jobs SET status='doing' WHERE id=%s", (job,))
        anzeige.refresh()
        assert "Daemon verarbeitet" in ausgabe.getvalue()
        assert "Chunks" not in ausgabe.getvalue() and "Plan" not in ausgabe.getvalue()


@pytest.mark.db
@pytest.mark.parametrize("debug", [True, False])
async def test_audio_cli_zeigt_elevenlabs_versuch_und_abschluss(
    db_conn: Any, settings: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, debug: bool,
) -> None:
    import asyncio
    import dataclasses
    import uuid
    from mineru_ocr import daemon, daemons

    konfig = tmp_path / "config.toml"
    konfig.write_text(f'db_dsn = "{TEST_DSN}"\nendpoint_id = "testendpoint"\n')
    monkeypatch.setenv("MINERU_OCR_CONFIG", str(konfig))
    pfad = tmp_path / "synthetisch.pdf"
    pfad.write_bytes(b"fLaC" + bytes(51))
    kontakte: list[str] = []

    async def antworten(self: Any, request: httpx.Request) -> httpx.Response:
        # Kein HTTP-Aufruf kann das Netz erreichen, insbesondere kein RunPod.
        assert str(request.url) == "https://api.elevenlabs.io/v1/speech-to-text"
        kontakte.append(request.method)
        await asyncio.sleep(1.3)
        return httpx.Response(200, json={"words": [
            {"type": "word", "text": "Erfunden.", "speaker_id": "speaker_0"},
        ]})

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", antworten)
    einstellung = dataclasses.replace(settings, elevenlabs_api_key="erfunden-audio")
    sock = daemons.verzeichnis(einstellung) / ("test72-" + uuid.uuid4().hex[:12] + ".sock")
    laufend = asyncio.create_task(daemon._run(
        einstellung, control=(sock, tmp_path / "daemon.log"),
    ))
    try:
        for _ in range(200):
            bereit = await asyncio.to_thread(daemons.fragen, sock)
            if bereit and bereit["ready"]:
                break
            await asyncio.sleep(0.02)
        else:
            raise AssertionError("Testdaemon nicht bereit")
        lauf = await asyncio.to_thread(CliRunner().invoke, app, [
            str(pfad), "--timeout", "5", *(["-d"] if debug else []),
        ])
        assert lauf.exit_code == 0, lauf.output
        assert lauf.stdout == "Sprecher 1: Erfunden.\n"
        if debug:
            assert "ElevenLabs verarbeitet" in lauf.stderr
            assert "Schritt" in lauf.stderr and "gesamt" in lauf.stderr
            assert "final done" in lauf.stderr
            assert all(wort not in lauf.stderr for wort in ("RunPod", "Chunk", "Plan", "RP:"))
        else:
            assert lauf.stderr == ""
        # Derselbe Pfad ist ein Treffer, ohne einen weiteren HTTP-Aufruf.
        treffer = await asyncio.to_thread(CliRunner().invoke, app, [str(pfad), "-d"])
        assert treffer.stdout == lauf.stdout
        assert "Datenbanktreffer (done)" in treffer.stderr
        assert kontakte == ["POST"]
        assert "erfunden-audio" not in lauf.output
        assert bereit["lanes"]["audio"]["total"] == 2
    finally:
        laufend.cancel()
        try:
            await laufend
        except asyncio.CancelledError:
            pass
    assert await asyncio.to_thread(daemons.fragen, sock) is None


@pytest.mark.db
async def test_alter_daemon_meldet_audio_belegung_ohne_geratene_kapazitaet(
    db_conn: Any, settings: Any,
) -> None:
    import asyncio
    import json
    import uuid
    from mineru_ocr import daemons

    ids = [db_conn.execute("INSERT INTO procrastinate_workers DEFAULT VALUES RETURNING id").fetchone()[0]
           for _ in range(3)]
    db_conn.execute(
        "INSERT INTO procrastinate_jobs (queue_name, task_name, status, worker_id) "
        "VALUES ('audio', 'synthetisch', 'doing', %s)", (ids[2],),
    )
    sock = daemons.verzeichnis(settings) / ("alt72-" + uuid.uuid4().hex[:12] + ".sock")

    async def antworten(leser: asyncio.StreamReader, schreiber: asyncio.StreamWriter) -> None:
        await leser.readline()
        schreiber.write((json.dumps({
            "pid": 72, "available": 3, "worker_ids": ids, "ready": True,
            "stopping": False, "log_path": "synthetisch.log",
        }) + "\n").encode())
        await schreiber.drain()
        schreiber.close()
        await schreiber.wait_closed()

    server = await asyncio.start_unix_server(antworten, path=sock)
    try:
        lauf = await asyncio.to_thread(CliRunner().invoke, app, ["daemon", "list", "--dsn", TEST_DSN])
        assert lauf.exit_code == 0, lauf.output
        daten = json.loads(lauf.stdout)
        assert daten["count"] == 1
        assert daten["lanes"]["ocr"]["slots"] == {"occupied": 0, "total": 3}
        assert daten["lanes"]["audio"] == {
            "slots": {"occupied": 1, "total": None}, "queue": {"waiting": 0, "running": 1},
        }
        assert daten["daemons"][0]["lanes"]["audio"] == {"occupied": 1, "total": None}
    finally:
        server.close()
        await server.wait_closed()
        sock.unlink(missing_ok=True)
