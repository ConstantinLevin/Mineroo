"""Die Fassade: einreihen, warten, nachsehen. Der uebliche Einstieg.

Der Client ist die Sicht des Aufrufers auf ein System, dessen Arbeit anderswo
stattfindet. Er verarbeitet nichts. Er schreibt Zeilen, reiht Jobs ein und liest
— mit genau drei Ausnahmen, die RunPod nur BEFRAGEN: `preflight`, `credit` und
`workers`.

Der Zuschnitt der Methoden stammt aus Jurgens Braindump und trennt zwei Dinge,
die oft in einem Aufruf verschmelzen: „dann das losschicken ist einfach einen job
erstellen und das warten auf eine antwort kann dann entweder darin oder am ende
eine seperate funktion das awaiten des resultats sein" (Plan 2.3). `ocr_start`
schickt los und kehrt zurueck; `wait` wartet; `ocr` und `ocr_path` sind die
Bequemlichkeit fuer ein Einzelstueck, die beides hintereinander tut.
"""

from __future__ import annotations

from pathlib import Path
from types import TracebackType
from typing import Any

import psycopg

from .config import Settings
from .db import DerivedStatus
from .wrapper import OCRMegaWrapper, OCRWrapper


class OCRClient:
    """Zugang zum OCR-Dienst. Als Kontext-Manager benutzen.

    Besitz und Lebensdauer: Der Client oeffnet beim Betreten eine
    Datenbankverbindung und schliesst sie beim Verlassen. Huellen, die er
    ausgegeben hat, halten diese Verbindung — sie nach dem Verlassen weiter zu
    benutzen ist ein Fehler.

    Nebenlaeufigkeit: Ein Client gehoert einem Aufrufer, synchron. Wer aus
    mehreren Programmteilen zugreift, nimmt mehrere Clients; die Datenbank
    darunter vertraegt beliebig viele.
    """

    def __init__(
        self,
        db_dsn: str | None = None,
        endpoint_id: str | None = None,
        api_key: str | None = None,
        *,
        preflight: bool = True,
        strict: bool = True,
        min_balance: float = 1.0,
        backend: str = "vlm-auto-engine",
    ) -> None:
        """Baut den Client. Die Angaben gehen an `Settings.load`.

        Args:
            db_dsn: Voreinstellung siehe `Settings.load`.
            endpoint_id: dito.
            api_key: dito.
            preflight: Ob beim Betreten geprueft wird. True ist die
                Voreinstellung, weil die haeufigste Betriebs-Ueberraschung dieser
                Architektur „es passiert nichts" bei totem Daemon ist — und der
                Preflight sagt es einem.
            strict: Ob eine Abweichung der Endpoint-Konfiguration abbricht.
            min_balance: Untergrenze fuer den Preflight.
            backend: Wird beim Einreihen als Task-Angabe mitgegeben.

        Kosten: keine. Der Konstruktor spricht mit niemandem — erst `__enter__`.
        """
        raise NotImplementedError

    def __enter__(self) -> OCRClient:
        """Oeffnet die Verbindung und fuehrt den Preflight aus."""
        raise NotImplementedError

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        raise NotImplementedError

    @property
    def settings(self) -> Settings:
        """Die geltende Einstellung. Der Schluessel ist in der Ausgabe maskiert."""
        raise NotImplementedError

    @property
    def conn(self) -> psycopg.Connection[Any]:
        """Die offene Verbindung — fuer die Huellen, die sie brauchen.

        Sie GEHOERT dem Client und wird von ihm geschlossen. Wer sie nach dem
        Verlassen des Kontext-Managers weiterbenutzt, bekommt einen Fehler von
        psycopg, und das ist richtig so: Eine Huelle, die nach dem Schliessen
        noch Zeilen laese, waere eine Huelle mit unklarer Lebensdauer.

        Raises:
            ConfigurationError: Der Client ist nicht betreten worden.
        """
        raise NotImplementedError

    # ----------------------------------------------------------------------
    # Preflight
    # ----------------------------------------------------------------------

    def preflight(self) -> dict[str, Any]:
        """Prueft, ob gearbeitet werden kann — und VERAENDERT DABEI NICHTS.

        Geprueft wird:
            * Der Endpoint existiert.
            * Soll gegen Ist bei `workersMax >= 1`, `workersMin == 0`,
              `idleTimeout == 60`, `executionTimeoutMs == 600000`.
              Die Umgebungsvariablen des Workers sind nur pruefbar, wenn gerade
              ein Worker laeuft; sonst lautet das Ergebnis „nicht pruefbar" und
              ausdruecklich NICHT „in Ordnung". Der Unterschied ist der ganze
              Wert dieser Pruefung.
            * Das Worker-Image. Ein alter Bezeichner ist eine WARNUNG, kein
              Abbruch — auch mit altem Image kann man arbeiten, man sollte es nur
              wissen.
            * Die Rechenzentren, schlicht gemeldet. Fuer den Steuer-Bestand gilt
              die EU-Pflicht; der Client richtet kein Verhalten danach, weil das
              eine Entscheidung des Menschen ist und kein Schalter im Programm
              (Plan 2.4, kein `pii`-Flag).
            * Der Kontostand gegen `min_balance`.
            * Ob ein Daemon lebt — ueber Procrastinates Worker-Lebenszeichen. Das
              ist eine undokumentierte Flaeche, eingesperrt in
              `db.worker_heartbeats`; siehe die Warnung dort.

        Returns:
            Ein Befund je Pruefpunkt mit Soll, Ist und Bewertung (in Ordnung,
            Warnung, Abweichung, nicht pruefbar).

        Zusicherung:
            MUTIERT NIE. Es gibt in diesem Paket keinen Weg, `workersMax` zu
            setzen, ein Template zu aendern oder einen Endpoint anzulegen — nicht
            als Voreinstellung, sondern gar nicht. Bezahlte Betriebsmittel
            eigenmaechtig anzulegen oder zu aendern ist ausgeschlossen (Plan 8,
            Regel 10).

        Raises:
            ConfigDriftError: Nur bei `strict=True` und einer echten Abweichung.
                Ohne `strict` steht dieselbe Abweichung als Warnung im Befund.

        Kosten: keine.
        """
        raise NotImplementedError

    # ----------------------------------------------------------------------
    # Arbeit
    # ----------------------------------------------------------------------

    def ocr_start(self, target: OCRWrapper | OCRMegaWrapper) -> OCRWrapper | OCRMegaWrapper:
        """Reiht alles ein, was noch kein Endergebnis hat. Blockiert nicht.

        Eingereiht wird jede Zeile OHNE endgueltigen Zustand — also alle auf
        `new`, einschliesslich derer, deren juengster Job endgueltig endete, ohne
        etwas zu schreiben. Auch die laufen dann schlicht noch einmal: „es wird
        dann ncohmal und das wars" (Plan 2.4).

        Args:
            target: Eine Huelle oder eine ganze Sammlung.

        Returns:
            Dasselbe Objekt, damit sich Aufrufe verketten lassen.

        Zusicherung:
            Kehrt sofort zurueck — der Daemon arbeitet, nicht dieser Aufruf. Wer
            warten will, nimmt `wait` oder `ocr`.

            Reiht EINZELN ein, nie im Stapel: Ein Stapel mit einem einzigen
            Konflikt wuerde vollstaendig zurueckgerollt (Feature-Verifikation
            Punkt 9) — bei dreissigtausend Dokumenten waere ein einziges bereits
            wartendes genug, damit gar nichts passiert.

            Einzeln heisst bei zehntausend Dokumenten auch zehntausend kleine
            Einfuege-Vorgaenge. Das ist schnell, aber es ist kein einzelner
            Aufruf: Der Vorgang ist an jeder Stelle UNTERBRECHBAR, und was schon
            eingereiht ist, bleibt eingereiht. Ein Abbruch mittendrin ist kein
            halber Zustand, sondern ein kleinerer — beim naechsten Aufruf laeuft
            nur noch der Rest.

            Laeuft ein Dokument schon, ist das kein Fehler, sondern der Normalfall
            beim wiederholten Durchlaufen eines Ordners.

        Raises:
            ConfigurationError: Kein Daemon erreichbar UND `strict` — sonst nur
                eine Warnung. Jobs anzuhaeufen, die niemand abholt, ist zulaessig
                (der Daemon kann spaeter starten), aber es soll gesagt sein.

        Kosten: keine unmittelbar. Mittelbar: alles, was der Daemon danach tut.
        """
        raise NotImplementedError

    def wait(
        self,
        target: str | OCRWrapper,
        timeout: float | None = None,
    ) -> OCRWrapper:
        """Wartet auf das Endergebnis EINES Dokuments.

        Lauscht auf dem Kanal `doc_done` und fragt zur Sicherheit alle fuenf
        Sekunden nach; die erste Abfrage kommt VOR dem Lauschen, damit ein
        bereits fertiges Dokument nicht auf ein Signal wartet, das schon vorbei
        ist. Das Signal ist Eigenbau — Procrastinate liefert kein „Job
        fertig"-Signal an aussenstehende Warter (Feature-Verifikation Punkt 10).

        Args:
            target: Hash oder Huelle.
            timeout: Sekunden, None fuer unbegrenzt.

        Returns:
            Die aufgefrischte Huelle — bei JEDEM Endzustand. Der Aufrufer prueft
            `.success`. Diese Methode wirft nicht, weil ein Dokument scheiterte;
            das Scheitern ist ein Ergebnis.

        Raises:
            DocumentNotFound: Unbekannter Hash — sofort, nicht nach Ablauf.
            TimeoutError: Die Zeit lief ab. Sagt nichts ueber das Dokument, nur
                ueber die Geduld.

        Kosten: keine.
        """
        raise NotImplementedError

    def ocr(self, data: bytes | OCRWrapper, name: str | None = None) -> OCRWrapper:
        """Ein Einzelstueck aus Bytes: einreihen und warten.

        Ist die Huelle bereits fertig, ist der Aufruf ein Nichts-Tun und kehrt
        sofort zurueck — Jurgens Regel: „wenn ocrwrapper bereits OCR'd ist dann
        ist es eine noop" (Plan 2.1).

        Args:
            data: Bytes oder eine bestehende Huelle.
            name: Anzeigename, noetig bei Bytes.

        Returns:
            Die fertige Huelle.

        Kosten: JA, mittelbar.
        """
        raise NotImplementedError

    def ocr_path(self, path: Path) -> OCRWrapper:
        """Dasselbe fuer eine Datei auf der Platte."""
        raise NotImplementedError

    def retry_failed(self, target: OCRWrapper | OCRMegaWrapper) -> int:
        """Legt gescheiterte Dokumente erneut vor. Duenner Durchgriff, kein Eigenbau.

        Benutzt Procrastinates eigenes Werkzeug (`retry_job_by_id`) auf den
        juengsten Job jedes gescheiterten Dokuments — „sie werden ja einfach von
        procrastinate dann zurückgesetzt das hat ja tooling dafür" (Plan 2.4).

        Erfasst werden AUSSCHLIESSLICH Dokumente im Zustand `failed`. Nicht
        `input_error`: Der Terminal-Guard des Executors deckt den ab, ein so
        wiedervorgelegter Job taete also nichts — verbrauchte aber einen Versuch
        aus dem Budget und wuerde im Rueckgabewert als erledigte Arbeit gezaehlt,
        die nie stattfindet. Ein zu grosses DOCX liesse sich damit scheinbar
        wiederholen, bliebe `input_error`, und beim naechsten echten Bedarf waere
        das Budget aufgebraucht. (`OCRMegaWrapper.failed` fasst beide Zustaende
        zusammen — hier wird ausdruecklich enger gefiltert.)

        Zwei Eigenheiten des Werkzeugs, die man kennen muss (beide am Objekt der
        installierten 3.9.0 belegt, Feature-Verifikation Punkt 19):

        Es nimmt nur Jobs in `doing` oder `failed`. Ein `aborted`-Job — etwa vom
        Herunterfahren — wird ABGEWIESEN. Sein Heilungsweg ist `ocr_start`, nicht
        diese Methode; deshalb ueberspringt sie solche Dokumente und zaehlt sie
        im Rueckgabewert nicht mit, statt sie mit einer Ausnahme aufzuhalten.

        Und jede Wiedervorlage erhoeht `attempts`. Ein Retry von Hand auf einen
        erschoepften Job ist genau EIN weiterer Versuch, keine neue Dreier-Runde.

        Fuer Einzelfaelle gibt es ausserdem das fertige Handwerkzeug
        `procrastinate shell` mit `retry`, `cancel` und `list_jobs`. Diese Methode
        ist Bequemlichkeit fuer den Stapel, kein Ersatz dafuer.

        Args:
            target: Eine Huelle oder eine Sammlung.

        Returns:
            Wie viele Jobs tatsaechlich wieder vorgelegt wurden.

        Zusicherung:
            Der wieder vorgelegte Job verarbeitet das Dokument WIRKLICH neu und
            ueberschreibt sein `raw`. Das ist kein Nebeneffekt, sondern der Zweck
            — und der Grund, warum der Terminal-Guard `failed` nicht deckt.

        Kosten: JA, mittelbar — jedes wieder vorgelegte Dokument wird erneut
        verarbeitet und erneut bezahlt.
        """
        raise NotImplementedError

    # ----------------------------------------------------------------------
    # Nachsehen
    # ----------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """Der Gesamtzustand — ABGELEITET, nirgends gespeichert.

        Endzustaende werden aus `documents` gezaehlt; Wartendes und Laufendes
        kommt aus Procrastinates Job-Tabellen. Nichts davon wird gespiegelt — das
        ist der ganze Sinn von „ein Buch, nicht zwei" (Plan 2.4).

        AUSSCHLIESSLICH in Summen, nie Dokument fuer Dokument. Das ist keine
        Bequemlichkeit, sondern eine Groessenfrage: Der Bestand hat
        zehntausend und mehr Dokumente, und der abgeleitete Zustand je Dokument
        braucht dessen juengsten Job. Es gibt in Procrastinates Lese-Schnittstelle
        keinen Filter „der juengste Job je `queueing_lock`" — wer das je Dokument
        aufloest, macht zehntausend Abfragen fuer eine Uebersicht, die niemand
        Dokument fuer Dokument liest.

        Gezaehlt wird deshalb so: Terminal-Zustaende mit einem `GROUP BY status`
        ueber `documents` (`db.counts_by_status`), Job-Zustaende mit
        `list_tasks`/`list_queues`, die fertige Zaehler je Zustand liefern
        (`todo`, `doing`, `succeeded`, `failed`, `cancelled`, `aborted`). Zwei
        Abfragen, unabhaengig von der Bestandsgroesse.

        Die Aufloesung je Dokument gibt es nur fuer ein BENANNTES Dokument —
        `document()` und `get()`. Das ist die richtige Stelle dafuer: Wer ein
        einzelnes Dokument sucht, nennt es.

        Returns:
            Zaehler je Dokument-Zustand, Zaehler je Job-Zustand, die Zahl offener
            Aufruest-Faelle, der Kontostand und ob ein Daemon lebt.

        Kosten: keine.
        """
        raise NotImplementedError

    def document(self, target: str | Path) -> DerivedStatus:
        """Der abgeleitete Zustand EINES Dokuments, ueber Hash oder Pfad.

        Raises:
            DocumentNotFound: Unbekannt.
        """
        raise NotImplementedError

    def get(self, target: str | Path) -> OCRWrapper:
        """Holt die Huelle zu einem bereits bekannten Dokument, ohne zu warten.

        Der Weg von einem Hash zu einem Ergebnis. `document()` gibt den Zustand,
        diese Methode gibt die Sache selbst — Text, Strukturliste, Bilder,
        Rohbestand. Ohne sie kaeme man an ein fertiges Ergebnis nur ueber `wait`
        (blockiert) oder ueber einen erneuten Lauf, und die Kommandozeile muesste
        sich ihre eigene Fassung der Getter bauen.

        Args:
            target: Hash oder Pfad. Die Unterscheidung ist festgelegt und nicht
                geraten: vierundsechzig Zeichen aus `[0-9a-f]` sind ein Hash,
                alles andere ist ein Pfad. Ein Verzeichnisname kann diese Form
                zwar annehmen, aber dann sind Hash und Pfad ohnehin dasselbe
                Dokument oder es gibt keins. Dieselbe Regel gilt in `document`,
                `wait` und allen Kommandos, die „Hash oder Pfad" annehmen.

        Raises:
            DocumentNotFound: Unbekannt. Ob das Dokument fertig ist, sagt die
                Huelle (`.status`, `.success`) — nicht diese Methode.

        Kosten: keine.
        """
        raise NotImplementedError

    def credit(self) -> float:
        """Der Kontostand, live geholt.

        Bewusst kein zwischengespeicherter Wert: „dynmiasche attribute wie
        runpodcredit […] die per getter direkt von der api gegettet werden wenn
        man darauf zugreift" (Plan 2.1). Ein zwischengespeicherter Kontostand ist
        genau dann falsch, wenn es darauf ankommt.
        """
        raise NotImplementedError

    def account(self) -> dict[str, Any]:
        """Die Kontoangaben als Ganzes."""
        raise NotImplementedError

    def workers(self) -> list[dict[str, Any]]:
        """Die Worker EINZELN aufgelistet — die zusammengefassten Zaehler luegen
        (Plan 8, Regel 11). Leere Liste heisst „gerade laeuft keiner" und ist bei
        einem auf null herunterskalierenden Endpoint der Normalfall."""
        raise NotImplementedError
