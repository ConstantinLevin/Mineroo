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
       Auch bei `ocr`, wenn eine ausdruecklich genannte Datei nicht abgeschickt
       werden konnte (nicht unterstuetzt, zu gross, nicht vorhanden): Die uebrigen
       sind dann trotzdem eingereiht.
    2  Aufruffehler — falsche Argumente oder Schalter; auch ein Pfad, den typer
       schon beim Entgegennehmen ablehnt (etwa eine Datei ohne Leserecht, dann
       wird NICHTS eingereiht). Setzt typer selbst.
    3  Nur bei `wait` und `markdown`: Ein Dokument ist endgueltig, aber NICHT
       erfolgreich (`failed` oder `input_error`). Ein eigener Code, weil das
       weder ein Erfolg noch ein Fehler DES WERKZEUGS ist — das Werkzeug hat
       einwandfrei gearbeitet, das Ergebnis ist nur schlecht. Wer beides auf 1
       wirft, kann in einer Schleife nicht mehr zwischen „kaputtes Dokument" und
       „kaputte Einrichtung" unterscheiden.
    4  Nur bei `wait`: Nach Ablauf der Frist ist noch ein Dokument offen. Der
       Aufrufer wartet spaeter erneut; die Dokumente laufen weiter.
    130 Strg-C (setzt typer). Beendet nur das Warten bzw. den Lauf.

Treffen bei `wait` mehrere zu, gilt der schwerste: 1 vor 4 vor 3 vor 0.

Gesetzt werden sie mit `raise typer.Exit(code=…)`.

Jedes Kommando, das etwas ausgibt, kennt `--json`. Menschenlesbar ist die
Voreinstellung; `--json` ist fuer den Fall, dass ein anderes Programm zuhoert.

Die Namen sind die der Python-Flaeche: `ocr` reiht ein wie `OCRClient.ocr_start`,
`status` und `wait` heissen wie ihre Methoden. Zwei Vokabulare fuer dieselbe
Sache waeren dieselbe Doppelung wie zwei Wahrheiten im Verhalten.

PFADE KENNT NUR DIESE DATEI. Die Bibliothek nimmt Bytes und gibt Hashes; die
Kommandozeile liest Dateien und Ordner, reicht deren Bytes herein und nennt in
ihrer Ausgabe die Zuordnung von Pfad zu Hash. Ziele fuer `show`, `wait`,
`markdown` und `retry` sind Hashes oder Pfade, die sie selbst hasht.

Auf der Standardausgabe stehen nur DATEN. Meldungen und Warnungen — auch die der
Bibliothek, etwa dass kein Daemon laeuft — gehen auf die Fehlerausgabe.
"""

from __future__ import annotations

import hashlib
import json as json_modul
import logging
import re
from pathlib import Path
from typing import Annotated, Any, Final

import typer

from . import core, db, mime
from .client import OCRClient
from .config import Settings
from .core import RetryOutcome
from .errors import (
    DocumentNotFound,
    MineruOcrError,
    TooLargeError,
    UnsupportedTypeError,
    WaitTimeoutError,
)

app = typer.Typer(
    name="mineru-ocr",
    help="OCR-Dienst fuer MinerU auf RunPod: Dokumente rein, Text und Struktur raus.",
    no_args_is_help=True,
)

HASH_FORM: Final = re.compile(r"^[0-9a-f]{64}$")
"""Woran die Kommandozeile einen Hash von einem Pfad unterscheidet.

