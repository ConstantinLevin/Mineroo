"""Die Kommandozeile. Enthaelt KEINE Logik — nur Uebersetzung.

Der zweite Zugangsweg neben `import mineru_ocr`, und ausdruecklich derselbe
Dienst und nicht ein zweiter. Jedes Kommando hier ruft genau eine oeffentliche
Funktion und formatiert deren Rueckgabe. Es entscheidet nichts, es rechnet
nichts, es prueft nichts, was nicht schon geprueft wird.

Warum diese Regel so hart ist: Sobald ein Kommando eine eigene Entscheidung
trifft — eine Voreinstellung anders setzt, einen Fall abfaengt, eine Schwelle
kennt —, verhaelt sich das Werkzeug ueber die Kommandozeile anders als aus Python
heraus. Von da an gibt es zwei Wahrheiten, und Fehler treten nur auf einem der
beiden Wege auf. Ein Kommando, das nur uebersetzt, kann das nicht.

Die Exit-Codes sind Teil des Vertrags, weil in einer Shell darauf verzweigt wird:
    0  Erfolg.
    1  Betriebsfehler — Endpoint nicht erreichbar, Datenbank weg, Konto leer.
    2  Aufruffehler — falsche Argumente, unbekannter Pfad. Setzt typer selbst.
    3  Nur bei `wait` und `markdown`: Das Dokument ist endgueltig, aber NICHT
       erfolgreich (`failed` oder `input_error`). Ein eigener Code, weil das
       weder ein Erfolg noch ein Fehler DES WERKZEUGS ist — das Werkzeug hat
       einwandfrei gearbeitet, das Ergebnis ist nur schlecht. Wer beides auf 1
       wirft, kann in einer Schleife nicht mehr zwischen „kaputtes Dokument" und
       „kaputte Einrichtung" unterscheiden.

Gesetzt werden sie mit `raise typer.Exit(code=…)`.

Jedes Kommando, das etwas ausgibt, kennt `--json`. Menschenlesbar ist die
Voreinstellung; `--json` ist fuer den Fall, dass ein anderes Programm zuhoert.

Die Namen sind die der Python-Flaeche: `ocr` reiht ein wie `OCRClient.ocr_path`,
`status` und `wait` heissen wie ihre Methoden. Zwei Vokabulare fuer dieselbe
Sache waeren dieselbe Doppelung wie zwei Wahrheiten im Verhalten.
"""

from __future__ import annotations

import contextlib
import json as json_modul
import logging
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated, Any

import typer

from . import db
from .client import OCRClient
from .config import Settings
from .db import DocumentStatus
from .errors import MineruOcrError, UnsupportedTypeError
from .wrapper import OCRMegaWrapper, OCRWrapper

app = typer.Typer(
    name="mineru-ocr",
    help="OCR-Dienst fuer MinerU auf RunPod: Dokumente rein, Text und Struktur raus.",
    no_args_is_help=True,
)


def _ausgeben(wert: Any, json_out: bool, text: str) -> None:
    """Eine Ausgabe, zwei Formen — und die Entscheidung faellt an EINER Stelle.

    Menschenlesbar ist die Voreinstellung; `--json` ist fuer den Fall, dass ein
    anderes Programm zuhoert. Beide zeigen DIESELBEN Daten: Der Text ist eine
    Darstellung des Werts und keine zweite, gekuerzte Auskunft.
    """
    if json_out:
        typer.echo(json_modul.dumps(wert, indent=2, ensure_ascii=False, default=str))
    else:
        typer.echo(text)


def _betriebsfehler(fehler: BaseException) -> None:
    """Meldet einen Betriebsfehler und beendet mit Code 1.

    Auf die FEHLERausgabe und nicht auf die Standardausgabe: Wer `mineru-ocr
    markdown … > datei.md` schreibt, soll die Meldung sehen und nicht in der
    Datei haben.
    """
    typer.echo(f"{type(fehler).__name__}: {fehler}", err=True)
    raise typer.Exit(code=1)


