"""Die Kommandozeile — Exit-Codes und die Regel, dass sie nur übersetzt.

Die Exit-Codes sind im Vertrag ausdrücklich Zusage, weil in einer Shell darauf
verzweigt wird. Eine Zusage ohne Test ist eine Absichtserklärung.
"""

from __future__ import annotations

import dataclasses
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
from mineru_ocr.config import Settings as _Settings
from mineru_ocr.config import Settings
from mineru_ocr.db import DocumentStatus, write_terminal_async
from mineru_ocr.errors import DatabaseError
from mineru_ocr.wrapper import OCRWrapper

pytestmark = pytest.mark.db

_ECHTES_LOAD = _Settings.load
"""`Settings.load`, bevor eine Vorrichtung es umbiegt."""

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
        # `OCRClient` reicht `api_key=None` ausdruecklich herein; `setdefault`
        # griffe dann nicht, und der Test laese still den echten Schluessel des
        # Entwicklers aus `~/.runpod/config.toml`.
        if angaben.get("api_key") is None:
            angaben["api_key"] = SCHLUESSEL
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
    """Eine Datei samt gescheitertem Ergebnis und Eingabe, ohne Job und ohne `ocr`.

    Fuer die Tests, die ein Ergebnis schreiben und dann ein Kommando ansehen. Die
    Zeile legt sonst erst der Job an; hier steht sie von Hand als `failed` da,
    wie der Job sie bei `failed` hinterlaesst, und die Tests schreiben ihr
    Ergebnis darueber (`failed` darf ueberschrieben werden). Die Datei liegt da,
    damit sich das Kommando auch mit ihrem Pfad aufrufen laesst.
    """
    pfad, sha256 = _datei(tmp_path, name)
    inhalt = pfad.read_bytes()
    with db_conn.transaction():
        db_conn.execute(
            "INSERT INTO documents (sha256, size_bytes, mime, status, endpoint_id) "
            "VALUES (%s, %s, 'image/png', 'failed', %s)",
            (
                sha256,
                len(inhalt),
                Settings.load(db_dsn=dsn, api_key=SCHLUESSEL).require_endpoint_id(),
            ),
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


def test_preflight_laeuft_auch_auf_einer_alten_tabelle(
    cli: CliRunner, db_conn: psycopg.Connection[Any], alte_documents_form: None
) -> None:
    """`preflight` veraendert nichts und darf jederzeit laufen — auch vor
    `setup` oder auf einer Tabelle von vor dem Update: Er liest weder
    `documents` noch `inputs`, also haelt ihn deren Form nicht auf."""
    ergebnis = cli.invoke(app, ["preflight"])
    assert ergebnis.exit_code == 0, ergebnis.output


def test_preflight_laeuft_auch_vor_setup(
    cli: CliRunner,
    db_conn: psycopg.Connection[Any],
    _test_datenbank: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Vor `setup` fehlen `documents`, `inputs` und Procrastinates Tabellen.
    `preflight` laeuft trotzdem durch: Ob ein Daemon lebt, ist dann „nicht
    pruefbar", kein Abbruch. Das leere Schema stellt eine Datenbank ohne
    `setup` dar."""
    db_conn.execute("CREATE SCHEMA IF NOT EXISTS leer_fuer_preflight")
    leer = _test_datenbank + " options='-csearch_path=leer_fuer_preflight'"
    bisher = Settings.load  # schon von `cli` auf die Test-Datenbank gebogen

    def auf_leeres_schema(**angaben: Any) -> Settings:
        return dataclasses.replace(bisher(**angaben), db_dsn=leer)

    monkeypatch.setattr(Settings, "load", staticmethod(auf_leeres_schema))
    try:
        ergebnis = cli.invoke(app, ["preflight", "--json"])
    finally:
        db_conn.execute("DROP SCHEMA leer_fuer_preflight")
    assert ergebnis.exit_code == 0, ergebnis.output
    (daemon_punkt,) = [
        e for e in json.loads(ergebnis.stdout)["checks"] if e["punkt"] == "daemon"
    ]
    assert daemon_punkt["bewertung"] == "nicht_pruefbar"


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
            raw={"markdown": "Text", "middle_json": [], "images": {}},
            endpoint_id="testendpoint",
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
            endpoint_id="testendpoint",
        )
        assert cli.invoke(app, ["wait", huelle.sha256]).exit_code == 3, zustand
        assert cli.invoke(app, ["markdown", huelle.sha256]).exit_code == 3, zustand


async def _endgueltig(
    async_conn: psycopg.AsyncConnection[Any],
    db_conn: psycopg.Connection[Any],
    tmp_path: Path,
    dsn: str,
    name: str,
    zustand: DocumentStatus,
) -> OCRWrapper:
    """Eine Datei mit Endergebnis `zustand`, fuer die Tests von `wait`."""
    huelle = _anlegen(db_conn, tmp_path, name, dsn)
    await write_terminal_async(
        async_conn,
        sha256=huelle.sha256,
        status=zustand,
        raw={"markdown": "Text"},
        endpoint_id="testendpoint",
    )
    return huelle


def _offen(db_conn: psycopg.Connection[Any], tmp_path: Path, name: str) -> str:
    """Eine Datei, deren Eingabe steht und die kein Ergebnis hat: offen."""
    pfad, sha256 = _datei(tmp_path, name)
    db.insert_input(db_conn, sha256=sha256, content=pfad.read_bytes())
    return sha256


async def test_wait_wartet_auf_viele_und_gibt_mit_json_je_dokument_eine_zeile(
    cli: CliRunner,
    db_conn: psycopg.Connection[Any],
    async_conn: psycopg.AsyncConnection[Any],
    _test_datenbank: str,
    tmp_path: Path,
) -> None:
    """`wait ZIEL…` nimmt Hashes und Pfade. Mit `--json` steht je Dokument eine
    JSON-Zeile auf stdout, ohne `raw`; alle `done` oder `empty` heisst 0."""
    fertig = await _endgueltig(
        async_conn, db_conn, tmp_path, _test_datenbank, "fertig.png", DocumentStatus.DONE
    )
    leer = await _endgueltig(
        async_conn, db_conn, tmp_path, _test_datenbank, "leer.png", DocumentStatus.EMPTY
    )

    ergebnis = cli.invoke(
        app, ["wait", fertig.sha256, str(tmp_path / "leer.png"), "--json"]
    )

    assert ergebnis.exit_code == 0, ergebnis.output
    zeilen = [json.loads(zeile) for zeile in ergebnis.stdout.splitlines()]
    assert sorted((z["sha256"], z["status"]) for z in zeilen) == sorted(
        [(fertig.sha256, "done"), (leer.sha256, "empty")]
    )
    assert all("raw" not in z for z in zeilen)


async def test_wait_ohne_json_nennt_je_dokument_hash_und_zustand(
    cli: CliRunner,
    db_conn: psycopg.Connection[Any],
    async_conn: psycopg.AsyncConnection[Any],
    _test_datenbank: str,
    tmp_path: Path,
) -> None:
    fertig = await _endgueltig(
        async_conn, db_conn, tmp_path, _test_datenbank, "fertig.png", DocumentStatus.DONE
    )
    ergebnis = cli.invoke(app, ["wait", fertig.sha256])
    assert ergebnis.exit_code == 0, ergebnis.output
    assert ergebnis.stdout == f"{fertig.sha256}: done\n"


async def test_wait_nennt_in_jeder_zeile_den_pfad_ihres_ziels(
    cli: CliRunner,
    db_conn: psycopg.Connection[Any],
    async_conn: psycopg.AsyncConnection[Any],
    _test_datenbank: str,
    tmp_path: Path,
) -> None:
    """Die Zeilen kommen in der Reihenfolge, in der die Dokumente fertig werden.
    War das Ziel ein Pfad, nennt die Zeile ihn deshalb mit — sonst liesse sich
    keine Zeile ihrer Datei zuordnen. Die Pfade kennt nur die CLI; ein Ziel, das
    ein Hash war, hat keinen. Dasselbe Dokument als Pfad und als Hash ist eine
    Zeile."""
    fertig = await _endgueltig(
        async_conn, db_conn, tmp_path, _test_datenbank, "fertig.png", DocumentStatus.DONE
    )
    leer = await _endgueltig(
        async_conn, db_conn, tmp_path, _test_datenbank, "leer.png", DocumentStatus.EMPTY
    )
    pfad_fertig, pfad_leer = str(tmp_path / "fertig.png"), str(tmp_path / "leer.png")
    ziele = ["wait", pfad_fertig, pfad_leer, leer.sha256]

    text = cli.invoke(app, ziele)
    mit_json = cli.invoke(app, [*ziele, "--json"])

    assert text.exit_code == 0, text.output
    assert sorted(text.stdout.splitlines()) == sorted(
        [f"{fertig.sha256}: done  {pfad_fertig}", f"{leer.sha256}: empty  {pfad_leer}"]
    )
    assert mit_json.exit_code == 0, mit_json.output
    assert sorted((z["sha256"], z["paths"]) for z in map(json.loads, mit_json.stdout.splitlines())) == sorted(
        [(fertig.sha256, [pfad_fertig]), (leer.sha256, [pfad_leer])]
    )
    nur_hash = cli.invoke(app, ["wait", fertig.sha256, "--json"])
    assert json.loads(nur_hash.stdout)["paths"] == []


async def test_wait_ein_zeilenumbruch_im_dateinamen_faelscht_keine_zeile(
    cli: CliRunner,
    db_conn: psycopg.Connection[Any],
    async_conn: psycopg.AsyncConnection[Any],
    _test_datenbank: str,
    tmp_path: Path,
) -> None:
    """Ein Dateiname darf einen Zeilenumbruch tragen. In der Textausgabe stuende
    sonst eine zweite Zeile, die wie das Ergebnis eines anderen Dokuments
    aussieht; ein solcher Pfad erscheint deshalb maskiert, als Python-Literal."""
    name = "a\n" + "b" * 64 + ": done.png"
    huelle = await _endgueltig(
        async_conn, db_conn, tmp_path, _test_datenbank, name, DocumentStatus.DONE
    )
    pfad = str(tmp_path / name)

    ergebnis = cli.invoke(app, ["wait", pfad])

    assert ergebnis.exit_code == 0, ergebnis.output
    assert ergebnis.stdout == f"{huelle.sha256}: done  {pfad!r}\n"


async def test_wait_endet_mit_drei_wenn_eines_scheiterte(
    cli: CliRunner,
    db_conn: psycopg.Connection[Any],
    async_conn: psycopg.AsyncConnection[Any],
    _test_datenbank: str,
    tmp_path: Path,
) -> None:
    """Ein gescheitertes Dokument beendet das Warten auf die uebrigen nicht; jedes
    hat seine Zeile, und der Code ist 3."""
    fertig = await _endgueltig(
        async_conn, db_conn, tmp_path, _test_datenbank, "fertig.png", DocumentStatus.DONE
    )
    kaputt = await _endgueltig(
        async_conn, db_conn, tmp_path, _test_datenbank, "kaputt.png", DocumentStatus.INPUT_ERROR
    )

    ergebnis = cli.invoke(app, ["wait", kaputt.sha256, fertig.sha256, "--json"])

    assert ergebnis.exit_code == 3, ergebnis.output
    assert len(ergebnis.stdout.splitlines()) == 2


async def test_wait_endet_mit_vier_wenn_nach_der_frist_eines_offen_ist(
    cli: CliRunner,
    db_conn: psycopg.Connection[Any],
    async_conn: psycopg.AsyncConnection[Any],
    _test_datenbank: str,
    tmp_path: Path,
) -> None:
    """Nach Ablauf der Frist offen schlaegt gescheitert: 4 vor 3, denn 4 verlangt
    eine Handlung (spaeter erneut warten), die Fehlschlaege stehen in den Zeilen.
    Die offenen nennt stderr; auf stdout stehen nur die endgueltigen."""
    kaputt = await _endgueltig(
        async_conn, db_conn, tmp_path, _test_datenbank, "kaputt.png", DocumentStatus.FAILED
    )
    offen = _offen(db_conn, tmp_path, "offen.png")

    ergebnis = cli.invoke(
        app, ["wait", kaputt.sha256, offen, "--timeout", "0.5", "--json"]
    )

    assert ergebnis.exit_code == 4, ergebnis.output
    assert [json.loads(z)["sha256"] for z in ergebnis.stdout.splitlines()] == [kaputt.sha256]
    assert offen in ergebnis.stderr


async def test_wait_ein_unbekannter_hash_ist_sofort_eins(
    cli: CliRunner,
    db_conn: psycopg.Connection[Any],
    tmp_path: Path,
) -> None:
    """Ein unbekannter Hash ist ein Betriebsfehler und kommt sofort: 1, noch ehe
    die Frist fuer das offene Dokument daneben ablaufen kann. Einen Vorrang
    zwischen 1 und 4 braucht es nicht weiter: Ein Betriebsfehler und der
    Fristablauf beenden das Warten beide, also kommt nie beides."""
    offen = _offen(db_conn, tmp_path, "offen.png")
    ergebnis = cli.invoke(app, ["wait", offen, "f" * 64, "--timeout", "30"])
    assert ergebnis.exit_code == 1
    assert "f" * 64 in ergebnis.stderr
    assert ergebnis.stdout == ""


async def test_wait_ein_betriebsfehler_nach_einem_gescheiterten_ist_eins(
    cli: CliRunner,
    db_conn: psycopg.Connection[Any],
    async_conn: psycopg.AsyncConnection[Any],
    _test_datenbank: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """1 vor 3: Steht schon die Zeile eines gescheiterten Dokuments, und endet
    das Warten dann mit einem Betriebsfehler, ist der Code 1. Die Zeile bleibt
    stehen."""
    kaputt = await _endgueltig(
        async_conn, db_conn, tmp_path, _test_datenbank, "kaputt.png", DocumentStatus.FAILED
    )
    echt = client_modul.OCRClient.as_completed

    def dann_betriebsfehler(self: Any, hashes: Any, timeout: Any = None) -> Any:
        yield from echt(self, hashes, timeout=timeout)
        raise DatabaseError("Warten: Datenbank antwortet nicht")

    monkeypatch.setattr(client_modul.OCRClient, "as_completed", dann_betriebsfehler)
    ergebnis = cli.invoke(app, ["wait", kaputt.sha256])

    assert ergebnis.exit_code == 1
    assert ergebnis.stdout == f"{kaputt.sha256}: failed\n"
    assert "DatabaseError" in ergebnis.stderr


@pytest.mark.parametrize("frist", ["nan", "-1"])
def test_wait_weist_eine_frist_ab_die_nie_oder_schon_abgelaufen_waere(
    cli: CliRunner, frist: str
) -> None:
    """`nan` liefe nie ab, eine negative Frist ist ein Irrtum: Fehlbedienung, 2."""
    ergebnis = cli.invoke(app, ["wait", "f" * 64, "--timeout", frist])
    assert ergebnis.exit_code == 2, ergebnis.output


def test_wait_endet_bei_strg_c_mit_130(
    cli: CliRunner, db_conn: psycopg.Connection[Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    offen = _offen(db_conn, tmp_path, "offen.png")

    def unterbrochen(self: Any, hashes: Any, timeout: Any = None) -> Any:
        raise KeyboardInterrupt

    monkeypatch.setattr(client_modul.OCRClient, "as_completed", unterbrochen)
    ergebnis = cli.invoke(app, ["wait", offen])
    assert ergebnis.exit_code == 130


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
    # Und `retry` ohne Ziel UND ohne `--all`, oder mit beidem: nicht klar, was
    # gemeint ist.
    assert cli.invoke(app, ["retry"]).exit_code == 2
    assert cli.invoke(app, ["retry", "a" * 64, "--all"]).exit_code == 2


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
        endpoint_id="testendpoint",
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
        raw={"markdown": "x" * 5000, "middle_json": [], "images": {}},
        endpoint_id="testendpoint",
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
    # `ocr` heisst wie `OCRClient.ocr` und `ocr_start`, `retry` wie `retry` und
    # `retry_all`.
    assert hasattr(OCRClient, "ocr")
    assert hasattr(OCRClient, "ocr_start")
    assert hasattr(OCRClient, "retry")
    assert hasattr(OCRClient, "retry_all")
    assert not hasattr(OCRClient, "retry_failed")


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
        raw={"markdown": "derselbe Text", "middle_json": [], "images": {}},
        endpoint_id="testendpoint",
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
    assert angaben["status"] is None
    assert angaben["phase"] == "waiting"
    assert angaben["job"]["status"] == "todo"

    # Ohne `--json` dasselbe, lesbar: offen, und was der Job tut.
    text = cli.invoke(app, ["show", sha])
    assert text.exit_code == 0, text.output
    assert "offen" in text.stdout and "waiting" in text.stdout


def test_show_raw_eines_offenen_dokuments_sagt_dass_es_noch_kein_ergebnis_hat(
    cli: CliRunner, db_conn: psycopg.Connection[Any], tmp_path: Path
) -> None:
    datei, sha = _datei(tmp_path, "offen.png")
    cli.invoke(app, ["ocr", str(datei)])

    ergebnis = cli.invoke(app, ["show", sha, "--raw"])

    assert ergebnis.exit_code == 1
    assert ergebnis.stdout == ""
    assert "noch kein Ergebnis" in ergebnis.stderr
    # Wer an der Kommandozeile sitzt, bekommt die Kommandos genannt, nicht nur
    # die Funktionen der Bibliothek.
    assert "mineru-ocr show" in ergebnis.stderr
    assert "mineru-ocr wait" in ergebnis.stderr

    markdown = cli.invoke(app, ["markdown", sha])
    assert markdown.exit_code == 1
    assert "mineru-ocr show" in markdown.stderr


async def test_show_zeigt_was_document_sagt_und_raw_die_vollstaendige_antwort(
    cli: CliRunner,
    db_conn: psycopg.Connection[Any],
    async_conn: psycopg.AsyncConnection[Any],
    _test_datenbank: str,
    tmp_path: Path,
) -> None:
    """`show` zeigt den Stand aus `document()` ohne `raw`; `show --raw` gibt die
    vollstaendige Antwort als JSON aus, auch ohne `--json`."""
    huelle = _anlegen(db_conn, tmp_path, "fertig.png", _test_datenbank)
    antwort = {"markdown": "ganz", "middle_json": [], "images": {}, "extras": {"a": [1]}}
    await write_terminal_async(
        async_conn,
        sha256=huelle.sha256,
        status=DocumentStatus.DONE,
        raw=antwort,
        endpoint_id="testendpoint",
        mineru_version="4.0.12",
    )

    stand = json.loads(cli.invoke(app, ["show", huelle.sha256, "--json"]).stdout)
    assert stand["status"] == "done"
    assert stand["phase"] == "terminal"
    assert stand["mime"] == "image/png"
    assert "raw" not in stand

    # Ohne `--json` dasselbe, lesbar: Zustand und die Angaben des Ergebnisses.
    text = cli.invoke(app, ["show", huelle.sha256])
    assert text.exit_code == 0, text.output
    assert "Zustand:   done" in text.stdout and "terminal" in text.stdout
    assert f"Typ:       image/png  {huelle.size_bytes} Byte" in text.stdout
    # Womit gelesen wurde (Mineroo#14): die Version, die der Worker meldete, und
    # die bestellten Lese-Einstellungen.
    assert "Gelesen:   MinerU 4.0.12, advanced, ocr, Bildanalyse an" in text.stdout
    assert stand["mineru_version"] == "4.0.12" and stand["tier"] == "advanced"
    assert "ganz" not in text.stdout  # kein raw

    # Ein Eingabefehler ohne Eingabe hat weder Typ noch Groesse: Das sagt die
    # Ausgabe, statt `None` zu drucken.
    ohne = "d" * 64
    await write_terminal_async(
        async_conn,
        sha256=ohne,
        status=DocumentStatus.INPUT_ERROR,
        raw={},
        error="Eingabe fehlt",
        endpoint_id="testendpoint",
    )
    text = cli.invoke(app, ["show", ohne])
    assert text.exit_code == 0, text.output
    assert "Typ:       unbekannt  ? Byte" in text.stdout
    assert "Fehler:    Eingabe fehlt" in text.stdout
    (typ_zeile,) = [z for z in text.stdout.splitlines() if "Typ:" in z]
    assert "None" not in typ_zeile

    ergebnis = cli.invoke(app, ["show", huelle.sha256, "--raw"])
    assert ergebnis.exit_code == 0, ergebnis.output
    vollstaendig = json.loads(ergebnis.stdout)
    assert vollstaendig["raw"] == antwort
    assert vollstaendig["status"] == "done"


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


async def test_status_nennt_die_verteilung_nach_version_und_lese_einstellungen(
    cli: CliRunner,
    db_conn: psycopg.Connection[Any],
    async_conn: psycopg.AsyncConnection[Any],
    _test_datenbank: str,
    tmp_path: Path,
) -> None:
    """Je Version und bestellten Lese-Einstellungen eine Zeile mit der Zahl der
    gelesenen Dokumente; mit `--json` unter `readings` (Mineroo#14)."""
    huelle = _anlegen(db_conn, tmp_path, "gelesen.png", _test_datenbank)
    await write_terminal_async(
        async_conn, sha256=huelle.sha256, status=DocumentStatus.DONE, raw={},
        endpoint_id="e", mineru_version="4.0.12",
    )

    text = cli.invoke(app, ["status"])
    assert text.exit_code == 0, text.output
    assert "Gelesen:    1  MinerU 4.0.12, advanced, ocr, Bildanalyse an" in text.stdout
    als_json = json.loads(cli.invoke(app, ["status", "--json"]).stdout)
    assert als_json["readings"] == [
        {"mineru_version": "4.0.12", "tier": "advanced", "ocr_mode": "ocr",
         "image_analysis": True, "documents": 1}
    ]


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

    # Offen hat keinen Endzustand: `null`.
    erste = json.loads(cli.invoke(app, ["ocr", str(datei), "--json"]).stdout)
    assert erste == {
        "documents": [
            {"path": str(datei), "sha256": sha, "status": None, "queued": True}
        ],
        "skipped": [],
    }

    # Dasselbe noch einmal: Das Dokument ist unterwegs, es geschieht nichts.
    zweite = json.loads(cli.invoke(app, ["ocr", str(datei), "--json"]).stdout)
    assert zweite["documents"] == [
        {"path": str(datei), "sha256": sha, "status": None, "queued": False}
    ]
    assert _jobs(db_conn) == 1


def _lese_einstellungen(db_conn: psycopg.Connection[Any], sha: str) -> tuple[Any, ...]:
    zeile = db_conn.execute(
        "SELECT tier, ocr_mode, image_analysis FROM inputs WHERE sha256 = %s", (sha,)
    ).fetchone()
    assert zeile is not None
    return tuple(zeile)


def test_ocr_nimmt_die_lese_einstellungen_als_optionen(
    cli: CliRunner, db_conn: psycopg.Connection[Any], tmp_path: Path
) -> None:
    """Je Lauf gewaehlt (Mineroo#14): `--tier`, `--ocr-mode` und
    `--image-analysis/--no-image-analysis` gelten fuer jede Datei des Laufs, auch
    in Ordnern; ohne Angabe gelten die Voreinstellungen."""
    vorein, sha_vorein = _datei(tmp_path / "vorein", "a.png")
    _, sha_b = _datei(tmp_path / "gewaehlt", "b.png")
    _, sha_c = _datei(tmp_path / "gewaehlt", "c.png")

    assert cli.invoke(app, ["ocr", str(vorein)]).exit_code == 0
    ergebnis = cli.invoke(
        app,
        [
            "ocr", str(tmp_path / "gewaehlt"),
            "--tier", "standard", "--ocr-mode", "txt", "--no-image-analysis",
        ],
    )

    assert ergebnis.exit_code == 0, ergebnis.output
    assert _lese_einstellungen(db_conn, sha_vorein) == ("advanced", "ocr", True)
    for sha in (sha_b, sha_c):
        assert _lese_einstellungen(db_conn, sha) == ("standard", "txt", False)


def test_ocr_weist_eine_unbekannte_lese_einstellung_ab_bevor_es_etwas_einreiht(
    cli: CliRunner, db_conn: psycopg.Connection[Any], tmp_path: Path
) -> None:
    """Ein Tippfehler ist ein Aufruffehler (Exit-Code 2) und reiht nichts ein;
    die gueltigen Werte nennt die Meldung."""
    datei, _ = _datei(tmp_path, "a.png")

    for falsch in (["--tier", "advance"], ["--ocr-mode", "OCR"]):
        ergebnis = cli.invoke(app, ["ocr", str(datei), *falsch])
        assert ergebnis.exit_code == 2, ergebnis.output
        assert falsch[1] in ergebnis.output
    assert _jobs(db_conn) == 0


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
        async_conn, sha256=sha, status=DocumentStatus.FAILED, raw={}, error="aus", endpoint_id="testendpoint"
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


def test_ocr_ordner_eine_kaputte_pdf_haelt_die_uebrigen_nicht_auf(
    cli: CliRunner, db_conn: psycopg.Connection[Any], tmp_path: Path
) -> None:
    """Das Einreihen oeffnet keine PDF: Eine kaputte in einem Ordner wird wie jede
    andere eingereiht (sie endet erst im Job als `input_error`), und die Dateien
    davor und danach laufen mit. Frueher brach sie den ganzen Ordnerlauf ab."""
    ordner = tmp_path / "scans"
    _, vorher = _datei(ordner, "a.png")
    _, kaputt = _datei(ordner, "b-kaputt.pdf", b"%PDF-1.7\nund dann nur noch Unsinn\n")
    _, nachher = _datei(ordner, "c.png")

    ergebnis = cli.invoke(app, ["ocr", str(ordner), "--json"])

    assert ergebnis.exit_code == 0, ergebnis.output
    angaben = json.loads(ergebnis.stdout)
    assert [(d["sha256"], d["queued"]) for d in angaben["documents"]] == [
        (vorher, True),
        (kaputt, True),
        (nachher, True),
    ]
    assert angaben["skipped"] == []
    assert _jobs(db_conn) == 3


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

    def zweiter_wird_unterbrochen(self: Any, data: Any, **lesen: Any) -> Any:
        aufrufe["n"] += 1
        if aufrufe["n"] == 2:
            raise KeyboardInterrupt
        return echtes(self, data, **lesen)

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

    def dritter_scheitert(self: Any, data: Any, **lesen: Any) -> Any:
        aufrufe["n"] += 1
        if aufrufe["n"] == 3:
            raise DatabaseError("Verbindung weg")
        return echtes(self, data, **lesen)

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


@pytest.fixture
def cli_ohne_schluessel(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch, _test_datenbank: str
) -> CliRunner:
    """Wie `cli`, aber ohne RunPod-Schluessel: keiner uebergeben, keine Datei
    (die liegt dank `conftest` nirgends). Spricht ein Kommando RunPod trotzdem an,
    scheitert der Test laut."""
    def ohne(**angaben: Any) -> Settings:
        angaben.setdefault("db_dsn", None)
        if angaben["db_dsn"] is None:
            angaben["db_dsn"] = _test_datenbank
        angaben["api_key"] = None
        return _ECHTES_LOAD(**angaben)

    def nie(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("RunPod wurde angesprochen")

    monkeypatch.setattr(Settings, "load", staticmethod(ohne))
    monkeypatch.setattr(client_modul, "_runpod_fragen", nie)
    return cli


def test_abschicken_und_lesen_brauchen_keinen_runpod_schluessel(
    cli_ohne_schluessel: CliRunner, db_conn: psycopg.Connection[Any], tmp_path: Path
) -> None:
    """Abschicken, `show`, `markdown`, `wait` und `retry` brauchen nur die
    Datenbank: Fehlt der Schluessel, ist das kein Fehler."""
    datei, sha = _datei(tmp_path, "a.png")

    ergebnis = cli_ohne_schluessel.invoke(app, ["ocr", str(datei), "--json"])
    assert ergebnis.exit_code == 0, ergebnis.output
    assert json.loads(ergebnis.stdout)["documents"][0]["queued"] is True
    assert _jobs(db_conn) == 1

    for kommando in (["show", sha], ["markdown", sha], ["retry", sha]):
        ergebnis = cli_ohne_schluessel.invoke(app, kommando)
        assert "API-Schluessel" not in ergebnis.output, kommando
        assert ergebnis.exception is None or isinstance(
            ergebnis.exception, SystemExit
        ), (kommando, ergebnis.output)
    ergebnis = cli_ohne_schluessel.invoke(app, ["wait", sha, "--timeout", "0.1"])
    assert "API-Schluessel" not in ergebnis.output
    assert ergebnis.exception is None or isinstance(ergebnis.exception, SystemExit)


def test_setup_braucht_weder_endpoint_noch_schluessel(
    cli_ohne_schluessel: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MINERU_OCR_ENDPOINT_ID")
    ergebnis = cli_ohne_schluessel.invoke(app, ["setup"])
    assert ergebnis.exit_code == 0, ergebnis.output


def test_setup_meldet_auf_stderr_und_nennt_kein_passwort(
    cli_ohne_schluessel: CliRunner, _test_datenbank: str
) -> None:
    """Auf stdout stehen nur Daten, und `setup` liefert keine: Die Meldung geht auf
    stderr. Die DSN kommt aus Konfiguration oder Umgebung und kann ein Passwort
    tragen; die Meldung nennt die Datenbank, nie das Passwort (Mineroo#3)."""
    dsn = f"{_test_datenbank} password=nur-ein-testwert"
    ergebnis = cli_ohne_schluessel.invoke(app, ["setup", "--dsn", dsn])
    assert ergebnis.exit_code == 0, ergebnis.output
    assert ergebnis.stdout == ""
    assert "mineru_ocr_test" in ergebnis.stderr
    assert "nur-ein-testwert" not in ergebnis.output


def test_abschicken_braucht_keinen_endpoint(
    cli_ohne_schluessel: CliRunner,
    db_conn: psycopg.Connection[Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Den Endpoint traegt erst die Ergebniszeile, die der Job am Ende anlegt;
    Abschicken reiht auch ohne ihn ein."""
    monkeypatch.delenv("MINERU_OCR_ENDPOINT_ID")
    datei, _ = _datei(tmp_path, "a.png")

    ergebnis = cli_ohne_schluessel.invoke(app, ["ocr", str(datei)])

    assert ergebnis.exit_code == 0, ergebnis.output
    assert _zaehlen(db_conn, "documents") == 0
    assert _jobs(db_conn) == 1


@pytest.mark.parametrize("kommando", [["status"], ["preflight"]])
def test_wer_runpod_anspricht_verlangt_den_schluessel(
    cli_ohne_schluessel: CliRunner, kommando: list[str]
) -> None:
    """`status` und `preflight` fragen RunPod: ohne Schluessel ein Betriebsfehler
    mit Auskunft, und RunPod wird gar nicht erst angesprochen."""
    ergebnis = cli_ohne_schluessel.invoke(app, kommando)
    assert ergebnis.exit_code == 1, ergebnis.output
    assert "Kein API-Schluessel" in ergebnis.stderr
    assert not isinstance(ergebnis.exception, AssertionError)


def test_daemon_verlangt_den_schluessel_vor_dem_start(
    cli_ohne_schluessel: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mineru_ocr import daemon as daemon_modul

    gestartet: list[bool] = []

    def gestartet_merken(*_a: Any, **_k: Any) -> int:
        gestartet.append(True)
        return 0

    monkeypatch.setattr(daemon_modul, "main", gestartet_merken)
    ergebnis = cli_ohne_schluessel.invoke(app, ["daemon"])
    assert ergebnis.exit_code == 1, ergebnis.output
    assert "Kein API-Schluessel" in ergebnis.stderr
    assert gestartet == []


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
    """`retry` fragt nach, bevor es Geld ausgibt, und sagt danach, was es tat.

    Jedes wieder vorgelegte Dokument wird VOLLSTAENDIG neu verarbeitet und neu
    bezahlt. Ein Kommando, das das ohne Rueckfrage tut, gibt fremdes Geld aus.

    Und die Gegenprobe im selben Test: Wird die Frage verneint, passiert NICHTS.
    """
    datei, sha = _datei(tmp_path, "teuer.png")
    cli.invoke(app, ["ocr", str(datei)])
    db_conn.execute("UPDATE procrastinate_jobs SET status = 'failed'")
    await write_terminal_async(
        async_conn, sha256=sha, status=DocumentStatus.FAILED, raw={}, endpoint_id="testendpoint"
    )

    verneint = cli.invoke(app, ["retry", str(datei)], input="n\n")
    assert verneint.exit_code == 0
    assert "kostet Geld" in verneint.output
    # Nichts passiert: Der Job steht unveraendert auf `failed`, das Ergebnis auch.
    zustand = db_conn.execute(
        "SELECT status FROM procrastinate_jobs WHERE lock = %s", (sha,)
    ).fetchone()
    assert zustand is not None and zustand[0] == "failed"
    assert _status(db_conn, sha) == "failed"

    bejaht = cli.invoke(app, ["retry", str(datei)], input="y\n")
    assert bejaht.exit_code == 0, bejaht.output
    assert f"{sha} zurueckgesetzt" in bejaht.stdout
    zustand = db_conn.execute(
        "SELECT status FROM procrastinate_jobs WHERE lock = %s", (sha,)
    ).fetchone()
    assert zustand is not None and zustand[0] == "todo"
    assert _status(db_conn, sha) is None


def _status(conn: psycopg.Connection[Any], sha: str) -> str | None:
    zeile = conn.execute("SELECT status FROM documents WHERE sha256 = %s", (sha,)).fetchone()
    return None if zeile is None else str(zeile[0])


def test_retry_mit_yes_fragt_nicht_und_ein_offenes_dokument_ist_unterwegs(
    cli: CliRunner, db_conn: psycopg.Connection[Any], tmp_path: Path
) -> None:
    """`--yes` ist der ausdrueckliche Verzicht auf die Rueckfrage — fuer Skripte.
    Ein offenes Dokument, dessen Job wartet, meldet `retry` als unterwegs und
    endet mit 0; ein Fehler ist das nicht."""
    datei, sha = _datei(tmp_path, "skript.png")
    cli.invoke(app, ["ocr", str(datei)])

    ergebnis = cli.invoke(app, ["retry", sha, "--yes"])

    assert ergebnis.exit_code == 0, ergebnis.output
    assert "Fortfahren" not in ergebnis.output
    # Auf stdout nur die Daten; die Kostenwarnung ist eine Meldung (stderr).
    assert ergebnis.stdout == f"{sha} unterwegs\n"
    assert "kostet Geld" in ergebnis.stderr
    assert _jobs(db_conn) == 1


async def test_retry_all_nimmt_jedes_dokument_ohne_gueltiges_ergebnis(
    cli: CliRunner,
    db_conn: psycopg.Connection[Any],
    async_conn: psycopg.AsyncConnection[Any],
    tmp_path: Path,
) -> None:
    """`--all` laesst die Bibliothek auswaehlen: jedes Dokument ohne gueltiges
    Ergebnis. Gefragt wird einmal, ohne Anzahl, und danach stehen die Zahlen je
    Ausgang da."""
    gescheitert_datei, gescheitert = _datei(tmp_path, "gescheitert.png")
    abgesagt_datei, abgesagt = _datei(tmp_path, "abgesagt.png")
    for datei in (gescheitert_datei, abgesagt_datei):
        cli.invoke(app, ["ocr", str(datei)])
    await write_terminal_async(
        async_conn, sha256=gescheitert, status=DocumentStatus.FAILED, raw={},
        endpoint_id="testendpoint",
    )
    for sha, zustand in ((gescheitert, "failed"), (abgesagt, "cancelled")):
        db_conn.execute(
            "UPDATE procrastinate_jobs SET status = %s WHERE lock = %s", (zustand, sha)
        )

    ergebnis = cli.invoke(app, ["retry", "--all"], input="y\n")

    assert ergebnis.exit_code == 0, ergebnis.output
    # Rueckfrage und Warnung sind Meldungen (stderr); auf stdout stehen die
    # Zeilen je Dokument. (Die Antwort „y“ steht dort nur, weil CliRunner die
    # Eingabe mitschreibt, im Terminal schreibt sie der Benutzer.)
    assert "Fortfahren" in ergebnis.stderr
    assert "kostet Geld" in ergebnis.stderr
    assert "Fortfahren" not in ergebnis.stdout
    assert "kostet Geld" not in ergebnis.stdout
    assert f"{gescheitert} zurueckgesetzt" in ergebnis.stdout
    assert f"{abgesagt} neu eingereiht" in ergebnis.stdout
    assert "1 zurueckgesetzt, 1 neu eingereiht, 0 unterwegs, 0 mit Ergebnis" in ergebnis.stderr


def test_retry_all_ohne_offene_dokumente_tut_nichts(cli: CliRunner) -> None:
    """Nichts zu wiederholen ist kein Fehler — und kostet nichts."""
    ergebnis = cli.invoke(app, ["retry", "--all", "--yes"])
    assert ergebnis.exit_code == 0
    assert "Nichts zu wiederholen" in ergebnis.stderr
    assert ergebnis.stdout == ""


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
        raw={"markdown": "Der ganze Text", "middle_json": [], "images": {}},
        endpoint_id="testendpoint",
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

    Der Fliesstext steht hier als Block vom Typ `text` im Middle-JSON und
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
            "middle_json": [
                {
                    "pages": [
                        {
                            "page_idx": 0,
                            "blocks": [
                                {
                                    "type": "header",
                                    "index": 0,
                                    "content": [
                                        {"type": "text", "content": "Aktenzeichen 123/456/7890"}
                                    ],
                                },
                                {
                                    "type": "text",
                                    "index": 1,
                                    "content": [{"type": "text", "content": "Der ganze Fliesstext."}],
                                },
                            ],
                        }
                    ]
                }
            ],
            "images": {},
        },
        endpoint_id="testendpoint",
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
    erwiesen hat. Ohne `middle_json` fehlt das Aktenzeichen, und die Ausgabe
    saehe vollstaendig aus.
    """
    huelle = _anlegen(db_conn, tmp_path, "ohne_liste.png", _test_datenbank)
    await write_terminal_async(
        async_conn,
        sha256=huelle.sha256,
        status=DocumentStatus.DONE,
        raw={"markdown": "Fliesstext ohne die Randbloecke.", "images": {}},
        endpoint_id="testendpoint",
    )

    ziel = tmp_path / "ohne_liste.md"
    ergebnis = cli.invoke(app, ["markdown", huelle.sha256, "--out", str(ziel)])
    assert ergebnis.exit_code == 1
    assert not ziel.exists()
    assert "middle_json" in ergebnis.output
