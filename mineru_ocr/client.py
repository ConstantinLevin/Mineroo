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
import contextlib
import logging
from collections.abc import (
    AsyncGenerator,
    Awaitable,
    Callable,
    Coroutine,
    Generator,
    Iterable,
    Sequence,
)
from datetime import datetime
from types import TracebackType
from typing import Any, Final, TypeVar, overload

import procrastinate
import procrastinate.exceptions
import psycopg
from procrastinate import utils
from procrastinate.jobs import Job
from psycopg_pool import AsyncNullConnectionPool, NullConnectionPool

from . import core, db, tasks
from .config import DAEMON_HEARTBEAT_MAX_AGE_SECONDS, MASKE, Settings, ist_geheimnisname
from .core import RetryOutcome, Submitted
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


def _dokumente(data: object) -> list[bytes]:
    """Was `ocr_start` bekam, als Liste von Dokumenten — fuer beide Clients.

    Weist alles ab, was keine Bytes und keine Folge von Bytes ist.
    """
    if isinstance(data, bytes):
        return [data]
    if isinstance(data, (str, bytearray, memoryview)) or not isinstance(
        data, Iterable
    ):
        # Eine Zeichenkette, ein Puffer oder ein Pfad ist KEINE Folge von
        # Dokumenten; `list(...)` machte aus einem `bytearray` eine Liste
        # von Zahlen und aus einem Pfad eine unverstaendliche Meldung.
        raise TypeError(_nimmt_bytes(data))
    dokumente = list(data)
    for dokument in dokumente:
        if not isinstance(dokument, bytes):
            raise TypeError(_nimmt_bytes(dokument))
    return dokumente


def _daemon_warnung_geben(bisher: str | None, fehler: BaseException | None) -> str:
    """Warnt, dass kein Daemon laeuft oder das nicht lesbar ist — einmal je Lage.

    Die Entscheidung beider Clients, nachdem sie keinen frischen Herzschlag
    gesehen haben (`fehler` None) oder ihn nicht lesen konnten. Eine Lage ist die
    daemonlose Phase oder die nicht lesbare Lebendigkeit; nur eine neue Lage wird
    gesagt.

    Returns:
        Die Lage, die der Client sich bis zum naechsten Mal merkt.
    """
    if fehler is None:
        lage = "kein_daemon"
        grund = (
            "Es laeuft kein Daemon (kein Worker-Herzschlag juenger als "
            f"{DAEMON_HEARTBEAT_MAX_AGE_SECONDS:g} s)"
        )
    else:
        lage = "nicht_lesbar"
        ursache = (str(fehler.__cause__ or fehler).splitlines() or [""])[0]
        grund = f"Ob ein Daemon laeuft, ist nicht pruefbar ({ursache})"
    if lage != bisher:
        log.warning(
            "%s. Offene Dokumente bleiben liegen, bis einer startet: "
            "`mineru-ocr daemon`.",
            grund,
        )
    return lage


def _gefragte_hashes(hashes: object, timeout: float | None) -> list[str]:
    """Die gefragten Hashes als Liste, fuer das Warten beider Clients; weist einen
    einzelnen Hash, Pfade und eine unbrauchbare Frist ab."""
    db.check_wait_timeout(timeout)
    if isinstance(hashes, (str, bytes)) or not isinstance(hashes, Iterable):
        raise TypeError(
            "Erwartet wird eine Folge von Hashes, nicht "
            f"{type(hashes).__name__}. Fuer EIN Dokument gibt es `wait(sha256)`."
        )
    gefragt = list(hashes)
    for sha256 in gefragt:
        _hash_pruefen(sha256)
    return gefragt


def _unbekannt(sha256: str) -> DocumentNotFound:
    """Der Fehler fuer einen Hash ohne Ergebnis und ohne Eingabe."""
    return DocumentNotFound(
        f"Kein Dokument zu {sha256!r}: weder Ergebnis noch Eingabe. "
        f"Angenommen wird der sha256, den `ocr_start` zurueckgab "
        f"(vierundsechzig Zeichen aus 0-9a-f)."
    )


