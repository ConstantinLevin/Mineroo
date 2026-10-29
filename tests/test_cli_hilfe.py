"""Jede Hilfeseite des installierten Befehls endet mit der Paketversion."""

import os
import subprocess
from pathlib import Path

import pytest

from mineru_ocr import __version__


@pytest.mark.parametrize("kommando", [
    [],
    ["setup"],
    ["daemon"],
    ["daemon", "start"],
    ["daemon", "stop"],
    ["daemon", "list"],
])
def test_hilfe_endet_mit_der_version(kommando: list[str]) -> None:
    """Auch die Haupthilfe nennt die Version erst nach allen Kommandos."""
    wurzel = Path(__file__).resolve().parent.parent
    umgebung = {**os.environ, "PYTHONPATH": str(wurzel), "NO_COLOR": "1"}
    lauf = subprocess.run(
        ["mineru-ocr", *kommando, "--help"],
        cwd=wurzel,
        env=umgebung,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert lauf.returncode == 0, lauf.stderr
    versionszeile = f"mineru-ocr {__version__}"
    assert lauf.stdout.rstrip().splitlines()[-1].strip() == versionszeile
    assert lauf.stdout.count(versionszeile) == 1
    assert "--version" not in lauf.stdout
