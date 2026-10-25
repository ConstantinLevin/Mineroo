"""Die Kommandozeile — Exit-Codes und die Regel, dass sie nur übersetzt.

Die Exit-Codes sind im Vertrag ausdrücklich Zusage, weil in einer Shell darauf
verzweigt wird. Eine Zusage ohne Test ist eine Absichtserklärung.
"""

from __future__ import annotations

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
from mineru_ocr.cli import app
from mineru_ocr.config import Settings
from mineru_ocr.db import DocumentStatus, write_terminal_async
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
    # Ein lebendes Lebenszeichen, damit `ocr` nicht ohne Daemon warnt.
    db_conn.execute("INSERT INTO procrastinate_workers DEFAULT VALUES")
    return CliRunner()


def _anlegen(
    db_conn: psycopg.Connection[Any], tmp_path: Path, name: str, dsn: str
) -> OCRWrapper:
    pfad = tmp_path / name
    pfad.write_bytes(PNG_KOPF + name.encode())
    return OCRWrapper.from_path(
        pfad, db_conn, Settings.load(db_dsn=dsn, api_key=SCHLUESSEL)
    )


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
    # `ocr` heisst wie `OCRClient.ocr`/`ocr_path`, `retry` wie `retry_failed`.
    assert hasattr(OCRClient, "ocr")
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
    huelle = _anlegen(db_conn, tmp_path, "wartend.png", _test_datenbank)
    cli.invoke(app, ["ocr", str(tmp_path / "wartend.png")])

    ergebnis = cli.invoke(app, ["show", huelle.sha256, "--json"])
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


def test_ocr_reiht_auch_ohne_daemon_ein_und_warnt_auf_stderr(
    cli: CliRunner, db_conn: psycopg.Connection[Any], tmp_path: Path
) -> None:
    """Abschicken haengt nicht vom Daemon ab: Die Jobs liegen in der Datenbank,
    bis einer startet. Die Warnung steht auf stderr, damit stdout (und `--json`)
    sauber bleibt."""
    db_conn.execute("DELETE FROM procrastinate_workers")
    for name in ("a.png", "b.png"):
        (tmp_path / name).write_bytes(PNG_KOPF + name.encode())

    ergebnis = cli.invoke(app, ["ocr", str(tmp_path), "--json"])

    assert ergebnis.exit_code == 0, ergebnis.output
    assert json.loads(ergebnis.stdout)["queued"] == 2
    assert "mineru-ocr daemon" in ergebnis.stderr
    assert "kein Daemon" not in ergebnis.stdout
    # Einmal, nicht je Dokument.
    assert ergebnis.stderr.count("kein Daemon") == 1
    anzahl = db_conn.execute("SELECT count(*) FROM procrastinate_jobs").fetchone()
    assert anzahl is not None and anzahl[0] == 2


def test_ocr_warnt_nicht_bei_laufendem_daemon(
    cli: CliRunner, tmp_path: Path
) -> None:
    """Die `cli`-Vorrichtung legt ein frisches Lebenszeichen an."""
    datei = tmp_path / "a.png"
    datei.write_bytes(PNG_KOPF + b"a")

    ergebnis = cli.invoke(app, ["ocr", str(datei)])

    assert ergebnis.exit_code == 0, ergebnis.output
    assert "Daemon" not in ergebnis.stderr


def test_ocr_hat_keine_strict_option_mehr(cli: CliRunner, tmp_path: Path) -> None:
    """Der Abbruch ohne Daemon ist abgeloest, und die Option, die ihn schaltete,
    entfaellt mit ihm. `preflight` behaelt seine eigene `--strict`-Option: Sie
    betrifft Konfigurations-Abweichungen und nicht den Daemon."""
    datei = tmp_path / "a.png"
    datei.write_bytes(PNG_KOPF + b"a")

    for schalter in ("--strict", "--no-strict"):
        ergebnis = cli.invoke(app, ["ocr", str(datei), schalter])
        assert ergebnis.exit_code == 2, (schalter, ergebnis.output)

    assert "strict" not in inspect.signature(cli_modul.ocr).parameters
    assert "strict" in inspect.signature(cli_modul.preflight).parameters


def test_der_stderr_handler_haengt_nach_dem_aufruf_nicht_mehr_am_logger() -> None:
    """Der Handler haelt den Strom fest, auf den `sys.stderr` gerade zeigt. Bliebe
    er nach einem Fehler im Rumpf haengen, doppelte jede Warnung in einem Prozess,
    der die Kommandozeile mehrfach aufruft (und jeder Test taete es auch)."""
    logger = logging.getLogger("mineru_ocr")
    vorher = list(logger.handlers)

    with pytest.raises(RuntimeError):
        with cli_modul._warnungen_auf_stderr():
            assert len(logger.handlers) == len(vorher) + 1
            raise RuntimeError("Fehler im Rumpf")

    assert logger.handlers == vorher


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
    huelle = _anlegen(db_conn, tmp_path, "teuer.png", _test_datenbank)
    cli.invoke(app, ["ocr", str(tmp_path / "teuer.png")])
    db_conn.execute("UPDATE procrastinate_jobs SET status = 'failed'")
    await write_terminal_async(
        async_conn, sha256=huelle.sha256, status=DocumentStatus.FAILED, raw={}
    )

    verneint = cli.invoke(app, ["retry", huelle.sha256], input="n\n")
    assert verneint.exit_code == 0
    assert "KOSTET" in verneint.output or "kostet Geld" in verneint.output
    # Nichts passiert: Der Job steht unveraendert auf `failed`.
    zustand = db_conn.execute(
        "SELECT status FROM procrastinate_jobs WHERE queueing_lock = %s",
        (huelle.sha256,),
    ).fetchone()
    assert zustand is not None and zustand[0] == "failed"

    bejaht = cli.invoke(app, ["retry", huelle.sha256], input="j\ny\n")
    assert bejaht.exit_code == 0
    zustand = db_conn.execute(
        "SELECT status FROM procrastinate_jobs WHERE queueing_lock = %s",
        (huelle.sha256,),
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
    huelle = _anlegen(db_conn, tmp_path, "skript.png", _test_datenbank)
    cli.invoke(app, ["ocr", str(tmp_path / "skript.png")])
    db_conn.execute("UPDATE procrastinate_jobs SET status = 'failed'")
    await write_terminal_async(
        async_conn, sha256=huelle.sha256, status=DocumentStatus.FAILED, raw={}
    )

    ergebnis = cli.invoke(app, ["retry", huelle.sha256, "--yes"])
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
