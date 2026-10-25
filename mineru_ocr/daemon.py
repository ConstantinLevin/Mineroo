"""Der Exekutor als eigener Prozess: zwei Worker in einem, sauberes Herunterfahren.

Dies ist der einzige Prozess, der mit RunPod spricht, und der einzige mit
Nebenlaeufigkeit. Alles andere — Fassade und Kommandozeile — legt Zeilen an
und liest. Damit ist jede Komponente einzeln startbar und tot-startbar, und der
Zustand liegt zu jedem Zeitpunkt vollstaendig an einem Ort, den man mit
gewoehnlichem SQL ansehen kann.

Zwei Worker in EINEM Prozess, und das ist die eine Stelle dieses Entwurfs, die
auf unbelegtem Grund steht. Gebraucht werden sie, weil periodische Tasks
gewoehnliche Jobs sind und den Slot-Vorrat ihres Workers teilen: Haengen alle
vierundzwanzig OCR-Plaetze in Warteschleifen wegen leeren Kontos, kaeme ein
Waechter auf derselben Queue nie an die Reihe. Die Procrastinate-Doku empfiehlt
fuer genau dieses Problem die Trennung nach Queues (Feature-Verifikation Punkt 2)
— sie sagt aber nirgends, ob zwei Worker-Koroutinen in einem Prozess erlaubt
sind. Dokumentiert ist ausschliesslich „mehrere Worker = mehrere Prozesse"
(Feature-Verifikation Punkt 1, NICHT BELEGT).

Deshalb steht dieser Vermerk hier und nicht im Kleingedruckten: Die Konstruktion
wird im Integrationstest ausdruecklich geprueft (Plan 10.2 h). Traegt sie nicht,
ist der Rueckfallweg der dokumentierte — ein zweiter Prozess, der nur die
Wartungs-Queue bedient. Der Rueckfall ist vorbereitet und nicht bloss gedacht:
`run` nimmt dafuer den Parameter `queues`.

Kein Autostart im ersten Wurf. Der Daemon wird von Hand gestartet, damit man ihm
im echten Bestandslauf zusieht, bevor er ein Hintergrundprozess wird, den man
vergisst (Plan 5.3).
"""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Awaitable, Sequence
from typing import Any, TypeVar

from . import config, tasks
from .config import Settings

T = TypeVar("T")
"""Der Rueckgabetyp der Arbeit, die `_unabbrechbar_abwarten` zu Ende bringt. Sie
steht ueber dem Einsammeln der Worker (liefert deren Ergebnisliste) und ueber dem
Schliessen der Betriebsmittel (liefert nichts) — daher generisch."""

log = logging.getLogger("mineru_ocr.daemon")


