"""Die Pflicht-Szenarien aus Plan 10.2 — mit echter Datenbank, ohne echtes RunPod.

Jedes Szenario prueft ZWEI Endzustaende: den der Zeile und den des Jobs. Nur eins
von beiden zu pruefen liesse genau den Fehler durch, gegen den das ganze
Ein-Buch-Modell steht — dass die beiden Buecher verschiedene Geschichten
erzaehlen.

Die Buchstaben in Klammern verweisen auf Plan 10.2, damit man von dort hierher
und zurueckfindet.
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import hashlib
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import procrastinate
import procrastinate.exceptions
import psycopg
import pytest
from procrastinate import utils
from procrastinate.jobs import Job
from procrastinate.manager import JobManager
from psycopg_pool import NullConnectionPool

import ersatz_antworten as ersatz
from mineru_ocr import config, daemon, db, tasks
from mineru_ocr.client import OCRClient
from mineru_ocr.config import Settings
from mineru_ocr.core import RetryOutcome
from mineru_ocr.db import DocumentStatus, Phase, derive_status, get_document
from mineru_ocr.errors import ConfigurationError, DatabaseError, DocumentNotFound
from mineru_ocr.runpod import RunPodClient

pytestmark = pytest.mark.db


# --------------------------------------------------------------------------
# Vorrichtungen: ein echter Daemon gegen die Test-Datenbank
# --------------------------------------------------------------------------


@pytest.fixture
def int_settings(settings: Settings, _test_datenbank: str) -> Settings:
    """Betriebseinstellung mit KURZEN Zeiten, sonst wie im Betrieb.

    Verkuerzt sind nur die Wartezeiten — Abfragetakt, Rueckzug, Nachfrist. Alles
    Uebrige bleibt die Voreinstellung, damit die Tests gegen das laufen, was
    wirklich gilt.
    """
    return dataclasses.replace(
        settings,
        db_dsn=_test_datenbank,
        poll_interval_seconds=0.01,
        balance_recheck_seconds=0.01,
        retry_base_seconds=0.05,
        retry_factor=1.0,
        shutdown_graceful_timeout=2.0,
        stalled_check_cron="* * * * *",
        ocr_concurrency=2,
    )


@dataclasses.dataclass
class Erzeuger:
    """Die einreihende Seite: eine App wie die des Clients, auf einer Verbindung."""

    app: procrastinate.App
    conn: psycopg.Connection[Any]

    def einreihen(self, sha: str) -> int:
        """Reiht ein Dokument ein, wie der Client es tut (`connection=`), und
        merkt die Kennung an der Eingabe."""
        kennung = tasks.enqueue_document(self.app, self.conn, sha)
        db.set_input_job(self.conn, sha256=sha, job_id=kennung)
        return kennung


@pytest.fixture
def erzeuger(
    db_conn: psycopg.Connection[Any], int_settings: Settings
) -> Iterator[Erzeuger]:
    """Die App des Clients, gebaut und geoeffnet wie in `OCRClient.__enter__`.

    Im Betrieb sind Daemon und Client ZWEI PROZESSE mit je eigener App. Im Test
    teilen sie sich den Prozess, aber nicht die App: Der Daemon baut seine in
    `daemon.run`, diese hier gehoert dem Test. Kein Zugang wird umgehaengt.
    """
    pool: NullConnectionPool[psycopg.Connection[Any]] = NullConnectionPool(
        int_settings.db_dsn, max_size=1, num_workers=1, open=False
    )
    pool.open()
    app = tasks.build_client_app(int_settings).open(pool)
    try:
        yield Erzeuger(app, db_conn)
    finally:
        app.close()
        pool.close()


class ProtokollRunPod:
    """RunPod-Doppel mit steuerbarem Verhalten je Aufruf."""

    def __init__(self, verhalten: Any = None) -> None:
        self.verhalten = verhalten or (lambda nr: _fertig())
        self.aufrufe = 0
        self.dateien: list[bytes] = []
        self.gestartet = asyncio.Event()
        self.freigabe: asyncio.Event | None = None
        self.abgebrochen: list[str] = []

    async def submit(self, *, file_b64: str, **_: Any) -> str:
        self.aufrufe += 1
        self.dateien.append(base64.b64decode(file_b64))
        return f"job-{self.aufrufe}"

    async def await_result(self, job_id: str) -> dict[str, Any]:
        self.gestartet.set()
        if self.freigabe is not None:
            await self.freigabe.wait()
        return self.verhalten(self.aufrufe)

    async def cancel(self, job_id: str) -> dict[str, Any]:
        self.abgebrochen.append(job_id)
        return {"id": job_id, "status": "CANCELLED"}

    async def image_tag(self) -> str | None:
        return ":17d4955b2"

    async def balance(self) -> float:
        return 5.0

    async def aclose(self) -> None:
        return None


def _fertig(markdown: str = "Text") -> dict[str, Any]:
    """Eine COMPLETED-Antwort des MinerU-4-Workers um eine Seite mit `markdown`
    (Bauform und Quellen: `ersatz_antworten`)."""
    return ersatz.fertige_antwort(markdown)


PNG_KOPF = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
"""Der Dateikopf, an dem der Job ein PNG erkennt: Den Typ bestimmt er aus den
Bytes, denn eine Ergebniszeile, die ihn tragen koennte, gibt es vor dem Ende
nicht."""


def _anlegen(conn: psycopg.Connection[Any], tmp_path: Path, name: str) -> str:
    """Legt die Eingabe an, wie das Abschicken es tut: keine Ergebniszeile."""
    inhalt = PNG_KOPF + f"inhalt von {name}".encode()
    pfad = tmp_path / name
    pfad.write_bytes(inhalt)
    sha = hashlib.sha256(inhalt).hexdigest()
    db.insert_input(conn, sha256=sha, content=inhalt)
    return sha


def _eingabe_steht(conn: psycopg.Connection[Any], sha: str) -> bool:
    return (
        conn.execute("SELECT 1 FROM inputs WHERE sha256 = %s", (sha,)).fetchone()
        is not None
    )


def _jobs_zu(conn: psycopg.Connection[Any], sha: str) -> list[Job]:
    """Liest die Jobs eines Dokuments — ueber ihren `lock`, den Hash.

    Genau der Weg des Ein-Buch-Modells: „DER JOB LINKED AUF DAS DOKUMENT DAS ER
    PROCESSED UND WENN MAN DEN STATUS EINES DOKUMENTS HABEN WILL QUERIED MAN
    NACH DE LETZTEN JOB DER AUF DAS DOKUMENT LINKT" (Plan 2.4).
    """
    zeilen = conn.execute(
        "SELECT id, status, attempts, queue_name, lock, task_name "
        "FROM procrastinate_jobs WHERE lock = %s ORDER BY id",
        (sha,),
    ).fetchall()
    return [
        Job(
            id=z[0],
            status=z[1],
            attempts=z[2],
            queue=z[3],
            lock=z[4],
            queueing_lock=None,
            task_name=z[5],
        )
        for z in zeilen
    ]


def _job_der_eingabe(conn: psycopg.Connection[Any], sha: str) -> Job | None:
    """Der Job, dessen Kennung an der Eingabe steht (`inputs.job_id`) — der Weg,
    auf dem `document()` und `retry` ihn finden. None ohne Eingabe oder Job."""
    zeile = conn.execute(
        "SELECT job_id FROM inputs WHERE sha256 = %s", (sha,)
    ).fetchone()
    if zeile is None or zeile[0] is None:
        return None
    return next((job for job in _jobs_zu(conn, sha) if job.id == zeile[0]), None)


async def _daemon_laufen_lassen(
    int_settings: Settings,
    runpod: Any,
    *,
    bis: Any,
    zeit: float = 15.0,
) -> None:
    """Startet den echten Daemon, wartet auf eine Bedingung, faehrt herunter.

    Der ECHTE Daemon und kein nachgebauter: Die Zwei-Koroutinen-Konstruktion ist
    die eine unbelegte Stelle des Entwurfs, und sie laesst sich nur pruefen,
    wenn wirklich `daemon.run` laeuft. Ausgetauscht wird allein der
    RunPod-Zugang — die Naht, an der Geld ausgegeben wuerde.
    """
    echte_mittel = tasks.worker_context_resources

    def mit_doppel(s: Settings) -> dict[str, Any]:
        mittel = echte_mittel(s)
        mittel["runpod"] = runpod
        return mittel

    tasks.worker_context_resources = mit_doppel  # type: ignore[assignment]
    try:
        lauf = asyncio.create_task(daemon.run(int_settings))
        try:
            async with asyncio.timeout(zeit):
                while not await bis():
                    if lauf.done():
                        await lauf  # bringt den Fehler des Daemons ans Licht
                        raise AssertionError("Der Daemon hat sich selbst beendet.")
                    await asyncio.sleep(0.05)
        finally:
            lauf.cancel()
            with pytest.raises((asyncio.CancelledError, SystemExit)):
                await lauf
    finally:
        tasks.worker_context_resources = echte_mittel


# --------------------------------------------------------------------------
# Die Szenarien
# --------------------------------------------------------------------------


async def test_dokument_laeuft_vollstaendig_durch(
    db_conn: psycopg.Connection[Any], int_settings: Settings, erzeuger: Erzeuger, tmp_path: Path
) -> None:
    """Der Grundfall, der alle anderen traegt: einreihen, Daemon laeuft, fertig.

    Geprueft werden BEIDE Buecher — die Zeile steht auf `done`, und der
    erfolgreiche Job ist geloescht (`delete_jobs = "successful"`): An ihm haengt
    kein Zustand mehr, die Eingabe ist mit dem Ergebnis gegangen. Der abgeleitete
    Zustand sagt TERMINAL.
    """
    sha = _anlegen(db_conn, tmp_path, "a.png")
    erzeuger.einreihen(sha)

    async def fertig() -> bool:
        return get_document(db_conn, sha) is not None

    await _daemon_laufen_lassen(int_settings, ProtokollRunPod(), bis=fertig)

    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.DONE
    assert zeile.raw is not None
    assert zeile.raw["markdown"] == "Text"

    assert _jobs_zu(db_conn, sha) == []
    assert derive_status(sha, zeile, _job_der_eingabe(db_conn, sha)).phase is Phase.TERMINAL


async def test_abgeschicktes_dokument_wird_aus_inputs_gelesen(
    db_conn: psycopg.Connection[Any], int_settings: Settings
) -> None:
    """Abschicken legt die Eingabe in `inputs` an, und der Job liest sie von dort.

    Der ganze Weg mit echtem Daemon und echtem Postgres: Die Bytes, die bei
    RunPod ankommen, sind die abgeschickten; nach dem Ergebnis ist die Eingabe
    geloescht; es liegt keine Spool-Datei und kein Pfad dazwischen.
    """
    inhalt = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"abgeschickt"
    with OCRClient(
        db_dsn=int_settings.db_dsn, api_key="erfunden", preflight=False
    ) as klient:
        sha = klient.ocr_start(inhalt)

    zeile = db_conn.execute(
        "SELECT content FROM inputs WHERE sha256 = %s", (sha,)
    ).fetchone()
    assert zeile is not None and bytes(zeile[0]) == inhalt
    assert len(_jobs_zu(db_conn, sha)) == 1

    async def fertig() -> bool:
        gelesen = get_document(db_conn, sha)
        return gelesen is not None

    runpod = ProtokollRunPod()
    await _daemon_laufen_lassen(int_settings, runpod, bis=fertig)

    assert runpod.dateien == [inhalt]
    gelesen = get_document(db_conn, sha)
    assert gelesen is not None and gelesen.status is DocumentStatus.DONE
    assert db_conn.execute(
        "SELECT 1 FROM inputs WHERE sha256 = %s", (sha,)
    ).fetchone() is None


async def test_nicht_wiederholbarer_fehler_im_ersten_versuch(
    db_conn: psycopg.Connection[Any], int_settings: Settings, erzeuger: Erzeuger, tmp_path: Path
) -> None:
    """(b) Zeile sofort `failed`, juengster Job `failed` — kein Retry-Karussell."""
    sha = _anlegen(db_conn, tmp_path, "b.png")
    erzeuger.einreihen(sha)

    runpod = ProtokollRunPod(lambda nr: {"id": "j", "status": "FAILED"})

    async def fertig() -> bool:
        zeile = get_document(db_conn, sha)
        return zeile is not None

    await _daemon_laufen_lassen(int_settings, runpod, bis=fertig)

    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.FAILED
    jobs = _jobs_zu(db_conn, sha)
    assert jobs[-1].status == "failed"
    # GENAU EIN Lauf, kein Karussell: Ein endgueltiger Fehler wird nicht
    # wiederholt.
    #
    # Warum der gespeicherte Zaehler dabei auf 1 steht und nicht auf 0 — das ist
    # am SQL-Schema hergeleitet und nicht an der Beobachtung angepasst:
    # `procrastinate_finish_job_v1` erhoeht `attempts` beim Beenden eines Jobs,
    # der in `doing` stand (`attempts = CASE status WHEN 'doing' THEN attempts+1
    # ELSE attempts END`). WAEHREND eines Laufs ist `attempts` also die Zahl der
    # VORHERIGEN Versuche (beim ersten Lauf 0 — so steht es auch in der
    # Referenz), DANACH die Zahl der Laeufe. Beide Lesarten sind richtig, sie
    # gelten nur zu verschiedenen Zeitpunkten.
    assert jobs[-1].attempts == 1


async def test_erschoepfte_versuche_enden_failed(
    db_conn: psycopg.Connection[Any], int_settings: Settings, erzeuger: Erzeuger, tmp_path: Path
) -> None:
    """(c) Nach den erlaubten voruebergehenden Fehlern: Zeile `failed`.

    Und der Versuchszaehler steht am Ende auf `max_attempts - 1` — die Zahl der
    Laeufe ist `attempts + 1`, also genau `max_attempts`.

    DIESER TEST BEHAUPTETE LANGE DAS GEGENTEIL SEINES EIGENEN NAMENS. Sein Titel
    und dieser Docstring sagten „Zeile `failed`"; der Koerper pruefte
    `status is NEW` und trug einen Kommentar, der die Abweichung wegerklaerte
    („Die Zeile bleibt `new`: Ein WIEDERHOLBARER Fehler laesst sie unberuehrt,
    auch der letzte"). Damit war die Absicht im Titel festgehalten und der Defekt
    im Koerper festgeschrieben — und zwei Zusicherungen im Paket standen
    daneben und sagten dasselbe wie der Titel: `errors.py` („nach Erschoepfung
    der Versuche wird daraus `failed`") und `db.py` (`FAILED` = „nicht
    wiederholbar ODER Versuche erschoepft").

    Gebaut war es nicht: Die Strategie gab bei erschoepftem Budget `None`
    zurueck, der Executor schrieb bei wiederholbaren Fehlern nichts, und er
    kannte den Versuchszaehler gar nicht. Ein dreimal transient gescheitertes
    Dokument endete mit Job `failed` und Zeile `new` — es tauchte nie im
    `failed`-Zaehler auf, sondern fuer immer unter „pending".

    Jetzt reicht `ocr_document` dem Executor durch, ob dies der letzte Versuch
    ist, und der Executor schreibt dann — mit den bis dahin abgeholten,
    BEZAHLTEN Teilstuecken, die ein Schreibvorgang von der Task-Ebene aus nicht
    haette.
    """
    sha = _anlegen(db_conn, tmp_path, "c.png")
    erzeuger.einreihen(sha)

    # TIMED_OUT ist wiederholbar. Nach dem Budget muss Schluss sein.
    runpod = ProtokollRunPod(lambda nr: {"id": "j", "status": "TIMED_OUT"})

    async def fertig() -> bool:
        jobs = _jobs_zu(db_conn, sha)
        return bool(jobs) and jobs[-1].status in ("failed", "succeeded")

    await _daemon_laufen_lassen(int_settings, runpod, bis=fertig)

    jobs = _jobs_zu(db_conn, sha)
    assert jobs[-1].status == "failed"
    # Der gespeicherte Zaehler steht am Ende auf der Zahl der LAEUFE (siehe die
    # Herleitung am vorigen Test), und die ist genau `max_attempts`.
    assert jobs[-1].attempts == int_settings.max_attempts
    assert runpod.aufrufe == int_settings.max_attempts

    zeile = get_document(db_conn, sha)
    assert zeile is not None
    # DIE ZEILE SAGT `failed` — wie Titel, Docstring, `errors.py` und `db.py` es
    # alle vier zusichern. Ein wiederholbarer Fehler laesst sie unberuehrt,
    # SOLANGE noch ein Versuch kommt; beim letzten wird geschrieben.
    assert zeile.status is DocumentStatus.FAILED
    assert derive_status(sha, zeile, _job_der_eingabe(db_conn, sha)).phase is Phase.TERMINAL
    # Und der Grund steht daneben, sonst saehe die Zeile aus wie ein Ergebnis
    # ohne Anlass.
    assert zeile.raw is not None
    assert zeile.raw["failure"]["type"] == "TransientRunPodError"
    assert zeile.error is not None and "TIMED_OUT" in zeile.error


async def test_absturz_mitten_im_dokument_heilt_durch_den_waechter(
    db_conn: psycopg.Connection[Any], int_settings: Settings, erzeuger: Erzeuger, tmp_path: Path
) -> None:
    """(a) Der Prozess stirbt HART mitten im Dokument — der Waechter legt neu vor.

    Dieses Szenario stand als Pflicht in Plan 10.2 und FEHLTE ersatzlos. Es ist
    der einzige Weg, auf dem `retry_stalled` ueber seine ganze Laenge geprueft
    wird: `test_wartungs_queue_verhungert_nicht` zeigt nur, dass der Waechter
    ueberhaupt laeuft, nicht dass er ein haengendes Dokument heilt.

    Der Unterschied zum geordneten Herunterfahren (Szenario (e)) ist wesentlich:
    Dort reiht der Worker seine abgebrochenen Jobs selbst neu ein, bevor er sich
    abmeldet. Hier stirbt der Prozess, OHNE das zu tun — der Job bleibt
    auf `doing` stehen, und niemand arbeitet mehr daran. Ohne den Waechter bliebe
    er dort fuer immer liegen, und das Dokument waere unsichtbar verloren: Es
    stuende offen da, saehe aus wie „laeuft" und wartete auf niemanden.

    DER HARTE TOD WIRD NACHGESTELLT statt erzeugt, und zwar deterministisch: Ein
    wirklich abgeschossener Prozess braeuchte im Test dreissig Sekunden, bis sein
    Herzschlag als veraltet gilt (`get_stalled_jobs` zaehlt ab
    `seconds_since_heartbeat=30`). Nachgestellt wird deshalb der Zustand, den er
    hinterlaesst: ein Job auf `doing`, dessen Worker-Zeile einen alten Herzschlag
    traegt. Genau das sieht der Waechter, und genau daran entscheidet er.
    """
    sha = _anlegen(db_conn, tmp_path, "a.png")
    erzeuger.einreihen(sha)

    # Den Zustand nach einem harten Tod herstellen: Der Job ist `doing`, der
    # Worker, der ihn hielt, meldet sich seit zwei Minuten nicht mehr.
    # `id` ist GENERATED ALWAYS — die Kennung vergibt Postgres, wir lesen sie.
    tote_kennung = db_conn.execute(
        "INSERT INTO procrastinate_workers (last_heartbeat) "
        "VALUES (now() - interval '2 minutes') RETURNING id"
    ).fetchone()
    assert tote_kennung is not None
    db_conn.execute(
        "UPDATE procrastinate_jobs SET status = 'doing', worker_id = %s "
        "WHERE lock = %s",
        (tote_kennung[0], sha),
    )
    db_conn.commit()

    jobs = _jobs_zu(db_conn, sha)
    assert len(jobs) == 1
    assert jobs[0].status == "doing"
    assert get_document(db_conn, sha) is None
    assert _eingabe_steht(db_conn, sha)

    # Jetzt laeuft ein FRISCHER Daemon. Diesmal antwortet RunPod normal.
    runpod = ProtokollRunPod()

    async def dokument_fertig() -> bool:
        z = get_document(db_conn, sha)
        return z is not None and z.status is DocumentStatus.DONE

    await _daemon_laufen_lassen(
        int_settings, runpod, bis=dokument_fertig, zeit=90.0
    )

    # DER WAECHTER HAT GEHEILT: Das Dokument ist von vorn gelaufen und fertig.
    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.DONE
    assert zeile.raw is not None
    # Und es wurde wirklich verarbeitet, nicht bloss umgebucht.
    assert runpod.aufrufe >= 1


async def test_waechter_beendet_einen_haengenden_job_ohne_restbudget_mit_failed(
    db_conn: psycopg.Connection[Any], int_settings: Settings, erzeuger: Erzeuger, tmp_path: Path
) -> None:
    """(a2) Starb der Worker im letzten Versuch, legt der Waechter nicht mehr
    vor: Er schreibt `failed` („Worker wiederholt abgestuerzt") und beendet den
    Job als `failed` (Mineroo#27).

    Ohne Budget lief ein Dokument, das den Worker abstuerzen laesst, endlos.
    Der harte Tod wird wie in Szenario (a) nachgestellt, diesmal mit einem
    Versuchszaehler, nach dem kein Versuch mehr kommt. RunPod wird nicht
    gefragt, und die Eingabe bleibt fuer `retry` stehen.
    """
    sha = _anlegen(db_conn, tmp_path, "a2.png")
    erzeuger.einreihen(sha)

    tote_kennung = db_conn.execute(
        "INSERT INTO procrastinate_workers (last_heartbeat) "
        "VALUES (now() - interval '2 minutes') RETURNING id"
    ).fetchone()
    assert tote_kennung is not None
    db_conn.execute(
        "UPDATE procrastinate_jobs SET status = 'doing', worker_id = %s, "
        "attempts = %s WHERE lock = %s",
        (tote_kennung[0], int_settings.max_attempts - 1, sha),
    )
    db_conn.commit()

    runpod = ProtokollRunPod()

    async def job_beendet() -> bool:
        jobs = _jobs_zu(db_conn, sha)
        return bool(jobs) and jobs[-1].status == "failed"

    await _daemon_laufen_lassen(int_settings, runpod, bis=job_beendet, zeit=90.0)

    jobs = _jobs_zu(db_conn, sha)
    assert len(jobs) == 1
    assert jobs[0].status == "failed"
    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.FAILED
    assert zeile.error is not None and "Worker wiederholt abgestuerzt" in zeile.error
    assert runpod.aufrufe == 0
    assert _eingabe_steht(db_conn, sha)


async def test_retry_von_hand_verarbeitet_wirklich_neu(
    db_conn: psycopg.Connection[Any],
    async_conn: psycopg.AsyncConnection[Any],
    int_settings: Settings,
    erzeuger: Erzeuger,
    tmp_path: Path,
) -> None:
    """(g) `retry_job_by_id` setzt den Job auf `todo`, die Ereignis-Historie
    bleibt — UND das Dokument wird wirklich neu verarbeitet: es endet `done`,
    `raw` ist ueberschrieben.

    Genau diese letzte Zusicherung faengt ein Nachsehen, das faelschlich
    `failed` deckt. Ohne sie saehe ein Nichts-Tun wie ein Erfolg aus.
    """
    sha = _anlegen(db_conn, tmp_path, "g.png")
    erzeuger.einreihen(sha)

    # Erster Durchlauf: endgueltiger Fehler.
    scheiternd = ProtokollRunPod(lambda nr: {"id": "j", "status": "FAILED"})

    async def gescheitert() -> bool:
        zeile = get_document(db_conn, sha)
        return zeile is not None and zeile.status is DocumentStatus.FAILED

    await _daemon_laufen_lassen(int_settings, scheiternd, bis=gescheitert)

    jobs = _jobs_zu(db_conn, sha)
    assert jobs[-1].status == "failed"
    ereignisse_vorher = db_conn.execute(
        "SELECT count(*) FROM procrastinate_events WHERE job_id = %s", (jobs[-1].id,)
    ).fetchone()
    assert ereignisse_vorher is not None

    # Retry von Hand — Procrastinates eigenes Bordmittel.
    kennung = jobs[-1].id
    # `Job.id` ist als optional deklariert (ein noch nicht eingereihter Job
    # hat keine). Ein aus der Datenbank gelesener hat immer eine — das wird
    # hier festgestellt statt unterdrueckt.
    assert kennung is not None
    erzeuger.app.job_manager.retry_job_by_id(
        kennung, utils.utcnow(), jobs[-1].priority, jobs[-1].queue, jobs[-1].lock
    )

    nach_retry = _jobs_zu(db_conn, sha)
    assert nach_retry[-1].status == "todo"
    # Jede Wiedervorlage zaehlt hoch: 1 aus dem gescheiterten Lauf, +1 aus dem
    # Retry von Hand. Beide laufen durch dieselbe SQL-Funktion
    # (Feature-Verifikation Punkt 19).
    assert nach_retry[-1].attempts == 2
    # Die Ereignis-Historie bleibt.
    danach = db_conn.execute(
        "SELECT count(*) FROM procrastinate_events WHERE job_id = %s",
        (nach_retry[-1].id,),
    ).fetchone()
    assert danach is not None
    assert danach[0] >= ereignisse_vorher[0]

    # Zweiter Durchlauf: er muss WIRKLICH neu verarbeiten.
    gelingend = ProtokollRunPod(lambda nr: _fertig("Beim zweiten Mal geklappt"))

    async def gelungen() -> bool:
        zeile = get_document(db_conn, sha)
        return zeile is not None and zeile.status is DocumentStatus.DONE

    await _daemon_laufen_lassen(int_settings, gelingend, bis=gelungen)

    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.DONE
    assert zeile.raw is not None
    assert zeile.raw["markdown"] == "Beim zweiten Mal geklappt"
    # Es wurde WIRKLICH gearbeitet und nicht am Guard vorbeigeschlichen.
    assert gelingend.aufrufe == 1


async def test_wartungs_queue_verhungert_nicht(
    db_conn: psycopg.Connection[Any], int_settings: Settings, erzeuger: Erzeuger, tmp_path: Path
) -> None:
    """(h) Alle OCR-Plaetze blockiert, der Waechter auf der Wartungs-Queue laeuft
    trotzdem.

    DIE UNBELEGTE STELLE DES ENTWURFS (Feature-Verifikation Punkt 1): Zwei
    Worker-Koroutinen in einem Prozess sind in der Procrastinate-Doku weder
    erlaubt noch verboten; dokumentiert ist ausschliesslich „mehrere Worker =
    mehrere Prozesse". Schlaegt dieser Test fehl, wird auf den dokumentierten
    Rueckfall umgeplant — zwei Prozesse, einer je Queue —, und `daemon.run` ist
    mit seinem `queues`-Parameter darauf vorbereitet.

    Aufbau: `ocr_concurrency` steht auf 2, und es werden 2 Dokumente eingereiht,
    deren RunPod-Aufruf HAENGT. Damit sind beide OCR-Plaetze belegt. Der Waechter
    laeuft periodisch jede Minute — geprueft wird, dass ueberhaupt ein
    Wartungs-Job entsteht und durchlaeuft, waehrend die OCR-Queue steht.
    """
    for nr in range(int_settings.ocr_concurrency):
        sha = _anlegen(db_conn, tmp_path, f"h{nr}.png")
        erzeuger.einreihen(sha)

    haengend = ProtokollRunPod()
    haengend.freigabe = asyncio.Event()  # wird nie gesetzt: die Plaetze bleiben belegt

    # Die Belegung wird WAEHREND des Laufs mitgeschrieben, nicht danach abgefragt.
    # Danach ginge nicht mehr: Ein sauberes Herunterfahren beendet die haengenden
    # Jobs, sie stehen anschliessend auf `aborted` und nicht mehr auf `doing`.
    # Frueher stand die Pruefung hinter dem Lauf und bestand auch — aber nur,
    # weil der Daemon seine Worker gar nicht herunterfuhr und die Jobs als
    # Karteileichen auf `doing` stehenblieben. Der Test mass damit das Symptom
    # eines Fehlers statt der Zusicherung, um die es ihm geht: dass die
    # OCR-Plaetze belegt waren, WAEHREND der Waechter lief.
    hoechststand = 0

    async def waechter_lief() -> bool:
        nonlocal hoechststand
        belegt = db_conn.execute(
            "SELECT count(*) FROM procrastinate_jobs "
            "WHERE queue_name = %s AND status = 'doing'",
            (config.QUEUE_OCR,),
        ).fetchone()
        if belegt is not None:
            hoechststand = max(hoechststand, belegt[0])

        # Der Waechter ist der Task auf der Wartungs-Queue. Sobald er einen Job
        # bekommen und abgeschlossen hat, ist die Frage beantwortet. Erfolgreiche
        # Jobs loescht der Worker (`delete_jobs = "successful"`); abgeschlossen
        # heisst deshalb: eingereiht (der periodische Vermerk steht) und kein
        # Waechter-Job mehr da, der nicht erfolgreich waere.
        zeile = db_conn.execute(
            "SELECT count(*) FROM procrastinate_periodic_defers d "
            "WHERE d.task_name = %s AND NOT EXISTS ("
            "SELECT 1 FROM procrastinate_jobs j "
            "WHERE j.task_name = %s AND j.status <> 'succeeded')",
            (config.TASK_RETRY_STALLED, config.TASK_RETRY_STALLED),
        ).fetchone()
        return zeile is not None and zeile[0] >= 1

    await _daemon_laufen_lassen(
        int_settings, haengend, bis=waechter_lief, zeit=90.0
    )

    # Und die Gegenprobe: Die OCR-Plaetze WAREN wirklich belegt, waehrend der
    # Waechter durchlief.
    assert haengend.gestartet.is_set()
    assert hoechststand == int_settings.ocr_concurrency


async def test_abbruch_beim_herunterfahren_wird_neu_eingereiht(
    db_conn: psycopg.Connection[Any], int_settings: Settings, erzeuger: Erzeuger, tmp_path: Path
) -> None:
    """(e) Ein beim Herunterfahren abgebrochener Job wird neu eingereiht, und die
    Wiedervorlage zaehlt als Versuch (Mineroo#27).

    Procrastinate bricht den laufenden Job nach der Nachfrist mit
    `CancelledError` ab, fragt die Retry-Strategie, und die sagt ja, solange
    Versuche uebrig sind. Danach steht der Job wieder auf `todo`, `attempts` ist
    gestiegen, es gibt kein Ergebnis, die Eingabe steht, und der RunPod-Job ist
    abgebrochen. Der naechste Daemon laesst das Dokument durchlaufen.
    """
    sha = _anlegen(db_conn, tmp_path, "e.png")
    erzeuger.einreihen(sha)

    haengend = ProtokollRunPod()
    haengend.freigabe = asyncio.Event()

    async def laeuft() -> bool:
        return haengend.gestartet.is_set()

    await _daemon_laufen_lassen(int_settings, haengend, bis=laeuft)

    # Nach dem Herunterfahren: neu eingereiht, ein Versuch verbraucht, kein
    # Ergebnis, die Eingabe steht, und RunPods Job ist abgebrochen.
    assert get_document(db_conn, sha) is None
    assert _eingabe_steht(db_conn, sha)
    jobs = _jobs_zu(db_conn, sha)
    assert len(jobs) == 1
    assert jobs[0].status == "todo"
    assert jobs[0].attempts == 1
    assert haengend.abgebrochen == ["job-1"]
    kennung = jobs[0].id

    # Der naechste Daemon nimmt DENSELBEN Job wieder auf, ohne dass jemand
    # einreiht, und das Dokument laeuft durch. Er behaelt erfolgreiche Jobs
    # (`delete_jobs="never"`), sonst loeschte er ihn (Mineroo#26), und es liesse
    # sich nicht mehr sehen, welcher Job das Ergebnis schrieb.
    gelingend = ProtokollRunPod(lambda nr: _fertig("Nach dem Neustart"))

    async def fertig() -> bool:
        z = get_document(db_conn, sha)
        return z is not None and z.status is DocumentStatus.DONE

    await _daemon_laufen_lassen(
        dataclasses.replace(int_settings, delete_jobs="never"), gelingend, bis=fertig
    )

    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.DONE
    jobs = _jobs_zu(db_conn, sha)
    assert [(job.id, job.status) for job in jobs] == [(kennung, "succeeded")]


async def test_abbruch_beim_herunterfahren_im_letzten_versuch_endet_failed(
    db_conn: psycopg.Connection[Any], int_settings: Settings, erzeuger: Erzeuger, tmp_path: Path
) -> None:
    """(e2) Faellt das Herunterfahren auf den letzten Versuch, steht danach
    `failed` mit dem Vermerk da, und der Job endet `aborted` (Mineroo#27).

    Das ist der Fall, fuer den die Regel „jeder Job, der nicht neu eingereiht
    wird, schreibt ein Ergebnis" steht: Ein Daemon, der waehrend eines langen
    Dokuments so oft neu startet, wie es Versuche gibt, liesse es sonst ohne
    Ergebnis zurueck, und `wait` hinge ewig. Zwei Versuche halten den Test kurz;
    die Rechnung ist dieselbe wie bei drei.
    """
    zwei = dataclasses.replace(int_settings, max_attempts=2)
    sha = _anlegen(db_conn, tmp_path, "e2.png")
    erzeuger.einreihen(sha)

    # Zwei Daemon-Laeufe, jeder faehrt herunter, waehrend RunPod rechnet. Nach
    # dem ersten ist der Job neu eingereiht; nach dem zweiten, dem letzten
    # Versuch, endet er. `attempts` zaehlt jeden beendeten Lauf, die
    # Wiedervorlage wie das Ende (`procrastinate_finish_job`).
    for nach_dem_lauf, laeufe in (("todo", 1), ("aborted", 2)):
        haengend = ProtokollRunPod()
        haengend.freigabe = asyncio.Event()

        async def laeuft(h: ProtokollRunPod = haengend) -> bool:
            return h.gestartet.is_set()

        await _daemon_laufen_lassen(zwei, haengend, bis=laeuft)
        assert haengend.abgebrochen == ["job-1"]
        jobs = _jobs_zu(db_conn, sha)
        assert len(jobs) == 1
        assert jobs[0].status == nach_dem_lauf
        assert jobs[0].attempts == laeufe
        if nach_dem_lauf == "todo":
            assert get_document(db_conn, sha) is None

    # Nach dem zweiten Herunterfahren: kein neuer Versuch, aber ein Ergebnis.
    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.FAILED
    assert zeile.error is not None
    assert (
        "im letzten Versuch abgebrochen (Herunterfahren oder Abbruch ueber die API)"
        in zeile.error
    )
    # Die Eingabe bleibt, damit `retry` etwas zu lesen hat.
    assert _eingabe_steht(db_conn, sha)


async def test_abbruch_ueber_die_api_wird_nicht_neu_eingereiht(
    db_conn: psycopg.Connection[Any], int_settings: Settings, erzeuger: Erzeuger, tmp_path: Path
) -> None:
    """(e3) Ein Abbruch ueber die API (`cancel_job_by_id(…, abort=True)`) kommt
    beim Task als derselbe `CancelledError` an wie das Herunterfahren, und die
    Retry-Strategie sagt ja. Neu eingereiht wird er trotzdem nicht:
    `procrastinate_retry_job_v2` setzt einen Job mit `abort_requested` auf
    `failed`. Vor dem letzten Versuch endet das Dokument damit ohne Ergebnis,
    einer der Faelle, fuer die `retry` der Weg zurueck ist (Mineroo#27,
    CLAUDE.md). Der RunPod-Job ist abgebrochen.
    """
    sha = _anlegen(db_conn, tmp_path, "e3.png")
    erzeuger.einreihen(sha)

    haengend = ProtokollRunPod()
    haengend.freigabe = asyncio.Event()
    abgesagt = False

    async def abgebrochen() -> bool:
        nonlocal abgesagt
        if haengend.gestartet.is_set() and not abgesagt:
            kennung = _jobs_zu(db_conn, sha)[0].id
            assert kennung is not None
            assert erzeuger.app.job_manager.cancel_job_by_id(kennung, abort=True)
            abgesagt = True
        return abgesagt and _jobs_zu(db_conn, sha)[0].status != "doing"

    await _daemon_laufen_lassen(int_settings, haengend, bis=abgebrochen)

    jobs = _jobs_zu(db_conn, sha)
    assert len(jobs) == 1
    assert jobs[0].status == "failed"
    assert get_document(db_conn, sha) is None
    assert _eingabe_steht(db_conn, sha)
    assert haengend.abgebrochen == ["job-1"]


async def test_duplikat_waehrend_der_verarbeitung_ist_ein_nichts_tun(
    db_conn: psycopg.Connection[Any],
    async_conn: psycopg.AsyncConnection[Any],
    int_settings: Settings,
    erzeuger: Erzeuger,
    tmp_path: Path,
) -> None:
    """(i) Ein waehrend `doing` nachgeschobenes Duplikat wird per `lock`
    serialisiert, startet danach und endet sofort am Nachsehen — keine
    Doppelzahlung.

    Der Fall ist real und nicht ausgedacht: `queueing_lock` bindet NUR den
    Zustand `todo` und ist frei, sobald der Job laeuft („allows a single job in
    todo status. Meanwhile, it allows multiple jobs to be in doing status",
    Feature-Verifikation Punkt 5). Ohne den zweiten Lock liefe das Duplikat
    parallel und bezahlte dasselbe Dokument ein zweites Mal.

    Aufbau ohne Zeitrennen: Das Dokument wird zuerst fertig verarbeitet, DANN
    ein zweiter Job eingereiht. Weil das Ergebnis dann `done` ist, muss der
    zweite Job am Nachsehen enden, ohne RunPod auch nur anzufassen.
    """
    sha = _anlegen(db_conn, tmp_path, "i.png")
    erzeuger.einreihen(sha)

    erster = ProtokollRunPod()

    async def fertig() -> bool:
        z = get_document(db_conn, sha)
        return z is not None and z.status is DocumentStatus.DONE

    await _daemon_laufen_lassen(int_settings, erster, bis=fertig)
    assert erster.aufrufe == 1

    # Jetzt das Duplikat.
    erzeuger.einreihen(sha)
    zweiter = ProtokollRunPod()

    async def zweiter_job_fertig() -> bool:
        # Erfolgreich heisst geloescht (`delete_jobs = "successful"`).
        return _jobs_zu(db_conn, sha) == []

    await _daemon_laufen_lassen(int_settings, zweiter, bis=zweiter_job_fertig)

    # DER PUNKT: Der zweite Job lief durch, ohne RunPod anzufassen.
    assert zweiter.aufrufe == 0
    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.DONE


async def test_jede_wiedervorlage_erhoeht_den_versuchszaehler(
    db_conn: psycopg.Connection[Any], int_settings: Settings, erzeuger: Erzeuger, tmp_path: Path
) -> None:
    """(j) Bestaetigt am laufenden System, was am SQL-Schema belegt ist
    (Feature-Verifikation Punkt 19): Jedes Zuruecksetzen auf `todo` — durch den
    Waechter wie durch einen Retry von Hand — erhoeht `attempts` und zehrt am
    Budget aus `max_attempts`.

    Das Ergebnis steht als Kommentar an der Wiederholungs-Strategie, damit es
    niemand fuer einen Fehler haelt.
    """
    sha = _anlegen(db_conn, tmp_path, "j.png")
    erzeuger.einreihen(sha)

    runpod = ProtokollRunPod(lambda nr: {"id": "j", "status": "TIMED_OUT"})

    async def erschoepft() -> bool:
        jobs = _jobs_zu(db_conn, sha)
        return bool(jobs) and jobs[-1].status == "failed"

    await _daemon_laufen_lassen(int_settings, runpod, bis=erschoepft)

    jobs = _jobs_zu(db_conn, sha)
    # Genau EIN Job, dessen Zaehler mit jedem Strategie-Retry gestiegen ist —
    # kein neuer Job je Versuch.
    assert len(jobs) == 1
    assert jobs[0].attempts == int_settings.max_attempts


async def test_daemon_faehrt_sauber_herunter(
    db_conn: psycopg.Connection[Any],
    int_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Beide Worker enden, die Betriebsmittel werden NACH ihnen geschlossen.

    Andersherum zoege man einem noch laufenden Task den Boden weg — und dessen
    letzter Schreibvorgang ist der mit dem bezahlten Ergebnis.

    WAS DIESER TEST FRUEHER NICHT GEPRUEFT HAT, obwohl sein Name es sagte: Er sah
    nur nach, ob der Pool am Ende geschlossen ist. Das bestand er auch, als der
    Daemon die Worker gar nicht herunterfuhr — `run` kehrte damals zurueck,
    WAEHREND Pool und HTTP-Zugang noch schlossen und die Worker weiterliefen
    (`asyncio.shield` laesst den Wartenden sofort zurueckkehren). Gemessen wurden
    dabei 21 verwaiste, noch laufende Tasks bei bereits geschlossenem Pool, ein
    an `PoolClosed` gestorbener Dokument-Job — und Worker, die sich nie
    abmeldeten und als Karteileichen in `procrastinate_workers` stehenblieben,
    was die uebrigen Integrationstests reihenfolgeabhaengig scheitern liess.

    Deshalb pruefen die Zusicherungen jetzt die REIHENFOLGE und nicht nur den
    Endzustand: Nach der Rueckkehr von `run` darf kein Worker-Task mehr laufen,
    und die Worker-Tabelle muss leer sein — beides ist nur erreichbar, wenn das
    Herunterfahren VOR dem Schliessen fertig war.
    """
    gebaut: dict[str, Any] = {}
    echte_mittel = tasks.worker_context_resources

    def merken(s: Settings) -> dict[str, Any]:
        mittel = echte_mittel(s)
        mittel["runpod"] = ProtokollRunPod()
        gebaut.update(mittel)
        return mittel

    # Gewartet wird, bis BEIDE Worker ihre Anmeldung abgeschlossen haben, nicht
    # nur, bis der Pool offen ist. Procrastinates `Worker.run` meldet sich
    # ungeschuetzt an: Trifft der Abbruch einen Worker zwischen dem INSERT in
    # `procrastinate_workers` und der Rueckkehr von `register_worker`, kennt er
    # seine Kennung nie und kann sich nicht abmelden. Das war die Ursache, warum
    # dieser Test etwa einmal in sieben Laeufen scheiterte: Er brach genau dann
    # ab, wenn sich die Worker gerade anmeldeten. Was er prueft, das Abmelden
    # beim Herunterfahren, setzt angemeldete Worker voraus.
    echtes_anmelden = JobManager.register_worker
    angemeldet: list[int] = []

    async def anmelden_und_merken(selbst: JobManager) -> int:
        kennung = await echtes_anmelden(selbst)
        angemeldet.append(kennung)
        return kennung

    monkeypatch.setattr(JobManager, "register_worker", anmelden_und_merken)
    tasks.worker_context_resources = merken  # type: ignore[assignment]
    try:
        lauf = asyncio.create_task(daemon.run(int_settings))
        # Warten, bis der Pool wirklich offen ist — sonst prueft der Test das
        # Schliessen von etwas, das nie auf war — und beide Worker angemeldet
        # sind.
        async with asyncio.timeout(15.0):
            while not gebaut or gebaut["pool"].closed or len(angemeldet) < 2:
                await asyncio.sleep(0.05)
        lauf.cancel()
        with pytest.raises(asyncio.CancelledError):
            await lauf
    finally:
        tasks.worker_context_resources = echte_mittel

    # Der Abbruch mitten im `async with` laesst Procrastinate aufraeumen; der
    # Pool gehoert dem Daemon und wird von ihm geschlossen.
    assert gebaut["pool"].closed

    # Und jetzt das, was der Test frueher nicht sah. Erstens: KEIN Task des
    # Daemons laeuft noch. Verwaiste Worker-Tasks bei geschlossenem Pool sind
    # genau die Lage, in der ein bezahltes Ergebnis nicht mehr geschrieben
    # werden kann.
    uebrig = [
        aufgabe
        for aufgabe in asyncio.all_tasks()
        if not aufgabe.done()
        and aufgabe is not asyncio.current_task()
        and (aufgabe.get_name().startswith("mineru-ocr-") or aufgabe.get_name() == "beenden")
    ]
    assert uebrig == [], f"Noch laufende Daemon-Tasks nach der Rueckkehr: {uebrig}"

    # Zweitens: Die Worker haben sich ABGEMELDET. Das geht nur, solange der Pool
    # noch offen ist — die leere Tabelle ist damit der Beleg, dass das Beenden
    # vor dem Schliessen fertig war, und nicht bloss angestossen.
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM procrastinate_workers")
        zeile = cur.fetchone()
    assert zeile is not None
    assert zeile[0] == 0, (
        f"{zeile[0]} Worker-Registrierung(en) blieben stehen. Der Daemon hat "
        f"seine Worker nicht heruntergefahren, bevor er zurueckkehrte."
    )


async def test_ohne_daemon_passiert_nichts_und_das_wird_gesagt(
    db_conn: psycopg.Connection[Any], int_settings: Settings, erzeuger: Erzeuger, tmp_path: Path
) -> None:
    """Ohne laufenden Daemon stapeln sich Jobs, und der abgeleitete Zustand
    sagt es.

    Die haeufigste Betriebs-Ueberraschung dieses Aufbaus: Alles sieht normal aus,
    nur arbeitet niemand. Der abgeleitete Zustand unterscheidet deshalb
    ausdruecklich zwischen „kein Job mehr unterwegs" und „wartet" — und die
    Worker-Lebenszeichen sagen, ob ueberhaupt jemand da ist.
    """
    sha = _anlegen(db_conn, tmp_path, "leer.png")

    # Eingabe ohne Job: Unterwegs ist nichts.
    assert get_document(db_conn, sha) is None
    assert derive_status(sha, None, _job_der_eingabe(db_conn, sha)).phase is Phase.ENDED_WITHOUT_RESULT

    erzeuger.einreihen(sha)
    # Danach: wartet — und niemand arbeitet.
    assert derive_status(sha, None, _job_der_eingabe(db_conn, sha)).phase is Phase.WAITING
    assert db.worker_heartbeats(db_conn) == []


# --------------------------------------------------------------------------
# Eine App je Prozess
# --------------------------------------------------------------------------


async def test_vom_client_eingereihter_job_wird_vom_daemon_abgearbeitet(
    db_conn: psycopg.Connection[Any], int_settings: Settings
) -> None:
    """Der ganze Weg ueber den echten `OCRClient`: Er reiht per Namen ein (eigene
    App, eigene Verbindung), der Job liegt in der Queue, die der Daemon liest, und
    der Daemon arbeitet ihn ab.

    Beide Seiten sind hier zwei Apps im selben Prozess — im Betrieb sind es zwei
    Prozesse. Gerade deshalb prueft der Test, dass sie sich nur ueber die
    Datenbank finden: Name und Queue des Jobs muessen die sein, unter denen der
    Daemon seinen Task angemeldet hat.
    """
    with OCRClient(
        db_dsn=int_settings.db_dsn,
        api_key="erfunden",
        preflight=False,
    ) as klient:
        sha = klient.ocr_start(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"client")

        jobs = _jobs_zu(db_conn, sha)
        assert len(jobs) == 1
        assert jobs[0].queue == config.QUEUE_OCR
        assert jobs[0].task_name == config.TASK_OCR_DOCUMENT

        async def fertig() -> bool:
            return get_document(db_conn, sha) is not None

        await _daemon_laufen_lassen(int_settings, ProtokollRunPod(), bis=fertig)

    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.DONE
    # Erfolgreich durchgelaufen und deshalb geloescht.
    assert _jobs_zu(db_conn, sha) == []


# --------------------------------------------------------------------------
# Die Ergebniszeile entsteht am Ende des Jobs
# --------------------------------------------------------------------------


async def test_abschicken_legt_nur_die_eingabe_an_und_der_job_am_ende_die_zeile(
    db_conn: psycopg.Connection[Any], int_settings: Settings
) -> None:
    """Abschicken heisst, Procrastinate einen Job erstellen zu lassen: Danach gibt
    es Eingabe und Job, aber kein Ergebnis. Die Zeile legt der Job an, wenn er
    endet, mit Typ, Groesse und Endpoint aus Bytes und Einstellung; die Eingabe
    geht mit ihr."""
    inhalt = PNG_KOPF + b"nur die eingabe"
    with OCRClient(db_dsn=int_settings.db_dsn, preflight=False) as klient:
        sha = klient.ocr_start(inhalt)
        assert sha.queued is True and sha.status is None
        assert get_document(db_conn, sha) is None
        assert _eingabe_steht(db_conn, sha)
        assert len(_jobs_zu(db_conn, sha)) == 1

        async def fertig() -> bool:
            return get_document(db_conn, sha) is not None

        await _daemon_laufen_lassen(int_settings, ProtokollRunPod(), bis=fertig)

    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.DONE
    assert (zeile.mime, zeile.size_bytes) == ("image/png", len(inhalt))
    assert zeile.endpoint_id == int_settings.endpoint_id
    assert not _eingabe_steht(db_conn, sha)


def test_abschicken_legt_eingabe_und_job_in_einer_transaktion_an_und_merkt_die_kennung(
    db_conn: psycopg.Connection[Any],
    int_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Eingabe und Job entstehen in EINER Transaktion: Solange das Einreihen
    laeuft, sieht eine andere Verbindung weder die Eingabe noch den Job. Danach
    steht die Kennung, die `defer` zurueckgab, in `inputs.job_id`, und der Job
    liegt in der Queue des Daemons und traegt nur `lock`, keinen
    `queueing_lock`: Die Sperre gegen Dubletten ist die Eingabe-Tabelle.

    Nur gegen echtes Postgres pruefbar, weil Procrastinates `InMemoryConnector`
    `connection=` uebergeht."""
    echtes_einreihen = tasks.enqueue_document
    gesehen: list[tuple[object, object]] = []

    def beobachtet(app: Any, conn: Any, sha256: str) -> int:
        kennung = echtes_einreihen(app, conn, sha256)
        with psycopg.connect(int_settings.db_dsn, autocommit=True) as andere:
            gesehen.append(
                (
                    andere.execute(
                        "SELECT 1 FROM inputs WHERE sha256 = %s", (sha256,)
                    ).fetchone(),
                    andere.execute(
                        "SELECT 1 FROM procrastinate_jobs WHERE id = %s", (kennung,)
                    ).fetchone(),
                )
            )
        return kennung

    monkeypatch.setattr(tasks, "enqueue_document", beobachtet)
    with OCRClient(db_dsn=int_settings.db_dsn, preflight=False) as klient:
        sha = klient.ocr_start(PNG_KOPF + b"eine transaktion")

    assert gesehen == [(None, None)]
    zeilen = db_conn.execute(
        "SELECT j.id, j.task_name, j.queue_name, j.lock, j.queueing_lock, i.job_id "
        "FROM procrastinate_jobs j, inputs i WHERE i.sha256 = %s",
        (sha,),
    ).fetchall()
    assert len(zeilen) == 1
    kennung, name, queue, lock, queueing_lock, gemerkt = zeilen[0]
    assert (name, queue, lock, queueing_lock) == (
        config.TASK_OCR_DOCUMENT,
        config.QUEUE_OCR,
        sha,
        None,
    )
    assert gemerkt == kennung


async def test_zweites_abschicken_waehrend_der_job_laeuft_legt_keinen_zweiten_an(
    db_conn: psycopg.Connection[Any], int_settings: Settings
) -> None:
    """Waehrend der Job laeuft, steht die Eingabe: Ein zweites Abschicken
    derselben Bytes findet sie und tut nichts. Frueher war der `queueing_lock` in
    diesem Augenblick schon frei."""
    inhalt = PNG_KOPF + b"zweimal waehrend des laufs"
    haengend = ProtokollRunPod()
    haengend.freigabe = asyncio.Event()
    zweites: list[Any] = []

    with OCRClient(db_dsn=int_settings.db_dsn, preflight=False) as klient:
        sha = klient.ocr_start(inhalt)

        async def ablauf() -> bool:
            if not haengend.gestartet.is_set():
                return False
            if not zweites:
                # Der Job laeuft jetzt bei RunPod: dasselbe noch einmal.
                assert _jobs_zu(db_conn, sha)[0].status == "doing"
                zweites.append(klient.ocr_start(inhalt))
                assert len(_jobs_zu(db_conn, sha)) == 1
                assert haengend.freigabe is not None
                haengend.freigabe.set()
            return get_document(db_conn, sha) is not None

        await _daemon_laufen_lassen(int_settings, haengend, bis=ablauf)

    assert zweites[0] == sha
    assert zweites[0].queued is False and zweites[0].status is None
    assert haengend.aufrufe == 1
    zeile = get_document(db_conn, sha)
    assert zeile is not None and zeile.status is DocumentStatus.DONE


def test_gleichzeitiges_abschicken_derselben_bytes_legt_einen_job_an(
    db_conn: psycopg.Connection[Any],
    int_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Zwei Aufrufer schicken gleichzeitig dieselben Bytes: Postgres laesst den
    zweiten am Primaerschluessel von `inputs` auf den ersten warten, und danach
    tut der zweite nichts. Ohne Zeitrennen: Der erste haelt seine Transaktion
    beim Einreihen offen, bis Postgres den zweiten nachweislich warten laesst."""
    import threading

    inhalt = PNG_KOPF + b"gleichzeitig"
    echtes_einreihen = tasks.enqueue_document
    erster_haelt = threading.Event()
    zweiter_wartete: list[bool] = []

    def langsam(app: Any, conn: Any, sha256: str) -> int:
        # Die Eingabe ist abgelegt, aber nicht festgeschrieben. Gehalten wird,
        # bis Postgres den zweiten am Primaerschluessel warten laesst — keine
        # feste Pause, die auf einer langsamen Maschine still verstriche und
        # den Test dann nacheinander statt gleichzeitig laufen liesse.
        erster_haelt.set()
        frist = time.monotonic() + 10
        with psycopg.connect(int_settings.db_dsn, autocommit=True) as beobachter:
            while time.monotonic() < frist:
                wartend = beobachter.execute(
                    "SELECT 1 FROM pg_stat_activity "
                    "WHERE datname = current_database() "
                    "AND wait_event_type = 'Lock' "
                    "AND query LIKE 'INSERT INTO inputs%'"
                ).fetchone()
                if wartend is not None:
                    zweiter_wartete.append(True)
                    break
                time.sleep(0.01)
        return echtes_einreihen(app, conn, sha256)

    monkeypatch.setattr(tasks, "enqueue_document", langsam)
    ergebnisse: dict[str, Any] = {}

    def abschicken(wer: str) -> None:
        with OCRClient(db_dsn=int_settings.db_dsn, preflight=False) as klient:
            ergebnisse[wer] = klient.ocr_start(inhalt)

    erster = threading.Thread(target=abschicken, args=("erster",))
    erster.start()
    assert erster_haelt.wait(10)
    zweiter = threading.Thread(target=abschicken, args=("zweiter",))
    zweiter.start()
    erster.join(20)
    zweiter.join(20)

    assert zweiter_wartete, "der zweite hat nie auf den ersten gewartet"
    assert ergebnisse["erster"].queued is True
    assert ergebnisse["zweiter"].queued is False
    assert ergebnisse["zweiter"].status is None
    sha = ergebnisse["erster"]
    assert len(_jobs_zu(db_conn, sha)) == 1
    anzahl = db_conn.execute("SELECT count(*) FROM inputs").fetchone()
    assert anzahl is not None and anzahl[0] == 1


def test_document_findet_den_job_ueber_inputs_job_id(
    db_conn: psycopg.Connection[Any], int_settings: Settings, erzeuger: Erzeuger
) -> None:
    """`document()` sagt den Zustand des Jobs, dessen Kennung an der Eingabe
    steht, nicht den irgendeines Jobs mit demselben Hash: Ein juengerer, nicht
    verknuepfter Job mit demselben `lock` aendert nichts an der Auskunft."""
    with OCRClient(db_dsn=int_settings.db_dsn, preflight=False) as klient:
        sha = klient.ocr_start(PNG_KOPF + b"ueber die kennung")
        (verknuepft,) = db_conn.execute(
            "SELECT job_id FROM inputs WHERE sha256 = %s", (sha,)
        ).fetchone() or (None,)
        db_conn.execute(
            "UPDATE procrastinate_jobs SET status = 'doing' WHERE id = %s",
            (verknuepft,),
        )
        # Ein juengerer Job desselben Dokuments, NICHT an der Eingabe vermerkt.
        tasks.enqueue_document(erzeuger.app, db_conn, sha)

        stand = klient.document(sha)

    assert stand.phase is Phase.RUNNING
    assert stand.job_id == verknuepft
    assert stand.job_status == "doing"


async def test_ein_treffer_auch_failed_legt_nichts_an(
    db_conn: psycopg.Connection[Any], int_settings: Settings
) -> None:
    """Endet das Dokument `failed`, ist erneutes Abschicken ein Treffer: kein
    Job, die Eingabe bleibt (fuer `retry`), und das Ergebnis unberuehrt."""
    inhalt = PNG_KOPF + b"scheitert"
    scheiternd = ProtokollRunPod(
        lambda nr: {"id": f"job-{nr}", "status": "FAILED", "error": "kaputt"}
    )
    with OCRClient(db_dsn=int_settings.db_dsn, preflight=False) as klient:
        sha = klient.ocr_start(inhalt)

        async def fertig() -> bool:
            return get_document(db_conn, sha) is not None

        await _daemon_laufen_lassen(int_settings, scheiternd, bis=fertig)
        vorher = get_document(db_conn, sha)
        jobs_vorher = len(_jobs_zu(db_conn, sha))

        wieder = klient.ocr_start(inhalt)

    assert vorher is not None and vorher.status is DocumentStatus.FAILED
    assert wieder.queued is False and wieder.status is DocumentStatus.FAILED
    assert len(_jobs_zu(db_conn, sha)) == jobs_vorher
    assert _eingabe_steht(db_conn, sha)
    assert get_document(db_conn, sha) == vorher


async def test_daemon_prozess_haelt_genau_eine_app(
    db_conn: psycopg.Connection[Any],
    int_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Waehrend `daemon.run` laeuft, entsteht genau EINE Procrastinate-App.

    Jede weitere App im Daemon-Prozess benennt Procrastinates eingebauten Task
    `remove_old_jobs` um. Gezaehlt werden die Aufrufe des Konstruktors, nicht die
    Tasks einer App: Eine zweite App ohne angemeldete Tasks fiele sonst durch.
    """
    gebaut: list[procrastinate.App] = []
    echt = procrastinate.App.__init__

    def zaehlend(self: procrastinate.App, *args: Any, **kwargs: Any) -> None:
        gebaut.append(self)
        echt(self, *args, **kwargs)

    monkeypatch.setattr(procrastinate.App, "__init__", zaehlend)

    async def daemon_lebt() -> bool:
        return bool(db.worker_heartbeats(db_conn))

    await _daemon_laufen_lassen(int_settings, ProtokollRunPod(), bis=daemon_lebt)

    assert len(gebaut) == 1


async def test_pool_des_daemons_ersetzt_eine_beendete_verbindung(
    db_conn: psycopg.Connection[Any], int_settings: Settings
) -> None:
    """Nach einem Postgres-Neustart schreibt der Daemon nicht ueber eine tote
    Verbindung: Der Pool prueft beim Ausleihen und ersetzt sie.

    Der Neustart wird nachgestellt, indem der Server die Verbindung beendet, die
    der Pool gerade verwahrt. Ohne die Pruefung gaebe der Pool genau diese
    Verbindung heraus, und der erste Zugriff darauf scheiterte.
    """
    mittel = tasks.worker_context_resources(int_settings)
    pool = mittel["pool"]
    await pool.open(wait=True)
    try:
        async with pool.connection() as verbindung:
            alt = verbindung.info.backend_pid
        db_conn.execute("SELECT pg_terminate_backend(%s)", (alt,))
        # Der Server braucht einen Augenblick, bis die Verbindung wirklich weg
        # ist; ohne das bekaeme der Pool sie noch lebendig zurueck.
        ende = time.monotonic() + 5.0
        while time.monotonic() < ende:
            noch_da = db_conn.execute(
                "SELECT 1 FROM pg_stat_activity WHERE pid = %s", (alt,)
            ).fetchone()
            if noch_da is None:
                break
            await asyncio.sleep(0.02)

        async with pool.connection() as verbindung:
            zeile = await (await verbindung.execute("SELECT 1")).fetchone()
            assert zeile == (1,)
            assert verbindung.info.backend_pid != alt
    finally:
        await pool.close()
        await mittel["runpod"].aclose()


# --------------------------------------------------------------------------
# Herunterfahren — der Pfad, auf dem Jobs `aborted` enden
# --------------------------------------------------------------------------


async def test_daemon_faengt_die_signale_selbst_und_gibt_sie_wieder_frei(
    db_conn: psycopg.Connection[Any], int_settings: Settings
) -> None:
    """Waehrend der Daemon laeuft, haengen SIGTERM und SIGINT an SEINER Schleife
    — danach nicht mehr.

    Warum das eigens geprueft wird: Beide Worker laufen mit
    `install_signal_handlers=False`, weil zwei Worker nicht beide Handler
    einrichten duerfen — der zweite ueberschriebe den ersten, und beim Beenden
    wuerde einer nie benachrichtigt. Der Prozess faengt die Signale deshalb
    selbst. Faellt diese Einrichtung aus, merkt man es NICHT im Betrieb: Der
    Daemon laeuft, arbeitet, und reagiert nur auf Strg-C nicht mehr — bis jemand
    ihn hart abschiesst und die laufenden Dokumente mitnimmt.

    Geprueft ohne ein echtes Signal zu senden: `add_signal_handler` setzt am
    Prozess einen eigenen Handler, `signal.getsignal` gibt ihn zurueck, und nach
    `remove_signal_handler` steht dort wieder die Voreinstellung. Ein echtes
    SIGTERM waere hier gefaehrlich — kaeme es einen Wimpernschlag nach dem
    Aufraeumen an, beendete es den Testlauf.
    """
    import signal as signal_modul

    voreinstellung = signal_modul.getsignal(signal_modul.SIGTERM)
    gesehen: list[Any] = []

    async def haengt_am_signal() -> bool:
        gesehen.append(signal_modul.getsignal(signal_modul.SIGTERM))
        return gesehen[-1] is not voreinstellung

    await _daemon_laufen_lassen(
        int_settings, ProtokollRunPod(), bis=haengt_am_signal
    )

    assert gesehen[-1] is not voreinstellung, (
        "Der Daemon hat SIGTERM nicht an sich gezogen — Strg-C wuerde ihn dann "
        "hart beenden statt sauber herunterzufahren."
    )
    # Und danach ist der Prozess wieder so, wie er war. Ein Daemon, der seine
    # Handler stehen laesst, veraendert einen Prozess, dem er nicht gehoert.
    assert signal_modul.getsignal(signal_modul.SIGTERM) is voreinstellung


@pytest.mark.parametrize("zweites_signal", ["nach_der_nachfrist", "in_der_nachfrist"])
async def test_zweites_signal_beendet_den_daemon_auch_wenn_das_failed_schreiben_haengt(
    db_conn: psycopg.Connection[Any],
    int_settings: Settings,
    erzeuger: Erzeuger,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    zweites_signal: str,
) -> None:
    """Haengt das Schreiben von `failed` an einer blockierten Datenbank, beendet
    ein zweites Signal den Daemon trotzdem (Mineroo#27; `daemon.run`: „Ein
    zweites Signal waehrend des Herunterfahrens bricht sofort ab").

    Zwei Lagen, weil Procrastinate sie verschieden behandelt:

        Nach der Nachfrist: Das erste Signal fuehrt nach der Nachfrist zum
        Abbruch des Jobs, sein Fehlerzweig schreibt, und das Schreiben haengt.
        Das zweite Signal erreicht den Job als zweiter Abbruch.

        In der Nachfrist: Der Job haengt schon im Schreiben, als die Signale
        kommen. Procrastinates Herunterfahren kann dabei unterbrochen werden,
        bevor es die Jobs abbricht; dann bleibt der Job-Task nach der Rueckkehr
        von `run` zurueck und muss enden, wenn ihn der Abbau des Prozesses
        (`asyncio.run`) abbricht.

    Hier werden echte Signale an den eigenen Prozess geschickt, aber nur,
    waehrend der Daemon sie haelt: vom Start bis zum Ende seines
    Herunterfahrens, und das blockierte Schreiben haelt ihn genau dort.
    """
    import os
    import signal as signal_modul

    einstellungen = dataclasses.replace(int_settings, max_attempts=1)
    sha = _anlegen(db_conn, tmp_path, "z.png")
    erzeuger.einreihen(sha)

    schreiben_begonnen = asyncio.Event()
    aufraeumen = asyncio.Event()

    async def haengendes_schreiben(*_a: Any, **_k: Any) -> Any:
        schreiben_begonnen.set()
        await aufraeumen.wait()
        raise psycopg.OperationalError("blockiert")

    monkeypatch.setattr(db, "write_terminal_async", haengendes_schreiben)
    if zweites_signal == "nach_der_nachfrist":
        runpod = ProtokollRunPod()
        runpod.freigabe = asyncio.Event()  # RunPod rechnet, bis abgebrochen wird
    else:
        runpod = ProtokollRunPod(lambda nr: {"id": "j", "status": "FAILED"})

    voreinstellung = signal_modul.getsignal(signal_modul.SIGTERM)
    echte_mittel = tasks.worker_context_resources

    def mit_doppel(s: Settings) -> dict[str, Any]:
        mittel = echte_mittel(s)
        mittel["runpod"] = runpod
        return mittel

    monkeypatch.setattr(tasks, "worker_context_resources", mit_doppel)
    lauf = asyncio.create_task(daemon.run(einstellungen))
    try:
        async with asyncio.timeout(15.0):
            if zweites_signal == "nach_der_nachfrist":
                await runpod.gestartet.wait()
            else:
                await schreiben_begonnen.wait()
            while signal_modul.getsignal(signal_modul.SIGTERM) is voreinstellung:
                await asyncio.sleep(0.01)
        os.kill(os.getpid(), signal_modul.SIGTERM)
        if zweites_signal == "nach_der_nachfrist":
            async with asyncio.timeout(15.0):
                await schreiben_begonnen.wait()
        else:
            await asyncio.sleep(0.2)
        assert not lauf.done()
        os.kill(os.getpid(), signal_modul.SIGTERM)

        # `await lauf` und nicht `asyncio.wait`: Ein Daemon, der beim
        # Herunterfahren mit einem Fehler endet, soll den Test scheitern lassen
        # und nicht als sauberes Ende durchgehen.
        async with asyncio.timeout(8.0):
            await lauf

        # Wie `asyncio.run` beim Abbau des Prozesses: Was dann noch laeuft (in
        # der Nachfrist bleibt der Job-Task zurueck), wird EINMAL abgebrochen
        # und muss danach enden, obwohl das Schreiben weiter haengt. Sonst
        # hinge der Prozess nach der Rueckkehr von `run`.
        uebrig = [
            aufgabe
            for aufgabe in asyncio.all_tasks()
            if aufgabe is not asyncio.current_task() and not aufgabe.done()
        ]
        for aufgabe in uebrig:
            aufgabe.cancel()
        if uebrig:
            async with asyncio.timeout(5.0):
                await asyncio.wait(uebrig)
    finally:
        aufraeumen.set()
        if not lauf.done():
            lauf.cancel()
            await asyncio.wait([lauf])

    assert get_document(db_conn, sha) is None


async def test_gestorbener_worker_beendet_den_daemon_mit_fehler(
    db_conn: psycopg.Connection[Any], int_settings: Settings
) -> None:
    """Stirbt EIN Worker an einem Fehler, endet `run` mit diesem Fehler.

    Der Fall, gegen den das steht, ist der unangenehmste im Betrieb: Ein Prozess,
    der sich beendet hat und Exit-Code 0 meldet. Man bemerkt ihn erst am leeren
    Bestand — Stunden spaeter, wenn nichts fertig geworden ist.

    Geprueft, indem `run_worker_async` beim zweiten Aufruf wirft; der erste
    Worker laeuft dann noch, der zweite nicht.

    Der zweite stirbt ERST, wenn der erste angemeldet ist (sein Lebenszeichen
    steht in der Datenbank). Stuerbe er im selben Augenblick, in dem der erste
    seine allerersten Abfragen stellt, trafe das Abbrechen des ersten genau in die
    Pruefung einer Pool-Verbindung — und die verschluckt psycopg_pool 3.3.0
    (`CLIENT_EXCEPTIONS` enthaelt `CancelledError`): Der Worker liefe weiter, das
    Herunterfahren haengte. Das ist ein Zeitfenster von Millisekunden und kein
    Fall, den dieser Test prueft; siehe `tasks.worker_context_resources`.
    """
    from procrastinate import App

    echt = App.run_worker_async
    zaehler = {"n": 0}

    async def manchmal_kaputt(self: Any, **optionen: Any) -> None:
        zaehler["n"] += 1
        if zaehler["n"] == 2:
            while not db.worker_heartbeats(db_conn):
                await asyncio.sleep(0.02)
            raise RuntimeError("dieser Worker ist gestorben")
        await echt(self, **optionen)

    App.run_worker_async = manchmal_kaputt  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError, match="gestorben"):
            await daemon.run(int_settings)
    finally:
        App.run_worker_async = echt  # type: ignore[method-assign]


@pytest.mark.parametrize("fehlt", ["endpoint_id", "api_key"])
async def test_daemon_ohne_endpoint_oder_schluessel_startet_nicht_und_laesst_die_jobs_liegen(
    db_conn: psycopg.Connection[Any],
    int_settings: Settings,
    fehlt: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`Settings.load` laesst Endpoint und Schluessel fehlen, damit Abschicken und
    Lesen ohne sie gehen. Der Daemon braucht beide: Startete er ohne, nahme er
    jeden Job an, scheiterte beim Absenden an der Konfiguration und schriebe jedes
    Dokument endgueltig als `failed`. Er verlangt sie deshalb VOR dem Start der
    Worker, auch fuer Aufrufer aus Python, und die Jobs bleiben, wie sie sind."""
    with OCRClient(
        db_dsn=int_settings.db_dsn, api_key="erfunden", preflight=False
    ) as klient:
        sha = klient.ocr_start(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"ohne " + fehlt.encode())
    ohne = (
        dataclasses.replace(int_settings, endpoint_id=None)
        if fehlt == "endpoint_id"
        else dataclasses.replace(int_settings, api_key=None)
    )

    # RunPod darf dieser Test unter keinen Umstaenden erreichen, auch nicht, wenn
    # die Pruefung fehlt: Die Betriebsmittel (darunter der RunPod-Zugang) duerfen
    # gar nicht erst entstehen, und jeder HTTP-Versand scheitert laut.
    def keine_betriebsmittel(_s: Settings) -> dict[str, Any]:
        raise AssertionError("der Daemon hat seine Betriebsmittel gebaut")

    async def kein_netz(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("eine HTTP-Anfrage wollte hinaus")

    monkeypatch.setattr(tasks, "worker_context_resources", keine_betriebsmittel)
    monkeypatch.setattr(httpx.AsyncClient, "send", kein_netz)

    with pytest.raises(ConfigurationError):
        await asyncio.wait_for(daemon.run(ohne), timeout=10)

    assert get_document(db_conn, sha) is None
    assert _eingabe_steht(db_conn, sha)
    assert [job.status for job in _jobs_zu(db_conn, sha)] == ["todo"]
    assert db.worker_heartbeats(db_conn) == []


async def test_daemon_auf_der_alten_tabelle_startet_nicht_und_laesst_die_jobs_liegen(
    db_conn: psycopg.Connection[Any],
    int_settings: Settings,
    alte_documents_form: None,
    erzeuger: Erzeuger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nach einem Paket-Update ohne `setup` steht die alte Tabelle noch da, mit
    offenen `new`-Zeilen und ihren Jobs. Startete der Daemon, scheiterte jeder
    dieser Jobs beim Nachsehen an `DocumentStatus('new')` und endete ohne
    Ergebnis. Er prueft das Schema deshalb VOR dem Start der Worker."""
    sha = "c" * 64
    db_conn.execute(
        "INSERT INTO documents (sha256, endpoint_id, mime, size_bytes) "
        "VALUES (%s, 'e', 'image/png', 1)",
        (sha,),
    )
    db.insert_input(db_conn, sha256=sha, content=b"alt")
    assert erzeuger.einreihen(sha)

    def keine_betriebsmittel(_s: Settings) -> dict[str, Any]:
        raise AssertionError("der Daemon hat seine Betriebsmittel gebaut")

    monkeypatch.setattr(tasks, "worker_context_resources", keine_betriebsmittel)

    with pytest.raises(DatabaseError, match="setup"):
        await asyncio.wait_for(daemon.run(int_settings), timeout=10)

    assert [job.status for job in _jobs_zu(db_conn, sha)] == ["todo"]
    assert _eingabe_steht(db_conn, sha)
    assert db.worker_heartbeats(db_conn) == []


async def test_die_schemapruefung_beim_start_haelt_die_ereignisschleife_nicht_an(
    int_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Die Pruefung ist synchrones Postgres. Liefe sie direkt in der Koroutine,
    hielte eine haengende Datenbank die ganze Schleife an, und kein
    `wait_for` und kein Abbruch kaemen durch."""

    import threading

    freigabe = threading.Event()
    zurueck = threading.Event()

    def haengt(_conn: Any) -> None:
        freigabe.wait(10)
        zurueck.set()

    monkeypatch.setattr(db, "check_schema", haengt)
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(daemon.run(int_settings), timeout=0.1)
        # Ohne Uhr: Der Abbruch kam durch, waehrend die Pruefung noch haengt.
        assert not zurueck.is_set()
    finally:
        freigabe.set()
    assert zurueck.wait(10)


async def test_die_schemapruefung_beim_start_hat_eine_frist(
    int_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ein abgebrochener Start laesst den Faden der Pruefung weiterlaufen, bis die
    Datenbank antwortet; ohne Frist hinge er (und `asyncio.run` beim Beenden)
    so lange wie die Datenbank. Verbindungsaufbau und Abfragen sind begrenzt."""
    gesehen: dict[str, Any] = {}
    echtes_verbinden = db.connect

    def verbinden(dsn: str) -> Any:
        gesehen["connect_timeout"] = psycopg.conninfo.conninfo_to_dict(dsn).get(
            "connect_timeout"
        )
        return echtes_verbinden(dsn)

    def pruefen(conn: Any) -> None:
        zeile = conn.execute("SHOW statement_timeout").fetchone()
        gesehen["statement_timeout"] = zeile[0] if zeile else None
        raise DatabaseError("Abbruch nach dem Nachsehen")

    monkeypatch.setattr(db, "connect", verbinden)
    monkeypatch.setattr(db, "check_schema", pruefen)
    with pytest.raises(DatabaseError):
        await asyncio.wait_for(daemon.run(int_settings), timeout=10)
    assert gesehen["connect_timeout"] == str(daemon.SCHEMA_PRUEFUNG_FRIST_SEKUNDEN)
    assert gesehen["statement_timeout"] == f"{daemon.SCHEMA_PRUEFUNG_FRIST_SEKUNDEN}s"


def test_main_uebersetzt_in_exit_codes(int_settings: Settings) -> None:
    """`main` gibt 0 bei ordentlichem Ende und 1 bei einem Fehler.

    Der synchrone Einstieg der Kommandozeile. Ein Abbruch von Hand ist dabei
    ausdruecklich ein ORDENTLICHES Ende und kein Fehler: Wer Strg-C drueckt,
    wollte das.
    """
    import mineru_ocr.daemon as daemon_modul

    echt = daemon_modul.run

    async def sofort_fertig(settings: Settings, *, queues: Any = None) -> None:
        return None

    async def abgebrochen(settings: Settings, *, queues: Any = None) -> None:
        raise KeyboardInterrupt()

    async def kaputt(settings: Settings, *, queues: Any = None) -> None:
        raise RuntimeError("Datenbank weg")

    try:
        daemon_modul.run = sofort_fertig
        assert daemon_modul.main(int_settings) == 0

        daemon_modul.run = abgebrochen
        assert daemon_modul.main(int_settings) == 0

        daemon_modul.run = kaputt
        assert daemon_modul.main(int_settings) == 1
    finally:
        daemon_modul.run = echt


def test_zwei_prozess_rueckfall_ist_wirklich_vorbereitet(
    int_settings: Settings,
) -> None:
    """Mit ausdruecklichen Queues startet GENAU EIN Worker fuer genau diese.

    Das ist der dokumentierte Rueckfall fuer den Fall, dass die
    Zwei-Koroutinen-Konstruktion nicht traegt (Feature-Verifikation Punkt 1).
    Sie traegt — aber ein Rueckfall, der nie geprueft wurde, ist keiner, sondern
    eine Absichtserklaerung. Genau dann braucht man ihn: wenn das Uebliche
    gerade nicht geht.
    """
    from mineru_ocr.daemon import _worker_optionen

    beide = _worker_optionen(int_settings, None, {"settings": int_settings})
    assert len(beide) == 2
    assert [o["queues"] for o in beide] == [[config.QUEUE_OCR], [config.QUEUE_MAINTENANCE]]
    # Der Waechter bekommt genau EINEN Platz; die OCR-Queue die eingestellte Zahl.
    assert beide[0]["concurrency"] == int_settings.ocr_concurrency
    assert beide[1]["concurrency"] == 1
    # KEINER der beiden richtet Signal-Handler ein — das tut der Prozess selbst.
    assert all(o["install_signal_handlers"] is False for o in beide)
    # Erfolgreiche Jobs loescht der Worker; gescheiterte bleiben fuer `retry`.
    assert all(o["delete_jobs"] == "successful" for o in beide)

    einzeln = _worker_optionen(
        int_settings, [config.QUEUE_MAINTENANCE], {"settings": int_settings}
    )
    assert len(einzeln) == 1
    assert einzeln[0]["queues"] == [config.QUEUE_MAINTENANCE]


# --------------------------------------------------------------------------
# Abbruch bei RunPod — aufgegebene und abgebrochene Jobs kosten nichts mehr
# --------------------------------------------------------------------------


class ErsatzRunPodHttp:
    """RunPod als HTTP-Gegenstelle: Hier endet das Netz, nicht am Python-Objekt.

    Anders als `ProtokollRunPod` ersetzt dieses Doppel nicht den ganzen Zugang,
    sondern nur den Transport unter einem ECHTEN `RunPodClient`. Nur so laeuft die
    Zeitbudget-Logik der Warteschleife mit und ist durch den echten Daemon
    pruefbar. Es schreibt jede Anfrage mit; `verlauf` ist die Reihenfolge, in der
    RunPod sie sah.
    """

    def __init__(self, status_je_abfrage: Any) -> None:
        # Bekommt (Job-Kennung, laufende Nummer der Abfrage dieses Jobs,
        # Sekunden seit der ersten Abfrage) und liefert die Statusantwort.
        self._status = status_je_abfrage
        self.verlauf: list[tuple[str, str]] = []
        self._jobs = 0
        self._abfragen: dict[str, int] = {}
        self._erste_abfrage: dict[str, float] = {}

    def __call__(self, anfrage: Any) -> Any:
        pfad = anfrage.url.path
        if anfrage.url.host == "api.runpod.io":
            text = anfrage.content.decode()
            if "imageName" in text:
                return httpx.Response(200, json={"data": {"myself": {"endpoints": []}}})
            return httpx.Response(
                200,
                json={
                    "data": {
                        "myself": {
                            "clientBalance": 5.0,
                            "currentSpendPerHr": 0.0,
                            "spendLimit": 80,
                        }
                    }
                },
            )
        if pfad.endswith("/run"):
            self._jobs += 1
            kennung = f"job-{self._jobs}"
            self.verlauf.append(("run", kennung))
            return httpx.Response(200, json={"id": kennung, "status": "IN_QUEUE"})
        if "/cancel/" in pfad:
            kennung = pfad.rsplit("/", 1)[1]
            self.verlauf.append(("cancel", kennung))
            return httpx.Response(200, json={"id": kennung, "status": "CANCELLED"})
        kennung = pfad.rsplit("/", 1)[1]
        self.verlauf.append(("status", kennung))
        nummer = self._abfragen.get(kennung, 0)
        self._abfragen[kennung] = nummer + 1
        erste = self._erste_abfrage.setdefault(kennung, time.monotonic())
        antwort: dict[str, Any] = self._status(
            kennung, nummer, time.monotonic() - erste
        )
        return httpx.Response(200, json=antwort)

    def ereignisse(self, art: str) -> list[str]:
        return [kennung for a, kennung in self.verlauf if a == art]


def _runpod_ueber_http(
    int_settings: Settings, gegenstelle: ErsatzRunPodHttp
) -> Any:
    echte = dataclasses.replace(
        int_settings, api_key="erfundener-schluessel-nicht-echt"
    )
    return RunPodClient(
        echte,
        http=httpx.AsyncClient(transport=httpx.MockTransport(gegenstelle)),
    )


async def test_herunterfahren_bricht_den_laufenden_runpod_job_ab(
    db_conn: psycopg.Connection[Any],
    int_settings: Settings,
    tmp_path: Path,
    erzeuger: Erzeuger,
) -> None:
    """Faehrt der Daemon herunter, waehrend ein Job bei RunPod laeuft, geht
    `/cancel` fuer genau diesen Job hinaus, bevor der Daemon zurueckkehrt.

    Sonst liefe der Job bei RunPod weiter und kostete Geld, ohne dass ihn noch
    jemand abholt. Der ECHTE Daemon laeuft, nur RunPod ist ersetzt.
    """
    sha = _anlegen(db_conn, tmp_path, "abbruch.png")
    erzeuger.einreihen(sha)
    gegenstelle = ErsatzRunPodHttp(lambda job, nr, sek: {"id": job, "status": "IN_PROGRESS"})
    runpod = _runpod_ueber_http(int_settings, gegenstelle)

    async def job_laeuft() -> bool:
        return "job-1" in gegenstelle.ereignisse("status")

    try:
        await _daemon_laufen_lassen(int_settings, runpod, bis=job_laeuft)
    finally:
        await runpod.aclose()

    assert gegenstelle.ereignisse("cancel") == ["job-1"]
    # Der Abbruch kam NACH dem Absenden und nach mindestens einer Abfrage.
    vor_dem_abbruch = gegenstelle.verlauf[: gegenstelle.verlauf.index(("cancel", "job-1"))]
    assert ("run", "job-1") in vor_dem_abbruch
    assert ("status", "job-1") in vor_dem_abbruch
    assert vor_dem_abbruch.index(("run", "job-1")) < vor_dem_abbruch.index(
        ("status", "job-1")
    )


async def test_aufgegebener_job_wird_vor_der_wiederholung_abgebrochen_durch_den_daemon(
    db_conn: psycopg.Connection[Any],
    int_settings: Settings,
    tmp_path: Path,
    erzeuger: Erzeuger,
) -> None:
    """Reisst ein Job sein Zeitbudget, geht `/cancel` fuer ihn hinaus, BEVOR der
    zweite Versuch ein neues `/run` schickt."""
    sha = _anlegen(db_conn, tmp_path, "budget.png")
    erzeuger.einreihen(sha)
    knapp = dataclasses.replace(int_settings, job_timeout_seconds=0.3)
    gegenstelle = ErsatzRunPodHttp(lambda job, nr, sek: {"id": job, "status": "IN_PROGRESS"})
    runpod = _runpod_ueber_http(knapp, gegenstelle)

    async def zweiter_versuch_hat_gesendet() -> bool:
        return len(gegenstelle.ereignisse("run")) >= 2

    try:
        await _daemon_laufen_lassen(int_settings, runpod, bis=zweiter_versuch_hat_gesendet)
    finally:
        await runpod.aclose()

    verlauf = gegenstelle.verlauf
    assert ("cancel", "job-1") in verlauf
    assert verlauf.index(("cancel", "job-1")) < verlauf.index(("run", "job-2"))


async def test_lange_in_der_warteschlange_ist_kein_grund_aufzugeben(
    db_conn: psycopg.Connection[Any],
    int_settings: Settings,
    tmp_path: Path,
    erzeuger: Erzeuger,
) -> None:
    """Ein Job, der dreimal laenger in der Schlange steht, als das Zeitbudget
    erlaubt, wird nicht abgebrochen und nicht neu abgesendet, sondern fertig.

    Ohne das stuende der Job hinter einem grossen Lauf in einer Schleife aus
    Abbruch und Neu-Einreihen, und jeder Durchgang kostete neu.
    """
    sha = _anlegen(db_conn, tmp_path, "schlange.png")
    erzeuger.einreihen(sha)
    knapp = dataclasses.replace(int_settings, job_timeout_seconds=0.3)

    def status(job: str, nr: int, sekunden: float) -> dict[str, Any]:
        if sekunden < 0.9:
            return {"id": job, "status": "IN_QUEUE"}
        return {**_fertig(), "id": job}

    gegenstelle = ErsatzRunPodHttp(status)
    runpod = _runpod_ueber_http(knapp, gegenstelle)

    async def fertig() -> bool:
        zeile = get_document(db_conn, sha)
        return zeile is not None

    try:
        await _daemon_laufen_lassen(int_settings, runpod, bis=fertig)
    finally:
        await runpod.aclose()

    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.DONE
    assert gegenstelle.ereignisse("run") == ["job-1"]
    assert gegenstelle.ereignisse("cancel") == []


# --------------------------------------------------------------------------
# `retry` auf Procrastinates Weg
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "endzustand",
    [DocumentStatus.DONE, DocumentStatus.EMPTY, DocumentStatus.INPUT_ERROR],
)
async def test_retry_tut_nichts_bei_einem_gueltigen_ergebnis(
    db_conn: psycopg.Connection[Any],
    async_conn: psycopg.AsyncConnection[Any],
    int_settings: Settings,
    endzustand: DocumentStatus,
) -> None:
    """Steht ein Ergebnis `done`, `empty` oder `input_error` da, gibt es nichts zu
    wiederholen: kein Job entsteht, das Ergebnis bleibt."""
    with OCRClient(db_dsn=int_settings.db_dsn, preflight=False) as klient:
        sha = klient.ocr_start(PNG_KOPF + b"hat ein ergebnis")
        await db.write_terminal_async(
            async_conn,
            sha256=sha,
            status=endzustand,
            raw={},
            endpoint_id="testendpoint",
        )
        jobs_vorher = _jobs_zu(db_conn, sha)

        ausgang = klient.retry(sha)

    assert ausgang is RetryOutcome.HAS_RESULT
    assert _jobs_zu(db_conn, sha) == jobs_vorher
    zeile = get_document(db_conn, sha)
    assert zeile is not None and zeile.status is endzustand


@pytest.mark.parametrize("job_zustand", ["todo", "doing"])
def test_retry_tut_nichts_bei_einem_job_der_unterwegs_ist(
    db_conn: psycopg.Connection[Any], int_settings: Settings, job_zustand: str
) -> None:
    """Wartet oder laeuft der Job der Eingabe, ist das Dokument unterwegs: `retry`
    setzt ihn nicht zurueck und legt keinen zweiten an. Frueher setzte es auch
    einen laufenden Job zurueck, und zweimal `retry` liess ein Dokument doppelt
    laufen."""
    with OCRClient(db_dsn=int_settings.db_dsn, preflight=False) as klient:
        sha = klient.ocr_start(PNG_KOPF + b"unterwegs")
        db_conn.execute(
            "UPDATE procrastinate_jobs SET status = %s WHERE lock = %s",
            (job_zustand, sha),
        )
        vorher = _jobs_zu(db_conn, sha)

        ausgang = klient.retry(sha)
        zweimal = klient.retry(sha)

    assert ausgang is RetryOutcome.UNDERWAY
    assert zweimal is RetryOutcome.UNDERWAY
    assert _jobs_zu(db_conn, sha) == vorher
    assert get_document(db_conn, sha) is None


async def test_retry_setzt_einen_gescheiterten_job_zurueck_und_ein_warten_anderswo_bekommt_das_neue_ergebnis(
    db_conn: psycopg.Connection[Any], int_settings: Settings
) -> None:
    """Endet das Dokument `failed` mit Job `failed`, nimmt `retry` erst die
    `failed`-Zeile weg und setzt dann DENSELBEN Job mit `retry_job_by_id` zurueck:
    kein zweiter Job, der Versuchszaehler zaehlt weiter, Procrastinate vermerkt
    `retried`. Danach ist das Dokument offen, und ein Warten in einem anderen
    Zugang (eigene Verbindung, eigener Client) bekommt das neue Ergebnis, nicht das
    alte `failed`."""
    import threading

    inhalt = PNG_KOPF + b"scheitert erst"
    scheiternd = ProtokollRunPod(
        lambda nr: {"id": f"job-{nr}", "status": "FAILED", "error": "kaputt"}
    )
    with OCRClient(db_dsn=int_settings.db_dsn, preflight=False) as klient:
        sha = klient.ocr_start(inhalt)

        async def gescheitert() -> bool:
            return get_document(db_conn, sha) is not None

        await _daemon_laufen_lassen(int_settings, scheiternd, bis=gescheitert)
        (vorher,) = _jobs_zu(db_conn, sha)
        assert vorher.status == "failed"

        ausgang = klient.retry(sha)

        assert ausgang is RetryOutcome.RESET
        assert get_document(db_conn, sha) is None
        (danach,) = _jobs_zu(db_conn, sha)
        assert danach.id == vorher.id
        assert danach.status == "todo"
        assert danach.attempts == vorher.attempts + 1
        vermerkt = db_conn.execute(
            "SELECT count(*) FROM procrastinate_events "
            "WHERE job_id = %s AND type = 'retried'",
            (vorher.id,),
        ).fetchone()
        assert vermerkt is not None and vermerkt[0] == 1

    gewartet: list[Any] = []

    def anderswo_warten() -> None:
        with OCRClient(db_dsn=int_settings.db_dsn, preflight=False) as anderer:
            huelle = anderer.wait(sha, timeout=20)
            gewartet.append((huelle.status, huelle.raw["markdown"]))

    warter = threading.Thread(target=anderswo_warten)
    warter.start()
    gelingend = ProtokollRunPod(lambda nr: _fertig("Beim zweiten Mal"))

    async def fertig() -> bool:
        return not warter.is_alive()

    await _daemon_laufen_lassen(int_settings, gelingend, bis=fertig, zeit=20)
    warter.join(5)

    assert gelingend.aufrufe == 1
    assert gewartet == [(DocumentStatus.DONE, "Beim zweiten Mal")]


@pytest.mark.parametrize(
    "lage", ["aborted", "cancelled", "failed_mit_abbruchwunsch", "ohne_job"]
)
async def test_retry_reiht_einen_neuen_job_ein_wo_procrastinate_nicht_wieder_vorlegt(
    db_conn: psycopg.Connection[Any],
    async_conn: psycopg.AsyncConnection[Any],
    int_settings: Settings,
    lage: str,
) -> None:
    """`aborted`, `cancelled`, `failed` mit stehendem `abort_requested` (das
    Zuruecksetzen scheiterte dort am CHECK `check_not_todo_abort_requested`) oder
    kein Job: Procrastinate legt sie nicht wieder vor. `retry` reiht in einer
    Transaktion einen neuen Job ein, nimmt eine `failed`-Zeile weg und schreibt die
    neue Kennung in `inputs.job_id`; der Daemon arbeitet ihn ab."""
    with OCRClient(db_dsn=int_settings.db_dsn, preflight=False) as klient:
        sha = klient.ocr_start(PNG_KOPF + f"neu einreihen {lage}".encode())
        alt = _job_der_eingabe(db_conn, sha)
        assert alt is not None and alt.id is not None
        if lage == "ohne_job":
            db_conn.execute("DELETE FROM procrastinate_jobs WHERE id = %s", (alt.id,))
        elif lage == "failed_mit_abbruchwunsch":
            db_conn.execute(
                "UPDATE procrastinate_jobs SET status = 'failed', "
                "abort_requested = true WHERE id = %s",
                (alt.id,),
            )
            await db.write_terminal_async(
                async_conn,
                sha256=sha,
                status=DocumentStatus.FAILED,
                raw={},
                endpoint_id="testendpoint",
                error="abgebrochen",
            )
        else:
            db_conn.execute(
                "UPDATE procrastinate_jobs SET status = %s WHERE id = %s",
                (lage, alt.id),
            )

        ausgang = klient.retry(sha)

    assert ausgang is RetryOutcome.REQUEUED
    assert get_document(db_conn, sha) is None
    neu = _job_der_eingabe(db_conn, sha)
    assert neu is not None and neu.id != alt.id
    assert (neu.status, neu.queue, neu.lock, neu.task_name) == (
        "todo",
        config.QUEUE_OCR,
        sha,
        config.TASK_OCR_DOCUMENT,
    )

    async def fertig() -> bool:
        return get_document(db_conn, sha) is not None

    await _daemon_laufen_lassen(int_settings, ProtokollRunPod(), bis=fertig)
    zeile = get_document(db_conn, sha)
    assert zeile is not None and zeile.status is DocumentStatus.DONE


def test_retry_ohne_eingabe_meldet_es_und_aendert_nichts(
    db_conn: psycopg.Connection[Any], int_settings: Settings
) -> None:
    """Ein `failed`-Ergebnis ohne Eingabe (gibt es nur, wenn jemand von Hand
    geloescht hat) laesst sich nicht wiederholen: Ein neuer Job faende nichts zu
    lesen und ersetzte das Ergebnis durch einen Eingabefehler. `retry` sagt es und
    laesst Ergebnis und Jobs, wie sie sind."""
    sha = "f" * 64
    db_conn.execute(
        "INSERT INTO documents (sha256, status, endpoint_id, error) "
        "VALUES (%s, 'failed', 'testendpoint', 'kaputt')",
        (sha,),
    )
    with OCRClient(db_dsn=int_settings.db_dsn, preflight=False) as klient:
        with pytest.raises(DatabaseError, match="Eingabe"):
            klient.retry(sha)

    zeile = get_document(db_conn, sha)
    assert zeile is not None and zeile.status is DocumentStatus.FAILED
    assert _zaehlen_jobs(db_conn) == 0


def _zaehlen_jobs(conn: psycopg.Connection[Any]) -> int:
    zeile = conn.execute("SELECT count(*) FROM procrastinate_jobs").fetchone()
    assert zeile is not None
    return int(zeile[0])


async def test_retry_all_nimmt_ohne_angabe_jedes_dokument_ohne_gueltiges_ergebnis(
    db_conn: psycopg.Connection[Any],
    async_conn: psycopg.AsyncConnection[Any],
    int_settings: Settings,
) -> None:
    """Ohne Angabe waehlt `retry_all` selbst aus: jedes Dokument ohne gueltiges
    Ergebnis, also jede stehende Eingabe — gescheiterte wie offene, deren Job
    abgesagt ist —, und behandelt jedes wie `retry`. Ein fertiges Dokument hat
    keine Eingabe mehr und kommt nicht vor; mit ausdruecklicher Angabe meldet es
    `HAS_RESULT`."""
    with OCRClient(db_dsn=int_settings.db_dsn, preflight=False) as klient:
        fertig = klient.ocr_start(PNG_KOPF + b"alle fertig")
        await db.write_terminal_async(
            async_conn, sha256=fertig, status=DocumentStatus.DONE, raw={},
            endpoint_id="testendpoint",
        )
        laeuft = klient.ocr_start(PNG_KOPF + b"alle laeuft")
        gescheitert = klient.ocr_start(PNG_KOPF + b"alle gescheitert")
        await db.write_terminal_async(
            async_conn, sha256=gescheitert, status=DocumentStatus.FAILED, raw={},
            endpoint_id="testendpoint",
        )
        abgesagt = klient.ocr_start(PNG_KOPF + b"alle abgesagt")
        for sha, zustand in ((laeuft, "doing"), (gescheitert, "failed"), (abgesagt, "cancelled")):
            db_conn.execute(
                "UPDATE procrastinate_jobs SET status = %s WHERE lock = %s",
                (zustand, sha),
            )

        alle = klient.retry_all()
        genannt = klient.retry_all([fertig])

    assert alle == {
        laeuft: RetryOutcome.UNDERWAY,
        gescheitert: RetryOutcome.RESET,
        abgesagt: RetryOutcome.REQUEUED,
    }
    assert genannt == {fertig: RetryOutcome.HAS_RESULT}
    assert get_document(db_conn, gescheitert) is None


def test_retry_all_prueft_erst_alle_hashes(
    db_conn: psycopg.Connection[Any], int_settings: Settings
) -> None:
    """Ein unbekannter Hash in der Angabe haelt alles auf, bevor etwas geschieht:
    Sonst waeren die vorderen schon neu vorgelegt, und der Aufrufer bekaeme eine
    Ausnahme statt ihrer Ausgaenge."""
    with OCRClient(db_dsn=int_settings.db_dsn, preflight=False) as klient:
        sha = klient.ocr_start(PNG_KOPF + b"vorne")
        db_conn.execute("UPDATE procrastinate_jobs SET status = 'cancelled'")
        with pytest.raises(DocumentNotFound):
            klient.retry_all([sha, "e" * 64])

    assert [job.status for job in _jobs_zu(db_conn, sha)] == ["cancelled"]


def test_gleichzeitiges_retry_reiht_nur_einen_neuen_job_ein(
    db_conn: psycopg.Connection[Any],
    int_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Zwei Aufrufer rufen `retry` gleichzeitig fuer dasselbe Dokument ohne
    laufenden Job: Nur einer reiht ein, der andere sieht danach dessen Job und
    meldet unterwegs. Sonst entstuenden zwei Jobs, und scheiterte der erste, liefe
    der zweite und bezahlte noch einmal. Ohne Zeitrennen: Der erste haelt beim
    Einreihen an, bis Postgres den zweiten nachweislich warten laesst (oder eine
    Frist verstreicht, wenn nichts ihn aufhaelt)."""
    import threading

    with OCRClient(db_dsn=int_settings.db_dsn, preflight=False) as klient:
        sha = klient.ocr_start(PNG_KOPF + b"zweimal retry")
    db_conn.execute("UPDATE procrastinate_jobs SET status = 'cancelled'")

    echtes_einreihen = tasks.enqueue_document
    erster_haelt = threading.Event()
    zweiter_wartete: list[bool] = []

    def langsam(app: Any, conn: Any, sha256: str) -> int:
        if not erster_haelt.is_set():
            erster_haelt.set()
            frist = time.monotonic() + 10
            with psycopg.connect(int_settings.db_dsn, autocommit=True) as beobachter:
                while time.monotonic() < frist:
                    wartend = beobachter.execute(
                        "SELECT 1 FROM pg_stat_activity "
                        "WHERE datname = current_database() "
                        "AND wait_event_type = 'Lock'"
                    ).fetchone()
                    if wartend is not None:
                        zweiter_wartete.append(True)
                        break
                    time.sleep(0.01)
        return echtes_einreihen(app, conn, sha256)

    monkeypatch.setattr(tasks, "enqueue_document", langsam)
    ausgaenge: dict[str, RetryOutcome] = {}

    def wiederholen(wer: str) -> None:
        with OCRClient(db_dsn=int_settings.db_dsn, preflight=False) as anderer:
            ausgaenge[wer] = anderer.retry(sha)

    erster = threading.Thread(target=wiederholen, args=("erster",))
    erster.start()
    assert erster_haelt.wait(10)
    zweiter = threading.Thread(target=wiederholen, args=("zweiter",))
    zweiter.start()
    erster.join(30)
    zweiter.join(30)

    assert sorted(o.value for o in ausgaenge.values()) == ["requeued", "underway"]
    assert [job.status for job in _jobs_zu(db_conn, sha)] == ["cancelled", "todo"]
    assert zweiter_wartete


def test_retry_gibt_seine_sperre_frei_solange_der_client_offen_ist(
    db_conn: psycopg.Connection[Any], int_settings: Settings
) -> None:
    """Die Sperre gilt nur fuer die Dauer eines `retry`: Ein anderer Client kann
    dasselbe Dokument gleich danach wiederholen, auch wenn der erste noch offen
    ist. Behielte die Sitzung sie, wartete jeder andere, bis der erste schliesst."""
    import threading

    with OCRClient(db_dsn=int_settings.db_dsn, preflight=False) as erster:
        sha = erster.ocr_start(PNG_KOPF + b"sperre frei")
        assert erster.retry(sha) is RetryOutcome.UNDERWAY
        danach: list[RetryOutcome] = []

        def anderswo() -> None:
            with OCRClient(db_dsn=int_settings.db_dsn, preflight=False) as zweiter:
                danach.append(zweiter.retry(sha))

        faden = threading.Thread(target=anderswo, daemon=True)
        faden.start()
        faden.join(10)
        frei = not faden.is_alive()

    assert frei, "der zweite Client wartete auf die Sperre des ersten"
    assert danach == [RetryOutcome.UNDERWAY]


def test_retry_tut_nichts_bei_einem_job_in_einem_unbekannten_zustand(
    db_conn: psycopg.Connection[Any], int_settings: Settings
) -> None:
    """`retry` und `document()` lesen den Zustand des Jobs nach derselben Regel
    (`db.derive_status`): Einen Zustand, den sie nicht kennen (hier Procrastinates
    altes `aborting`), fuehrt `document()` als wartend, also ist er fuer `retry`
    unterwegs. Sonst stuende neben einem Job, den `document()` als wartend zeigt,
    ein zweiter, bezahlter."""
    with OCRClient(db_dsn=int_settings.db_dsn, preflight=False) as klient:
        sha = klient.ocr_start(PNG_KOPF + b"unbekannter zustand")
        db_conn.execute(
            "UPDATE procrastinate_jobs SET status = 'aborting' WHERE lock = %s", (sha,)
        )
        vorher = _jobs_zu(db_conn, sha)

        ausgang = klient.retry(sha)
        phase = klient.document(sha).phase

    assert phase is Phase.WAITING
    assert ausgang is RetryOutcome.UNDERWAY
    assert _jobs_zu(db_conn, sha) == vorher


def test_retry_auf_ein_eben_fertig_gewordenes_dokument_reiht_nichts_ein(
    db_conn: psycopg.Connection[Any],
    int_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wird der Job fertig, waehrend `retry` liest (der Daemon schreibt `done`,
    loescht damit die Eingabe, und der Worker loescht den erfolgreichen Job), hat
    das Dokument ein Ergebnis: `retry` meldet es und reiht keinen Job ein, statt
    „neu eingereiht“ zu melden und einen ueberfluessigen Job anzulegen."""
    with OCRClient(db_dsn=int_settings.db_dsn, preflight=False) as klient:
        sha = klient.ocr_start(PNG_KOPF + b"wird gerade fertig")
        echt = OCRClient._job

        def job_endet_dazwischen(self: OCRClient, stand: db.DocumentState) -> Any:
            _ergebnis_schreiben(int_settings.db_dsn, sha, DocumentStatus.DONE)
            db_conn.execute("DELETE FROM procrastinate_jobs WHERE lock = %s", (sha,))
            return echt(self, stand)

        monkeypatch.setattr(OCRClient, "_job", job_endet_dazwischen)
        ausgang = klient.retry(sha)

    assert ausgang is RetryOutcome.HAS_RESULT
    assert _jobs_zu(db_conn, sha) == []
    zeile = get_document(db_conn, sha)
    assert zeile is not None and zeile.status is DocumentStatus.DONE


def _ergebnis_schreiben(
    dsn: str, sha: str, status: DocumentStatus, fehler: str | None = None
) -> None:
    """Schreibt ein Ergebnis so, wie ein Job es am Ende taete: ueber
    `db.write_terminal_async` auf einer eigenen Verbindung (wie im Daemon). Ein
    gueltiges Ergebnis loescht dabei die Eingabe."""

    async def schreiben() -> None:
        async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as ac:
            await db.write_terminal_async(
                ac, sha256=sha, status=status, raw={},
                endpoint_id="testendpoint", error=fehler,
            )

    asyncio.run(schreiben())


def test_retry_setzt_einen_job_nicht_zurueck_dessen_dokument_eben_ein_ergebnis_bekam(
    db_conn: psycopg.Connection[Any],
    int_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Endet das Dokument, waehrend `retry` liest, mit einem gueltigen Ergebnis und
    bleibt sein Job dabei `failed` stehen (etwa `input_error`), hat es ein
    Ergebnis: `retry` meldet das und setzt den Job nicht zurueck, statt
    „zurueckgesetzt“ zu melden und einen ueberfluessigen Versuch einzuplanen."""
    with OCRClient(db_dsn=int_settings.db_dsn, preflight=False) as klient:
        sha = klient.ocr_start(PNG_KOPF + b"wird gerade input_error")
        echt = OCRClient._job

        def job_endet_dazwischen(self: OCRClient, stand: db.DocumentState) -> Any:
            _ergebnis_schreiben(
                int_settings.db_dsn, sha, DocumentStatus.INPUT_ERROR, "kaputt"
            )
            db_conn.execute(
                "UPDATE procrastinate_jobs SET status = 'failed' WHERE lock = %s", (sha,)
            )
            return echt(self, stand)

        monkeypatch.setattr(OCRClient, "_job", job_endet_dazwischen)
        ausgang = klient.retry(sha)

    assert ausgang is RetryOutcome.HAS_RESULT
    (job,) = _jobs_zu(db_conn, sha)
    assert (job.status, job.attempts) == ("failed", 0)
    zeile = get_document(db_conn, sha)
    assert zeile is not None and zeile.status is DocumentStatus.INPUT_ERROR


def test_retry_haelt_die_eingabe_bis_zum_commit_fest(
    db_conn: psycopg.Connection[Any],
    int_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Waehrend `retry` neu einreiht, kann kein Endschreiben die Eingabe loeschen:
    Es wartet bis zum Commit. Sonst wuerde das Dokument fertig, waehrend `retry`
    seinen neuen Job an eine Eingabe schreibt, die es nicht mehr gibt."""
    with OCRClient(db_dsn=int_settings.db_dsn, preflight=False) as klient:
        sha = klient.ocr_start(PNG_KOPF + b"eingabe festhalten")
        db_conn.execute("UPDATE procrastinate_jobs SET status = 'cancelled'")
        echtes_einreihen = tasks.enqueue_document
        blockiert: list[bool] = []

        def einreihen_waehrend_eines_endschreibens(app: Any, conn: Any, sha256: str) -> int:
            with psycopg.connect(int_settings.db_dsn, autocommit=True) as anderswo:
                anderswo.execute("SET lock_timeout = '300ms'")
                try:
                    anderswo.execute("DELETE FROM inputs WHERE sha256 = %s", (sha256,))
                    blockiert.append(False)
                except psycopg.errors.LockNotAvailable:
                    blockiert.append(True)
            return echtes_einreihen(app, conn, sha256)

        monkeypatch.setattr(tasks, "enqueue_document", einreihen_waehrend_eines_endschreibens)
        ausgang = klient.retry(sha)

    assert blockiert == [True]
    assert ausgang is RetryOutcome.REQUEUED
    neu = _job_der_eingabe(db_conn, sha)
    assert neu is not None and neu.status == "todo"


def test_retry_nimmt_die_failed_zeile_weg_bevor_es_den_job_zuruecksetzt(
    db_conn: psycopg.Connection[Any],
    int_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Erst die `failed`-Zeile weg, dann der Job zurueck: Schreibt der
    zurueckgesetzte Job sofort ein neues Ergebnis (hier scheitert er gleich wieder),
    bleibt es stehen. Andersherum loeschte das Wegnehmen das neue Ergebnis."""
    with OCRClient(db_dsn=int_settings.db_dsn, preflight=False) as klient:
        sha = klient.ocr_start(PNG_KOPF + b"reihenfolge")
        db_conn.execute("UPDATE procrastinate_jobs SET status = 'failed' WHERE lock = %s", (sha,))
        _ergebnis_schreiben(int_settings.db_dsn, sha, DocumentStatus.FAILED, "das alte")
        manager = klient._procrastinate.job_manager
        echt = manager.retry_job_by_id

        def zuruecksetzen_und_gleich_scheitern(*args: Any, **kwargs: Any) -> None:
            echt(*args, **kwargs)
            _ergebnis_schreiben(int_settings.db_dsn, sha, DocumentStatus.FAILED, "das neue")

        monkeypatch.setattr(manager, "retry_job_by_id", zuruecksetzen_und_gleich_scheitern)
        ausgang = klient.retry(sha)

    assert ausgang is RetryOutcome.RESET
    zeile = get_document(db_conn, sha)
    assert zeile is not None and zeile.error == "das neue"


async def test_scheitert_das_neu_einreihen_bleibt_alles_wie_es_war(
    db_conn: psycopg.Connection[Any],
    async_conn: psycopg.AsyncConnection[Any],
    int_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wegnehmen, Einreihen und Kennung-Schreiben sind eine Transaktion: Scheitert
    ein Schritt danach, stehen die `failed`-Zeile, die alte Kennung und die alten
    Jobs wie vorher, und kein neuer Job ist entstanden."""
    with OCRClient(db_dsn=int_settings.db_dsn, preflight=False) as klient:
        sha = klient.ocr_start(PNG_KOPF + b"einreihen scheitert")
        db_conn.execute("UPDATE procrastinate_jobs SET status = 'cancelled' WHERE lock = %s", (sha,))
        alt = _job_der_eingabe(db_conn, sha)
        assert alt is not None
        await db.write_terminal_async(
            async_conn, sha256=sha, status=DocumentStatus.FAILED, raw={},
            endpoint_id="testendpoint", error="abgesagt",
        )
        vorher = _jobs_zu(db_conn, sha)

        def kaputt(*args: Any, **kwargs: Any) -> bool:
            raise DatabaseError("Kennung schreiben scheitert")

        monkeypatch.setattr(db, "set_input_job", kaputt)
        with pytest.raises(DatabaseError, match="Kennung schreiben"):
            klient.retry(sha)

    zeile = get_document(db_conn, sha)
    assert zeile is not None and zeile.status is DocumentStatus.FAILED
    assert _job_der_eingabe(db_conn, sha) == alt
    assert _jobs_zu(db_conn, sha) == vorher


def test_retry_gibt_seine_sperre_auch_nach_einem_fehler_frei(
    db_conn: psycopg.Connection[Any], int_settings: Settings
) -> None:
    """Endet `retry` mit einem Fehler, ist die Sperre des Dokuments trotzdem frei,
    solange der Client offen bleibt; sonst wartete jedes weitere `retry` darauf,
    bis der Client schliesst."""
    sha = "f" * 64
    db_conn.execute(
        "INSERT INTO documents (sha256, status, endpoint_id, error) "
        "VALUES (%s, 'failed', 'testendpoint', 'kaputt')",
        (sha,),
    )
    with OCRClient(db_dsn=int_settings.db_dsn, preflight=False) as klient:
        with pytest.raises(DatabaseError, match="Eingabe"):
            klient.retry(sha)
        frei = db_conn.execute(
            "SELECT pg_try_advisory_lock(%s, hashtext(%s))",
            (db.RETRY_SPERRKLASSE, sha),
        ).fetchone()
        db_conn.execute(
            "SELECT pg_advisory_unlock(%s, hashtext(%s))", (db.RETRY_SPERRKLASSE, sha)
        )

    assert frei == (True,)


def test_retry_all_nennt_jeden_hash_einmal_in_der_reihenfolge_der_angabe(
    db_conn: psycopg.Connection[Any], int_settings: Settings
) -> None:
    """Ein doppelt genannter Hash wird einmal wiederholt (ein Job, nicht zwei),
    und die Ausgaenge stehen in der Reihenfolge der Angabe."""
    with OCRClient(db_dsn=int_settings.db_dsn, preflight=False) as klient:
        erster = klient.ocr_start(PNG_KOPF + b"reihenfolge eins")
        zweiter = klient.ocr_start(PNG_KOPF + b"reihenfolge zwei")
        db_conn.execute("UPDATE procrastinate_jobs SET status = 'cancelled'")
        hinten, vorne = sorted([erster, zweiter], reverse=True)

        ausgaenge = klient.retry_all([hinten, vorne, hinten])

    assert list(ausgaenge.items()) == [
        (hinten, RetryOutcome.REQUEUED),
        (vorne, RetryOutcome.REQUEUED),
    ]
    for sha in (hinten, vorne):
        assert [job.status for job in _jobs_zu(db_conn, sha)] == ["cancelled", "todo"]


def test_retry_meldet_einen_fehlschlag_beim_zuruecksetzen_als_datenbankfehler(
    db_conn: psycopg.Connection[Any],
    int_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setzt jemand anderes den gescheiterten Job im selben Augenblick zurueck
    (etwa Procrastinates Shell), lehnt Procrastinate das zweite Zuruecksetzen ab.
    `retry` gibt das als `DatabaseError` des Pakets weiter, nicht als rohe
    Procrastinate-Ausnahme, und sagt, dass ein neues `retry` den Stand klaert."""
    with OCRClient(db_dsn=int_settings.db_dsn, preflight=False) as klient:
        sha = klient.ocr_start(PNG_KOPF + b"fremdes zuruecksetzen")
        db_conn.execute("UPDATE procrastinate_jobs SET status = 'failed' WHERE lock = %s", (sha,))

        def abgelehnt(*args: Any, **kwargs: Any) -> None:
            raise procrastinate.exceptions.ConnectorException("Database error.")

        monkeypatch.setattr(klient._procrastinate.job_manager, "retry_job_by_id", abgelehnt)
        with pytest.raises(DatabaseError, match="retry"):
            klient.retry(sha)
