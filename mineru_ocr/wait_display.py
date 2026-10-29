"""CLI-Debuganzeige: dauerhafte Fakten lesen, fluechtige Fakten nur beobachten."""
from __future__ import annotations

import asyncio
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
import os
import shutil
import sys
import time
from typing import Any, TextIO
import unicodedata

import psycopg

from . import daemons, db
from .client import OCRClient
from .errors import MineruOcrError
from .wrapper import OCRWrapper


def _sicher(text: str) -> str:
    return text if text.isprintable() else repr(text)


def _breite(text: str) -> int:
    return sum(0 if unicodedata.combining(z) else (2 if unicodedata.east_asian_width(z) in {"W", "F"} else 1) for z in text)


def _kuerzen(text: str, breite: int) -> str:
    """Eine physische Terminalzeile, auch bei breiten Unicode-Zeichen."""
    ausgabe = ""
    benutzt = 0
    for zeichen in text:
        platz = 0 if unicodedata.combining(zeichen) else (2 if unicodedata.east_asian_width(zeichen) in {"W", "F"} else 1)
        if benutzt + platz > breite:
            return ausgabe[:-1] + "~" if ausgabe else ""
        ausgabe += zeichen
        benutzt += platz
    return ausgabe


@dataclass
class _Datei:
    pfad: str
    sha256: str
    beginn: float = field(default_factory=time.time)
    letzte: str | None = None
    db_beginn: float | None = None


