"""Die Daemon-Verwaltung ueber die oeffentlichen CLI-Kommandos (#58)."""
from __future__ import annotations

import json
from typing import Any

import pytest
from typer.testing import CliRunner

from mineru_ocr.cli import app

pytestmark = pytest.mark.db


def test_liste_ohne_daemon(db_conn: Any) -> None:
    lauf = CliRunner().invoke(app, ["daemon", "list"])
    assert lauf.exit_code == 0, lauf.output
    daten = json.loads(lauf.stdout)
    assert daten == {
        "count": 0, "daemons": [], "slots": {"occupied": 0, "available": 0},
        "queue": {"waiting": 0, "running": 0},
    }
    assert '\n  "count"' in lauf.stdout


@pytest.mark.parametrize("kommando", ["start", "stop"])
@pytest.mark.parametrize("anzahl", ["0", "-1", "1.5", "falsch"])
def test_anzahl_muss_positive_ganze_zahl_sein(kommando: str, anzahl: str) -> None:
    lauf = CliRunner().invoke(app, ["daemon", kommando, anzahl])
    assert lauf.exit_code == 2


def test_drei_hintergrundprozesse_stoppen_und_absturz(
    db_conn: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os
    import signal
    import time
    from pathlib import Path
    from mineru_ocr.config import Settings
    from mineru_ocr import daemons
    from conftest import TEST_DSN

    konfig = tmp_path / "config.toml"
    konfig.write_text('db_dsn = "' + TEST_DSN + '"\nendpoint_id = "testendpoint"\n')
    key = tmp_path / "runpod.toml"
    key.write_text('apikey = "erfundener-schluessel-nicht-echt"\n')
    monkeypatch.setenv("MINERU_OCR_CONFIG", str(konfig))
    monkeypatch.setattr("mineru_ocr.config.RUNPOD_CONFIG_PATH", key)
    einstellung = Settings.load(ocr_concurrency=2)
    assert einstellung.db_dsn == TEST_DSN
    assert einstellung.endpoint_id == "testendpoint"
    assert einstellung.require_api_key() == "erfundener-schluessel-nicht-echt"
    print(f"Daemon-Test: db={einstellung.db_dsn}; config={konfig}; endpoint=testendpoint; concurrency=2; key=fake (maskiert)", flush=True)
    pids: list[int] = []
    try:
        start = CliRunner().invoke(app, ["daemon", "start", "3", "--concurrency", "2"])
        assert start.exit_code == 0, start.output
        gestartet = json.loads(start.stdout)["daemons"]
        pids = [d["pid"] for d in gestartet]
        liste = CliRunner().invoke(app, ["daemon", "list"])
        daten = json.loads(liste.stdout)
        assert daten["count"] == 3
        assert daten["slots"] == {"occupied": 0, "available": 6}
        assert len({d["log_path"] for d in daten["daemons"]}) == 3
        assert all(Path(d["log_path"]).is_file() for d in daten["daemons"])
        # Echte Procrastinate-Zeilen, synthetisch und ohne ausfuehrbaren OCR-Aufruf.
        # Ein laufender Job am letzten Daemon: stop muss die beiden leeren waehlen.
        worker_id = gestartet[-1]["worker_ids"][0]
        db_conn.execute(
            "INSERT INTO procrastinate_jobs (queue_name, task_name, status, worker_id) "
            "VALUES ('ocr', 'synthetisch', 'doing', %s)", (worker_id,),
        )
        db_conn.execute(
            "INSERT INTO procrastinate_jobs (queue_name, task_name, scheduled_at) "
            "VALUES ('ocr', 'synthetisch', now() + interval '1 day')"
        )
        daten = json.loads(CliRunner().invoke(app, ["daemon", "list"]).stdout)
        assert daten["slots"] == {"occupied": 1, "available": 6}
        assert daten["queue"] == {"waiting": 1, "running": 1}
        stop = CliRunner().invoke(app, ["daemon", "stop", "2"])
        assert stop.exit_code == 0, stop.output
        daten = json.loads(CliRunner().invoke(app, ["daemon", "list"]).stdout)
        assert daten["count"] == 1
        assert daten["daemons"][0]["pid"] == pids[-1]
        # Nur den in diesem Test gestarteten Prozess hart beenden.
        os.kill(pids[-1], signal.SIGKILL)
        frist = time.monotonic() + 5
        while daemons.status(einstellung)["count"] and time.monotonic() < frist:
            time.sleep(0.05)
        assert daemons.status(einstellung)["count"] == 0
        stop = CliRunner().invoke(app, ["daemon", "stop", "4"])
        assert stop.exit_code == 0, stop.output
        assert json.loads(stop.stdout) == {"requested": 4, "stopped": 0, "running_before": 0}
    finally:
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        for _ in range(100):
            if not daemons.status(einstellung)["count"]:
                break
            time.sleep(0.05)
        # Auch der harte Absturz wird geerntet, ohne fremde Prozesse anzufassen.
        for pid in pids:
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass
        assert daemons.status(einstellung)["count"] == 0
        db_conn.execute("TRUNCATE procrastinate_jobs, procrastinate_workers RESTART IDENTITY CASCADE")
        assert db_conn.execute("SELECT count(*) FROM procrastinate_workers").fetchone()[0] == 0
