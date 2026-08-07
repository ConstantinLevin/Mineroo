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

from pathlib import Path
from typing import Annotated

import typer

app = typer.Typer(
    name="mineru-ocr",
    help="OCR-Dienst fuer MinerU auf RunPod: Dokumente rein, Text und Struktur raus.",
    no_args_is_help=True,
)


@app.command()
def setup(
    dsn: Annotated[str | None, typer.Option(help="Verbindung; sonst die Voreinstellung.")] = None,
) -> None:
    """Legt Datenbank und Schema an. Mehrfach aufrufbar, veraendert nie Daten.

    Ruft `db.setup`. Keine Migrationen — findet das Kommando eine Tabelle, die
    nicht zur Zeilenklasse passt, sagt es das samt Abhilfe, statt selbst etwas
    umzubauen.
    """
    raise NotImplementedError


@app.command()
def daemon(
    dsn: Annotated[str | None, typer.Option(help="Verbindung; sonst die Voreinstellung.")] = None,
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
    raise NotImplementedError


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

    Gibt aus, wie viele Dokumente neu, wie viele bereits vorhanden, wie viele
    uebersprungen und wie viele eingereiht wurden.
    """
    raise NotImplementedError


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
    raise NotImplementedError


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
    raise NotImplementedError


@app.command()
def wait(
    target: Annotated[str, typer.Argument(help="Hash oder Pfad.")],
    timeout: Annotated[float | None, typer.Option(help="Sekunden; ohne Angabe unbegrenzt.")] = None,
) -> None:
    """Wartet, bis ein Dokument endgueltig ist.

    Exit-Code 0 bei `done` und `empty`, 3 bei `failed` und `input_error`,
    1 bei Ablauf der Wartezeit.
    """
    raise NotImplementedError


@app.command()
def markdown(
    target: Annotated[str, typer.Argument(help="Hash oder Pfad.")],
    out: Annotated[Path | None, typer.Option(help="Zieldatei; ohne Angabe auf die Standardausgabe.")] = None,
) -> None:
    """Schreibt den fertigen Text eines Dokuments heraus.

    Nur den Text. Strukturliste, Bilder und die Rohantworten gibt es ueber den
    Python-Weg (`OCRWrapper.content_list`, `.images`, `.raw`) — sie auch noch
    ueber die Kommandozeile anzubieten hiesse, die Getter-Schicht ein zweites Mal
    zu bauen, und Binaerdaten haben auf der Standardausgabe ohnehin nichts zu
    suchen.

    Ist das Dokument nicht erfolgreich terminal, wird NICHTS ausgegeben und der
    Exit-Code ist 3 (`raise typer.Exit(code=3)`). Einen halben Text auszugeben
    waere schlimmer als keinen: Er saehe vollstaendig aus.
    """
    raise NotImplementedError


@app.command()
def retry(
    target: Annotated[str | None, typer.Argument(help="Hash oder Pfad; ohne Angabe siehe --failed.")] = None,
    failed: Annotated[bool, typer.Option(help="Alle gescheiterten Dokumente erneut vorlegen.")] = False,
    yes: Annotated[bool, typer.Option("--yes", help="Ohne Rueckfrage ausfuehren.")] = False,
) -> None:
    """Legt gescheiterte Dokumente erneut vor. KOSTET GELD.

    Durchgriff auf Procrastinates eigenes Werkzeug. Abgebrochene Jobs (`aborted`,
    etwa vom Herunterfahren) nimmt es NICHT an — die werden uebersprungen und
    gemeldet; ihr Weg ist `add` (erneut einreihen).

    Jedes wieder vorgelegte Dokument wird vollstaendig neu verarbeitet und neu
    bezahlt. Das Kommando sagt vorher, um wie viele es geht, und fragt nach —
    ausser mit `--yes`.
    """
    raise NotImplementedError


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
    raise NotImplementedError


def main() -> None:
    """Einstiegspunkt des Kommandos `mineru-ocr` (siehe `pyproject.toml`).

    Baut die Typer-App mit `pretty_exceptions_enable=False`. Grund: Mit der
    Voreinstellung druckt Typer bei einer unbehandelten Ausnahme einen reich
    formatierten Traceback SAMT den lokalen Variablen — und unter denen kann eine
    `Settings` stehen. Deren `__repr__` maskiert den Schluessel zwar, aber sich
    darauf zu verlassen hiesse, eine Sicherheitszusage an die Darstellungs-Logik
    einer fremden Bibliothek zu haengen. Ein schlichter Traceback genuegt.
    """
    raise NotImplementedError
