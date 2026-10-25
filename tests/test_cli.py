"""Die Kommandozeile — Exit-Codes und die Regel, dass sie nur übersetzt.

Die Exit-Codes sind im Vertrag ausdrücklich Zusage, weil in einer Shell darauf
verzweigt wird. Eine Zusage ohne Test ist eine Absichtserklärung.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner

from mineru_ocr import cli as cli_modul
from mineru_ocr import client as client_modul
from mineru_ocr import db
from mineru_ocr.cli import app
from mineru_ocr.config import Settings
from mineru_ocr.db import DocumentStatus, write_terminal_async
from mineru_ocr.errors import DatabaseError
from mineru_ocr.wrapper import OCRWrapper

pytestmark = pytest.mark.db

PNG_KOPF = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
SCHLUESSEL = "ganz-geheimer-schluessel"


class Zugang:
    """Der RunPod-Zugang als Doppel — dieselbe Naht wie in test_client."""

    async def endpoint(self) -> dict[str, Any]:
        return {
            "id": "testendpoint",
            "workersMin": 0,
            "workersMax": 10,
            "idleTimeout": 60,
            "executionTimeoutMs": 600000,
            "workers": [],
        }

    async def image_tag(self) -> str | None:
        return ":neu12345"

    async def balance(self) -> float:
        return 5.0

    async def account(self) -> dict[str, Any]:
        return {"clientBalance": 5.0}

    async def workers(self) -> list[dict[str, Any]]:
        return []


@pytest.fixture
def cli(
    monkeypatch: pytest.MonkeyPatch,
    _test_datenbank: str,
    db_conn: psycopg.Connection[Any],
) -> CliRunner:
    """Ein Laeufer fuer die Kommandos, mit Test-Datenbank und ohne Netz.

    Die Kommandos bauen ihren Client selbst — sie bekommen die
    Verbindungszeichenkette also nicht als Argument. Deshalb wird `Settings.load`
    hier so umgebogen, dass die Voreinstellung auf die Test-Datenbank zeigt.
    Alles Uebrige bleibt, wie es im Betrieb waere.
    """
    echtes_load = Settings.load

    def auf_testdatenbank(**angaben: Any) -> Settings:
        angaben.setdefault("db_dsn", None)
        if angaben["db_dsn"] is None:
            angaben["db_dsn"] = _test_datenbank
        angaben.setdefault("api_key", SCHLUESSEL)
        return echtes_load(**angaben)

    monkeypatch.setattr(Settings, "load", staticmethod(auf_testdatenbank))
    monkeypatch.setattr(
        client_modul,
        "_runpod_fragen",
        lambda settings, was: client_modul._synchron_ausfuehren(was(Zugang())),
    )
    # Ein lebendes Lebenszeichen, damit `ocr` nicht warnt, es laufe kein Daemon.
    db_conn.execute("INSERT INTO procrastinate_workers DEFAULT VALUES")
    return CliRunner()


def _datei(
    ordner: Path, name: str, inhalt: bytes | None = None
) -> tuple[Path, str]:
    """Schreibt eine Datei und gibt sie samt dem Hash ihrer Bytes.

    Legt nichts in der Datenbank an: Das tut erst `ocr`.
    """
    pfad = ordner / name
    pfad.parent.mkdir(parents=True, exist_ok=True)
    inhalt = PNG_KOPF + name.encode() if inhalt is None else inhalt
    pfad.write_bytes(inhalt)
    return pfad, hashlib.sha256(inhalt).hexdigest()


def _anlegen(
    db_conn: psycopg.Connection[Any], tmp_path: Path, name: str, dsn: str
) -> OCRWrapper:
    """Eine Datei samt Zeile `new` und Eingabe, ohne Job und ohne `ocr`.

    Fuer die Tests, die ein Ergebnis in die Zeile schreiben und dann ein Kommando
    ansehen. Die Datei liegt da, damit sich das Kommando auch mit ihrem Pfad
    aufrufen laesst.
    """
    pfad, sha256 = _datei(tmp_path, name)
    inhalt = pfad.read_bytes()
    with db_conn.transaction():
        db.insert_document(
            db_conn,
            sha256=sha256,
            size_bytes=len(inhalt),
            mime="image/png",
            endpoint_id=Settings.load(db_dsn=dsn, api_key=SCHLUESSEL).endpoint_id,
        )
        db.insert_input(db_conn, sha256=sha256, content=inhalt)
    zeile = db.get_document(db_conn, sha256, with_raw=False)
    assert zeile is not None
    return OCRWrapper.from_row(zeile, db_conn)


# --- Exit-Codes -----------------------------------------------------------


def test_erfolg_ist_null(cli: CliRunner, tmp_path: Path) -> None:
    """Jedes Kommando gibt bei Erfolg 0."""
    for argumente in (["setup"], ["status"], ["preflight"]):
        ergebnis = cli.invoke(app, argumente)
        assert ergebnis.exit_code == 0, (argumente, ergebnis.output)

    datei = tmp_path / "a.png"
    datei.write_bytes(PNG_KOPF + b"a")
    ergebnis = cli.invoke(app, ["ocr", str(datei)])
    assert ergebnis.exit_code == 0, ergebnis.output


async def test_fertiges_dokument_gibt_null(
    cli: CliRunner,
    db_conn: psycopg.Connection[Any],
    async_conn: psycopg.AsyncConnection[Any],
    _test_datenbank: str,
    tmp_path: Path,
) -> None:
    """`wait` und `markdown` geben 0 bei `done` und bei `empty`."""
    for name, zustand in (("done.png", DocumentStatus.DONE), ("empty.png", DocumentStatus.EMPTY)):
        huelle = _anlegen(db_conn, tmp_path, name, _test_datenbank)
        await write_terminal_async(
            async_conn,
            sha256=huelle.sha256,
            status=zustand,
            raw={"markdown": "Text", "content_list": [], "images": {}},
        )
        assert cli.invoke(app, ["wait", huelle.sha256]).exit_code == 0, zustand
        ergebnis = cli.invoke(app, ["markdown", huelle.sha256])
        assert ergebnis.exit_code == 0, zustand
        assert "Text" in ergebnis.output


async def test_gescheitertes_dokument_gibt_drei(
    cli: CliRunner,
    db_conn: psycopg.Connection[Any],
    async_conn: psycopg.AsyncConnection[Any],
    _test_datenbank: str,
    tmp_path: Path,
) -> None:
    """`wait` und `markdown` geben 3 bei `failed` und `input_error`.

    Ein eigener Code, weil das kein Fehler DES WERKZEUGS ist. Wer beides auf 1
    wirft, kann in einer Schleife nicht mehr zwischen „kaputtes Dokument" und
    „kaputte Einrichtung" unterscheiden.
    """
    for name, zustand in (
        ("failed.png", DocumentStatus.FAILED),
        ("input.png", DocumentStatus.INPUT_ERROR),
    ):
        huelle = _anlegen(db_conn, tmp_path, name, _test_datenbank)
        await write_terminal_async(
            async_conn,
            sha256=huelle.sha256,
            status=zustand,
            raw={"markdown": "Teilstueck"},
            error="aus",
        )
        assert cli.invoke(app, ["wait", huelle.sha256]).exit_code == 3, zustand
        assert cli.invoke(app, ["markdown", huelle.sha256]).exit_code == 3, zustand


def test_betriebsfehler_gibt_eins(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Endpoint nicht erreichbar, Datenbank weg: 1."""
    echtes_load = Settings.load
    monkeypatch.setattr(
        Settings,
        "load",
        staticmethod(
            lambda **a: echtes_load(
                **{**a, "db_dsn": "host=localhost port=1 dbname=gibtesnicht"}
            )
        ),
    )
    ergebnis = cli.invoke(app, ["status"])
    assert ergebnis.exit_code == 1


