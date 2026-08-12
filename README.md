# NewOrderDevon — die OCR-Strecke für das DEVONthink-Archiv

**Stand:** 10.08.2026. Diese Datei ist der Einstieg für jemanden, der das
Repository zum ersten Mal öffnet und **keinen** Vorkontext hat. Sie sagt, was
hier liegt, wie man es zum Laufen bringt, wo die eigentliche Dokumentation steht
und was ehrlicherweise noch nicht getan ist. Sie ersetzt keines der Dokumente
unter `docs/` — sie führt in sie hinein.

---

## 1. Worum es geht

In einer DEVONthink-Datenbank liegt das Dokumentenarchiv eines Family Office. Es
soll klassifiziert werden, und die Klassifikation arbeitet mit Text-Detektoren.
Ein großer Teil des Bestands hat aber keine oder — der schwierigere Fall — eine
**vorhandene, aber zerstörte** Textebene; für diese Dokumente sind die Detektoren
blind.

Die Abhilfe ist eine OCR-Strecke auf Basis von **MinerU**, die auf einem
warteschlangenbasierten Serverless-Endpoint bei **RunPod** läuft. Dieses
Repository enthält die **lokale Seite** davon: Dokumente hinein, Text und
Struktur heraus.

Wozu der herausgeholte Text da ist, in Jurgens Worten (09.08.2026): *„sie stehen
da primär damit sie von llms die nicht multimodal sind benutzt und verarbeitet
werden können"*. Was mit ihm sonst noch geschieht — insbesondere ob und wie er
nach DEVONthink zurückgeschrieben wird — ist entworfen, aber **nicht
entschieden**; das steht in `docs/MinerU-DEVONthink-Integrationsstrecke.md`.

---

## 2. Zwei Schichten liegen hier, und sie sind verschieden alt

Das ist die wichtigste Orientierung überhaupt, weil beide Schichten „Dokumente"
enthalten und man sie sonst verwechselt.

**Die Beweis- und Begründungsschicht** — `docs/`, `plans/`, `artifacts/`. Sie ist
älter, sie ist umfangreich, und sie ist das eigentliche Kapital des Projekts: Sie
hält fest, was gemessen wurde, welche Entscheidung mit welcher Begründung
gefallen ist und was davon später zurückgenommen wurde. Einstieg:
**[`docs/README.md`](docs/README.md)** — dort steht die Lesereihenfolge der vier
Fachdokumente, die zwanzig übertragbaren Lehren des Projekts und die Pflegeregeln
des Dokumentensatzes.

**Das gebaute Werkzeug** — `mineru_ocr/`, `tests/`. Ein Python-Paket mit zwei
Zugängen (`import mineru_ocr` und das Kommando `mineru-ocr`), gebaut ab dem
07.08.2026 nach `plans/2026-08-05-ocr-service-plan.md`.

> **Achtung, eine Stelle der älteren Schicht ist dadurch überholt.**
> `docs/README.md` Abschnitt 5 sagt: *„Es gibt keinen Driver. Für dieses Vorhaben
> existiert noch keine Zeile Code."* Das stimmte am 05.08.2026 und stimmt seit dem
> 07.08.2026 nicht mehr. Der Satz ist dort als überholt gekennzeichnet und nicht
> überschrieben — die Pflegeregel 2 des Dokumentensatzes verlangt genau das, weil
> die Anatomie eines Irrtums wertvoller ist als die zurückgenommene Aussage.

---

## 3. Loslegen

**Der Interpreter ist die Miniconda-Base, und es gibt keine virtuellen
Umgebungen.** Das ist die Umgebung dieses Rechners, keine Vorliebe des Projekts:
Wer hier ein venv anlegt, arbeitet an einer Installation vorbei, in der alle
Abhängigkeiten schon liegen. **Es gibt keinen Build-Schritt.** Gearbeitet wird aus
dem Verzeichnis heraus:

```bash
/opt/homebrew/Caskroom/miniconda/base/bin/python3 -m mineru_ocr.cli --help
```

Vorausgesetzt wird außerdem eine laufende **Postgres-Instanz auf Port 5437** und
ein RunPod-API-Schlüssel in `~/.runpod/config.toml` im Feld `apikey`.

Das Paket ist zwar installierbar — `pyproject.toml` definiert unter
`[project.scripts]` das kurze Kommando `mineru-ocr` —, aber **Stand 10.08.2026 ist
es nicht installiert**, und die Miniconda-Base ist mit anderen Projekten geteilt.
Wer `pip install -e .` fährt, entscheidet damit über eine gemeinsam benutzte
Umgebung; unten steht deshalb überall die Modul-Form. Nach einer Installation
tritt schlicht `mineru-ocr` an ihre Stelle, sonst ändert sich nichts — es sind
zwei Namen für denselben Einstiegspunkt.