async def run(
    settings: Settings,
    *,
    queues: Sequence[str] | None = None,
) -> None:
    """Startet die Worker und laeuft, bis ein Beendigungssignal kommt.

    Im Normalfall startet die Funktion ZWEI Worker-Koroutinen im selben Prozess:
    einen fuer die OCR-Queue mit `settings.ocr_concurrency` Plaetzen, einen fuer
    die Wartungs-Queue mit einem Platz.

    Beide laufen mit `install_signal_handlers=False`, und das ist keine Feinheit:
    Zwei Worker duerfen nicht beide Signal-Handler einrichten — der zweite
    ueberschriebe den ersten, und beim Beenden wuerde einer der beiden nie
    benachrichtigt. Stattdessen faengt dieser Prozess SIGTERM und SIGINT selbst
    und bricht beide Koroutinen ab. Das ist genau das dokumentierte Muster
    „Worker in einer groesseren Anwendung" (Feature-Verifikation Punkt 1).

    Beide laufen ausserdem mit `delete_jobs="never"`. Job-Zeilen bleiben stehen,
    weil der abgeleitete Dokument-Zustand aus ihnen GELESEN wird — wer sie
    aufraeumt, raeumt die Beobachtbarkeit weg. Das Aufraeumen alter Jobs ist ein
    dokumentiertes Spaeter (`remove_old_jobs`) und keine Voreinstellung.

    Args:
        settings: Vollstaendige Betriebseinstellung.
        queues: Welche Queues bedient werden. None heisst beide, jede mit ihrem
            eigenen Worker — der Normalfall. Eine ausdrueckliche Auswahl startet
            genau einen Worker fuer genau diese Queues und ist der Weg fuer den
            Rueckfall auf zwei Prozesse.

    Returns:
        Nichts. Kehrt zurueck, wenn beide Worker beendet sind.

    Herunterfahren:
        Auf SIGTERM oder SIGINT werden beide Worker abgebrochen. Laufende
        Dokumente werden bis zu `settings.shutdown_graceful_timeout` zu Ende
        betreut; was dann noch laeuft, wird abgebrochen. Ein abgebrochener Job
        endet `aborted` — endgueltig und OHNE automatische Wiederholung. Fuer
        `aborted` gibt es kein Retry-Bordmittel (Feature-Verifikation Punkt 19);
        das betroffene Dokument bleibt ohne Ergebnis und offen (die Eingabe
        steht), der abgeleitete Zustand zeigt „endgueltig beendet ohne
        Ergebnis"; `ocr_start` findet die stehende Eingabe und tut nichts, den
        Rueckweg bringt das Ticket #26. Kein Waechter noetig.

        Ein zweites Signal waehrend des Herunterfahrens bricht sofort ab. Wer
        zweimal Strg-C drueckt, meint es so.

    Die App:
        Der Daemon-Prozess haelt genau EINE App (`tasks.build_daemon_app`), mit
        den angemeldeten Tasks und der Wiederholungs-Strategie, und oeffnet sie
        mit seinem eigenen Pool. Eine weitere App baut er nicht: Jede weitere
        benennt Procrastinates eingebauten Task `remove_old_jobs` um.

    Betriebsmittel:
        RunPod-Zugang und Verbindungspool werden EINMAL gebaut, von allen Tasks
        geteilt und beim Herunterfahren geschlossen — in dieser Reihenfolge:
        erst die Worker beenden, dann die Betriebsmittel schliessen. Andersherum
        zoege man einem noch laufenden Task den Boden weg, und dessen letzter
        Schreibvorgang — der mit dem bezahlten Ergebnis — waere der, der
        fehlschlaegt.

    Nebenlaeufigkeit:
        Mehrere Daemon-Prozesse auf derselben Datenbank sind moeglich und
        unschaedlich: Procrastinate verteilt die Jobs, und der `lock` je Dokument
        verhindert, dass zwei dasselbe Dokument anfassen. Gewollt ist es
        trotzdem nicht — ein Prozess ist beobachtbar, zwei sind eine Frage mehr,
        die niemand stellt, wenn etwas schiefgeht.

    Raises:
        ConfigurationError: Kein Endpoint oder kein RunPod-Schluessel eingestellt
            (`Settings.load` laesst beide fehlen, weil Abschicken und Lesen sie
            nicht brauchen); Datenbank nicht erreichbar oder Schema fehlt. Der
            Daemon startet dann gar nicht erst, statt still nichts zu tun — oder,
            ohne Endpoint, jeden Job anzunehmen und endgueltig als `failed` zu
            schreiben.

    Kosten: JA, mittelbar — er arbeitet die Warteschlange ab.
    """
    # VOR allem anderen, auch fuer Aufrufer aus Python (`main`, `run`): Ohne
    # Endpoint naehme jeder Worker Jobs an, scheiterte beim Absenden an der
    # Konfiguration, und `classify_failure` machte daraus einen Endzustand.
    settings.require_endpoint_id()
    settings.require_api_key()
    app = tasks.build_daemon_app(settings)
    mittel = tasks.worker_context_resources(settings)
    pool = mittel["pool"]
    runpod = mittel["runpod"]

    # Der Pool wird HIER geoeffnet und nicht im Konstruktor: Letzteres ist in
    # psycopg_pool 3.3.0 abgekuendigt und schriebe eine Warnung in genau die
    # Konsole, in der der erste Bestandslauf beobachtet werden soll.
    await pool.open(wait=True)

    beenden = asyncio.Event()
    zweites_signal = False

    def _signal_empfangen(nummer: int) -> None:
        nonlocal zweites_signal
        if beenden.is_set():
            # Wer zweimal Strg-C drueckt, meint es so. Kein zweites Warten auf
            # die Nachfrist — sofort raus.
            zweites_signal = True
            for aufgabe in laufende:
                aufgabe.cancel()
            return
        log.info(
            "Signal %s empfangen — Worker werden beendet, laufende Dokumente "
            "bekommen bis zu %.0f Sekunden.",
            signal.Signals(nummer).name,
            settings.shutdown_graceful_timeout,
        )
        beenden.set()

    schleife = asyncio.get_running_loop()
    laufende: list[asyncio.Task[None]] = []
    ergebnisse: list[Any] = []

    try:
        # `App.open_async(pool=…)` reicht UNSEREN Pool durch, statt Procrastinate
        # einen zweiten aufmachen zu lassen. Sonst laufen im selben Prozess zwei
        # unabhaengige Obergrenzen fuer Verbindungen zu derselben Datenbank, von
        # denen keine in `Settings` steht — man bemerkt das erst, wenn Postgres
        # „too many connections" sagt, und dann mitten im Bestandslauf.
        async with app.open_async(pool=pool):
            for nummer in (signal.SIGTERM, signal.SIGINT):
                schleife.add_signal_handler(nummer, _signal_empfangen, nummer)
            try:
                for optionen in _worker_optionen(settings, queues, mittel):
                    laufende.append(
                        asyncio.create_task(
                            app.run_worker_async(**optionen), name=optionen["name"]
                        )
                    )

                # Auf das Signal warten — oder darauf, dass ein Worker von sich
                # aus endet. Der zweite Fall ist der wichtige: Stirbt EIN Worker
                # an einem Fehler, soll der Prozess nicht halb weiterlaufen und
                # so tun, als arbeite er noch.
                warten = asyncio.create_task(beenden.wait(), name="beenden")
                try:
                    await asyncio.wait(
                        [*laufende, warten], return_when=asyncio.FIRST_COMPLETED
                    )
                finally:
                    warten.cancel()
            finally:
                # Die Worker beenden — UND ZWAR AUCH DANN, wenn `run` selbst
                # abgebrochen wurde. Frueher stand dieser Block hinter dem
                # `await asyncio.wait(...)` im try-Koerper: Ein Abbruch von `run`
                # liess den CancelledError aus `asyncio.wait` heraus, und damit
                # lief weder das Abbrechen der Worker noch das Einsammeln. Der
                # aeussere `finally` schloss trotzdem Pool und HTTP-Zugang — also
                # genau die Reihenfolge, die der Vertrag oben ausschliesst.
                #
                # Gemessen wurde das mit 21 verwaisten, noch laufenden Tasks bei
                # bereits geschlossenem Pool; die Worker liefen danach in
                # `psycopg_pool.PoolClosed`. Beobachtet wurde dabei auch der
                # teure Fall selbst: ein laufender Dokument-Job starb am
                # geschlossenen Pool, statt sein bezahltes Ergebnis zu schreiben.
                # Zweite messbare Folge: ueber einen Lauf mit rund fuenfzehn
                # Daemon-Starts kam genau EIN `unregister_worker` zustande — der
                # Rest blieb als Karteileiche in `procrastinate_workers` stehen,
                # was die Integrationstests reihenfolgeabhaengig scheitern liess.
                for aufgabe in laufende:
                    aufgabe.cancel()
                if laufende:
                    ergebnisse = list(
                        await _unabbrechbar_abwarten(
                            asyncio.gather(*laufende, return_exceptions=True)
                        )
                    )
                for nummer in (signal.SIGTERM, signal.SIGINT):
                    schleife.remove_signal_handler(nummer)
    finally:
        # Betriebsmittel ZULETZT schliessen, nach den Workern. Andersherum zoege
        # man einem noch laufenden Task den Boden weg, und dessen letzter
        # Schreibvorgang — der mit dem bezahlten Ergebnis — waere der, der
        # fehlschlaegt.
        #
        # Im `finally` und nicht dahinter: Wird `run` SELBST abgebrochen — was
        # beim Prozess-Abbau der Normalfall ist —, springt die Ausfuehrung aus
        # dem `async with` heraus, und alles, was danach steht, laeuft nie.
        await _unabbrechbar_abwarten(_betriebsmittel_schliessen(runpod, pool))

    if zweites_signal:
        log.warning("Zweites Signal — sofort beendet, ohne Nachfrist.")

    # Ein Worker, der mit einem Fehler geendet ist, wird WEITERGEREICHT. Ihn zu
    # schlucken hiesse, einen Prozess zu haben, der sich beendet hat und einen
    # Exit-Code 0 meldet — die Art Fehlschlag, die man erst am leeren Bestand
    # bemerkt.
    for ergebnis in ergebnisse:
        if isinstance(ergebnis, BaseException) and not isinstance(
            ergebnis, asyncio.CancelledError
        ):
            raise ergebnis


