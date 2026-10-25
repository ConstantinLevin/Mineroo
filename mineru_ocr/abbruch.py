"""Warten, das ein Abbruch nicht unterbricht.

Zwei Muster fuer Arbeit, die beim Herunterfahren fertig werden soll, obwohl der
eigene Task schon abgebrochen ist:

- `unabbrechbar_abwarten` haelt JEDEN Abbruch fern und holt ihn danach nach.
  Fuer `daemon.run`: Worker einsammeln, Betriebsmittel schliessen.
- `abwarten_bis_zum_zweiten_abbruch` haelt einen Abbruch fern, den zweiten
  nicht, und holt nichts nach. Fuer das Schreiben eines Ergebnisses im
  Fehlerzweig des Executors.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import TypeVar

T = TypeVar("T")
"""Der Rueckgabetyp der Arbeit, die `unabbrechbar_abwarten` zu Ende bringt. Sie
steht ueber dem Einsammeln der Worker (liefert deren Ergebnisliste) und ueber dem
Schliessen der Betriebsmittel (liefert nichts) — daher generisch."""

AUFRAEUMFRIST_SEKUNDEN = 5.0
"""Wie lange `abwarten_bis_zum_zweiten_abbruch` nach dem zweiten Abbruch darauf
wartet, dass die abgebrochene Arbeit aufgeraeumt hat. Ohne Frist hielte ein
unerreichbarer Datenbankserver den Daemon trotz des zweiten Signals fest:
psycopg wartet nach einem Abbruch auf seine Antwort."""


async def unabbrechbar_abwarten(arbeit: Awaitable[T]) -> T:
    """Wartet, bis `arbeit` WIRKLICH fertig ist — auch bei wiederholtem Abbruch.

    Wozu es diese Funktion gibt: Beim Herunterfahren gibt es zwei Schritte, die
    fertig werden MUESSEN, bevor `daemon.run` zurueckkehrt — die Worker
    einsammeln und die Betriebsmittel schliessen, in dieser Reihenfolge. Beides
    steht in einem `finally`, und beides wird also genau in dem Moment
    ausgefuehrt, in dem der eigene Task schon abgebrochen ist.

    Was dabei wirklich passiert, ist am 08.08.2026 mit drei kleinen Programmen
    gemessen worden, weil hier vorher eine falsche Annahme im Kommentar stand:

        Nach EINEM `Task.cancel()` laufen `await`s in einem `finally` ganz
        normal durch — die Behauptung, ein abgebrochener Task bekaeme „bei jedem
        weiteren await sofort wieder CancelledError", stimmt so nicht.

        Nach einem ZWEITEN `cancel()` waehrend des Aufraeumens stimmt sie
        dagegen: Der Aufraeum-`await` wird unterbrochen.

        Ein blosses `asyncio.shield` loest das NICHT, sondern verschlimmert es:
        Es schuetzt zwar die innere Aufgabe, laesst den Wartenden aber sofort
        mit `CancelledError` zurueckkehren — `run` war damit fertig, WAEHREND
        Pool und HTTP-Zugang noch schlossen und die Worker noch liefen. Genau
        das war der gemessene Fehler.

    Deshalb der Schild IN EINER SCHLEIFE: Der Schild haelt den Abbruch von der
    Arbeit fern, und die Schleife wartet nach jedem Abbruch weiter, bis die
    Arbeit wirklich fertig ist. Erst danach wird der Abbruch nachgeholt.

    Args:
        arbeit: Was zu Ende laufen muss. Wird zu einer Aufgabe gemacht, laeuft
            also auch dann weiter, wenn der Wartende unterbrochen wird.

    Returns:
        Das Ergebnis von `arbeit` — aber nur, wenn der eigene Task waehrenddessen
        NICHT abgebrochen wurde.

    Zusicherung:
        Kehrt nie zurueck und wirft nie, solange `arbeit` noch laeuft. Wurde der
        eigene Task unterwegs abgebrochen, wird `arbeit` trotzdem fertig, und der
        Abbruch wird DANACH nachgeholt — die aufrufende Ebene endet also nach wie
        vor abgebrochen, nur eben zum richtigen Zeitpunkt.

    Raises:
        asyncio.CancelledError: Wenn der eigene Task abgebrochen wurde
            (nachgeholt, nachdem `arbeit` fertig ist) oder wenn `arbeit` selbst
            abgebrochen wurde.

    Kosten: keine.
    """
    aufgabe = asyncio.ensure_future(arbeit)
    unterbrochen = False
    while True:
        try:
            ergebnis = await asyncio.shield(aufgabe)
        except asyncio.CancelledError:
            if aufgabe.done():
                # Die ARBEIT selbst wurde abgebrochen, nicht nur der Wartende.
                # Dann gibt es nichts mehr abzuwarten.
                raise
            unterbrochen = True
            continue
        if unterbrochen:
            # Die Arbeit ist fertig. Jetzt — und keinen Moment frueher — wird der
            # Abbruch des eigenen Tasks nachgeholt.
            raise asyncio.CancelledError
        return ergebnis


async def abwarten_bis_zum_zweiten_abbruch(arbeit: Awaitable[T]) -> T:
    """Wartet `arbeit` ab; einen Abbruch haelt es von ihr fern, den zweiten nicht.

    Fuer das Schreiben eines Ergebnisses im Fehlerzweig des Executors
    (Mineroo#27): Bricht das Herunterfahren den Job genau waehrend des
    Schreibens ab, soll das Schreiben fertig werden, sonst bliebe das Dokument
    ohne Ergebnis. Ein ZWEITER Abbruch dagegen ist das zweite Signal an den
    Daemon, und das bricht sofort ab (`daemon.run`). Haelte das Schreiben auch
    ihn fern, hielte eine haengende Datenbank den Daemon fest, bis nur noch
    `kill -9` hilft.

    Gezaehlt werden die Abbrueche des eigenen Tasks (`Task.cancelling()`), auch
    die, die schon vor dem Warten kamen: Fuehrte ein Abbruch in den Fehlerzweig,
    ist schon der naechste der zweite, und kamen schon zwei, laeuft die Arbeit
    gar nicht erst an.

    Anders als `unabbrechbar_abwarten` holt diese Funktion einen abgehaltenen
    Abbruch NICHT nach: Sie gibt das Ergebnis zurueck, und der Aufrufer
    entscheidet, was danach geworfen wird.

    Args:
        arbeit: Was zu Ende laufen soll.

    Returns:
        Das Ergebnis von `arbeit`, auch wenn ein Abbruch abgehalten wurde.

    Raises:
        asyncio.CancelledError: Beim zweiten Abbruch (die Arbeit wird dann
            abgebrochen; auf ihr Aufraeumen wird hoechstens
            `AUFRAEUMFRIST_SEKUNDEN` gewartet) oder wenn `arbeit` selbst
            abgebrochen wurde. Was `arbeit` selbst wirft, kommt heraus.

    Kosten: keine.
    """
    aufgabe = asyncio.ensure_future(arbeit)
    eigener = asyncio.current_task()

    def schon_zweimal() -> bool:
        return eigener is not None and eigener.cancelling() > 1

    if schon_zweimal():
        # Die Arbeit laeuft gar nicht erst an: Eine abgebrochene Aufgabe, die
        # noch keinen Schritt getan hat, fuehrt ihren Rumpf nie aus.
        aufgabe.cancel()
        raise asyncio.CancelledError
    while True:
        try:
            return await asyncio.shield(aufgabe)
        except asyncio.CancelledError:
            if aufgabe.done():
                # Fertig heisst nicht abgebrochen: Wurde die Arbeit im selben
                # Augenblick fertig, in dem der Abbruch kam, gilt ihr Ergebnis
                # (oder ihr Fehler). `result()` wirft den Abbruch selbst nur,
                # wenn die ARBEIT abgebrochen wurde.
                return aufgabe.result()
            if schon_zweimal():
                aufgabe.cancel()
                # Mit Frist: psycopg wartet nach einem Abbruch auf die Antwort
                # des Servers, und ein unerreichbarer Server antwortet nie.
                await asyncio.wait([aufgabe], timeout=AUFRAEUMFRIST_SEKUNDEN)
                raise