### Der übliche Ablauf

Kurzschreibweise für diesen Abschnitt: `PY=/opt/homebrew/Caskroom/miniconda/base/bin/python3`

```bash
$PY -m mineru_ocr.cli preflight     # prueft Endpoint, Konto und Daemon. VERAENDERT NICHTS.
$PY -m mineru_ocr.cli setup         # legt Datenbank und Schema an. Mehrfach aufrufbar.
$PY -m mineru_ocr.cli daemon        # in einem EIGENEN Fenster: hier laeuft der Exekutor
$PY -m mineru_ocr.cli ocr ~/Scans   # einreihen — kehrt sofort zurueck
$PY -m mineru_ocr.cli status
```

Neun Kommandos gibt es: `setup`, `daemon`, `ocr`, `status`, `show`, `wait`,
`markdown`, `retry`, `preflight`. Die Exit-Codes sind belegt (0/1/2/3).

### Und derselbe Dienst aus Python

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

### Tests und Typprüfung

```bash
/opt/homebrew/Caskroom/miniconda/base/bin/python3 -m pytest -q
/opt/homebrew/Caskroom/miniconda/base/bin/python3 -m mypy mineru_ocr tests
```

Zwei Kennzeichnungen ändern, was ein bestandener Lauf überhaupt beweist:

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

## 4. Wie es gebaut ist — die vier Dinge, die man nicht aus einer Datei sieht

Die ausführliche Fassung steht in [`CLAUDE.md`](CLAUDE.md) und die Begründungen in
`plans/2026-08-05-ocr-service-plan.md`. Hier die vier Entscheidungen, ohne die man
den Code falsch liest:

**Ein Buch, nicht zwei.** Die Tabelle `documents` trägt ausschließlich
Endzustände: `new | done | empty | failed | input_error`. Es gibt bewusst **kein**
`processing`. „Läuft gerade" wird aus dem jüngsten verknüpften Procrastinate-Job
**gelesen** und nirgends gespiegelt. Wer das nicht weiß, sucht vergeblich nach dem
Feld — oder führt es ein und erzeugt genau die zwei Bücher, die auseinanderlaufen.

**`empty` ist ein gültiges Ergebnis, kein Fehler.** Der Gegenstand war leer. Als
Erfolg geführt wäre es unsichtbarer Verlust, als Fehler geführt erzeugte es
endlose Wiedervorlage und Doppelkosten. Beide Fehler sind in den früheren
Ad-hoc-Skripten je einmal wirklich passiert.

**Teilstücke sind vor Postgres vollständig abstrahiert.** Hinein geht das ganze
Dokument, heraus kommt das ganze Dokument. Dass ein PDF unterwegs in Stücke zu 20
Seiten zerfällt, sieht die Datenbank nie: Der Exekutor setzt die Teilergebnisse
**vor** dem Schreiben zusammen. Es gibt keine Chunk-Tabelle.

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
- **Der Bestand ist personenbezogen.** Was an RunPod geht, geht in ein
  EU-Rechenzentrum; der `s3`-Transport ist nicht die Voreinstellung.

---

## 5. Der ehrliche Stand

