# Mineroo — ein MinerU-Wrapper für OCR auf Abruf

Mineroo schickt Dokumente an **MinerU auf RunPod** und speichert die vollständige
Antwort in Postgres: Text, Struktur, Bilder und Rohdaten. Du benutzt es über die
Kommandozeile `mineru-ocr`, die Python-Bibliothek `mineru_ocr` oder einen
[Agent-Skill](skills/mineroo/SKILL.md). Gleiche Bytes ergeben denselben SHA-256-Hash:
Ein bereits gespeichertes Ergebnis kommt ohne neuen Lauf und ohne neue OCR-Kosten.

Audiodateien transkribiert Mineroo mit **ElevenLabs Scribe v2**, in einer eigenen
Daemon-Lane. Dateiaufruf, Hash-Cache und Ausgabewege sind dieselben. Audio wird
in den **USA** verarbeitet; Einzelheiten und Kosten stehen unter [Audio](#audio).

Dieses Repository enthält die lokale Seite. Der MinerU-4-Worker liegt im Fork
[ConstantinLevin/mineru-runpod](https://github.com/ConstantinLevin/mineru-runpod).

**Direkt zu:** [CLI-Schnellstart](#cli-schnellstart) ·
[Python-Schnellstart](#python-schnellstart) · [CLI-Referenz](#cli-referenz) ·
[Python-Referenz](#python-referenz) · [Konfiguration](#konfiguration) ·
[Daemons](#daemons) · [Tests](#tests-und-typprüfung) ·
[Aufbau und Hintergründe](#aufbau-und-hintergründe)

## Installieren

Du brauchst **Python ab 3.12**, eine erreichbare **Postgres-Instanz** und für neue
OCR-Läufe einen eingerichteten **RunPod-Serverless-Endpoint mit dem Worker aus dem
Fork** sowie RunPod-Guthaben. Mineroo richtet den Endpoint nicht ein.

In einer eigenen Python-Umgebung aus dem Checkout installieren:

```bash
git clone https://github.com/ConstantinLevin/Mineroo.git
cd Mineroo
python -m pip install .
mineru-ocr --help
```

Ist Mineroo schon installiert und konfiguriert, geht es direkt mit den
Schnellstarts weiter. Sonst zuerst [Datenbank, Endpoint und Schlüssel
konfigurieren](#konfiguration). Datenbank und Endpoint haben **keine
Voreinstellung im Paket**. Die folgenden Beispiele wählen ausdrücklich eine
Testdatenbank; dieselbe Verbindung gilt für CLI, Python und Daemon:

```bash
export MINERU_OCR_DB_DSN='host=localhost port=5437 dbname=mineru_ocr_test'
```

Host, Port und gegebenenfalls die Datenbankrolle an die eigene Instanz anpassen.
Die Umgebungsvariable hat Vorrang vor der Konfigurationsdatei.

## CLI-Schnellstart

```bash
mineru-ocr setup                         # Datenbank und Schema anlegen; mehrfach erlaubt
mineru-ocr daemon list                   # nachsehen, was schon läuft und wartet
mineru-ocr daemon start 1                # einen Daemon im Hintergrund starten
mineru-ocr dokument.pdf --timeout 600     # abschicken, warten, Volltext auf stdout
mineru-ocr dokument.pdf -o text.md        # Volltext in Datei, deren Pfad auf stdout
mineru-ocr dokument.pdf --re -o lesbar.md  # auch HTML-Tabellen als Markdown
mineru-ocr dokument.pdf --ra -o roh.json  # vollständiges Ergebnis samt raw als JSON
mineru-ocr daemon stop 1                 # bis zu einen Daemon ordentlich beenden
```

**Ein Daemon arbeitet die ganze Warteschlange seiner Datenbank ab.** Neue Läufe
kosten RunPod-Guthaben bzw. für Audio ElevenLabs-Credits. `stop 1` wählt den am wenigsten belegten lokalen Daemon;
es bezeichnet nicht zwingend den zuvor gestarteten. Ein gespeicherter Treffer
braucht keinen Daemon. Braucht eine Datei noch Verarbeitung und läuft keiner,
endet der Dateiaufruf mit Code **5**, ohne diese Datei einzureihen.

Mehrere Dateien und Ordner gehen ebenso:

```bash
mineru-ocr a.pdf b.png scans/ --timeout 600
mineru-ocr scans/ -o stapel.md
mineru-ocr -d scans/ --timeout 600 2>fortschritt.log
mineru-ocr digital.pdf --tier advanced --ocr-mode txt --no-image-analysis
```

Ordner werden rekursiv nach Pfad sortiert gelesen; der Text eines Stapels folgt
der Eingabereihenfolge. Meldungen und Gründe stehen auf stderr. `-d` zeigt dort
den Wartefortschritt, umgeleitet als Klartextzeilen. [Alle Optionen und
Exit-Codes](#cli-referenz) stehen weiter unten.

## Python-Schnellstart

Einmal `mineru-ocr setup` ausführen. Für neue Dokumente muss ein Daemon laufen
(siehe oben); die Bibliothek startet selbst keinen. Sie nimmt **Bytes**, keine
Pfade, und gibt **Hashes** zurück. Die Beispiele verwenden die oben gesetzte
Verbindung ausdrücklich als `db_dsn`. `preflight=False` unterbindet die
RunPod-Prüfung beim Öffnen; Abschicken und Lesen benötigen nur Postgres.

### Ein Dokument lesen

```python
import os
from pathlib import Path
from mineru_ocr import OCRClient

with OCRClient(db_dsn=os.environ["MINERU_OCR_DB_DSN"], preflight=False) as client:
    fertig = client.ocr(Path("dokument.pdf").read_bytes(), timeout=600)
    if fertig.success:  # done oder empty
        print(fertig.rendered_markdown)
    else:
        print(fertig.status, fertig.row.error)
```

`rendered_markdown` ist die Vollfassung wie beim CLI-Dateiaufruf, einschließlich
Kopf- und Fußzeilen, Seitenzahlen, Randtexten, Fußnoten und vorhandenen
Bildbeschreibungen. Die synchrone Hülle innerhalb des `with` benutzen: Sie lädt
`raw` bei Bedarf über die Verbindung des Clients nach.

### Abschicken und später abholen

```python
import os
from pathlib import Path
from mineru_ocr import OCRClient

with OCRClient(db_dsn=os.environ["MINERU_OCR_DB_DSN"], preflight=False) as client:
    h = client.ocr_start(Path("dokument.pdf").read_bytes())
    print(h, h.queued, h.status, h.is_open)
    stand = client.document(h)              # ohne raw zu laden
    print(stand.phase)
    fertig = client.wait(h, timeout=600)     # auf einen Endzustand warten
    huelle = client.get(h)                  # Ergebnis ohne Warten holen
    print(huelle.to_dict(raw=True))         # Angaben plus vollständige Antwort
```

`h` ist ein `Submitted`: eine Zeichenkette mit dem SHA-256-Hash und Angaben zu
diesem Abschicken. `queued` sagt, ob dieser Aufruf eingereiht hat; `status` ist
bei einem Treffer der Endzustand, sonst `None`; `is_open` bedeutet, dass noch kein
Ergebnis da ist. Den Hash kann ein anderer Prozess später mit `get()` oder
`wait()` weiterverwenden. `get()` wirft `DocumentNotFound`, solange das Dokument
noch offen ist; `document()` zeigt dagegen auch seinen Job-Zustand.

### Viele Dokumente

```python
import os
from contextlib import closing
from pathlib import Path
from mineru_ocr import OCRClient

with OCRClient(db_dsn=os.environ["MINERU_OCR_DB_DSN"], preflight=False) as client:
    hashes = client.ocr_start([Path(p).read_bytes() for p in ("a.pdf", "b.png")])
    alle = client.wait_all(hashes, timeout=1800)  # Reihenfolge der Frage
    print([huelle.status for huelle in alle])

    # Alternativ direkt nach ocr_start: jedes Ergebnis, sobald es fertig ist.
    with closing(client.as_completed(hashes, timeout=1800)) as ergebnisse:
        for huelle in ergebnisse:
            print(huelle.sha256, huelle.status)
```

Ein gescheitertes Dokument beendet das Warten auf die übrigen nicht: Jeder
Endzustand ist ein Ergebnis. `timeout` gilt für den ganzen Warteaufruf bis zum Endzustand; das Laden
fertiger Rohdaten kommt zeitlich hinzu. `WaitTimeoutError.pending` nennt die noch offenen Hashes; Jobs bleiben bestehen.
`as_completed()` hält eine eigene Verbindung, bis der Iterator erschöpft oder
mit `close()` geschlossen ist. `closing(...)` schließt ihn auch bei vorzeitigem
Abbruch.

### Text, Struktur und Bilder speichern

```python
import os
from pathlib import Path
from mineru_ocr import OCRClient

with OCRClient(db_dsn=os.environ["MINERU_OCR_DB_DSN"], preflight=False) as client:
    huelle = client.ocr(Path("dokument.pdf").read_bytes(), timeout=600)
    if huelle.success:
        Path("text.md").write_text(huelle.rendered_markdown, encoding="utf-8")
        print(huelle.readable_markdown)      # wie --re: HTML als Markdown
        print(huelle["middle_json"])         # Seiten und Blöcke je Teilstück
        for verweis, daten in huelle.images.items():
            ziel = Path(verweis)
            ziel.parent.mkdir(parents=True, exist_ok=True)
            ziel.write_bytes(daten)
    else:
        print(huelle.status, huelle.row.error)
```

### Asynchron

```python
import asyncio
import os
from pathlib import Path
from mineru_ocr import AsyncOCRClient

async def lesen():
    async with AsyncOCRClient(db_dsn=os.environ["MINERU_OCR_DB_DSN"]) as client:
        hashes = await client.ocr_start(
            [Path(p).read_bytes() for p in ("a.pdf", "b.png")]
        )
        async for huelle in client.as_completed(hashes, timeout=600):
            print(huelle.sha256, huelle.status)
        alle = await client.wait_all(hashes, timeout=600)
        fertig = await client.wait(hashes[0], timeout=600)
        neu = await client.get(hashes[0])
        direkt = await client.ocr(Path("a.pdf").read_bytes(), timeout=600)
        print(len(alle), fertig.success, neu.success, direkt.success)

asyncio.run(lesen())
```

In einer bereits laufenden Ereignisschleife, etwa Jupyter, stattdessen
`await lesen()` verwenden. Der Async-Client fragt RunPod nie. Seine Hüllen
tragen `raw` bereits geladen und halten keine Verbindung; neu lesen heißt
`await client.get(h)` statt `refresh()`. Bei vorzeitigem Abbruch eines
Async-Iterators schließt `contextlib.aclosing(...)` ihn zuverlässig.

## CLI-Referenz

`mineru-ocr PATH…` verbindet Abschicken, Warten und Textausgabe. Der Dateiaufruf
fragt RunPod nicht ab. Alle Hilfen, auch die der Unterkommandos, zeigen am Ende
die Paketversion (derzeit `mineru-ocr 1.0`).

| Option | Wirkung |
|---|---|
| `-t` | Volltext in eine temporäre UTF-8-Datei schreiben; nur deren Pfad auf stdout. |
| `-o PFAD` | Gesamte Ausgabe in diese Datei schreiben; nur deren Pfad auf stdout. Kein Verzeichnis und keine Eingabedatei als Ziel. Schließt `-t` aus. |
| `--re` | HTML-Inseln im Volltext als Markdown, Tabellen als GFM-Tabellen. Vorhandenes Markdown außerhalb von HTML, Code und Formeln bleiben zeichengetreu. |
| `--ra` | Vollständige Dokumentangaben plus `raw` als JSON **in eine Datei**: mit `-o` dorthin, sonst temporär mit Endung `.json`. stdout enthält nur den Pfad. Schließt `--re` aus. |
| `-d` | Bekannten Fortschritt je Datei auf stderr anzeigen. Reiht offene Dateien des Stapels vor dem Warten ein. stdout und Exit-Codes bleiben gleich. |
| `--timeout S` | Höchstens S Sekunden je offenem Dokument warten; endliche Zahl ab null. Ohne Angabe unbegrenzt. |
| `--tier STUFE` | `advanced` (Standard), `standard`, `basic`, `flash`; nur für neu eingereihte Dokumente. |
| `--ocr-mode MODUS` | `ocr` (Standard), `txt` (Textebene digitaler PDFs), `auto`; nur für neu eingereihte Dokumente. |
| `--image-analysis` / `--no-image-analysis` | Bildbeschreibungen an/aus; Standard an, wirkt nur in `advanced`. |
| `--help` | Hilfe anzeigen. |

Bei mehreren Eingaben beginnt jeder ausgegebene Text mit `# Datei: PFAD`.
`-t` und `-o` schreiben den ganzen Stapel in **eine** Datei. Auch ein erfolgreiches
`empty` legt eine leere Textdatei an. Dateien unbekannten Typs oder über der
Datenbankgrenze (`db.MAX_INPUT_BYTES`, 1 GB) werden in Ordnern mit Grund
übersprungen; ausdrücklich genannte solche Dateien ergeben Code 1.

Bilder und Ganzdokument-Formate innerhalb der Datenbankgrenze, aber über
`max_payload_bytes` (Standard: 7,4 MB), ergeben bei frischem Daemon-Herzschlag
und ohne gespeichertes Ergebnis auch im Ordner `TooLargeError` und Gesamtcode 1
([bekannte Einschränkung #66](https://github.com/ConstantinLevin/Mineroo/issues/66)).
Die übrigen Dateien laufen weiter, einschließlich Einreihen und Warten für
zulässige neue Dokumente bei frischem Herzschlag sowie Verarbeitung von Treffern.

`--ra` liefert bei einer Eingabe ein Objekt wie `huelle.to_dict(raw=True)`,
bei mehreren ein Array mit zusätzlichem `path` je Eintrag. Auch die Endzustände
`empty`, `failed` und `input_error` stehen darin; deren Meldungen und Exit-Codes
bleiben erhalten. Dateien ohne Endergebnis können keinen Ergebniseintrag liefern.

Mit `-d` erscheinen Datenbanktreffer, gespeicherte Eingaben, wartende oder laufende
Versuche, Wiederholungen und Zeit im Schritt sowie insgesamt. Lokale aktuelle
Daemons liefern zusätzlich den Chunk-Plan, gesendete und fertige Chunks und
RunPods `IN_QUEUE`/`IN_PROGRESS`; fehlende Angaben heißen `unbekannt`.
Am Terminal aktualisieren sich Zeilen an Ort und Stelle; große Stapel wechseln
alle fünf Sekunden zwischen Ausschnitten. Umgeleitetes stderr enthält nur
sachliche Änderungen, keine Uhrenticks oder Steuerzeichen.

### Exit-Codes des Dateiaufrufs

| Code | Bedeutung |
|---|---|
| 0 | Erfolg, auch `empty` (Grund auf stderr, kein Text). |
| 1 | Betriebs-, Datei- oder Ausgabefehler; auch beschädigtes gespeichertes Ergebnis. |
| 2 | Ungültiger Aufruf oder Optionen, etwa `--ra --re` oder `-t -o`. |
| 3 | Mindestens ein `failed` oder `input_error`, je Datei mit Grund. |
| 4 | Wartefrist abgelaufen; Job bleibt bestehen. |
| 5 | Kein Daemon mit frischem Herzschlag für eine neue/offene Datei; diese wird nicht eingereiht. |
| 130 | Strg-C; beendet nur den CLI-Aufruf, keine eingereihten Jobs. |

Bei mehreren Fehlern gilt **1 vor 5 vor 4 vor 3 vor 0**. Bereits erfolgreiche
Texte werden trotzdem ausgegeben. Ist die Lebendigkeit des Daemons nicht lesbar,
ist das ein Betriebsfehler (1). Eine Wartefrist schützt auch dann, wenn der Daemon
nach der Prüfung ausfällt.

### Frühere Kommandos

`ocr`, `status`, `show`, `wait`, `markdown`, `retry` und `preflight` sind keine
CLI-Kommandos mehr. Der frühere Ablauf aus Abschicken, Warten und Markdown wird
zu `mineru-ocr PATH…`; die Vordergrundform von `daemon` wird zu
`daemon start X`, `daemon stop X` und `daemon list`. Struktur, Bilder,
Dokumentzustand, Wiederholungen und Preflight erreichst du über Python.

## Python-Referenz

### Methoden und Lese-Einstellungen

| Methode | Ergebnis |
|---|---|
| `ocr_start(data, *, tier=None, ocr_mode=None, image_analysis=None)` | Ein `Submitted`, bei einer Liste von Bytes eine Liste. Prüft erst alle Eingaben, bevor etwas eingereiht wird. |
| `ocr(data, timeout=None, *, tier=None, ocr_mode=None, image_analysis=None)` | Ein Dokument abschicken und seine Hülle abwarten. |
| `wait(sha256, timeout=None)` | Hülle zu jedem Endzustand, auch `failed` und `input_error`. |
| `wait_all(hashes, timeout=None)` | Alle Hüllen in der Reihenfolge der Frage; doppelt genannte Hashes bleiben doppelt. |
| `as_completed(hashes, timeout=None)` | Iterator mit jeder fertigen Hülle; jeden unterschiedlichen Hash einmal. |
| `get(sha256)` | Hülle zum gespeicherten Ergebnis, ohne Warten. |
| `document(sha256)` | `DerivedStatus` ohne `raw`; `phase` ist `Phase.TERMINAL`, `WAITING`, `RUNNING` oder `ENDED_WITHOUT_RESULT`. |

`ocr_start`, `ocr`, `wait`, `wait_all` und `get` gibt es asynchron mit `await`;
`as_completed` liefert dort einen Async-Iterator. Verwaltungsmethoden gibt es
nur synchron. Jeder Client gehört einem Aufrufer bzw. einer Async-Aufgabe.

Die Lese-Parameter haben dieselben Werte und Standards wie die CLI. Sie gelten
nur beim **neuen Einreihen**: Ein Treffer und eine bereits gespeicherte Eingabe
behalten ihre Einstellungen. Beispiel im geöffneten Client:
`client.ocr_start(daten, tier="advanced", ocr_mode="txt", image_analysis=False)`.
Ganzdokument-Formate (Office, OpenDocument, RTF, EPUB, OFD, HTML, MHTML) liest der
Worker immer in `flash`; am Ergebnis steht `read_settings=None`.

### Was in der Hülle steckt

| Zugriff auf `OCRWrapper` | Inhalt |
|---|---|
| `.sha256`, `.status` | Hash und Endzustand: `done`, `empty`, `failed`, `input_error`. |
| `.success` | `True` bei `done` und `empty`. |
| `.mime`, `.size_bytes`, `.page_count` | Vom Job bestimmte Angaben; können fehlen, Ganzdokumente haben keine Seitenzahl. |
| `.rendered_markdown` | Ganze Vollfassung; wirft, wenn diese fehlt oder unvollständig ist. |
| `.readable_markdown` | Vollfassung mit HTML als Markdown, wie `--re`. |
| `huelle["markdown"]` | Gewöhnliches MinerU-Markdown, ohne die Vollfassung der Randbereiche. |
| `huelle["middle_json"]` | Je Teilstück Seiten und Blöcke, mit Seitenzahlen des Originals. |
| `huelle["structured_content"]`, `huelle["model_output"]` | Weitere Inhalte der zusammengesetzten Antwort. Jedes Element von `raw` ist so erreichbar; fehlt es, kommt `KeyError`. |
| `"markdown_full" in huelle` | Prüft die Anwesenheit des Schlüssels, nicht die Vollständigkeit seines Werts. |
| `.images` | `{verweis: bytes}` unter dem Verweis aus dem Text; `huelle["images"]` enthält dagegen die gespeicherte Base64-Kodierung. |
| `.raw` | Vollständige gespeicherte Antwort, auch bei Fehlschlägen. |
| `.to_dict(raw=True)` | Dokumentangaben plus `raw`, wie das JSON bei `--ra`. Ohne Argument fehlt `raw`. |
| `.mineru_version`, `.read_settings`, `.notes` | Gemeldete Version (bei mehreren die niedrigste), **bestellte** Lese-Einstellungen und Vermerke. Tatsächlich gemeldete Einstellungen stehen je Teilstück in `raw["envelopes"]`; Abweichungen in `notes`. |
| `.refresh()` | Im synchronen Client die Zeile neu lesen und zwischengespeicherte Darstellungen verwerfen. |

### Fehlschläge und Wiederholen

`failed` und `input_error` sind Endzustände. `row.error` nennt den Grund,
`raw` bewahrt, was ankam. Ergebniszugriffe wie `huelle["markdown"]` werfen dann
`IncompleteResultError`; auch die Schlüsselprüfung mit `in` braucht ein
erfolgreiches Ergebnis.

Erneutes Abschicken derselben Bytes ist **auch bei `failed` ein Treffer**.
`retry(sha256)` ist der ausdrückliche Wiederanlauf; `retry_all(hashes)` liefert
ein Dict `{hash: RetryOutcome}`. Die Ausgänge:

| `RetryOutcome` | Was passiert |
|---|---|
| `HAS_RESULT` | Nichts: `done`, `empty` **oder `input_error`** steht bereits da. |
| `UNDERWAY` | Nichts: Der Job wartet oder läuft. |
| `RESET` | `failed`-Ergebnis entfernt, gescheiterten Job zurückgesetzt. |
| `REQUEUED` | Neuen Job eingereiht, etwa nach Abbruch oder Verlust des alten Jobs; vorhandenes `failed`-Ergebnis entfernt. |

**`RESET` und `REQUEUED` kosten erneut Geld:** Das Dokument wird vollständig neu
verarbeitet. `input_error` wird mit `retry` nicht neu gelesen. Ohne Hash-Liste
nimmt `retry_all()` jede stehende Eingabe, also gescheiterte und offene
Dokumente; nach einem Absturz besser gezielt mit Hashes wiederholen.

```python
import os
from pathlib import Path
from mineru_ocr import OCRClient

with OCRClient(db_dsn=os.environ["MINERU_OCR_DB_DSN"], preflight=False) as client:
    h = client.ocr_start(Path("dokument.pdf").read_bytes())
    print(client.retry(h))
    print(client.retry_all([h]))
```

### Übersicht und RunPod-Prüfung

| Methode (nur synchron) | Inhalt |
|---|---|
| `status()` | Summen unter `documents`, `readings`, `jobs`, außerdem `upgrade_cases`, `balance`, `daemon_alive`. Braucht einen RunPod-Schlüssel für die Kontofrage. |
| `preflight()` | Endpoint, Image, EWR-Rechenzentren und Sollwerte prüfen. Braucht Endpoint und Schlüssel. Berichtet nur, verändert keine RunPod-Ressource. |
| `credit()` | Kontostand live, braucht den Schlüssel. |
| `account()` | Kontoangaben, braucht den Schlüssel. |
| `workers()` | Worker des Endpoints, braucht Endpoint und Schlüssel; Geheimnisse werden maskiert. |

`OCRClient()` nimmt seine Verbindung aus der Konfiguration. Beim Betreten prüft
er das Schema und, sofern Endpoint und Schlüssel vorhanden sind, den Endpoint.
Weitere Konstruktorparameter sind `db_dsn`, `endpoint_id`, `api_key`,
`preflight=True`, `strict=True`, `min_balance=1.0`, `schema_check=True`.
`strict=False` lässt den Preflight Abweichungen berichten, statt sie als Fehler
abzubrechen. `schema_check=False` erlaubt die Prüfung vor dem Schema-Setup;
die Datenbank selbst muss bereits existieren. Zum Lesen und Abschicken die
Schemaprüfung eingeschaltet lassen. `AsyncOCRClient` nimmt nur
`db_dsn` als Konstruktorparameter.

### Wichtige Fehlerklassen

Alle folgenden Klassen sind aus `mineru_ocr` importierbar und erben von
`MineruOcrError`:

| Klasse | Anlass |
|---|---|
| `ConfigurationError` | Fehlende/ungültige Einstellungen, einschließlich benötigtem Endpoint oder Schlüssel. |
| `UnsupportedTypeError`, `TooLargeError` | Beim Abschicken: nicht unterstützter Inhalt oder Größenlimit überschritten. |
| `DocumentNotFound` | Unbekannter Hash; bei `get()` auch ein noch offenes Dokument. |
| `WaitTimeoutError` | Wartefrist abgelaufen; zugleich `TimeoutError`, offene Hashes unter `.pending`. |
| `IncompleteResultError` | Ergebnis gescheitert, beschädigt oder Vollfassung fehlt. |
| `DatabaseError` | Datenbank nicht erreichbar oder Schema fehlt/passt nicht; Schema mit `mineru-ocr setup` prüfen. |

## Konfiguration

Datei: `~/.mineru_ocr/config.toml`; `MINERU_OCR_CONFIG` wählt eine andere.
Eine fehlende Standarddatei ist erlaubt, eine ausdrücklich gewählte fehlende
Datei nicht. Unbekannte TOML-Schlüssel sind Fehler. Rangfolge:
**ausdrückliches Argument → `MINERU_OCR_<NAME>` → Datei → Paketvoreinstellung**.
Für Datenbank und Endpoint gibt es keine Paketvoreinstellung.

Für den Einstieg in der Datei `db_dsn` setzen und `endpoint_id` mit der eigenen
Endpoint-Kennung belegen. **Der RunPod-Schlüssel bleibt in der lokalen Datei
`~/.runpod/config.toml`, Feld `apikey`**, nicht im Repository oder
in die Mineroo-Konfiguration. Die Python-API erlaubt außerdem die ausdrückliche
Übergabe als `api_key`.

Folgendes TOML ist eine gültige Konfiguration für die Beispiel-Testdatenbank,
ohne Endpoint und Schlüssel; zum Lesen gespeicherter Ergebnisse reicht es:

```toml
db_dsn = "host=localhost port=5437 dbname=mineru_ocr_test"
max_pages_per_chunk = 20
max_payload_bytes = 7400000
job_timeout_seconds = 300
queue_timeout_seconds = 1800
runpod_cache_seconds = 60
```

`setup`, CLI-Dateiaufruf und die Python-Methoden zum Abschicken, Abholen, Warten
und Wiederholen brauchen nur Postgres (im Sync-Client gegebenenfalls
`preflight=False`). Erst der Daemon führt die eingereihten Jobs über RunPod aus.
Bei `setup` sowie `daemon start/stop/list` überschreibt `--dsn` die Verbindung;
beim Dateiaufruf gilt `MINERU_OCR_DB_DSN` bzw. die Datei.

### Sollwerte und Grenzen

| TOML-Schlüssel | Paketvoreinstellung / Bedeutung |
|---|---|
| `endpoint_id`, `db_dsn` | Keine; vom Betreiber zu setzen. |
| `expected_image_tag` | Keiner; Image-Herkunft dann „nicht prüfbar“. |
| `workers_min`, `workers_max` | 0 / 10. |
| `idle_timeout` | 5 Sekunden. |
| `execution_timeout_ms` | 300000 Millisekunden. |
| `allowed_data_centers` | `EU-CZ-1`, `EU-FR-1`, `EU-NL-1`, `EU-RO-1`, `EU-SE-1`, `EUR-IS-1`, `EUR-IS-2`, `EUR-NO-1`. Konfigurierbare EWR-Liste. |
| `max_pages_per_chunk`, `max_payload_bytes` | 20 Seiten / 7400000 rohe Bytes je Teilstück. |
| `job_timeout_seconds` | 300 Sekunden Bearbeitungsbudget je Teilstück. |
| `queue_timeout_seconds` | 1800 Sekunden ununterbrochen `IN_QUEUE`; positiv und endlich. |
| `runpod_cache_seconds` | 60 Sekunden; gemeinsamer Cache für Kontostand und Image-Herkunft im Daemon, auch für Fehler. Positiv und endlich. |
| `[endpoint_env]` | Tabelle der Sollwerte für Endpoint-Variablen: `MINERU_MAX_CONCURRENCY = "1"`; `MINERU_VIRTUAL_VRAM_SIZE` hat keinen Sollwert und wird nur berichtet. |

Alle genannten Schlüssel außer `[endpoint_env]` haben eine Umgebungsvariable,
etwa `MINERU_OCR_QUEUE_TIMEOUT_SECONDS`. Bei `MINERU_OCR_ALLOWED_DATA_CENTERS`
sind die Kennungen durch Kommas getrennt. Weitere interne Einstellungen und
ihre Herleitung stehen in [`mineru_ocr/config.py`](mineru_ocr/config.py); nicht
jedes Feld von `Settings` ist ein TOML-Schlüssel.

Der Preflight prüft Image-Herkunft, Sollwerte und die gemeldete Umgebung aller
zuletzt gelaufenen Worker, auch beendeter. Er berichtet FlashBoot. Eine leere
Rechenzentrumsliste aus RunPod v2 bedeutet freie Regionswahl und ist eine
Abweichung. Fehlende Angaben heißen **„nicht prüfbar“**, nicht „in Ordnung“. Rechenzentren liest er
über RunPods REST-API v2, andere Endpoint-Angaben über v1. Die konfigurierte
Liste muss jedes für den Endpoint eingestellte Rechenzentrum enthalten;
Image-eigene Variablen sind in der Worker-Umgebung nicht sichtbar. Geheimnisse
werden als `***` gezeigt, auch in `repr(Settings)`. `KEY`, `SECRET`, `TOKEN`
und `PASSWORD` zählen dabei auch als Teilwort (also auch in `MONKEY` und
`MAX_TOKENS`); weitere Namensteile stehen in `config.py`. Da RunPod-Antworten
bereits maskiert sind, ist bei solchen Variablen nur das Vorhandensein prüfbar,
nicht die Wertgleichheit; ein vorhandener geheimer Wert gilt als „nicht prüfbar“.
Eine eigene Serialisierung von `Settings` maskiert nicht automatisch.

Nach Ablauf von `queue_timeout_seconds` fragt der Daemon ein letztes Mal ab:
fertige Antwort übernehmen, sonst `/cancel` und die bestehende Wiederholung.
Ein Wechsel nach `IN_PROGRESS` beendet die Wartephase; erneutes Einreihen
beginnt sie neu. Fehlgeschlagene Abfragen setzen sie nicht zurück. Das
Bearbeitungsbudget bleibt getrennt; nach einer Kontopause (HTTP 402) beginnen
beide Fristen beim erneuten Warten auf denselben Job neu.

Der Cache teilt laufende RunPod-Abfragen zwischen OCR-Plätzen. Ohne gelesenen
ausreichenden Kontostand wird nicht weitergearbeitet. HTTP 402 verlangt eine
nach der Ablehnung begonnene Kontofrage; bestätigt niedriges Guthaben wird nach
`balance_recheck_seconds` (intern 900 Sekunden) erneut geprüft. Clientmethoden
fragen direkt, ohne diesen Daemon-Cache.

## Daemons

```bash
mineru-ocr daemon start 3
mineru-ocr daemon start 1 --concurrency 48
mineru-ocr daemon list
mineru-ocr daemon stop 2
mineru-ocr daemon start --help
```

`start X` startet X vom Terminal gelöste Prozesse, jeweils mit OCR-, Audio- und
Wartungs-Worker. Vorher werden Endpoint, Schlüssel und Schema geprüft.
`ELEVENLABS_API_KEY` muss gesetzt sein; ein kostenfreies `GET /v1/user`
prüft den ElevenLabs-Zugang vor dem ersten Kindprozess. Der Schlüssel braucht
Lesezugriff auf diese Route. Bei ungültigem Schlüssel, fehlendem Zugriff oder
Unerreichbarkeit startet kein Kind; ein Timeout wird einmal wiederholt.
Kontodaten und Antworttexte werden nicht ausgegeben. Standard
sind 24 OCR-Plätze je Prozess (`ocr_concurrency`); `--concurrency N` überschreibt
das. X und N müssen ganze Zahlen ab 1 sein.

`stop X` beendet bis zu X nach der Summe laufender OCR- und Audio-Jobs,
die am wenigsten belegten zuerst, bei Gleichstand nach aufsteigender PID.
Wartungs-Jobs und wartende Jobs zählen nicht mit. Laufende Dokumente
bekommen die Nachfrist `shutdown_graceful_timeout` (intern 30 Sekunden); danach
greift die vorhandene Wiederholungsregel. Ist das Versuchsbudget erschöpft,
endet das Dokument `failed`. Das JSON nennt `requested`, `stopped` und
`running_before`. Es wird bis zum Prozessende einschließlich Zombie-Zustand
gewartet.

`list` zeigt die Daemons **dieses Rechners für die gewählte Datenbank**,
die Warteschlange gilt datenbankweit:

| JSON-Feld | Bedeutung |
|---|---|
| `count`, `daemons` | Zahl lokaler laufender Daemon-Prozesse und ihre Einträge. |
| `daemons[].pid`, `daemons[].socket`, `daemons[].log_path`, `daemons[].stopping` | Prozessnummer, Kontrollsocket, Protokollpfad und ob er herunterfährt. |
| `daemons[].occupied`, `daemons[].available` | Belegte OCR-Plätze / konfigurierte OCR-Kapazität; keine Wartungsplätze. |
| `slots.occupied`, `slots.available` | Summen der Belegung / Kapazität über die lokalen Daemons. |
| `queue.waiting`, `queue.running` | OCR-Jobs in Procrastinate (`todo`/`doing`), einschließlich später vorgemerkter Jobs. |
| `lanes.ocr`, `lanes.audio` | Je Lane `slots.occupied` / `slots.total` für lokale belegte Plätze / gesamte Kapazität sowie `queue.waiting` / `queue.running` für die datenbankweite Warteschlange. |
| `daemons[].lanes.ocr`, `daemons[].lanes.audio` | `occupied` / `total` je Lane dieses Prozesses, ohne Wartungsplätze. |

**`available` ist die Kapazität, nicht die Zahl freier Plätze:** Bei sechs
Plätzen und einem laufenden Job steht `occupied=1`, `available=6`.
Die bisherigen Felder `slots`, `queue`, `occupied` und `available` beziehen sich
weiter auf OCR. Ohne Daemons sind die Lane-Kapazitäten null; offene Jobs bleiben
in ihrer Warteschlange sichtbar. Bei älteren Daemons ohne Lane-Auskunft steht
die unbekannte Audio-Kapazität als JSON-`null`; nach Neustart ist sie bekannt.

Ein laufender Daemon antwortet am Kontrollsocket und hat frische
Worker-Herzschläge (jünger als 30 Sekunden, Datenbankuhr). Eine alte Socketdatei
oder Worker-Zeile reicht nicht. Socket und eigene Logs liegen im privaten
Verzeichnis `/tmp/mineru-<uid>-<kennung>/`; keine PID-Kartei. Die Kennung kommt
aus PostgreSQLs Clusterkennung und Datenbank-OID, unabhängig von IPv4, IPv6 oder
Unix-Socket. Die Rolle braucht `EXECUTE` auf `pg_control_system()`.

Nach einem Rechnerneustart startet nichts von selbst. Auch Bibliothek und
Dateiaufruf starten keine Daemons. Die Bibliothek reiht ohne Daemon trotzdem
ein und warnt im Logger `mineru_ocr.client`; die CLI lehnt neue/offene Dateien
ohne frischen Herzschlag ab. Beim Wechsel von der früheren adressabhängigen
Daemon-Kennung zunächst mit der alten Version stoppen, dann mit der neuen
starten.

## Audio

```bash
mineru-ocr aufnahme.m4a --timeout 600
mineru-ocr aufnahme.m4a -o transkript.md
mineru-ocr aufnahme.m4a --ra -o antwort.json
mineru-ocr eingang/ -o texte.md --timeout 600  # Audio und Dokumente gemischt
```

MP3, M4A/AAC, WAV, FLAC, OGG/Opus und AIFF werden am Inhalt erkannt, auch bei
anderer Dateiendung. Jede Datei geht ungeteilt in einer synchronen Anfrage an
Scribe v2, mit Sprechererkennung und Geräuschmarkierungen. Je Sprecherwechsel
steht ein Absatz `Sprecher N:`, ab 1 in Reihenfolge des ersten Auftretens;
`[lacht]` und andere Geräusche bleiben erhalten. Ohne erkanntes Wort endet die
Datei `empty`. Gleiche Bytes lesen den Cache. Gemischte Stapel bleiben in
Eingabereihenfolge.

Der Daemon liest den Schlüssel aus `ELEVENLABS_API_KEY` und reicht ihn über sein
anonymes Startrohr weiter. Der Schlüssel gehört weder in `config.toml` noch in
Ausgaben, Protokolle oder das Repository. `.env` ist ignoriert und wird von
Mineroo nicht automatisch geladen. Abschicken und Abholen brauchen ihn nicht.
Diese Felder ergänzen die bestehende Mineroo-Konfiguration:

```toml
audio_concurrency = 2
audio_region = "us"
```

Die Umgebung kann sie als `MINERU_OCR_AUDIO_CONCURRENCY` und
`MINERU_OCR_AUDIO_REGION` überschreiben. Jeder Daemon hat neben OCR und Wartung
einen Audio-Worker mit eigenen Plätzen. `audio_region = "us"` ist die ausdrückliche
USA-Einstellung, vom Eigentümer gewollt
([#69](https://github.com/ConstantinLevin/Mineroo/issues/69): „ja das passt mit er
verarbeitung.“). RunPods EWR-Regel bleibt bestehen. EU-Verarbeitung und
Abschalten der Speicherung bei ElevenLabs setzen nach #69 Enterprise voraus
und sind hier nicht vorgesehen.

Gemessen am 30.10.2026: rund **0,40 USD je Audiostunde**, über das Tarifkontingent
(Free damals 10.000 Credits/Monat, etwa 2,5 Stunden), darüber Pay as you go.
Das sind historische Messwerte aus #69. Neue Läufe kosten Credits, Treffer nicht.
Ein Verbindungsabbruch nach der Berechnung kann einen erneuten bezahlten Lauf
nötig machen, weil die Transkript-ID erst mit der Antwort kommt.

Die Hülle liefert Audio über `rendered_markdown` und unverändert über
`readable_markdown`. `--re`, `-t`, `-o` und `--ra` funktionieren wie bei Dokumenten.
`raw.response` enthält die vollständige ElevenLabs-Antwort mit Wortzeiten,
`raw.parameters` die gesendeten Parameter. Das Ergebnis steht in derselben
Tabelle mit `endpoint_id = "elevenlabs"`; Seitenzahl, MinerU-Version und
OCR-Leseeinstellungen bleiben leer. Die Grenze ist `db.MAX_INPUT_BYTES` (1 GB),
RunPods Anfragegrenze gilt nicht für Audio. Video ist nicht unterstützt.

Audio nutzt dieselbe zentrale Fehler- und Wiederholungsregel wie OCR: Ein
fehlender oder ungültiger Schlüssel endet als Konfigurationsfehler (`failed`),
eine unlesbare Datei (HTTP 422) als `input_error` mit dem maskierten Grund aus
der Antwort. HTTP 429/5xx, Verbindungsabbruch und Zeitüberschreitung werden bis
zum Versuchsbudget wiederholt; danach steht `failed`. Bei erschöpftem
Kontingent wartet der laufende Versuch und fragt nach `balance_recheck_seconds`
(intern 900 s) erneut an, ohne einen Versuch zu verbrauchen. `retry` und
`retry_all` lesen gescheitertes Audio wieder in der Queue `audio`. Beim
Herunterfahren wird neu eingereiht, solange Versuche übrig sind; im letzten
Versuch steht `failed` mit Abbruchvermerk.

Die Anfragefrist beträgt 300 Sekunden plus ein Zehntel der Aufnahmedauer.
`ffprobe` ermittelt die Dauer lokal mit einer Frist von zehn Sekunden. Fehlt es
oder lässt sich die Dauer nicht lesen, wird mit 8 kbit/s geschätzt;
die endgültige Datei-Abweisung kommt von ElevenLabs. Bei einer Stunde Audio
sind es mit gelesener Dauer 660 Sekunden (Messung #69: 59 Minuten in 43 Sekunden).

`daemon list` zeigt
OCR und Audio getrennt; `daemon start` prüft den ElevenLabs-Zugang kostenfrei.
Mit `-d` zeigt Audio auf stderr das Warten auf einen Daemon-Platz, beim lokalen
aktuellen Daemon `ElevenLabs verarbeitet` mit Schritt- und Gesamtzeit sowie den
Endzustand. Chunk- und RunPod-Angaben entfallen für Audio, auch am schmalen
Terminal. Bei älteren oder entfernten Daemons ist nur der Datenbankstand bekannt.
Ohne `-d` bleiben Textausgabe und Exit-Codes unverändert.

## Formate und Verarbeitung

Unterstützt werden PDF, Bilder (PNG, JPEG, GIF, BMP, TIFF, WebP), Office (DOC,
XLS, PPT, DOCX, XLSX, PPTX), OpenDocument (ODT, ODS, ODP), RTF, EPUB, OFD, HTML
und MHTML. Erkannt wird **am Inhalt**, nicht am Dateinamen. Text, CSV und
beliebige ZIPs werden abgewiesen. Mehrbild-Bilddateien enden `input_error` mit
dem Hinweis, sie als PDF einzureichen.

Ganzdokument-Formate gehen ungeteilt hinaus und müssen unter die Uploadgrenze
passen. Bei PDFs schneidet `pypdfium2` Teilstücke; `pypdf` ist im Projekt wegen
der gemessenen Größenaufblähung verboten. Vor `%PDF-` sind ASCII-Leerraum und
vollständige UTF-8-BOMs erlaubt; andere Vorspänne bleiben abgewiesen. HTML in
UTF-16LE/BE wird für den Worker nach UTF-8 umkodiert. Hash und gespeicherte
Eingabe bleiben in beiden Fällen die Originalbytes.

Zu große Antworten führen zu weiterer Teilung des betroffenen PDF-Teilstücks;
erst eine einzelne Seite mit zu großer Antwort endet `failed`. Eine einzelne
PDF-Seite über der Uploadgrenze wird als JPEG gerendert (200 dpi, längste Kante
höchstens 3500 Pixel, Qualität 90). `notes` nennt solche Seiten unter
`seiten_gerendert`, null-basiert. Passt auch das Bild nicht, endet es
`input_error`. Bereits bezahlte Antworten werden bewahrt; erneut gesendete
Teilstücke kosten erneut Geld.

## Tests und Typprüfung

Für Entwicklung in einer eigenen Umgebung: `python -m pip install -e '.[dev]'`.
Die Offline-Suite einschließlich Datenbanktests und die Typprüfung:

```bash
flock /tmp/mineroo-testdb.lock python -m pytest -q -m "not live" -ra > pytest.log 2>&1
cat pytest.log
python -m mypy mineru_ocr tests
```

`flock` muss als Systemwerkzeug verfügbar sein. Datenbanktests verwenden
**ausschließlich `mineru_ocr_test` auf Port 5437**; die Testrolle steht in
[`tests/conftest.py`](tests/conftest.py). Diese Tests leeren ihre Tabellen,
deshalb Läufe mit dem gemeinsamen Lock serialisieren und niemals die
Produktionsdatenbank als Testziel einsetzen.

Fehlt Postgres, werden die `db`-Tests übersprungen. **Den Skip-Zähler prüfen:**
Ein grüner Lauf mit solchen Skips belegt die Datenbankseite nicht. `live`-Tests
sprechen den echten Endpoint an und kosten Geld; nur mit ausdrücklicher
Zustimmung. Die Typprüfung umfasst **Paket und Tests**. Zum Arbeiten aus dem
Checkout ist auch `python -m mineru_ocr.cli --help` möglich; die editierbare
Installation zeigt immer auf den Checkout, aus dem sie gemacht wurde.

## Aufbau und Hintergründe

Vier Entscheidungen tragen das Paket:

- **Ein Buch:** `documents` enthält ausschließlich Endzustände. Offene Eingaben
  liegen in `inputs`; der laufende Zustand wird aus ihrem Procrastinate-Job
  abgeleitet, nicht daneben gespeichert.
- **`empty` ist ein eigenes, gültiges Ergebnis:** Kein Inhalt erkannt heißt
  weder `done` mit unsichtbarem Verlust noch Fehler mit endloser Wiedervorlage.
- **Ein Dokument hinein, ein Dokument heraus:** Teilstücke werden vor dem
  Schreiben nach Postgres zusammengesetzt; keine Chunk-Tabelle. Fehlende Seiten
  oder benötigte Antwortdateien führen zu `failed` statt stillem Teiltext.
- **Alles holen, alles persistieren, nie filtern:** Unbekannte Antwortdateien
  bleiben in `extras`. Der Transport `tarball_b64` holt alles. Die Strecke nutzt
  `/run` und `/status`, keinen `/runsync`-Betriebsweg. Schlüssel reisen im
  `Authorization`-Header, nie in URLs. Die Endpoint-Prüfung verlangt die
  konfigurierten EWR-Rechenzentren.

Das Paket spricht den MinerU-4-Job-Vertrag. Aufgenommene Antworten aus dem
Betriebsweg `advanced` mit OCR liegen unter
[`artifacts/2026-10-26-mineru-4.0.12-endpoint/`](artifacts/2026-10-26-mineru-4.0.12-endpoint/)
und werden offline abgespielt.

Weiterlesen:

- [`skills/mineroo/SKILL.md`](skills/mineroo/SKILL.md): Kurzer Einstieg für Agenten
  mit Verweisen auf fünf [Referenzen](skills/mineroo/references/): ein Dokument,
  viele Dokumente, CLI, Python sowie Technik und Betrieb.
- [`CLAUDE.md`](CLAUDE.md): Aufbau und Arbeitsregeln für die Entwicklung.
- [`docs/README.md`](docs/README.md): historische Betriebserfahrung,
  Kalibrierung und Entscheidungen bis MinerU 3.4; kein aktuelles Runbook.
- [`plans/2026-08-05-ocr-service-plan.md`](plans/2026-08-05-ocr-service-plan.md):
  ursprünglicher Service-Entwurf.
- [`plans/2026-08-06-procrastinate-feature-verifikation.md`](plans/2026-08-06-procrastinate-feature-verifikation.md):
  Belege zu Procrastinate, mit Nachtrag zur verwendeten Version.
- [Mineroo#1](https://github.com/ConstantinLevin/Mineroo/issues/1): Spec des
  MinerU-4-Umbaus; Aufgaben im [Issue-Tracker](https://github.com/ConstantinLevin/Mineroo/issues),
  Verfahren unter [`docs/agents/`](docs/agents/).

Für die Benutzung gilt die im aktuellen Code vorhandene Schnittstelle; ältere
Pläne und historische Skripte unter `scripts/` erklären ihre Herkunft.