async def _unabbrechbar_abwarten(arbeit: Awaitable[T]) -> T:
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


async def _betriebsmittel_schliessen(runpod: Any, pool: Any) -> None:
    """Schliesst HTTP-Zugang und Verbindungspool — beide, auch wenn einer bricht.

    Einzeln gefangen und nicht in einem Rutsch: Scheitert das Schliessen des
    einen, soll das andere trotzdem geschlossen werden. Ein offen gebliebener
    Verbindungspool ist im Bestandslauf teurer als eine Meldung.
    """
    for name, schliessen in (("RunPod-Zugang", runpod.aclose), ("Pool", pool.close)):
        try:
            await schliessen()
        except Exception as fehler:  # noqa: BLE001
            log.warning("%s liess sich nicht schliessen: %s", name, fehler)


def _worker_optionen(
    settings: Settings,
    queues: Sequence[str] | None,
    mittel: dict[str, Any],
) -> list[dict[str, Any]]:
    """Stellt die Optionen je Worker zusammen.

    Ohne ausdrueckliche Auswahl sind es ZWEI: einer fuer die OCR-Queue mit
    `settings.ocr_concurrency` Plaetzen, einer fuer die Wartungs-Queue mit genau
    einem. Die Trennung ist der ganze Zweck — periodische Tasks sind gewoehnliche
    Jobs und teilen den Slot-Vorrat ihres Workers; haengen alle OCR-Plaetze in
    Warteschleifen, kaeme ein Waechter auf derselben Queue nie an die Reihe.

    Mit ausdruecklicher Auswahl ist es EINER fuer genau diese Queues. Das ist der
    Rueckfallweg auf zwei Prozesse, falls die Zwei-Koroutinen-Konstruktion nicht
    traegt (Feature-Verifikation Punkt 1, NICHT BELEGT) — und er ist damit
    vorbereitet und nicht bloss gedacht.

    `install_signal_handlers=False` bei ALLEN: Zwei Worker duerfen nicht beide
    Signal-Handler einrichten, der zweite ueberschriebe den ersten, und beim
    Beenden wuerde einer nie benachrichtigt. Der Prozess faengt die Signale
    selbst.
    """
    gemeinsam: dict[str, Any] = {
        "wait": True,
        "install_signal_handlers": False,
        "shutdown_graceful_timeout": settings.shutdown_graceful_timeout,
        "delete_jobs": settings.delete_jobs,
        "additional_context": mittel,
    }

    if queues is not None:
        return [
            {
                **gemeinsam,
                "name": "mineru-ocr-" + "-".join(queues),
                "queues": list(queues),
                "concurrency": settings.ocr_concurrency,
            }
        ]

    return [
        {
            **gemeinsam,
            "name": "mineru-ocr-ocr",
            "queues": [config.QUEUE_OCR],
            "concurrency": settings.ocr_concurrency,
        },
        {
            **gemeinsam,
            "name": "mineru-ocr-maintenance",
            "queues": [config.QUEUE_MAINTENANCE],
            # Genau EIN Platz: Der Waechter ist ein Vier-Zeilen-Task, der alle
            # zehn Minuten laeuft. Mehr Plaetze koennten nur dazu fuehren, dass
            # er sich selbst ueberholt — wogegen ohnehin sein `queueing_lock`
            # steht.
            "concurrency": 1,
        },
    ]


def main(settings: Settings, *, queues: Sequence[str] | None = None) -> int:
    """Synchroner Einstieg fuer die Kommandozeile: startet `run` und wartet.

    Args:
        settings: Vollstaendige Betriebseinstellung.
        queues: wie bei `run`.

    Returns:
        Der Exit-Code des Prozesses: 0 nach ordentlichem Herunterfahren,
        1 nach einem Abbruch mit Fehler.

    Kosten: JA, mittelbar.
    """
    try:
        asyncio.run(run(settings, queues=queues))
    except KeyboardInterrupt:
        # Kann trotz der eigenen Signal-Behandlung ankommen — etwa wenn das
        # Signal faellt, bevor die Handler eingerichtet sind. Das ist ein
        # ordentliches Ende und kein Fehler.
        log.info("Abgebrochen.")
        return 0
    except Exception as fehler:
        # Ausgeschrieben und nicht nur der Typ: Wer den Daemon startet, sieht
        # eine Konsole und keinen Debugger.
        log.error("Der Daemon ist beendet worden: %s: %s", type(fehler).__name__, fehler)
        return 1
    return 0
