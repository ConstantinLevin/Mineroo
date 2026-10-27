"""Kontostand und Image-Herkunft, einmal je Intervall fuer den ganzen Daemon."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from time import monotonic as _jetzt
from typing import Generic, TypeVar

from .config import Settings
from .runpod import RunPodAccess

_schlafen = asyncio.sleep
T = TypeVar("T")


class _Abfrage(Generic[T]):
    """Speichert auch Fehler und buendelt gleichzeitige Leser mit einer Sperre.

    Die Frist beginnt mit dem Ende der Abfrage, auch bei Abbruch. Es gibt keinen
    eigenen Hintergrundtask: Abfrage, Sperre und Frist bleiben beim Herunterfahren
    abbrechbar. Ein abgebrochener Leser gibt die Sperre frei, aber nicht das
    Zeitfenster fuer eine weitere Anfrage.
    """

    def __init__(self, sekunden: float) -> None:
        self._sekunden = sekunden
        self._sperre = asyncio.Lock()
        self._begonnen = float("-inf")
        self._naechste = float("-inf")
        self._wert: tuple[T] | None = None
        self._fehler: Exception | None = None

    async def warten(self) -> None:
        """Wartet bis zur naechsten erlaubten Abfrage, ohne die Frist zu verlaengern."""
        await _schlafen(max(0.0, self._naechste - _jetzt()))

    async def lesen(self, abfrage: Callable[[], Awaitable[T]], nach: float | None = None) -> T:
        """Liest einen frischen Wert; nach 402 muss auch der Abfragestart frisch sein."""
        async with self._sperre:
            if _jetzt() < self._naechste:
                if nach is None or self._begonnen >= nach:
                    if self._fehler is not None:
                        raise self._fehler.with_traceback(None)
                    if self._wert is not None:
                        return self._wert[0]
                # Ein alter Wert beantwortet keine neue 402. Auch dann gilt
                # die Frist; Invalidieren darf keine Abfragewelle ausloesen.
                await self.warten()
            self._begonnen = _jetzt()
            self._naechste = self._begonnen + self._sekunden
            self._wert = None
            self._fehler = None
            try:
                wert = await abfrage()
            except Exception as fehler:
                self._fehler = fehler
                raise
            finally:
                self._naechste = _jetzt() + self._sekunden
            self._wert = (wert,)
            return wert


class Lesefragen:
    """Ein Speicher je Daemon; Kontostand und Image haben getrennte Fristen.

    Der Zugang kommt beim Lesen dazu, damit dieses Betriebsmittel keine zweite
    HTTP-Verbindung besitzt. Clients und Preflight benutzen diesen Speicher nie.
    """

    def __init__(self, settings: Settings) -> None:
        self._konto: _Abfrage[float] = _Abfrage(settings.runpod_cache_seconds)
        self._image: _Abfrage[str | None] = _Abfrage(settings.runpod_cache_seconds)

    @staticmethod
    def jetzt() -> float:
        """Monotone Zeit fuer die Frischegrenze einer 402."""
        return _jetzt()

    async def kontostand(self, runpod: RunPodAccess, nach: float | None = None) -> float:
        """Der gelesene Kontostand oder der gespeicherte Fehler, niemals ein Ersatzwert."""
        return await self._konto.lesen(runpod.balance, nach)

    async def image_tag(self, runpod: RunPodAccess) -> str | None:
        """Die Herkunft; auch ein fehlender Tag und Lesefehler werden gespeichert."""
        return await self._image.lesen(runpod.image_tag)

    async def kontopruefung_abwarten(self) -> None:
        """Nach einem voruebergehenden Fehler am Ende des Cache-Intervalls weiter."""
        await self._konto.warten()
