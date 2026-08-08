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

import asyncio
import concurrent.futures
import re
from collections.abc import Awaitable, Callable, Coroutine
from pathlib import Path
from types import TracebackType
from typing import Any, Final, TypeVar

import psycopg
from procrastinate import utils

from . import db, tasks
from .config import Settings
from .db import DerivedStatus, DocumentStatus
from .errors import ConfigDriftError, ConfigurationError, DocumentNotFound
from .runpod import RunPodClient
from .wrapper import OCRMegaWrapper, OCRWrapper

T = TypeVar("T")

HASH_FORM: Final = re.compile(r"^[0-9a-f]{64}$")
"""Woran ein Hash von einem Pfad unterschieden wird.

Festgelegt und nicht geraten: vierundsechzig Zeichen aus `[0-9a-f]` sind ein
Hash, alles andere ist ein Pfad. Ein Verzeichnisname kann diese Form zwar
annehmen, aber dann sind Hash und Pfad ohnehin dasselbe Dokument oder es gibt
keins. Dieselbe Regel gilt in `get`, `document` und `wait` — sie steht deshalb
EINMAL hier und nicht dreimal verstreut."""

ENDPOINT_SOLL: Final[dict[str, Any]] = {
    "workersMin": 0,
    "idleTimeout": 60,
    "executionTimeoutMs": 600000,
}
"""Die Sollwerte des Betriebshandbuchs, gegen die der Preflight vergleicht.

`workersMax` fehlt hier mit Absicht: Dafuer gilt keine feste Zahl, sondern
„mindestens eins" — steht es auf null, kann nichts laufen. Das wird eigens
geprueft, weil ein Vergleich auf Gleichheit dort eine Zahl erfinden muesste."""


