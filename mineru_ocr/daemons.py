"""Lokale Daemon-Verwaltung: lebende Kontrollsockets statt einer PID-Kartei.

Nur der Besitzer darf das Verzeichnis betreten. Ein Socket antwortet aus dem
Daemon selbst; alte Dateien und wiederverwendete PIDs koennen niemanden stoppen.
Job-Zustand bleibt ausschliesslich in Procrastinate.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from typing import Any
import uuid

import psycopg

from . import db
from .config import QUEUE_AUDIO, QUEUE_OCR, Settings
from .errors import DatabaseError


def verzeichnis(
    settings: Settings, *, identitaet: tuple[Any, ...] | None = None,
) -> Path:
    """Gleiche Datenbank auf diesem Rechner bedeutet gleiche Kontrollsockets."""
    try:
        if identitaet is None:
            with db.connect(settings.db_dsn) as conn:
                identitaet = conn.execute(
                    # Clusterkennung und Datenbank-OID haengen nicht vom Zugangsweg ab.
                    "SELECT system_identifier, oid FROM pg_control_system(), pg_database "
                    "WHERE datname = current_database()"
                ).fetchone()
    except psycopg.Error as fehler:
        raise DatabaseError(
            "Datenbankkennung nicht lesbar; die Datenbankrolle braucht "
            "EXECUTE auf pg_control_system()."
        ) from fehler
    kennung = hashlib.sha256(repr(identitaet).encode()).hexdigest()[:16]
    pfad = Path("/tmp") / f"mineru-{os.getuid()}-{kennung}"
    pfad.mkdir(mode=0o700, exist_ok=True)
    if pfad.is_symlink() or pfad.stat().st_uid != os.getuid() or pfad.stat().st_mode & 0o077:
        raise DatabaseError(f"Unsicheres Daemon-Verzeichnis: {pfad}")
    return pfad


def fragen(pfad: Path, befehl: str = "status", *, timeout: float = 2) -> dict[str, Any] | None:
    """Fragt den Prozess selbst, mit Frist; ein totes Socket zaehlt nicht."""
    try:
        ende = time.monotonic() + timeout
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as kanal:
            kanal.settimeout(timeout)
            kanal.connect(str(pfad))
            rest = ende - time.monotonic()
            if rest <= 0:
                return None
            kanal.settimeout(rest)
            kanal.sendall((befehl + "\n").encode())
            daten = bytearray()
            while b"\n" not in daten:
                rest = ende - time.monotonic()
                if rest <= 0:
                    return None
                kanal.settimeout(rest)
                teil = kanal.recv(65536)
                if not teil:
                    return None
                daten.extend(teil)
            antwort: dict[str, Any] = json.loads(daten)
            return antwort
    except (OSError, ValueError):
        return None


def status(settings: Settings) -> dict[str, Any]:
    """Lokale Plaetze und datenbankweite Warteschlangen, getrennt nach Lane.

    Die bisherigen Felder bleiben OCR. Alte Kontrollsockets kennen die
    Audio-Kapazitaet nicht; dort steht null statt einer geratenen Zahl.
    """
    daemons = []
    lanes = (QUEUE_OCR, QUEUE_AUDIO)
    with db.connect(settings.db_dsn) as conn:
        db.check_schema(conn)
        for pfad in sorted(verzeichnis(settings).glob("*.sock")):
            antwort = fragen(pfad)
            if antwort is None or not antwort["ready"]:
                continue
            ids = antwort.pop("worker_ids")
            frisch = {kennung for kennung, _ in db.worker_heartbeats(conn)}
            if not set(ids).issubset(frisch):
                continue
            antwort.pop("ready")
            antwort["socket"] = str(pfad)
            belegt = dict(conn.execute(
                "SELECT queue_name, count(*) FROM procrastinate_jobs "
                "WHERE worker_id = ANY(%s) AND status = 'doing' GROUP BY queue_name",
                (ids,),
            ).fetchall())
            kapazitaet = antwort.get("lanes", {QUEUE_OCR: {"total": antwort["available"]}})
            antwort["lanes"] = {
                lane: {"occupied": belegt.get(lane, 0),
                       "total": kapazitaet.get(lane, {}).get("total")}
                for lane in lanes
            }
            antwort["occupied"] = belegt.get(QUEUE_OCR, 0)
            daemons.append(antwort)
        queues = {lane: {"waiting": wartend, "running": laufend}
                  for lane, wartend, laufend in conn.execute(
            "SELECT queue_name, count(*) FILTER (WHERE status = 'todo'), "
            "count(*) FILTER (WHERE status = 'doing') "
            "FROM procrastinate_jobs WHERE queue_name = ANY(%s) GROUP BY queue_name",
            (list(lanes),),
        ).fetchall()}
    pro_lane = {}
    for lane in lanes:
        gesamt = [d["lanes"][lane]["total"] for d in daemons]
        pro_lane[lane] = {
            "slots": {"occupied": sum(d["lanes"][lane]["occupied"] for d in daemons),
                      "total": None if None in gesamt else sum(gesamt)},
            "queue": queues.get(lane, {"waiting": 0, "running": 0}),
        }
    return {
        "count": len(daemons), "daemons": daemons,
        "slots": {"occupied": sum(d["occupied"] for d in daemons),
                  "available": sum(d["available"] for d in daemons)},
        "queue": pro_lane[QUEUE_OCR]["queue"],
        "lanes": pro_lane,
    }


def start(settings: Settings, anzahl: int) -> list[dict[str, Any]]:
    """Prueft vor dem ersten Kind; wartet nur auf Start, niemals auf Dokumente."""
    from .daemon import _schema_pruefen
    from .elevenlabs import check_access

    settings.require_endpoint_id()
    schluessel = settings.require_api_key()
    check_access(settings)
    _schema_pruefen(settings.db_dsn)
    if settings.ocr_concurrency < 1:
        raise ValueError("ocr_concurrency muss mindestens 1 sein.")
    ordner = verzeichnis(settings)
    daten = dataclasses.asdict(dataclasses.replace(settings, api_key=schluessel))
    daten["config_path"] = None
    gestartet = []
    kinder: list[subprocess.Popen[bytes]] = []
    try:
        for _ in range(anzahl):
            kennung = uuid.uuid4().hex[:12]
            sock = ordner / f"{kennung}.sock"
            protokoll = ordner / f"{kennung}.log"
            with protokoll.open("xb") as ausgabe:
                kind = subprocess.Popen(
                    [sys.executable, "-m", "mineru_ocr.daemon", str(sock), str(protokoll)],
                    stdin=subprocess.PIPE, stdout=ausgabe, stderr=ausgabe,
                    start_new_session=True, close_fds=True,
                    cwd=Path(__file__).resolve().parent.parent,
                )
            kinder.append(kind)
            assert kind.stdin is not None
            # Geheimnisse nur im anonymen Rohr, nie in Argumenten oder Dateien.
            kind.stdin.write(json.dumps(daten).encode())
            kind.stdin.close()
            frist = time.monotonic() + 20
            while kind.poll() is None and time.monotonic() < frist:
                antwort = fragen(sock)
                if antwort and antwort["ready"]:
                    gestartet.append(antwort)
                    break
                time.sleep(0.05)
            else:
                raise DatabaseError(f"Daemon startet nicht; Protokoll: {protokoll}")
    except BaseException:
        for kind in kinder:
            if kind.poll() is None:
                kind.terminate()
        for kind in kinder:
            try:
                kind.wait(timeout=settings.shutdown_graceful_timeout + 10)
            except subprocess.TimeoutExpired:
                kind.kill()
                kind.wait()
        raise
    return gestartet


def stop(settings: Settings, anzahl: int) -> dict[str, int]:
    """Stoppt die am wenigsten belegten; der Socket ersetzt unsichere PID-Signale."""
    import fcntl

    # Zwei gleichzeitige stop-Aufrufe duerfen nicht dieselben Prozesse waehlen.
    with (verzeichnis(settings) / "stop.lock").open("a") as sperre:
        fcntl.flock(sperre, fcntl.LOCK_EX)
        kandidaten = sorted(status(settings)["daemons"], key=lambda d: (d["occupied"], d["pid"]))
        ausgewaehlt: list[int] = []
        for kandidat in kandidaten:
            if len(ausgewaehlt) == anzahl:
                break
            pfad = Path(kandidat["socket"])
            antwort = fragen(pfad, "stop")
            if antwort and antwort.get("stopping"):
                ausgewaehlt.append(antwort["pid"])
        frist = time.monotonic() + settings.shutdown_graceful_timeout + 15
        # Ein blockierter Socket ist kein beendeter Prozess. Die PIDs stammen
        # aus der Stop-Bestaetigung; ps liest nur, ohne PID-Signale zu senden.
        while ausgewaehlt and not _prozesse_beendet(ausgewaehlt):
            if time.monotonic() > frist:
                raise DatabaseError("Daemons fahren noch herunter; bitte daemon list pruefen.")
            time.sleep(0.05)
    return {"requested": anzahl, "stopped": len(ausgewaehlt), "running_before": len(kandidaten)}


def _prozesse_beendet(pids: list[int]) -> bool:
    """Fehlende Prozesse und Zombies sind beendet, auch ohne erreichbaren Socket."""
    try:
        lauf = subprocess.run(
            ["ps", "-p", ",".join(str(pid) for pid in pids), "-o", "stat="],
            capture_output=True, text=True, timeout=2,
        )
    except subprocess.TimeoutExpired as fehler:
        raise DatabaseError("Daemon-Prozesszustand konnte nicht gelesen werden.") from fehler
    # ps liefert 1, wenn keine der angefragten PIDs mehr existiert. Fehler
    # duerfen niemals als erfolgreiches Herunterfahren durchgehen.
    if lauf.returncode not in (0, 1) or lauf.stderr.strip():
        raise DatabaseError("Daemon-Prozesszustand konnte nicht gelesen werden.")
    return all(zeile.strip().startswith("Z") for zeile in lauf.stdout.splitlines())