Festgelegt und nicht geraten: vierundsechzig Zeichen aus `[0-9a-f]` sind ein Hash,
alles andere ist ein Pfad. Die Regel gilt in `show`, `wait`, `markdown` und
`retry` und steht deshalb EINMAL hier."""

_LOG_NAME: Final = "mineru_ocr"


class _FehlerAusgabe(logging.Handler):
    """Schreibt Log-Meldungen der Bibliothek auf die Fehlerausgabe des Kommandos.

    Ueber `typer.echo(err=True)` und nicht ueber einen `StreamHandler`, der seinen
    Strom beim Anlegen festhielte: Der Strom wechselt mit jedem Aufruf (im Test mit
    jedem `invoke`), und ein festgehaltener zeigte auf einen geschlossenen.
    """

    def emit(self, record: logging.LogRecord) -> None:
        typer.echo(self.format(record), err=True)


@app.callback()
def _vorweg() -> None:
    """OCR-Dienst fuer MinerU auf RunPod: Dokumente rein, Text und Struktur raus."""
    # Meldungen und Warnungen der Bibliothek gehen auf stderr (nie auf stdout).
    # Einmal anmelden, nicht bei jedem Aufruf.
    logger = logging.getLogger(_LOG_NAME)
    if not any(isinstance(h, _FehlerAusgabe) for h in logger.handlers):
        logger.addHandler(_FehlerAusgabe())
    logger.setLevel(logging.WARNING)


def _ziel(target: str) -> str:
    """Der Hash zu einem Ziel: ein Hash bleibt ein Hash, ein Pfad wird gehasht.

    Die Bibliothek kennt keine Pfade; hier ist die einzige Stelle, die einen in
    einen Hash verwandelt. Gehasht wird in Stuecken (`hashlib.file_digest`),
    mit demselben Ergebnis wie `core.sha256_of` auf den ganzen Bytes.

    Raises:
        DocumentNotFound: Weder ein Hash noch eine lesbare Datei.
    """
    if HASH_FORM.match(target):
        return target
    pfad = Path(target)
    if not pfad.is_file():
        raise DocumentNotFound(
            f"Kein Dokument zu {target!r}: Es ist weder ein sha256 noch eine "
            f"lesbare Datei."
        )
    # In Stuecken und nicht ueber `read_bytes`: Eine grosse Datei soll nicht ganz in
    # den Speicher, nur um einen Hash zu bilden. Derselbe Hash wie `core.sha256_of`.
    try:
        with pfad.open("rb") as datei:
            return hashlib.file_digest(datei, "sha256").hexdigest()
    except OSError as fehler:
        # Eine Datei, die da ist, aber nicht lesbar (kein Leserecht), ist fuer das
        # Ziel dasselbe wie keine: ein Betriebsfehler mit Auskunft, kein Traceback.
        raise DocumentNotFound(
            f"Kein Dokument zu {target!r}: Die Datei ist nicht lesbar "
            f"({fehler.strerror or fehler})."
        ) from fehler


def _lesen(datei: Path) -> bytes:
    """Liest eine Datei, nachdem sie die Vorpruefung bestanden hat.

    Die Vorpruefung steht VOR dem Lesen, weil eine grosse Datei fremden Typs nicht
    erst ganz in den Speicher soll, um dann abgewiesen zu werden: Der Typ wird am
    Anfang der Datei erkannt (bei einem ZIP am Verzeichnis), die Groesse am
    Dateisystem. Die endgueltige Pruefung macht `ocr_start` an den Bytes.

    Raises:
        UnsupportedTypeError: Der Typ wird nicht unterstuetzt.
        TooLargeError: Die Datei passt nicht in die Datenbank.
        OSError: Nicht lesbar.
    """
    with datei.open("rb") as geoeffnet:
        mime.sniff_file(geoeffnet)
    core.check_size(datei.stat().st_size, datei.name)
    return datei.read_bytes()


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


def _daemon_zeile(lebt: bool | None) -> str:
    """Die Daemon-Angabe von `status`; `None` heisst „nicht lesbar“, nicht „aus“."""
    if lebt is None:
        return "unbekannt"
    return "laeuft" if lebt else "LAEUFT NICHT"


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
        # Der Daemon spricht RunPod an: Endpoint und Schluessel VOR dem Start, nicht
        # erst beim ersten Job.
        einstellung.require_endpoint_id()
        einstellung.require_api_key()
    except MineruOcrError as fehler:
        _betriebsfehler(fehler)
    raise typer.Exit(code=daemon_modul.main(einstellung, queues=queues or None))


@app.command()
def ocr(
    paths: Annotated[list[Path], typer.Argument(help="Dateien oder Verzeichnisse.")],
    glob: Annotated[str, typer.Option(help="Welche Dateien in Verzeichnissen.")] = "**/*",
    json_out: Annotated[bool, typer.Option("--json", help="Maschinenlesbar ausgeben.")] = False,
) -> None:
    """Liest Dateien, schickt sie ab und kehrt sofort zurueck. Blockiert nicht.

    Die Kommandozeile ist die einzige, die Pfade kennt: Sie liest die Dateien (in
    Verzeichnissen ueber ein Muster), hasht sie und nennt in der Ausgabe je Datei
    Pfad, Hash, Zustand und ob dieser Aufruf sie eingereiht hat. Mit `--json` sind
    das die `documents`, dazu die `skipped`: die uebersprungenen Dateien samt Grund.
    Ruft je Datei `OCRClient.ocr_start`.

    In Verzeichnissen werden nicht unterstuetzte und zu grosse Dateien
    UEBERSPRUNGEN und gezaehlt, statt den Lauf abzubrechen. Eine ausdruecklich
    genannte Datei dagegen wird mit Grund gemeldet — wer sie beim Namen nennt,
    meint sie —, die uebrigen werden trotzdem eingereiht, und das Kommando endet
    mit Exit-Code 1.

    Wiederholtes Aufrufen auf demselben Verzeichnis ist der vorgesehene Gebrauch,
    kein Missbrauch: Ein Dokument, zu dem es ein Ergebnis gibt (auch `failed`), oder
    das schon unterwegs ist, wird nicht angefasst.

    Bricht der Lauf mittendrin ab, steht auf stdout, was bis dahin eingereiht ist.
    Bei einem Betriebsfehler (Datenbank weg) endet das Kommando mit Exit-Code 1, und
    die Meldung des Abbruchs steht auf stderr; Exit-Code 1 mit Ausgabe heisst also
    nicht immer „einzelne Dateien uebersprungen, der Rest eingereiht". Bei Strg-C
    endet es, wie typer es fuer einen Abbruch vorsieht, mit Exit-Code 130.

    Laeuft kein Daemon, wird trotzdem eingereiht: Abschicken haengt nicht vom
    Daemon ab, die Jobs liegen in der Datenbank, bis einer startet. Die Warnung
    steht auf stderr (einmal), und das Kommando endet deshalb nicht mit einem
    Fehler. Den Daemon startet der Betreiber (`mineru-ocr daemon`), das Kommando
    tut es nicht.
    """
    dokumente: list[dict[str, Any]] = []
    uebersprungen: list[dict[str, str]] = []
    ausdruecklich_fehlgeschlagen = False
    betriebsfehler: MineruOcrError | None = None

    try:
        with OCRClient(preflight=False) as klient:
            for pfad in paths:
                ausdruecklich = not pfad.is_dir()
                kandidaten = (
                    [pfad]
                    if ausdruecklich
                    else [d for d in sorted(pfad.glob(glob)) if d.is_file()]
                )
                for datei in kandidaten:
                    try:
                        gereicht = klient.ocr_start(_lesen(datei))
                    except (UnsupportedTypeError, TooLargeError, OSError) as fehler:
                        grund = _grund(fehler)
                        uebersprungen.append(
                            {"path": str(datei), "reason": grund, "detail": str(fehler)}
                        )
                        if ausdruecklich:
                            # Eine ausdruecklich genannte Datei wird MIT GRUND
                            # gemeldet; die uebrigen laufen weiter.
                            typer.echo(f"{datei}: {fehler}", err=True)
                            ausdruecklich_fehlgeschlagen = True
                        continue
                    dokumente.append(
                        {
                            "path": str(datei),
                            "sha256": str(gereicht),
                            # Offen (eingereiht oder schon unterwegs) hat keinen
                            # Endzustand: `null`, kein erfundenes Wort.
                            "status": gereicht.status.value
                            if gereicht.status is not None
                            else None,
                            "queued": gereicht.queued,
                        }
                    )
    except MineruOcrError as fehler:
        betriebsfehler = fehler
    finally:
        # AUCH bei einem Abbruch mitten im Lauf (Betriebsfehler, Strg-C): Was
        # bis dahin eingereiht ist, steht in der Datenbank, und die Zuordnung von
        # Pfad zu Hash ist der Zweck dieser Ausgabe.
        if betriebsfehler is None or dokumente or uebersprungen:
            _ocr_ausgeben(dokumente, uebersprungen, json_out)
    if betriebsfehler is not None:
        _betriebsfehler(betriebsfehler)
    if ausdruecklich_fehlgeschlagen:
        raise typer.Exit(code=1)


def _ocr_ausgeben(
    dokumente: list[dict[str, Any]],
    uebersprungen: list[dict[str, str]],
    json_out: bool,
) -> None:
    """Die Ausgabe von `ocr`: Daten auf stdout, die Zaehlzeile auf stderr."""
    if json_out:
        typer.echo(
            json_modul.dumps(
                {"documents": dokumente, "skipped": uebersprungen},
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        for eintrag in dokumente:
            typer.echo(
                f"{eintrag['sha256']}  {eintrag['status'] or 'offen':<12} "
                f"{'eingereiht' if eintrag['queued'] else 'nicht eingereiht':<16} "
                f"{eintrag['path']}"
            )
    eingereiht = sum(1 for e in dokumente if e["queued"])
    gruende: dict[str, int] = {}
    for eintrag in uebersprungen:
        gruende[eintrag["reason"]] = gruende.get(eintrag["reason"], 0) + 1
    typer.echo(
        f"{len(dokumente)} Dokumente, davon {eingereiht} eingereiht"
        + (
            f"; {len(uebersprungen)} uebersprungen ("
            + ", ".join(f"{grund}: {anzahl}" for grund, anzahl in sorted(gruende.items()))
            + ")"
            if uebersprungen
            else ""
        ),
        err=True,
    )


def _grund(fehler: BaseException) -> str:
    """Der Grund einer uebersprungenen Datei, als Schluessel fuer Zaehler und JSON."""
    if isinstance(fehler, UnsupportedTypeError):
        return "nicht_unterstuetzter_typ"
    if isinstance(fehler, TooLargeError):
        return "zu_gross"
    return "nicht_lesbar"


@app.command()
def status(
    json_out: Annotated[bool, typer.Option("--json", help="Maschinenlesbar ausgeben.")] = False,
) -> None:
    """Zeigt den Gesamtzustand: Endzustaende, Offenes, Jobs, Daemon, Guthaben.

    Alles davon ist abgeleitet und nirgends gespeichert. Offen (`open`) sind die
    Dokumente, deren Eingabe steht und die noch kein Ergebnis haben; wie viele
    Jobs warten oder laufen, zeigt die Zeile darunter. Weist ausserdem offene
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
        f"Daemon:     {_daemon_zeile(uebersicht['daemon_alive'])}",
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
    if uebersicht["daemon_alive"] is False:
        zeilen.append(
            "Es laeuft kein Daemon — eingereihte Jobs bleiben liegen. "
            "Starten mit `mineru-ocr daemon`."
        )
    _ausgeben(uebersicht, json_out, "\n".join(zeilen))


