"""Der Alltagsweg: Pfad hinein, Volltext heraus (Mineroo#55)."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb
from typer.testing import CliRunner

from mineru_ocr.cli import app

pytestmark = pytest.mark.db
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"


def datei(tmp_path: Path, name: str = "dokument.png") -> Path:
    pfad = tmp_path / name
    pfad.parent.mkdir(parents=True, exist_ok=True)
    pfad.write_bytes(PNG + name.encode())
    return pfad


def speichern(
    conn: psycopg.Connection[Any], pfad: Path, text: str = "# Volltext\n\nKopf und Fuss.",
    status: str = "done", error: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO documents (sha256, status, raw, endpoint_id, error) "
        "VALUES (%s, %s, %s, 'testendpoint', %s)",
        (hashlib.sha256(pfad.read_bytes()).hexdigest(), status,
         Jsonb({"markdown": "gefiltert", "markdown_full": text}), error),
    )


def test_treffer_gibt_ohne_daemon_den_volltext_aus(
    db_conn: psycopg.Connection[Any], tmp_path: Path,
) -> None:
    pfad = datei(tmp_path)
    speichern(db_conn, pfad)
    lauf = CliRunner().invoke(app, [str(pfad)])
    assert lauf.exit_code == 0, lauf.output
    assert lauf.stdout == "# Volltext\n\nKopf und Fuss.\n"
    assert lauf.stderr == ""
    assert db_conn.execute("SELECT count(*) FROM procrastinate_jobs").fetchone() == (0,)


def test_ohne_daemon_wird_nichts_eingereiht(
    db_conn: psycopg.Connection[Any], tmp_path: Path,
) -> None:
    pfad = datei(tmp_path)
    lauf = CliRunner().invoke(app, [str(pfad)])
    assert lauf.exit_code == 5, lauf.output
    assert lauf.stdout == ""
    assert "keine Daemons" in lauf.stderr
    assert "mineru-ocr daemon start X" in lauf.stderr
    assert db_conn.execute("SELECT count(*) FROM inputs").fetchone() == (0,)
    assert db_conn.execute("SELECT count(*) FROM procrastinate_jobs").fetchone() == (0,)


def test_neue_datei_wird_eingereiht_abgewartet_und_ausgegeben(
    db_conn: psycopg.Connection[Any], tmp_path: Path, _test_datenbank: str,
) -> None:
    """Ein zweiter Prozess schreibt erst nach dem Einreihen das Ergebnis."""
    import asyncio
    import time
    from concurrent.futures import ThreadPoolExecutor

    from mineru_ocr import db
    from mineru_ocr.db import DocumentStatus

    pfad = datei(tmp_path)
    sha = hashlib.sha256(pfad.read_bytes()).hexdigest()
    db_conn.execute("INSERT INTO procrastinate_workers DEFAULT VALUES")

    def beantworten() -> None:
        with db.connect(_test_datenbank) as conn:
            frist = time.monotonic() + 5
            while time.monotonic() < frist:
                eingabe = conn.execute("SELECT content, job_id FROM inputs WHERE sha256 = %s", (sha,)).fetchone()
                if eingabe is not None:
                    assert bytes(eingabe[0]) == pfad.read_bytes()
                    assert eingabe[1] is not None
                    break
                time.sleep(0.01)
            else:
                raise AssertionError("Keine Eingabe eingereiht")

        async def schreiben() -> None:
            conn = await db.connect_async(_test_datenbank)
            try:
                await db.write_terminal_async(
                    conn, sha256=sha, status=DocumentStatus.DONE,
                    raw={"markdown_full": "Frisch gelesen."}, endpoint_id="testendpoint",
                )
            finally:
                await conn.close()
        asyncio.run(schreiben())

    with ThreadPoolExecutor(max_workers=1) as pool:
        arbeit = pool.submit(beantworten)
        lauf = CliRunner().invoke(app, [str(pfad)])
        assert lauf.exit_code == 0, lauf.output
        arbeit.result(timeout=10)
    assert lauf.stdout == "Frisch gelesen.\n"


def test_temp_schreibt_utf8_und_gibt_nur_den_pfad_aus(
    db_conn: psycopg.Connection[Any], tmp_path: Path,
) -> None:
    import tempfile

    pfad = datei(tmp_path)
    speichern(db_conn, pfad, "Überschrift\n\nFußnote.")
    lauf = CliRunner().invoke(app, ["-t", str(pfad)])
    assert lauf.exit_code == 0, lauf.output
    ausgabe = Path(lauf.stdout.strip())
    try:
        assert ausgabe.is_relative_to(Path(tempfile.gettempdir()))
        assert ausgabe.read_text(encoding="utf-8") == "Überschrift\n\nFußnote."
    finally:
        ausgabe.unlink(missing_ok=True)


def test_out_schreibt_an_den_gewaehlten_pfad(
    db_conn: psycopg.Connection[Any], tmp_path: Path,
) -> None:
    pfad = datei(tmp_path)
    speichern(db_conn, pfad)
    ziel = tmp_path / "text.md"
    lauf = CliRunner().invoke(app, [str(pfad), "-o", str(ziel)])
    assert lauf.exit_code == 0, lauf.output
    assert ziel.read_text(encoding="utf-8") == "# Volltext\n\nKopf und Fuss."
    assert lauf.stdout.strip() == str(ziel)


@pytest.mark.parametrize("status,code,grund", [
    ("empty", 0, "kein Text"),
    ("failed", 3, "Testfehler beim Lesen"),
    ("input_error", 3, "Testfehler beim Lesen"),
])
def test_endzustaende_ohne_text_werden_mit_grund_gemeldet(
    db_conn: psycopg.Connection[Any], tmp_path: Path, status: str, code: int, grund: str,
) -> None:
    pfad = datei(tmp_path)
    speichern(db_conn, pfad, status=status, error=None if status == "empty" else grund)
    lauf = CliRunner().invoke(app, [str(pfad)])
    assert lauf.exit_code == code, lauf.output
    assert lauf.stdout == ""
    assert str(pfad) in lauf.stderr
    assert status in lauf.stderr
    assert grund in lauf.stderr


def test_mehrere_dateien_sind_zugeordnet_auch_bei_gleichen_bytes(
    db_conn: psycopg.Connection[Any], tmp_path: Path,
) -> None:
    a = datei(tmp_path, "eins.png")
    b = datei(tmp_path, "zwei.png")
    b.write_bytes(a.read_bytes())
    speichern(db_conn, a, "Gemeinsamer Text")
    lauf = CliRunner().invoke(app, [str(a), str(b)])
    assert lauf.exit_code == 0, lauf.output
    assert str(a) in lauf.stdout
    assert str(b) in lauf.stdout
    assert lauf.stdout.count("Gemeinsamer Text") == 2


def test_ordner_liefert_unterordner_und_ueberspringt_fremde_dateien(
    db_conn: psycopg.Connection[Any], tmp_path: Path,
) -> None:
    a = datei(tmp_path, "eingabe/eins.png")
    b = datei(tmp_path, "eingabe/unter/zwei.png")
    speichern(db_conn, a, "Erster Text")
    speichern(db_conn, b, "Zweiter Text")
    (a.parent / "unbekannt.txt").write_text("synthetische Notiz")
    lauf = CliRunner().invoke(app, [str(a.parent)])
    assert lauf.exit_code == 0, lauf.output
    assert str(a) in lauf.stdout and "Erster Text" in lauf.stdout
    assert str(b) in lauf.stdout and "Zweiter Text" in lauf.stdout
    assert "unbekannt.txt" in lauf.stderr


@pytest.mark.parametrize("option", ["-t", "-o"])
def test_dateiausgabe_eines_stapels_enthaelt_alle_texte(
    db_conn: psycopg.Connection[Any], tmp_path: Path, option: str,
) -> None:
    a = datei(tmp_path, "eins.png")
    b = datei(tmp_path, "zwei.png")
    speichern(db_conn, a, "Erster Text")
    speichern(db_conn, b, "Zweiter Text")
    args = [str(a), str(b), option]
    if option == "-o":
        args.append(str(tmp_path / "stapel.md"))
    lauf = CliRunner().invoke(app, args)
    assert lauf.exit_code == 0, lauf.output
    assert len(lauf.stdout.splitlines()) == 1
    ausgabe = Path(lauf.stdout.strip())
    try:
        text = ausgabe.read_text(encoding="utf-8")
        assert str(a) in text and "Erster Text" in text
        assert str(b) in text and "Zweiter Text" in text
    finally:
        ausgabe.unlink(missing_ok=True)


@pytest.mark.parametrize("hindernis,code", [("neu", 5), ("fehlt", 1), ("fremd", 1)])
def test_ein_problem_haelt_treffer_nicht_auf(
    db_conn: psycopg.Connection[Any], tmp_path: Path, hindernis: str, code: int,
) -> None:
    a = datei(tmp_path, "problem.png")
    b = datei(tmp_path, "treffer.png")
    speichern(db_conn, b, "Trotzdem vollstaendig")
    if hindernis == "fehlt":
        a.unlink()
    elif hindernis == "fremd":
        a.write_text("synthetischer Fremdtyp")
    lauf = CliRunner().invoke(app, [str(a), str(b)])
    assert lauf.exit_code == code, lauf.output
    assert "Trotzdem vollstaendig" in lauf.stdout
    assert str(a) in lauf.stderr
    assert db_conn.execute("SELECT count(*) FROM procrastinate_jobs").fetchone() == (0,)


@pytest.mark.parametrize("extra", [
    ["-t", "-o", "ausgabe.md"],
    ["--timeout", "-1"], ["--timeout", "nan"], ["--timeout", "inf"],
    ["--tier", "unbekannt"], ["--ocr-mode", "unbekannt"],
])
def test_ungueltige_optionen_reihen_nichts_ein(
    db_conn: psycopg.Connection[Any], tmp_path: Path, extra: list[str],
) -> None:
    pfad = datei(tmp_path)
    lauf = CliRunner().invoke(app, [str(pfad), *extra])
    assert lauf.exit_code == 2, lauf.output
    assert db_conn.execute("SELECT count(*) FROM inputs").fetchone() == (0,)


def test_ausgabe_darf_die_eingabe_nicht_ueberschreiben(
    db_conn: psycopg.Connection[Any], tmp_path: Path,
) -> None:
    pfad = datei(tmp_path)
    vorher = pfad.read_bytes()
    speichern(db_conn, pfad)
    lauf = CliRunner().invoke(app, [str(pfad), "-o", str(pfad)])
    assert lauf.exit_code == 2, lauf.output
    assert pfad.read_bytes() == vorher


def test_abgelaufene_wartefrist_laesst_den_job_stehen(
    db_conn: psycopg.Connection[Any], tmp_path: Path,
) -> None:
    pfad = datei(tmp_path)
    db_conn.execute("INSERT INTO procrastinate_workers DEFAULT VALUES")
    lauf = CliRunner().invoke(app, [str(pfad), "--timeout", "0"])
    assert lauf.exit_code == 4, lauf.output
    assert lauf.stdout == ""
    assert str(pfad) in lauf.stderr
    assert db_conn.execute("SELECT count(*) FROM inputs").fetchone() == (1,)
    assert db_conn.execute("SELECT status FROM procrastinate_jobs").fetchone() == ("todo",)


def test_alter_herzschlag_zaehlt_nicht_als_daemon(
    db_conn: psycopg.Connection[Any], tmp_path: Path, abgestuerzter_worker: Any,
) -> None:
    abgestuerzter_worker()
    pfad = datei(tmp_path)
    lauf = CliRunner().invoke(app, [str(pfad)])
    assert lauf.exit_code == 5, lauf.output
    assert db_conn.execute("SELECT count(*) FROM inputs").fetchone() == (0,)


def test_offenes_dokument_ohne_daemon_wird_nicht_abgewartet(
    db_conn: psycopg.Connection[Any], tmp_path: Path,
) -> None:
    from mineru_ocr.client import OCRClient

    pfad = datei(tmp_path)
    with OCRClient(preflight=False) as client:
        client.ocr_start(pfad.read_bytes())
    lauf = CliRunner().invoke(app, [str(pfad), "--timeout", "0"])
    assert lauf.exit_code == 5, lauf.output
    assert db_conn.execute("SELECT count(*) FROM procrastinate_jobs").fetchone() == (1,)


def test_leseeinstellungen_gelten_fuer_neue_dokumente(
    db_conn: psycopg.Connection[Any], tmp_path: Path,
) -> None:
    pfad = datei(tmp_path)
    db_conn.execute("INSERT INTO procrastinate_workers DEFAULT VALUES")
    lauf = CliRunner().invoke(app, [
        str(pfad), "--timeout", "0", "--tier", "flash",
        "--ocr-mode", "txt", "--no-image-analysis",
    ])
    assert lauf.exit_code == 4, lauf.output
    assert db_conn.execute("SELECT tier, ocr_mode, image_analysis FROM inputs").fetchone() == (
        "flash", "txt", False,
    )


def test_beschaedigter_treffer_gibt_keinen_halben_text_aus(
    db_conn: psycopg.Connection[Any], tmp_path: Path,
) -> None:
    pfad = datei(tmp_path)
    speichern(db_conn, pfad)
    db_conn.execute("UPDATE documents SET raw = %s", (Jsonb({"markdown": "Nur ein Teil"}),))
    lauf = CliRunner().invoke(app, [str(pfad)])
    assert lauf.exit_code == 1, lauf.output
    assert lauf.stdout == ""
    assert "IncompleteResultError" in lauf.stderr
    assert "Nur ein Teil" not in lauf.stderr


def test_betriebskommandos_und_lesehilfe_bleiben_erreichbar() -> None:
    runner = CliRunner()
    hilfe = runner.invoke(app, ["--help"])
    assert hilfe.exit_code == 0
    assert "PATH" in hilfe.stdout and "-t" in hilfe.stdout and "-o" in hilfe.stdout
    for name in ("setup", "daemon"):
        lauf = runner.invoke(app, [name, "--help"])
        assert lauf.exit_code == 0, lauf.output
    from typer.main import get_command
    command = get_command(app)
    import click
    assert isinstance(command, click.Group)
    assert set(command.commands) == {"setup", "daemon"}


def test_setup_braucht_weder_endpoint_noch_schluessel(
    db_conn: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MINERU_OCR_ENDPOINT_ID")
    lauf = CliRunner().invoke(app, ["setup"])
    assert lauf.exit_code == 0, lauf.output
    assert lauf.stdout == ""
    assert "mineru_ocr_test" in lauf.stderr


def test_daemon_verlangt_den_schluessel_vor_dem_start(
    db_conn: psycopg.Connection[Any],
) -> None:
    lauf = CliRunner().invoke(app, ["daemon"])
    assert lauf.exit_code == 1, lauf.output
    assert "Kein API-Schluessel" in lauf.stderr
    assert "Traceback" not in lauf.stderr


def test_strg_c_beendet_nur_das_warten_mit_130(
    db_conn: psycopg.Connection[Any], tmp_path: Path, _test_datenbank: str,
) -> None:
    import os
    import signal
    import subprocess
    import sys
    import time

    pfad = datei(tmp_path)
    db_conn.execute("INSERT INTO procrastinate_workers DEFAULT VALUES")
    config = tmp_path / "config.toml"
    config.write_text("")
    umgebung = {k: v for k, v in os.environ.items() if not k.startswith("MINERU_OCR_")}
    umgebung.update(MINERU_OCR_DB_DSN=_test_datenbank, MINERU_OCR_CONFIG=str(config))
    # Keine private RunPod-Konfiguration im Kindprozess; kein Zugriff aufs Netz.
    skript = (
        "from pathlib import Path; import sys; import signal; import mineru_ocr.config as c; "
        "signal.signal(signal.SIGINT, signal.default_int_handler); "
        "c.RUNPOD_CONFIG_PATH = Path(sys.argv[1]); "
        "from mineru_ocr.cli import main; sys.argv = ['mineru-ocr', sys.argv[2]]; main()"
    )
    prozess = subprocess.Popen(
        [sys.executable, "-c", skript, str(tmp_path / "kein-runpod.toml"), str(pfad)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=umgebung,
    )
    try:
        frist = time.monotonic() + 5
        while time.monotonic() < frist:
            if db_conn.execute("SELECT count(*) FROM inputs").fetchone() == (1,):
                break
            if prozess.poll() is not None:
                raise AssertionError(prozess.communicate(timeout=2))
            time.sleep(0.01)
        else:
            raise AssertionError("CLI hat nicht eingereiht")
        prozess.send_signal(signal.SIGINT)
        stdout, stderr = prozess.communicate(timeout=5)
        assert prozess.returncode == 130, stderr
        assert stdout == ""
        assert db_conn.execute("SELECT status FROM procrastinate_jobs").fetchone() == ("todo",)
    finally:
        if prozess.poll() is None:
            prozess.kill()
            prozess.communicate(timeout=5)


@pytest.mark.parametrize("option", ["-o", "-t"])
@pytest.mark.parametrize("anzahl", [1, 2])
def test_leere_ergebnisse_schreiben_eine_leere_datei(
    db_conn: psycopg.Connection[Any], tmp_path: Path, option: str, anzahl: int,
) -> None:
    pfade = [datei(tmp_path, f"leer-{nr}.png") for nr in range(anzahl)]
    for pfad in pfade:
        speichern(db_conn, pfad, "", status="empty")
    ziel = tmp_path / "text.md"
    ziel.write_text("Unzugehoeriger alter Text")
    args = [*(str(p) for p in pfade), option]
    if option == "-o":
        args.append(str(ziel))
    lauf = CliRunner().invoke(app, args)
    assert lauf.exit_code == 0, lauf.output
    assert len(lauf.stdout.splitlines()) == 1
    ausgabe = Path(lauf.stdout.strip())
    try:
        assert ausgabe.is_file()
        assert ausgabe.read_bytes() == b""
        assert lauf.stderr.count("empty") == anzahl
        if option == "-o":
            assert ausgabe == ziel
    finally:
        ausgabe.unlink(missing_ok=True)


@pytest.mark.parametrize("status,code", [("failed", 3), ("input_error", 3), ("neu", 5)])
@pytest.mark.parametrize("vorhanden", [False, True])
def test_reine_fehlschlaege_lassen_das_ausgabeziel_unberuehrt(
    db_conn: psycopg.Connection[Any], tmp_path: Path, status: str, code: int, vorhanden: bool,
) -> None:
    pfad = datei(tmp_path)
    if status != "neu":
        speichern(db_conn, pfad, status=status, error="synthetischer Fehler")
    ziel = tmp_path / "text.md"
    if vorhanden:
        ziel.write_text("Alter Text")
    lauf = CliRunner().invoke(app, [str(pfad), "-o", str(ziel)])
    assert lauf.exit_code == code, lauf.output
    assert lauf.stdout == ""
    assert ziel.exists() == vorhanden
    if vorhanden:
        assert ziel.read_text() == "Alter Text"