@contextlib.contextmanager
def _warnungen_auf_stderr() -> Iterator[None]:
    """Leitet die Warnungen des Pakets (Logger `mineru_ocr`) auf stderr.

    Die Bibliothek warnt ueber `logging` und kennt keinen Strom; die Kommandozeile
    entscheidet, wohin das geht, und zwar auf die FEHLERausgabe wie
    `_betriebsfehler`: Wer `--json` auswertet oder eine Ausgabe umleitet, hat die
    Warnung nicht in seinen Daten. Der Handler entsteht erst im Aufruf und nicht
    beim Import, weil er den Strom FESTHAELT, auf den `sys.stderr` gerade zeigt.
    """
    logger = logging.getLogger("mineru_ocr")
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(logging.WARNING)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    try:
        yield
    finally:
        logger.removeHandler(handler)


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
        db.setup(einstellung.db_dsn)
    except MineruOcrError as fehler:
        _betriebsfehler(fehler)
    typer.echo(f"Datenbank und Schema stehen: {einstellung.db_dsn}")


@app.command()
def daemon(
    dsn: Annotated[str | None, typer.Option(help="Verbindung; sonst die Konfiguration (MINERU_OCR_DB_DSN, config.toml).")] = None,
    concurrency: Annotated[int | None, typer.Option(help="Gleichzeitig betreute Dokumente; sonst 24.")] = None,
    queues: Annotated[list[str] | None, typer.Option(help="Nur diese Queues bedienen (Rueckfall auf zwei Prozesse).")] = None,
) -> None:
    """DER EXEKUTOR. Laeuft im Vordergrund, bis Strg-C oder SIGTERM kommt.

    Das ist der eigene Prozess, in dem die Arbeit stattfindet — der einzige, der
    mit RunPod spricht. Alles andere reiht nur ein und liest.

    Er laeuft im VORDERGRUND und gibt sein Log auf die Konsole. Kein Zweig in den
    Hintergrund, kein Autostart: Erst wird zugesehen, wie er sich im echten
    Bestandslauf verhaelt, bevor er ein Prozess wird, den man vergisst
    (Plan 5.3).

    Ohne `--queues` startet er zwei Worker in einem Prozess — OCR und Wartung
    getrennt, damit der Waechter nicht verhungert, wenn alle OCR-Plaetze belegt
    sind. Diese Konstruktion ist in der Procrastinate-Doku weder erlaubt noch
    verboten (Feature-Verifikation Punkt 1); traegt sie im Test nicht, ist
    `--queues ocr` in einem und `--queues maintenance` in einem zweiten Prozess
    der dokumentierte Rueckfall.
    """
    # Erst hier importiert und nicht oben: `daemon` zieht den ganzen
    # Verarbeitungsweg nach — Procrastinate-Worker, RunPod-Zugang, pypdfium2.
    # Wer `mineru-ocr status` aufruft, soll das nicht laden muessen.
    from . import daemon as daemon_modul

    try:
        einstellung = Settings.load(db_dsn=dsn, ocr_concurrency=concurrency)
    except MineruOcrError as fehler:
        _betriebsfehler(fehler)
    raise typer.Exit(code=daemon_modul.main(einstellung, queues=queues or None))


