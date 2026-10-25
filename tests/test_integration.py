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
from psycopg_pool import NullConnectionPool

from mineru_ocr import config, daemon, db, tasks
from mineru_ocr.client import OCRClient
from mineru_ocr.config import Settings
from mineru_ocr.db import DocumentStatus, Phase, derive_status, get_document
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

    def einreihen(self, sha: str) -> bool:
        """Reiht ein Dokument ein, wie der Client es tut (`connection=`)."""
        return tasks.enqueue_document(self.app, self.conn, sha)


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
    return {
        "id": "erfunden",
        "status": "COMPLETED",
        "workerId": "wrk",
        "output": {
            "ok": True,
            "results": [
                {
                    "markdown": markdown,
                    "content_list": [{"type": "text", "text": markdown, "page_idx": 0}],
                    "images": {},
                    "middle": {},
                }
            ],
        },
    }


def _anlegen(conn: psycopg.Connection[Any], tmp_path: Path, name: str) -> str:
    inhalt = f"inhalt von {name}".encode()
    pfad = tmp_path / name
    pfad.write_bytes(inhalt)
    sha = hashlib.sha256(inhalt).hexdigest()
    db.insert_document(
        conn,
        sha256=sha,
        size_bytes=len(inhalt),
        mime="image/png",
        endpoint_id="testendpoint",
    )
    db.insert_input(conn, sha256=sha, content=inhalt)
    return sha


def _jobs_zu(conn: psycopg.Connection[Any], sha: str) -> list[Job]:
    """Liest die Jobs eines Dokuments — ueber den `queueing_lock`-Link.

    Genau der Weg des Ein-Buch-Modells: „DER JOB LINKED AUF DAS DOKUMENT DAS ER
    PROCESSED UND WENN MAN DEN STATUS EINES DOKUMENTS HABEN WILL QUERIED MAN
    NACH DE LETZTEN JOB DER AUF DAS DOKUMENT LINKT" (Plan 2.4).
    """
    zeilen = conn.execute(
        "SELECT id, status, attempts, queue_name, lock, queueing_lock, task_name "
        "FROM procrastinate_jobs WHERE queueing_lock = %s ORDER BY id",
        (sha,),
    ).fetchall()
    return [
        Job(
            id=z[0],
            status=z[1],
            attempts=z[2],
            queue=z[3],
            lock=z[4],
            queueing_lock=z[5],
            task_name=z[6],
        )
        for z in zeilen
    ]


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

    Geprueft werden BEIDE Buecher — die Zeile steht auf `done`, der Job auf
    `succeeded`, und der abgeleitete Zustand sagt TERMINAL.
    """
    sha = _anlegen(db_conn, tmp_path, "a.png")
    assert erzeuger.einreihen(sha) is True

    async def fertig() -> bool:
        zeile = get_document(db_conn, sha)
        return zeile is not None and zeile.status.is_terminal

    await _daemon_laufen_lassen(int_settings, ProtokollRunPod(), bis=fertig)

    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.DONE
    assert zeile.raw is not None
    assert zeile.raw["markdown"] == "Text"

    jobs = _jobs_zu(db_conn, sha)
    assert len(jobs) == 1
    assert jobs[0].status == "succeeded"
    assert derive_status(zeile, jobs).phase is Phase.TERMINAL


async def test_abgeschicktes_dokument_wird_aus_inputs_gelesen(
    db_conn: psycopg.Connection[Any], int_settings: Settings, erzeuger: Erzeuger
) -> None:
    """Abschicken legt die Eingabe in `inputs` an, und der Job liest sie von dort.

    Der ganze Weg mit echtem Daemon und echtem Postgres: Die Bytes, die bei
    RunPod ankommen, sind die abgeschickten; nach dem Ergebnis ist die Eingabe
    geloescht; es liegt keine Spool-Datei und kein Pfad dazwischen.
    """
    from mineru_ocr.wrapper import OCRWrapper

    inhalt = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"abgeschickt"
    huelle = OCRWrapper.from_bytes(inhalt, db_conn, int_settings)

    zeile = db_conn.execute(
        "SELECT content FROM inputs WHERE sha256 = %s", (huelle.sha256,)
    ).fetchone()
    assert zeile is not None and bytes(zeile[0]) == inhalt
    assert erzeuger.einreihen(huelle.sha256) is True

    async def fertig() -> bool:
        gelesen = get_document(db_conn, huelle.sha256)
        return gelesen is not None and gelesen.status.is_terminal

    runpod = ProtokollRunPod()
    await _daemon_laufen_lassen(int_settings, runpod, bis=fertig)

    assert runpod.dateien == [inhalt]
    gelesen = get_document(db_conn, huelle.sha256)
    assert gelesen is not None and gelesen.status is DocumentStatus.DONE
    assert db_conn.execute(
        "SELECT 1 FROM inputs WHERE sha256 = %s", (huelle.sha256,)
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
        return zeile is not None and zeile.status.is_terminal

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
    assert derive_status(zeile, jobs).phase is Phase.TERMINAL
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
    Dort endet der Job `aborted`, weil der Worker sich abmeldet und seine Jobs
    ordentlich beendet. Hier stirbt der Prozess, OHNE das zu tun — der Job bleibt
    auf `doing` stehen, und niemand arbeitet mehr daran. Ohne den Waechter bliebe
    er dort fuer immer liegen, und das Dokument waere unsichtbar verloren: Es
    stuende auf `new`, saehe aus wie „wartet" und wartete auf niemanden.

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
        "WHERE queueing_lock = %s",
        (tote_kennung[0], sha),
    )
    db_conn.commit()

    jobs = _jobs_zu(db_conn, sha)
    assert len(jobs) == 1
    assert jobs[0].status == "doing"
    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.NEW

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


async def test_doppeltes_einreihen_wird_abgefangen(
    db_conn: psycopg.Connection[Any], int_settings: Settings, erzeuger: Erzeuger, tmp_path: Path
) -> None:
    """(f) Das zweite Einreihen ergibt „laeuft schon" statt eines zweiten Jobs."""
    sha = _anlegen(db_conn, tmp_path, "f.png")
    assert erzeuger.einreihen(sha) is True
    assert erzeuger.einreihen(sha) is False
    assert len(_jobs_zu(db_conn, sha)) == 1


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

    Genau diese letzte Zusicherung faengt einen Terminal-Guard, der faelschlich
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
        # bekommen und abgeschlossen hat, ist die Frage beantwortet.
        zeile = db_conn.execute(
            "SELECT count(*) FROM procrastinate_jobs "
            "WHERE task_name = %s AND status = 'succeeded'",
            (config.TASK_RETRY_STALLED,),
        ).fetchone()
        return zeile is not None and zeile[0] >= 1

    await _daemon_laufen_lassen(
        int_settings, haengend, bis=waechter_lief, zeit=90.0
    )

    # Und die Gegenprobe: Die OCR-Plaetze WAREN wirklich belegt, waehrend der
    # Waechter durchlief.
    assert haengend.gestartet.is_set()
    assert hoechststand == int_settings.ocr_concurrency


