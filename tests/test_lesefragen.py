"""Geteilte Lesefragen des Daemons, mit eigener Uhr und ohne RunPod-Kontakt."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import pytest

from mineru_ocr import executor, lesefragen
from mineru_ocr.config import Settings
from mineru_ocr.errors import ConfigurationError, InsufficientBalanceError, TransientRunPodError
from mineru_ocr.runpod import RunPodAccess


class Uhr:
    """Zeit schreitet nur auf Anweisung fort; Schlafende bleiben abbrechbar."""

    def __init__(self) -> None:
        self.zeit = 100.0
        self.wechsel = asyncio.Event()

    def jetzt(self) -> float:
        return self.zeit

    async def schlafen(self, sekunden: float) -> None:
        ziel = self.zeit + sekunden
        while self.zeit < ziel:
            await self.wechsel.wait()

    def weiter(self, sekunden: float) -> None:
        self.zeit += sekunden
        self.wechsel.set()
        self.wechsel = asyncio.Event()


class ZaehlenderRunPod:
    """Eigener RunPodAccess: zaehlt Lesefragen und haelt Antworten auf Wunsch an."""

    def __init__(self) -> None:
        self.kontoabfragen = 0
        self.imageabfragen = 0
        self.konto: float | Exception = 0.8
        self.image: str | None | Exception = ':test'
        self.freigabe = asyncio.Event()
        self.freigabe.set()

    async def balance(self) -> float:
        self.kontoabfragen += 1
        await self.freigabe.wait()
        if isinstance(self.konto, Exception):
            raise self.konto
        return self.konto

    async def image_tag(self) -> str | None:
        self.imageabfragen += 1
        await self.freigabe.wait()
        if isinstance(self.image, Exception):
            raise self.image
        return self.image

    async def submit(
        self, *, file_b64: str, tier: str, ocr_mode: str, image_analysis: bool,
        transport: str, start_page: int | None = None, end_page: int | None = None,
    ) -> str:
        return 'testjob'

    async def await_result(self, job_id: str) -> dict[str, Any]:
        return {}

    async def cancel(self, job_id: str) -> dict[str, Any]:
        return {}


@pytest.fixture
def uhr(monkeypatch: pytest.MonkeyPatch) -> Uhr:
    u = Uhr()
    monkeypatch.setattr(lesefragen, '_jetzt', u.jetzt)
    monkeypatch.setattr(lesefragen, '_schlafen', u.schlafen)
    return u


async def drehen() -> None:
    """Laesst alle bereiten Tasks bis zu ihrem naechsten Warten laufen."""
    for _ in range(5):
        await asyncio.sleep(0)


async def test_48_plaetze_teilen_laufende_abfrage(uhr: Uhr, settings: Settings) -> None:
    rp = ZaehlenderRunPod()
    zugang: RunPodAccess = rp
    assert zugang is rp
    cache = lesefragen.Lesefragen(settings)
    rp.freigabe.clear()
    jobs = [asyncio.create_task(executor.wait_for_balance(rp, settings, lesefragen=cache))
            for _ in range(48)]
    await drehen()
    assert rp.kontoabfragen == 1
    assert not any(j.done() for j in jobs)
    rp.freigabe.set()
    await asyncio.wait_for(asyncio.gather(*jobs), 1)
    await executor.wait_for_balance(rp, settings, lesefragen=cache)
    assert rp.kontoabfragen == 1
    uhr.weiter(60)
    await executor.wait_for_balance(rp, settings, lesefragen=cache)
    assert rp.kontoabfragen == 2


async def test_429_alle_warten_und_einer_fragt_nach_einer_minute(
    uhr: Uhr, settings: Settings,
) -> None:
    rp = ZaehlenderRunPod()
    rp.konto = TransientRunPodError('HTTP 429')
    cache = lesefragen.Lesefragen(settings)
    jobs = [asyncio.create_task(executor.wait_for_balance(rp, settings, lesefragen=cache))
            for _ in range(48)]
    await drehen()
    assert rp.kontoabfragen == 1
    uhr.weiter(59)
    await drehen()
    assert rp.kontoabfragen == 1
    assert not any(j.done() for j in jobs)
    rp.konto = 0.8
    uhr.weiter(1)
    await asyncio.wait_for(asyncio.gather(*jobs), 1)
    assert rp.kontoabfragen == 2


async def test_402_verbraucht_keine_runde_mit_altem_stand(
    uhr: Uhr, settings: Settings,
) -> None:
    rp = ZaehlenderRunPod()
    cache = lesefragen.Lesefragen(settings)
    await executor.wait_for_balance(rp, settings, lesefragen=cache)
    uhr.weiter(30)
    versuche = 0

    async def absenden() -> str:
        nonlocal versuche
        versuche += 1
        if versuche == 1:
            raise InsufficientBalanceError('HTTP 402')
        return 'fertig'

    job = asyncio.create_task(executor._mit_kontopause(
        absenden, runpod=rp, settings=settings, schritt='Absenden', lesefragen=cache,
    ))
    await drehen()
    uhr.weiter(29)
    await drehen()
    assert versuche == 1
    assert rp.kontoabfragen == 1
    assert not job.done()
    uhr.weiter(1)
    assert await asyncio.wait_for(job, 1) == 'fertig'
    assert rp.kontoabfragen == 2
    assert versuche == 2


async def test_402_verwirft_auch_eine_vorher_begonnene_laufende_abfrage(
    uhr: Uhr, settings: Settings,
) -> None:
    rp = ZaehlenderRunPod()
    cache = lesefragen.Lesefragen(settings)
    rp.freigabe.clear()
    vorher = asyncio.create_task(executor.wait_for_balance(rp, settings, lesefragen=cache))
    await drehen()
    uhr.weiter(30)
    versuche = 0

    async def absenden() -> None:
        nonlocal versuche
        versuche += 1
        if versuche == 1:
            raise InsufficientBalanceError('HTTP 402')

    nachher = asyncio.create_task(executor._mit_kontopause(
        absenden, runpod=rp, settings=settings, schritt='Absenden', lesefragen=cache,
    ))
    await drehen()
    rp.freigabe.set()
    await vorher
    await drehen()
    assert not nachher.done()
    assert versuche == 1
    uhr.weiter(60)
    await asyncio.wait_for(nachher, 1)
    assert rp.kontoabfragen == 2


async def test_leeres_konto_fragt_trotz_kurzer_wiederholung_nur_minutlich(
    uhr: Uhr, settings: Settings, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(executor, '_kontopause_schlafen', uhr.schlafen)
    settings = replace(settings, balance_recheck_seconds=10)
    rp = ZaehlenderRunPod()
    rp.konto = 0.49
    cache = lesefragen.Lesefragen(settings)
    job = asyncio.create_task(executor.wait_for_balance(rp, settings, lesefragen=cache))
    await drehen()
    for _ in range(5):
        uhr.weiter(10)
        await drehen()
        assert not job.done()
        assert rp.kontoabfragen == 1
    rp.konto = 0.5
    uhr.weiter(10)
    await asyncio.wait_for(job, 1)
    assert rp.kontoabfragen == 2


async def test_abbruch_aller_48_waehrend_einer_abfrage(uhr: Uhr, settings: Settings) -> None:
    rp = ZaehlenderRunPod()
    rp.freigabe.clear()
    cache = lesefragen.Lesefragen(settings)
    jobs = [asyncio.create_task(executor.wait_for_balance(rp, settings, lesefragen=cache))
            for _ in range(48)]
    await drehen()
    for job in jobs:
        job.cancel()
    ergebnisse = await asyncio.wait_for(asyncio.gather(*jobs, return_exceptions=True), 1)
    assert all(isinstance(e, asyncio.CancelledError) for e in ergebnisse)
    assert rp.kontoabfragen == 1


@pytest.mark.parametrize('fehler', [ConfigurationError('HTTP 401'), ConfigurationError('HTTP 403')])
async def test_endgueltiger_fehler_wird_geteilt_und_sofort_geworfen(
    uhr: Uhr, settings: Settings, fehler: Exception,
) -> None:
    rp = ZaehlenderRunPod()
    rp.konto = fehler
    cache = lesefragen.Lesefragen(settings)
    ergebnisse = await asyncio.gather(*(
        executor.wait_for_balance(rp, settings, lesefragen=cache) for _ in range(48)
    ), return_exceptions=True)
    assert all(isinstance(e, ConfigurationError) for e in ergebnisse)
    assert rp.kontoabfragen == 1


@pytest.mark.parametrize('wert', [':test', None, TransientRunPodError('HTTP 429')])
async def test_image_werte_und_fehler_werden_geteilt(
    uhr: Uhr, settings: Settings, wert: str | None | Exception,
) -> None:
    rp = ZaehlenderRunPod()
    rp.image = wert
    rp.freigabe.clear()
    cache = lesefragen.Lesefragen(settings)
    vermerke: list[list[dict[str, Any]]] = [[] for _ in range(48)]
    jobs = [asyncio.create_task(executor._image_tag_lesen(rp, v, lesefragen=cache))
            for v in vermerke]
    await drehen()
    assert rp.imageabfragen == 1
    rp.freigabe.set()
    werte = await asyncio.wait_for(asyncio.gather(*jobs), 1)
    assert werte == [None if isinstance(wert, Exception) else wert] * 48
    assert all(bool(v) == isinstance(wert, Exception) for v in vermerke)
    uhr.weiter(59)
    await executor._image_tag_lesen(rp, [], lesefragen=cache)
    assert rp.imageabfragen == 1
    uhr.weiter(1)
    await executor._image_tag_lesen(rp, [], lesefragen=cache)
    assert rp.imageabfragen == 2


async def test_gescheiterte_erneuerung_gibt_keinen_alten_erfolg_frei(
    uhr: Uhr, settings: Settings,
) -> None:
    rp = ZaehlenderRunPod()
    cache = lesefragen.Lesefragen(settings)
    await executor.wait_for_balance(rp, settings, lesefragen=cache)
    uhr.weiter(60)
    rp.konto = TransientRunPodError('HTTP 429')
    job = asyncio.create_task(executor.wait_for_balance(rp, settings, lesefragen=cache))
    await drehen()
    assert rp.kontoabfragen == 2
    assert not job.done()
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job


async def test_abgebrochener_abfrager_gibt_das_zeitfenster_nicht_frei(
    uhr: Uhr, settings: Settings,
) -> None:
    rp = ZaehlenderRunPod()
    rp.freigabe.clear()
    cache = lesefragen.Lesefragen(settings)
    erster = asyncio.create_task(executor.wait_for_balance(rp, settings, lesefragen=cache))
    await drehen()
    zweiter = asyncio.create_task(executor.wait_for_balance(rp, settings, lesefragen=cache))
    await drehen()
    erster.cancel()
    with pytest.raises(asyncio.CancelledError):
        await erster
    rp.freigabe.set()
    await drehen()
    assert rp.kontoabfragen == 1
    assert not zweiter.done()
    uhr.weiter(60)
    await asyncio.wait_for(zweiter, 1)
    assert rp.kontoabfragen == 2