def _noch_offen(sha256: str) -> DocumentNotFound:
    """Der Fehler von `get` fuer ein Dokument, das abgeschickt und offen ist."""
    return DocumentNotFound(
        f"{sha256} hat noch kein Ergebnis: Es ist abgeschickt und offen. "
        f"Wo es steht, sagt `document()` (`mineru-ocr show {sha256}`); "
        f"darauf warten `wait()` (`mineru-ocr wait {sha256}`)."
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
    dem Pool. Je Agent bleiben so ein bis zwei Verbindungen: die des Clients und,
    solange er wartet, die eigene Verbindung der Warteschleife (`wait`, `wait_all`,
    `as_completed`; je Aufruf eine, nie aus dem Pool, denn ein Pool hebt kein
    `LISTEN` auf).

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
        dokumente = _dokumente(data)
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
            fehler = None
        except (DatabaseError, psycopg.Error) as gefangen:
            # `db.worker_heartbeats` uebersetzt psycopgs Fehler in `DatabaseError`;
            # psycopg.Error steht dabei, damit auch ein Fehler, der an dieser
            # Uebersetzung vorbeigeht, das Abschicken nicht scheitern laesst.
            fehler = gefangen
        self._daemon_warnung = _daemon_warnung_geben(self._daemon_warnung, fehler)

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
                # erst beim Commit hinaus. Die Kennung kommt an die Eingabe, ueber
                # sie finden `document()` und `retry` den Job.
                kennung = tasks.enqueue_document(self._procrastinate, self.conn, sha256)
                db.set_input_job(self.conn, sha256=sha256, job_id=kennung)
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

        Fuer viele gibt es `wait_all` und `as_completed`; ein einziges `wait`, das
        beides naehme, waere unter `mypy --strict` mehrdeutig, denn ein Hash ist
        selbst eine Folge von Zeichen. Gewartet wird wie dort (`db.wait_for_terminal`):
        auf einer eigenen Verbindung, erst lauschen auf `doc_done`, dann lesen,
        damit ein bereits fertiges Dokument nicht auf ein Signal wartet, das schon
        vorbei ist; im Takt (`settings.wait_poll_seconds`) liest es ohnehin nach.
        Das Signal ist Eigenbau — Procrastinate liefert kein „Job fertig"-Signal
        an aussenstehende Warter (Feature-Verifikation Punkt 10).

        Gelesen werden Ergebnis und Eingabe in einer Anweisung, ohne `raw`:
        Ergebnis da heisst fertig, Eingabe da heisst offen, keins von beiden
        unbekannt. Die Huelle laedt `raw` erst, wenn jemand das Ergebnis will.
        Procrastinate wird nicht gefragt, und ob ein Daemon laeuft, aendert am
        Warten nichts; ohne Daemon endet es erst mit der Frist.

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
            ValueError: Die Frist ist `nan` oder negativ.
            WaitTimeoutError: Die Zeit lief ab (ein `TimeoutError`); er nennt den
                Hash. Sagt nichts ueber das Dokument, nur ueber die Geduld.

        Frist und Strg-C beenden nur das Warten; kein Job wird angefasst.

        Kosten: keine.
        """
        _hash_pruefen(sha256)
        (huelle,) = self.wait_all([sha256], timeout=timeout)
        return huelle

    def wait_all(
        self, hashes: Sequence[str], timeout: float | None = None
    ) -> list[OCRWrapper]:
        """Wartet auf ALLE Dokumente und liefert sie in der Reihenfolge der Frage.

        Wie `as_completed`, nur gesammelt: Ein doppelt gefragter Hash steht
        zweimal in der Liste. Ein gescheitertes Dokument beendet das Warten auf
        die uebrigen nicht.

        Args:
            hashes: Die Hashes, wie `ocr_start` sie gab. Eine Folge, kein
                einzelner Hash — fuer einen gibt es `wait`.
            timeout: Sekunden fuer den GANZEN Aufruf, None fuer unbegrenzt.

        Returns:
            Je gefragtem Hash die Huelle, bei JEDEM Endzustand.

        Raises:
            TypeError: `hashes` ist ein einzelner Hash, oder ein Element ist kein
                Hash (etwa ein `Path`).
            ValueError: Die Frist ist `nan` oder negativ.
            DocumentNotFound: Ein Hash ist unbekannt — sofort.
            WaitTimeoutError: Die Frist lief ab; `pending` nennt die offenen.

        Kosten: keine.
        """
        gefragt = self._hashes_pruefen(hashes, timeout)
        fertig = {huelle.sha256: huelle for huelle in self._fertige(gefragt, timeout)}
        return [fertig[sha256] for sha256 in gefragt]

    def as_completed(
        self, hashes: Sequence[str], timeout: float | None = None
    ) -> Generator[OCRWrapper]:
        """Liefert jedes Dokument, sobald es endgueltig ist — in der Reihenfolge,
        in der sie fertig werden; bereits fertige sofort.

        Jeder Endzustand ist ein Ergebnis, auch `failed` und `input_error`, und
        ein gescheitertes beendet das Warten auf die uebrigen nicht. Gewartet wird
        auf EINER eigenen Verbindung fuer den ganzen Aufruf; sie schliesst, wenn
        der Iterator erschoepft ist, mit einem Fehler endet oder geschlossen wird
        (`close()`, oder `contextlib.closing` um den Iterator, wer vorher aufhoert).

        Args:
            hashes: Die Hashes, wie `ocr_start` sie gab. Eine Folge, kein
                einzelner Hash — fuer einen gibt es `wait`. Doppelte kommen einmal.
            timeout: Sekunden fuer den GANZEN Aufruf, None fuer unbegrenzt.

        Yields:
            Je Dokument einmal die Huelle.

        Raises:
            TypeError: Sofort beim Aufruf, wie bei `wait_all`.
            ValueError: Sofort beim Aufruf: Die Frist ist `nan` oder negativ.
            DocumentNotFound: Beim ersten Weiterschalten, wenn ein Hash unbekannt
                ist — bevor irgendein Dokument geliefert wird.
            WaitTimeoutError: Die Frist lief ab; `pending` nennt die offenen.

        Kosten: keine.
        """
        return self._fertige(self._hashes_pruefen(hashes, timeout), timeout)

    def _hashes_pruefen(
        self, hashes: Sequence[str], timeout: float | None
    ) -> list[str]:
        """Die gefragten Hashes als Liste; weist einen einzelnen Hash, Pfade und
        eine unbrauchbare Frist ab, und prueft, dass der Client betreten ist —
        alles beim Aufruf, auch fuer den Iterator von `as_completed`."""
        gefragt = _gefragte_hashes(hashes, timeout)
        self.conn  # Nicht betreten: der Fehler jetzt, nicht beim ersten Ergebnis.
        return gefragt

    def _fertige(
        self, hashes: list[str], timeout: float | None
    ) -> Generator[OCRWrapper]:
        """Die Warteschleife (`db.wait_for_terminal`) mit Huellen statt Zeilen.

        Die Huellen halten die Verbindung des Clients, um `raw` bei Bedarf
        nachzuladen; gewartet wird auf der eigenen Verbindung der Schleife, die
        mit diesem Generator schliesst.
        """
        with contextlib.closing(
            db.wait_for_terminal(
                self._settings.db_dsn,
                hashes,
                timeout=timeout,
                poll_seconds=self._settings.wait_poll_seconds,
            )
        ) as zeilen:
            for zeile in zeilen:
                yield OCRWrapper.from_row(zeile, self.conn)

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
            ValueError: Die Frist ist `nan` oder negativ. Gemeldet, BEVOR
                eingereiht wird: Sonst liefe ein bezahlter Job fuer einen Aufruf,
                der mit einem Fehler endete.
            WaitTimeoutError: Die Zeit lief ab (ein `TimeoutError`); er nennt den
                Hash. Das Dokument bleibt unterwegs — ohne laufenden Daemon wird
                es erst fertig, wenn einer startet.

        Kosten: JA, mittelbar.
        """
        db.check_wait_timeout(timeout)
        return self.wait(self.ocr_start(data), timeout=timeout)

    def retry(self, sha256: str) -> RetryOutcome:
        """Bringt EIN Dokument ohne gueltiges Ergebnis wieder in Gang, auf
        Procrastinates eigenem Weg.

        Der Ablauf:

        1. Steht ein Ergebnis `done`, `empty` oder `input_error` da, geschieht
           nichts (`HAS_RESULT`).
        2. Sonst wird der Job ueber die Kennung an der Eingabe gelesen
           (`inputs.job_id`, `list_jobs(id=…)`):

           * `todo` oder `doing`: nichts, der Job ist unterwegs (`UNDERWAY`). Ein
             laufender Job wird nicht zurueckgesetzt, und zweimal `retry` laesst
             ein Dokument nicht doppelt laufen. Unterwegs heisst, was `document()`
             als wartend oder laufend zeigt (`db.derive_status`, dieselbe Regel):
             auch ein Job-Zustand, den das Paket nicht kennt.
           * `failed`: Erst geht die `failed`-Zeile weg (eigene Transaktion), dann
             setzt `retry_job_by_id` DENSELBEN Job zurueck (`RESET`). So
             dokumentiert und benutzt Procrastinate es selbst („Retry a Job
             Manually", Shell `retry`) und vermerkt es als Ereignis `retried`. Der
             Job behaelt seinen Versuchszaehler, und das Zuruecksetzen zaehlt
             selbst einen Versuch dazu.
           * `aborted`, `cancelled`, `failed` mit stehendem `abort_requested` (ein
             Abbruch ueber die API; das Zuruecksetzen scheiterte dort am CHECK
             `check_not_todo_abort_requested`) oder kein Job: Procrastinate legt
             sie nicht wieder vor. In EINER Transaktion geht eine `failed`-Zeile
             weg, ein neuer Job entsteht, und seine Kennung kommt an die Eingabe
             (`REQUEUED`); er liest die Eingabe samt ihren Einstellungen.

           In beiden Faellen wird die Eingabe zuerst gesperrt: Ist sie inzwischen
           weg (der Job wurde zwischen Lesen und Handeln fertig, und das Ergebnis
           loeschte sie), hat das Dokument ein Ergebnis, und es geschieht nichts
           (`HAS_RESULT`).

        Warum die `failed`-Zeile weggeht: Dann ist „offen" wieder eine Tatsache
        der Datenbank (Eingabe da, kein Ergebnis), und jeder, der wartet, wartet
        auf den neuen Lauf, in jedem Prozess, auch `mineru-ocr wait` nach
        `mineru-ocr retry`. Ihr Rohergebnis ginge ohnehin verloren, denn der neue
        Lauf ueberschreibt es. Erst Wegnehmen, dann Zuruecksetzen ist Absicht:
        Andersherum koennte der zurueckgesetzte Job schon ein neues Ergebnis
        geschrieben haben, das das Wegnehmen loeschte. Stirbt `retry` zwischen
        beiden Schritten, steht das Dokument ohne Ergebnis mit `failed`-Job da, und
        `retry` noch einmal setzt ihn zurueck.

        Gleichzeitige `retry` auf dasselbe Dokument laufen nacheinander
        (`db.retry_lock`, eine Advisory-Sperre um Lesen und Handeln): Der zweite
        liest erst, wenn der erste fertig ist, sieht dessen Job und meldet
        unterwegs, statt einen zweiten einzureihen, der noch einmal bezahlt wuerde.

        Lese-Einstellungen nimmt `retry` keine: Es wiederholt einen Job. Ein Fehler
        von Procrastinate (etwa ein Job, den jemand anderes zwischen Lesen und
        Zuruecksetzen zuruecksetzte) geht als `DatabaseError` nach oben, statt als
        „nicht wiederholt" verbucht zu werden.

        Args:
            sha256: Der Hash des Dokuments, wie `ocr_start` ihn gab.

        Returns:
            Was geschehen ist (`RetryOutcome`).

        Raises:
            DocumentNotFound: Weder Ergebnis noch Eingabe.
            DatabaseError: Ein `failed`-Ergebnis ohne Eingabe; ein neuer Lauf
                haette nichts zu lesen, und es bleibt, wie es ist. Oder
                Procrastinate lehnte das Zuruecksetzen ab; die `failed`-Zeile ist
                dann schon weg, und ein neues `retry` liest den Stand.

        Kosten: JA, mittelbar, bei `RESET` und `REQUEUED` — das Dokument wird neu
        verarbeitet und neu bezahlt.
        """
        _hash_pruefen(sha256)
        with db.retry_lock(self.conn, sha256):
            return self._retry_unter_sperre(sha256)

    def _retry_unter_sperre(self, sha256: str) -> RetryOutcome:
        """`retry` fuer ein Dokument, dessen Sperre der Aufrufer haelt: Erst unter
        ihr wird der Stand gelesen, auf dem die Entscheidung beruht."""
        stand = self._stand(sha256)
        if stand.row is not None and stand.row.status is not DocumentStatus.FAILED:
            return RetryOutcome.HAS_RESULT
        if not stand.has_input:
            raise DatabaseError(
                f"{sha256} steht auf `failed`, aber seine Eingabe fehlt: Ein neuer "
                f"Lauf haette nichts zu lesen. Das Dokument neu abschicken geht "
                f"nicht, solange das Ergebnis steht; es bleibt, wie es ist."
            )
        job = self._job(stand)
        # Dieselbe Regel wie `document()`: Was es als wartend oder laufend zeigt,
        # ist unterwegs, auch ein Job-Zustand, den das Paket nicht kennt. Ohne
        # Ergebnis gelesen, weil nur noch `failed` oder keins da sein kann.
        if db.derive_status(sha256, None, job).phase in (
            db.Phase.WAITING,
            db.Phase.RUNNING,
        ):
            return RetryOutcome.UNDERWAY
        if (
            job is not None
            and job.id is not None
            and job.status == "failed"
            and not job.abort_requested
        ):
            with self.conn.transaction():
                if not db.lock_input(self.conn, sha256):
                    # Wie beim Neu-Einreihen: Die Eingabe ist weg, das Dokument
                    # bekam ein Ergebnis (etwa `input_error`, Job `failed`).
                    return RetryOutcome.HAS_RESULT
                db.delete_failed_result(self.conn, sha256)
            try:
                self._procrastinate.job_manager.retry_job_by_id(
                    job.id, utils.utcnow(), job.priority, job.queue, job.lock
                )
            except procrastinate.exceptions.ProcrastinateException as fehler:
                raise DatabaseError(
                    f"Procrastinate konnte Job {job.id} fuer {sha256} nicht "
                    f"zuruecksetzen ({fehler}), etwa weil ihn jemand anderes "
                    f"gleichzeitig zuruecksetzte oder die Verbindung abbrach. Die "
                    f"`failed`-Zeile ist schon weg; ein neues `retry` liest den "
                    f"Stand und tut, was noch fehlt."
                ) from fehler
            return RetryOutcome.RESET
        with self.conn.transaction():
            if not db.lock_input(self.conn, sha256):
                # Die Eingabe ist weg, seit `_stand` sie sah: Der Job wurde
                # fertig, und sein Ergebnis loeschte sie. Nichts einzureihen.
                return RetryOutcome.HAS_RESULT
            db.delete_failed_result(self.conn, sha256)
            kennung = tasks.enqueue_document(self._procrastinate, self.conn, sha256)
            db.set_input_job(self.conn, sha256=sha256, job_id=kennung)
        return RetryOutcome.REQUEUED

    def retry_all(
        self, hashes: Sequence[str] | None = None
    ) -> dict[str, RetryOutcome]:
        """Bringt viele Dokumente wieder in Gang: jedes wie `retry`.

        Ohne Angabe waehlt die Bibliothek selbst aus: jedes Dokument ohne
        gueltiges Ergebnis, also jede stehende Eingabe (`inputs`). Das sind die
        gescheiterten (`failed` laesst die Eingabe stehen) und die offenen, auch
        die, deren Job abgebrochen, abgesagt oder weg ist; wartende und laufende
        meldet `retry` als unterwegs und laesst sie, wie sie sind. Ein Dokument mit
        gueltigem Ergebnis hat keine Eingabe mehr und kommt nicht vor.

        Mit Angabe werden erst ALLE Hashes geprueft, dann wird wiederholt: Sonst
        waeren bei einem unbekannten Hash die vorderen schon neu vorgelegt, und der
        Aufrufer bekaeme eine Ausnahme statt ihrer Ausgaenge.

        Je Dokument eine Abfrage seines Jobs (`list_jobs(id=…)`); waehrend eines
        grossen Laufs mit vielen offenen Dokumenten sind das entsprechend viele.

        Args:
            hashes: Die Dokumente, oder None fuer jedes ohne gueltiges Ergebnis.

        Returns:
            Je Dokument, was geschehen ist, in der Reihenfolge der Angabe (ohne
            Angabe nach Hash geordnet). Ein doppelt genannter Hash steht einmal.

        Raises:
            DocumentNotFound: Ein genannter Hash ist unbekannt; dann ist NICHTS
                geschehen.
            DatabaseError: Wie bei `retry`; die Dokumente davor sind dann schon
                wiederholt.

        Kosten: JA, mittelbar — jedes zurueckgesetzte oder neu eingereihte
        Dokument wird neu verarbeitet und neu bezahlt.
        """
        if hashes is None:
            ziele = db.input_hashes(self.conn)
        else:
            ziele = list(dict.fromkeys(hashes))
            for sha256 in ziele:
                _hash_pruefen(sha256)
            for stand in db.read_states(self.conn, ziele):
                if stand.is_unknown:
                    raise DocumentNotFound(
                        f"Kein Dokument zu {stand.sha256!r}: weder Ergebnis noch "
                        f"Eingabe. Es ist nichts wiederholt worden."
                    )
        return {sha256: self.retry(sha256) for sha256 in ziele}

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
        braucht dessen Job. Den fragt Procrastinates Lese-Schnittstelle einzeln ab
        (`list_jobs(id=…)`) — wer das je Dokument
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
        # braeuchte je Dokument eine Abfrage seines Jobs.
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
        Job (`inputs.job_id`) tut: wartet, laeuft, oder ist ohne Ergebnis zu Ende (dann
        hilft `retry`). Das ist die eine Stelle, an der man ein steckendes
        Dokument von einem unterscheidet, das noch dauert; das Warten fragt
        Procrastinate nicht.

        Ergebnis und Eingabe kommen aus einer Anweisung (`db.read_states`), der
        Job ueber die Kennung an der Eingabe (`inputs.job_id`, `list_jobs(id=…)`).

        Raises:
            DocumentNotFound: Unbekannt: weder Ergebnis noch Eingabe.

        Kosten: keine.
        """
        stand = self._stand(sha256)
        return db.derive_status(sha256, stand.row, self._job(stand))

    def _job(self, stand: db.DocumentState) -> Job | None:
        """Der Job, dessen Kennung an der Eingabe steht, None ohne Eingabe oder
        wenn er nicht mehr da ist."""
        if stand.job_id is None:
            return None
        jobs = self._procrastinate.job_manager.list_jobs(id=stand.job_id)
        return jobs[0] if jobs else None

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
            raise _noch_offen(sha256)
        return OCRWrapper.from_row(stand.row, self.conn)

    def _stand(self, sha256: str) -> db.DocumentState:
        """Ergebnis und Eingabe zu einem Hash, ohne `raw` — oder `DocumentNotFound`,
        wenn es keins von beiden gibt."""
        _hash_pruefen(sha256)
        (stand,) = db.read_states(self.conn, [sha256])
        if stand.is_unknown:
            raise _unbekannt(sha256)
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


class AsyncOCRClient:
    """Der asynchrone Zugang: dieselben Namen und dieselbe Bedeutung wie
    `OCRClient`, jeweils mit `await`. Als `async with` benutzen.

    `ocr_start`, `ocr`, `wait`, `wait_all`, `as_completed` und `get` gibt es hier;
    `status`, `preflight`, `document` und `retry` nur im `OCRClient`. Der
    Async-Client fragt RunPod nie und braucht weder Endpoint noch Schluessel.

    Er ist die zweite duenne Schale um denselben Kern ohne Ein- und Ausgabe
    (`core`, die SQL-Texte in `db`), ueber psycopgs `AsyncConnection` statt
    `Connection`, so wie httpx es macht. Eine Bruecke wie asgiref oder anyio gibt
    es auf diesem Weg nicht: Sie scheiterte in einer laufenden Ereignisschleife,
    etwa in Jupyter, und hielte Strg-C auf.

    Besitz und Lebensdauer wie beim Sync-Client, nur asynchron: `__aenter__`
    oeffnet die Verbindung des Clients, prueft auf ihr das Schema
    (`db.check_schema_async`), oeffnet einen `AsyncNullConnectionPool` ohne
    stehende Verbindungen und die eigene Procrastinate-App
    (`tasks.build_async_client_app`, `PsycopgConnector`); `__aexit__` schliesst
    sie in derselben Ereignisschleife, den Pool selbst, denn `app.close_async()`
    schliesst einen uebergebenen Pool nicht.

    Nebenlaeufigkeit: Ein Client gehoert einer Aufgabe. Wer aus mehreren Aufgaben
    zugleich abschickt, nimmt mehrere Clients; eine `AsyncConnection` fuehrt
    Anweisungen nacheinander aus, und eine Transaktion des einen Aufrufs gehoerte
    sonst auch dem anderen.
    """

    def __init__(self, db_dsn: str | None = None) -> None:
        """Baut den Client. `db_dsn` geht an `Settings.load`: Fehlt die Angabe,
        gilt die Konfiguration (Umgebung, dann Datei).

        Kosten: keine. Der Konstruktor spricht mit niemandem — erst `__aenter__`.
        """
        self._settings = Settings.load(db_dsn=db_dsn)
        self._conn: psycopg.AsyncConnection[Any] | None = None
        self._pool: AsyncNullConnectionPool[psycopg.AsyncConnection[Any]] | None = None
        self._app: procrastinate.App | None = None
        self._daemon_warnung: str | None = None

    async def __aenter__(self) -> AsyncOCRClient:
        """Oeffnet Verbindung, Pool und App. Scheitert eines davon, wird wieder
        zugemacht, was schon offen ist."""
        try:
            self._conn = await db.connect_async(self._settings.db_dsn)
            # Wie im Sync-Client: Ein fehlendes oder veraltetes Schema meldet sich
            # hier mit dem Weg, nicht spaeter beim ersten Lesen.
            await db.check_schema_async(self._conn)
            pool: AsyncNullConnectionPool[psycopg.AsyncConnection[Any]] = (
                AsyncNullConnectionPool(
                    self._settings.db_dsn, max_size=1, num_workers=1, open=False
                )
            )
            await pool.open()
            self._pool = pool
            self._app = await tasks.build_async_client_app(self._settings).open_async(
                pool
            )
        except BaseException:
            await self.__aexit__(None, None, None)
            raise
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        # Dieselbe Reihenfolge wie im Sync-Client: App, Pool, Verbindung.
        if self._app is not None:
            await self._app.close_async()
            self._app = None
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def settings(self) -> Settings:
        """Die geltende Einstellung."""
        return self._settings

    @property
    def _verbindung(self) -> psycopg.AsyncConnection[Any]:
        """Die Verbindung des Clients.

        Raises:
            ConfigurationError: Der Client ist nicht betreten worden.
        """
        if self._conn is None:
            raise ConfigurationError(
                "Der Client ist nicht betreten worden. Er oeffnet seine "
                "Verbindung erst dort: `async with AsyncOCRClient() as client: …`"
            )
        return self._conn

    @property
    def _procrastinate(self) -> procrastinate.App:
        """Die App des Clients — fuer das Einreihen.

        Raises:
            ConfigurationError: Der Client ist nicht betreten worden.
        """
        if self._app is None:
            raise ConfigurationError(
                "Der Client ist nicht betreten worden. Er oeffnet seine "
                "Procrastinate-App erst dort: `async with AsyncOCRClient() as "
                "client: …`"
            )
        return self._app

    @overload
    async def ocr_start(self, data: bytes) -> Submitted: ...

    @overload
    async def ocr_start(self, data: Sequence[bytes]) -> list[Submitted]: ...

    async def ocr_start(
        self, data: bytes | Sequence[bytes]
    ) -> Submitted | list[Submitted]:
        """Schickt Dokumente ab: Bytes, eines oder viele. Wartet nicht.

        Dieselbe Bedeutung wie `OCRClient.ocr_start`, dort ausfuehrlich: erst ALLE
        pruefen, dann einreihen; ein Ergebnis ist ein Treffer (auch `failed`), und
        es geschieht nichts; eine stehende Eingabe heisst unterwegs; sonst Eingabe
        und Job in einer eigenen Transaktion je Dokument, die Kennung des Jobs in
        `inputs.job_id`. Laeuft kein Daemon, wird trotzdem eingereiht und gewarnt.

        Returns:
            Bei Bytes ein `Submitted`, bei einer Folge eine Liste in der
            Reihenfolge der Eingabe.

        Raises:
            TypeError: Ein Element ist keine Bytes — meist ein Pfad oder Text.
            UnsupportedTypeError: Ein Typ, den das Paket nicht liest. Es ist dann
                NICHTS eingereiht worden.
            TooLargeError: Ein Dokument ist zu gross. Dito.

        Kosten: keine unmittelbar. Mittelbar: alles, was der Daemon danach tut.
        """
        einzeln = isinstance(data, bytes)
        dokumente = _dokumente(data)
        # ERST ALLES PRUEFEN, DANN EINREIHEN.
        vorbereitet = [core.prepare(dokument, self._settings) for dokument in dokumente]
        ergebnisse = [
            await self._abschicken(dokument, bereit)
            for dokument, bereit in zip(dokumente, vorbereitet, strict=True)
        ]
        if any(e.status is None for e in ergebnisse):
            await self._ohne_daemon_warnen()
        if einzeln:
            return ergebnisse[0]
        return ergebnisse

    async def wait(self, sha256: str, timeout: float | None = None) -> OCRWrapper:
        """Wartet auf das Endergebnis EINES Dokuments, wie `OCRClient.wait`.

        Die Huelle traegt `raw` geladen. Wirft nicht, weil ein Dokument
        scheiterte: Jeder Endzustand ist ein Ergebnis.

        Raises:
            DocumentNotFound: Weder Ergebnis noch Eingabe — sofort.
            ValueError: Die Frist ist `nan` oder negativ.
            WaitTimeoutError: Die Frist lief ab; er nennt den Hash.

        Kosten: keine.
        """
        _hash_pruefen(sha256)
        (huelle,) = await self.wait_all([sha256], timeout=timeout)
        return huelle

    async def wait_all(
        self, hashes: Sequence[str], timeout: float | None = None
    ) -> list[OCRWrapper]:
        """Wartet auf ALLE und liefert sie in der Reihenfolge der Frage, wie
        `OCRClient.wait_all`; ein doppelt gefragter Hash steht zweimal da.

        Raises:
            TypeError: `hashes` ist ein einzelner Hash, oder ein Element ist kein
                Hash.
            ValueError: Die Frist ist `nan` oder negativ.
            DocumentNotFound: Ein Hash ist unbekannt — sofort.
            WaitTimeoutError: Die Frist lief ab; `pending` nennt die offenen.

        Kosten: keine.
        """
        gefragt = self._hashes_pruefen(hashes, timeout)
        fertig = {h.sha256: h async for h in self._fertige(gefragt, timeout)}
        return [fertig[sha256] for sha256 in gefragt]

    def as_completed(
        self, hashes: Sequence[str], timeout: float | None = None
    ) -> AsyncGenerator[OCRWrapper]:
        """Liefert jedes Dokument, sobald es endgueltig ist, wie
        `OCRClient.as_completed` — als async-Generator: `async for`.

        Ein gewoehnlicher Aufruf und kein `async def`, damit Hashes, Frist und
        Betreten beim AUFRUF geprueft werden, nicht erst beim ersten
        Weiterschalten. Gewartet wird auf EINER eigenen Verbindung fuer den ganzen
        Aufruf; sie schliesst, wenn der Generator erschoepft ist, mit einem Fehler
        endet oder geschlossen wird (`aclose()`, oder `contextlib.aclosing`).

        Raises:
            TypeError, ValueError: Sofort beim Aufruf, wie bei `wait_all`.
            DocumentNotFound: Beim ersten Weiterschalten, wenn ein Hash unbekannt
                ist.
            WaitTimeoutError: Die Frist lief ab; `pending` nennt die offenen.

        Kosten: keine.
        """
        return self._fertige(self._hashes_pruefen(hashes, timeout), timeout)

    def _hashes_pruefen(self, hashes: Sequence[str], timeout: float | None) -> list[str]:
        """Wie `OCRClient._hashes_pruefen`: Ziele, Frist und Betreten beim Aufruf."""
        gefragt = _gefragte_hashes(hashes, timeout)
        self._verbindung  # Nicht betreten: der Fehler jetzt.
        return gefragt

    async def _fertige(
        self, hashes: list[str], timeout: float | None
    ) -> AsyncGenerator[OCRWrapper]:
        """Die Warteschleife (`db.wait_for_terminal_async`) mit Huellen, die `raw`
        tragen: Die Schleife liest ohne `raw`, und eine Eigenschaft kann nicht
        `await`en; geladen wird hier, je fertigem Dokument, ueber die Verbindung
        des Clients."""
        async with contextlib.aclosing(
            db.wait_for_terminal_async(
                self._settings.db_dsn,
                hashes,
                timeout=timeout,
                poll_seconds=self._settings.wait_poll_seconds,
            )
        ) as zeilen:
            async for zeile in zeilen:
                yield await self._huelle(zeile.sha256)

    async def _huelle(self, sha256: str) -> OCRWrapper:
        """Die Huelle zu einem Dokument mit Ergebnis, `raw` geladen.

        Raises:
            DocumentNotFound: Das Ergebnis ist inzwischen weg — `retry` nahm eine
                `failed`-Zeile weg, und das Dokument ist wieder offen.
        """
        zeile = await db.get_document_async(self._verbindung, sha256)
        if zeile is None:
            raise _noch_offen(sha256)
        return OCRWrapper.from_row(zeile, None)

    async def ocr(self, data: bytes, timeout: float | None = None) -> OCRWrapper:
        """Ein Dokument: abschicken und auf dieses eine warten, wie
        `OCRClient.ocr`. Ein Treffer kommt sofort.

        Raises:
            ValueError: Die Frist ist `nan` oder negativ. Gemeldet, BEVOR
                eingereiht wird.
            WaitTimeoutError: Die Zeit lief ab; das Dokument bleibt unterwegs.

        Kosten: JA, mittelbar.
        """
        db.check_wait_timeout(timeout)
        return await self.wait(await self.ocr_start(data), timeout=timeout)

    async def get(self, sha256: str) -> OCRWrapper:
        """Holt die Huelle zu einem Dokument mit Ergebnis, ohne zu warten, wie
        `OCRClient.get` — hier mit geladenem `raw`.

        Raises:
            DocumentNotFound: Es gibt (noch) kein Ergebnis; die Meldung sagt, ob
                das Dokument offen oder unbekannt ist.

        Kosten: keine.
        """
        _hash_pruefen(sha256)
        (stand,) = await db.read_states_async(self._verbindung, [sha256])
        if stand.is_unknown:
            raise _unbekannt(sha256)
        return await self._huelle(sha256)

    async def _ohne_daemon_warnen(self) -> None:
        """Wie `OCRClient._ohne_daemon_warnen`: einmal je Lage, und ein gesehener
        Daemon beendet die daemonlose Phase. Nur Auskunft; scheitert das Lesen,
        steht das in der Warnung, und eingereiht ist trotzdem."""
        try:
            if await db.worker_heartbeats_async(self._verbindung):
                self._daemon_warnung = None
                return
            fehler = None
        except (DatabaseError, psycopg.Error) as gefangen:
            fehler = gefangen
        self._daemon_warnung = _daemon_warnung_geben(self._daemon_warnung, fehler)

    async def _abschicken(self, data: bytes, bereit: core.Prepared) -> Submitted:
        """Wie `OCRClient._abschicken`: Treffer ohne `raw` lesen und nicht
        anfassen, sonst Eingabe und Job in EINER Transaktion oder gar nicht."""
        sha256 = bereit.sha256
        conn = self._verbindung
        vorhanden = await db.get_document_async(conn, sha256, with_raw=False)
        if vorhanden is not None:
            return Submitted(sha256, queued=False, status=vorhanden.status)
        try:
            async with conn.transaction():
                if not await db.insert_input_async(conn, sha256=sha256, content=data):
                    raise _SchonUnterwegs
                kennung = await tasks.enqueue_document_async(
                    self._procrastinate, conn, sha256
                )
                await db.set_input_job_async(conn, sha256=sha256, job_id=kennung)
        except _SchonUnterwegs:
            jetzt = await db.get_document_async(conn, sha256, with_raw=False)
            return Submitted(
                sha256, queued=False, status=jetzt.status if jetzt is not None else None
            )
        return Submitted(sha256, queued=True, status=None)
