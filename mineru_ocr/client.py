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
import hashlib
import logging
import re
from collections.abc import Awaitable, Callable, Coroutine
from pathlib import Path
from types import TracebackType
from typing import Any, Final, TypeVar

import procrastinate
import procrastinate.exceptions
import psycopg
from procrastinate import utils
from psycopg_pool import NullConnectionPool

from . import db, tasks
from .config import Settings
from .db import DerivedStatus, DocumentStatus
from .errors import ConfigDriftError, ConfigurationError, DocumentNotFound
from .runpod import RunPodClient
from .wrapper import OCRMegaWrapper, OCRWrapper

log = logging.getLogger("mineru_ocr.client")
"""Der Logger dieses Moduls.

Er steht hier, weil `ocr_start` eine Warnung ueber den fehlenden Daemon BAUTE
und dann wegwarf: Bei `strict` wurde sie geworfen, sonst verfiel sie
ersatzlos — `client.py` kannte `logging` nicht. Der Docstring sagte „aber es
soll gesagt sein", und gesagt wurde nichts."""

T = TypeVar("T")

HASH_FORM: Final = re.compile(r"^[0-9a-f]{64}$")
"""Woran ein Hash von einem Pfad unterschieden wird.

Festgelegt und nicht geraten: vierundsechzig Zeichen aus `[0-9a-f]` sind ein
Hash, alles andere ist ein Pfad. Ein Verzeichnisname kann diese Form zwar
annehmen, aber dann sind Hash und Pfad ohnehin dasselbe Dokument oder es gibt
keins. Dieselbe Regel gilt in `get`, `document` und `wait` — sie steht deshalb
EINMAL hier und nicht dreimal verstreut."""

def _synchron_ausfuehren(arbeit: Coroutine[Any, Any, T]) -> T:
    """Fuehrt eine Koroutine aus synchronem Code aus — auch aus einer Schleife.

    Der Client ist synchron, die Auskuenfte von RunPod kommen aber aus einer
    asynchronen Quelle. Der uebliche Weg dafuer ist `asyncio.run`. (Procrastinates
    Job-Verwaltung braucht diese Bruecke nicht: Der Client haelt dafuer eine
    synchrone App.)

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


OHNE_ANGABE: Final = "(nicht angegeben)"
"""Was im Befund fuer einen Worker steht, dessen Umgebung die Endpoint-Antwort
nicht fuehrt. Ein eigener Text und nicht `None`: `None` heisst dort „die Variable
fehlt in der Umgebung", und das ist etwas anderes."""


def _tag_von(image: Any) -> str | None:
    """Der Tag eines Image-Namens, `None` ohne einen.

    Gesucht wird nur im letzten Pfadteil (`registry:5000/repo` hat Port und keinen
    Tag) und nach dem letzten Doppelpunkt. Ein fuehrender Doppelpunkt (`:abc`)
    zaehlt: so steht der Tag manchmal an der Zeile, ohne den Repository-Namen.
    """
    if not isinstance(image, str):
        return None
    name = image.rsplit("/", 1)[-1]
    if ":" not in name:
        return None
    return name.rpartition(":")[2] or None


def _gesamtbewertung(zustaende: list[str], falsch: str) -> str:
    """Faltet die Zustaende der einzelnen Worker zu EINER Bewertung.

    `falsch` gewinnt vor allem, `unbekannt` vor `in_ordnung`: Ein einziger
    abweichender Worker macht den Befund zur Abweichung, ein einziger, an dem sich
    nichts feststellen laesst, nimmt ihm das „in Ordnung" — sonst bescheinigte der
    Preflight etwas, das er nur an einem Teil der Worker gesehen hat. Ohne Worker
    ist nichts zu pruefen: „nicht pruefbar", ausdruecklich nicht „in Ordnung".
    """
    if "falsch" in zustaende:
        return falsch
    if not zustaende or "unbekannt" in zustaende:
        return "nicht_pruefbar"
    return "in_ordnung"


