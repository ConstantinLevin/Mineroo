"""Debug bleibt Beobachtung: getrennte Versuche, keine dauerhafte Kopie."""
from __future__ import annotations

import asyncio
from io import StringIO
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from mineru_ocr import progress
from mineru_ocr.cli import app
from mineru_ocr.client import OCRClient
from mineru_ocr.runpod import RunPodClient
from mineru_ocr.wait_display import Anzeige, _kuerzen


async def test_gleichzeitige_versuche_bleiben_getrennt_und_verschwinden() -> None:
    staende = progress.Fortschritte()
    bereit = asyncio.Event()

    async def lauf(sha: str, job: int) -> None:
        with staende.versuch(sha, job):
            progress.melden("Test", geplant=2)
            progress.gesendet()
            await bereit.wait()
            progress.fertig()
            assert staende.snapshot()[sha]["fertig"] == 1
            if job == 1:
                raise ValueError("synthetisch")

    a = asyncio.create_task(lauf("a", 1))
    b = asyncio.create_task(lauf("b", 2))
    await asyncio.sleep(0)
    assert {s["job_id"] for s in staende.snapshot().values()} == {1, 2}
    assert all(s["gesendet"] == 1 for s in staende.snapshot().values())
    bereit.set()
    await asyncio.gather(a, b, return_exceptions=True)
    assert staende.snapshot() == {}
    progress.gesendet()  # Ausserhalb eines Versuchs wirkungslos.
    assert staende.snapshot() == {}


