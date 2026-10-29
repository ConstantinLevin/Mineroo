"""Paketmetadaten und Bibliothek nennen dieselbe Version."""

import tomllib
from pathlib import Path

from mineru_ocr import __version__


def test_paketversion_ist_1_0_und_stimmt_mit_der_bibliothek_ueberein() -> None:
    """Eine Versionsaenderung darf die beiden Angaben nicht auseinanderziehen."""
    with (Path(__file__).resolve().parent.parent / "pyproject.toml").open("rb") as datei:
        projekt = tomllib.load(datei)["project"]
    assert projekt["version"] == __version__
    assert __version__ == "1.0"