@app.command()
def ocr(
    paths: Annotated[list[Path], typer.Argument(help="Dateien oder Verzeichnisse.")],
    glob: Annotated[str, typer.Option(help="Welche Dateien in Verzeichnissen.")] = "**/*",
    start: Annotated[bool, typer.Option(help="Direkt einreihen (Voreinstellung) oder nur registrieren.")] = True,
    json_out: Annotated[bool, typer.Option("--json", help="Maschinenlesbar ausgeben.")] = False,
) -> None:
    """Registriert Dokumente und reiht sie ein. Blockiert nicht.

    Heisst wie die Python-Fassade (`OCRClient.ocr_path`), damit es nicht zwei
    Vokabulare fuer dieselbe Sache gibt.

    Verzeichnisse werden durchsucht; nicht unterstuetzte Dateien werden
    UEBERSPRUNGEN und gezaehlt, statt den Lauf abzubrechen. Ausdruecklich
    genannte Dateien dagegen werfen bei nicht unterstuetztem Typ — wer eine Datei
    beim Namen nennt, meint sie.

    Wiederholtes Aufrufen auf demselben Verzeichnis ist der vorgesehene Gebrauch,
    kein Missbrauch: Fertiges wird nicht angefasst, nur Fehlendes laeuft.

    OHNE LAUFENDEN DAEMON wird trotzdem eingereiht: Abschicken haengt nicht vom
    Daemon ab, die Jobs liegen in der Datenbank, bis einer startet. Das Kommando
    warnt dann auf stderr (einmal) und beendet mit 0. Den Daemon startet der
    Betreiber (`mineru-ocr daemon`), das Kommando tut es nicht. Frueher brach es
    hier ab und kannte dafuer `--strict`; beides entfaellt, siehe
    `OCRClient.ocr_start`.

    Gibt aus, wie viele Dokumente neu, wie viele bereits vorhanden, wie viele
    uebersprungen und wie viele eingereiht wurden.
    """
    try:
        with _warnungen_auf_stderr(), OCRClient(preflight=False) as klient:
            huellen: list[OCRWrapper] = []
            uebersprungen: dict[str, int] = {}

            for pfad in paths:
                if pfad.is_dir():
                    # Verzeichnis: ueberspringen und ZAEHLEN.
                    sammlung = OCRMegaWrapper.from_dir(
                        pfad, klient.conn, klient.settings, glob=glob
                    )
                    huellen.extend(sammlung)
                    for grund, anzahl in sammlung.stats()["skipped"].items():
                        uebersprungen[grund] = uebersprungen.get(grund, 0) + anzahl
                else:
                    # Ausdruecklich genannte Datei: WIRFT bei falschem Typ.
                    # Wer eine Datei beim Namen nennt, meint sie.
                    huellen.append(
                        OCRWrapper.from_path(pfad, klient.conn, klient.settings)
                    )

            offen = [h for h in huellen if not h.status.is_terminal]
            fertig = len(huellen) - len(offen)
            eingereiht = 0
            if start and offen:
                for huelle in offen:
                    klient.ocr_start(huelle)
                eingereiht = len(offen)

            ergebnis = {
                "documents": len(huellen),
                "already_terminal": fertig,
                "queued": eingereiht,
                "skipped": uebersprungen,
            }
    except UnsupportedTypeError as fehler:
        _betriebsfehler(fehler)
    except MineruOcrError as fehler:
        _betriebsfehler(fehler)

    _ausgeben(
        ergebnis,
        json_out,
        f"{ergebnis['documents']} Dokumente, davon {fertig} bereits fertig; "
        f"{eingereiht} eingereiht"
        + (f"; uebersprungen: {uebersprungen}" if uebersprungen else ""),
    )