async def test_runpod_meldet_nur_tatsaechlich_gelesene_zustaende(settings: Any) -> None:
    antworten = iter(["IN_QUEUE", "IN_PROGRESS", "COMPLETED"])
    def antwort(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": next(antworten)})
    staende = progress.Fortschritte()
    async with httpx.AsyncClient(transport=httpx.MockTransport(antwort)) as http:
        async with RunPodClient(settings, http=http) as zugang:
            with staende.versuch("a", 1):
                for erwartet in ["IN_QUEUE", "IN_PROGRESS", "COMPLETED"]:
                    await zugang.status("fake-job")
                    assert staende.snapshot()["a"]["runpod"] == erwartet


@pytest.mark.db
@pytest.mark.parametrize("extra", [[], ["--re"], ["--ra"], ["-t"]])
def test_cache_debug_ist_nur_stderr_und_ohne_flag_bleibt_es_still(
    db_conn: Any, tmp_path: Path, extra: list[str],
) -> None:
    from test_cli import datei, speichern
    pfad = datei(tmp_path)
    speichern(db_conn, pfad, "synthetischer Volltext")
    ziel = tmp_path / "ausgabe"
    args = [str(pfad), *extra]
    if extra in (["--ra"], ["-t"]):
        args = [str(pfad), *(extra if extra == ["--ra"] else []), "-o", str(ziel)]
    vorher = CliRunner().invoke(app, args)
    inhalt = ziel.read_bytes() if ziel.exists() else None
    nachher = CliRunner().invoke(app, [*args, "-d"])
    assert vorher.exit_code == nachher.exit_code == 0
    assert vorher.stdout == nachher.stdout
    assert vorher.stderr == ""
    assert "Datenbanktreffer (done)" in nachher.stderr
    assert "Schritt" not in nachher.stderr  # Kein Wartebild fuer Treffer.
    if inhalt is not None:
        assert ziel.read_bytes() == inhalt
    assert db_conn.execute("SELECT count(*) FROM inputs").fetchone() == (0,)


@pytest.mark.db
@pytest.mark.parametrize("ordner", [False, True])
def test_debug_reiht_stapel_ein_und_zeigt_dateien_ohne_steuerzeichen(
    db_conn: Any, tmp_path: Path, ordner: bool,
) -> None:
    from test_cli import datei
    a = datei(tmp_path, "a.png")
    b = datei(tmp_path, "b.png")
    db_conn.execute("INSERT INTO procrastinate_workers DEFAULT VALUES")
    args = [str(tmp_path)] if ordner else [str(a), str(b)]
    lauf = CliRunner().invoke(app, [*args, "-d", "--timeout", "0"])
    assert lauf.exit_code == 4, lauf.output
    assert lauf.stdout == ""
    assert "a.png: wartet auf Daemon-Platz" in lauf.stderr
    assert "b.png: wartet auf Daemon-Platz" in lauf.stderr
    assert "DB: ja" in lauf.stderr
    assert "RunPod: unbekannt" in lauf.stderr
    assert "Schritt" in lauf.stderr and "gesamt" in lauf.stderr
    assert all(c.isprintable() or c == '\n' for c in lauf.stderr)
    assert db_conn.execute("SELECT count(*) FROM inputs").fetchone() == (2,)


@pytest.mark.db
@pytest.mark.parametrize("tty", [False, True])
def test_protokoll_aendert_sich_nicht_durch_uhren_und_fremde_snapshots(
    db_conn: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tty: bool,
) -> None:
    from test_cli import datei
    class Ausgabe(StringIO):
        def isatty(self) -> bool:
            return tty
    pfad = datei(tmp_path)
    db_conn.execute("INSERT INTO procrastinate_workers DEFAULT VALUES")
    ausgabe = Ausgabe()
    with OCRClient(preflight=False) as klient:
        sha = str(klient.ocr_start(pfad.read_bytes()))
        anzeige = Anzeige(klient, stream=ausgabe)
        anzeige.hinzufuegen(pfad, sha)
        anzeige.gesucht = True
        anzeige.refresh(klient.conn)
        vorher = ausgabe.getvalue()
        anzeige.refresh(klient.conn)
        if not tty:
            assert ausgabe.getvalue() == vorher
        job, worker = db_conn.execute("SELECT id, (SELECT id FROM procrastinate_workers LIMIT 1) FROM procrastinate_jobs WHERE queue_name='ocr'").fetchone()
        db_conn.execute("UPDATE procrastinate_jobs SET status='doing', worker_id=%s WHERE id=%s", (worker, job))
        def snapshot() -> dict[str, Any]:
            return {sha: {"job_id": job + 1, "worker_ids": [worker], "schritt": "FALSCH", "runpod": "FALSCH", "gesendet": 88}}
        monkeypatch.setattr(anzeige, "_fortschritte", snapshot)
        anzeige.refresh(klient.conn)
        assert "FALSCH" not in ausgabe.getvalue()
        assert "Daemon verarbeitet" in ausgabe.getvalue()
        monkeypatch.setattr(anzeige, "_fortschritte", lambda: {sha: {
            "job_id": job, "worker_ids": [worker], "schritt": "RunPod IN_QUEUE",
            "runpod": "IN_QUEUE", "gesendet": 1, "fertig": 0, "geplant": 2,
        }})
        anzeige.refresh(klient.conn)
        erwartet = "ges:1 fertig:0 Plan:2" if tty else "Chunks gesendet 1, beendet 0, Plan 2"
        assert erwartet in ausgabe.getvalue()
        assert "IN_QUEUE" in ausgabe.getvalue()


def test_terminalzeile_zaehlt_breite_unicodezeichen() -> None:
    assert _kuerzen("ab界界cd", 5) == "ab~"
    assert _kuerzen("ab界", 4) == "ab界"


async def test_kontrollsocket_liefert_nur_lebenden_fortschritt(
    settings: Any, tmp_path: Path,
) -> None:
    from types import SimpleNamespace
    from tempfile import TemporaryDirectory
    from mineru_ocr import daemon, daemons
    # macOS begrenzt die Laenge eines Unix-Socketpfads auf rund 100 Bytes.
    kurz = TemporaryDirectory(prefix="m64-", dir="/tmp")
    sock = Path(kurz.name) / "control.sock"
    staende = progress.Fortschritte()
    worker: Any = SimpleNamespace(worker_id=10)
    server = await daemon._kontrollserver_starten(
        (sock, tmp_path / "log"), settings, [worker],
        asyncio.Event(), lambda _: None, staende,
    )
    try:
        with staende.versuch("a", 7):
            progress.gesendet()
            antwort = await asyncio.to_thread(daemons.fragen, sock, "progress")
            assert antwort is not None
            assert antwort["progress"]["a"]["gesendet"] == 1
            assert antwort["progress"]["a"]["job_id"] == 7
            status = await asyncio.to_thread(daemons.fragen, sock)
            assert status is not None and "progress" not in status
        antwort = await asyncio.to_thread(daemons.fragen, sock, "progress")
        assert antwort is not None and antwort["progress"] == {}
    finally:
        server.close()
        await server.wait_closed()
        sock.unlink(missing_ok=True)
        kurz.cleanup()


@pytest.mark.db
def test_debug_beobachtet_aenderungen_waehrend_des_wartens(
    db_conn: Any, tmp_path: Path, _test_datenbank: str,
) -> None:
    import hashlib
    import time
    from concurrent.futures import ThreadPoolExecutor
    from psycopg.types.json import Jsonb
    from mineru_ocr import db
    from test_cli import datei
    pfad = datei(tmp_path)
    sha = hashlib.sha256(pfad.read_bytes()).hexdigest()
    db_conn.execute("INSERT INTO procrastinate_workers DEFAULT VALUES")

    def beantworten() -> None:
        with db.connect(_test_datenbank) as conn:
            frist = time.monotonic() + 5
            while time.monotonic() < frist:
                job = conn.execute("SELECT job_id FROM inputs WHERE sha256=%s", (sha,)).fetchone()
                if job:
                    break
                time.sleep(0.01)
            else:
                raise AssertionError("Eingabe nicht eingereiht")
            time.sleep(0.1)
            conn.execute("UPDATE procrastinate_jobs SET status='doing' WHERE id=%s", job)
            time.sleep(1.2)
            conn.execute(
                "INSERT INTO documents (sha256, status, raw, endpoint_id) VALUES (%s,'done',%s,'testendpoint')",
                (sha, Jsonb({"markdown_full": "Frisch gelesen."})),
            )
            conn.execute("NOTIFY doc_done, '" + sha + "'")
    with ThreadPoolExecutor(max_workers=1) as pool:
        arbeit = pool.submit(beantworten)
        lauf = CliRunner().invoke(app, [str(pfad), "-d", "--timeout", "5"])
        arbeit.result(timeout=10)
    assert lauf.exit_code == 0, lauf.output
    assert lauf.stdout == "Frisch gelesen.\n"
    assert "wartet auf Daemon-Platz" in lauf.stderr
    assert "Daemon verarbeitet" in lauf.stderr
    assert "final done" in lauf.stderr
    assert len([s for s in lauf.stderr.splitlines() if "wartet auf Daemon-Platz" in s]) == 1


@pytest.mark.db
def test_terminalblock_bleibt_kleiner_als_das_fenster(
    db_conn: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os
    import re
    from test_cli import datei
    class Terminal(StringIO):
        def isatty(self) -> bool:
            return True
    monkeypatch.setattr("mineru_ocr.wait_display.shutil.get_terminal_size", lambda _: os.terminal_size((80, 24)))
    ausgabe = Terminal()
    with OCRClient(preflight=False) as klient:
        anzeige = Anzeige(klient, stream=ausgabe)
        anzeige.gesucht = True
        for nr in range(25):
            pfad = datei(tmp_path, f"{nr}.png")
            anzeige.hinzufuegen(pfad, str(klient.ocr_start(pfad.read_bytes())))
        anzeige.refresh(klient.conn)
        assert ausgabe.getvalue().count('\n') <= 22
        assert "Dateien" in ausgabe.getvalue()
        ausgabe.seek(0)
        ausgabe.truncate(0)
        anzeige.refresh(klient.conn)
        assert all(int(nr) <= 22 for nr in re.findall(r'\x1b\[(\d+)A', ausgabe.getvalue()))


@pytest.mark.db
def test_terminalgroesse_folgt_stderr_und_resize(
    db_conn: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os
    import re
    from test_cli import datei
    breite = [215]
    class Terminal(StringIO):
        def isatty(self) -> bool:
            return True
        def fileno(self) -> int:
            return 99
    # stdout bleibt breit (oder ist umgeleitet); nur stderr wird schmaler.
    monkeypatch.setattr(os, "get_terminal_size", lambda fd=1: os.terminal_size((breite[0] if fd == 99 else 215, 24)))
    monkeypatch.delenv("COLUMNS", raising=False)
    monkeypatch.delenv("LINES", raising=False)
    ausgabe = Terminal()
    pfad = datei(tmp_path, "ein-langer-synthetischer-name.png")
    with OCRClient(preflight=False) as klient:
        anzeige = Anzeige(klient, stream=ausgabe)
        anzeige.gesucht = True
        anzeige.hinzufuegen(pfad, str(klient.ocr_start(pfad.read_bytes())))
        anzeige.refresh(klient.conn)
        alte_zeile = re.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', ausgabe.getvalue()).strip()
        assert len(alte_zeile) > 80
        ausgabe.seek(0)
        ausgabe.truncate(0)
        breite[0] = 80
        anzeige.refresh(klient.conn)
        zeilen = re.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', ausgabe.getvalue()).splitlines()
        assert all(len(z) <= 79 for z in zeilen)
        assert "S:" in ausgabe.getvalue() and "T:" in ausgabe.getvalue()
        # Der alte breite Frame belegt nach dem Umbruch zwei physische Zeilen.
        assert '\x1b[2A' in ausgabe.getvalue()