def test_falsches_argument_gibt_zwei(cli: CliRunner) -> None:
    """Unbekannter Pfad, fehlendes Argument: 2 (von typer gesetzt)."""
    assert cli.invoke(app, ["show"]).exit_code == 2
    assert cli.invoke(app, ["gibtesnicht"]).exit_code == 2
    # Und `retry` ohne Ziel UND ohne `--failed`: nicht klar, was gemeint ist.
    assert cli.invoke(app, ["retry"]).exit_code == 2


async def test_markdown_gibt_bei_misserfolg_nichts_aus(
    cli: CliRunner,
    db_conn: psycopg.Connection[Any],
    async_conn: psycopg.AsyncConnection[Any],
    _test_datenbank: str,
    tmp_path: Path,
) -> None:
    """Kein halber Text auf der Standardausgabe — er saehe vollstaendig aus."""
    huelle = _anlegen(db_conn, tmp_path, "halb.png", _test_datenbank)
    await write_terminal_async(
        async_conn,
        sha256=huelle.sha256,
        status=DocumentStatus.FAILED,
        raw={"markdown": "NUR EIN TEILSTUECK"},
        error="mittendrin",
    )
    ergebnis = cli.invoke(app, ["markdown", huelle.sha256])
    assert ergebnis.exit_code == 3
    assert "NUR EIN TEILSTUECK" not in ergebnis.stdout


# --- Keine zweite Wahrheit ------------------------------------------------


def test_strict_ist_ueberall_gleich_voreingestellt() -> None:
    """`mineru-ocr preflight` und `OCRClient()` verhalten sich gleich.

    Der Test gegen genau das, was der Vorspann der Datei verbietet: dieselbe
    Pruefung ueber die Kommandozeile nachsichtiger als aus Python heraus.
    """
    from mineru_ocr.client import OCRClient

    aus_python = inspect.signature(OCRClient.__init__).parameters["strict"].default
    aus_cli = inspect.signature(cli_modul.preflight).parameters["strict"].default
    assert aus_python is True
    assert aus_cli is True
    assert aus_python == aus_cli


def test_kein_kommando_rechnet_selbst() -> None:
    """Jedes Kommando ruft genau eine oeffentliche Funktion und formatiert deren
    Rueckgabe.

    Geprueft an dem, was die Kommandozeile NICHT importiert: Sie kennt weder den
    Verarbeitungsweg (`executor`) noch den Zuschnitt (`chunking`) noch den
    RunPod-Zugang (`runpod`) noch das Zusammensetzen (`assembly`). Wer eine
    Schwelle oder eine Regel in ein Kommando einbaute, muesste eines davon
    importieren — und faellt hier auf.

    `daemon` ist die eine Ausnahme, und sie ist harmlos: Es importiert
    `mineru_ocr.daemon` erst IM Funktionskoerper, damit `mineru-ocr status` nicht
    den ganzen Verarbeitungsweg laden muss.
    """
    quelle = inspect.getsource(cli_modul)
    kopf = quelle.split("def setup(")[0]
    for verboten in ("executor", "chunking", "assembly", "runpod"):
        assert f"from .{verboten}" not in kopf, verboten
        assert f"import {verboten}" not in kopf, verboten


def test_es_gibt_kein_kommando_das_mutiert() -> None:
    """Keines legt eine bezahlte Ressource an oder aendert eine — nicht
    abgeschaltet, sondern nicht vorhanden."""
    from typer.main import get_command

    namen = set(get_command(app).commands)  # type: ignore[attr-defined]
    assert namen == {
        "setup",
        "daemon",
        "ocr",
        "status",
        "show",
        "wait",
        "markdown",
        "retry",
        "preflight",
    }
    quelle = inspect.getsource(cli_modul)
    for verboten in ("workersMax", "PATCH", "DELETE", "create_endpoint"):
        assert verboten not in quelle, verboten


# --- Ausgabe --------------------------------------------------------------


def test_json_ist_maschinenlesbar(cli: CliRunner, tmp_path: Path) -> None:
    """`--json` gibt gueltiges JSON und sonst nichts auf die Standardausgabe."""
    for argumente in (["status", "--json"], ["preflight", "--json"]):
        ergebnis = cli.invoke(app, argumente)
        assert ergebnis.exit_code == 0, ergebnis.output
        json.loads(ergebnis.stdout)