def _synchron_ausfuehren(arbeit: Coroutine[Any, Any, T]) -> T:
    """Fuehrt eine Koroutine aus synchronem Code aus — auch aus einer Schleife.

    Der Client ist synchron, mehrere seiner Auskuenfte kommen aber aus
    asynchronen Quellen (RunPod, Procrastinates Job-Verwaltung). Der uebliche
    Weg dafuer ist `asyncio.run`.

    DER FALL, DER DABEI BRICHT, und der beim Bauen aufgefallen ist: `asyncio.run`
    verweigert die Arbeit, wenn im selben Faden schon eine Ereignisschleife
    laeuft — „asyncio.run() cannot be called from a running event loop". Das ist
    kein Test-Artefakt, sondern der Alltag: In einem Notebook laeuft immer eine,
    und wer den Client aus einer asynchronen Anwendung heraus benutzt, ebenso.
    Eine synchrone Fassade, die von dort nicht benutzbar ist, waere nur halb
    synchron.

    Deshalb: Laeuft schon eine Schleife, wird die Arbeit in einen EIGENEN FADEN
    mit eigener Schleife gegeben und dort zu Ende gefuehrt. Der Aufrufer merkt
    nichts davon ausser der Wartezeit, die er ohnehin hat.

    Warum nicht `nest_asyncio` oder Aehnliches: Das flickt an der Schleife des
    Aufrufers herum, und es ist eine zusaetzliche Abhaengigkeit fuer ein
    Problem, das die Standardbibliothek loest.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # Keine Schleife im Faden — der Normalfall, und der billigste Weg.
        return asyncio.run(arbeit)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as faden:
        return faden.submit(asyncio.run, arbeit).result()


def _runpod_fragen(
    settings: Settings, was: Callable[[RunPodClient], Awaitable[T]]
) -> T:
    """Fuehrt EINE lesende RunPod-Abfrage aus synchronem Code aus.

    Der Client ist synchron, der RunPod-Zugang asynchron — dazwischen steht
    dieser Uebergang. Er baut den Zugang, fragt und schliesst ihn wieder.

    Warum je Abfrage ein eigener Zugang und kein dauerhafter: Der Client stellt
    diese Fragen selten (Preflight, Kontostand, Worker-Liste) und nie in einer
    Schleife. Ein dauerhafter Zugang haette dagegen eine Lebensdauer, die an den
    Kontext-Manager gebunden werden muesste — und ein offener HTTP-Client, den
    niemand benutzt, ist ein Betriebsmittel ohne Zweck. Der Daemon, der wirklich
    in Schleife fragt, teilt sich sehr wohl einen.
    """

    async def lauf() -> T:
        async with RunPodClient(settings) as zugang:
            return await was(zugang)

    return _synchron_ausfuehren(lauf())


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
        self._settings = Settings.load(
            db_dsn=db_dsn,
            endpoint_id=endpoint_id,
            api_key=api_key,
            strict=strict,
            backend=backend,
            min_balance_start_usd=min_balance,
        )
        self._preflight_gewuenscht = preflight
        self._conn: psycopg.Connection[Any] | None = None
        self._letzter_preflight: dict[str, Any] | None = None

    def __enter__(self) -> OCRClient:
        """Oeffnet die Verbindung und fuehrt den Preflight aus."""
        self._conn = db.connect(self._settings.db_dsn)
        # Der Produzenten-Zugang: synchron, und er kann NUR einreihen. Ein
        # Produzent, der versehentlich einen Worker startet, waere ein zweiter
        # Exekutor, von dem niemand weiss.
        tasks.configure(self._settings, for_worker=False)
        if self._preflight_gewuenscht:
            self._letzter_preflight = self.preflight()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @property
    def settings(self) -> Settings:
        """Die geltende Einstellung. Der Schluessel ist in der Ausgabe maskiert."""
        return self._settings

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
        if self._conn is None:
            raise ConfigurationError(
                "Der Client ist nicht betreten worden. Er oeffnet seine "
                "Verbindung erst dort: `with OCRClient() as client: …`"
            )
        return self._conn

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
        befunde: list[dict[str, Any]] = []

        def merken(punkt: str, soll: Any, ist: Any, bewertung: str) -> None:
            befunde.append(
                {"punkt": punkt, "soll": soll, "ist": ist, "bewertung": bewertung}
            )

        # --- Endpoint und seine Konfiguration ---------------------------
        try:
            konfiguration = _runpod_fragen(self._settings, lambda z: z.endpoint())
        except Exception as fehler:  # noqa: BLE001
            merken("endpoint", "erreichbar", f"{type(fehler).__name__}: {fehler}", "abweichung")
            konfiguration = {}
        else:
            merken("endpoint", "erreichbar", konfiguration.get("id"), "in_ordnung")

            for feld, soll in ENDPOINT_SOLL.items():
                ist = konfiguration.get(feld)
                merken(
                    feld, soll, ist, "in_ordnung" if ist == soll else "abweichung"
                )

            hoechstzahl = konfiguration.get("workersMax")
            merken(
                "workersMax",
                ">= 1",
                hoechstzahl,
                "in_ordnung"
                if isinstance(hoechstzahl, int) and hoechstzahl >= 1
                else "abweichung",
            )

            # Die Umgebungsvariablen des Workers sind NUR pruefbar, wenn gerade
            # einer laeuft. Sonst lautet das Ergebnis „nicht pruefbar" und
            # ausdruecklich NICHT „in Ordnung" — der Unterschied ist der ganze
            # Wert dieser Pruefung.
            laufende = konfiguration.get("workers") or []
            umgebungen = [
                w.get("env") for w in laufende if isinstance(w, dict) and w.get("env")
            ]
            merken(
                "worker_env",
                "MINERU_MAX_CONCURRENCY vorhanden",
                umgebungen[0] if umgebungen else None,
                "in_ordnung" if umgebungen else "nicht_pruefbar",
            )

            # Die Rechenzentren werden schlicht GEMELDET. Fuer den
            # Steuer-Bestand gilt die EU-Pflicht; der Client richtet kein
            # Verhalten danach, weil das eine Entscheidung des Menschen ist und
            # kein Schalter im Programm (Plan 2.4, kein `pii`-Flag).
            merken(
                "datacenters",
                "EU fuer den Steuer-Bestand (Entscheidung des Menschen)",
                konfiguration.get("dataCenterIds") or konfiguration.get("gpuTypeIds"),
                "gemeldet",
            )

        # --- Das Worker-Image: Warnung, kein Abbruch --------------------
        try:
            marke = _runpod_fragen(self._settings, lambda z: z.image_tag())
        except Exception as fehler:  # noqa: BLE001
            merken("image_tag", "lesbar", f"{type(fehler).__name__}", "nicht_pruefbar")
        else:
            # Ein alter Bezeichner ist eine WARNUNG und kein Abbruch: Auch mit
            # altem Image kann man arbeiten, man sollte es nur wissen.
            merken(
                "image_tag",
                "aktuelles Worker-Image",
                marke,
                "warnung" if marke and ":17d4955b2" in marke else "in_ordnung",
            )

        # --- Kontostand -------------------------------------------------
        try:
            stand = _runpod_fragen(self._settings, lambda z: z.balance())
        except Exception as fehler:  # noqa: BLE001
            merken("balance", self._settings.min_balance_start_usd,
                   f"{type(fehler).__name__}", "nicht_pruefbar")
        else:
            merken(
                "balance",
                self._settings.min_balance_start_usd,
                stand,
                "in_ordnung"
                if stand >= self._settings.min_balance_start_usd
                else "abweichung",
            )

        # --- Lebt ein Daemon? -------------------------------------------
        # Die haeufigste Betriebs-Ueberraschung dieser Architektur: Alles sieht
        # normal aus, nur arbeitet niemand. Der Client SAGT es, statt es einen
        # suchen zu lassen.
        try:
            lebenszeichen = db.worker_heartbeats(self.conn)
        except Exception as fehler:  # noqa: BLE001
            merken("daemon", "laeuft", f"{type(fehler).__name__}", "nicht_pruefbar")
        else:
            merken(
                "daemon",
                "mindestens einer",
                len(lebenszeichen),
                "in_ordnung" if lebenszeichen else "warnung",
            )

        abweichungen = [b for b in befunde if b["bewertung"] == "abweichung"]
        if abweichungen and self._settings.strict:
            raise ConfigDriftError(
                f"Der Preflight fand {len(abweichungen)} Abweichung(en): "
                f"{abweichungen}\nMit `strict=False` sind das Warnungen statt "
                f"eines Abbruchs. Der Preflight VERAENDERT nichts — jede "
                f"Anpassung am Endpoint ist eine Entscheidung des Menschen "
                f"(Kosten-Regel)."
            )
        return {"checks": befunde, "drift": len(abweichungen)}

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
        if not db.worker_heartbeats(self.conn):
            meldung = (
                "Es laeuft kein Daemon. Die Jobs werden eingereiht und bleiben "
                "liegen, bis einer startet — das ist zulaessig, aber es soll "
                "gesagt sein. Starten mit `mineru-ocr daemon`."
            )
            if self._settings.strict:
                raise ConfigurationError(meldung)

        huellen = [target] if isinstance(target, OCRWrapper) else list(target)
        for huelle in huellen:
            if huelle.status.is_terminal:
                # Fertiges wird nicht angefasst. Eingereiht wird jede Zeile OHNE
                # endgueltigen Zustand — einschliesslich derer, deren juengster
                # Job endgueltig endete, ohne etwas zu schreiben. Auch die laufen
                # dann schlicht noch einmal.
                continue
            # EINZELN, nie im Stapel: Ein Stapel mit einem einzigen Konflikt
            # wuerde vollstaendig zurueckgerollt (Feature-Verifikation Punkt 9).
            # Bei dreissigtausend Dokumenten waere ein einziges bereits
            # wartendes genug, damit gar nichts passiert.
            tasks.defer_document(huelle.sha256)
        return target

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
        sha256 = target.sha256 if isinstance(target, OCRWrapper) else target
        zeile = db.wait_for_terminal(
            self.conn,
            sha256,
            timeout=timeout,
            poll_seconds=self._settings.wait_poll_seconds,
        )
        if isinstance(target, OCRWrapper):
            target.row = zeile
            target._zwischenspeicher.clear()
            return target
        return OCRWrapper.from_row(zeile, self.conn)

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
        if isinstance(data, OCRWrapper):
            huelle = data
        else:
            if name is None:
                raise ValueError(
                    "Bei Bytes wird ein Anzeigename gebraucht — er landet in "
                    "`source_name` und ist spaeter das Einzige, woran ein Mensch "
                    "das Dokument erkennt."
                )
            huelle = OCRWrapper.from_bytes(data, name, self.conn, self._settings)

        if huelle.status.is_terminal:
            # „wenn ocrwrapper bereits OCR'd ist dann ist es eine noop"
            return huelle
        self.ocr_start(huelle)
        return self.wait(huelle)

    def ocr_path(self, path: Path) -> OCRWrapper:
        """Dasselbe fuer eine Datei auf der Platte."""
        huelle = OCRWrapper.from_path(path, self.conn, self._settings)
        if huelle.status.is_terminal:
            return huelle
        self.ocr_start(huelle)
        return self.wait(huelle)

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
        huellen = [target] if isinstance(target, OCRWrapper) else list(target)
        # AUSSCHLIESSLICH `failed`, nicht `input_error`: Der Terminal-Guard des
        # Executors deckt letzteren ab, ein so wiedervorgelegter Job taete also
        # nichts — verbrauchte aber einen Versuch aus dem Budget und wuerde hier
        # als erledigte Arbeit gezaehlt, die nie stattfindet.
        betroffene = [h for h in huellen if h.status is DocumentStatus.FAILED]
        if not betroffene:
            return 0

        app = tasks.configure(self._settings, for_worker=True)

        async def vorlegen() -> int:
            erledigt = 0
            async with app.open_async():
                for huelle in betroffene:
                    jobs = sorted(
                        await app.job_manager.list_jobs_async(
                            queueing_lock=huelle.sha256
                        ),
                        key=lambda j: j.id or 0,
                    )
                    if not jobs:
                        continue
                    juengster = jobs[-1]
                    if juengster.id is None:
                        continue
                    try:
                        await app.job_manager.retry_job_by_id_async(
                            juengster.id,
                            utils.utcnow(),
                            juengster.priority,
                            juengster.queue,
                            juengster.lock,
                        )
                    except Exception:  # noqa: BLE001
                        # Ein `aborted`-Job wird vom Werkzeug ABGEWIESEN (am
                        # Objekt belegt, Feature-Verifikation Punkt 19). Sein
                        # Heilungsweg ist `ocr_start`, nicht diese Methode —
                        # also uebergehen und NICHT mitzaehlen, statt mit einer
                        # Ausnahme den ganzen Stapel aufzuhalten.
                        continue
                    erledigt += 1
            return erledigt

        anzahl = _synchron_ausfuehren(vorlegen())
        # Zurueck auf den Produzenten-Zugang: Der Client ist ein Produzent, und
        # ein synchroner Zugang kann nur einreihen — das ist die Absicht.
        tasks.configure(self._settings, for_worker=False)
        return anzahl

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
        zeilen = db.counts_by_status(self.conn)

        app = tasks.configure(self._settings, for_worker=True)

        async def job_zahlen() -> list[dict[str, Any]]:
            async with app.open_async():
                return list(await app.job_manager.list_tasks_async())

        aufgaben = _synchron_ausfuehren(job_zahlen())
        tasks.configure(self._settings, for_worker=False)

        # ZWEI Abfragen, unabhaengig von der Bestandsgroesse: ein `GROUP BY
        # status` ueber `documents` und `list_tasks`, das fertige Zaehler je
        # Zustand liefert. Die Aufloesung je Dokument gibt es nur fuer ein
        # BENANNTES Dokument (`document`, `get`) — wer sie fuer alle wollte,
        # braeuchte je Dokument dessen juengsten Job, und dafuer gibt es in
        # Procrastinates Lese-Schnittstelle keinen Filter.
        jobs: dict[str, int] = {}
        for eintrag in aufgaben:
            for zustand in (
                "todo",
                "doing",
                "succeeded",
                "failed",
                "cancelled",
                "aborted",
            ):
                jobs[zustand] = jobs.get(zustand, 0) + int(eintrag.get(zustand, 0) or 0)

        offene_aufruest_faelle = self.conn.execute(
            "SELECT count(*) FROM documents "
            "WHERE status = 'input_error' AND error LIKE %s",
            ("%AUFRUESTBAR%",),
        ).fetchone()

        try:
            stand: float | None = self.credit()
        except Exception:  # noqa: BLE001
            # Ein FEHLGESCHLAGENER Kontostand ist nicht dasselbe wie ein leeres
            # Konto. Er wird als „unbekannt" gemeldet und nicht als Null.
            stand = None

        return {
            "documents": {
                zustand.value: anzahl for zustand, anzahl in zeilen.items()
            },
            "jobs": jobs,
            "upgrade_cases": offene_aufruest_faelle[0] if offene_aufruest_faelle else 0,
            "balance": stand,
            "daemon_alive": bool(db.worker_heartbeats(self.conn)),
        }

    def document(self, target: str | Path) -> DerivedStatus:
        """Der abgeleitete Zustand EINES Dokuments, ueber Hash oder Pfad.

        Raises:
            DocumentNotFound: Unbekannt.
        """
        zeile = self._zeile_finden(target)

        app = tasks.configure(self._settings, for_worker=True)

        async def jobs_holen() -> list[Any]:
            async with app.open_async():
                return list(
                    await app.job_manager.list_jobs_async(queueing_lock=zeile.sha256)
                )

        jobs = _synchron_ausfuehren(jobs_holen())
        tasks.configure(self._settings, for_worker=False)
        return db.derive_status(zeile, jobs)

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
        return OCRWrapper.from_row(self._zeile_finden(target), self.conn)

    def _zeile_finden(self, target: str | Path) -> db.DocumentRow:
        """Hash ODER Pfad zu einer Zeile — die eine Stelle mit dieser Regel.

        Die Unterscheidung steht als Muster in `HASH_FORM` und nicht dreimal
        verstreut: `get`, `document` und die Kommandozeile nehmen alle „Hash oder
        Pfad", und drei Fassungen derselben Regel liefen frueher oder spaeter
        auseinander.
        """
        text = str(target)
        zeile = (
            db.get_document(self.conn, text)
            if HASH_FORM.match(text)
            else db.get_document_by_path(self.conn, str(Path(text).resolve()))
        )
        if zeile is None:
            raise DocumentNotFound(
                f"Kein Dokument zu {text!r}. Angenommen werden ein sha256 "
                f"(vierundsechzig Zeichen aus 0-9a-f) oder ein Pfad."
            )
        return zeile

    def credit(self) -> float:
        """Der Kontostand, live geholt.

        Bewusst kein zwischengespeicherter Wert: „dynmiasche attribute wie
        runpodcredit […] die per getter direkt von der api gegettet werden wenn
        man darauf zugreift" (Plan 2.1). Ein zwischengespeicherter Kontostand ist
        genau dann falsch, wenn es darauf ankommt.
        """
        return _runpod_fragen(self._settings, lambda z: z.balance())

    def account(self) -> dict[str, Any]:
        """Die Kontoangaben als Ganzes."""
        return _runpod_fragen(self._settings, lambda z: z.account())

    def workers(self) -> list[dict[str, Any]]:
        """Die Worker EINZELN aufgelistet — die zusammengefassten Zaehler luegen
        (Plan 8, Regel 11). Leere Liste heisst „gerade laeuft keiner" und ist bei
        einem auf null herunterskalierenden Endpoint der Normalfall."""
        return _runpod_fragen(self._settings, lambda z: z.workers())
