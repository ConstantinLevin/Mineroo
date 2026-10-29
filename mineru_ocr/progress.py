"""Fluechtige Beobachtung eines Executor-Versuchs; niemals Datenbankzustand.

Der Daemon besitzt die Eintraege. ContextVar trennt gleichzeitige OCR-Tasks
auch am geteilten RunPod-Client; nach jedem Versuch verschwindet der Eintrag.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
import time
from typing import Any


@dataclass
class Fortschritt:
    job_id: int
    schritt: str = "Eingabe pruefen"
    seit: float = field(default_factory=time.time)
    geplant: int | None = None
    gesendet: int = 0
    fertig: int = 0
    runpod: str | None = None


_aktuell: ContextVar[Fortschritt | None] = ContextVar("ocr_fortschritt", default=None)


def melden(schritt: str | None = None, **werte: Any) -> None:
    """Nur bekannte Beobachtungen melden, ohne I/O oder Einfluss auf den Job."""
    stand = _aktuell.get()
    if stand is None:
        return
    if schritt is not None and stand.schritt != schritt:
        stand.schritt = schritt
        stand.seit = time.time()
    for name, wert in werte.items():
        setattr(stand, name, wert)


def gesendet() -> None:
    stand = _aktuell.get()
    if stand is not None:
        stand.gesendet += 1
        melden("RunPod angenommen", runpod=None)


def fertig() -> None:
    stand = _aktuell.get()
    if stand is not None:
        stand.fertig += 1
        melden("Antwort verarbeiten", runpod=None)


class Fortschritte:
    """Ein Speicher je Daemon; enthaelt nur gerade ausgefuehrte Versuche."""

    def __init__(self) -> None:
        self._staende: dict[str, Fortschritt] = {}

    @contextmanager
    def versuch(self, sha256: str, job_id: int) -> Iterator[None]:
        stand = Fortschritt(job_id)
        self._staende[sha256] = stand
        token = _aktuell.set(stand)
        try:
            yield
        finally:
            _aktuell.reset(token)
            if self._staende.get(sha256) is stand:
                del self._staende[sha256]

    def snapshot(self) -> dict[str, Any]:
        return {sha: asdict(stand) for sha, stand in self._staende.items()}
