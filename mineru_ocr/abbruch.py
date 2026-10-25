"""Warten, das ein Abbruch nicht unterbricht.

Eine Stelle fuer das eine Muster, das Daemon und Executor beide brauchen: Arbeit,
die beim Herunterfahren fertig werden MUSS, obwohl der eigene Task schon
abgebrochen ist (Worker einsammeln, Betriebsmittel schliessen, ein Ergebnis
schreiben).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import TypeVar

T = TypeVar("T")
"""Der Rueckgabetyp der Arbeit, die `unabbrechbar_abwarten` zu Ende bringt. Sie
steht ueber dem Einsammeln der Worker (liefert deren Ergebnisliste) und ueber dem
Schliessen der Betriebsmittel (liefert nichts) — daher generisch."""


async def unabbrechbar_abwarten(arbeit: Awaitable[T]) -> T:
    """Wartet, bis `arbeit` WIRKLICH fertig ist — auch bei wiederholtem Abbruch.

    Wozu es diese Funktion gibt: Beim Herunterfahren gibt es zwei Schritte, die
    fertig werden MUESSEN, bevor `run` zurueckkehrt — die Worker einsammeln und
    die Betriebsmittel schliessen, in dieser Reihenfolge. Beides steht in einem
    `finally`, und beides wird also genau in dem Moment ausgefuehrt, in dem der
    eigene Task schon abgebrochen ist.

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