@app.command()
def show(
    target: Annotated[str, typer.Argument(help="Hash oder Pfad.")],
    json_out: Annotated[bool, typer.Option("--json", help="Maschinenlesbar ausgeben.")] = False,
    raw: Annotated[bool, typer.Option("--raw", help="Die vollstaendige Antwort als JSON ausgeben.")] = False,
) -> None:
    """Zeigt, wo ein Dokument steht: Endzustand, oder offen samt seinem Job.

    Ruft `OCRClient.document` und laedt dabei `raw` nicht. Der Job-Teil ist die
    Antwort auf „warum passiert nichts?" — er zeigt, ob ein offenes Dokument
    wartet, laeuft, wie oft es schon versucht wurde, oder ob sein Job (der,
    dessen Kennung an der Eingabe steht) endete, ohne ein Ergebnis zu schreiben
    (dann hilft `retry`).

    `--raw` gibt die vollstaendige Antwort aus (`OCRClient.get(...).to_dict(
    raw=True)`), immer als JSON: die Angaben und unter `raw` alles, was MinerU
    lieferte. Ein offenes Dokument hat noch keine; das Kommando sagt es auf
    stderr und endet mit Exit-Code 1. Ein unbekannter Hash endet ebenso.
    """
    if raw:
        try:
            with OCRClient(preflight=False) as klient:
                vollstaendig = klient.get(_ziel(target)).to_dict(raw=True)
        except MineruOcrError as fehler:
            _betriebsfehler(fehler)
        _ausgeben(vollstaendig, True, "")
        return

    try:
        with OCRClient(preflight=False) as klient:
            angaben = klient.document(_ziel(target)).to_dict()
    except MineruOcrError as fehler:
        _betriebsfehler(fehler)

    zeilen = [
        f"{angaben['sha256']}",
        f"  Zustand:   {angaben['status'] or 'offen'}   Phase: {angaben['phase']}",
        f"  Job:       {angaben['job']}",
    ]
    if angaben["status"] is not None:
        # Ein Ergebnis: seine Angaben. Ein offenes Dokument hat keine Zeile.
        zeilen.insert(
            1,
            f"  Typ:       {angaben['mime'] or 'unbekannt'}  "
            f"{angaben['size_bytes'] if angaben['size_bytes'] is not None else '?'} Byte"
            + (f"  {angaben['page_count']} Seiten" if angaben["page_count"] else ""),
        )
        if angaben["error"]:
            zeilen.append(f"  Fehler:    {angaben['error']}")
        if angaben["image_tag_at_submit"]:
            zeilen.append(f"  Image:     {angaben['image_tag_at_submit']}")
        for vermerk in angaben["notes"]:
            zeilen.append(f"  Vermerk:   {vermerk}")
    _ausgeben(angaben, json_out, "\n".join(zeilen))