async def test_json_traegt_kein_raw(
    cli: CliRunner,
    db_conn: psycopg.Connection[Any],
    async_conn: psycopg.AsyncConnection[Any],
    _test_datenbank: str,
    tmp_path: Path,
) -> None:
    """Auch hier nicht — zweistellige Megabyte je Dokument."""
    huelle = _anlegen(db_conn, tmp_path, "gross.png", _test_datenbank)
    await write_terminal_async(
        async_conn,
        sha256=huelle.sha256,
        status=DocumentStatus.DONE,
        raw={"markdown": "x" * 5000, "content_list": [], "images": {}},
    )
    ergebnis = cli.invoke(app, ["show", huelle.sha256, "--json"])
    assert ergebnis.exit_code == 0, ergebnis.output
    angaben = json.loads(ergebnis.stdout)
    assert "raw" not in angaben
    assert "x" * 100 not in ergebnis.stdout


def test_schluessel_erscheint_in_keiner_ausgabe(
    cli: CliRunner, tmp_path: Path
) -> None:
    """Weder im Bericht, noch in `--json`, noch in einer Fehlermeldung."""
    for argumente in (
        ["status"],
        ["status", "--json"],
        ["preflight"],
        ["preflight", "--json"],
        ["show", "f" * 64],
    ):
        ergebnis = cli.invoke(app, argumente)
        assert SCHLUESSEL not in ergebnis.output, argumente


def test_traceback_ist_schlicht() -> None:
    """`pretty_exceptions_enable=False`: kein reich formatierter Traceback mit
    lokalen Variablen, unter denen eine Einstellung stehen koennte.

    Deren `__repr__` maskiert den Schluessel zwar — aber sich darauf zu
    verlassen hiesse, eine Sicherheitszusage an die Darstellungs-Logik einer
    fremden Bibliothek zu haengen.
    """
    quelle = inspect.getsource(cli_modul.main)
    assert "pretty_exceptions_enable = False" in quelle


# --- Namen ----------------------------------------------------------------


def test_kommandonamen_folgen_der_python_flaeche() -> None:
    """`ocr`, `status`, `wait` heissen wie ihre Methoden. Zwei Vokabulare fuer
    dieselbe Sache waeren dieselbe Doppelung wie zwei Wahrheiten im Verhalten."""
    from mineru_ocr.client import OCRClient

    for name in ("status", "wait", "preflight"):
        assert hasattr(OCRClient, name), name
    # `ocr` heisst wie `OCRClient.ocr` und `ocr_start`, `retry` wie `retry_failed`.
    assert hasattr(OCRClient, "ocr")
    assert hasattr(OCRClient, "ocr_start")
    assert hasattr(OCRClient, "retry_failed")


async def test_hash_und_pfad_werden_gleich_aufgeloest(
    cli: CliRunner,
    db_conn: psycopg.Connection[Any],
    async_conn: psycopg.AsyncConnection[Any],
    _test_datenbank: str,
    tmp_path: Path,
) -> None:
    """Vierundsechzig Zeichen aus `[0-9a-f]` sind ein Hash, alles andere ein
    Pfad — in jedem Kommando dieselbe Regel."""
    huelle = _anlegen(db_conn, tmp_path, "beide.png", _test_datenbank)
    await write_terminal_async(
        async_conn,
        sha256=huelle.sha256,
        status=DocumentStatus.DONE,
        raw={"markdown": "derselbe Text", "content_list": [], "images": {}},
    )

    ueber_hash = cli.invoke(app, ["markdown", huelle.sha256])
    ueber_pfad = cli.invoke(app, ["markdown", str(tmp_path / "beide.png")])
    assert ueber_hash.exit_code == ueber_pfad.exit_code == 0
    assert ueber_hash.stdout == ueber_pfad.stdout
    assert "derselbe Text" in ueber_hash.stdout


def test_show_zeigt_den_juengsten_job(
    cli: CliRunner,
    db_conn: psycopg.Connection[Any],
    _test_datenbank: str,
    tmp_path: Path,
) -> None:
    """Der Job-Teil ist die Antwort auf „warum passiert nichts?".

    Er zeigt, ob das Dokument wartet, laeuft, wie oft es schon versucht wurde,
    oder ob sein juengster Job endete, ohne etwas zu schreiben.
    """
    datei, sha = _datei(tmp_path, "wartend.png")
    cli.invoke(app, ["ocr", str(datei)])

    ergebnis = cli.invoke(app, ["show", sha, "--json"])
    assert ergebnis.exit_code == 0, ergebnis.output
    angaben = json.loads(ergebnis.stdout)
    assert angaben["phase"] == "waiting"
    assert angaben["job"]["status"] == "todo"


def test_status_sagt_wenn_kein_daemon_laeuft(
    cli: CliRunner, db_conn: psycopg.Connection[Any]
) -> None:
    """Die haeufigste Betriebs-Ueberraschung: Alles sieht normal aus, nur
    arbeitet niemand."""
    db_conn.execute("DELETE FROM procrastinate_workers")
    ergebnis = cli.invoke(app, ["status"])
    assert ergebnis.exit_code == 0
    assert "LAEUFT NICHT" in ergebnis.output
    assert "mineru-ocr daemon" in ergebnis.output


def test_status_zaehlt_einen_alten_herzschlag_nicht_als_daemon(
    cli: CliRunner,
    db_conn: psycopg.Connection[Any],
    abgestuerzter_worker: Callable[[], None],
) -> None:
    """Nach einem Absturz bleibt die Worker-Zeile stehen, und `status` darf sie
    nicht fuer einen laufenden Daemon halten (eine 67 Tage alte tat es)."""
    db_conn.execute("DELETE FROM procrastinate_workers")
    abgestuerzter_worker()
    ergebnis = cli.invoke(app, ["status"])
    assert ergebnis.exit_code == 0
    assert "LAEUFT NICHT" in ergebnis.output