@app.command()
def status(
    json_out: Annotated[bool, typer.Option("--json", help="Maschinenlesbar ausgeben.")] = False,
) -> None:
    """Zeigt den Gesamtzustand: Endzustaende, Wartendes, Laufendes, Daemon, Guthaben.

    Alles davon ist abgeleitet und nirgends gespeichert. Weist ausserdem offene
    Aufruest-Faelle aus — das ist der Weg, auf dem ein zu grosses Dokument zu
    einem Aufgaben-Eintrag wird: Der Bericht zeigt ihn, der Mensch legt ihn an.

    Kontostand und Worker-Liste stecken hier mit drin und haben KEINE eigenen
    Kommandos. Sie sind Teil der Antwort auf „wie steht es gerade?", und wer sie
    einzeln braucht, nimmt den Python-Weg (`client.credit()`, `client.workers()`).
    """
    try:
        with OCRClient(preflight=False) as klient:
            uebersicht = klient.status()
    except MineruOcrError as fehler:
        _betriebsfehler(fehler)

    zeilen = [
        "Dokumente:  "
        + "  ".join(
            f"{name}={anzahl}" for name, anzahl in uebersicht["documents"].items()
        ),
        "Jobs:       "
        + "  ".join(f"{name}={anzahl}" for name, anzahl in uebersicht["jobs"].items()),
        f"Daemon:     {'laeuft' if uebersicht['daemon_alive'] else 'LAEUFT NICHT'}",
        f"Guthaben:   {uebersicht['balance'] if uebersicht['balance'] is not None else 'unbekannt'}",
    ]
    if uebersicht["upgrade_cases"]:
        # Der Weg, auf dem ein zu grosses Dokument zu einem Aufgaben-Eintrag
        # wird: Der Bericht zeigt ihn, der Mensch legt ihn an.
        zeilen.append(
            f"OFFENE AUFRUEST-FAELLE: {uebersicht['upgrade_cases']} — bitte je "
            f"einen Aufgaben-Eintrag anlegen (`mineru-ocr show <hash>` zeigt den "
            f"Hinweis)."
        )
    if not uebersicht["daemon_alive"]:
        zeilen.append(
            "Es laeuft kein Daemon — eingereihte Jobs bleiben liegen. "
            "Starten mit `mineru-ocr daemon`."
        )
    _ausgeben(uebersicht, json_out, "\n".join(zeilen))


@app.command()
def show(
    target: Annotated[str, typer.Argument(help="Hash oder Pfad.")],
    json_out: Annotated[bool, typer.Option("--json", help="Maschinenlesbar ausgeben.")] = False,
) -> None:
    """Zeigt ein Dokument: Angaben, Zustand und den juengsten Job dazu.

    Der Job-Teil ist die Antwort auf „warum passiert nichts?" — er zeigt, ob das
    Dokument wartet, laeuft, wie oft es schon versucht wurde, oder ob sein
    juengster Job endete, ohne etwas zu schreiben.
    """
    try:
        with OCRClient(preflight=False) as klient:
            huelle = klient.get(target)
            abgeleitet = klient.document(target)
            angaben = huelle.to_dict()
            angaben["phase"] = abgeleitet.phase.value
            angaben["job"] = {
                "id": abgeleitet.job_id,
                "status": abgeleitet.job_status,
                "attempts": abgeleitet.attempts,
                "count": abgeleitet.job_count,
            }
    except MineruOcrError as fehler:
        _betriebsfehler(fehler)

    zeilen = [
        f"{angaben['sha256']}",
        f"  Typ:       {angaben['mime']}  {angaben['size_bytes']} Byte"
        + (f"  {angaben['page_count']} Seiten" if angaben["page_count"] else ""),
        f"  Zustand:   {angaben['status']}   Phase: {angaben['phase']}",
        f"  Job:       {angaben['job']}",
    ]
    if angaben["error"]:
        zeilen.append(f"  Fehler:    {angaben['error']}")
    if angaben["image_tag_at_submit"]:
        zeilen.append(f"  Image:     {angaben['image_tag_at_submit']}")
    for vermerk in angaben["notes"]:
        zeilen.append(f"  Vermerk:   {vermerk}")
    _ausgeben(angaben, json_out, "\n".join(zeilen))


@app.command()
def wait(
    target: Annotated[str, typer.Argument(help="Hash oder Pfad.")],
    timeout: Annotated[float | None, typer.Option(help="Sekunden; ohne Angabe unbegrenzt.")] = None,
) -> None:
    """Wartet, bis ein Dokument endgueltig ist.

    Exit-Code 0 bei `done` und `empty`, 3 bei `failed` und `input_error`,
    1 bei Ablauf der Wartezeit.
    """
    try:
        with OCRClient(preflight=False) as klient:
            huelle = klient.wait(klient.get(target).sha256, timeout=timeout)
    except TimeoutError as fehler:
        _betriebsfehler(fehler)
    except MineruOcrError as fehler:
        _betriebsfehler(fehler)

    typer.echo(f"{huelle.sha256}: {huelle.status.value}")
    if not huelle.success:
        # Ein eigener Code, weil das weder ein Erfolg noch ein Fehler DES
        # WERKZEUGS ist: Das Werkzeug hat einwandfrei gearbeitet, das Ergebnis
        # ist nur schlecht. Wer beides auf 1 wirft, kann in einer Schleife nicht
        # mehr zwischen „kaputtes Dokument" und „kaputte Einrichtung"
        # unterscheiden.
        raise typer.Exit(code=3)