@app.command()
def wait(
    targets: Annotated[list[str], typer.Argument(help="Hashes oder Pfade.")],
    timeout: Annotated[float | None, typer.Option(help="Sekunden fuer alle zusammen; ohne Angabe unbegrenzt.")] = None,
    json_out: Annotated[bool, typer.Option("--json", help="Je Dokument eine JSON-Zeile.")] = False,
) -> None:
    """Wartet, bis die Dokumente endgueltig sind, und nennt jedes, sobald es das ist.

    Ruft `OCRClient.as_completed`: Je Dokument steht eine Zeile auf stdout, sobald
    es endgueltig ist (`HASH: ZUSTAND`, mit `--json` eine JSON-Zeile mit seinen
    Angaben, ohne `raw`). Ein gescheitertes beendet das Warten auf die uebrigen
    nicht. Die Frist gilt fuer alle zusammen; nach ihrem Ablauf nennt stderr die
    noch offenen. Frist und Strg-C beenden nur das Warten, kein Job wird
    angefasst.

    Exit-Code: 0, wenn alle `done` oder `empty` sind; 3, wenn eines `failed` oder
    `input_error` ist; 4, wenn nach Ablauf der Frist noch eines offen ist; 1 bei
    einem Betriebsfehler (auch ein unbekannter Hash, sofort gemeldet); 130 bei
    Strg-C. Bei mehreren gilt der schwerste, 1 vor 4 vor 3 vor 0.
    """
    code = 0
    try:
        with OCRClient(preflight=False) as klient:
            hashes = [_ziel(target) for target in targets]
            try:
                for huelle in klient.as_completed(hashes, timeout=timeout):
                    if json_out:
                        typer.echo(
                            json_modul.dumps(huelle.to_dict(), ensure_ascii=False, default=str)
                        )
                    else:
                        typer.echo(f"{huelle.sha256}: {huelle.status.value}")
                    if not huelle.success:
                        # Weder ein Erfolg noch ein Fehler DES WERKZEUGS: Es hat
                        # einwandfrei gearbeitet, das Ergebnis ist nur schlecht.
                        code = 3
            except WaitTimeoutError as fehler:
                # 4 vor 3: Es verlangt eine Handlung (spaeter erneut warten); die
                # Fehlschlaege stehen ohnehin in den Zeilen.
                typer.echo(
                    f"Nach Ablauf der Frist ({timeout} s) noch offen "
                    f"({len(fehler.pending)}): "
                    + " ".join(fehler.pending),
                    err=True,
                )
                code = 4
    except MineruOcrError as fehler:
        _betriebsfehler(fehler)
    if code:
        raise typer.Exit(code=code)


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
            huelle = klient.get(_ziel(target))
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