def _worker_kennungen(arbeiter: list[Any]) -> list[tuple[str, dict[str, Any]]]:
    """Die Worker der Endpoint-Antwort mit einer Kennung je Worker.

    ALLE, die die Antwort fuehrt — auch die beendeten: Sie tragen die Umgebung und
    das Image der zuletzt gelaufenen Worker, und genau die sind es, an denen sich
    eine falsche Einstellung zeigt, wenn gerade keiner laeuft.
    """
    ergebnis: list[tuple[str, dict[str, Any]]] = []
    for nummer, eintrag in enumerate(arbeiter):
        if isinstance(eintrag, dict):
            ergebnis.append((str(eintrag.get("id") or f"#{nummer}"), eintrag))
    return ergebnis


class OCRClient:
    """Zugang zum OCR-Dienst. Als Kontext-Manager benutzen.

    Besitz und Lebensdauer: Der Client oeffnet beim Betreten eine
    Datenbankverbindung und schliesst sie beim Verlassen. Huellen, die er
    ausgegeben hat, halten diese Verbindung — sie nach dem Verlassen weiter zu
    benutzen ist ein Fehler.

    Dazu haelt er seine EIGENE Procrastinate-App (`tasks.build_client_app`, mit
    synchronem Zugang) und einen Pool, den er beim Betreten anlegt und beim
    Verlassen schliesst — `app.close()` schliesst einen uebergebenen Pool nicht.
    Der Pool ist ein `NullConnectionPool` ohne stehende Verbindungen: Zwischen
    zwei Aufrufen haelt der Client keine Procrastinate-Verbindung. Das Einreihen
    laeuft ueber seine eigene Verbindung (`connection=`), die Abfragen
    (`list_jobs`, `retry_job_by_id`, `list_tasks`) ueber je eine kurzlebige aus
    dem Pool. Je Agent bleibt so eine Verbindung: die des Clients, auch waehrend
    `wait` (das laeuft auf ihr; die Spec rechnet mit bis zu zwei, gemessen ist eine).

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
            db_dsn: Fehlt die Angabe, gilt die Konfiguration (siehe
                `Settings.load`): Umgebung, dann Datei. Eine Voreinstellung im
                Code gibt es nicht.
            endpoint_id: dito.
            api_key: Fehlt die Angabe, gilt `~/.runpod/config.toml`.
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
        self._app: procrastinate.App | None = None
        self._pool: NullConnectionPool[psycopg.Connection[Any]] | None = None
        self._letzter_preflight: dict[str, Any] | None = None

    def __enter__(self) -> OCRClient:
        """Oeffnet Verbindung, Pool und App und fuehrt den Preflight aus.

        Scheitert eines davon, wird wieder zugemacht, was schon offen ist: Ein
        `with` ruft `__exit__` nicht, wenn schon `__enter__` wirft.
        """
        try:
            self._conn = db.connect(self._settings.db_dsn)
            # Ein Pool OHNE stehende Verbindungen (`NullConnectionPool`,
            # `max_size=1`, ein Hintergrund-Faden): Er hat nichts auf Vorrat und
            # leiht fuer jede Abfrage eine kurzlebige Verbindung. Der Client legt
            # ihn selbst an und schliesst ihn selbst, weil `app.close()` einen
            # uebergebenen Pool nicht schliesst. `open=False` und dann `open()`:
            # Im Konstruktor zu oeffnen ist in psycopg_pool 3.3.0 abgekuendigt.
            pool: NullConnectionPool[psycopg.Connection[Any]] = NullConnectionPool(
                self._settings.db_dsn, max_size=1, num_workers=1, open=False
            )
            pool.open()
            self._pool = pool
            # Die App des Clients: synchroner Zugang, keine angemeldeten Tasks.
            # Sie gehoert diesem Client und wird nie umgehaengt.
            self._app = tasks.build_client_app(self._settings).open(pool)
            if self._preflight_gewuenscht:
                self._letzter_preflight = self.preflight()
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        # Erst die App, dann der Pool, zuletzt die Verbindung. `app.close()`
        # laesst einen uebergebenen Pool offen; den schliesst der, der ihn
        # angelegt hat.
        if self._app is not None:
            self._app.close()
            self._app = None
        if self._pool is not None:
            self._pool.close()
            self._pool = None
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

    @property
    def _procrastinate(self) -> procrastinate.App:
        """Die App des Clients — fuer das Einreihen und die Job-Abfragen.

        Raises:
            ConfigurationError: Der Client ist nicht betreten worden.
        """
        if self._app is None:
            raise ConfigurationError(
                "Der Client ist nicht betreten worden. Er oeffnet seine "
                "Procrastinate-App erst dort: `with OCRClient() as client: …`"
            )
        return self._app

    # ----------------------------------------------------------------------
    # Preflight
    # ----------------------------------------------------------------------

    def preflight(self) -> dict[str, Any]:
        """Prueft, ob gearbeitet werden kann — und VERAENDERT DABEI NICHTS.

        Geprueft wird:
            * Der Endpoint existiert.
            * Soll gegen Ist bei `workersMin`, `idleTimeout`, `executionTimeoutMs`
              und `workersMax`. Die Sollwerte kommen aus der Konfiguration
              (`Settings`), nicht aus dem Code.
            * Die Endpoint-Variablen, auf die es ankommt (Modellquelle,
              `MINERU_MAX_CONCURRENCY`, GPU-Speicheranteil), nach ihrem WERT, an
              der Umgebung, die die Endpoint-Antwort fuer die zuletzt gelaufenen
              Worker fuehrt — auch fuer beendete — und an jedem davon. Gibt es
              keinen Worker, lautet das Ergebnis „nicht pruefbar" und
              ausdruecklich NICHT „in Ordnung". Der Unterschied ist der ganze
              Wert dieser Pruefung.
            * Das Template-Image und das Image jedes Workers gegen den erwarteten
              Tag. Ein anderer Tag ist eine WARNUNG, kein Abbruch — auch mit
              einem anderen Image kann man arbeiten, man sollte es nur wissen.
            * Die Rechenzentren: alle muessen in der zulaessigen Liste (EWR)
              liegen. Nennt die Antwort keine, ist es „nicht pruefbar".
            * FlashBoot, nur berichtet.
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

        einstellung = self._settings

        # --- Endpoint und seine Konfiguration ---------------------------
        try:
            konfiguration = _runpod_fragen(self._settings, lambda z: z.endpoint())
        except Exception as fehler:  # noqa: BLE001
            merken("endpoint", "erreichbar", f"{type(fehler).__name__}: {fehler}", "abweichung")
            konfiguration = {}
        else:
            merken("endpoint", "erreichbar", konfiguration.get("id"), "in_ordnung")

            sollwerte = (
                ("workersMin", einstellung.workers_min),
                ("idleTimeout", einstellung.idle_timeout),
                ("executionTimeoutMs", einstellung.execution_timeout_ms),
                ("workersMax", einstellung.workers_max),
            )
            for feld, soll in sollwerte:
                ist = konfiguration.get(feld)
                merken(
                    feld, soll, ist, "in_ordnung" if ist == soll else "abweichung"
                )

            # FlashBoot wird nur berichtet: Es gibt keinen Sollwert, an dem es
            # falsch sein koennte, aber es bestimmt die Kaltstartzeit und gehoert
            # deshalb in jeden Bericht.
            flashboot = konfiguration.get("flashboot")
            merken(
                "flashboot",
                None,
                flashboot,
                "gemeldet" if isinstance(flashboot, bool) else "nicht_pruefbar",
            )

            # Die Rechenzentren muessen ALLE in der zulaessigen Liste liegen
            # (EWR, ein Sollwert der Konfiguration). Eines ausserhalb ist eine
            # Abweichung, auch wenn die uebrigen stimmen: Daran haengt, ob
            # personenbezogene Daten die EU verlassen koennen.
            #
            # AUSSCHLIESSLICH `dataCenterIds`, und KEIN Rueckfall auf
            # `gpuTypeIds`. Der Rueckfall stand hier und hat am 08.08.2026 im
            # echten Lauf genau das getan, wovor er schuetzen sollte: Er meldete
            # GPU-Typen unter der Ueberschrift „EU fuer den Steuer-Bestand". Die
            # Zeile sah aus, als haette sie die Datenschutz-Frage beantwortet, und
            # hatte ein Feld angesehen, das sie gar nicht beantworten kann.
            #
            # Nennt die Antwort keine, lautet das Ergebnis „nicht pruefbar" — die
            # Frage bleibt sichtbar offen, statt falsch beantwortet zu sein.
            zulaessig = list(einstellung.allowed_data_centers)
            rechenzentren = konfiguration.get("dataCenterIds")
            if isinstance(rechenzentren, list) and rechenzentren:
                bewertung = (
                    "in_ordnung"
                    if all(name in zulaessig for name in rechenzentren)
                    else "abweichung"
                )
            else:
                bewertung = "nicht_pruefbar"
                rechenzentren = None
            merken("datacenters", zulaessig, rechenzentren, bewertung)

            # Was TATSAECHLICH laeuft, steht an den Workern der Antwort.
            arbeiter = _worker_kennungen(konfiguration.get("workers") or [])

            # Die Endpoint-Variablen, auf die es ankommt, nach WERT. Gezeigt wird
            # nur der Wert der geprueften Variablen: Die Umgebung eines echten
            # Workers traegt `RUNPOD_AI_API_KEY` und `RUNPOD_ENDPOINT_SECRET` im
            # Klartext (am 08.08.2026 standen beide in der Konsolenausgabe eines
            # gewoehnlichen Preflights). Erlaubt wird, was bekannt harmlos ist —
            # die konfigurierten Variablen —, statt zu verbieten, was bekannt
            # gefaehrlich ist: Eine solche Liste ist immer unvollstaendig.
            for variable, env_soll in einstellung.endpoint_env.items():
                ist_je_worker: dict[str, Any] = {}
                zustaende: list[str] = []
                for kennung, eintrag in arbeiter:
                    umgebung = eintrag.get("env")
                    if not isinstance(umgebung, dict):
                        ist_je_worker[kennung] = OHNE_ANGABE
                        zustaende.append("unbekannt")
                        continue
                    wert = umgebung.get(variable)
                    ist_je_worker[kennung] = wert
                    zustaende.append("ok" if env_soll is not None and wert == env_soll else "falsch")
                if env_soll is None:
                    # Noch kein Sollwert (z. B. der GPU-Speicheranteil vor der
                    # Messung): berichten, nicht bewerten.
                    bewertung = "nicht_pruefbar"
                else:
                    bewertung = _gesamtbewertung(zustaende, "abweichung")
                merken(f"env:{variable}", env_soll, ist_je_worker, bewertung)

            # Das Image jedes Workers gegen den erwarteten Tag. Das Template kann
            # stimmen und ein Worker trotzdem noch das alte Image fahren, weil er
            # vor dem Build gestartet wurde.
            erwartet = einstellung.expected_image_tag
            tags: dict[str, Any] = {}
            zustaende = []
            for kennung, eintrag in arbeiter:
                tag = _tag_von(eintrag.get("imageName"))
                tags[kennung] = tag
                if tag is None:
                    zustaende.append("unbekannt")
                else:
                    zustaende.append("ok" if tag == erwartet else "falsch")
            merken(
                "worker_image",
                erwartet,
                tags,
                "nicht_pruefbar"
                if erwartet is None
                else _gesamtbewertung(zustaende, "warnung"),
            )

        # --- Das Template-Image: Warnung, kein Abbruch ------------------
        try:
            marke = _runpod_fragen(self._settings, lambda z: z.image_tag())
        except Exception as fehler:  # noqa: BLE001
            merken("image_tag", "lesbar", f"{type(fehler).__name__}", "nicht_pruefbar")
        else:
            # Ein anderer Tag ist eine WARNUNG und kein Abbruch: Auch mit einem
            # anderen Image kann man arbeiten, man sollte es nur wissen.
            #
            # UND EIN UNBEKANNTER ODER UNERWARTETER IST „NICHT PRUEFBAR", nicht
            # „in Ordnung". `image_tag()` gibt vertragsgemaess `None` zurueck,
            # wenn das Template den Bezeichner nicht ausweist — ein gueltiges
            # Ergebnis. Die frueherer Fassung fiel damit auf „in_ordnung": Sie
            # bescheinigte ein aktuelles Image genau dann, wenn sie ueberhaupt
            # keines kannte. Dasselbe gilt, solange kein Tag erwartet wird: Was
            # man nicht vergleichen kann, ist nicht in Ordnung.
            erwartet = einstellung.expected_image_tag
            if not marke or erwartet is None:
                merken("image_tag", erwartet, marke, "nicht_pruefbar")
            else:
                merken(
                    "image_tag",
                    erwartet,
                    marke,
                    "in_ordnung" if _tag_von(marke) == erwartet else "warnung",
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

    def check_daemon(self) -> None:
        """Sagt, ob ein Daemon laeuft: mit `strict` per Ausnahme, sonst per Warnung.

        Eigene Methode, weil die Kommandozeile sie VOR dem Ablegen der Eingaben
        braucht. Seit die Bytes in `inputs` liegen, hinterlaesst ein Abbruch erst in
        `ocr_start` die ganzen Dokumente in der Datenbank, obwohl das Kommando
        „reiht nichts ein" sagt.

        Raises:
            ConfigurationError: Kein Daemon erreichbar UND `strict`.

        Kosten: keine.
        """
        if not db.worker_heartbeats(self.conn):
            meldung = (
                "Es laeuft kein Daemon. Starten mit `mineru-ocr daemon`."
            )
            if self._settings.strict:
                raise ConfigurationError(meldung)
            log.warning(
                "%s Die Jobs werden eingereiht und bleiben liegen, bis einer "
                "startet — das ist ohne `strict` zulaessig, aber es soll gesagt "
                "sein.",
                meldung,
            )

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

        Verhalten bei fehlendem Daemon — die beiden Faelle sauber getrennt:

            Mit `strict` (der VOREINSTELLUNG) wird geworfen, BEVOR etwas
            eingereiht ist. Das ist eine ausdrueckliche Festlegung des
            Auftraggebers und keine Ableitung: „ja natürlich der client soll
            dann sterben und errorn". Praktische Folge, die man kennen muss:
            `mineru-ocr ocr ~/Scans` schlaegt ohne laufenden Daemon mit
            Exit-Code 1 fehl und reiht NICHTS ein. Das ist gewollt.

            Ohne `strict` wird eingereiht und die Lage protokolliert. Jobs
            anzuhaeufen, die niemand abholt, ist dann zulaessig — der Daemon
            kann spaeter starten.

            HIER STAND FRUEHER EIN WIDERSPRUCH, und zwar zwei Zeilen
            auseinander: „Jobs anzuhaeufen, die niemand abholt, ist zulaessig"
            unmittelbar ueber einem Wurf, der genau das verhinderte. Der Satz
            beschrieb den `strict=False`-Fall und las sich wie eine Aussage ueber
            beide. Aufgeloest ist er, indem die Faelle getrennt benannt sind —
            NICHT, indem der Wurf entfernt wird. Der Wurf ist die Entscheidung,
            der Satz war die ungenaue Beschreibung.

            Und die Warnung im nicht-strengen Fall wird jetzt wirklich
            ausgegeben. Sie wurde vorher gebaut und weggeworfen: `client.py`
            kannte `logging` nicht, also verfiel sie ersatzlos, waehrend der
            Docstring „aber es soll gesagt sein" versprach.

        Raises:
            ConfigurationError: Kein Daemon erreichbar UND `strict`.

        Kosten: keine unmittelbar. Mittelbar: alles, was der Daemon danach tut.
        """
        self.check_daemon()

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
            tasks.enqueue_document(self._procrastinate, self.conn, huelle.sha256)
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

    def ocr(self, data: bytes | OCRWrapper) -> OCRWrapper:
        """Ein Einzelstueck aus Bytes: einreihen und warten.

        Ist die Huelle bereits fertig, ist der Aufruf ein Nichts-Tun und kehrt
        sofort zurueck — Jurgens Regel: „wenn ocrwrapper bereits OCR'd ist dann
        ist es eine noop" (Plan 2.1).

        Args:
            data: Bytes oder eine bestehende Huelle.

        Returns:
            Die fertige Huelle.

        Kosten: JA, mittelbar.
        """
        if isinstance(data, OCRWrapper):
            huelle = data
        else:
            huelle = OCRWrapper.from_bytes(data, self.conn, self._settings)

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
        von 3.9.0 belegt und fuer 3.10.0 unveraendert, Feature-Verifikation Punkt 19):

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

        app = self._procrastinate
        erledigt = 0
        for huelle in betroffene:
            jobs = sorted(
                app.job_manager.list_jobs(queueing_lock=huelle.sha256),
                key=lambda j: j.id or 0,
            )
            if not jobs:
                continue
            juengster = jobs[-1]
            if juengster.id is None:
                continue
            try:
                app.job_manager.retry_job_by_id(
                    juengster.id,
                    utils.utcnow(),
                    juengster.priority,
                    juengster.queue,
                    juengster.lock,
                )
            except procrastinate.exceptions.ProcrastinateException as fehler:
                # NUR Procrastinates eigene Ablehnung wird uebergangen.
                # Ein `aborted`-Job wird vom Werkzeug ABGEWIESEN (am
                # Objekt belegt, Feature-Verifikation Punkt 19). Sein
                # Heilungsweg ist `ocr_start`, nicht diese Methode —
                # also uebergehen und NICHT mitzaehlen, statt mit einer
                # Ausnahme den ganzen Stapel aufzuhalten.
                #
                # FRUEHER STAND HIER `except Exception` — und damit
                # verschwand JEDER Fehler in diesem `continue`: ein
                # Datenbank-Aussetzer, ein geloeschter Job, ein
                # Programmierfehler. Die Kommandozeile uebersetzt die
                # Differenz zwischen gefundenen und vorgelegten Jobs
                # anschliessend in „vermutlich abgebrochene Jobs
                # (`aborted`)". Ein Postgres-Aussetzer waehrend eines
                # Stapel-Retrys ueber 200 Dokumente erschien damit als
                # „0 von 200 wieder vorgelegt — vermutlich abgebrochen":
                # eine Fehldiagnose, die als plausible Auskunft auftritt
                # und die niemand als falsch erkennen kann.
                #
                # Alles, was NICHT von Procrastinate kommt, geht deshalb
                # jetzt nach oben. Ein lauter Abbruch mitten im Stapel
                # ist unangenehm; eine stille Falschmeldung ueber den
                # ganzen Stapel ist schlimmer.
                log.debug(
                    "Job %s wurde nicht wieder vorgelegt (%s: %s) — "
                    "vermutlich `aborted`.",
                    juengster.id,
                    type(fehler).__name__,
                    fehler,
                )
                continue
            erledigt += 1
        return erledigt

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

        aufgaben = self._procrastinate.job_manager.list_tasks()

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

        jobs = self._procrastinate.job_manager.list_jobs(queueing_lock=zeile.sha256)
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
        if HASH_FORM.match(text):
            sha256 = text
        else:
            # Ein Pfad: Die Datenbank kennt keine Pfade, also wird die Datei
            # gehasht und ueber den Hash gesucht. Gibt es sie nicht, gibt es
            # auch kein Dokument dazu, das sich so finden liesse.
            pfad = Path(text)
            if not pfad.is_file():
                raise DocumentNotFound(
                    f"Kein Dokument zu {text!r}: Es ist weder ein sha256 noch "
                    f"eine lesbare Datei."
                )
            with pfad.open("rb") as datei:
                sha256 = hashlib.file_digest(datei, "sha256").hexdigest()
        zeile = db.get_document(self.conn, sha256)
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
