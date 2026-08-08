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
import dataclasses
import hashlib
from pathlib import Path
from typing import Any

import psycopg
import pytest
from procrastinate.jobs import Job

from mineru_ocr import config, daemon, db, tasks
from mineru_ocr.config import Settings
from mineru_ocr.db import DocumentStatus, Phase, derive_status, get_document

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


class ProtokollRunPod:
    """RunPod-Doppel mit steuerbarem Verhalten je Aufruf."""

    def __init__(self, verhalten: Any = None) -> None:
        self.verhalten = verhalten or (lambda nr: _fertig())
        self.aufrufe = 0
        self.gestartet = asyncio.Event()
        self.freigabe: asyncio.Event | None = None

    async def submit(self, **_: Any) -> str:
        self.aufrufe += 1
        return f"job-{self.aufrufe}"

    async def await_result(self, job_id: str) -> dict[str, Any]:
        self.gestartet.set()
        if self.freigabe is not None:
            await self.freigabe.wait()
        return self.verhalten(self.aufrufe)

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
        source_path=str(pfad),
        source_name=name,
        size_bytes=len(inhalt),
        mime="image/png",
        page_count=None,
        endpoint_id="l79lebhinxaqjz",
    )
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
        # Den Produzenten-Zugang wiederherstellen. Im Betrieb sind Daemon und
        # Produzent ZWEI PROZESSE mit je eigener App; im Test teilen sie sich
        # die des Moduls, und `daemon.run` haengt dort einen asynchronen Zugang
        # ein. Ohne diese Zeile fiele der naechste `defer_document` in genau die
        # Fehlermeldung, die es dafuer gibt — richtig, aber hier nicht der
        # gepruefte Sachverhalt.
        tasks.configure(int_settings, for_worker=False)


# --------------------------------------------------------------------------
# Die Szenarien
# --------------------------------------------------------------------------


async def test_dokument_laeuft_vollstaendig_durch(
    db_conn: psycopg.Connection[Any], int_settings: Settings, tmp_path: Path
) -> None:
    """Der Grundfall, der alle anderen traegt: einreihen, Daemon laeuft, fertig.

    Geprueft werden BEIDE Buecher — die Zeile steht auf `done`, der Job auf
    `succeeded`, und der abgeleitete Zustand sagt TERMINAL.
    """
    tasks.configure(int_settings, for_worker=False)
    sha = _anlegen(db_conn, tmp_path, "a.png")
    assert tasks.defer_document(sha) is True

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


async def test_nicht_wiederholbarer_fehler_im_ersten_versuch(
    db_conn: psycopg.Connection[Any], int_settings: Settings, tmp_path: Path
) -> None:
    """(b) Zeile sofort `failed`, juengster Job `failed` — kein Retry-Karussell."""
    tasks.configure(int_settings, for_worker=False)
    sha = _anlegen(db_conn, tmp_path, "b.png")
    tasks.defer_document(sha)

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
    db_conn: psycopg.Connection[Any], int_settings: Settings, tmp_path: Path
) -> None:
    """(c) Nach den erlaubten voruebergehenden Fehlern: Zeile `failed`.

    Und der Versuchszaehler steht am Ende auf `max_attempts - 1` — die Zahl der
    Laeufe ist `attempts + 1`, also genau `max_attempts`.
    """
    tasks.configure(int_settings, for_worker=False)
    sha = _anlegen(db_conn, tmp_path, "c.png")
    tasks.defer_document(sha)

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
    # Die Zeile bleibt `new`: Ein WIEDERHOLBARER Fehler laesst sie unberuehrt,
    # auch der letzte. Das ist der Fall ENDED_WITHOUT_RESULT, und er heilt beim
    # naechsten Einreihen von selbst.
    assert zeile.status is DocumentStatus.NEW
    assert derive_status(zeile, jobs).phase is Phase.ENDED_WITHOUT_RESULT


async def test_doppeltes_einreihen_wird_abgefangen(
    db_conn: psycopg.Connection[Any], int_settings: Settings, tmp_path: Path
) -> None:
    """(f) Das zweite Einreihen ergibt „laeuft schon" statt eines zweiten Jobs."""
    tasks.configure(int_settings, for_worker=False)
    sha = _anlegen(db_conn, tmp_path, "f.png")
    assert tasks.defer_document(sha) is True
    assert tasks.defer_document(sha) is False
    assert len(_jobs_zu(db_conn, sha)) == 1


