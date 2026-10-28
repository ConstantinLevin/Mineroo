"""Dateien abschicken, synchron warten und den ganzen Dokumenttext ausgeben.

Die Bibliothek bleibt der Zugang fuer Struktur, Rohantworten und Job-Verwaltung.
Pfade und Ausgabeziele kennt nur die CLI. `setup` und `daemon` sind Betrieb.

Exit-Codes: 0 Erfolg (auch empty), 1 Betriebs-/Dateifehler, 2 Aufruffehler,
3 failed/input_error, 4 Wartefrist abgelaufen, 5 kein laufender Daemon,
130 Strg-C. Bei gemischten Ergebnissen gilt 1 vor 5 vor 4 vor 3 vor 0.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import tempfile
from pathlib import Path
from typing import Annotated, Final

import click
import typer
from typer.core import TyperGroup
from typer.main import get_command

from . import core, db, mime
from .client import OCRClient
from .config import GUELTIGE_OCR_MODI, GUELTIGE_STUFEN, Settings
from .errors import DocumentNotFound, MineruOcrError, UnsupportedTypeError, TooLargeError, WaitTimeoutError
from .db import DocumentStatus


class _PfadGruppe(TyperGroup):
    """Leitet Dateiaufrufe an die Lese-App, Betriebskommandos an die Gruppe."""

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        if args and args[0] not in {"setup", "daemon", "--help"}:
            args = ["_pfade", *args]
        return super().parse_args(ctx, args)

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        if cmd_name == "_pfade":
            return get_command(_lese_app)
        return super().get_command(ctx, cmd_name)

    def format_help(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        get_command(_lese_app).format_help(ctx, formatter)
        self.format_commands(ctx, formatter)


app = typer.Typer(name="mineru-ocr", cls=_PfadGruppe, no_args_is_help=True)
_lese_app = typer.Typer(name="mineru-ocr", pretty_exceptions_enable=False)
_LOG_NAME: Final = "mineru_ocr"


class _FehlerAusgabe(logging.Handler):
    """Schreibt Bibliotheksmeldungen auf den aktuellen stderr-Strom."""

    def emit(self, record: logging.LogRecord) -> None:
        typer.echo(self.format(record), err=True)


@app.callback()
def _vorweg(ctx: typer.Context) -> None:
    logger = logging.getLogger(_LOG_NAME)
    if not any(isinstance(h, _FehlerAusgabe) for h in logger.handlers):
        logger.addHandler(_FehlerAusgabe())
    logger.setLevel(logging.INFO if ctx.invoked_subcommand == "daemon" else logging.WARNING)


def _pfad_zeigen(pfad: str) -> str:
    return pfad if pfad.isprintable() else repr(pfad)


def _lesen(datei: Path) -> bytes:
    with datei.open("rb") as geoeffnet:
        mime.sniff_file(geoeffnet)
    core.check_size(datei.stat().st_size, datei.name)
    return datei.read_bytes()


def _ausgabe_pruefen(out: Path, kandidaten: list[tuple[Path, bool]]) -> None:
    """Verhindert Verzeichnisse als Ziel und das Ueberschreiben einer Eingabe."""
    if out.is_dir():
        raise typer.BadParameter("-o braucht eine Datei, kein Verzeichnis.")
    for datei, _ in kandidaten:
        if out.resolve() == datei.resolve() or (
            out.exists() and datei.exists() and out.samefile(datei)
        ):
            raise typer.BadParameter("Die Ausgabe darf keine Eingabedatei ueberschreiben.")


def _textdatei_schreiben(text: str, out: Path | None) -> str:
    """Schreibt UTF-8 ins gewaehlte oder temporaere Ziel und liefert dessen Pfad."""
    if out is not None:
        out.write_text(text, encoding="utf-8")
        return str(out)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", prefix="mineru-ocr-", suffix=".md", delete=False,
    ) as ausgabe:
        ausgabe.write(text)
    return ausgabe.name


def _betriebsfehler(fehler: BaseException) -> None:
    typer.echo(f"{type(fehler).__name__}: {fehler}", err=True)
    raise typer.Exit(code=1)


@app.command()
def setup(
    dsn: Annotated[str | None, typer.Option(help="Verbindung; sonst die Konfiguration (MINERU_OCR_DB_DSN, config.toml).")] = None,
) -> None:
    """Legt Datenbank und Schema an. Mehrfach aufrufbar, veraendert nie Daten.

    Ruft `db.setup`. Keine Migrationen — findet das Kommando eine Tabelle, die
    nicht zur Zeilenklasse passt, sagt es das samt Abhilfe, statt selbst etwas
    umzubauen.
    """
    try:
        einstellung = Settings.load(db_dsn=dsn)
        name = db.setup(einstellung.db_dsn)
    except MineruOcrError as fehler:
        _betriebsfehler(fehler)
    # Eine Meldung, keine Daten, also stderr; und nie die DSN, sie kann ein
    # Passwort tragen.
    typer.echo(f"Datenbank {name!r} und Schema stehen.", err=True)


_daemon_app = typer.Typer(no_args_is_help=True)
app.add_typer(_daemon_app, name="daemon", help="Daemons im Hintergrund verwalten.")


@_daemon_app.command("start")
def daemon_start(
    anzahl: Annotated[int, typer.Argument(min=1, metavar="X")],
    dsn: Annotated[str | None, typer.Option()] = None,
    concurrency: Annotated[int | None, typer.Option(min=1)] = None,
) -> None:
    """Startet X vollstaendige Daemons mit je eigenem Protokoll."""
    from . import daemons
    try:
        daten = daemons.start(Settings.load(db_dsn=dsn, ocr_concurrency=concurrency), anzahl)
        typer.echo(json.dumps({"started": len(daten), "daemons": daten}, indent=2))
    except (MineruOcrError, OSError, ValueError) as fehler:
        _betriebsfehler(fehler)


@_daemon_app.command("list")
def daemon_list(dsn: Annotated[str | None, typer.Option()] = None) -> None:
    """Zeigt lokale Daemons, OCR-Plaetze und die OCR-Warteschlange als JSON."""
    from . import daemons
    try:
        typer.echo(json.dumps(daemons.status(Settings.load(db_dsn=dsn)), indent=2))
    except (MineruOcrError, OSError) as fehler:
        _betriebsfehler(fehler)


@_daemon_app.command("stop")
def daemon_stop(
    anzahl: Annotated[int, typer.Argument(min=1, metavar="X")],
    dsn: Annotated[str | None, typer.Option()] = None,
) -> None:
    """Beendet bis zu X Daemons ordentlich, die am wenigsten belegten zuerst."""
    from . import daemons
    try:
        typer.echo(json.dumps(daemons.stop(Settings.load(db_dsn=dsn), anzahl), indent=2))
    except (MineruOcrError, OSError) as fehler:
        _betriebsfehler(fehler)


@_lese_app.command()
def lesen(
    paths: Annotated[list[Path], typer.Argument(help="Dateien oder Verzeichnisse.", metavar="PATH…")],
    temporary: Annotated[bool, typer.Option("-t", help="Text in eine temporaere Datei schreiben; deren Pfad ausgeben.")] = False,
    out: Annotated[Path | None, typer.Option("-o", help="Text in diese UTF-8-Datei schreiben; deren Pfad ausgeben.")] = None,
    timeout: Annotated[float | None, typer.Option(help="Wartefrist je Dokument in Sekunden; ohne Angabe unbegrenzt.")] = None,
    tier: Annotated[str | None, typer.Option(click_type=click.Choice(sorted(GUELTIGE_STUFEN)), help="MinerU-Stufe fuer neue Dokumente.")] = None,
    ocr_mode: Annotated[str | None, typer.Option(click_type=click.Choice(sorted(GUELTIGE_OCR_MODI)), help="Textquelle fuer neue Dokumente.")] = None,
    image_analysis: Annotated[bool | None, typer.Option("--image-analysis/--no-image-analysis", help="Bildanalyse fuer neue Dokumente.")] = None,
) -> None:
    """Schickt Dokumente ab, wartet und schreibt den Volltext auf stdout.

    Treffer aus der Datenbank brauchen keinen Daemon. Neue oder offene Dateien
    brauchen einen frischen Herzschlag; ohne ihn wird nichts eingereiht.
    Mehrere Dateien erhalten je eine Pfad-Ueberschrift; -t/-o schreiben alle
    Texte in eine Datei. Meldungen und Gruende stehen auf stderr.

    Exit-Codes: 0 Erfolg (auch empty), 1 Betriebs-/Dateifehler, 2 Aufruffehler,
    3 failed/input_error, 4 Wartefrist abgelaufen, 5 kein laufender Daemon,
    130 Strg-C. Vorrang bei mehreren: 1 vor 5 vor 4 vor 3 vor 0.
    """
    if temporary and out is not None:
        raise typer.BadParameter("-t und -o schliessen einander aus.")
    if timeout is not None and (not math.isfinite(timeout) or timeout < 0):
        raise typer.BadParameter("--timeout muss eine endliche Zahl ab null sein.")
    kandidaten: list[tuple[Path, bool]] = []
    for pfad in paths:
        if pfad.is_dir():
            kandidaten.extend((d, False) for d in sorted(pfad.rglob("*")) if d.is_file())
        else:
            kandidaten.append((pfad, True))
    if out is not None:
        _ausgabe_pruefen(out, kandidaten)
    codes: set[int] = set()
    texte: list[str] = []
    erfolg = False
    try:
        with OCRClient(preflight=False) as klient:
            for pfad, ausdruecklich in kandidaten:
                try:
                    try:
                        inhalt = _lesen(pfad)
                    except (UnsupportedTypeError, TooLargeError) as fehler:
                        typer.echo(f"{_pfad_zeigen(str(pfad))}: uebersprungen: {fehler}", err=True)
                        if ausdruecklich:
                            codes.add(1)
                        continue
                    try:
                        huelle = klient.get(hashlib.sha256(inhalt).hexdigest())
                    except DocumentNotFound:
                        if not db.worker_heartbeats(klient.conn):
                            typer.echo(
                                f"{_pfad_zeigen(str(pfad))}: Es laufen keine Daemons. "
                                "Start: `mineru-ocr daemon start X` "
                                "und Kontrolle: `mineru-ocr daemon list`.",
                                err=True,
                            )
                            codes.add(5)
                            continue
                        gereicht = klient.ocr_start(
                            inhalt, tier=tier, ocr_mode=ocr_mode, image_analysis=image_analysis,
                        )
                        huelle = klient.wait(str(gereicht), timeout=timeout)
                    if huelle.status != DocumentStatus.DONE:
                        typer.echo(
                            f"{_pfad_zeigen(str(pfad))}: {huelle.status.value}: "
                            f"{huelle.row.error or 'kein Text im Dokument'}",
                            err=True,
                        )
                        if not huelle.success:
                            codes.add(3)
                        else:
                            erfolg = True
                        continue
                    text = huelle.rendered_markdown
                    erfolg = True
                    if len(kandidaten) > 1:
                        text = f"# Datei: {_pfad_zeigen(str(pfad))}\n\n{text}"
                    if temporary or out is not None:
                        texte.append(text)
                    else:
                        typer.echo(text)
                except WaitTimeoutError as fehler:
                    typer.echo(f"{_pfad_zeigen(str(pfad))}: Wartefrist abgelaufen: {fehler}", err=True)
                    codes.add(4)
                except (MineruOcrError, OSError) as fehler:
                    typer.echo(f"{_pfad_zeigen(str(pfad))}: {type(fehler).__name__}: {fehler}", err=True)
                    codes.add(1)
        if (temporary or out is not None) and erfolg:
            typer.echo(_textdatei_schreiben("\n\n".join(texte), out))
    except (MineruOcrError, OSError) as fehler:
        _betriebsfehler(fehler)
    for code in (1, 5, 4, 3):
        if code in codes:
            raise typer.Exit(code=code)


def main() -> None:
    """Einstiegspunkt des Kommandos `mineru-ocr` (siehe `pyproject.toml`).

    Baut die Typer-App mit `pretty_exceptions_enable=False`. Grund: Mit der
    Voreinstellung druckt Typer bei einer unbehandelten Ausnahme einen reich
    formatierten Traceback SAMT den lokalen Variablen — und unter denen kann eine
    `Settings` stehen. Deren `__repr__` maskiert den Schluessel zwar, aber sich
    darauf zu verlassen hiesse, eine Sicherheitszusage an die Darstellungs-Logik
    einer fremden Bibliothek zu haengen. Ein schlichter Traceback genuegt.
    """
    app.pretty_exceptions_enable = False
    app()


if __name__ == "__main__":  # pragma: no cover — nur der Modul-Aufruf
    # `python -m mineru_ocr.cli …` ohne installiertes Paket. Ohne diesen Block
    # tat der Aufruf NICHTS und endete mit Exit 0 — kein Kommando, keine
    # Meldung, kein Fehler. Wer aus dem Verzeichnis heraus arbeitet (so steht
    # es in CLAUDE.md), probiert genau ihn zuerst und haelt das stille
    # Nichtstun fuer einen Erfolg.
    main()