class Anzeige:
    """Besitzt nur Darstellungsdaten; die Warteschleife bleibt massgeblich."""

    def __init__(self, klient: OCRClient, *, stream: TextIO | None = None) -> None:
        self.klient = klient
        self.stream = sys.stderr if stream is None else stream
        self.dateien: list[_Datei] = []
        self.zeilen = 0
        self.block: list[str] = []
        self.ordner: Path | None = None
        self.gesucht = False
        self.beginn = time.monotonic()

    def hinzufuegen(self, pfad: Path, sha256: str) -> None:
        self.dateien.append(_Datei(_sicher(str(pfad)), sha256))

    def treffer(self, pfad: Path, huelle: OCRWrapper) -> None:
        self.stream.write(f"{_sicher(str(pfad))}: Datenbanktreffer ({huelle.status.value})\n")
        self.stream.flush()

    def _fortschritte(
        self, identitaet: tuple[Any, ...] | None, ende: float,
    ) -> dict[str, Any]:
        if not self.gesucht and identitaet is not None:
            self.gesucht = True
            try:
                self.ordner = daemons.verzeichnis(self.klient.settings, identitaet=identitaet)
            except (MineruOcrError, psycopg.Error, OSError):
                pass
        daten: dict[str, Any] = {}
        if self.ordner is not None:
            # Ein fremder oder alter Daemon darf die Anzeige nicht aufhalten.
            for pfad in sorted(self.ordner.glob("*.sock")):
                rest = ende - time.monotonic()
                if rest <= 0:
                    break
                antwort = daemons.fragen(pfad, "progress", timeout=min(0.05, rest))
                if antwort and isinstance(antwort.get("progress"), dict):
                    for sha, stand in antwort["progress"].items():
                        if isinstance(stand, dict):
                            daten[sha] = {**stand, "worker_ids": antwort.get("worker_ids", [])}
        return daten

    def refresh(self, ende: float | None = None) -> None:
        # Das Budget beginnt in der Warteschleife, vor jeder Beobachtung.
        ende = min(time.monotonic() + 0.2, ende) if ende is not None else time.monotonic() + 0.2
        if time.monotonic() >= ende:
            return
        try:
            self._refresh(ende)
        except (MineruOcrError, psycopg.Error, OSError, ValueError, TypeError, KeyError, TimeoutError):
            # Alte oder unvollstaendige Socket-Antworten sind keine OCR-Fehler.
            self.schliessen()

    def _refresh(self, ende: float) -> None:
        try:
            # Die Client-Verbindung kennt den ausgewaehlten Server bereits.
            # hostaddr erspart DNS im Beobachter und damit auch das Warten auf
            # einen blockierten Resolver beim Schliessen von asyncio.run.
            info = self.klient.conn.info
            dsn = psycopg.conninfo.make_conninfo(
                self.klient.settings.db_dsn,
                host=info.host, hostaddr=info.hostaddr, port=info.port,
            )
            staende, identitaet = asyncio.run(db.read_wait_snapshot(
                dsn, [d.sha256 for d in self.dateien],
                timeout=max(0, ende - time.monotonic()),
            ))
            fortschritte = self._fortschritte(identitaet, ende)
        except (MineruOcrError, psycopg.Error, OSError):
            # Beobachtung darf Erfolg, Wartefrist und Exit-Code nicht aendern.
            self.schliessen()
            return
        nach_hash = {s["sha256"]: s for s in staende}
        tty = self.stream.isatty()
        try:
            groesse = os.get_terminal_size(self.stream.fileno())
        except (OSError, ValueError, AttributeError):
            groesse = shutil.get_terminal_size((120, 24))
        hoehe = max(1, groesse.lines - 2)
        block: list[str] = []
        erste, letzte = 0, len(self.dateien)
        if tty and len(self.dateien) > hoehe:
            plaetze = max(1, hoehe - 1)
            seiten = (len(self.dateien) + plaetze - 1) // plaetze
            seite = int((time.monotonic() - self.beginn) / 5) % seiten
            erste = seite * plaetze
            letzte = min(erste + plaetze, len(self.dateien))
            block.append(f"Dateien {erste + 1}-{letzte} von {len(self.dateien)} (Wechsel alle 5s)")
        for nr, datei in enumerate(self.dateien):
            stand = nach_hash[datei.sha256]
            jetzt = float(stand["jetzt"])
            if stand["beginn"]:
                datei.db_beginn = stand["beginn"].timestamp()
            beginn = datei.db_beginn if datei.db_beginn is not None else datei.beginn
            seit = stand["schritt"].timestamp() if stand["schritt"] else datei.beginn
            detail = "RunPod: unbekannt"
            phase = "gespeichert; Job unbekannt"
            if stand["job_status"] == "todo":
                phase = "wartet auf Daemon-Platz"
                if stand["scheduled_at"] and stand["scheduled_at"].timestamp() > jetzt:
                    phase = "wartet auf Wiedervorlage"
            elif stand["job_status"] == "doing":
                phase = "Daemon verarbeitet"
            elif stand["job_status"]:
                phase = f"Job {stand['job_status']}; ohne Ergebnis"
            fluechtig = fortschritte.get(datei.sha256, {})
            if not (stand["job_status"] == "doing" and fluechtig.get("job_id") == stand["id"]
                    and stand["worker_id"] in fluechtig.get("worker_ids", [])):
                fluechtig = {}
            if fluechtig:
                phase = _sicher(str(fluechtig.get("schritt", phase)))
                seit = float(fluechtig.get("seit", seit))
                detail = (f"RunPod: {fluechtig.get('runpod') or 'Status unbekannt'}; "
                          f"Chunks gesendet {fluechtig.get('gesendet', 0)}, "
                          f"beendet {fluechtig.get('fertig', 0)}, "
                          f"Plan {fluechtig.get('geplant') or '?'} (Teilung moeglich)")
            if stand["final"]:
                jetzt = stand["updated_at"].timestamp()
                phase = f"final {stand['final']}"
                seit = stand["updated_at"].timestamp()
                detail = "RunPod: nicht mehr beobachtbar"
            elif not stand["stored"]:
                phase = "Eingabe und Ergebnis unbekannt"
            gespeichert = "Ergebnis" if stand["final"] else ("ja" if stand["stored"] else "nein")
            versuch = "" if stand["attempts"] is None else f"; Wiederholungen {stand['attempts']}"
            # Uhren zaehlen auf TTY weiter; im Protokoll entscheidet nur der Fakt.
            fakten = f"{phase}; DB: {gespeichert}; {detail}{versuch}"
            zeit = f"Schritt {max(0, int(jetzt - seit))}s / gesamt {max(0, int(jetzt - beginn))}s"
            zeile = f"{datei.pfad}: {fakten}; {zeit}"
            if tty and erste <= nr < letzte:
                breite = groesse.columns - 1
                if breite < 140:
                    kurzphase = phase
                    if stand["job_status"] == "todo":
                        kurzphase = "Daemon-Platz" if phase == "wartet auf Daemon-Platz" else "Wiedervorlage"
                    if fluechtig.get("runpod"):
                        kurzphase = "RP:" + _sicher(str(fluechtig["runpod"]))
                    kurz = f"{kurzphase} DB:{gespeichert} RP:?"
                    if fluechtig.get("job_id") == stand["id"] and stand["job_status"] == "doing":
                        kurz = (f"{kurzphase} DB:{gespeichert} ges:{fluechtig.get('gesendet', 0)} "
                                f"fertig:{fluechtig.get('fertig', 0)} Plan:{fluechtig.get('geplant') or '?'}")
                    if stand["attempts"] is not None:
                        kurz += f" W:{stand['attempts']}"
                    zeit = f"S:{max(0, int(jetzt - seit))}s T:{max(0, int(jetzt - beginn))}s"
                    fakten_tty = kurz
                else:
                    fakten_tty = fakten
                # Zeiten bleiben auch auf einem schmalen Terminal sichtbar.
                label = Path(datei.pfad).name
                if sum(Path(d.pfad).name == label for d in self.dateien) > 1:
                    label = str(Path(datei.pfad).parent.name + "/" + label)
                kopf = _kuerzen(f"{_kuerzen(label, 16)}: {fakten_tty}", max(1, breite - len(zeit) - 3))
                block.append(f"{kopf} | {zeit}")
            elif not tty and fakten != datei.letzte:
                self.stream.write(zeile + "\n")
            datei.letzte = fakten
        if tty:
            if self.zeilen:
                # Beim Verkleinern bricht das Terminal den alten Frame um.
                # Zurueck geht es um physische Zeilen, nicht um Dateizeilen.
                spalten = max(1, groesse.columns)
                physisch = sum(max(1, (_breite(z) + spalten - 1) // spalten) for z in self.block)
                self.stream.write(f"\x1b[{min(physisch, hoehe)}A\r\x1b[J")
            self.block = [_kuerzen(z, max(1, groesse.columns - 1)) for z in block[:hoehe]]
            for zeile_tty in self.block:
                self.stream.write("\r\x1b[2K" + zeile_tty + "\n")
            self.zeilen = len(self.block)
        self.stream.flush()

    def schliessen(self) -> None:
        """Der Cursor steht nach dem Block; stdout darf wieder schreiben."""
        self.zeilen = 0
        self.block = []

    def wait(self, sha256: str, timeout: float | None) -> OCRWrapper:
        try:
            with closing(db.wait_for_terminal(
                self.klient.settings.db_dsn, [sha256], timeout=timeout,
                poll_seconds=1, _on_poll=self.refresh,
            )) as ergebnisse:
                zeile = next(ergebnisse)
            return OCRWrapper.from_row(zeile, self.klient.conn)
        finally:
            self.schliessen()