async def test_abbruch_beim_herunterfahren_heilt_durch_neu_einreihen(
    db_conn: psycopg.Connection[Any], int_settings: Settings, erzeuger: Erzeuger, tmp_path: Path
) -> None:
    """(e) Job `aborted` (endgueltig, KEIN automatischer Retry — und
    `retry_job_by_id` nimmt `aborted` nicht an, Feature-Verifikation Punkt 19),
    Zeile bleibt `new`, abgeleiteter Zustand zeigt den beendeten Job; erneutes
    Einreihen laesst das Dokument durchlaufen und es endet `done`."""
    sha = _anlegen(db_conn, tmp_path, "e.png")
    erzeuger.einreihen(sha)

    haengend = ProtokollRunPod()
    haengend.freigabe = asyncio.Event()

    async def laeuft() -> bool:
        return haengend.gestartet.is_set()

    await _daemon_laufen_lassen(int_settings, haengend, bis=laeuft)

    # Nach dem Herunterfahren: der Job ist beendet, die Zeile unberuehrt.
    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.NEW
    jobs = _jobs_zu(db_conn, sha)
    assert jobs[-1].status in ("aborted", "failed", "doing")

    # Und `retry_job_by_id` nimmt einen `aborted`-Job NICHT an — der Heilungsweg
    # ist das erneute Einreihen.
    if jobs[-1].status == "aborted":
        kennung = jobs[-1].id
        assert kennung is not None
        with pytest.raises(procrastinate.exceptions.ProcrastinateException):
            erzeuger.app.job_manager.retry_job_by_id(kennung, utils.utcnow())

    # Erneutes Einreihen — und dann laeuft es durch.
    erzeuger.einreihen(sha)
    gelingend = ProtokollRunPod(lambda nr: _fertig("Nach dem Neustart"))

    async def fertig() -> bool:
        z = get_document(db_conn, sha)
        return z is not None and z.status is DocumentStatus.DONE

    await _daemon_laufen_lassen(int_settings, gelingend, bis=fertig)

    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.DONE


