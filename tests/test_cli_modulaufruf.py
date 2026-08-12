"""Der Modul-Aufruf `python -m mineru_ocr.cli` — der Weg ohne Installation.

WARUM DIESE DATEI GETRENNT VON `test_cli.py` STEHT, und das ist kein Geschmack:
`test_cli.py` traegt modulweit `pytestmark = pytest.mark.db`. Dieser Test hier
braucht keine Datenbank, keinen Endpoint und kein Geld — er darf deshalb nicht
mit `-m "not db"` herausfallen, und genau das taete er in jener Datei. Die
Auswahl `pytest -q -m "not db"` ist in CLAUDE.md als Arbeitsweise dokumentiert;
ein Test, der ohne Datenbank laeuft, aber bei dieser Auswahl verschwindet, ist
schlimmer als keiner, weil er im vollen Lauf gruen ist und beim schnellen nie
gefragt wird.

WARUM ES IHN ueberhaupt GIBT — der Fehler, gegen den er steht, war am
10.08.2026 im Code:

    `python -m mineru_ocr.cli --help` tat NICHTS. Kein Kommando, keine Meldung,
    kein Fehler, Exit-Code 0.

Die Ursache war ein fehlender `__main__`-Block. `pyproject.toml` bindet unter
`[project.scripts]` den Namen `mineru-ocr` an `mineru_ocr.cli:main`; ohne
installiertes Paket ruft aber niemand `main` auf, und ein Modul ohne
`if __name__ == "__main__":` fuehrt beim `-m`-Aufruf nur seine Definitionen aus
und endet. Erfolgreich, im Sinne des Exit-Codes.

Das ist die teuerste Fehlerform, die dieses Projekt kennt: erfolgreich aussehen
und nichts tun. Und es ist ausgerechnet der Aufruf, den man zuerst probiert,
denn CLAUDE.md sagt, gearbeitet werde aus dem Verzeichnis heraus, und das Paket
ist nicht installiert.

WARUM EIN UNTERPROZESS UND NICHT `CliRunner`: `CliRunner` importiert `app` und
ruft es auf. Damit prueft es die Kommandos, aber NIE den Weg dorthin — weder den
`__main__`-Block noch `main()`. Beide koennten spurlos verschwinden, und alle
uebrigen Tests blieben gruen. Nur ein echter Prozessstart geht denselben Weg wie
ein Mensch an der Tastatur.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from mineru_ocr.cli import app

WURZEL = Path(__file__).resolve().parent.parent
"""Das Repository-Wurzelverzeichnis. `python -m` legt das Arbeitsverzeichnis auf
`sys.path`, deshalb findet der Unterprozess das Paket von hier aus auch ohne
Installation — genau wie ein Mensch, der im Projektverzeichnis steht."""


def _kommandonamen() -> set[str]:
    """Die Namen aller registrierten Kommandos, AUS DER APP gelesen.

    Nicht abgeschrieben: Eine Liste von Hand veraltet still, sobald jemand ein
    zehntes Kommando anlegt, und dieser Test wuerde dann weiterhin neun pruefen
    und gruen bleiben. Typer traegt `name=None`, wenn das Kommando so heisst wie
    seine Funktion — der Normalfall hier —, deshalb wird auf den Funktionsnamen
    zurueckgegriffen. Unterstriche wuerden zu Bindestrichen; im Moment hat kein
    Kommando welche, aber die Umwandlung steht hier, damit der Test nicht bei
    der ersten Umbenennung falsch liegt.
    """
    namen: set[str] = set()
    for befehl in app.registered_commands:
        if befehl.name:
            namen.add(befehl.name)
        elif befehl.callback is not None:
            namen.add(befehl.callback.__name__.replace("_", "-"))
    return namen


def test_der_modulaufruf_zeigt_wirklich_die_hilfe() -> None:
    """`python -m mineru_ocr.cli --help` endet mit 0 UND gibt alle Kommandos aus.

    Die zweite Haelfte traegt den Test. Der urspruengliche Fehler endete mit
    Exit-Code 0 — ein Test, der nur den Exit-Code prueft, waere damals gruen
    gewesen und haette nichts gemerkt.
    """
    umgebung = dict(os.environ)
    # Breite Ausgabe erzwingen: Typer bricht die Hilfe an der Terminalbreite um,
    # und ein Umbruch mitten in einem Kommandonamen liesse den Test scheitern,
    # ohne dass etwas kaputt waere. NO_COLOR haelt Steuerzeichen heraus.
    umgebung["COLUMNS"] = "200"
    umgebung["NO_COLOR"] = "1"

    lauf = subprocess.run(
        [sys.executable, "-m", "mineru_ocr.cli", "--help"],
        cwd=WURZEL,
        env=umgebung,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert lauf.returncode == 0, (
        f"Exit-Code {lauf.returncode}. stderr:\n{lauf.stderr}"
    )
    # Die eigentliche Zusicherung: Es kommt ueberhaupt etwas heraus. Genau das
    # fehlte, und der Exit-Code sagte es nicht.
    assert lauf.stdout.strip(), (
        "Der Modul-Aufruf hat NICHTS ausgegeben und trotzdem mit 0 geendet. "
        "Das ist der Zustand vom 10.08.2026: der `__main__`-Block in cli.py "
        "fehlt oder ruft `main()` nicht mehr auf."
    )

    fehlend = sorted(name for name in _kommandonamen() if name not in lauf.stdout)
    assert not fehlend, (
        f"Diese Kommandos stehen nicht in der Hilfe: {fehlend}. "
        f"Ausgabe war:\n{lauf.stdout}"
    )


RICH_RAHMEN = ("╭", "╰", "│", "─")
"""Die Kastenzeichen, mit denen Rich einen Traceback rahmt. Sie sind der
Unterschied, an dem sich `main()` von `app()` messen laesst — und sie bleiben
auch dann stehen, wenn `NO_COLOR` gesetzt ist und die Ausgabe in eine Pipe
geht (beides nachgemessen am 12.08.2026)."""


def test_der_modulaufruf_geht_wirklich_durch_main() -> None:
    """Ein unbehandelter Fehler kommt SCHLICHT heraus, nicht rich-formatiert.

    HIER STAND EIN TEST, DER SEINE EIGENE ZUSAGE NICHT EINGELOEST HAT. Er hiess
    `test_ein_unbekanntes_kommando_endet_mit_zwei`, prueste `mineru-ocr
    gibtesnicht` auf Exit-Code 2 und behauptete im Docstring, damit sei
    gesichert, dass der Modul-Weg durch `main()` laeuft. Nachgemessen
    (12.08.2026) stimmt das nicht: Ein unbekanntes Kommando endet auch bei
    direktem `app()`-Aufruf mit 2. Der Test waere gruen geblieben, waehrend
    genau die Zusage gebrochen ist, um derentwillen er dastand.

    WORUM ES WIRKLICH GEHT: `main()` setzt `app.pretty_exceptions_enable =
    False`, bevor es `app()` aufruft. Mit der Voreinstellung druckt Typer bei
    einer unbehandelten Ausnahme einen rich-formatierten Traceback SAMT den
    lokalen Variablen jedes Rahmens — und unter denen kann eine `Settings`
    stehen. Deren `__repr__` maskiert den Schluessel zwar, aber sich darauf zu
    verlassen hiesse, eine Sicherheitszusage an die Darstellungs-Logik einer
    fremden Bibliothek zu haengen. Die volle Begruendung steht an `cli.main`.

    Ein `__main__`-Block, der `app()` statt `main()` aufriefe, faellt beim
    `--help` NICHT auf: Die Hilfe saehe zeichengleich aus. Sichtbar wird der
    Unterschied nur, wenn eine Ausnahme durchschlaegt.

    DER GEWAEHLTE FEHLERWEG, und warum es dieser ist: Jedes Kommando in `cli.py`
    faengt ausschliesslich `MineruOcrError`. Alles andere entkommt und landet bei
    Typer — das ist die Stelle, an der sich die Einstellung zeigt. Ein
    syntaktisch ungueltiger Verbindungs-String erzeugt in psycopg eine
    `ProgrammingError`, also gerade KEINEN `MineruOcrError`, und er wird
    zurueckgewiesen, BEVOR irgendeine Verbindung aufgebaut wird. Damit braucht
    dieser Test keine laufende Datenbank, ruehrt keine an und kostet nichts.

    Gemessen wurde der Kontrast am 12.08.2026, mit denselben
    Umgebungsvariablen wie unten: ueber `app()` kommen 12126 Zeichen mit
    Kastenzeichen heraus, ueber `main()` 2711 ohne.
    """
    umgebung = dict(os.environ)
    umgebung["COLUMNS"] = "200"
    umgebung["NO_COLOR"] = "1"

    lauf = subprocess.run(
        [sys.executable, "-m", "mineru_ocr.cli", "setup", "--dsn", "kein-gueltiger-dsn ==="],
        cwd=WURZEL,
        env=umgebung,
        capture_output=True,
        text=True,
        timeout=60,
    )
    ausgabe = lauf.stdout + lauf.stderr

    # Erst die Voraussetzung des Tests selbst: Es MUSS eine unbehandelte
    # Ausnahme durchgeschlagen sein. Faengt cli.py diesen Fall eines Tages ab
    # oder wandelt psycopg ihn in einen MineruOcrError, misst der Test nichts
    # mehr — und soll das sagen, statt still gruen zu bleiben.
    assert "Traceback (most recent call last)" in ausgabe, (
        "Es ist keine unbehandelte Ausnahme durchgeschlagen, also prueft dieser "
        "Test die Traceback-Form gar nicht mehr. Entweder faengt `cli.setup` "
        "den Fall inzwischen ab, oder psycopg wirft etwas anderes. Der Test "
        f"braucht dann einen neuen Fehlerweg. Ausgabe war:\n{ausgabe}"
    )

    gefunden = [z for z in RICH_RAHMEN if z in ausgabe]
    assert not gefunden, (
        f"Der Traceback ist rich-formatiert (Kastenzeichen {gefunden} gefunden). "
        f"Das heisst: `pretty_exceptions_enable` steht auf True, der "
        f"`__main__`-Block ruft also nicht `main()` auf, sondern `app()` "
        f"unmittelbar. Damit druckt ein Absturz die lokalen Variablen jedes "
        f"Rahmens — moeglicherweise samt einer `Settings`. Ausgabe war:\n{ausgabe}"
    )
