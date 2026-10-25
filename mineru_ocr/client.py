"""Die Fassade: einreihen, warten, nachsehen. Der uebliche Einstieg.

Der Client ist die Sicht des Aufrufers auf ein System, dessen Arbeit anderswo
stattfindet. Er verarbeitet nichts. Er schreibt Zeilen, reiht Jobs ein und liest
— mit genau drei Ausnahmen, die RunPod nur BEFRAGEN: `preflight`, `credit` und
`workers`.

Der Zuschnitt der Methoden stammt aus Jurgens Braindump und trennt zwei Dinge,
die oft in einem Aufruf verschmelzen: „dann das losschicken ist einfach einen job
erstellen und das warten auf eine antwort kann dann entweder darin oder am ende
eine seperate funktion das awaiten des resultats sein" (Plan 2.3). `ocr_start`
schickt los und kehrt zurueck; `wait` wartet; `ocr` ist die Bequemlichkeit fuer
ein Einzelstueck, die beides hintereinander tut.

Die Bibliothek nimmt BYTES und gibt HASHES. Pfade kennt sie nicht, sie kennt nur
die Kommandozeile: Wer Dateien lesen will, liest sie selbst und reicht die Bytes
herein. Der Hash, den `ocr_start` zurueckgibt, ist der Griff, mit dem es in jedem
Prozess weitergeht, auch Tage spaeter.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from collections.abc import Awaitable, Callable, Coroutine, Iterable, Sequence
from datetime import datetime
from types import TracebackType
from typing import Any, Final, TypeVar, overload

import procrastinate
import procrastinate.exceptions
import psycopg
from procrastinate import utils
from psycopg_pool import NullConnectionPool

from . import core, db, tasks
from .config import DAEMON_HEARTBEAT_MAX_AGE_SECONDS, MASKE, Settings, ist_geheimnisname
from .core import Submitted
from .db import DerivedStatus, DocumentStatus
from .errors import (
    ConfigDriftError,
    ConfigurationError,
    DatabaseError,
    DocumentNotFound,
)
from .runpod import RunPodClient
from .wrapper import OCRWrapper

log = logging.getLogger("mineru_ocr.client")
"""Der Logger dieses Moduls.

Er steht hier, weil `ocr_start` ohne laufenden Daemon trotzdem einreiht und
das SAGT: auf diesem Logger, nicht per Ausnahme. Die Kommandozeile leitet die
Warnungen des Pakets auf stderr (`cli._FehlerAusgabe`)."""

T = TypeVar("T")


def _nimmt_bytes(wert: object) -> str:
    """Die Meldung, wenn `ocr_start` etwas anderes als Bytes bekommt."""
    return (
        f"ocr_start nimmt Bytes, nicht {type(wert).__name__}. "
        f"Pfade und Text kennt die Bibliothek nicht: Wer eine Datei "
        f"abschicken will, liest sie und reicht ihre Bytes herein."
    )


def _hash_pruefen(sha256: object) -> None:
    """Weist ein Ziel ab, das kein Hash ist: Pfade kennt die Bibliothek nicht."""
    if not isinstance(sha256, str):
        raise TypeError(
            f"Angenommen wird der sha256 als Zeichenkette, nicht "
            f"{type(sha256).__name__}. Pfade kennt die Bibliothek nicht."
        )


class _SchonUnterwegs(Exception):
    """Macht aus „das Dokument ist schon unterwegs" einen Rollback der Transaktion,
    in der `_abschicken` gerade nichts Neues angelegt hat. Verlaesst die Methode
    nie."""


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
nicht oder leer fuehrt. Ein eigener Text und nicht `None`: `None` heisst dort „die Variable
fehlt in der Umgebung", und das ist etwas anderes."""


