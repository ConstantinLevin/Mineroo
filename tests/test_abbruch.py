"""Tests fuer `mineru_ocr.abbruch`: Warten, das ein Abbruch nicht unterbricht."""

from __future__ import annotations

import asyncio

import pytest

from mineru_ocr.abbruch import abwarten_bis_zum_zweiten_abbruch


async def test_ein_abbruch_unterbricht_die_arbeit_nicht() -> None:
    """Der erste Abbruch des eigenen Tasks erreicht die Arbeit nicht: Sie laeuft
    zu Ende, und ihr Ergebnis kommt zurueck. Ob der Abbruch danach nachgeholt
    wird, entscheidet der Aufrufer."""
    begonnen = asyncio.Event()
    weiter = asyncio.Event()

    async def arbeit() -> str:
        begonnen.set()
        await weiter.wait()
        return "geschrieben"

    lauf = asyncio.ensure_future(abwarten_bis_zum_zweiten_abbruch(arbeit()))
    await begonnen.wait()
    lauf.cancel()
    await asyncio.sleep(0)
    weiter.set()
    assert await lauf == "geschrieben"


async def test_der_zweite_abbruch_bricht_die_arbeit_ab() -> None:
    """Ein zweiter Abbruch gewinnt: Die Arbeit wird abgebrochen, und der
    Abbruch kommt heraus. Sonst hielte eine haengende Arbeit den Prozess auch
    gegen ein zweites Signal fest."""
    begonnen = asyncio.Event()
    abgebrochen = asyncio.Event()

    async def arbeit() -> None:
        begonnen.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            abgebrochen.set()
            raise

    lauf = asyncio.ensure_future(abwarten_bis_zum_zweiten_abbruch(arbeit()))
    await begonnen.wait()
    lauf.cancel()
    await asyncio.sleep(0)
    lauf.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(lauf, timeout=5)
    assert abgebrochen.is_set()


async def test_ein_frueherer_abbruch_zaehlt_mit() -> None:
    """War der eigene Task schon abgebrochen, als das Warten begann (der Abbruch
    fuehrte in den Fehlerzweig), ist schon der naechste Abbruch der zweite."""
    begonnen = asyncio.Event()

    async def arbeit() -> None:
        begonnen.set()
        await asyncio.Event().wait()

    async def nach_einem_abbruch() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await abwarten_bis_zum_zweiten_abbruch(arbeit())
            raise

    lauf = asyncio.ensure_future(nach_einem_abbruch())
    await asyncio.sleep(0)
    lauf.cancel()
    await begonnen.wait()
    lauf.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(lauf, timeout=5)


async def test_eine_fertige_arbeit_gilt_auch_wenn_der_abbruch_zugleich_kommt() -> None:
    """Wird die Arbeit im selben Augenblick fertig, in dem der erste Abbruch
    ankommt, kommt ihr Ergebnis zurueck und nicht der Abbruch.

    Fertig heisst hier nicht abgebrochen: Kaeme der Abbruch heraus, warf der
    Executor ihn statt des endgueltigen Fehlers, dessen `failed` eben
    geschrieben wurde, und Procrastinate reihte das Dokument neu ein.
    """
    begonnen = asyncio.Event()
    weiter = asyncio.Event()

    async def arbeit() -> str:
        begonnen.set()
        await weiter.wait()
        return "geschrieben"

    lauf = asyncio.ensure_future(abwarten_bis_zum_zweiten_abbruch(arbeit()))
    await begonnen.wait()
    # Ohne Pause dazwischen: Die Arbeit wird im naechsten Schritt fertig, und
    # der Abbruch erreicht den Wartenden, bevor er das sieht.
    weiter.set()
    lauf.cancel()
    assert await lauf == "geschrieben"


async def test_zwei_abbrueche_vor_dem_warten_lassen_die_arbeit_nicht_anlaufen() -> None:
    """Kamen schon zwei Abbrueche, bevor das Warten begann (der zweite etwa
    waehrend `/cancel` an RunPod hinausging), laeuft die Arbeit gar nicht erst
    an, und der Abbruch kommt sofort heraus. Sonst hielte eine haengende
    Datenbank den Daemon bis zu einem dritten Signal fest."""
    begonnen = asyncio.Event()

    async def arbeit() -> None:
        begonnen.set()
        await asyncio.Event().wait()

    async def nach_zwei_abbruechen() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await abwarten_bis_zum_zweiten_abbruch(arbeit())
                raise

    lauf = asyncio.ensure_future(nach_zwei_abbruechen())
    await asyncio.sleep(0)
    lauf.cancel()
    await asyncio.sleep(0)
    lauf.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(lauf, timeout=5)
    await asyncio.sleep(0)
    assert not begonnen.is_set()


async def test_nach_dem_zweiten_abbruch_wird_aufs_aufraeumen_nur_befristet_gewartet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Haelt die Arbeit auch ihren eigenen Abbruch hin (psycopg wartet danach
    auf die Antwort des Servers, und ein unerreichbarer Server antwortet nie),
    kommt der zweite Abbruch nach `AUFRAEUMFRIST_SEKUNDEN` trotzdem heraus.
    Ohne Frist hielte das den Daemon trotz des zweiten Signals fest."""
    from mineru_ocr import abbruch

    monkeypatch.setattr(abbruch, "AUFRAEUMFRIST_SEKUNDEN", 0.2)
    begonnen = asyncio.Event()
    freigabe = asyncio.Event()

    innere: list[asyncio.Task[None]] = []

    async def arbeit() -> None:
        aufgabe = asyncio.current_task()
        assert aufgabe is not None
        innere.append(aufgabe)
        begonnen.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await freigabe.wait()  # raeumt erst auf, wenn der Server antwortet
            raise

    lauf = asyncio.ensure_future(abwarten_bis_zum_zweiten_abbruch(arbeit()))
    await begonnen.wait()
    lauf.cancel()
    await asyncio.sleep(0)
    lauf.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(lauf), timeout=2)
        # Herausgekommen, obwohl die Arbeit noch aufraeumt.
        assert not innere[0].done()
    finally:
        freigabe.set()
        await asyncio.wait([lauf, *innere])
