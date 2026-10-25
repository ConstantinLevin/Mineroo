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
Abhängigkeiten schon liegen.

### Der installierte Befehl

`mineru-ocr` ist ein installierter Befehl, kein Pfad. `pyproject.toml` definiert
ihn unter `[project.scripts]`; installiert wird er einmal, aus dem Haupt-Checkout,
in die Miniconda-Base, in der seine Abhängigkeiten schon liegen:

```bash
/opt/homebrew/Caskroom/miniconda/base/bin/python3 -m pip install --no-deps --no-build-isolation -e .
```

`--no-deps` und `--no-build-isolation` sind der Grund, warum das die geteilte Base
nicht verändert: Es wird nur dieses Paket eingetragen — keine Abhängigkeit
installiert, aktualisiert oder entfernt, und kein Build-Werkzeug nachgeladen
(`setuptools` liegt in der Base). Die Installation ist „editierbar": Sie zeigt auf
den Checkout, aus dem sie gemacht wurde, und folgt dessen Änderungen. Deshalb
nicht aus einem Arbeitsbaum installieren, der wieder verschwindet.

Ohne Installation geht dasselbe aus dem Verzeichnis heraus als
`/opt/homebrew/Caskroom/miniconda/base/bin/python3 -m mineru_ocr.cli …`; es gibt
keinen Build-Schritt.

### Konfiguration

Endpoint, Datenbank und die Sollwerte des Preflights stehen **nicht im Code**,
sondern in einer Konfiguration. Es gibt für Endpoint und Datenbank keine
Voreinstellung: Wer sie nirgends einstellt, bekommt beim ersten Aufruf eine
Meldung, die alle Stellen nennt.

Quellen, vom Stärksten: ausdrückliche Angabe (Argument oder Option) →
Umgebungsvariable `MINERU_OCR_<NAME>` → Konfigurationsdatei → Voreinstellung. Die
Datei ist TOML und liegt unter `~/.mineru_ocr/config.toml`; `MINERU_OCR_CONFIG`
zeigt auf eine andere. Fehlt die Standarddatei, ist das kein Fehler; eine
ausdrücklich genannte, die fehlt, schon. Ein Schlüssel, den die Datei nicht
kennt, ist ein Fehler und kein stilles Ignorieren.

```toml
# ~/.mineru_ocr/config.toml
endpoint_id = "<RunPod-Endpoint-Kennung>"
db_dsn = "host=localhost port=5437 dbname=mineru_ocr user=<benutzer>"

# Sollwerte, gegen die `preflight` vergleicht (Voreinstellung in Klammern):
expected_image_tag = "<Tag des gebauten Images>"   # (keiner: dann "nicht prüfbar")
workers_min = 0                                      # (0)
workers_max = 10                                     # (10, die Kontogrenze)
idle_timeout = 60                                    # (60, Sekunden)
execution_timeout_ms = 600000                        # (600000)
allowed_data_centers = ["EU-RO-1", "EUR-IS-1"]       # (alle EWR-Rechenzentren)

# Endpoint-Variablen, die der Preflight nach Wert prüft; was die Tabelle nicht
# nennt, behält seinen Sollwert:
[endpoint_env]
MINERU_MODEL_SOURCE = "local"                        # (local)
MINERU_MAX_CONCURRENCY = "1"                         # (1)
MINERU_VIRTUAL_VRAM_SIZE = "<Wert aus der Messung>"  # (offen: nur berichtet)
```

Jeder Schlüssel außer der Tabelle hat eine Umgebungsvariable, zum Beispiel
`MINERU_OCR_ENDPOINT_ID`, `MINERU_OCR_DB_DSN`, `MINERU_OCR_EXPECTED_IMAGE_TAG`,
`MINERU_OCR_IDLE_TIMEOUT`; die Rechenzentren stehen dort durch Kommas getrennt in
`MINERU_OCR_ALLOWED_DATA_CENTERS`.

Der Preflight zeigt die Werte der Variablen aus `[endpoint_env]`. Ist ein ganzer
Namensteil (getrennt an allem außer Buchstaben und Ziffern) `KEY(S)`,
`APIKEY(S)`, `ACCESSKEY`, `PRIVATEKEY`, `SSHKEY`, `SECRET(S)`, `SECRETKEY`,
`TOKEN`, `PASSWORD(S)`, `PASSWD`, `PASS`, `PASSPHRASE`, `PWD`,
`CREDENTIAL(S)`, `AUTH`, `AUTHORIZATION` oder `BEARER`, wird die Variable
verglichen und als `***` gezeigt, im Befund wie in `repr(Settings)`. Teilwörter
zählen nicht: `HF_TOKEN` wird verdeckt, `MAX_TOKENS` und `MONKEY` nicht.

**Der RunPod-Schlüssel bleibt in `~/.runpod/config.toml`**, Feld `apikey`, und
steht nie in dieser Konfiguration. „Nur EU" heißt EWR — EU-Staaten plus Island und
Norwegen, denn dort gilt die DSGVO —, und die zulässige Liste ist der Sollwert
`allowed_data_centers`.