@app.command()
def markdown(
    target: Annotated[str, typer.Argument(help="Hash oder Pfad.")],
    out: Annotated[Path | None, typer.Option(help="Zieldatei; ohne Angabe auf die Standardausgabe.")] = None,
) -> None:
    """Schreibt das GANZE Dokument in Markdown-Form heraus.

    Ausgegeben wird `OCRWrapper.rendered_markdown` — also alles, was auf dem
    Dokument steht, einschliesslich der Randbloecke, die MinerU aus seinem
    eigenen Markdown herausfiltert und in denen das Aktenzeichen liegt. Wer das
    nackte Engine-Markdown will, nimmt den Python-Weg (`huelle["markdown"]`).

    Nur der Text. Strukturliste, Bilder und die Rohantworten gibt es ueber den
    Python-Weg (`huelle["content_list"]`, `huelle.images`, `huelle.raw`) — sie
    auch noch ueber die Kommandozeile anzubieten hiesse, die Zugangsschicht ein
    zweites Mal zu bauen, und Binaerdaten haben auf der Standardausgabe ohnehin
    nichts zu suchen.

    Ist das Dokument nicht erfolgreich terminal, wird NICHTS ausgegeben und der
    Exit-Code ist 3 (`raise typer.Exit(code=3)`). Einen halben Text auszugeben
    waere schlimmer als keinen: Er saehe vollstaendig aus.
    """
    try:
        with OCRClient(preflight=False) as klient:
            huelle = klient.get(target)
            if not huelle.success:
                typer.echo(
                    f"{huelle.sha256} steht auf {huelle.status.value} — "
                    f"es gibt keinen vollstaendigen Text. Was ankam, zeigt "
                    f"`mineru-ocr show {target}`.",
                    err=True,
                )
                raise typer.Exit(code=3)
            text = huelle.rendered_markdown
    except MineruOcrError as fehler:
        _betriebsfehler(fehler)

    if out is not None:
        out.write_text(text, encoding="utf-8")
        typer.echo(f"{len(text)} Zeichen nach {out} geschrieben.", err=True)
    else:
        typer.echo(text)