def test_status_sagt_unbekannt_wenn_die_lebendigkeit_nicht_lesbar_ist(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Die Lebendigkeit ist nur Auskunft: `status` scheitert nicht an ihr, und es
    behauptet auch nicht „LAEUFT NICHT“, wo es das nicht weiss."""

    def kaputt(conn: psycopg.Connection[Any]) -> Any:
        raise DatabaseError("procrastinate_workers sieht anders aus")

    monkeypatch.setattr(db, "worker_heartbeats", kaputt)

    ergebnis = cli.invoke(app, ["status"])

    assert ergebnis.exit_code == 0, ergebnis.output
    assert "Daemon:     unbekannt" in ergebnis.output
    assert "LAEUFT NICHT" not in ergebnis.output
    # Auch der Hinweis „Es laeuft kein Daemon“ bleibt aus: Wer nicht weiss, ob
    # einer laeuft, soll nicht zum Starten eines zweiten aufgefordert werden.
    assert "kein Daemon" not in ergebnis.output


def _zaehlen(db_conn: psycopg.Connection[Any], tabelle: str) -> int:
    zeile = db_conn.execute(f"SELECT count(*) FROM {tabelle}").fetchone()
    assert zeile is not None
    return int(zeile[0])


def test_ocr_ohne_daemon_laesst_keine_eingabe_ohne_job_zurueck(
    cli: CliRunner, db_conn: psycopg.Connection[Any], tmp_path: Path
) -> None:
    """Seit die Bytes in `inputs` liegen, ist „abgelegt“ ein Zustand der Datenbank:
    Wer ohne Daemon abschickt, soll danach zu jeder abgelegten Eingabe einen Job
    haben. Frueher stand das auf dem Abbruch („reiht nichts ein“); jetzt steht es
    darauf, dass eingereiht wird."""
    db_conn.execute("DELETE FROM procrastinate_workers")
    for name in ("a.png", "b.png"):
        (tmp_path / name).write_bytes(PNG_KOPF + name.encode())

    ergebnis = cli.invoke(app, ["ocr", str(tmp_path)])

    assert ergebnis.exit_code == 0, ergebnis.output
    assert _zaehlen(db_conn, "inputs") == 2
    assert _zaehlen(db_conn, "procrastinate_jobs") == 2


def _jobs(db_conn: psycopg.Connection[Any]) -> int:
    zeile = db_conn.execute("SELECT count(*) FROM procrastinate_jobs").fetchone()
    assert zeile is not None
    return int(zeile[0])


def test_ocr_reiht_eine_datei_ein_und_nennt_pfad_und_hash(
    cli: CliRunner, db_conn: psycopg.Connection[Any], tmp_path: Path
) -> None:
    """Pfade kennt nur die Kommandozeile: Sie liest die Datei, hasht sie und nennt
    die Zuordnung von Pfad zu Hash in ihrer Ausgabe."""
    datei, sha = _datei(tmp_path, "a.png")


    ergebnis = cli.invoke(app, ["ocr", str(datei)])

    assert ergebnis.exit_code == 0, ergebnis.output
    assert sha in ergebnis.stdout
    assert str(datei) in ergebnis.stdout
    assert _jobs(db_conn) == 1


def test_ocr_json_nennt_je_dokument_hash_zustand_und_ob_eingereiht(
    cli: CliRunner, db_conn: psycopg.Connection[Any], tmp_path: Path
) -> None:
    datei, sha = _datei(tmp_path, "a.png")

    erste = json.loads(cli.invoke(app, ["ocr", str(datei), "--json"]).stdout)
    assert erste == {
        "documents": [
            {"path": str(datei), "sha256": sha, "status": "new", "queued": True}
        ],
        "skipped": [],
    }

    # Dasselbe noch einmal: Das Dokument ist unterwegs, es geschieht nichts.
    zweite = json.loads(cli.invoke(app, ["ocr", str(datei), "--json"]).stdout)
    assert zweite["documents"] == [
        {"path": str(datei), "sha256": sha, "status": "new", "queued": False}
    ]
    assert _jobs(db_conn) == 1


async def test_ocr_json_nennt_ein_ergebnis_als_treffer_und_reiht_nichts_ein(
    cli: CliRunner,
    db_conn: psycopg.Connection[Any],
    async_conn: psycopg.AsyncConnection[Any],
    tmp_path: Path,
) -> None:
    """Ein Treffer gilt, auch bei `failed`; es entsteht kein Job."""
    datei, sha = _datei(tmp_path, "fertig.png")
    cli.invoke(app, ["ocr", str(datei)])
    db_conn.execute("DELETE FROM procrastinate_jobs")
    await write_terminal_async(
        async_conn, sha256=sha, status=DocumentStatus.FAILED, raw={}, error="aus"
    )

    ergebnis = json.loads(cli.invoke(app, ["ocr", str(datei), "--json"]).stdout)

    assert ergebnis["documents"] == [
        {"path": str(datei), "sha256": sha, "status": "failed", "queued": False}
    ]
    assert _jobs(db_conn) == 0


def test_ocr_ordner_ueberspringt_nicht_unterstuetztes_und_nennt_es(
    cli: CliRunner, db_conn: psycopg.Connection[Any], tmp_path: Path
) -> None:
    """In Ordnern werden nicht unterstuetzte Dateien uebersprungen und gezaehlt,
    statt an der ersten `.DS_Store` zu sterben."""
    ordner = tmp_path / "scans"
    _datei(ordner, "a.png")
    _datei(ordner, "unter/b.png")
    (ordner / "notiz.txt").write_text("Sehr geehrte Damen und Herren")

    ergebnis = cli.invoke(app, ["ocr", str(ordner), "--json"])

    assert ergebnis.exit_code == 0, ergebnis.output
    angaben = json.loads(ergebnis.stdout)
    assert len(angaben["documents"]) == 2
    assert [s["reason"] for s in angaben["skipped"]] == ["nicht_unterstuetzter_typ"]
    assert angaben["skipped"][0]["path"] == str(ordner / "notiz.txt")
    assert _jobs(db_conn) == 2


def test_ocr_ordner_nennt_in_der_zaehlzeile_die_uebersprungenen_nach_grund(
    cli: CliRunner, tmp_path: Path
) -> None:
    """Die Zaehlzeile auf stderr sagt, WARUM Dateien uebersprungen wurden, mit
    Zahl je Grund — auch bei einem Grund, der mehrfach vorkommt."""
    ordner = tmp_path / "scans"
    _datei(ordner, "a.png")
    (ordner / "x.txt").write_text("Text")
    (ordner / "y.txt").write_text("Anderer Text")
    (ordner / "film.mov").write_bytes(b"\x00\x00\x00\x14ftypqt  " + b"x" * 64)

    ergebnis = cli.invoke(app, ["ocr", str(ordner)])

    assert ergebnis.exit_code == 0, ergebnis.output
    assert "3 uebersprungen (nicht_unterstuetzter_typ: 3)" in ergebnis.stderr


def test_ocr_nennt_nach_strg_c_mitten_im_lauf_die_bis_dahin_eingereihten(
    cli: CliRunner,
    db_conn: psycopg.Connection[Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auch ein Strg-C beendet den Lauf nicht, ohne zu sagen, was schon eingereiht
    ist. Das Kommando endet unsauber (Exit-Code ungleich 0)."""
    dateien = [_datei(tmp_path, name) for name in ("a.png", "b.png", "c.png")]
    echtes = client_modul.OCRClient.ocr_start
    aufrufe = {"n": 0}

    def zweiter_wird_unterbrochen(self: Any, data: Any) -> Any:
        aufrufe["n"] += 1
        if aufrufe["n"] == 2:
            raise KeyboardInterrupt
        return echtes(self, data)

    monkeypatch.setattr(client_modul.OCRClient, "ocr_start", zweiter_wird_unterbrochen)

    ergebnis = cli.invoke(app, ["ocr", str(tmp_path), "--json"])

    assert ergebnis.exit_code != 0
    angaben = json.loads(ergebnis.stdout)
    assert [d["sha256"] for d in angaben["documents"]] == [dateien[0][1]]
    assert _jobs(db_conn) == 1


def test_ocr_ordner_waechst_und_ein_zweiter_lauf_reiht_nur_das_neue_ein(
    cli: CliRunner, db_conn: psycopg.Connection[Any], tmp_path: Path
) -> None:
    """Der vorgesehene Gebrauch: Ein Ordner wird wieder und wieder durchlaufen, und
    es laeuft nur, was noch fehlt."""
    ordner = tmp_path / "scans"
    _datei(ordner, "a.png")
    cli.invoke(app, ["ocr", str(ordner)])
    _datei(ordner, "b.png")

    angaben = json.loads(cli.invoke(app, ["ocr", str(ordner), "--json"]).stdout)

    assert [d["queued"] for d in angaben["documents"]] == [False, True]
    assert _jobs(db_conn) == 2


def test_ocr_ordner_nimmt_ein_muster(
    cli: CliRunner, db_conn: psycopg.Connection[Any], tmp_path: Path
) -> None:
    ordner = tmp_path / "scans"
    _datei(ordner, "a.png")
    _datei(ordner, "unter/b.png")

    angaben = json.loads(
        cli.invoke(app, ["ocr", str(ordner), "--glob", "*.png", "--json"]).stdout
    )

    assert len(angaben["documents"]) == 1
    assert angaben["documents"][0]["path"] == str(ordner / "a.png")


def test_ocr_eine_ausdruecklich_genannte_nicht_unterstuetzte_datei_haelt_die_uebrigen_nicht_auf(
    cli: CliRunner, db_conn: psycopg.Connection[Any], tmp_path: Path
) -> None:
    """Wer eine Datei beim Namen nennt, meint sie: `ocr` meldet sie mit Grund auf
    der Fehlerausgabe und endet mit 1 — reiht aber die uebrigen trotzdem ein."""
    vorn, _ = _datei(tmp_path, "vorn.png")
    schlecht = tmp_path / "notizen.txt"
    schlecht.write_text("Sehr geehrte Damen und Herren")
    hinten, _ = _datei(tmp_path, "hinten.png")

    ergebnis = cli.invoke(app, ["ocr", str(vorn), str(schlecht), str(hinten), "--json"])

    assert ergebnis.exit_code == 1
    assert "notizen.txt" in ergebnis.stderr
    assert "Kein unterstuetzter Typ" in ergebnis.stderr
    angaben = json.loads(ergebnis.stdout)
    assert [d["path"] for d in angaben["documents"]] == [str(vorn), str(hinten)]
    assert [s["path"] for s in angaben["skipped"]] == [str(schlecht)]
    assert _jobs(db_conn) == 2


def test_ocr_eine_ausdruecklich_genannte_zu_grosse_datei_haelt_die_uebrigen_nicht_auf(
    cli: CliRunner,
    db_conn: psycopg.Connection[Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(db, "MAX_INPUT_BYTES", 40)
    klein, _ = _datei(tmp_path, "klein.png", PNG_KOPF)
    gross = tmp_path / "gross.png"
    gross.write_bytes(PNG_KOPF + b"x" * 100)

    ergebnis = cli.invoke(app, ["ocr", str(gross), str(klein), "--json"])

    assert ergebnis.exit_code == 1
    assert "gross.png" in ergebnis.stderr
    angaben = json.loads(ergebnis.stdout)
    assert [d["path"] for d in angaben["documents"]] == [str(klein)]
    assert [s["reason"] for s in angaben["skipped"]] == ["zu_gross"]
    assert _jobs(db_conn) == 1


def test_ocr_eine_nicht_vorhandene_datei_hindert_die_uebrigen_nicht(
    cli: CliRunner, db_conn: psycopg.Connection[Any], tmp_path: Path
) -> None:
    gut, _ = _datei(tmp_path, "gut.png")

    ergebnis = cli.invoke(
        app, ["ocr", str(tmp_path / "gibt-es-nicht.png"), str(gut), "--json"]
    )

    assert ergebnis.exit_code == 1
    assert "gibt-es-nicht.png" in ergebnis.stderr
    assert len(json.loads(ergebnis.stdout)["documents"]) == 1
    assert _jobs(db_conn) == 1


def test_ocr_liest_eine_nicht_unterstuetzte_oder_zu_grosse_datei_nicht(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Eine grosse Datei fremden Typs darf nicht erst ganz in den Speicher, um dann
    abgewiesen zu werden."""
    film = tmp_path / "film.mov"
    film.write_bytes(b"\x00\x00\x00\x14ftypqt  " + b"x" * 100)
    gross = tmp_path / "gross.png"
    gross.write_bytes(PNG_KOPF + b"x" * 100)
    monkeypatch.setattr(db, "MAX_INPUT_BYTES", 40)

    echtes_lesen = Path.read_bytes

    def nie(self: Path) -> bytes:
        # Nur diese beiden Dateien; die Einstellungen lesen ihre eigene.
        if self.name in ("film.mov", "gross.png"):
            raise AssertionError("die Datei wurde ganz gelesen")
        return echtes_lesen(self)

    monkeypatch.setattr(Path, "read_bytes", nie)
    ergebnis = cli.invoke(app, ["ocr", str(film), str(gross)])

    assert ergebnis.exit_code == 1
    assert "film.mov" in ergebnis.stderr and "gross.png" in ergebnis.stderr


def test_ocr_ohne_daemon_reiht_trotzdem_ein_und_warnt_auf_stderr(
    cli: CliRunner, db_conn: psycopg.Connection[Any], tmp_path: Path
) -> None:
    """Abschicken haengt nicht davon ab, ob ein Daemon laeuft. Auf stdout stehen nur
    Daten, die Warnung geht auf stderr."""
    db_conn.execute("DELETE FROM procrastinate_workers")
    datei, sha = _datei(tmp_path, "a.png")
    _datei(tmp_path, "b.png")

    ergebnis = cli.invoke(app, ["ocr", str(tmp_path), "--json"])

    assert ergebnis.exit_code == 0, ergebnis.output
    angaben = json.loads(ergebnis.stdout)
    assert [d["queued"] for d in angaben["documents"]] == [True, True]
    assert sha in [d["sha256"] for d in angaben["documents"]]
    assert "mineru-ocr daemon" in ergebnis.stderr
    assert "kein Daemon" not in ergebnis.stdout
    # Einmal, nicht je Dokument.
    assert ergebnis.stderr.count("kein Daemon") == 1
    assert _jobs(db_conn) == 2


def test_ocr_warnt_ohne_daemon_einmal_je_lauf_und_nicht_je_datei(
    cli: CliRunner, db_conn: psycopg.Connection[Any], tmp_path: Path
) -> None:
    """Die Kommandozeile reicht die Dateien einzeln herein; trotzdem steht die
    Warnung einmal da und nicht dreissigtausend Mal."""
    db_conn.execute("DELETE FROM procrastinate_workers")
    for name in ("a.png", "b.png", "c.png"):
        _datei(tmp_path, name)

    ergebnis = cli.invoke(app, ["ocr", str(tmp_path)])

    assert ergebnis.exit_code == 0, ergebnis.output
    assert _jobs(db_conn) == 3
    assert ergebnis.stderr.count("kein Daemon") == 1


def test_ocr_warnt_ohne_daemon_auch_wenn_alles_schon_unterwegs_ist(
    cli: CliRunner, db_conn: psycopg.Connection[Any], tmp_path: Path
) -> None:
    """Ein zweiter Lauf ohne Daemon reiht nichts mehr ein, aber die Dokumente sind
    weiter offen: „0 eingereiht“ ohne Hinweis saehe aus wie ein Haenger."""
    db_conn.execute("DELETE FROM procrastinate_workers")
    _datei(tmp_path, "a.png")
    cli.invoke(app, ["ocr", str(tmp_path)])

    ergebnis = cli.invoke(app, ["ocr", str(tmp_path), "--json"])

    assert ergebnis.exit_code == 0, ergebnis.output
    assert json.loads(ergebnis.stdout)["documents"][0]["queued"] is False
    assert ergebnis.stderr.count("kein Daemon") == 1
    assert _jobs(db_conn) == 1


def test_ocr_warnt_nicht_wenn_ein_daemon_laeuft(
    cli: CliRunner, db_conn: psycopg.Connection[Any], tmp_path: Path
) -> None:
    """Die `cli`-Vorrichtung legt ein frisches Lebenszeichen an."""
    datei, _ = _datei(tmp_path, "a.png")
    ergebnis = cli.invoke(app, ["ocr", str(datei)])
    # Erst dass der Aufruf gelungen ist und etwas eingereiht hat, macht das
    # Ausbleiben der Warnung zu einer Aussage.
    assert ergebnis.exit_code == 0, ergebnis.output
    assert _jobs(db_conn) == 1
    assert "Daemon" not in ergebnis.stderr


def test_ocr_nennt_nach_einem_betriebsfehler_mitten_im_lauf_die_bis_dahin_eingereihten(
    cli: CliRunner,
    db_conn: psycopg.Connection[Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bricht ein Lauf ueber einen Ordner mittendrin ab, sind die ersten Dokumente
    schon eingereiht. Die Zuordnung von Pfad zu Hash ist der Zweck der Ausgabe und
    geht deshalb nicht mit dem Abbruch verloren."""
    dateien = [_datei(tmp_path, name) for name in ("a.png", "b.png", "c.png", "d.png")]
    echtes = client_modul.OCRClient.ocr_start
    aufrufe = {"n": 0}

    def dritter_scheitert(self: Any, data: Any) -> Any:
        aufrufe["n"] += 1
        if aufrufe["n"] == 3:
            raise DatabaseError("Verbindung weg")
        return echtes(self, data)

    monkeypatch.setattr(client_modul.OCRClient, "ocr_start", dritter_scheitert)

    ergebnis = cli.invoke(app, ["ocr", str(tmp_path), "--json"])

    assert ergebnis.exit_code == 1
    assert "Verbindung weg" in ergebnis.stderr
    angaben = json.loads(ergebnis.stdout)
    assert [d["sha256"] for d in angaben["documents"]] == [h for _, h in dateien[:2]]
    assert _jobs(db_conn) == 2


def test_ein_pfad_als_ziel_wird_gehasht_ohne_die_datei_ganz_zu_lesen(
    cli: CliRunner,
    db_conn: psycopg.Connection[Any],
    _test_datenbank: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`show` auf den Pfad einer grossen Datei hasht sie in Stuecken: Sie wird
    nicht ganz in den Speicher geladen, nur um einen Hash zu bilden."""
    huelle = _anlegen(db_conn, tmp_path, "gross.png", _test_datenbank)

    echtes_lesen = Path.read_bytes

    def nie(self: Path) -> bytes:
        if self.name == "gross.png":
            raise AssertionError("die Datei wurde ganz gelesen")
        return echtes_lesen(self)

    monkeypatch.setattr(Path, "read_bytes", nie)
    ergebnis = cli.invoke(app, ["show", str(tmp_path / "gross.png"), "--json"])

    assert ergebnis.exit_code == 0, ergebnis.output
    assert json.loads(ergebnis.stdout)["sha256"] == huelle.sha256


def test_ocr_hat_weder_strict_noch_start(cli: CliRunner, tmp_path: Path) -> None:
    """Abgebrochen wird ohne Daemon nichts, und eine Zeile ohne Job gibt es nicht
    mehr: Die Schalter `--strict` und `--no-start` entfallen."""
    datei, _ = _datei(tmp_path, "a.png")
    for schalter in ("--strict", "--no-strict", "--start", "--no-start"):
        assert cli.invoke(app, ["ocr", str(datei), schalter]).exit_code == 2, schalter

    # `preflight` behaelt seine eigene `--strict`-Option: Sie betrifft
    # Konfigurations-Abweichungen und nicht den Daemon.
    assert "strict" not in inspect.signature(cli_modul.ocr).parameters
    assert "strict" in inspect.signature(cli_modul.preflight).parameters


def test_der_stderr_handler_wird_einmal_angemeldet_und_haelt_keinen_strom(
    cli: CliRunner, db_conn: psycopg.Connection[Any], tmp_path: Path
) -> None:
    """Die Warnungen des Pakets gehen ueber EINEN Handler auf stderr, der den Strom
    erst beim Schreiben nimmt. Ein Handler je Aufruf doppelte jede Warnung in einem
    Prozess, der die Kommandozeile mehrfach aufruft, und ein Handler, der den Strom
    beim Anlegen festhielte, schriebe nach dem ersten Aufruf ins Leere."""
    logger = logging.getLogger("mineru_ocr")
    db_conn.execute("DELETE FROM procrastinate_workers")

    for name in ("a.png", "b.png"):
        datei, _ = _datei(tmp_path, name)
        ergebnis = cli.invoke(app, ["ocr", str(datei)])
        assert ergebnis.stderr.count("kein Daemon") == 1, name

    eigene = [h for h in logger.handlers if isinstance(h, cli_modul._FehlerAusgabe)]
    assert len(eigene) == 1
    assert not isinstance(eigene[0], logging.StreamHandler)


def test_ziel_ohne_hash_und_ohne_datei_ist_unbekannt(
    cli: CliRunner, tmp_path: Path
) -> None:
    """Ein Pfad, den es nicht gibt, ist kein Hash und keine Datei, die sich hashen
    liesse: Betriebsfehler mit dieser Auskunft statt eines Tracebacks."""
    for kommando in ("show", "wait", "markdown", "retry"):
        ergebnis = cli.invoke(app, [kommando, str(tmp_path / "gibt-es-nicht.png")])
        assert ergebnis.exit_code == 1, kommando
        assert "weder ein sha256 noch" in ergebnis.stderr, kommando


def test_ziel_als_nicht_lesbare_datei_ist_ein_betriebsfehler_ohne_traceback(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Eine Datei, die da ist, aber nicht lesbar (kein Leserecht), endet wie eine,
    die es nicht gibt: Exit-Code 1 mit Auskunft statt eines `PermissionError`."""
    datei, _ = _datei(tmp_path, "gesperrt.png")
    echtes_oeffnen = Path.open

    def gesperrt(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self.name == "gesperrt.png":
            raise PermissionError(13, "Permission denied", str(self))
        return echtes_oeffnen(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", gesperrt)
    for kommando in ("show", "wait", "markdown", "retry"):
        ergebnis = cli.invoke(app, [kommando, str(datei)])
        assert ergebnis.exit_code == 1, kommando
        assert "nicht lesbar" in ergebnis.stderr, kommando
        assert ergebnis.exception is None or isinstance(
            ergebnis.exception, SystemExit
        ), kommando


def test_hash_oder_pfad_wird_festgelegt_unterschieden() -> None:
    """Vierundsechzig Zeichen aus `[0-9a-f]` sind ein Hash, alles andere ist ein
    Pfad. Die Regel steht als Muster an EINER Stelle der Kommandozeile."""
    assert cli_modul.HASH_FORM.match("a" * 64)
    assert not cli_modul.HASH_FORM.match("a" * 63)
    assert not cli_modul.HASH_FORM.match("A" * 64)  # Grossbuchstaben sind kein sha256
    assert not cli_modul.HASH_FORM.match("/tmp/x.pdf")


# --- Das Kommando, das Geld ausgibt --------------------------------------


async def test_retry_fragt_vor_dem_geldausgeben(
    cli: CliRunner,
    db_conn: psycopg.Connection[Any],
    async_conn: psycopg.AsyncConnection[Any],
    _test_datenbank: str,
    tmp_path: Path,
) -> None:
    """`retry` sagt, um wie viele Dokumente es geht, und fragt nach.

    Jedes wieder vorgelegte Dokument wird VOLLSTAENDIG neu verarbeitet und neu
    bezahlt. Ein Kommando, das das ohne Rueckfrage tut, gibt fremdes Geld aus.

    Und die Gegenprobe im selben Test: Wird die Frage verneint, passiert NICHTS
    — kein halber Lauf, keine Teilmenge.
    """
    datei, sha = _datei(tmp_path, "teuer.png")
    cli.invoke(app, ["ocr", str(datei)])
    db_conn.execute("UPDATE procrastinate_jobs SET status = 'failed'")
    await write_terminal_async(
        async_conn, sha256=sha, status=DocumentStatus.FAILED, raw={}
    )

    verneint = cli.invoke(app, ["retry", sha], input="n\n")
    assert verneint.exit_code == 0
    assert "KOSTET" in verneint.output or "kostet Geld" in verneint.output
    # Nichts passiert: Der Job steht unveraendert auf `failed`.
    zustand = db_conn.execute(
        "SELECT status FROM procrastinate_jobs WHERE queueing_lock = %s", (sha,)
    ).fetchone()
    assert zustand is not None and zustand[0] == "failed"

    bejaht = cli.invoke(app, ["retry", sha], input="j\ny\n")
    assert bejaht.exit_code == 0
    zustand = db_conn.execute(
        "SELECT status FROM procrastinate_jobs WHERE queueing_lock = %s", (sha,)
    ).fetchone()
    assert zustand is not None and zustand[0] == "todo"


async def test_retry_mit_yes_fragt_nicht(
    cli: CliRunner,
    db_conn: psycopg.Connection[Any],
    async_conn: psycopg.AsyncConnection[Any],
    _test_datenbank: str,
    tmp_path: Path,
) -> None:
    """`--yes` ist der ausdrueckliche Verzicht auf die Rueckfrage — fuer Skripte.

    Ausdruecklich und nicht als Voreinstellung: Wer ihn setzt, hat die Frage
    schon beantwortet.
    """
    datei, sha = _datei(tmp_path, "skript.png")
    cli.invoke(app, ["ocr", str(datei)])
    db_conn.execute("UPDATE procrastinate_jobs SET status = 'failed'")
    await write_terminal_async(
        async_conn, sha256=sha, status=DocumentStatus.FAILED, raw={}
    )

    ergebnis = cli.invoke(app, ["retry", sha, "--yes"])
    assert ergebnis.exit_code == 0
    assert "1 von 1" in ergebnis.output


def test_retry_ohne_gescheiterte_tut_nichts(cli: CliRunner) -> None:
    """Nichts zu wiederholen ist kein Fehler — und kostet nichts."""
    ergebnis = cli.invoke(app, ["retry", "--failed", "--yes"])
    assert ergebnis.exit_code == 0
    assert "Nichts zu wiederholen" in ergebnis.output


async def test_markdown_schreibt_in_eine_datei(
    cli: CliRunner,
    db_conn: psycopg.Connection[Any],
    async_conn: psycopg.AsyncConnection[Any],
    _test_datenbank: str,
    tmp_path: Path,
) -> None:
    """Mit `--out` geht der Text in die Datei und die Meldung auf die
    FEHLERausgabe.

    Der Unterschied ist keine Feinheit: Wer `mineru-ocr markdown … > datei.md`
    schreibt, will den Text in der Datei haben und die Meldung nicht.
    """
    huelle = _anlegen(db_conn, tmp_path, "raus.png", _test_datenbank)
    await write_terminal_async(
        async_conn,
        sha256=huelle.sha256,
        status=DocumentStatus.DONE,
        raw={"markdown": "Der ganze Text", "content_list": [], "images": {}},
    )

    ziel = tmp_path / "ergebnis.md"
    ergebnis = cli.invoke(app, ["markdown", huelle.sha256, "--out", str(ziel)])
    assert ergebnis.exit_code == 0
    assert ziel.read_text(encoding="utf-8") == "Der ganze Text"
    assert "Der ganze Text" not in ergebnis.stdout


async def test_markdown_meldet_eine_beschaedigte_zeile_statt_sie_auszugeben(
    cli: CliRunner,
    db_conn: psycopg.Connection[Any],
    async_conn: psycopg.AsyncConnection[Any],
    _test_datenbank: str,
    tmp_path: Path,
) -> None:
    """Eine `done`-Zeile mit beschaedigtem `raw` endet mit 1 und schreibt NICHTS.

    DER GEFAEHRLICHE FALL, und deshalb steht er hier: Der Wachposten des
    Kommandos ist `huelle.success`, und der liest die STATUS-Spalte. Ueber `raw`
    sagt er gar nichts. Eine Zeile, die `done` ist und deren `markdown` als
    JSON-`null` dasteht, kommt also bis zur Darstellung durch.

    GEMESSEN, bevor es diesen Test gab (10.08.2026, durch genau diesen Weg):
    Exit-Code 0, und in der Datei stand „Aktenzeichen 123/456/7890" — der eine
    Randblock. Der Fliesstext des Bescheids, der im echten Betrieb aus
    `markdown` kaeme, fehlte vollstaendig, und nichts sagte es. Die Datei sah
    aus wie ein kurzes Dokument.

    Der Fliesstext steht hier als `content_list`-Eintrag vom Typ `text` und
    NICHT in `markdown`, und das ist der Kern des Tests: Ein Typ, den
    `rendered_markdown` nicht anhaengt, weil er normalerweise schon im Markdown
    steht. Faellt das Markdown weg, faellt er lautlos mit weg.
    """
    huelle = _anlegen(db_conn, tmp_path, "beschaedigt.png", _test_datenbank)
    await write_terminal_async(
        async_conn,
        sha256=huelle.sha256,
        status=DocumentStatus.DONE,
        raw={
            "markdown": None,
            "content_list": [
                {"type": "header", "text": "Aktenzeichen 123/456/7890", "page_idx": 0},
                {"type": "text", "text": "Der ganze Fliesstext.", "page_idx": 0},
            ],
            "images": {},
        },
    )

    ziel = tmp_path / "beschaedigt.md"
    ergebnis = cli.invoke(app, ["markdown", huelle.sha256, "--out", str(ziel)])
    assert ergebnis.exit_code == 1
    assert not ziel.exists()
    assert "IncompleteResultError" in ergebnis.output
    assert "markdown" in ergebnis.output


async def test_markdown_meldet_auch_ein_FEHLENDES_pflichtfeld(
    cli: CliRunner,
    db_conn: psycopg.Connection[Any],
    async_conn: psycopg.AsyncConnection[Any],
    _test_datenbank: str,
    tmp_path: Path,
) -> None:
    """Nicht nur der falsche Typ, auch das fehlende Feld endet mit 1.

    Gemessen und nicht aus dem Nachbartest gefolgert: Der Wurf kommt zwar aus
    derselben Funktion, aber ob die Kommandozeile ihn wirklich faengt und ob die
    Datei wirklich ungeschrieben bleibt, ist eine Aussage ueber diesen Weg und
    nicht ueber `rendered_markdown`.

    Der Fall ist real und nicht konstruiert: Er entstand daraus, dass die erste
    Fassung der Pruefung ein einzeln fehlendes Feld DULDETE — mit einer
    Begruendung („aeltere Fassung"), die nie geprueft war und sich als falsch
    erwiesen hat. Ohne `content_list` fehlt das Aktenzeichen, und die Ausgabe
    saehe vollstaendig aus.
    """
    huelle = _anlegen(db_conn, tmp_path, "ohne_liste.png", _test_datenbank)
    await write_terminal_async(
        async_conn,
        sha256=huelle.sha256,
        status=DocumentStatus.DONE,
        raw={"markdown": "Fliesstext ohne die Randbloecke.", "images": {}},
    )

    ziel = tmp_path / "ohne_liste.md"
    ergebnis = cli.invoke(app, ["markdown", huelle.sha256, "--out", str(ziel)])
    assert ergebnis.exit_code == 1
    assert not ziel.exists()
    assert "content_list" in ergebnis.output