### Der übliche Ablauf

```bash
mineru-ocr setup         # legt Datenbank und Schema an. Mehrfach aufrufbar.
mineru-ocr preflight     # prueft Endpoint, Konto und Daemon. VERAENDERT NICHTS.
mineru-ocr daemon        # in einem EIGENEN Fenster: hier laeuft der Exekutor
mineru-ocr ocr ~/Scans   # einreihen — kehrt sofort zurueck, nennt Pfad und Hash
mineru-ocr status
```

Der Preflight berichtet nur und verändert nichts; das Paket legt keine bezahlte
Ressource an und ändert keine. Er prüft, was **tatsächlich läuft**: das Image des
Templates und jedes Workers gegen `expected_image_tag`, die Endpoint-Variablen
nach Wert an der Umgebung aller zuletzt gelaufenen Worker (auch der beendeten),
dass alle Rechenzentren in der zulässigen Liste liegen, und die Sollwerte des
Endpoints. FlashBoot wird berichtet. Was sich nicht feststellen lässt — kein
Worker, ein Worker ohne Umgebung, keine Rechenzentren in der Antwort, kein
erwarteter Tag —, heißt „nicht prüfbar" und ausdrücklich nicht „in Ordnung".

Neun Kommandos gibt es: `setup`, `daemon`, `ocr`, `status`, `show`, `wait`,
`markdown`, `retry`, `preflight`. Die Exit-Codes sind belegt (0/1/2/3).

**Pfade kennt nur die CLI.** Sie liest Dateien und Ordner (in Ordnern über das
Muster `--glob`), hasht sie und nennt in ihrer Ausgabe die Zuordnung von Pfad zu
Hash; `ocr --json` gibt je Dokument den Hash, den Zustand und ob es eingereiht
wurde, dazu die übersprungenen Dateien. Eine ausdrücklich genannte, nicht
unterstützte oder zu große Datei hält die übrigen nicht auf: Sie wird mit Grund
gemeldet, und `ocr` endet mit Exit-Code 1. In Ordnern werden solche Dateien
übersprungen und gezählt. Ziele für `show`, `wait`, `markdown` und `retry` sind
Hashes oder Pfade, die die CLI selbst hasht. Auf stdout stehen nur Daten;
Meldungen und Warnungen gehen auf stderr.

### Und dieselbe Fläche aus Python

Die Bibliothek nimmt **Bytes** und gibt **Hashes**; einen Pfad kennt sie nicht.

```python
from pathlib import Path
from mineru_ocr import OCRClient

with OCRClient() as client:
    # Bytes rein, Hash raus; kehrt SOFORT zurueck, es laeuft noch nichts. Eines oder
    # viele (`ocr_start([a, b, c])` gibt eine Liste). Erst werden alle geprueft,
    # dann eingereiht.
    h = client.ocr_start(Path("scan.pdf").read_bytes())
    print(client.status())

    fertig = client.wait(h)          # oder spaeter, auch aus einem anderen Prozess
    print(fertig.rendered_markdown)  # das GANZE Dokument als Markdown
    print(fertig["markdown"])        # das nackte Markdown der Engine
    print(fertig["content_list"])    # jeder Block einzeln, mit type und page_idx
    print(fertig.raw)                # die vollstaendige Antwort, ungefiltert

    antwort = client.ocr(b"...")     # Abschicken und Warten in einem Zug
    ohne_warten = client.get(h)      # holt ein Ergebnis ab, ohne zu warten
```

Gibt es zu den Bytes schon ein Ergebnis (auch ein `failed`), ist das ein Treffer:
Es wird nichts angelegt und nichts eingereiht; ein gescheitertes Dokument liest
nur `retry` neu. Abschicken lädt kein `raw`; Hüllen laden es erst, wenn jemand das
Ergebnis will.

Kommandozeile und `import mineru_ocr` sind zwei Zugänge auf **dieselbe** Fläche;
das Kommando enthält keine eigene Logik.

### Die eine Überraschung, die jeder einmal hat

**Der Daemon ist ein eigener Prozess.** `ocr_start` bzw. das Kommando `ocr` legt
Jobs an und kehrt sofort zurück. Läuft kein `daemon`, wird trotzdem eingereiht —
aber danach passiert gar nichts, und es sieht aus wie ein Hänger. Deshalb warnt
das Abschicken (auf stderr bzw. dem Logger `mineru_ocr.client`), und `status` und
`preflight` sagen es einem ausdrücklich, statt es einen suchen zu lassen; einen
Daemon startet der Betreiber. Als laufend gilt ein Daemon, wenn ein Worker einen
Herzschlag hat, der jünger ist als 30 Sekunden — gemessen an der Uhr der
Datenbank, damit die Zeile eines abgestürzten Daemons nicht weiter „läuft" sagt.

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