**Gebaut und geprüft.** 341 Tests grün, 11 übersprungen (die `live`-Marker),
dreimal hintereinander gelaufen; mypy strict sauber; Abdeckung 90 %. **Bis zum
09.08.2026** elf bezahlte Läufe gegen den echten Endpoint, zusammen 0,4179 USD —
der Abschlusslauf schrieb 8 Dokumente `done`, 0 `failed`, 25 Jobs alle
`succeeded`. (Am 10.08.2026 kamen zwei `probe`-Jobs dazu, die nichts parsen; die
Summe ist seither nicht neu ermittelt worden. Der maßgebliche Ort für Kosten ist
ohnehin nicht diese Datei, sondern die Tageszeile der Abrechnung —
Betriebshandbuch 9.7, „Abrechnungs-Wahrheiten".) Unter
`artifacts/2026-08-08-erster-tarball-lauf/` liegen sechs echte Antworten des
Produktionspfads als Testmaterial, darunter eine leere Seite, eine
Fehlerantwort mit Worker-Traceback und ein zwölfseitiges Dokument über drei
Teilstücke.

**Nicht getan, und keines davon ist ein Versehen:**

- **Der große Lauf über den Gesamtbestand hat nicht stattgefunden.** Er kostet
  Geld und ist eine Entscheidung des Menschen.
- **Der Endpoint läuft weiter auf Worker v1.8.0** (`:17d4955b2`) mit dem
  Scale-down-Terminalfehler. Der Fix kam mit v1.8.4; der Release liegt im Fork,
  aber der Rebuild ist nie wirksam geworden. `preflight` meldet es als Warnung.
- **Ob die vom Modell erzeugten Deutungen ins Ergebnis dürfen, ist offen.** MinerU
  legt für Bild- und Diagrammblöcke eine eigene Lesart in den Text — gemessen
  wurde ein Mermaid-Flowchart, das seine eigene Logik nicht erfüllt. Die Lage ist
  vollständig geklärt, die Entscheidung gehört dem Menschen.
- **`ocr_concurrency = 24` ist gesetzt, nicht gemessen.** Messbar nur im
  Bestandslauf.

Die Aufgabenverwaltung läuft über **beads**, nicht über Dateien in diesem
Repository. Aus diesem Verzeichnis heraus braucht sie allerdings einen Zusatz:

```bash
bd -C /Users/jurgen list --status open
```

**Warum nicht einfach `bd list`:** Die beads-Datenbank liegt in
`/Users/jurgen/.beads/`, und `bd` sucht seinen Arbeitsbereich zwar aufwärts, hört
aber an der Repository-Grenze auf — die seit der Herauslösung am 10.08.2026
zwischen diesem Projekt und der Datenbank verläuft. Gemessen am 12.08.2026:
`bd where` meldet von hier aus „No active beads workspace found", während
derselbe Aufruf aus `/Users/jurgen` die 13 offenen Aufgaben liefert.
`BEADS_DIR=/Users/jurgen/.beads` wirkt ebenso.

Ein eigenes `bd init` wäre **falsch**: Es legte einen zweiten Tracker an, während
die Aufgaben dieses Projekts mit denen der übrigen in *einer* Datenbank stehen
und dort auch hingehören. Welcher der Wege dauerhaft eingerichtet wird, ist eine
offene Entscheidung — sie steht als Aufgabe `jurgen-s36` im Tracker selbst.

---

## 6. Was sonst noch im Wurzelverzeichnis liegt

`DEVONthink.sdef` ist **kein** Beifang, sondern die maßgebliche Quelle dafür, was
DEVONthinks Skript-Oberfläche hergibt — Jurgens Anweisung dazu wörtlich: *„Die
Datei ist die maßgebliche Quelle (Ground Truth) dafür, was per AppleScript/JXA
(und damit per MCP) machbar ist — NICHT dein Vorwissen über DEVONthink."* Sie wird
an drei Stellen der Dokumentation als Nachprüf-Ort geführt.

`StatusQuoDiscussion.md`, `ChatGPTOpinion.md`, `ChatGPTOpinion2.md`,
`RENDERED_TAGGER_PROMPT.md` und die drei `Prompt1_Answer_*.md` sind Diskussions-
und Recherchestände aus der Frühphase des Trägerprojekts. Sie sind **nicht** Teil
der Dokumentation und für die OCR-Strecke ungeprüft (`docs/README.md`,
Abschnitt 2).

`scripts/ocr_run.py` und `scripts/ocr_pdfs.py` sind die beiden Ad-hoc-Treiber der
früheren Produktionsläufe, mit fest verdrahteten Pfaden und mehreren benannten
Fehlern. Sie sind **Vorlagen und Messlatte, keine Bausteine** — `mineru_ocr` wird
an ihnen gemessen, nicht aus ihnen gepatcht. `scripts/ocr_test.py` ist der
Mini-Treiber des Doku-Verifikationstests vom 05.08.2026, für Einzelseiten-Tests
gebaut; seine Belege liegen unter `artifacts/2026-08-05-doku-verifikationstest/`.

---

## 7. Zur Herkunft dieses Repositories

Bis zum 10.08.2026 lag dieses Projekt als Unterverzeichnis in einem Repository,
dessen Wurzel das Home-Verzeichnis war — angelegt am 26.04.2026 von `bd init` für
die Aufgabenverwaltung, nicht für dieses Projekt. Seit dem 10.08.2026 ist
`NewOrderDevon` selbst das Repository. Die 29 Commits, die das Projekt berühren,
sind per `git subtree split` mitgekommen, ihre Nachrichten unverändert — bei
diesem Projekt sind sie das Begründungsarchiv und nicht Beiwerk. Der älteste
stammt vom 21.06.2026.
