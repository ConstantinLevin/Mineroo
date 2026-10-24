# Mineroo — ein MinerU-Wrapper für OCR auf Abruf

Mineroo ist eine Python-Bibliothek (das Paket `mineru_ocr`) mit einer CLI
(`mineru-ocr`), die Dokumente an **MinerU** auf einem warteschlangenbasierten
Serverless-Endpoint bei **RunPod** schickt und die vollständige Antwort
zurückgibt: ein Dokument oder viele, über die Bibliothek oder die Kommandozeile.
Die Antwort steht in Postgres; wer dasselbe Dokument noch einmal abschickt,
bekommt sie ohne neue Kosten aus der Datenbank.

Dieses Repository enthält die **lokale Seite**: Dateien hinein, Text und Struktur
heraus. Der Worker, der MinerU auf RunPod ausführt, lebt in seinem eigenen Fork,
`ConstantinLevin/mineru-runpod`.

**Stand:** Das Paket ist gebaut und getestet gegen den bisherigen Endpoint und
MinerU 3.4. Der Umbau auf MinerU 4, einen neuen Worker und einen neuen Endpoint,
die asynchrone Bibliothek, der Agent-Skill und die Behebung der bekannten Fehler
sind in der Spec [Mineroo#1](https://github.com/ConstantinLevin/Mineroo/issues/1)
beschrieben und werden in Tickets umgesetzt. **Wo dieses README und die Spec
auseinandergehen, gilt die Spec.** Der große Lauf über viele Dokumente hat noch
nicht stattgefunden; er kostet Geld und ist eine Entscheidung des Eigentümers.

---

## 1. Loslegen

**Der Interpreter ist die Miniconda-Base, und es gibt keine virtuellen
Umgebungen.** Das ist die Umgebung dieses Rechners, keine Vorliebe des Projekts:
Wer hier ein venv anlegt, arbeitet an einer Installation vorbei, in der alle
Abhängigkeiten schon liegen. Es gibt keinen Build-Schritt. Gearbeitet wird aus dem
Verzeichnis heraus:

```bash
/opt/homebrew/Caskroom/miniconda/base/bin/python3 -m mineru_ocr.cli --help
```

Vorausgesetzt werden eine laufende **Postgres-Instanz auf Port 5437** und ein
RunPod-API-Schlüssel in `~/.runpod/config.toml` im Feld `apikey`.

Das Paket ist installierbar — `pyproject.toml` definiert unter `[project.scripts]`
das Kommando `mineru-ocr` —, aber nicht installiert, und die Miniconda-Base ist
mit anderen Projekten geteilt. Wer `pip install -e .` fährt, entscheidet damit
über eine gemeinsam benutzte Umgebung; unten steht deshalb überall die
Modul-Form. Nach einer Installation tritt schlicht `mineru-ocr` an ihre Stelle.

### Der übliche Ablauf

Kurzschreibweise: `PY=/opt/homebrew/Caskroom/miniconda/base/bin/python3`

```bash
$PY -m mineru_ocr.cli preflight     # prueft Endpoint, Konto und Daemon. VERAENDERT NICHTS.
$PY -m mineru_ocr.cli setup         # legt Datenbank und Schema an. Mehrfach aufrufbar.
$PY -m mineru_ocr.cli daemon        # in einem EIGENEN Fenster: hier laeuft der Exekutor
$PY -m mineru_ocr.cli ocr ~/Scans   # einreihen — kehrt sofort zurueck
$PY -m mineru_ocr.cli status
```

Neun Kommandos gibt es: `setup`, `daemon`, `ocr`, `status`, `show`, `wait`,
`markdown`, `retry`, `preflight`. Die Exit-Codes sind belegt (0/1/2/3).

### Und dieselbe Fläche aus Python

```python
from pathlib import Path
from mineru_ocr import OCRClient, OCRMegaWrapper

with OCRClient() as client:
    mega = OCRMegaWrapper.from_dir(
        Path("~/Scans").expanduser(), client.conn, client.settings
    )
    client.ocr_start(mega)          # kehrt SOFORT zurueck, es laeuft noch nichts
    print(client.status())

    fertig = client.wait("<sha256>")
    print(fertig.rendered_markdown)  # das GANZE Dokument als Markdown
    print(fertig["markdown"])        # das nackte Markdown der Engine
    print(fertig["content_list"])    # jeder Block einzeln, mit type und page_idx
    print(fertig.raw)                # alles, ungefiltert
```

Kommandozeile und `import mineru_ocr` sind zwei Zugänge auf **dieselbe** Fläche;
das Kommando enthält keine eigene Logik.

### Die eine Überraschung, die jeder einmal hat

**Der Daemon ist ein eigener Prozess.** `ocr_start` bzw. das Kommando `ocr` legt
Jobs an und kehrt sofort zurück. Läuft kein `daemon`, passiert danach gar nichts
— und es sieht aus wie ein Hänger. Deshalb sagt `preflight` es einem
ausdrücklich, statt es einen suchen zu lassen.

---

## 2. Tests und Typprüfung

```bash
/opt/homebrew/Caskroom/miniconda/base/bin/python3 -m pytest -q > ausgabe.txt 2>&1
/opt/homebrew/Caskroom/miniconda/base/bin/python3 -m mypy mineru_ocr tests
```

Die Ausgabe eines Testlaufs wird immer erst ganz in eine Datei geschrieben und
dann gelesen oder durchsucht. Zwei Kennzeichnungen ändern, was ein bestandener
Lauf überhaupt beweist:

- **`db`** braucht die Postgres-Instanz auf Port 5437 mit der Datenbank
  `mineru_ocr_test`. Fehlt sie, werden diese Tests **übersprungen** statt zu
  scheitern — ein fehlender Server ist keine Aussage über den Code. Die Kehrseite:
  Ein „alles grün" ohne laufende Instanz hat die ganze Datenbankseite nie
  ausgeführt. Wer die Zahl der übersprungenen Tests nicht ansieht, hält einen
  halben Lauf für einen ganzen.
- **`live`** spricht mit dem echten Endpoint und **kostet Geld**. Nur nach
  ausdrücklicher Zustimmung, nie beiläufig.

Die Typprüfung läuft über `mineru_ocr` **und** `tests`. Warum beides nötig ist,
steht in `pyproject.toml` an der zweiten mypy-Ausnahme.

---

## 3. Wie es gebaut ist — die vier Dinge, die man nicht aus einer Datei sieht

Die ausführliche Fassung steht in [`CLAUDE.md`](CLAUDE.md) und die Begründungen in
`plans/2026-08-05-ocr-service-plan.md`, soweit die Spec sie nicht ablöst. Hier die
vier Entscheidungen, ohne die man den Code falsch liest:

**Ein Buch, nicht zwei.** Die Tabelle `documents` trägt ausschließlich
Endzustände: `new | done | empty | failed | input_error` (so ist es heute gebaut;
die Spec ändert das, siehe oben). „Läuft gerade" wird aus dem
jüngsten verknüpften Procrastinate-Job **gelesen** und nirgends gespiegelt. Wer
das nicht weiß, sucht vergeblich nach dem Feld — oder führt es ein und erzeugt
genau die zwei Bücher, die auseinanderlaufen.

**`empty` ist ein gültiges Ergebnis, kein Fehler.** Der Gegenstand war leer. Als
Erfolg geführt wäre es unsichtbarer Verlust, als Fehler geführt erzeugte es
endlose Wiedervorlage und Doppelkosten.

**Teilstücke sind vor Postgres vollständig abstrahiert.** Hinein geht das ganze
Dokument, heraus kommt das ganze Dokument. Dass ein PDF unterwegs in Stücke
zerfällt, sieht die Datenbank nie: Der Exekutor setzt die Teilergebnisse **vor**
dem Schreiben zusammen. Es gibt keine Chunk-Tabelle.

**Die Abruf-Regel — alles holen, alles persistieren, nie filtern — ist als
Bauweise umgesetzt, nicht als Vorsatz.** Die Ergebnisse liegen auf der Gegenseite
nur rund **30 Minuten**; was beim Auspacken fallengelassen wird, ist danach
unwiederbringlich und erzeugt dabei kein Fehlersignal. Deshalb trägt jedes
Teilstück ein Feld `extras`, in das alles Unerkannte wandert.

### Harte Regeln, jede wegen eines bezahlten Fehlers

- **`pypdf` ist im ganzen Projekt verboten.** Es bläht beim Schneiden auf: 360 MB
  Quelle wurden zu 4,1 GB Einzelseiten. Geschnitten wird mit `pypdfium2`.
- **Niemals `/runsync`.** Der Endpoint ist warteschlangenbasiert; `/runsync`
  wartet 60 Sekunden, ein Kaltstart dauert rund 8,5 Minuten.
- **Der API-Schlüssel steht nur im `Authorization`-Header**, nie in einer URL, und
  wird nie ausgegeben.
- **Was an RunPod geht, geht in ein EU-Rechenzentrum.** Ein allgemeiner Wrapper
  weiß nicht, ob die Daten personenbezogen sind; der `s3`-Transport ist nicht die
  Voreinstellung.

---

## 4. Aufgaben und Dokumentation

**Aufgaben sind GitHub-Issues**, nach `docs/agents/`: `ConstantinLevin/Mineroo`
für das Paket, der Fork `ConstantinLevin/mineru-runpod` für den Worker. Die
Spec zum laufenden Umbau ist
[Mineroo#1](https://github.com/ConstantinLevin/Mineroo/issues/1).

Unter `docs/` und `plans/` liegt die Begründungsschicht, die älter ist als der
Umbau: was gemessen wurde, welche Entscheidung mit welcher Begründung gefallen ist
und was davon später zurückgenommen wurde. Einstieg ist
[`docs/README.md`](docs/README.md). Diese Dokumente sind **Geschichte** — sie
beschreiben den Stand bis MinerU 3.4 und den alten Endpoint, tragen oben einen
Hinweis dazu und werden nicht umgeschrieben. Aktuell gelten dieses README und die
Spec. Eine Ausnahme: `plans/2026-08-06-procrastinate-feature-verifikation.md`
bleibt aktuell, solange Procrastinate die Warteschlange ist.

`scripts/ocr_run.py` und `scripts/ocr_pdfs.py` sind die beiden Ad-hoc-Treiber der
früheren Läufe, mit fest verdrahteten Pfaden und mehreren benannten Fehlern. Sie
sind **Vorlagen und Messlatte, keine Bausteine** — `mineru_ocr` wird an ihnen
gemessen, nicht aus ihnen gepatcht. `scripts/ocr_test.py` ist der Mini-Treiber
des Doku-Verifikationstests vom 05.08.2026; seine Belege liegen unter
`artifacts/2026-08-05-doku-verifikationstest/`.