def _tag_von(image: Any) -> str | None:
    """Der Tag eines Image-Namens, `None` ohne einen.

    Gesucht wird nur im letzten Pfadteil (`registry:5000/repo` hat Port und keinen
    Tag) und nach dem letzten Doppelpunkt, ohne einen angehaengten Digest. Ein
    fuehrender Doppelpunkt (`:abc`) zaehlt: so steht der Tag manchmal an der
    Zeile, ohne den Repository-Namen.
    """
    if not isinstance(image, str):
        return None
    # Ein Digest (`repo:tag@sha256:…`) ist kein Tag: erst er, dann der Tag.
    name = image.rsplit("/", 1)[-1].partition("@")[0]
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
        schema_check: bool = True,
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
                Preflight sagt es einem. Er laeuft nur, wenn ein RunPod-Schluessel
                eingestellt ist; ohne entfaellt er still, denn er fragt RunPod,
                und Abschicken und Lesen brauchen RunPod nicht. `preflight()`
                selbst verlangt den Schluessel weiter.
            strict: Ob eine Abweichung der Endpoint-Konfiguration abbricht.
            min_balance: Untergrenze fuer den Preflight.
            backend: Wird beim Einreihen als Task-Angabe mitgegeben.
            schema_check: Ob beim Betreten das Schema geprueft wird
                (`db.check_schema`). Aus nur fuer den, der weder `documents` noch
                `inputs` liest: das Kommando `preflight`, das jederzeit laufen
                darf, auch vor `setup`.

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
        self._schema_pruefung_gewuenscht = schema_check
        self._conn: psycopg.Connection[Any] | None = None
        self._app: procrastinate.App | None = None
        self._pool: NullConnectionPool[psycopg.Connection[Any]] | None = None
        self._letzter_preflight: dict[str, Any] | None = None
        self._daemon_warnung: str | None = None

    def __enter__(self) -> OCRClient:
        """Oeffnet Verbindung, Pool und App und fuehrt den Preflight aus.

        Scheitert eines davon, wird wieder zugemacht, was schon offen ist: Ein
        `with` ruft `__exit__` nicht, wenn schon `__enter__` wirft.
        """
        try:
            self._conn = db.connect(self._settings.db_dsn)
            # Ein fehlendes Schema oder eine Tabelle aus der Zeit vor dem letzten
            # Update meldet sich hier mit dem Weg, den die Meldung nennt, nicht
            # spaeter als `UndefinedTable` oder `ValueError` beim Lesen.
            if self._schema_pruefung_gewuenscht:
                db.check_schema(self._conn)
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
            # Nur mit Schluessel: Der Preflight fragt RunPod, Abschicken nicht.
            if self._preflight_gewuenscht and self._settings.api_key is not None:
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
              `db.worker_heartbeats`; siehe die Warnung dort. Ein Daemon lebt,
              wenn ein Worker einen Herzschlag hat, der juenger ist als
              `config.DAEMON_HEARTBEAT_MAX_AGE_SECONDS`, gemessen an der Uhr der
              Datenbank; die Zeile eines abgestuerzten Daemons zaehlt nicht.

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
            ConfigurationError: Kein Endpoint oder kein RunPod-Schluessel. Der
                Preflight prueft den Endpoint bei RunPod; ohne beides waere jeder
                Befund „nicht pruefbar", und das ist eine Frage der
                Konfiguration, kein Befund.

        Kosten: keine.
        """
        self._settings.require_endpoint_id()
        self._settings.require_api_key()
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
                    if not isinstance(umgebung, dict) or not umgebung:
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
                if ist_geheimnisname(variable):
                    # Verglichen wurde der Wert; gezeigt wird er nicht. Wer ein
                    # Geheimnis in `[endpoint_env]` nennt, will es geprueft haben.
                    env_soll = MASKE if env_soll is not None else None
                    ist_je_worker = {
                        k: v if v is None or v == OHNE_ANGABE else MASKE
                        for k, v in ist_je_worker.items()
                    }
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
            tag = _tag_von(marke)
            if tag is None or erwartet is None:
                merken("image_tag", erwartet, marke, "nicht_pruefbar")
            else:
                merken(
                    "image_tag",
                    erwartet,
                    marke,
                    "in_ordnung" if tag == erwartet else "warnung",
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
            lebenszeichen = self._lebenszeichen()
        except Exception as fehler:  # noqa: BLE001
            merken("daemon", "laeuft", f"{type(fehler).__name__}", "nicht_pruefbar")
        else:
            merken(
                "daemon",
                f"mindestens einer mit Herzschlag juenger als "
                f"{DAEMON_HEARTBEAT_MAX_AGE_SECONDS:g} s",
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

    @overload
    def ocr_start(self, data: bytes) -> Submitted: ...

    @overload
    def ocr_start(self, data: Sequence[bytes]) -> list[Submitted]: ...

    def ocr_start(self, data: bytes | Sequence[bytes]) -> Submitted | list[Submitted]:
        """Schickt Dokumente ab: Bytes, eines oder viele. Blockiert nicht.

        Je Dokument kommt dessen Hash zurueck, der Griff, mit dem es in jedem
        Prozess weitergeht (`wait`, `get`, `document`), auch Tage spaeter. Der
        Hash ist ein `Submitted`: eine Zeichenkette, die zusaetzlich sagt, ob
        dieser Aufruf das Dokument eingereiht hat und in welchem Zustand es steht.

        Der Ablauf:

        1. Erst werden ALLE uebergebenen Dokumente geprueft, dann wird eingereiht;
           sonst waeren bei einem abgewiesenen Element die vorderen schon
           abgeschickt, und der Aufrufer bekaeme eine Ausnahme statt ihrer Hashes.
           Abgewiesen wird, woraus nichts Neues entstehen kann („nur abschicken
           wenn etwas neues entstehen soll"): ein Dateityp, den das Paket nicht
           liest, und ein Ganzdokument-Format oder Bild ueber der Groessengrenze
           einer Anfrage (`max_payload_bytes`). Eine einzelne PDF-Seite ueber der
           Grenze findet erst das Schneiden im Job.
        2. Gibt es ein Ergebnis, ist das ein Treffer, AUCH BEI `failed`, und es
           geschieht nichts: Ein gescheitertes Dokument liest nur `retry` neu. Ein
           Treffer gilt unabhaengig von verlangten Lese-Einstellungen.
        3. Sonst wird die Eingabe abgelegt (`INSERT INTO inputs … ON CONFLICT DO
           NOTHING`). Steht sie schon, ist das Dokument unterwegs, und es geschieht
           nichts weiter; schicken zwei Aufrufer gleichzeitig dieselben Bytes,
           laesst Postgres den zweiten auf den ersten warten, und danach tut der
           zweite nichts. Kam sie neu hinein, wird in derselben Transaktion der Job
           eingereiht. Eine Ergebniszeile legt erst der Job an, wenn er endet.
           Jedes Dokument in seiner eigenen Transaktion und nie mehrere in einer:
           Zwei Aufrufer mit ueberlappenden Mengen in verschiedener Reihenfolge
           wuerden sich sonst gegenseitig blockieren. Das Signal, das die Worker
           weckt, geht erst beim Commit hinaus; kein Worker sieht den Job vor seiner
           Eingabe.

        Laeuft kein Daemon, wird trotzdem eingereiht, und der Client warnt (auf dem
        Logger `mineru_ocr.client`, Stufe WARNING; einmal je daemonloser Phase und
        nicht je Aufruf, und nur, wenn nach dem Aufruf Dokumente offen sind,
        eingereiht oder schon unterwegs): Offene Dokumente bleiben dann liegen,
        bis einer startet. Abgebrochen wird
        deshalb nichts — „das warten ist kuenstlich man wartet auf die datenbank
        nicht direkt auf den worker der ist ja unabhaengig". Ein Daemon gilt als
        laufend, wenn ein Worker einen Herzschlag hat, der juenger ist als
        `config.DAEMON_HEARTBEAT_MAX_AGE_SECONDS`, gemessen an der Uhr der
        Datenbank; die stehengebliebene Zeile eines abgestuerzten Daemons zaehlt
        nicht. Den Daemon startet der Betreiber, weder Bibliothek noch
        Kommandozeile tun es. Ist die Lebendigkeit gar nicht lesbar, steht auch
        das in der Warnung, und eingereiht ist trotzdem.

        Args:
            data: Die vollstaendigen Bytes EINES Dokuments, oder eine Folge davon.
                Keine Pfade und kein Text; das Lesen von Dateien ist Sache der
                Kommandozeile.

        Returns:
            Bei Bytes ein `Submitted`, bei einer Folge eine Liste in der
            Reihenfolge der Eingabe. Dasselbe Dokument zweimal in einer Folge wird
            einmal eingereiht; beide Eintraege tragen denselben Hash.

        Raises:
            TypeError: Ein Element ist keine Bytes — meist ein Pfad oder Text.
            UnsupportedTypeError: Ein Dokument hat einen Typ, den das Paket nicht
                liest. Es ist dann NICHTS eingereiht worden.
            TooLargeError: Ein Dokument ist zu gross. Dito.

        Weder Endpoint noch RunPod-Schluessel braucht das Abschicken: Es spricht
        RunPod nie an, und den Endpoint traegt erst die Ergebniszeile, die der Job
        anlegt.

        Kosten: keine unmittelbar. Mittelbar: alles, was der Daemon danach tut.
        """
        einzeln = isinstance(data, bytes)
        if isinstance(data, bytes):
            dokumente: list[bytes] = [data]
        elif isinstance(data, (str, bytearray, memoryview)) or not isinstance(
            data, Iterable
        ):
            # Eine Zeichenkette, ein Puffer oder ein Pfad ist KEINE Folge von
            # Dokumenten; `list(...)` machte aus einem `bytearray` eine Liste
            # von Zahlen und aus einem Pfad eine unverstaendliche Meldung.
            raise TypeError(_nimmt_bytes(data))
        else:
            dokumente = list(data)
        for dokument in dokumente:
            if not isinstance(dokument, bytes):
                raise TypeError(_nimmt_bytes(dokument))
        # ERST ALLES PRUEFEN, DANN EINREIHEN.
        vorbereitet = [core.prepare(dokument, self._settings) for dokument in dokumente]

        ergebnisse = [
            self._abschicken(dokument, bereit)
            for dokument, bereit in zip(dokumente, vorbereitet, strict=True)
        ]
        if any(e.status is None for e in ergebnisse):
            # Nur wenn nach dem Aufruf Dokumente offen sind, eingereiht oder schon
            # unterwegs: Die Warnung sagt, dass sie liegen bleiben, und bei lauter
            # fertigen waere das falsch. Ein schon unterwegs liegendes ist dieselbe
            # Lage, nur ohne Einreihen — „nicht eingereiht“ ohne Hinweis saehe wie
            # ein Haenger aus.
            self._ohne_daemon_warnen()
        if einzeln:
            return ergebnisse[0]
        return ergebnisse

    def _lebenszeichen(self) -> list[tuple[int, datetime]]:
        """Die Worker mit frischem Herzschlag (`db.worker_heartbeats`).

        Jeder Weg, der die Lebendigkeit liest — Abschicken, `status`,
        `preflight` —, geht hierher, und sieht er einen Daemon, endet die
        daemonlose Phase: Ein Daemon, der nur ueber `status` gesehen wurde und
        dann ausfaellt, ist fuer das naechste Abschicken eine NEUE Phase, und die
        wird gesagt.
        """
        lebende = db.worker_heartbeats(self.conn)
        if lebende:
            self._daemon_warnung = None
        return lebende

    def _ohne_daemon_warnen(self) -> None:
        """Warnt, wenn Dokumente offen sind und kein Daemon laeuft — einmal je Lage.

        Einmal, weil die Kommandozeile Dokument fuer Dokument einreiht und
        dreissigtausend gleiche Zeilen Laerm waeren. Eine Lage ist die daemonlose
        Phase oder die nicht lesbare Lebendigkeit; wechselt sie, wird erneut
        gewarnt, und ein gesehener Daemon beendet sie. Nur Auskunft: Ist die
        Lebendigkeit nicht lesbar (Procrastinates undokumentierte Worker-Tabelle
        fehlt oder sieht anders aus, die Verbindung ist weggebrochen), steht das
        samt Ursache in der Warnung, und eingereiht ist trotzdem — das Abschicken
        haengt nicht an einer Auskunft: Der Aufrufer bekommt seinen Hash und keine
        Ausnahme fuer eine Warnung.
        """
        try:
            if self._lebenszeichen():
                return
            lage = "kein_daemon"
            grund = (
                "Es laeuft kein Daemon (kein Worker-Herzschlag juenger als "
                f"{DAEMON_HEARTBEAT_MAX_AGE_SECONDS:g} s)"
            )
        except (DatabaseError, psycopg.Error) as fehler:
            # `db.worker_heartbeats` uebersetzt psycopgs Fehler in `DatabaseError`;
            # psycopg.Error steht dabei, damit auch ein Fehler, der an dieser
            # Uebersetzung vorbeigeht, das Abschicken nicht scheitern laesst.
            lage = "nicht_lesbar"
            ursache = (str(fehler.__cause__ or fehler).splitlines() or [""])[0]
            grund = f"Ob ein Daemon laeuft, ist nicht pruefbar ({ursache})"
        if lage == self._daemon_warnung:
            return
        self._daemon_warnung = lage
        log.warning(
            "%s. Offene Dokumente bleiben liegen, bis einer startet: "
            "`mineru-ocr daemon`.",
            grund,
        )

    def _abschicken(self, data: bytes, bereit: core.Prepared) -> Submitted:
        """Schickt EIN geprueftes Dokument ab, in seiner eigenen Transaktion.

        Ein Treffer (Ergebnis da) wird ohne `raw` gelesen und nicht angefasst.
        Sonst entstehen Eingabe und Job zusammen oder gar nicht: Eine Eingabe ohne
        Job hiesse „unterwegs", und niemand wuerde sie je wieder anfassen.
        """
        sha256 = bereit.sha256
        vorhanden = db.get_document(self.conn, sha256, with_raw=False)
        if vorhanden is not None:
            return Submitted(sha256, queued=False, status=vorhanden.status)
        try:
            with self.conn.transaction():
                if not db.insert_input(self.conn, sha256=sha256, content=data):
                    # Die Eingabe steht schon: Das Dokument ist unterwegs (oder ein
                    # anderer Aufrufer war schneller, und Postgres hat uns auf ihn
                    # warten lassen). Es bleibt bei seiner Eingabe und seinem Job.
                    raise _SchonUnterwegs
                # EINZELN und in dieser Transaktion: Das Signal an die Worker geht
                # erst beim Commit hinaus.
                if not tasks.enqueue_document(self._procrastinate, self.conn, sha256):
                    raise _SchonUnterwegs
        except _SchonUnterwegs:
            # Steht inzwischen ein Ergebnis da (der Job des anderen endete eben),
            # ist das der Zustand; sonst ist das Dokument offen.
            jetzt = db.get_document(self.conn, sha256, with_raw=False)
            return Submitted(
                sha256, queued=False, status=jetzt.status if jetzt is not None else None
            )
        return Submitted(sha256, queued=True, status=None)

    def wait(self, sha256: str, timeout: float | None = None) -> OCRWrapper:
        """Wartet auf das Endergebnis EINES Dokuments.

        Lauscht auf dem Kanal `doc_done` und fragt zur Sicherheit alle fuenf
        Sekunden nach; die erste Abfrage kommt VOR dem Lauschen, damit ein
        bereits fertiges Dokument nicht auf ein Signal wartet, das schon vorbei
        ist. Das Signal ist Eigenbau — Procrastinate liefert kein „Job
        fertig"-Signal an aussenstehende Warter (Feature-Verifikation Punkt 10).

        Gelesen werden Ergebnis und Eingabe in einer Anweisung
        (`db.read_states`), ohne `raw`: Ergebnis da heisst fertig, Eingabe da
        heisst offen, keins von beiden unbekannt. Die Huelle laedt `raw` erst,
        wenn jemand das Ergebnis will. Procrastinate wird nicht gefragt.

        Args:
            sha256: Der Hash des Dokuments, wie `ocr_start` ihn gab.
            timeout: Sekunden, None fuer unbegrenzt.

        Returns:
            Die Huelle — bei JEDEM Endzustand. Der Aufrufer prueft `.success`.
            Diese Methode wirft nicht, weil ein Dokument scheiterte; das Scheitern
            ist ein Ergebnis.

        Raises:
            DocumentNotFound: Weder Ergebnis noch Eingabe — sofort, nicht nach
                Ablauf.
            TimeoutError: Die Zeit lief ab. Sagt nichts ueber das Dokument, nur
                ueber die Geduld.

        Kosten: keine.
        """
        _hash_pruefen(sha256)
        zeile = db.wait_for_terminal(
            self.conn,
            sha256,
            timeout=timeout,
            poll_seconds=self._settings.wait_poll_seconds,
        )
        return OCRWrapper.from_row(zeile, self.conn)

    def ocr(self, data: bytes, timeout: float | None = None) -> OCRWrapper:
        """Ein Dokument: abschicken und auf dieses eine warten.

        Gibt es schon ein Ergebnis, ist das Abschicken ein Nichts-Tun und das
        Warten kehrt sofort zurueck — Jurgens Regel: „wenn ocrwrapper bereits OCR'd
        ist dann ist es eine noop" (Plan 2.1).

        Args:
            data: Die vollstaendigen Bytes.
            timeout: Sekunden fuer das Warten, None fuer unbegrenzt.

        Returns:
            Die Huelle zum Endzustand, auch bei `failed`.

        Raises:
            TimeoutError: Die Zeit lief ab. Das Dokument bleibt unterwegs.

        Kosten: JA, mittelbar.
        """
        return self.wait(self.ocr_start(data), timeout=timeout)

    def retry_failed(self, target: OCRWrapper | Sequence[OCRWrapper]) -> int:
        """Legt gescheiterte Dokumente erneut vor. Duenner Durchgriff, kein Eigenbau.

        Benutzt Procrastinates eigenes Werkzeug (`retry_job_by_id`) auf den
        juengsten Job jedes gescheiterten Dokuments — „sie werden ja einfach von
        procrastinate dann zurückgesetzt das hat ja tooling dafür" (Plan 2.4).

        Erfasst werden AUSSCHLIESSLICH Dokumente im Zustand `failed`. Nicht
        `input_error`: Das Nachsehen des Executors deckt den ab, ein so
        wiedervorgelegter Job taete also nichts — verbrauchte aber einen Versuch
        aus dem Budget und wuerde im Rueckgabewert als erledigte Arbeit gezaehlt,
        die nie stattfindet. Ein zu grosses DOCX liesse sich damit scheinbar
        wiederholen, bliebe `input_error`, und beim naechsten echten Bedarf waere
        das Budget aufgebraucht. (Der Zustand `failed` und `input_error` zusammen
        waere „nicht erfolgreich" — hier wird ausdruecklich enger gefiltert.)

        Zwei Eigenheiten des Werkzeugs, die man kennen muss (beide am Objekt der
        von 3.9.0 belegt und fuer 3.10.0 unveraendert, Feature-Verifikation Punkt 19):

        Es nimmt nur Jobs in `doing` oder `failed`. Ein `aborted`-Job — etwa vom
        Herunterfahren — wird ABGEWIESEN. Sein Heilungsweg ist nicht diese Methode
        (`ocr_start` findet die stehende Eingabe und tut nichts; den Rueckweg
        bringt das Ticket #26); deshalb ueberspringt sie solche Dokumente und
        zaehlt sie im Rueckgabewert nicht mit, statt sie mit einer Ausnahme
        aufzuhalten.

        Und jede Wiedervorlage erhoeht `attempts`. Ein Retry von Hand auf einen
        erschoepften Job ist genau EIN weiterer Versuch, keine neue Dreier-Runde.

        Fuer Einzelfaelle gibt es ausserdem das fertige Handwerkzeug
        `procrastinate shell` mit `retry`, `cancel` und `list_jobs`. Diese Methode
        ist Bequemlichkeit fuer den Stapel, kein Ersatz dafuer.

        Args:
            target: Eine Huelle oder mehrere.

        Returns:
            Wie viele Jobs tatsaechlich wieder vorgelegt wurden.

        Zusicherung:
            Der wieder vorgelegte Job verarbeitet das Dokument WIRKLICH neu und
            ueberschreibt sein `raw`. Das ist kein Nebeneffekt, sondern der Zweck
            — und der Grund, warum das Nachsehen des Executors `failed` nicht deckt.

        Kosten: JA, mittelbar — jedes wieder vorgelegte Dokument wird erneut
        verarbeitet und erneut bezahlt.
        """
        huellen = [target] if isinstance(target, OCRWrapper) else list(target)
        # AUSSCHLIESSLICH `failed`, nicht `input_error`: Das Nachsehen des
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
                # Heilungsweg ist nicht diese Methode (den Rueckweg
                # bringt #26; `ocr_start` findet die Eingabe) —
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

        Endzustaende werden aus `documents` gezaehlt, offene Dokumente (Eingabe
        da, kein Ergebnis) aus `inputs`; wie viele Jobs warten oder laufen, kommt
        aus Procrastinates Job-Tabellen. Nichts davon wird gespiegelt — das ist
        der ganze Sinn von „ein Buch, nicht zwei" (Plan 2.4). Einen Zustand `new`
        gibt es nicht.

        AUSSCHLIESSLICH in Summen, nie Dokument fuer Dokument. Das ist keine
        Bequemlichkeit, sondern eine Groessenfrage: Der Bestand hat
        zehntausend und mehr Dokumente, und der abgeleitete Zustand je Dokument
        braucht dessen juengsten Job. Es gibt in Procrastinates Lese-Schnittstelle
        keinen Filter „der juengste Job je `queueing_lock`" — wer das je Dokument
        aufloest, macht zehntausend Abfragen fuer eine Uebersicht, die niemand
        Dokument fuer Dokument liest.

        Gezaehlt wird deshalb so: Endzustaende mit einem `GROUP BY status`
        ueber `documents` (`db.counts_by_status`), die offenen mit einer Zaehlung
        ueber `inputs` ohne Ergebnis (`db.count_open`), Job-Zustaende mit
        `list_tasks`, das fertige Zaehler je Zustand liefert (`todo`, `doing`,
        `succeeded`, `failed`, `cancelled`, `aborted`). Drei Abfragen,
        unabhaengig von der Bestandsgroesse.

        Die Aufloesung je Dokument gibt es nur fuer ein BENANNTES Dokument —
        `document()` und `get()`. Das ist die richtige Stelle dafuer: Wer ein
        einzelnes Dokument sucht, nennt es.

        Returns:
            Zaehler je Endzustand und `open` fuer die offenen Dokumente (unter
            `documents`), Zaehler je Job-Zustand, die Zahl offener
            Aufruest-Faelle, der Kontostand und ob ein Daemon lebt. Beide
            letzten sind nur Auskunft: Sind sie nicht lesbar (RunPod antwortet
            nicht, Procrastinates Worker-Tabelle fehlt), stehen sie als `None`
            da, und die Uebersicht scheitert nicht.

        Raises:
            ConfigurationError: Kein RunPod-Schluessel. Die Uebersicht fragt
                RunPod nach dem Kontostand; ein fehlender Schluessel ist keine
                Auskunft „unbekannt", sondern eine Frage der Konfiguration.

        Kosten: keine.
        """
        self._settings.require_api_key()
        zeilen = db.counts_by_status(self.conn)
        offen = db.count_open(self.conn)

        aufgaben = self._procrastinate.job_manager.list_tasks()

        # Unabhaengig von der Bestandsgroesse: ein `GROUP BY status` ueber
        # `documents`, eine Zaehlung ueber `inputs` und `list_tasks`, das fertige
        # Zaehler je Zustand liefert. Die Aufloesung je Dokument gibt es nur fuer ein
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

        try:
            daemon_lebt: bool | None = bool(self._lebenszeichen())
        except (DatabaseError, psycopg.Error):
            # Wie der Kontostand: Nicht lesbar ist nicht dasselbe wie „laeuft
            # nicht“, und die Uebersicht bricht an einer Auskunft nicht ab. Derselbe
            # Fang wie beim Abschicken (`_ohne_daemon_warnen`).
            daemon_lebt = None

        return {
            "documents": {
                **{zustand.value: anzahl for zustand, anzahl in zeilen.items()},
                "open": offen,
            },
            "jobs": jobs,
            "upgrade_cases": offene_aufruest_faelle[0] if offene_aufruest_faelle else 0,
            "balance": stand,
            "daemon_alive": daemon_lebt,
        }

    def document(self, sha256: str) -> DerivedStatus:
        """Wo EIN Dokument steht, ueber seinen Hash, ohne `raw` zu laden.

        Gibt es ein Ergebnis, ist es sein Endzustand (`Phase.TERMINAL`, die Zeile
        ohne `raw` unter `.row`). Sonst ist es offen, und die Phase sagt, was sein
        juengster Job tut: wartet, laeuft, oder ist ohne Ergebnis zu Ende (dann
        hilft `retry`). Das ist die eine Stelle, an der man ein steckendes
        Dokument von einem unterscheidet, das noch dauert; das Warten fragt
        Procrastinate nicht.

        Ergebnis und Eingabe kommen aus einer Anweisung (`db.read_states`), der
        Job ueber `list_jobs(queueing_lock=…)`.

        Raises:
            DocumentNotFound: Unbekannt: weder Ergebnis noch Eingabe.

        Kosten: keine.
        """
        stand = self._stand(sha256)
        jobs = self._procrastinate.job_manager.list_jobs(queueing_lock=sha256)
        return db.derive_status(sha256, stand.row, jobs)

    def get(self, sha256: str) -> OCRWrapper:
        """Holt die Huelle zu einem bereits bekannten Dokument, ohne zu warten.

        Der Weg von einem Hash zu einem Ergebnis. `document()` gibt den Zustand,
        diese Methode gibt die Sache selbst — Text, Strukturliste, Bilder,
        Rohbestand. Die Huelle laedt `raw` erst, wenn jemand es braucht, ueber die
        Verbindung dieses Clients.

        Args:
            sha256: Der Hash des Dokuments, wie `ocr_start` ihn gab. Pfade kennt die
                Bibliothek nicht.

        Raises:
            DocumentNotFound: Es gibt (noch) kein Ergebnis: Das Dokument ist offen
                oder unbekannt, und die Meldung sagt, welches von beiden. Ob ein
                Ergebnis gelungen ist, sagt die Huelle (`.status`, `.success`) —
                nicht diese Methode.

        Kosten: keine.
        """
        stand = self._stand(sha256)
        if stand.row is None:
            raise DocumentNotFound(
                f"{sha256} hat noch kein Ergebnis: Es ist abgeschickt und offen. "
                f"Wo es steht, sagt `document()` (`mineru-ocr show {sha256}`); "
                f"darauf warten `wait()` (`mineru-ocr wait {sha256}`)."
            )
        return OCRWrapper.from_row(stand.row, self.conn)

    def _stand(self, sha256: str) -> db.DocumentState:
        """Ergebnis und Eingabe zu einem Hash, ohne `raw` — oder `DocumentNotFound`,
        wenn es keins von beiden gibt."""
        _hash_pruefen(sha256)
        (stand,) = db.read_states(self.conn, [sha256])
        if stand.is_unknown:
            raise DocumentNotFound(
                f"Kein Dokument zu {sha256!r}: weder Ergebnis noch Eingabe. "
                f"Angenommen wird der sha256, den `ocr_start` zurueckgab "
                f"(vierundsechzig Zeichen aus 0-9a-f)."
            )
        return stand

    def credit(self) -> float:
        """Der Kontostand, live geholt.

        Bewusst kein zwischengespeicherter Wert: „dynmiasche attribute wie
        runpodcredit […] die per getter direkt von der api gegettet werden wenn
        man darauf zugreift" (Plan 2.1). Ein zwischengespeicherter Kontostand ist
        genau dann falsch, wenn es darauf ankommt.

        Raises:
            ConfigurationError: Kein RunPod-Schluessel.
        """
        self._settings.require_api_key()
        return _runpod_fragen(self._settings, lambda z: z.balance())

    def account(self) -> dict[str, Any]:
        """Die Kontoangaben als Ganzes. Verlangt den RunPod-Schluessel."""
        self._settings.require_api_key()
        return _runpod_fragen(self._settings, lambda z: z.account())

    def workers(self) -> list[dict[str, Any]]:
        """Die Worker EINZELN aufgelistet — die zusammengefassten Zaehler luegen
        (Plan 8, Regel 11). Leere Liste heisst „gerade laeuft keiner" und ist bei
        einem auf null herunterskalierenden Endpoint der Normalfall. Verlangt
        Endpoint und RunPod-Schluessel."""
        self._settings.require_endpoint_id()
        self._settings.require_api_key()
        return _runpod_fragen(self._settings, lambda z: z.workers())
