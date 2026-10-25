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