@app.command()
def retry(
    target: Annotated[str | None, typer.Argument(help="Hash oder Pfad; ohne Angabe siehe --failed.")] = None,
    failed: Annotated[bool, typer.Option(help="Alle gescheiterten Dokumente erneut vorlegen.")] = False,
    yes: Annotated[bool, typer.Option("--yes", help="Ohne Rueckfrage ausfuehren.")] = False,
) -> None:
    """Legt gescheiterte Dokumente erneut vor. KOSTET GELD.

    Durchgriff auf Procrastinates eigenes Werkzeug. Abgebrochene Jobs (`aborted`,
    etwa vom Herunterfahren) nimmt es NICHT an — die werden uebersprungen und
    gemeldet; ihr Weg ist `ocr` (erneut einreihen).

    Jedes wieder vorgelegte Dokument wird vollstaendig neu verarbeitet und neu
    bezahlt. Das Kommando sagt vorher, um wie viele es geht, und fragt nach —
    ausser mit `--yes`.
    """
    if target is None and not failed:
        typer.echo(
            "Entweder ein Dokument nennen oder `--failed` fuer alle "
            "gescheiterten. Ohne beides waere nicht klar, was gemeint ist.",
            err=True,
        )
        raise typer.Exit(code=2)

    try:
        with OCRClient(preflight=False) as klient:
            if target is not None:
                betroffene: list[OCRWrapper] = [klient.get(target)]
            else:
                betroffene = [
                    OCRWrapper.from_row(zeile, klient.conn)
                    for zeile in db.list_documents(
                        klient.conn, status=DocumentStatus.FAILED
                    )
                ]

            zu_tun = [h for h in betroffene if h.status is DocumentStatus.FAILED]
            if not zu_tun:
                typer.echo("Nichts zu wiederholen.")
                return

            # VOR der Ausgabe von Geld wird gefragt. Jedes wieder vorgelegte
            # Dokument wird vollstaendig neu verarbeitet und neu bezahlt.
            typer.echo(
                f"{len(zu_tun)} Dokument(e) werden erneut vorgelegt und dabei "
                f"VOLLSTAENDIG NEU VERARBEITET — das kostet Geld."
            )
            if not yes and not typer.confirm("Fortfahren?"):
                raise typer.Exit(code=0)

            anzahl = klient.retry_failed(
                zu_tun[0] if len(zu_tun) == 1 else _als_sammlung(zu_tun, klient)
            )
    except MineruOcrError as fehler:
        _betriebsfehler(fehler)

    typer.echo(f"{anzahl} von {len(zu_tun)} Job(s) wieder vorgelegt.")
    if anzahl < len(zu_tun):
        # Ein `aborted`-Job wird vom Werkzeug abgewiesen; sein Weg ist das
        # erneute Einreihen. Das wird GESAGT und nicht stillschweigend
        # weggezaehlt.
        typer.echo(
            f"{len(zu_tun) - anzahl} uebersprungen — vermutlich abgebrochene "
            f"Jobs (`aborted`). Fuer die ist der Weg `mineru-ocr ocr <pfad>`, "
            f"nicht `retry`.",
            err=True,
        )


def _als_sammlung(huellen: list[OCRWrapper], klient: OCRClient) -> OCRMegaWrapper:
    """Fasst schon gelesene Huellen zu einer Sammlung zusammen, ohne neu zu hashen.

    `OCRMegaWrapper.from_paths` liest jede Datei — hier sind die Zeilen aber
    laengst da, und die Datei kann sogar weg sein. Deshalb wird die Sammlung
    unmittelbar gefuellt.
    """
    sammlung = OCRMegaWrapper.__new__(OCRMegaWrapper)
    sammlung._conn = klient.conn
    sammlung._settings = klient.settings
    sammlung._huellen = {h.sha256: h for h in huellen}
    sammlung._uebersprungen = {}
    return sammlung


@app.command()
def preflight(
    strict: Annotated[bool, typer.Option(help="Abweichungen als Fehler behandeln.")] = True,
    json_out: Annotated[bool, typer.Option("--json", help="Maschinenlesbar ausgeben.")] = False,
) -> None:
    """Prueft Endpoint, Konto und Daemon. VERAENDERT NICHTS.

    Es gibt in diesem Werkzeug kein Kommando, das eine bezahlte Ressource anlegt
    oder aendert — nicht abgeschaltet, sondern nicht vorhanden.

    `--strict` ist AN, wie in Python (`OCRClient(strict=True)`). Waere es hier
    aus, verhielte sich dieselbe Pruefung ueber die Kommandozeile nachsichtiger
    als aus Python heraus — genau die zwei Wahrheiten, die der Vorspann dieser
    Datei ausschliesst. Mit `--no-strict` wird dieselbe Abweichung zur Warnung.

    Exit-Code 1 bei einer Abweichung unter `--strict`, sonst 0 mit Warnungen im
    Bericht.
    """
    try:
        with OCRClient(preflight=False, strict=strict) as klient:
            befund = klient.preflight()
    except MineruOcrError as fehler:
        _betriebsfehler(fehler)

    zeilen = [
        f"{eintrag['bewertung']:<14} {eintrag['punkt']:<20} "
        f"soll={eintrag['soll']!r}  ist={eintrag['ist']!r}"
        for eintrag in befund["checks"]
    ]
    _ausgeben(befund, json_out, "\n".join(zeilen))


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