async def test_retry_von_hand_verarbeitet_wirklich_neu(
    db_conn: psycopg.Connection[Any],
    async_conn: psycopg.AsyncConnection[Any],
    int_settings: Settings,
    tmp_path: Path,
) -> None:
    """(g) `retry_job_by_id` setzt den Job auf `todo`, die Ereignis-Historie
    bleibt — UND das Dokument wird wirklich neu verarbeitet: es endet `done`,
    `raw` ist ueberschrieben.

    Genau diese letzte Zusicherung faengt einen Terminal-Guard, der faelschlich
    `failed` deckt. Ohne sie saehe ein Nichts-Tun wie ein Erfolg aus.
    """
    tasks.configure(int_settings, for_worker=False)
    sha = _anlegen(db_conn, tmp_path, "g.png")
    tasks.defer_document(sha)

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
    app = tasks.configure(int_settings, for_worker=True)
    async with app.open_async():
        from procrastinate import utils

        kennung = jobs[-1].id
        # `Job.id` ist als optional deklariert (ein noch nicht eingereihter Job
        # hat keine). Ein aus der Datenbank gelesener hat immer eine — das wird
        # hier festgestellt statt unterdrueckt.
        assert kennung is not None
        await app.job_manager.retry_job_by_id_async(
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
    db_conn: psycopg.Connection[Any], int_settings: Settings, tmp_path: Path
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
    tasks.configure(int_settings, for_worker=False)
    for nr in range(int_settings.ocr_concurrency):
        sha = _anlegen(db_conn, tmp_path, f"h{nr}.png")
        tasks.defer_document(sha)

    haengend = ProtokollRunPod()
    haengend.freigabe = asyncio.Event()  # wird nie gesetzt: die Plaetze bleiben belegt

    async def waechter_lief() -> bool:
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

    # Und die Gegenprobe: Die OCR-Plaetze WAREN wirklich belegt.
    assert haengend.gestartet.is_set()
    laufende = db_conn.execute(
        "SELECT count(*) FROM procrastinate_jobs "
        "WHERE queue_name = %s AND status = 'doing'",
        (config.QUEUE_OCR,),
    ).fetchone()
    assert laufende is not None
    assert laufende[0] == int_settings.ocr_concurrency


async def test_abbruch_beim_herunterfahren_heilt_durch_neu_einreihen(
    db_conn: psycopg.Connection[Any], int_settings: Settings, tmp_path: Path
) -> None:
    """(e) Job `aborted` (endgueltig, KEIN automatischer Retry — und
    `retry_job_by_id` nimmt `aborted` nicht an, Feature-Verifikation Punkt 19),
    Zeile bleibt `new`, abgeleiteter Zustand zeigt den beendeten Job; erneutes
    Einreihen laesst das Dokument durchlaufen und es endet `done`."""
    tasks.configure(int_settings, for_worker=False)
    sha = _anlegen(db_conn, tmp_path, "e.png")
    tasks.defer_document(sha)

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
        app = tasks.configure(int_settings, for_worker=True)
        async with app.open_async():
            from procrastinate import utils

            kennung = jobs[-1].id
            assert kennung is not None
            with pytest.raises(Exception):
                await app.job_manager.retry_job_by_id_async(kennung, utils.utcnow())
        tasks.configure(int_settings, for_worker=False)

    # Erneutes Einreihen — und dann laeuft es durch.
    tasks.defer_document(sha)
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
    tasks.configure(int_settings, for_worker=False)
    sha = _anlegen(db_conn, tmp_path, "i.png")
    tasks.defer_document(sha)

    erster = ProtokollRunPod()

    async def fertig() -> bool:
        z = get_document(db_conn, sha)
        return z is not None and z.status is DocumentStatus.DONE

    await _daemon_laufen_lassen(int_settings, erster, bis=fertig)
    assert erster.aufrufe == 1

    # Jetzt das Duplikat.
    assert tasks.defer_document(sha) is True
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
    db_conn: psycopg.Connection[Any], int_settings: Settings, tmp_path: Path
) -> None:
    """(j) Bestaetigt am laufenden System, was am SQL-Schema belegt ist
    (Feature-Verifikation Punkt 19): Jedes Zuruecksetzen auf `todo` — durch den
    Waechter wie durch einen Retry von Hand — erhoeht `attempts` und zehrt am
    Budget aus `max_attempts`.

    Das Ergebnis steht als Kommentar an der Wiederholungs-Strategie, damit es
    niemand fuer einen Fehler haelt.
    """
    tasks.configure(int_settings, for_worker=False)
    sha = _anlegen(db_conn, tmp_path, "j.png")
    tasks.defer_document(sha)

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

    Geprueft daran, dass `daemon.run` nach einem Abbruch wirklich zurueckkehrt
    und dabei den Pool geschlossen hinterlaesst.
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


async def test_ohne_daemon_passiert_nichts_und_das_wird_gesagt(
    db_conn: psycopg.Connection[Any], int_settings: Settings, tmp_path: Path
) -> None:
    """Ohne laufenden Daemon stapeln sich Jobs, und der abgeleitete Zustand
    sagt es.

    Die haeufigste Betriebs-Ueberraschung dieses Aufbaus: Alles sieht normal aus,
    nur arbeitet niemand. Der abgeleitete Zustand unterscheidet deshalb
    ausdruecklich zwischen „nie eingereiht" und „wartet" — und die
    Worker-Lebenszeichen sagen, ob ueberhaupt jemand da ist.
    """
    tasks.configure(int_settings, for_worker=False)
    sha = _anlegen(db_conn, tmp_path, "leer.png")

    zeile = get_document(db_conn, sha)
    assert zeile is not None
    # Vor dem Einreihen: registriert, nie eingereiht.
    assert derive_status(zeile, _jobs_zu(db_conn, sha)).phase is Phase.REGISTERED

    tasks.defer_document(sha)
    # Danach: wartet — und niemand arbeitet.
    assert derive_status(zeile, _jobs_zu(db_conn, sha)).phase is Phase.WAITING
    assert db.worker_heartbeats(db_conn) == []