_AUSGANG: Final = {
    RetryOutcome.RESET: "zurueckgesetzt",
    RetryOutcome.REQUEUED: "neu eingereiht",
    RetryOutcome.UNDERWAY: "unterwegs",
    RetryOutcome.HAS_RESULT: "mit Ergebnis",
}
"""Wie `retry` seine Ausgaenge nennt, in dieser Reihenfolge auch in der Summe."""


@app.command()
def retry(
    target: Annotated[str | None, typer.Argument(help="Hash oder Pfad; ohne Angabe siehe --all.")] = None,
    all_: Annotated[bool, typer.Option("--all", help="Jedes Dokument ohne gueltiges Ergebnis.")] = False,
    yes: Annotated[bool, typer.Option("--yes", help="Ohne Rueckfrage ausfuehren.")] = False,
) -> None:
    """Bringt Dokumente ohne gueltiges Ergebnis wieder in Gang. KOSTET GELD.

    Mit einem Ziel ruft es `OCRClient.retry`, mit `--all` `OCRClient.retry_all`,
    das selbst auswaehlt: jedes Dokument ohne gueltiges Ergebnis. Je Dokument:
    ein gueltiges Ergebnis oder ein wartender oder laufender Job bleibt, wie er
    ist; ein gescheiterter Job wird zurueckgesetzt; fuer einen abgebrochenen,
    abgesagten oder fehlenden wird ein neuer eingereiht.

    Jedes zurueckgesetzte oder neu eingereihte Dokument wird vollstaendig neu
    verarbeitet und neu bezahlt. Das Kommando fragt deshalb vorher nach — ausser
    mit `--yes` — und nennt danach je Dokument und in Summe, was geschah.
    """
    if (target is None) == (not all_):
        typer.echo(
            "Entweder ein Dokument nennen oder `--all` fuer jedes ohne gueltiges "
            "Ergebnis. Mit keinem oder beidem waere nicht klar, was gemeint ist.",
            err=True,
        )
        raise typer.Exit(code=2)

    try:
        sha256 = _ziel(target) if target is not None else None
        # VOR der Ausgabe von Geld wird gefragt, ohne Anzahl: Was je Dokument
        # geschieht, entscheidet erst die Bibliothek, in dem einen Aufruf.
        gemeint = sha256 if sha256 is not None else "Jedes Dokument ohne gueltiges Ergebnis"
        typer.echo(
            f"{gemeint}: Ohne gueltiges Ergebnis und ohne laufenden Job wird es "
            f"VOLLSTAENDIG NEU VERARBEITET — das kostet Geld.",
            err=True,
        )
        if not yes and not typer.confirm("Fortfahren?", err=True):
            raise typer.Exit(code=0)
        with OCRClient(preflight=False) as klient:
            if sha256 is not None:
                ausgaenge = {sha256: klient.retry(sha256)}
            else:
                ausgaenge = klient.retry_all()
    except MineruOcrError as fehler:
        _betriebsfehler(fehler)

    if not ausgaenge:
        typer.echo("Nichts zu wiederholen.", err=True)
        return
    for sha, ausgang in ausgaenge.items():
        typer.echo(f"{sha} {_AUSGANG[ausgang]}")
    anzahl = {ausgang: 0 for ausgang in _AUSGANG}
    for ausgang in ausgaenge.values():
        anzahl[ausgang] += 1
    typer.echo(
        ", ".join(f"{anzahl[ausgang]} {name}" for ausgang, name in _AUSGANG.items()),
        err=True,
    )


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
        # Ohne Schemapruefung: `preflight` liest weder `documents` noch `inputs`
        # und darf jederzeit laufen, auch vor `setup`.
        with OCRClient(preflight=False, strict=strict, schema_check=False) as klient:
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
