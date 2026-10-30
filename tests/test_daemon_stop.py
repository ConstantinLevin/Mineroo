"""Stop-Auswahl aus echten Job- und Worker-Zeilen (#77), ohne Prozesse."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from conftest import TEST_DSN
from mineru_ocr import daemons
from mineru_ocr.cli import app
from mineru_ocr.config import Settings

pytestmark = pytest.mark.db


@pytest.mark.parametrize("alter_socket", [False, True])
@pytest.mark.parametrize(("belegung", "anzahl", "erwartet"), [
    ([(100, 0, 2, 0), (200, 0, 0, 1)], 1, [200]),
    ([(100, 0, 3, 0), (200, 1, 0, 0), (300, 1, 1, 0), (400, 0, 1, 0)],
     3, [200, 400, 300]),
    ([(300, 1, 0, 0), (100, 0, 1, 0), (200, 0, 1, 0)], 5, [100, 200, 300]),
])
def test_stop_waehlt_nach_gesamter_dokumentbelegung_aus_der_datenbank(
    db_conn: Any, settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    alter_socket: bool, belegung: list[tuple[int, int, int, int]],
    anzahl: int, erwartet: list[int],
) -> None:
    """Nur Kontrollsockets und Prozessende ersetzen; Status und SQL bleiben echt.

    Wartung, wartende und beendete Jobs duerfen die Auswahl nicht erhoehen.
    Alte Sockets liefern weiterhin die richtige Belegung trotz unbekannter
    Audio-Kapazitaet. Kein Test startet oder signalisiert einen Prozess.
    """
    assert settings.db_dsn == TEST_DSN
    antworten: dict[Path, dict[str, Any]] = {}
    for nummer, (pid, ocr, audio, wartung) in enumerate(belegung):
        ids = [db_conn.execute(
            "INSERT INTO procrastinate_workers DEFAULT VALUES RETURNING id"
        ).fetchone()[0] for _ in range(3)]
        # Socket-Reihenfolge absichtlich unabhaengig von PID und Belegung.
        sock = tmp_path / f"{nummer}.sock"
        sock.touch()
        antworten[sock] = {
            "pid": pid, "available": 4, "worker_ids": ids, "ready": True,
            "stopping": False, "log_path": str(tmp_path / f"{nummer}.log"),
        }
        if not alter_socket:
            antworten[sock]["lanes"] = {"ocr": {"total": 4}, "audio": {"total": 3}}
        for lane, worker, laufend in zip(("ocr", "maintenance", "audio"), ids,
                                        (ocr, wartung, audio), strict=True):
            for _ in range(laufend):
                db_conn.execute(
                    "INSERT INTO procrastinate_jobs (queue_name, task_name, status, worker_id) "
                    "VALUES (%s, 'synthetisch', 'doing', %s)", (lane, worker),
                )
            db_conn.execute(
                "INSERT INTO procrastinate_jobs (queue_name, task_name, status, worker_id) "
                "VALUES (%s, 'synthetisch', 'succeeded', %s)", (lane, worker),
            )
            db_conn.execute(
                "INSERT INTO procrastinate_jobs (queue_name, task_name, scheduled_at) "
                "VALUES (%s, 'synthetisch', now() + interval '1 day')", (lane,),
            )
    ausgewaehlt: list[int] = []

    def fragen(pfad: Path, befehl: str = "status") -> dict[str, Any]:
        antwort = antworten[pfad]
        if befehl == "stop":
            ausgewaehlt.append(antwort["pid"])
            antwort["stopping"] = True
            antwort["ready"] = False
        return deepcopy(antwort)

    monkeypatch.setattr(daemons, "verzeichnis", lambda _: tmp_path)
    monkeypatch.setattr(daemons, "fragen", fragen)
    beendet = Mock(return_value=True)
    monkeypatch.setattr(daemons, "_prozesse_beendet", beendet)
    lauf = CliRunner().invoke(app, ["daemon", "list", "--dsn", TEST_DSN])
    assert lauf.exit_code == 0, lauf.output
    daten = json.loads(lauf.stdout)
    ocr_summe = sum(b[1] for b in belegung)
    audio_summe = sum(b[2] for b in belegung)
    assert daten["count"] == len(belegung)
    assert daten["slots"] == {"occupied": ocr_summe, "available": 4 * len(belegung)}
    assert daten["queue"] == {"waiting": len(belegung), "running": ocr_summe}
    assert daten["lanes"] == {
        "ocr": {"slots": {"occupied": ocr_summe, "total": 4 * len(belegung)},
                "queue": daten["queue"]},
        "audio": {"slots": {"occupied": audio_summe,
                            "total": None if alter_socket else 3 * len(belegung)},
                  "queue": {"waiting": len(belegung), "running": audio_summe}},
    }
    for eintrag, (pid, ocr, audio, _) in zip(daten["daemons"], belegung, strict=True):
        assert eintrag["pid"] == pid
        assert eintrag["occupied"] == ocr
        assert eintrag["available"] == 4
        assert eintrag["lanes"] == {
            "ocr": {"occupied": ocr, "total": 4},
            "audio": {"occupied": audio, "total": None if alter_socket else 3},
        }
    jobs_vorher = db_conn.execute(
        "SELECT id, status, worker_id FROM procrastinate_jobs ORDER BY id"
    ).fetchall()
    lauf = CliRunner().invoke(app, ["daemon", "stop", str(anzahl), "--dsn", TEST_DSN])
    assert lauf.exit_code == 0, lauf.output
    assert json.loads(lauf.stdout) == {
        "requested": anzahl, "stopped": len(erwartet), "running_before": len(belegung),
    }
    assert ausgewaehlt == erwartet
    beendet.assert_called_once_with(erwartet)
    # Ein freier Kandidat laesst die Audio-Anfragen anderer Daemons unberuehrt.
    assert db_conn.execute(
        "SELECT id, status, worker_id FROM procrastinate_jobs ORDER BY id"
    ).fetchall() == jobs_vorher
    assert {d["pid"] for d in daemons.status(settings)["daemons"]} == {
        b[0] for b in belegung if b[0] not in erwartet
    }
