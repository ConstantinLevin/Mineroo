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


SCHLICHTER_RAHMEN = '  File "'
"""Die Signatur eines gewoehnlichen Python-Tracebacks: Das `traceback`-Modul
schreibt je Rahmen eine Zeile `  File "<pfad>", line N, in <funktion>`. Rich
stellt Pfade anders dar und erzeugt diese Form nie. Nachgemessen am 12.08.2026
unter drei Kodierungen (Vorgabe, `ascii`, `utf-8`): im schlichten Traceback
immer vorhanden, im rich-formatierten nie."""


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

    WAS HIER GEPRUEFT WIRD, und das ist beim ersten Anlauf zu schwach gewesen:
    Die erste Fassung dieses Tests suchte nach Rich-Kastenzeichen (`╭`, `│`, …)
    und verlangte, dass keine da sind. Das war eine KOSMETISCHE Eigenschaft und
    von der Kodierung des Kindprozesses abhaengig: Unter
    `PYTHONIOENCODING=ascii` weicht Rich auf ASCII-Rahmen aus, die Suche fand
    nichts, und der Test waere trotz direktem `app()`-Aufruf gruen geblieben
    (nachgemessen 12.08.2026). Der Test erbte diese Variable ausserdem aus der
    Umgebung, kontrollierte sie also nicht einmal.

    Jetzt wird zweierlei geprueft, beides unabhaengig von der Kodierung und
    beides an der Sache statt an der Darstellung:

        DIE LOKALEN VARIABLEN SELBST. Das ist die Zusage, um die es geht, und
        sie ist direkt beobachtbar. Mit eingeschalteten Rich-Tracebacks druckt
        Typer je Rahmen eine Tabelle der lokalen Namen — gemessen steht darin
        woertlich `einstellung = Settings(db_dsn='kein-gueltiger-dsn ===',
        endpoint_id='testendpoint', api_key='***', …)`. Eine `Settings`
        landet also wirklich im Traceback. Dass der Schluessel dabei als `***`
        erscheint, ist genau der Umstand, auf den man sich NICHT verlassen soll:
        Die Maskierung steckt in `Settings.__repr__`, und eine
        Sicherheitszusage an die Darstellungs-Logik einer fremden Bibliothek zu
        haengen ist der Fehler, den `cli.main` ausdruecklich vermeidet.

        DIE FORM DES TRACEBACKS. Ein gewoehnlicher Python-Traceback traegt je
        Rahmen eine Zeile `  File "…", line N, in …`; Rich erzeugt diese Form
        nie. Zwei unabhaengige Merkmale sind besser als eines: Wenn eines durch
        eine kuenftige Fassung von Typer oder Rich stumpf wird, faellt es auf,
        weil das andere anschlaegt.

    Und die Kodierung des Kindprozesses wird unten festgelegt statt geerbt —
    nicht weil die Pruefungen sie noch braeuchten, sondern weil ein Test, dessen
    Aussagekraft an einer Umgebungsvariablen haengt, die er nicht setzt, genau
    so still versagt, wie es hier passiert ist.
    """
    umgebung = dict(os.environ)
    umgebung["COLUMNS"] = "200"
    umgebung["NO_COLOR"] = "1"
    umgebung["PYTHONIOENCODING"] = "utf-8"

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

    # Die Zusage selbst: keine lokalen Variablen in der Ausgabe. `Settings(` ist
    # dafuer das Mass, weil eine erzeugte `Settings` nur in der Locals-Tabelle
    # steht — die Quellzeile daneben lautet `Settings.load(...)`, nicht
    # `Settings(`. Nachgemessen unter `ascii` wie unter `utf-8`.
    assert "Settings(" not in ausgabe, (
        "Die Ausgabe enthaelt eine ausgeschriebene `Settings` — also druckt "
        "Typer die lokalen Variablen der Rahmen. Das heisst: "
        "`pretty_exceptions_enable` steht auf True, der `__main__`-Block ruft "
        "nicht `main()` auf, sondern `app()` unmittelbar. Genau davor steht die "
        "Begruendung an `cli.main`: Der Schluessel erscheint hier zwar als "
        "`***`, aber nur, weil `Settings.__repr__` ihn maskiert — darauf soll "
        f"sich die Zusage nicht stuetzen. Ausgabe war:\n{ausgabe}"
    )

    assert SCHLICHTER_RAHMEN in ausgabe, (
        f"Im Traceback fehlt die Zeilenform {SCHLICHTER_RAHMEN!r}, die das "
        f"`traceback`-Modul je Rahmen schreibt. Der Traceback stammt also nicht "
        f"aus Pythons eigener Ausgabe, sondern von Rich — dieselbe Ursache wie "
        f"oben. Ausgabe war:\n{ausgabe}"
    )


def test_fehlende_konfiguration_endet_mit_meldung_statt_traceback(tmp_path: Path) -> None:
    """Ohne Datenbank in der Konfiguration sagt `setup` es und endet mit 1.

    Es gibt keine Datenbank im Code; wer sie nirgends einstellt, soll die Stellen
    dafuer genannt bekommen — nicht einen Traceback lesen. Endpoint und
    RunPod-Schluessel verlangt `setup` nicht: Es spricht RunPod nie an. `HOME`
    zeigt auf ein leeres Verzeichnis, damit keine Konfigurationsdatei des
    Entwicklers mitspielt, und die Umgebungsvariablen des Pakets fehlen.
    """
    umgebung = {
        name: wert
        for name, wert in os.environ.items()
        if not name.startswith("MINERU_OCR_")
    }
    umgebung["HOME"] = str(tmp_path)
    umgebung["NO_COLOR"] = "1"

    lauf = subprocess.run(
        [sys.executable, "-m", "mineru_ocr.cli", "setup"],
        cwd=WURZEL,
        env=umgebung,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert lauf.returncode == 1, lauf.stderr
    assert "MINERU_OCR_DB_DSN" in lauf.stderr
    assert "ENDPOINT" not in lauf.stderr
    assert "API-Schluessel" not in lauf.stderr
    assert "Traceback" not in lauf.stderr


def test_fehlende_konfiguration_endet_bei_daemon_mit_meldung_statt_traceback(
    tmp_path: Path,
) -> None:
    """Dasselbe fuer `daemon`: Der Konfigurationsfehler kommt vor allem anderen
    und endet mit Meldung und Exit-Code 1."""
    umgebung = {
        name: wert
        for name, wert in os.environ.items()
        if not name.startswith("MINERU_OCR_")
    }
    umgebung["HOME"] = str(tmp_path)
    umgebung["NO_COLOR"] = "1"

    lauf = subprocess.run(
        [sys.executable, "-m", "mineru_ocr.cli", "daemon", "--dsn", "host=egal"],
        cwd=WURZEL,
        env=umgebung,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert lauf.returncode == 1, lauf.stderr
    assert "MINERU_OCR_ENDPOINT_ID" in lauf.stderr
    assert "Traceback" not in lauf.stderr