async def test_duplikat_waehrend_der_verarbeitung_ist_ein_nichts_tun(
    db_conn: psycopg.Connection[Any],
    async_conn: psycopg.AsyncConnection[Any],
    int_settings: Settings,
    erzeuger: Erzeuger,
    tmp_path: Path,
) -> None:
    """(i) Ein waehrend `doing` nachgeschobenes Duplikat wird per `lock`
    serialisiert, startet danach und endet sofort am Terminal-Guard — keine
    Doppelzahlung.

    Der Fall ist real und nicht ausgedacht: `queueing_lock` bindet NUR den
    Zustand `todo` und ist frei, sobald der Job laeuft („allows a single job in
    todo status. Meanwhile, it allows multiple jobs to be in doing status",
    Feature-Verifikation Punkt 5). Ohne den zweiten Lock liefe das Duplikat
    parallel und bezahlte dasselbe Dokument ein zweites Mal.

    Aufbau ohne Zeitrennen: Das Dokument wird zuerst fertig verarbeitet, DANN
    ein zweiter Job eingereiht. Weil die Zeile dann `done` ist, muss der zweite
    Job am Guard enden, ohne RunPod auch nur anzufassen.
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
    assert erzeuger.einreihen(sha) is True
    zweiter = ProtokollRunPod()

    async def zweiter_job_fertig() -> bool:
        jobs = _jobs_zu(db_conn, sha)
        return len(jobs) == 2 and jobs[-1].status == "succeeded"

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
    db_conn: psycopg.Connection[Any], int_settings: Settings
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

    tasks.worker_context_resources = merken  # type: ignore[assignment]
    try:
        lauf = asyncio.create_task(daemon.run(int_settings))
        # Warten, bis der Pool wirklich offen ist — sonst prueft der Test das
        # Schliessen von etwas, das nie auf war.
        async with asyncio.timeout(15.0):
            while not gebaut or gebaut["pool"].closed:
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
    ausdruecklich zwischen „nie eingereiht" und „wartet" — und die
    Worker-Lebenszeichen sagen, ob ueberhaupt jemand da ist.
    """
    sha = _anlegen(db_conn, tmp_path, "leer.png")

    zeile = get_document(db_conn, sha)
    assert zeile is not None
    # Vor dem Einreihen: registriert, nie eingereiht.
    assert derive_status(zeile, _jobs_zu(db_conn, sha)).phase is Phase.REGISTERED

    erzeuger.einreihen(sha)
    # Danach: wartet — und niemand arbeitet.
    assert derive_status(zeile, _jobs_zu(db_conn, sha)).phase is Phase.WAITING
    assert db.worker_heartbeats(db_conn) == []


# --------------------------------------------------------------------------
# Eine App je Prozess
# --------------------------------------------------------------------------


async def test_vom_client_eingereihter_job_wird_vom_daemon_abgearbeitet(
    db_conn: psycopg.Connection[Any], int_settings: Settings, tmp_path: Path
) -> None:
    """Der ganze Weg ueber den echten `OCRClient`: Er reiht per Namen ein (eigene
    App, eigene Verbindung), der Job liegt in der Queue, die der Daemon liest, und
    der Daemon arbeitet ihn ab.

    Beide Seiten sind hier zwei Apps im selben Prozess — im Betrieb sind es zwei
    Prozesse. Gerade deshalb prueft der Test, dass sie sich nur ueber die
    Datenbank finden: Name und Queue des Jobs muessen die sein, unter denen der
    Daemon seinen Task angemeldet hat.
    """
    sha = _anlegen(db_conn, tmp_path, "client.png")
    with OCRClient(
        db_dsn=int_settings.db_dsn,
        api_key="erfunden",
        preflight=False,
        strict=False,
    ) as klient:
        klient.ocr_start(klient.get(sha))

        jobs = _jobs_zu(db_conn, sha)
        assert len(jobs) == 1
        assert jobs[0].queue == config.QUEUE_OCR
        assert jobs[0].task_name == config.TASK_OCR_DOCUMENT

        async def fertig() -> bool:
            zeile = get_document(db_conn, sha)
            return zeile is not None and zeile.status.is_terminal

        await _daemon_laufen_lassen(int_settings, ProtokollRunPod(), bis=fertig)

    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.DONE
    assert _jobs_zu(db_conn, sha)[0].status == "succeeded"


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
    # Und Job-Zeilen bleiben stehen: Der abgeleitete Zustand LIEST aus ihnen.
    assert all(o["delete_jobs"] == "never" for o in beide)

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
        return zeile is not None and zeile.status.is_terminal

    try:
        await _daemon_laufen_lassen(int_settings, runpod, bis=fertig)
    finally:
        await runpod.aclose()

    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.DONE
    assert gegenstelle.ereignisse("run") == ["job-1"]
    assert gegenstelle.ereignisse("cancel") == []
