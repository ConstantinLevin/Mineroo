"""Der Exekutor als eigener Prozess: zwei Worker in einem, sauberes Herunterfahren.

Dies ist der einzige Prozess, der mit RunPod spricht, und der einzige mit
Nebenlaeufigkeit. Alles andere — Fassade, Kommandozeile, spaeterer
Rueckschreibeweg nach DEVONthink — legt Zeilen an und liest. Damit ist jede
Komponente einzeln startbar und tot-startbar, und der Zustand liegt zu jedem
Zeitpunkt vollstaendig an einem Ort, den man mit gewoehnlichem SQL ansehen kann.

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

from collections.abc import Sequence

from .config import Settings


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
        die betroffene Zeile bleibt `new`, der abgeleitete Zustand zeigt
        „endgueltig beendet ohne Ergebnis", und beim naechsten Einreihen laeuft
        das Dokument von vorn. Kein Waechter noetig, keine Reparatur.

        Ein zweites Signal waehrend des Herunterfahrens bricht sofort ab. Wer
        zweimal Strg-C drueckt, meint es so.

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
        ConfigurationError: Datenbank nicht erreichbar oder Schema fehlt. Der
            Daemon startet dann gar nicht erst, statt still nichts zu tun.

    Kosten: JA, mittelbar — er arbeitet die Warteschlange ab.
    """
    raise NotImplementedError


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
    raise NotImplementedError
