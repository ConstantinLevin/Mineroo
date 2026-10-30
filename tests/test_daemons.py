"""Die Daemon-Verwaltung ueber die oeffentlichen CLI-Kommandos (#58)."""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from mineru_ocr.cli import app

pytestmark = pytest.mark.db


@pytest.fixture(autouse=True)
def _audio_startpruefung(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nur die kostenlose HTTP-Grenze ersetzen, nie echte Zugangsdaten laden."""
    monkeypatch.setenv("ELEVENLABS_API_KEY", "erfundener-audio-schluessel")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", lambda self, request:
                        httpx.Response(200, json={"user_id": "erfundener-benutzer"}))


@pytest.mark.parametrize("startfehler", [True, False])
def test_teilstart_raeumt_alle_kinder_auf(
    settings: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch, startfehler: bool,
) -> None:
    """Auch Fehler nach einem bereiten Kind terminieren und ernten jedes Kind."""
    import subprocess
    from unittest.mock import MagicMock
    from mineru_ocr import daemons

    kinder = [MagicMock(), MagicMock()]
    for kind in kinder:
        kind.poll.return_value = None
    # Das erste Kind ignoriert terminate: Auch der kill-Zweig muss es ernten.
    kinder[0].wait.side_effect = [subprocess.TimeoutExpired("daemon", 1), 0]
    popen = MagicMock(side_effect=[kinder[0], OSError("Startfehler") if startfehler else kinder[1]])
    fragen = MagicMock(side_effect=[{"ready": True}, RuntimeError("Bereitschaftsfehler")])
    monkeypatch.setattr("mineru_ocr.daemons.subprocess.Popen", popen)
    monkeypatch.setattr(daemons, "fragen", fragen)
    monkeypatch.setattr(daemons, "verzeichnis", lambda _: tmp_path)
    monkeypatch.setattr("mineru_ocr.daemon._schema_pruefen", lambda _: None)
    import dataclasses
    settings = dataclasses.replace(settings, elevenlabs_api_key="erfundener-audio-schluessel")

    with pytest.raises((OSError, RuntimeError), match="fehler"):
        daemons.start(settings, 2)

    assert fragen.call_count == (1 if startfehler else 2)
    for kind in kinder[:1 if startfehler else 2]:
        kind.terminate.assert_called_once_with()
        kind.wait.assert_any_call(timeout=settings.shutdown_graceful_timeout + 10)
    kinder[0].kill.assert_called_once_with()
    assert kinder[0].wait.call_count == 2
    assert kinder[0].wait.call_args.args == ()
    if not startfehler:
        kinder[1].kill.assert_not_called()
        kinder[1].wait.assert_called_once()


def test_stop_wartet_auf_prozess_statt_auf_socket(
    settings: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ein Socket-Timeout nach der Bestaetigung darf keinen Erfolg melden."""
    from unittest.mock import MagicMock
    from mineru_ocr import daemons

    monkeypatch.setattr(daemons, "verzeichnis", lambda _: tmp_path)
    monkeypatch.setattr(daemons, "status", lambda _: {
        "daemons": [{"pid": 123, "socket": str(tmp_path / "test.sock"), "occupied": 0}],
    })
    fragen = MagicMock(side_effect=[{"pid": 123, "stopping": True}, None])
    beendet = MagicMock(side_effect=[False, False, True])
    monkeypatch.setattr(daemons, "fragen", fragen)
    monkeypatch.setattr(daemons, "_prozesse_beendet", beendet)
    monkeypatch.setattr("mineru_ocr.daemons.time.sleep", lambda _: None)
    assert daemons.stop(settings, 1)["stopped"] == 1
    assert beendet.call_count == 3
    fragen.assert_called_once_with(tmp_path / "test.sock", "stop")


def test_stop_meldet_frist_statt_erfolg(
    settings: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import MagicMock
    from mineru_ocr import daemons
    from mineru_ocr.errors import DatabaseError

    monkeypatch.setattr(daemons, "verzeichnis", lambda _: tmp_path)
    monkeypatch.setattr(daemons, "status", lambda _: {
        "daemons": [{"pid": 123, "socket": str(tmp_path / "test.sock"), "occupied": 0}],
    })
    monkeypatch.setattr(daemons, "fragen", lambda *args: {"pid": 123, "stopping": True})
    monkeypatch.setattr(daemons, "_prozesse_beendet", lambda _: False)
    monkeypatch.setattr("mineru_ocr.daemons.time.monotonic", MagicMock(side_effect=[0, 10000]))
    with pytest.raises(DatabaseError, match="fahren noch herunter"):
        daemons.stop(settings, 1)


@pytest.mark.parametrize(("ausgabe", "code", "beendet"), [
    ("S\n", 0, False), ("Z\n", 0, True), ("Z+\nS\n", 0, False), ("", 1, True),
])
def test_prozesszustand_erkennt_beendete_prozesse(
    monkeypatch: pytest.MonkeyPatch, ausgabe: str, code: int, beendet: bool,
) -> None:
    import subprocess
    from mineru_ocr import daemons

    monkeypatch.setattr("mineru_ocr.daemons.subprocess.run", lambda *args, **kwargs:
                        subprocess.CompletedProcess([], code, stdout=ausgabe, stderr=""))
    assert daemons._prozesse_beendet([123]) is beendet


@pytest.mark.parametrize("fehler", ["exit", "stderr", "timeout"])
def test_prozessabfrage_fehler_meldet_keinen_erfolg(
    monkeypatch: pytest.MonkeyPatch, fehler: str,
) -> None:
    import subprocess
    from unittest.mock import MagicMock
    from mineru_ocr import daemons
    from mineru_ocr.errors import DatabaseError

    lauf = MagicMock(return_value=subprocess.CompletedProcess(
        [], 2 if fehler == "exit" else 1,
        stdout="", stderr="Fehler" if fehler == "stderr" else "",
    ))
    if fehler == "timeout":
        lauf.side_effect = subprocess.TimeoutExpired("ps", 2)
    monkeypatch.setattr("mineru_ocr.daemons.subprocess.run", lauf)
    with pytest.raises(DatabaseError, match="Prozesszustand"):
        daemons._prozesse_beendet([123])


def test_db_kennung_ohne_leserecht_ist_betriebsfehler(
    settings: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import MagicMock
    from psycopg.errors import InsufficientPrivilege
    from mineru_ocr import daemons
    from mineru_ocr.errors import DatabaseError

    verbindung = MagicMock()
    verbindung.__enter__.return_value.execute.side_effect = InsufficientPrivilege("verboten")
    monkeypatch.setattr("mineru_ocr.daemons.db.connect", lambda _: verbindung)
    with pytest.raises(DatabaseError, match="EXECUTE auf pg_control_system"):
        daemons.verzeichnis(settings)


def test_db_identitaet_gilt_fuer_alle_zugangswege(db_conn: Any, settings: Any) -> None:
    import dataclasses
    from mineru_ocr import daemons
    from psycopg.conninfo import make_conninfo

    pfade = {daemons.verzeichnis(dataclasses.replace(
        settings, db_dsn=make_conninfo(settings.db_dsn, host=host),
    )) for host in ("localhost", "127.0.0.1", "::1", "/tmp")}
    assert len(pfade) == 1


def test_cluster_mit_gleicher_datenbank_oid_bleiben_getrennt(
    settings: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import MagicMock
    from mineru_ocr import daemons

    verbindung = MagicMock()
    verbindung.__enter__.return_value.execute.return_value.fetchone.side_effect = [
        (7652790125226463687, 123), (7652790125226463688, 123),
    ]
    monkeypatch.setattr("mineru_ocr.daemons.db.connect", lambda _: verbindung)
    pfade = [daemons.verzeichnis(settings), daemons.verzeichnis(settings)]
    try:
        assert pfade[0] != pfade[1]
    finally:
        for pfad in pfade:
            pfad.rmdir()


def test_liste_ohne_daemon(db_conn: Any) -> None:
    lauf = CliRunner().invoke(app, ["daemon", "list"])
    assert lauf.exit_code == 0, lauf.output
    daten = json.loads(lauf.stdout)
    assert daten == {
        "count": 0, "daemons": [], "slots": {"occupied": 0, "available": 0},
        "queue": {"waiting": 0, "running": 0},
        "lanes": {
            "ocr": {"slots": {"occupied": 0, "total": 0}, "queue": {"waiting": 0, "running": 0}},
            "audio": {"slots": {"occupied": 0, "total": 0}, "queue": {"waiting": 0, "running": 0}},
        },
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
    monkeypatch.setenv("MINERU_OCR_AUDIO_CONCURRENCY", "3")
    einstellung = Settings.load(ocr_concurrency=2)
    assert einstellung.db_dsn == TEST_DSN
    assert einstellung.endpoint_id == "testendpoint"
    assert einstellung.require_api_key() == "erfundener-schluessel-nicht-echt"
    print(f"Daemon-Test: db={einstellung.db_dsn}; config={konfig}; endpoint=testendpoint; concurrency=2; key=fake (maskiert)", flush=True)
    pids: list[int] = []
    try:
        start = CliRunner().invoke(app, ["daemon", "start", "3", "--concurrency", "2", "--dsn", TEST_DSN])
        assert start.exit_code == 0, start.output
        gestartet = json.loads(start.stdout)["daemons"]
        pids = [d["pid"] for d in gestartet]
        liste = CliRunner().invoke(app, ["daemon", "list", "--dsn", TEST_DSN])
        daten = json.loads(liste.stdout)
        assert daten["count"] == 3
        assert daten["slots"] == {"occupied": 0, "available": 6}
        assert daten["lanes"]["audio"]["slots"] == {"occupied": 0, "total": 9}
        assert daten["lanes"]["ocr"]["slots"] == {"occupied": 0, "total": 6}
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
        audio_worker_id = gestartet[-1]["worker_ids"][2]
        db_conn.execute(
            "INSERT INTO procrastinate_jobs (queue_name, task_name, status, worker_id) "
            "VALUES ('audio', 'synthetisch', 'doing', %s)", (audio_worker_id,),
        )
        db_conn.execute(
            "INSERT INTO procrastinate_jobs (queue_name, task_name, scheduled_at) "
            "VALUES ('audio', 'synthetisch', now() + interval '1 day'), "
            "('audio', 'synthetisch', now() + interval '1 day'), "
            "('maintenance', 'synthetisch', now() + interval '1 day')"
        )
        daten = json.loads(CliRunner().invoke(app, ["daemon", "list", "--dsn", TEST_DSN]).stdout)
        assert daten["slots"] == {"occupied": 1, "available": 6}
        assert daten["queue"] == {"waiting": 1, "running": 1}
        assert daten["lanes"] == {
            "ocr": {"slots": {"occupied": 1, "total": 6}, "queue": {"waiting": 1, "running": 1}},
            "audio": {"slots": {"occupied": 1, "total": 9}, "queue": {"waiting": 2, "running": 1}},
        }
        belegt = next(d for d in daten["daemons"] if d["pid"] == pids[-1])
        assert belegt["lanes"] == {"ocr": {"occupied": 1, "total": 2},
                                    "audio": {"occupied": 1, "total": 3}}
        stop = CliRunner().invoke(app, ["daemon", "stop", "2", "--dsn", TEST_DSN])
        assert stop.exit_code == 0, stop.output
        daten = json.loads(CliRunner().invoke(app, ["daemon", "list", "--dsn", TEST_DSN]).stdout)
        assert daten["count"] == 1
        assert daten["daemons"][0]["pid"] == pids[-1]
        # Nur den in diesem Test gestarteten Prozess hart beenden.
        os.kill(pids[-1], signal.SIGKILL)
        frist = time.monotonic() + 5
        while daemons.status(einstellung)["count"] and time.monotonic() < frist:
            time.sleep(0.05)
        assert daemons.status(einstellung)["count"] == 0
        stop = CliRunner().invoke(app, ["daemon", "stop", "4", "--dsn", TEST_DSN])
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
