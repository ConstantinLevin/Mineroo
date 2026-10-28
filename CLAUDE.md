# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Diese Datei gilt für Mineroo, den allgemeinen MinerU-Wrapper für OCR auf Abruf
(das Paket `mineru_ocr`). Aufgaben sind GitHub-Issues: `ConstantinLevin/Mineroo`
für das Paket, der Fork `ConstantinLevin/mineru-runpod` für den Worker (siehe
„Agent skills" unten und `docs/agents/`). Hier steht ausschließlich, was für
diesen Code gilt und was man nicht durch Hinsehen findet.

Die Projektsprache ist Deutsch: Docstrings, Kommentare, Pläne und Dokumentation
sind durchweg deutsch, ebenso die internen Bezeichner (`_stuecke_verarbeiten`,
`_endzustand_uebersetzen`). Neuer Code schreibt sich genauso, ohne Umlaute in
Docstrings (`ue`, `ae`, `oe`) — das ist die durchgehaltene Konvention des Pakets.

---

## Kommandos

**Der Interpreter ist die Miniconda-Base, und es gibt keine virtuellen
Umgebungen.** Das ist die Umgebung des Benutzers, keine Vorliebe dieses Projekts:
Wer hier ein venv anlegt, arbeitet an einer Installation vorbei, in der die
Abhängigkeiten bereits liegen.

```bash
/opt/homebrew/Caskroom/miniconda/base/bin/python3 -m pytest -q -m "not live"                      # alle Tests
/opt/homebrew/Caskroom/miniconda/base/bin/python3 -m pytest tests/test_chunking.py -q   # eine Datei
/opt/homebrew/Caskroom/miniconda/base/bin/python3 -m pytest -q -m "not live" -k "seitenversatz"       # ein Test nach Namensteil
/opt/homebrew/Caskroom/miniconda/base/bin/python3 -m pytest -q -m "not live" -m "not db"              # ohne Datenbank
/opt/homebrew/Caskroom/miniconda/base/bin/python3 -m mypy mineru_ocr tests              # Typprüfung
```

Die Typprüfung läuft **über `mineru_ocr` UND `tests`**, nicht nur über das Paket.
Der Grund steht in `pyproject.toml` an der zweiten mypy-Ausnahme: `tests/` hat
kein `__init__.py`, mypy führt die Dateien deshalb als Module der obersten Ebene,
und eine Ausnahme, die auf `tests.test_pdf` lautete, ginge still ins Leere. Nur
der Lauf über beide Pfade deckt das auf.

Abdeckung, falls gebraucht:

```bash
/opt/homebrew/Caskroom/miniconda/base/bin/python3 -m coverage run -m pytest -q -m "not live"
/opt/homebrew/Caskroom/miniconda/base/bin/python3 -m coverage report --include="mineru_ocr/*"
```

Es gibt keinen Build-Schritt und keinen Linter außer mypy. `pyproject.toml`
definiert das Kommando `mineru-ocr` unter `[project.scripts]`; es ist als Befehl in
die Miniconda-Base installiert, in der seine Abhängigkeiten liegen (siehe „Das
Kommando selbst“). Gearbeitet und getestet wird trotzdem aus dem Verzeichnis
heraus.

**Testausgabe immer vollständig in eine Datei schreiben, bevor gefiltert wird.**
Also `python3 -m pytest -q -m "not live" > /pfad/zur/ausgabe.txt 2>&1`, dann die Datei lesen
oder gezielt mit `grep` durchsuchen. Der Lauf selbst geht nie durch einen Filter:
Ein gefilterter Lauf zeigt nur, was der Filter durchlässt, und ein zweiter Lauf,
um „den Rest" zu sehen, kostet die Zeit nochmals und kann anders ausgehen.

### Die zwei Kennzeichnungen — und warum ein grüner Lauf trügen kann

`pyproject.toml` definiert zwei pytest-Marker, und beide ändern, was ein
bestandener Lauf überhaupt beweist:

- **`db`** braucht die laufende Postgres-Instanz auf **Port 5437** mit der
  Datenbank **`mineru_ocr_test`** (DSN in `tests/conftest.py`, Konstante
  `TEST_DSN`). Ist die Instanz nicht da, **überspringt** `conftest.py` diese
  Tests, statt sie scheitern zu lassen — ein fehlender Server ist keine Aussage
  über den Code. Die Kehrseite: Ein „alles grün" ohne laufende Instanz hat die
  gesamte Datenbankseite nie ausgeführt. Wer die Zahl der übersprungenen Tests
  nicht ansieht, hält einen halben Lauf für einen ganzen.
  `psql` liegt unter `/Applications/Postgres.app/Contents/Versions/latest/bin/psql`,
  nicht im `PATH`. Die Datenbank `mineru_ocr` enthält echte personenbezogene Daten
  und wird nur lesend geöffnet, mit
  `PGOPTIONS='-c default_transaction_read_only=on'`; Tests laufen ausschließlich
  gegen `mineru_ocr_test`.
- **`live`** spricht den echten RunPod-Endpoint an und **kostet Geld**. Diese
  Tests laufen nur nach ausdrücklicher Zustimmung des Benutzers, nie beiläufig
  und nie „zur Sicherheit".

### Das Kommando selbst

`mineru-ocr PATH…` schickt Dateien ab, wartet synchron und gibt den ganzen Text
(`rendered_markdown`) auf stdout aus. `-t` schreibt ihn in eine temporäre Datei,
`-o PFAD` in eine bestimmte; stdout nennt dann nur deren Pfad. Ein Stapel steht
in einer Datei, mit `# Datei: PFAD` vor jedem Text. Ordner werden rekursiv nach
Pfad sortiert gelesen. Explizite Dateifehler halten übrige Dateien nicht auf;
fremde und zu große Dateien in Ordnern werden mit Grund übersprungen.

```bash
mineru-ocr setup
mineru-ocr daemon start 3  # drei Hintergrundprozesse
mineru-ocr daemon list     # JSON: Daemons, Plaetze, Warteschlange
mineru-ocr daemon stop 2   # die zwei am wenigsten belegten ordentlich beenden
mineru-ocr scan.pdf     # abschicken, warten, Volltext ausgeben
mineru-ocr ~/Scans -o texte.md --timeout 600
```

`setup` und `daemon` bleiben Betriebskommandos. Die bisherigen Kommandos `ocr`,
`status`, `show`, `wait`, `markdown`, `retry` und `preflight` entfallen; diese
Funktionen bleiben in der Bibliothek. `daemon start X`, `daemon stop X` und
`daemon list` verwalten die Hintergrundprozesse (#58). Auch ein Dateiaufruf startet keinen Daemon.

Nur `setup` legt das Schema an. Daemon und jeder Client prüfen es beim
Öffnen rein lesend (`db.check_schema`, im `AsyncOCRClient` `db.check_schema_async`;
beide lesen den Katalog in einer Anweisung, `db._KATALOG_SQL`): Fehlt es oder stammt die Tabelle aus der
Zeit vor dem letzten Update (mit `new`), endet der Start mit `DatabaseError` und
dem Weg, statt dass ein Job an einer alten Zeile ohne Ergebnis endet. Ausgenommen
ist die Bibliotheksmethode `preflight` (`OCRClient(schema_check=False)`): Er liest beide Tabellen nicht
und darf jederzeit laufen. Die Prüfung im Daemon läuft in einem Faden, damit eine
hängende Datenbank die Ereignisschleife nicht anhält, und mit einer Frist für
Verbindung und Abfragen (`daemon.SCHEMA_PRUEFUNG_FRIST_SEKUNDEN`), damit der Faden
nach einem Abbruch nicht weiterhängt.

**Installation.** `mineru-ocr` ist ein installierter Befehl, kein Pfad. Er wird
einmal aus dem Haupt-Checkout installiert, in die Miniconda-Base, **ohne dabei ein
Paket der Base zu ändern**, das andere Projekte mitbenutzen:

```bash
/opt/homebrew/Caskroom/miniconda/base/bin/python3 -m pip install --no-deps --no-build-isolation -e .
```

`--no-deps` trägt nur dieses Paket ein und installiert, aktualisiert oder entfernt
nichts anderes; `--no-build-isolation` nimmt das in der Base liegende `setuptools`,
statt Build-Werkzeug nachzuladen. Die Installation ist editierbar und zeigt auf den
Checkout, aus dem sie gemacht wurde: nicht aus einem Arbeitsbaum, der wieder
verschwindet (ein Worktree eines Laufs).

**Der Agent-Skill.** `skills/mineroo/SKILL.md` sagt Agenten, die Mineroo
*benutzen*, wann und wie sie Dokumente lesen lassen (Bibliothek oder CLI, Warten
auf viele, Daemon, Kosten, EWR und personenbezogene Daten); für die Arbeit am Code
gilt diese Datei. Verlinkt wird er wie die Installation aus dem Haupt-Checkout, in
die Skill-Verzeichnisse von Claude Code und der übrigen Agenten; die Befehle stehen
in `README.md` unter „Für Agenten: der Skill“.

Der Skill trägt Abläufe und Regeln, die kein einzelnes `--help` und kein Docstring
sagen; Optionen, Ausgabeformen und Verträge einzelner Methoden überlässt er
diesen. Kein Test prüft ihn gegen den Code: Wer Kommandos, die Methoden der
Clients (auch welche nur der synchrone hat und welche kein `await` nehmen), die
Hülle, die Daemon-Regel, die Kosten, den Preflight oder die EWR-Regel ändert,
ändert ihn in derselben Änderung mit.

`preflight` verändert nichts und darf jederzeit laufen. Er prüft, was tatsächlich
läuft: das Image von Template und Workern gegen den erwarteten Tag, die
Endpoint-Variablen nach Wert an der Umgebung aller zuletzt gelaufenen Worker (auch
der beendeten), dass alle Rechenzentren in der zulässigen Liste liegen, und die
Sollwerte des Endpoints; FlashBoot wird berichtet. Was sich nicht feststellen lässt,
heißt „nicht prüfbar“, nie „in Ordnung“. Zwei Quellen, nicht eine: Sollwerte und
Worker liest er aus der REST-API v1 (`RunPodClient.endpoint`), die Rechenzentren
aus v2 (`RunPodClient.serverless`), denn nur v2 trägt am echten Endpoint
`dataCenterIds` (#31). Die gemeldete Worker-Umgebung zeigt keine Variablen des
Images; deshalb ist `MINERU_MODEL_SOURCE`, das das Image setzt, kein Sollwert.
`RunPodClient.endpoint` maskiert Geheimnisse der Worker-Umgebung bereits beim
Empfang (#40). Namen mit KEY, SECRET, TOKEN oder PASSWORD zählen unabhängig von
Großschreibung auch als Teilwort. Der Preflight kann deren Wert nicht vergleichen
(`nicht_pruefbar`), meldet aber fehlende oder leere erwartete Geheimnisse als
Abweichung. Leere Werte bleiben in der Worker-Auskunft leer.
Die CLI setzt Exit-Codes bewusst: 0 Erfolg (auch `empty`, mit Grund ohne Text),
1 Betriebs-/Datei-/Ausgabefehler, 2 ungültiger Aufruf, 3 `failed`/`input_error`
(je Datei mit Grund), 4 abgelaufene Wartefrist, **5 kein laufender Daemon**,
130 Strg-C. Bei mehreren gilt 1 vor 5 vor 4 vor 3 vor 0. Die CLI reiht eine Datei
nur ein, wenn noch kein Ergebnis vorliegt und ein frischer Herzschlag da ist.
Treffer werden ohne Daemon ausgegeben; gescheiterte Treffer werden nicht neu
gelesen. `--timeout S` begrenzt das Warten je Dokument; Frist und Strg-C lassen
Jobs bestehen. Ohne Frist wird unbegrenzt gewartet. `-t` und `-o` schließen
einander aus; eine Eingabedatei darf nicht überschrieben werden.

### Konfiguration — Endpoint, Datenbank und Sollwerte stehen nicht im Code

`config.Settings.load` liest sie aus (stärkste Quelle zuerst) ausdrücklicher
Angabe → Umgebungsvariable `MINERU_OCR_<NAME>` → TOML-Datei
`~/.mineru_ocr/config.toml` (`MINERU_OCR_CONFIG` oder `config_path` nennen eine
andere) → Feld-Voreinstellung. Für `endpoint_id` und `db_dsn` gibt es **keine**
Voreinstellung. Fehlt `db_dsn`, wirft `load` eine `ConfigurationError`, die alle
Stellen nennt. `endpoint_id` und der RunPod-Schlüssel dürfen beim Laden fehlen
(`None`); verlangt werden sie erst dort, wo sie gebraucht werden, mit
`Settings.require_endpoint_id()` und `Settings.require_api_key()`, und die Meldung
nennt dann die Stellen bzw. sagt, was mit `~/.runpod/config.toml` ist:

| Weg | Endpoint | RunPod-Schlüssel |
|---|---|---|
| `setup`, Dateiaufruf, `get`, `wait`, `retry` | nein | nein |
| Abschicken (`ocr_start`, `ocr`) | nein (den Endpoint trägt erst die Ergebniszeile, die der Job anlegt) | nein |
| `daemon`, `preflight`, `workers` | ja | ja |
| `status`, `credit`, `account` | nein | ja |

Den Schlüssel verlangt `RunPodClient` beim Bauen, die Lesefragen des Clients
vor dem Fragen; Endpoint und Schlüssel prüft `daemon.run` vor dem Start der Worker,
auch für Aufrufer aus Python. Der Preflight beim Öffnen eines `OCRClient` läuft nur,
wenn Schlüssel und Endpoint eingestellt sind; ohne einen davon entfällt er still. Die Sollwerte des Preflights (`expected_image_tag`, `workers_min`,
`workers_max`, `idle_timeout`, `execution_timeout_ms`, `allowed_data_centers`, die
Tabelle `[endpoint_env]`) sind Felder von `Settings` mit den Voreinstellungen aus
Mineroo#3 und den Werten am Endpoint (0, 10, 5, 300000). Die Leerlaufzeit von
5 s hat der Eigentümer festgelegt (Mineroo#19): FlashBoot holte den
herunterskalierten Worker nach 4,5 min in 1,3 s zurück; längeres Warmhalten
kostet nur Geld. Nach 15 min Pause gab es ohnehin rund 2 min Kaltstart.
Ebenso eingestellte Werte sind die Grenzen eines Teilstücks (Mineroo#20): `max_pages_per_chunk`
(20 Seiten), `max_payload_bytes` (7,4 MB roh) und `job_timeout_seconds` (300 s);
die Herleitung aus Antwort-, Anfrage- und Zeitgrenze steht an den Feldern in
`config.py` und unter „Teilstücke sind vor Postgres vollständig abstrahiert“.
Die Seitenzahl wirkt im Daemon, der zerlegt: Ein neuer Wert gilt nach dessen
Neustart. Ein unbekannter Schlüssel in der Datei, eine
ausdrücklich genannte, aber fehlende Datei und ein Wert vom falschen Typ sind
Fehler: ein Tippfehler im Namen wäre sonst ein Wert, der da steht und nirgends
wirkt. Der RunPod-Schlüssel bleibt in `~/.runpod/config.toml` und gehört nie in
diese Datei. Das Beispiel steht in `README.md`.

**Die Tests sind von der Konfiguration des Entwicklers abgeschirmt:** Eine
`autouse`-Vorrichtung in `tests/conftest.py` räumt jede `MINERU_OCR_*`-Variable,
biegt die Standarddatei und `config.RUNPOD_CONFIG_PATH` auf nicht vorhandene Pfade
um und stellt Endpoint und Datenbank auf Testwerte (die Testdatenbank). Einen
RunPod-Schlüssel hat ein Test nur, wenn er ihn ausdrücklich übergibt. Ein Test, der die Quellen selbst
prüft, übergibt `environ=` und `config_path=` ausdrücklich. Der Sollwert des
GPU-Speicheranteils (`MINERU_VIRTUAL_VRAM_SIZE`) ist bis zur Messung offen: Der
Preflight berichtet den Ist-Wert und bewertet ihn als „nicht prüfbar“.

**Pfade kennt nur die CLI.** Die Bibliothek nimmt Bytes und gibt Hashes
(`OCRClient.ocr_start(bytes | Sequence[bytes])`, `ocr`, `wait`, `wait_all`,
`as_completed`, `get`, `document`; dieselben Namen bis auf `document` mit `await`
im `AsyncOCRClient`);
`cli.py` liest Dateien und Ordner und gibt den Volltext aus. Struktur, Bilder,
Rohantworten und Zustandsabfragen bleiben der Bibliothek vorbehalten. Auf stdout
stehen nur Daten bzw. der Ausgabepfad; Meldungen und Warnungen gehen auf stderr.
Die Bibliothek kennt weiterhin keine Pfadparameter.

---

## Aufbau — was man nicht aus einer einzelnen Datei sieht

### Der Daemon ist ein eigener Prozess

Einreihen und Abarbeiten sind getrennt. Die Bibliothek (`ocr_start`, `retry`,
`retry_all`) reiht auch ohne Daemon ein und warnt einmal je daemonloser Phase
im Logger `mineru_ocr.client`, wenn danach Dokumente offen sind. Eingabe und Job
entstehen in einer Transaktion; das Ergebnis entsteht erst am Ende des Jobs.

Die CLI (`mineru-ocr PATH…`) wartet synchron und prüft für eine Datei ohne
Ergebnis **vor** dem Einreihen `db.worker_heartbeats`. Fehlt ein frischer
Herzschlag, reiht sie diese Datei nicht ein, wartet auch auf bereits offene
nicht und meldet Exit-Code 5. Die Meldung nennt `mineru-ocr daemon start X`
und zur Kontrolle `mineru-ocr daemon list`.
Treffer aus der Datenbank brauchen keinen Daemon. Beide Wege starten nie selbst
einen; den startet der Betreiber.

Ein Daemon läuft, wenn ein Worker einen Herzschlag jünger als
`config.DAEMON_HEARTBEAT_MAX_AGE_SECONDS` (30 s) hat, gemessen an der Datenbankuhr.
Die Zeile eines abgestürzten Daemons genügt nicht. Ist die Tabelle nicht lesbar,
bricht die CLI für diese Datei mit Betriebsfehler 1 ab, ohne einzureihen; die
Bibliothek warnt und reiht weiterhin ein. `status()` und `preflight()` berichten
über die Bibliothek. Ein nach der Prüfung ausfallender Daemon kann das Warten
weiterhin anhalten; dafür gibt es `--timeout`.

### Geteilte Lesefragen des Daemons

**Kontostand und Image-Herkunft teilen alle Plätze des Daemons**
(Mineroo#47): `tasks.worker_context_resources` baut einmal `lesefragen.Lesefragen`
und reicht denselben Speicher über `ocr_document` an den Exekutor weiter.
`runpod_cache_seconds` (60 s, TOML oder `MINERU_OCR_RUNPOD_CACHE_SECONDS`)
begrenzt beide Lesefragen getrennt. Je Frage hält eine asynchrone Sperre
gleichzeitige Leser bis zur Antwort an; Wert oder Fehler bleiben danach für
das Intervall gespeichert. Es gibt keinen Hintergrundtask und keinen globalen
Speicher. Auch eine abgebrochene Abfrage gibt das Zeitfenster nicht vorzeitig frei.

`wait_for_balance` gibt nach einem vorübergehenden Lesefehler nichts frei und
wartet im Daemon nur bis zum nächsten erlaubten Abfragezeitpunkt. Bei bestätigt
niedrigem Kontostand bleibt der bisherige Prüftakt `balance_recheck_seconds`
(900 s); endgültige Fehler brechen das Warten ab. `_mit_kontopause` verlangt
nach HTTP 402 eine danach **begonnene** Kontostandsabfrage, weiterhin unter
Wahrung der Cache-Frist. Damit zählen alte Werte nicht als Widerspruch, auch
wenn ihre Abfrage beim Eintreffen der 402 noch lief. Client, `credit`, `status`
und `preflight` benutzen diesen Speicher nicht. Die direkten Exekutor-Aufrufe
ohne übergebenen Speicher bleiben für isolierte Aufrufer unverändert.

### Welcher Prozess welche App hält

Procrastinate wird so benutzt, wie es vorgesehen ist: **jeder Prozess baut seine
App einmal und behält sie.** Es gibt keine App auf Modulebene (`tasks.py` hat
keine), und nirgends wird ein Konnektor zur Laufzeit umgeschaltet — kein
`replace_connector`, kein `with_connector`, kein Zuweisen an `app.connector`.

| Prozess | App | Gebaut von | Pool |
|---|---|---|---|
| `mineru-ocr daemon` | **genau eine**: `PsycopgConnector`, die Tasks samt Retry-Strategie und der Waechter angemeldet | `tasks.build_daemon_app` | der des Daemons (`AsyncConnectionPool`, `check=AsyncConnectionPool.check_connection`), per `app.open_async(pool=…)` uebergeben |
| jeder `OCRClient` (auch hinter dem Kommando) | **eine eigene**: `SyncPsycopgConnector`, **keine** angemeldeten Tasks | `tasks.build_client_app`, geoeffnet in `OCRClient.__enter__` | `NullConnectionPool(max_size=1, num_workers=1)`, vom Client angelegt **und** geschlossen |
| jeder `AsyncOCRClient` | **eine eigene**: `PsycopgConnector`, **keine** angemeldeten Tasks | `tasks.build_async_client_app`, geoeffnet in `AsyncOCRClient.__aenter__` per `app.open_async(pool)` | `AsyncNullConnectionPool(max_size=1, num_workers=1)`, vom Client in derselben Ereignisschleife angelegt **und** geschlossen |
| `mineru-ocr setup` | eine fluechtige, nur fuers Schema | `db._procrastinate_schema_anwenden_falls_noetig` | der Konnektor-eigene |

Was daraus folgt und nicht aus einer einzelnen Datei hervorgeht:

- **Der Daemon-Prozess baut keine zweite App.** Jede weitere App im selben Prozess
  benennt Procrastinates eingebauten Task `remove_old_jobs` um. (Die Tests haben
  zwei Apps im Prozess, das ist harmlos; dieses Paket benutzt `remove_old_jobs`
  nicht.) Ein Prozess mit `OCRClient` und `AsyncOCRClient` haelt zwei Client-Apps,
  wie Procrastinates eigener Abnahmetest fuer sync und async im selben Prozess.
- **Der Client reiht per Namen und mit ausdruecklicher Queue ein**, Name und Queue
  aus denselben `config`-Konstanten wie die Anmeldung des Daemons:
  `tasks.enqueue_document` ruft `app.configure_task(name=…, queue=…, lock=…,
  connection=…).defer(sha256=…)` und gibt die Kennung des Jobs zurueck (im
  Async-Client `tasks.enqueue_document_async`, dasselbe mit `defer_async`). Ohne
  `queue=` landete der Job in `default`, die der Daemon nie liest. Mit
  `connection=` laeuft das Einreihen in der Transaktion des Clients, nicht auf
  einer Verbindung aus dem Pool. Am Job steht nur `lock` = Hash, **kein
  `queueing_lock`**: Die Sperre gegen Dubletten ist die Eingabe-Tabelle, und ein
  `queueing_lock` daneben loeste Procrastinate-Issue #1446 aus (ein zweiter Job,
  eingereiht waehrend der erste laeuft, blieb fuer immer auf „laeuft").
- **Erfolgreiche Jobs loescht der Worker** (`delete_jobs = "successful"`, Feld
  von `Settings`): An ihnen haengt kein Zustand mehr. Gescheiterte und
  abgebrochene bleiben stehen; `retry` braucht sie.
- **Ein Client haelt zwischen zwei Aufrufen keine Procrastinate-Verbindung.**
  `list_jobs`, `list_tasks` und `retry_job_by_id` laufen synchron ueber seine App,
  mit je einer kurzlebigen Verbindung; der `AsyncOCRClient` fragt Procrastinate
  nur beim Einreihen, und das ueber seine eigene Verbindung. Je Agent bleiben so ein bis zwei
  Verbindungen: die des Clients und, solange er wartet, die eigene der
  Warteschleife (siehe „Warten“).
  `client._synchron_ausfuehren` gibt es nur noch fuer die kurzen Lesefragen an
  RunPod.
- **Der Client schliesst seinen Pool selbst**, weil `app.close()` einen uebergebenen
  Pool nicht schliesst. Der Pool des Daemons gehoert dem Daemon und wird NACH den
  Workern geschlossen.
- **Ein synchroner Konnektor kann nicht nur einreihen** — das stand frueher in
  `tasks.py` und ist am Objekt widerlegt: Er fuehrt auch `list_jobs`, `list_tasks`
  und `retry_job_by_id` aus. Nur einen Worker kann er nicht tragen.
- `InMemoryConnector` uebergeht `connection=`. Dass das Einreihen wirklich auf der
  Verbindung des Clients laeuft, laesst sich nur gegen echtes Postgres pruefen
  (`tests/test_client.py`, `tests/test_integration.py`).

### Ein Buch, nicht zwei

Jede Tatsache steht an genau einer Stelle: das Ergebnis in `documents`, die
offene Eingabe in `inputs`, was wartet oder läuft in Procrastinates Job-Tabelle.
Die Tabelle `documents` trägt **ausschließlich Endzustände**:
`done | empty | failed | input_error`. Es gibt **kein `new`** und **kein
`processing`**: Die Ergebniszeile legt **der Job an, wenn er endet**
(`db.write_terminal_async`; überschrieben werden darf nur eine `failed`-Zeile),
nicht das Abschicken. **Offen ist ein Dokument, solange seine Eingabe in
`inputs` steht** und es kein Ergebnis hat. Bei `failed` steht die Eingabe auch,
dann entscheidet das Ergebnis.

Gelesen wird beides in **einer** Anweisung (`db.read_states`, ein `LEFT JOIN`
über `documents` und `inputs`): Ergebnis da heißt fertig, Eingabe da heißt
offen, keins von beiden heißt unbekannt. Zwei Abfragen nacheinander (erst
`documents`, dann `inputs`) meldeten ein Dokument, dessen Job genau dazwischen
fertig wird, fälschlich als unbekannt. Das Warten (`db.wait_for_terminal`) fragt
Procrastinate nicht und lädt weder `raw` noch `content`.

**Warten** ist eine Schleife, einmal als sync- und einmal als async-Generator
(`db.wait_for_terminal`, `db.wait_for_terminal_async`), mit einem gemeinsamen Kern
ohne Ein- und Ausgabe (`db._Warteplan`). Sie läuft auf einer **eigenen**
`autocommit`-Verbindung je Aufruf (`application_name` `mineru-ocr wait`), nie aus
einem Pool (der hebt kein `LISTEN` auf) und nie auf der des Clients (dort wüchse
der Rückstau an Signalen). Zuerst `LISTEN doc_done`, dann alle offenen in einer
Anweisung lesen; ein unbekannter Hash wird sofort gemeldet. Die Signale werden mit
`notifies(timeout=…, stop_after=1)` **ohne `break`** bis zum Ende des Pakets
gelesen, sonst gehen die übrigen eines Pakets verloren; im Takt
(`wait_poll_seconds`) werden alle offenen gelesen. Nach einem wiederholbaren
Abbruch wird nach einer kurzen Pause neu verbunden und neu angefangen (ohne die
Pause bestürmte eine Verbindung, die gleich wieder abbricht, die Datenbank), nach
einem Zugangsfehler mit SQLSTATE nicht. Ein gescheitertes Verbinden trägt aber
keinen SQLSTATE (psycopg baut ihn nur aus dem Text, auch bei fehlender Datenbank
oder Rolle), und am Text wird bewusst nicht unterschieden: Ohne Frist wartet die
Schleife deshalb, bis die Datenbank zurück ist; wer das nicht will, setzt eine
Frist (`wait --timeout`). Die Frist gilt für den ganzen Aufruf, auch fürs
Verbinden (`connect_timeout`); vor ihrem Ablauf wird ein letztes Mal gelesen, und
ihr `WaitTimeoutError` (ein `TimeoutError`) nennt die Hashes, die dann noch offen
sind; Frist und Strg-C fassen keinen Job an. Der Client
(`wait`, `wait_all`, `as_completed`) liefert daraus Hüllen; der `AsyncOCRClient`
mit dem async-Generator und mit geladenem `raw` (eine Eigenschaft kann nicht
`await`en), seine Hüllen halten keine Verbindung (`refresh()` nennt den Weg über
`await client.get(h)`). Beim `AsyncOCRClient` begrenzt die Frist deshalb das
Warten auf Endzustände; das Laden fertiger Ergebnisse kommt hinzu: `wait_all([a, b], timeout=10)` mit `a`
fertig (2 GB `raw`) und `b` offen wirft nach etwa 10 s plus der Ladezeit von `a`.
Läuft zwischen dem Lesen der Schleife und dem Laden ein
`retry`, trägt die Hülle wie im Sync-Client den gesehenen Endzustand, ohne `raw`,
und `raw` meldet `DocumentNotFound`.

„Läuft gerade" wird aus dem Procrastinate-Job **gelesen**, dessen Kennung an der
Eingabe steht (`inputs.job_id`, `list_jobs(id=…)`), von `OCRClient.document` und
von `retry`, und nirgends gespiegelt. Die Kennung schreibt das Abschicken in
derselben Transaktion wie Eingabe und Job, `retry`, wenn es einen neuen Job
anlegt (`db.set_input_job`). Wer das nicht weiß, sucht
vergeblich nach dem Feld — oder, schlimmer, führt es ein und erzeugt genau die
zwei Bücher, die auseinanderlaufen. `document()` meldet den Endzustand, ein
offenes Dokument samt dem Zustand seines Jobs (`Phase.WAITING`, `RUNNING` oder
`ENDED_WITHOUT_RESULT`, dann hilft `retry`), und `DocumentNotFound` für einen
unbekannten Hash. `status` zählt die Endzustände und `open` (Eingabe ohne
Ergebnis, `db.count_open`).

Daraus folgt: Es gibt keinen Aufräum-Wächter, der verwaiste Zeilen
zurücksetzt, weil es keine gibt. Zuständig sind `db.DocumentStatus`,
`db.DocumentState`, `db.derive_status` und `db.Phase`; die Verknüpfung zum Job
ist `inputs.job_id`.

**`retry` geht Procrastinates eigenen Weg** (`OCRClient.retry(h)`, für viele
`retry_all(hs)`; ohne Angabe wählt `retry_all` selbst jedes Dokument ohne
gültiges Ergebnis, also jede stehende Eingabe). Je Dokument: Ergebnis `done`,
`empty` oder `input_error` oder ein Job, den `document()` als wartend oder
laufend zeigt (`db.derive_status`, dieselbe Regel; auch ein unbekannter
Job-Zustand) → nichts. Vor dem Handeln sperrt `retry` die Eingabe
(`db.lock_input`); ist sie inzwischen weg, wurde der Job eben fertig, und es
geschieht ebenfalls nichts. Job `failed` →
**erst** die `failed`-Zeile weg (eigene Transaktion), **dann**
`retry_job_by_id` auf denselben Job; andersherum könnte der zurückgesetzte Job
schon ein neues Ergebnis geschrieben haben, das das Wegnehmen löschte. Job
`aborted`, `cancelled`, `failed` mit `abort_requested` (ein Abbruch über die
API; das Zurücksetzen scheitert am CHECK `check_not_todo_abort_requested`) oder
kein Job → in **einer** Transaktion `failed`-Zeile weg, neuer Job, neue Kennung
in `inputs.job_id`. Weil die `failed`-Zeile weggeht, ist das Dokument danach
offen, und jedes Warten, in jedem Prozess, wartet auf den neuen Lauf; einen
Zeitstempel-Vergleich gibt es dafür nicht. Ein Warten, das schon läuft, wenn der
Job `failed` schreibt, endet mit `failed`; auf den neuen Lauf wartet, wer nach
`retry` wartet. Gleichzeitige `retry` auf dasselbe
Dokument laufen nacheinander (`db.retry_lock`, eine Advisory-Sperre der Sitzung um
Lesen und Handeln): Ohne sie läsen zwei Aufrufer beide „kein Job“ und reihten
zwei Jobs ein, die beide bezahlt würden. Fehler von Procrastinate gehen nach
oben, keiner wird als „nicht wiederholt" verschluckt; ein abgelehntes
Zurücksetzen kommt als `DatabaseError` des Pakets an. Bekannte Grenze: Setzt
jemand denselben Job im selben Moment von Hand außerhalb des Pakets zurück
(Procrastinate-Shell oder Admin), kann `retry_job_by_id` einen schon wieder
laufenden Job noch einmal starten (`procrastinate_retry_job_v2` nimmt auch
`doing` an); `db.retry_lock` schützt nur vor Aufrufern des Pakets. Wiederholungen
erfolgen ausschließlich über die Bibliothek und kosten erneut Geld.

`empty` ist ein **gültiges** Ergebnis, kein Fehler: Der Gegenstand war leer. Als
Erfolg geführt wäre es unsichtbarer Verlust, als Fehler geführt erzeugte es
endlose Wiedervorlage und Doppelkosten.

### Die Eingabe liegt als Bytes in der Datenbank, nicht als Pfad

Die Bytes eines Dokuments stehen in der Tabelle `inputs` (`sha256 text PRIMARY
KEY`, `content bytea NOT NULL`, die Lese-Einstellungen `tier`, `ocr_mode`,
`image_analysis`, alle NOT NULL, und `job_id bigint`), **solange das Dokument kein
Endergebnis hat**.
Pfade gibt es weder in `documents` noch im Job; der Job trägt nur den Hash, und
`OCRClient.ocr_start` legt Eingabe und Job beim Abschicken in **einer**
Transaktion an (`INSERT INTO inputs … ON CONFLICT DO NOTHING`, dann einreihen),
**je Dokument** in einer eigenen und nie mehrere in einer (zwei Aufrufer mit
überlappenden Mengen in verschiedener Reihenfolge würden sich sonst an den
Eindeutigkeitsprüfungen gegenseitig blockieren). Steht die Eingabe schon, ist das
Dokument unterwegs, und es geschieht nichts: Der Primärschlüssel von `inputs` ist
die Sperre gegen ein zweites Einreihen, auch gleichzeitig und während der Job
läuft. Es entsteht keine Datei, und das Einreihen öffnet keine PDF: Typ, Größe und
Seitenzahl bestimmt der Job aus den Bytes und schreibt sie mit dem Endergebnis
(`mime` bleibt leer bei einem Typ, den das Paket nicht kennt, beides bei fehlender
oder beschädigter Eingabe).

Abschicken prüft **erst alle** übergebenen Dokumente (`core.prepare`), dann reiht es
ein; sonst wären bei einem abgewiesenen Element die vorderen schon abgeschickt.
Abgewiesen wird, woraus nichts Neues entstehen kann: ein Typ, den das Paket nicht
liest, und ein Ganzdokument-Format oder Bild über `max_payload_bytes`. Gibt es eine
Ergebniszeile, ist das ein **Treffer**, auch bei `failed`, und es geschieht nichts
(ein gescheitertes Dokument liest nur `retry` neu); eine stehende Eingabe ohne
Ergebnis heißt unterwegs. Der Treffer wird ohne `raw` gelesen: ein erneuter Lauf
über 30.000 fertige Dokumente zöge sonst rund 34 GB. `Submitted.status` ist der
Endzustand eines Treffers und `None` für ein offenes Dokument.

Der Job liest `content` über den Hash auf einer **eigenen kurzlebigen
Verbindung** (`db.read_input_async`), immer mit `binary=True`, und prüft den Hash
nach. Nicht aus dem Pool des Daemons: libpq behält seine Puffer in der Größe des
größten je gelesenen Werts, bis die Verbindung schließt, und 24 Verbindungen
banden so das Gigabyte. Ohne `binary=True` ginge der Wert als Hex über die
Leitung, doppelt so groß.

**Gelöscht wird die Eingabe mit dem Endergebnis, in derselben Transaktion**
(`db.write_terminal_async`), bei `done`, `empty` und `input_error`. Bei `failed`
bleibt sie stehen, damit `retry` etwas zu lesen hat. Findet ein Job schon ein
Ergebnis `done`, `empty` oder `input_error` vor (ein Abschicken legte die Eingabe
genau im Augenblick des Endschreibens neu an), löscht er die Eingabe und endet
ohne RunPod-Aufruf.

Eine Eingabe über `db.MAX_INPUT_BYTES` (1 GB, Postgres begrenzt ein Feld auf knapp
1 GiB) weist das Abschicken mit `TooLargeError` ab; die CLI prüft Typ und Größe schon
vor dem Lesen einer Datei und zählt sie in Ordnern als `zu_gross`. Das gilt auch für
PDFs, die der Job später in Stücke schneiden würde.

### Die Vorerkennung lässt grob durch; MinerU entscheidet

`mime` erkennt am Inhalt, nie am Namen, und nur grob (Mineroo#17): Welcher Typ
eine Datei genau ist, entscheidet MinerUs Erkennung im Worker (DocVortex 0.5.16,
`guess_suffix_by_bytes`). Durch lässt `mime` PDF, die Bilder und die
**Ganzdokument-Formate** (`DocumentType.is_whole_document`, alles außer PDF und
Bild): jeden OLE2-Container (DOC, XLS, PPT unterscheidet es nicht, `mime` heißt
dann `application/x-ole-storage`), ZIP mit dem Aufbau von OOXML, OpenDocument
(nur ODT, ODS, ODP), EPUB oder OFD, RTF und MHTML an ihrem Kopf, HTML an seinem
Anfang. Dafür liest es bis 64 KiB (`KOPF_BYTES`, der MHTML-Kopf); die Meldung bei
einem unbekannten Typ nennt davon die ersten 16 Bytes. Was durchgeht und MinerU
dann nicht liest, endet `input_error` über `errors.ist_dateiabweisung`; dafür
stehen DocVortex' Fehlertypen der Ganzdokumente in `DATEIABWEISUNG_KENNUNGEN`
(am Endpoint beobachtet: ein OLE2-Container ohne `WordDocument` endete sonst
`failed`, `artifacts/2026-10-27-ganzdokumente/`). Ein
PDF darf nach ASCII-Leerraum und vollständigen UTF-8-BOMs beginnen (#56).
Nur der Executor entfernt diesen Vorspann für PDFium und den Worker, nach der
Hashprüfung; Hash, gespeicherte Eingabe und `size_bytes` bleiben die des
Originals. Fremde Bytes vor `%PDF-` bleiben abgewiesen; steht die Kennung im
gelesenen Kopf, nennt die Meldung ihren null-basierten Byteversatz als
Rettungshinweis. Ein Ganzdokument-Format geht ungeteilt als ein Stück hinaus (unter
`max_payload_bytes`, sonst weist schon das Abschicken es ab) und hat keine
Seitenzahl, also keinen Seitenvergleich. CSV und TSV tragen keine Kennung und
bleiben draußen.

HTML erkennt `mime` auch in UTF-16LE und UTF-16BE, mit und ohne BOM (#56),
weiterhin nur mit HTML-Kennung am Anfang. `mime.worker_bytes` kodiert es erst
im Executor nach UTF-8 um; kaputte UTF-16-Zeichen ergeben `input_error`, ohne
RunPod-Kontakt. Die Anfragegrenze prüft der Executor nach dem Umkodieren, denn
UTF-8 kann größer sein. Die Originalbytes bleiben für Hash, Eingabe und
`size_bytes` maßgeblich, auch wenn ein fehlgeschlagener Job sie für `retry` behält.

### Die Lese-Einstellungen eines Dokuments stehen an seiner Eingabe

Stufe, OCR-Modus und Bildanalyse reisen als **ein Wert**, `config.ReadSettings`
(Mineroo#14). Er prüft beim Bilden gegen `config.GUELTIGE_STUFEN` und
`GUELTIGE_OCR_MODI` und bestellt die Bildanalyse außerhalb von `advanced` als aus;
was er sagt, geht an RunPod und an die Zeile. Wo er auf dem Weg steht:

- **Gewählt** beim Abschicken: `ocr_start` und `ocr` (beide Clients) nehmen
  `tier=`, `ocr_mode=`, `image_analysis=`, der CLI-Dateiaufruf `--tier`,
  `--ocr-mode` und `--image-analysis/--no-image-analysis`. Was fehlt, kommt aus
  `Settings.read_settings`, also aus den Feld-Voreinstellungen von `Settings`
  (`advanced`, `ocr`, an); Schlüssel der Konfigurationsdatei sind sie nicht.
  Geprüft wird vor dem ersten Einreihen eines Aufrufs.
- **Abgelegt** an der Eingabe (`db.insert_input`). Der Job trägt weiter nur den
  Hash; die Einstellungen als Job-Argument mitzugeben ist ausdrücklich nicht
  gemeint. Ein Dokument, das schon unterwegs ist, behält die Einstellungen, mit
  denen es eingereiht wurde; ein Treffer gilt unabhängig von den verlangten.
  `retry` nimmt keine: Ein zurückgesetzter wie ein neuer Job liest dieselbe Eingabe.
- **Gesendet** vom Job: `db.read_input_async` liest sie mit den Bytes in einer
  Anweisung, und jedes Teilstück geht mit ihnen hinaus. **Die `tier`, `ocr_mode` und
  `image_analysis` in der Einstellung des Daemons wirken auf keinen Job**; sie sind
  nur die Voreinstellung des Abschickens. Erst an der Leitung zerfällt der Wert:
  `RunPodAccess.submit` und `runpod.job_eingabe` nehmen die drei Felder einzeln,
  weil die Nutzlast auch Werte tragen können muss, die das Paket nie bilden würde
  (die erzwungene Fehlerantwort der Aufnahmen schickt eine Stufe, die es nicht gibt).
- **Vermerkt** an der Ergebniszeile: `documents` trägt `tier`, `ocr_mode`,
  `image_analysis` (die BESTELLTEN; `db.write_terminal_async` nimmt sie in derselben
  Anweisung aus der Eingabe, also auch beim `failed` des Wächters, und ohne Eingabe
  bleiben sie leer, ebenso bei einem Ganzdokument-Format, siehe unten) und
  `mineru_version` (die der Worker meldet, auch in einer
  Fehlerantwort; bei mehreren Teilstücken die niedrigste nach PEP 440, eine
  unlesbare zählt als die niedrigste, `db.mineru_versionsrang`; beim Überschreiben
  eines `failed` die des neuen Laufs, auch keine). Ein U+0000 in dem, was der
  Worker meldet, steht in Spalte, `error`, Vermerken und dem Fehler-Umschlag des
  `failed` ohne Inhalt als `\u0000`, ein einzelnes Surrogat als `\ud800`
  (`executor._tief_speicherbar`), sonst ließe sich auch das `failed` ohne Inhalt
  nicht schreiben; `raw` bleibt unbereinigt. Was MinerU tatsächlich nahm, steht im
  Rohergebnis unter `envelopes[…]["output_meta"]`; bei `auto` der Modus, den es für
  die Datei wählte. `DocumentRow.read_settings`, die Hülle (`mineru_version`,
  `read_settings`) und `document()` nennen sie.
- **Abweichungen** sind Vermerke, kein Fehler, und das Ergebnis gilt trotzdem
  (`executor._lesung_vermerken`): `lese_einstellungen_abweichend`, wenn ein
  Teilstück andere Einstellungen meldet als bestellt (`auto` passt zu `txt` wie zu
  `ocr`, eine fehlende Meldung passt nicht), auch eine bezahlte, unverwertbare
  Antwort, sofern sie Einstellungen meldet (eine Fehlerantwort tut es nicht), und
  `mineru_versionen_verschieden`. Bestimmt werden beide einmal; scheitert das
  Endschreiben, nimmt der Fehlerzweig sie.
- **Bei Ganzdokument-Formaten wirken sie nicht** (Mineroo#17): Der Worker liest
  alles außer PDF und Bildern in `flash`, gleich was bestellt war, und meldet es
  so. Gesendet werden die Einstellungen der Eingabe trotzdem, aber die
  Ergebniszeile trägt keine (`write_terminal_async(store_read_settings=False)`,
  auch beim Überschreiben eines `failed` und beim `failed` des Wächters, der dafür
  die Eingabe liest und ihren Typ bestimmt), und verglichen wird nicht. Die Eingabe
  trägt sie weiter, denn `inputs` verlangt sie NOT NULL.
- **Gezählt** in `status()["readings"]` (`db.counts_by_reading`): je Version und
  bestellten Einstellungen die Zahl der Dokumente mit `done` oder `empty`, nach
  Version geordnet wie MinerU sie zählt (4.0.9 vor 4.0.12), ohne Version am Ende. Bei
  einer neuen MinerU-Version geschieht nichts; ein Neu-Lesen gibt es nicht.

### Drei Antworten auf einen Fehlschlag — an genau einer Stelle entschieden

`errors.classify_failure` bildet jede Ausnahme auf `FailureKind` ab
(`TERMINAL | RETRYABLE | BALANCE_PAUSE`), und `errors.should_run_again` ist die
**eine** Ja/Nein-Antwort, die **beide** Aufrufer benutzen: der Exekutor (schreibe
ich einen Endzustand?) und die Retry-Strategie in `tasks.py` (lege ich den Job
wieder vor?). Diese Zusammenführung ist Absicht und teuer erkauft — vorher
gruppierten beide Seiten die Fälle unterschiedlich, und `BALANCE_PAUSE` hätte
einen Job endgültig gemacht, während der Exekutor ihn als wiederholbar warf. Wer
an einer der beiden Stellen eine eigene Fallunterscheidung einbaut, stellt genau
diesen Fehler wieder her.

### Jeder Job, der nicht neu eingereiht wird, schreibt ein Ergebnis

Das ist die Regel, die das Modell trägt (Mineroo#27): Sonst bleibt ein Dokument
offen, ohne dass jemand daran arbeitet, und `wait` hängt ewig. Ob ein Versuch der
letzte ist, beantwortet `tasks.wird_erneut_vorgelegt` für Retry-Strategie,
`ocr_document` und Wächter mit derselben Rechnung; der Exekutor bekommt die
Antwort als `letzter_versuch`. Daraus folgt:

- **Herunterfahren.** Procrastinate bricht die nach der Nachfrist noch laufenden
  Jobs mit `asyncio.CancelledError` ab und fragt dann die Retry-Strategie.
  `classify_failure` stuft den Abbruch als wiederholbar ein, also reiht
  Procrastinate den Job neu ein, und die Wiedervorlage zählt als Versuch (gewollt
  und durch Procrastinates Abnahmetests abgesichert, PR #1215, aber nicht in
  seiner Doku zum Herunterfahren). Im letzten Versuch schreibt der Exekutor
  `failed` mit dem Vermerk „im letzten Versuch abgebrochen (Herunterfahren oder
  Abbruch ueber die API)“ (`executor.ABBRUCH_IM_LETZTEN_VERSUCH`) und wirft dann
  weiter; der Job endet `aborted`. Der Abbruch bei RunPod (`/cancel`) geht in beiden Fällen hinaus.
  Trifft der Abbruch den Fehlerzweig mitten im Schreiben von `failed`, läuft das
  Schreiben zu Ende (`abbruch.abwarten_bis_zum_zweiten_abbruch`), und
  weitergeworfen wird der Fehler, der in den Fehlerzweig führte, nicht der
  Abbruch. Ein zweiter Abbruch (das zweite Signal an den Daemon) bricht das
  Schreiben ab, damit eine hängende Datenbank den Daemon nicht festhält. Mit
  Restbudget reiht Procrastinate den Job dann neu ein, und das Dokument läuft
  noch einmal, auch nach einem endgültigen Fehler; im letzten Versuch bleibt es
  ohne Ergebnis. Wird das Schreiben trotz des zweiten Abbruchs binnen
  `abbruch.AUFRAEUMFRIST_SEKUNDEN` doch fertig (die Datenbank hatte schon
  bestätigt), gilt es als geschrieben, und weitergeworfen wird wieder der Fehler,
  der in den Fehlerzweig führte. Kamen zwei Abbrüche schon vor dem Schreiben,
  beginnt es gar nicht erst. Das Schreiben von `done` und `empty` ist genauso
  abgeschirmt: Trifft es der Abbruch, läuft es zu Ende, und der Job endet ohne
  Fehler, statt ein bezahltes Ergebnis zu verwerfen und das Dokument noch einmal
  laufen zu lassen. (`input_error` geht durch den Fehlerzweig und ist damit schon
  abgeschirmt.)
- **Nachsehen und Lesen im Fehlerzweig.** Das Nachsehen nach einem Ergebnis und
  das Lesen der Eingabe stehen im `try` des Exekutors, damit auch ein
  Datenbankfehler dort im letzten Versuch `failed` schreibt.
- **Nicht speicherbares Ergebnis.** Weist Postgres das Endschreiben wegen des
  Inhalts ab (SQLSTATE-Klasse 22 oder 54: ein U+0000 im Text, die Größe von
  `jsonb`; `db._nicht_speicherbar`) oder ist `raw` größer als eine Nachricht an
  Postgres (`db.MAX_RAW_JSON_BYTES`, vor dem Senden geprüft; gesendet trennte der
  Server die Verbindung, und das sähe wiederholbar aus), packt
  `db.write_terminal_async` das in `UnstorableResultError`, und
  `classify_failure` stuft es als endgültig ein,
  obwohl es ein `DatabaseError` ist: Ein weiterer Lauf liefert denselben Inhalt.
  Der Exekutor schreibt dann schon im ersten Versuch `failed`, ohne den Inhalt,
  mit dem Vermerk `ergebnis_nicht_speicherbar`, der den Grund nennt; ebenso, wenn
  bei einem anderen Fehler das bis dahin Bezahlte denselben Mangel trägt. Den
  Inhalt bereinigt er nicht, um ihn speicherbar zu machen.
- **`retry_stalled`** legt einen hängenden Job nur vor, solange Versuche übrig
  sind. Danach schreibt es `failed` („Worker wiederholt abgestürzt“), aber nur,
  wenn noch kein Ergebnis da ist (`executor.write_failure_if_no_result`; ein
  vorhandenes `failed` mit bezahltem `raw` bleibt), und beendet den Job über
  `JobManager.finish_job` als `failed`, mit derselben Löschregel wie der Worker.
  Das Budget zählt je Job, nicht je Schuldigem: Ein Dokument, das den
  Daemon-Prozess abstürzen lässt (etwa PDFium beim Schneiden), lässt mit ihm alle
  Jobs hängen, die gerade mitliefen. Jeder Absturz kostet jeden von ihnen einen
  Versuch, und stürzt der Prozess wiederholt ab, enden auch die unschuldigen
  Dokumente als `failed` („Worker wiederholt abgestürzt“). Eine Mechanik, die
  Schuldige und Mitläufer trennt, gibt es nicht und soll es nicht geben: Der
  Betreiber holt die Mitläufer gezielt zurück, mit `retry HASH` je Dokument oder
  `retry_all(hashes)` mit Angabe, nicht mit `retry --all` (`retry_all()` ohne
  Angabe), das den Schuldigen mit einreihte und den Daemon erneut abstürzen
  ließe.

Ohne Ergebnis enden kann danach nur noch Seltenes oder von Hand Ausgelöstes: ein
Abbruch über die API (`cancel_job_by_id(…, abort=True)`), die Absage eines
wartenden Jobs (Shell `cancel` oder API), ein unbekannter Task-Name (Daemon und
Bibliothek in verschiedenen Ständen), ein Endschreiben, das im letzten Versuch
an einem Datenbankausfall scheitert oder das ein zweites Signal an den Daemon
abbricht. Für all diese Fälle ist `retry` der Weg zurück (ein neuer Job, siehe
oben); `document()` zeigt sie als `Phase.ENDED_WITHOUT_RESULT`.

Der API-Abbruch hat eine Eigenheit, die man kennen muss: Er kommt beim Task als
derselbe `CancelledError` an wie das Herunterfahren, und die Retry-Strategie sagt
ja. Neu eingereiht wird er trotzdem nicht, denn `procrastinate_retry_job_v2`
setzt einen Job mit `abort_requested` auf `failed` statt auf `todo` (Procrastinate
3.10.0, am laufenden Daemon geprüft). Im letzten Versuch hat der Exekutor vorher
schon `failed` geschrieben; dann steht ein Ergebnis da.

### MinerU 4, der Worker im Fork und der neue Endpoint

Das Paket spricht nur noch den **MinerU-4-Worker**. Er lebt im Fork
`ConstantinLevin/mineru-runpod` (Basis Upstream v1.13.0), ruft MinerU 4s
`parse_async` und schreibt das Ergebnis mit `ParseResult.save`. **Maßgeblich ist
sein Job-Vertrag**, `docs/content/docs/reference/api.mdx` im Fork; der Code des
Workers (`handler.py`, `worker/parse.py`, `worker/harness.py`) und die
Harness-Bibliothek `runpod-doc-worker` legen die Antwortform fest. MinerU-3.4-
Ergebnisse gibt es nicht zu bedienen (Mineroo#1, „Nur noch MinerU 4").

- **Abgeschickt** werden `tier`, `ocr_mode` und `image_analysis` (Voreinstellung
  `advanced`, `ocr`, `true`), je Dokument aus seiner Eingabe (siehe „Die
  Lese-Einstellungen eines Dokuments stehen an seiner Eingabe"), nie `backend`,
  das der Worker abweist. Außerhalb von `advanced` ist die Bildanalyse als aus
  bestellt (`config.ReadSettings`, und `runpod.job_eingabe` noch einmal), weil sie
  dort nicht wirkt.
- **Zurück** kommt im Tarball (`tarball_b64`, gzip-Tar) je Teilstück:
  `markdown.md`, `markdown_full.md` (Vollmodus, vom Worker gerendert),
  `middle_json.json` (Schema `docvortex.middle` 2.0: Seiten mit `page_idx` und
  `blocks`), `structured_content.json`, `model_output.json` (nur mit
  Modellausgabe) und `images/page_{page_idx}_{typ}_{index}.{endung}`. Die Namen
  stehen fest in `assembly` (`DATEI_*`, `BILDER_VERZEICHNIS`); was auf keinen
  passt, geht nach `extras`. Ohne `markdown.md` ist eine Antwort unverwertbar.
- **Abweisungen**: Ein `FAILED`, dessen `error` mit der Abweisung des Workers
  oder MinerUs beginnt (`errors.DATEIABWEISUNG_KENNUNGEN`,
  `errors.ist_dateiabweisung`), wird zur `WorkerRejectedError` und endet
  `input_error` mit dem Grund aus der Antwort; jedes andere `FAILED` endet
  `failed`.
- **Der neue Endpoint** ist queue-basiert, wird aus dem Fork über RunPods
  GitHub-Integration gebaut und rechnet nur in EWR-Rechenzentren (Spec,
  „Endpoint"). Seine Kennung steht wie jede in der Konfiguration
  (`endpoint_id`), nie im Code; der alte Endpoint `l79lebhinxaqjz` wird nicht
  repariert. Seine echten Antworten im Betriebsweg liegen als Aufnahmen unter
  `artifacts/2026-10-26-mineru-4.0.12-endpoint/` (siehe „Das Testmaterial").
  Das Image wird nie von Hand am Endpoint gesetzt; wie aus dem Fork ein Image
  am Endpoint wird, steht in der `AGENTS.md` des Forks, und woran ein Image
  einmal unveröffentlicht blieb, im Fork-Commit `46dd9e7`.

### Teilstücke sind vor Postgres vollständig abstrahiert

Hinein geht das ganze Dokument, heraus kommt das ganze Dokument. Dass ein PDF
unterwegs in Stücke von höchstens `max_pages_per_chunk` Seiten zerfällt
(`chunking.plan_chunks`), sieht die Datenbank nie: Der Exekutor setzt die
Teilergebnisse **vor** dem Schreiben mit `assembly.assemble` zusammen, und `raw`
ist **ein** Objekt auf Dokumentebene. Es gibt keine Chunk-Tabelle und keine
Teil-Zeilen.

**Die Größe eines Teilstücks begrenzt die Antwort, nicht die Anfrage**
(Mineroo#20). Der Worker weist eine Antwort über 19,4 MB selbst ab
(`ResponseTooLargeError`). Der Daemon gibt das abgewiesene Teilstück in denselben
Zuschnitt zurück: in aufgerundet Antwortgröße/Grenze Teile, ohne Größenangaben
halbiert (Mineroo#49). Passt ein Teil wieder nicht, wird weitergeteilt; erst
eine einzelne Seite mit zu großer Antwort endet `failed`. Die 10 MB, die RunPod
laut Doku und Worker auf `/run` kappen soll, gelten für die Antwort nicht: Eine
Antwort von 14,0 MB kam vollständig an. Die dichtesten echten Scans brachten
729 KB Antwort je Seite; mit 25 % Abstand zu 19,4 MB sind das **20 Seiten**
(`max_pages_per_chunk`), und 20 Seiten × 5,0 s × 3 sind genau `executionTimeout`. Die Rohbytes eines Teilstücks begrenzt
`max_payload_bytes` (**7,4 MB**, kodiert 9,87 MB unter der Anfragegrenze von
`/run`, `config.RUN_ANFRAGEGRENZE_BYTES`; ein größerer Wert wirft beim Bilden der
Einstellung). Das Wartebudget je Teilstück, `job_timeout_seconds` (**300 s**),
folgt derselben Rechnung wie `executionTimeout` des Endpoints (langsamste Seite
5,0 s × 20 × 3) und ist ihm gleich. Belegt mit zwei Live-Jobs am 27.10.2026
(erfundene dichte Scan-Seiten: 24 Seiten, 26,7 MB, abgewiesen; 12 Seiten,
14,0 MB, vollständig angekommen); die Messwerte
stehen in `docs/MinerU-OCR-Kalibrierung-und-Backend-Vergleich.md`, Abschnitt 8.

`chunking.iter_chunks` verwaltet beide Grenzen in derselben Warteschlange:
`send(anzahl)` verlangt die erneute Teilung des zuletzt gelieferten Stücks,
`send(None)` bestätigt es. Bereits ausgepackte Antworten bleiben im Exekutor;
bezahlte Abweisungen stehen auch bei `done` in `raw["unverwertbare_antworten"]`.
Das abgewiesene Stück wird in Teilen erneut bezahlt.

Eine einzelne PDF-Seite über der Anfragegrenze rendert `PdfReader.render_page`
als JPEG mit 200 dpi, längster Kante höchstens 3.500 Pixel und Qualitätsstufe 90.
Erst wenn auch dieses Bild nicht passt, endet das Dokument `input_error`.
`notes` nennt die Originalseiten null-basiert unter `seiten_gerendert`; deren
Seitenbereiche bleiben beim Zusammensetzen und bei der Vollständigkeitsprüfung
die des Originals. Rendern läuft wie Schneiden synchron, weil PDFium nicht
threadsicher ist. Zusätzliche Einstellungen oder ein neues Schema gibt es nicht.

Jede Datei der Antwort hat in `raw` ihren eigenen Schlüssel: `markdown` und
`markdown_full` als Text über alle Teilstücke (`markdown_full` ist `None`, wenn
es einem Teilstück fehlt; die bezahlten Vollfassungen der übrigen stehen dann
unter `markdown_full_je_teilstueck`; verbunden werden die Vollfassungen mit
MinerUs Trennlinie zwischen zwei Seiten, `assembly.SEITENTRENNER_VOLLFASSUNG`,
sonst verriete der Text die Schnittkante), `middle_json`, `structured_content` und
`model_output` als Liste mit einem Eintrag je Teilstück, als JSON-Struktur. In
`middle_json` und `structured_content` zählt jedes Seiten-`page_idx` die Seite
des Originals (`assembly._seiten_verschoben`). **Bilder bekommen je Teilstück
einen eigenen Namensraum**, `images/teilstueck_{n}/…`
(`assembly.BILD_NAMENSRAUM`): MinerU 4 zählt die Bildnamen in jedem Teilstück
ab Seite 0, ohne Namensraum überschrieben sie einander. Jeder Verweis in jeder
Datei des Teilstücks (das Ziel eines Markdown-Bildes, das ganze Feld `image_path`
oder `image_source`, die ganze Quelle eines `<img src>`) wird mit umbenannt, und
`raw["images"]` trägt jedes Bild unter genau diesem Verweis. Text des Dokuments,
der einen solchen Pfad nennt, bleibt stehen.
Leer (`assembly.is_empty`) ist ein Dokument ohne Text in beiden Markdowns, ohne
Block im Middle-JSON und ohne Bild. Eine Seite ohne Bloecke traegt **keinen**
`blocks`-Schluessel (`{"page_idx": 0}`, MinerU schreibt Voreinstellungen nicht),
gemessen am Endpoint in der Stufe `advanced` mit OCR, als Bild wie als PDF.

Eine PDF, die ungeteilt hinausgeht, lädt der Job vorher Seite für Seite
(`PdfReader.check_pages`): Eine Seite, die sich nicht laden lässt, endet
`input_error` ohne RunPod-Kontakt, wie geschnitten in `PdfReader.extract`;
sonst schiede sie erst im Worker aus, bezahlt, als `failed`.

**Der Text für Abnehmer ist `markdown_full`, unverändert** (Mineroo#13):
`assembly.rendered_markdown`, dahinter `OCRWrapper.rendered_markdown` und
`mineru-ocr PATH`. Die Aufnahmen zeigen, dass die Vollmodus-Fassung alle
fünf Randklassen (Kopfzeile, Fußzeile, Seitenzahl, Randtext, Seitenfußnote) je
einmal an ihrer Stelle in MinerUs Lesereihenfolge trägt und die Bild-Deutungen
als eingeklappte Blöcke hinter dem Bild; `markdown` lässt vier der fünf Klassen
weg, und dort stehen oft die Kennungen. Das Paket setzt deshalb nichts selbst
zusammen. Ist `markdown_full` `None` (einem Teilstück fehlte die Datei), wirft
die Darstellung `IncompleteResultError`; ein Ersatz aus `markdown` sähe
vollständig aus und ließe die Randklassen weg.

`chunking.verify_page_coverage` prüft dabei strukturell und in dieser Reihenfolge:
Beginn bei 0 → lückenlose Nachbarschaft → Ende bei `page_count - 1`. Die frühere
Summenprüfung ist entfallen, weil sie von den drei Strukturprüfungen impliziert
wird und, zuerst geprüft, die unbrauchbarste Fehlermeldung erzeugte („falsche
Summe" statt „Lücke zwischen Stück 2 und 3").

**Nach jedem Teilstück wird geprüft, ob seine Antwort vollständig ist**
(`vollstaendigkeit.teilstueck_pruefen`, Mineroo#12), denn der Worker meldet
`COMPLETED` auch, wenn etwas fehlt. Terminal (`MissingArtifactError`,
`MissingPagesError`, beide `TerminalRunPodError`, also über `classify_failure`)
und damit `failed` mit der Antwort zusammengesetzt in `raw` ist: eine Datei aus
`vollstaendigkeit.BENOETIGTE_DATEIEN` fehlt oder steht in `degraded` (ein
`degraded`-Eintrag, der kein Objekt ist, verdeckt die gültigen daneben nicht),
bei PDF und Bild (`seiten_zaehlen`; ein Bild ist eine Seite) sind die `page_idx`
des Middle-JSON nicht genau die geschickten Seiten, und bei jedem Typ hat das
Middle-JSON nicht die Form, die die Darstellung liest
(`assembly.middle_json_lesbar`, derselbe Leser wie `assembly.is_empty`). Die
folgenden Teilstücke werden
dann nicht mehr bezahlt. Ein `degraded` zu einer anderen Datei geht als Vermerk
`unvollstaendige_antwort` nach `notes`. `BENOETIGTE_DATEIEN` ist die eine Liste
der Dateien, die der Darstellungscode liest; wer Code schreibt, der eine weitere
Datei des gespeicherten Ergebnisses braucht, trägt sie dort ein. Das Format von
`degraded` legt der Job-Vertrag fest („Incomplete responses"); die echten
Antworten tragen keins, die Tests leiten es aus ihnen ab
(`ersatz_antworten.abgeleitete_antwort`).

### Die Abruf-Regel ist als Bauweise umgesetzt, nicht als Vorsatz

**Alles holen, alles persistieren, nie filtern.** Die Ergebnisse liegen auf der
Gegenseite nur rund **30 Minuten**; was beim Auspacken fallengelassen wird, ist
danach unwiederbringlich und erzeugt dabei kein Fehlersignal. Deshalb trägt
`assembly.ChunkArtifacts` das Feld **`extras`**, in das alles Unerkannte wandert.
Wer `unpack_response` erweitert, erweitert `extras` mit — ein neues Feld der
Gegenseite darf nirgends stillschweigend verschwinden. (`extras` ist pro Chunk
eine **Liste**, keine Vereinigung: Die Schlüssel wiederholen sich über Stücke
hinweg, und eine Vereinigung behielte nur das letzte.)

### Wer mit wem redet

- `executor.py` kennt **Procrastinate nicht**. Es bekommt Einstellungen, einen
  RunPod-Zugang und eine Verbindung; die Warteschlange liegt darüber in
  `tasks.py`. Deshalb ist der Exekutor ohne Warteschlange testbar.
- `db.py` ist die **einzige** Stelle mit SQL auf `documents` und `inputs`. Wer anderswo eine
  Abfrage schreibt, umgeht den Vertrag; für das Nachsehen aus dem Exekutor gibt
  es `db.get_status_async` und `db.delete_input_async`.
- `runpod.py` ist die einzige Stelle, die HTTP mit RunPod spricht. `RunPodAccess`
  ist das Protokoll dazu — Tests setzen dort eine eigene Umsetzung ein, statt mit
  `type: ignore` an der Klasse vorbeizuarbeiten.
- `pdf.py` ist die einzige Stelle, die `pypdfium2` importiert. `PdfReader` öffnet
  und schneidet aus `bytes` (ein `memoryview` nimmt pypdfium2 nicht an), nie von
  einem Pfad. Die mypy-Ausnahme
  in `pyproject.toml` gilt genau dafür; importiert es jemand woanders, meldet
  mypy es sofort.
- `images.py` ist die einzige Stelle, die `PIL` (Pillow) importiert. Es fragt, ob
  eine Bilddatei ein zweites Bild trägt (TIFF: `Image.seek(1)`; GIF, PNG, WebP:
  `is_animated`, weil `seek(1)` dort das erste Bild dekodiert; nie `n_frames`, das
  jede Seite durchzählt); ja heißt: im Job vor jedem RunPod-Kontakt, auch dem
  lesenden, `input_error` (`MultiImageError`, Hinweis: als PDF einreichen). Die
  Frage läuft nach der Größenprüfung in einem Thread (`asyncio.to_thread`), weil
  ein bösartig gebautes GIF Sekunden bis Minuten braucht. Gefragt wird nur bei TIFF, GIF, WebP und PNG
  (`DocumentType.can_hold_several_images`); JPEG und BMP tragen keine Seitenfolge,
  ein Multi-Picture-JPEG ist ein Foto. Ein Bild, das Pillow nicht lesen kann, geht
  weiter — ob es lesbar ist, entscheidet die Engine.
- `client.py` bietet die vollständige Bibliothek. `cli.py` verbindet Abschicken,
  Warten und Volltextausgabe für Pfade. Nur die CLI verlangt vor dem Einreihen
  einen laufenden Daemon; die Bibliothek behält ihre Warnung ohne Abbruch.
- `core.py` ist der Kern des Abschickens ohne Ein- und Ausgabe (Hash,
  Typ- und Größenprüfung, `Submitted`, dazu `RetryOutcome`, das Bibliothek und
  Kommando teilen), und `client.py` hält die zwei dünnen Schalen darüber:
  `OCRClient` über psycopgs `Connection`, `AsyncOCRClient` über `AsyncConnection`.
  Was beide teilen, ohne Ein- und Ausgabe, steht als Funktion auf Modulebene
  (`_dokumente`, `_gefragte_hashes`, `_daemon_warnung_geben`), die SQL-Texte in `db`.
  Auf dem Weg Abschicken–Warten gibt es keine Brücke wie asgiref oder anyio.

---

## Harte Regeln dieses Codes

Jede steht wegen eines bezahlten Fehlers, nicht wegen einer Vorliebe.

- **`pypdf` ist im ganzen Projekt verboten.** Es bläht beim Schneiden auf: 360 MB
  Quelle wurden zu 4,1 GB Einzelseiten. Geschnitten wird mit `pypdfium2`
  (gemessen 1,00×). Die Begründung steht im Kommentar an der Abhängigkeit.
- **Niemals `/runsync`.** Der Endpoint ist warteschlangenbasiert; `/runsync`
  wartet 60 Sekunden, ein Kaltstart dauert rund 8,5 Minuten. Abgesendet wird über
  `/run`, abgefragt über `/status/<job-id>`, abgebrochen über
  `/cancel/<job-id>`. Abgebrochen wird ein Job, den das Paket aufgibt (Zeitbudget
  abgelaufen, oder das Warten endet mit einem Fehler, etwa der dritten
  widersprüchlichen 402 der Kontopause) und einer, dessen Warten der Daemon beim
  Herunterfahren abbricht — sonst läuft er bei RunPod weiter und kostet. Das
  Zeitbudget (`job_timeout_seconds`) zählt die Zeit in `IN_PROGRESS` über alle
  Läufe des Jobs; Warteschlange und Kaltstart zählen nicht, und die Zeit in der
  Warteschlange hat eine eigene Frist für ununterbrochenes `IN_QUEUE`
  (`queue_timeout_seconds`, 1800 s; auch über `MINERU_OCR_QUEUE_TIMEOUT_SECONDS`
  einstellbar). Eine Bearbeitung setzt diese Frist zurück, eine fehlgeschlagene
  Abfrage nicht. Beim Absenden jedes Teilstücks protokolliert der Exekutor die
  RunPod-Job-ID mit Hash und null-basiertem Seitenbereich; `daemon` zeigt INFO
  standardmäßig auf stderr. Fehlschlagende Abfragen haben eine eigene,
  längere Frist (`poll_failure_timeout_seconds`, 1500 s), denn das Ergebnis liegt
  bei RunPod rund 30 Minuten; solange sie scheitern, gibt auch das Zeitbudget
  nicht auf. Vor dem Aufgeben fragt das Paket ein letztes Mal ab.
- **Der API-Schlüssel steht nur im `Authorization`-Header**, nie in einer URL,
  und wird nie ausgegeben. Er liegt in `~/.runpod/config.toml` im Feld `apikey`.
- **PDFium ist nicht threadsicher.** `asyncio.to_thread` um `pdf.py` herum ist
  deshalb ausgeschlossen; dass der Schnitt die Ereignisschleife blockiert, ist in
  `pdf.py` bewusst dokumentiert und kein Versehen.
- **`procrastinate.App.schema_manager.apply_schema()` ist nicht idempotent.**
  `db.setup` prüft deshalb erst auf die Existenz von `procrastinate_jobs`.
- **Was an RunPod geht, geht in ein EU-Rechenzentrum.** Ein allgemeiner Wrapper
  weiß nicht, ob die Daten personenbezogen sind, und muss deshalb vom Schlimmsten
  ausgehen. „EU“ heißt EWR — EU-Staaten plus Island und Norwegen, denn dort gilt
  die DSGVO —; die zulässige Liste ist der Sollwert `allowed_data_centers` der
  Konfiguration, den der Preflight prüft. Der `s3`-Transport ist nicht die
  Voreinstellung.

---

## Das Testmaterial — was echt ist und was nicht

**Echte MinerU-4-Antworten des neuen Endpoints** liegen unter
`artifacts/2026-10-26-mineru-4.0.12-endpoint/` (Herkunft, Job-Kennungen, Zeiten
und Kosten im `README.md` dort): die Aufnahmen aus Mineroo#10, im Betriebsweg
erzeugt (Stufe `advanced`, OCR, Bildanalyse an), abgeschickt und abgeholt mit
`runpod.RunPodClient`. Das Material ist erfunden und ohne personenbezogene Daten:
normale PDF-Seite mit allen fünf Randklassen, leere Seite als Bild und als PDF,
zwölfseitiges Dokument in drei Teilstücken mit dem Schnitt des Pakets, DOCX,
HTML, eine erzwungene Fehlerantwort und ein PNG mit kaputten Pixeldaten, das
der Worker abweist. Die Tests spielen sie über
`ersatz_antworten.echte_antwort`, `echte_teilstuecke` und `ECHT_ENDPOINT` ab,
auch am Exekutor (`test_echte_mineru4_antworten_ergeben_ein_dokument`; die
Fehlerantwort über die Vorrichtung `echte_fehlerantwort`). Sie haben gezeigt: Die
Bildnamen der Teilstücke kollidieren wirklich, eine leere Seite trägt keinen
`blocks`-Schlüssel, und DOCX und HTML liest der Worker unabhängig von der
Bestellung als `flash` mit `txt`. Neu erzeugen lassen sie sich mit den Skripten
unter `erzeugung/` dort; das kostet Geld.

Daneben liegt unter `artifacts/2026-10-26-mineru-4.0.12-bildanalyse/` auf
demselben Weg eine Seite mit zwei Bildern, die keine Diagramme sind (Mineroo#13,
`ersatz_antworten.ECHT_BILDANALYSE`). Sie zeigt, wo die Bild-Deutungen stehen:
in beiden Markdowns hinter dem Bild als `<details>`, eine Szene als
Beschreibung, ein Organigramm als Mermaid-Graph.

Unter `artifacts/2026-10-27-ganzdokumente/` liegen zwei weitere Läufe zu
Mineroo#17 (ein erfundenes DOC und ein OLE2-Container ohne Word-Daten, über
`mineru-ocr` mit Daemon); die Abweisung spielt `ersatz_antworten.echte_antwort`
mit `ECHT_GANZDOKUMENTE` ab.

**Wo es keine echte Ausgabe gibt, spielen die Tests Ersatz-Antworten aus
`tests/ersatz_antworten.py`.**
Sie sind nicht geraten, sondern nach den Quellen gebaut, die die Antwortform
festlegen, und jede Bauform nennt ihre Quelle: der Job-Vertrag des Forks
(`api.mdx`), der Code des Workers (`handler.py`, `worker/parse.py`,
`worker/harness.py`), die Harness-Bibliothek `runpod-doc-worker` 0.9.1 (Archiv
und Inline-Bilder), MinerU 4.0.12 (Tag `mineru-4.0.12-released`,
`ParseResult.save`, Schema des Middle-JSON) und DocVortex 0.5.16 (Blocktypen,
Bildnamen und ihre Verweise). Sie decken ab, was die Aufnahmen nicht zeigen
(Grenzfälle, Inline-Bilder, fehlende Dateien). Wo sie von den Aufnahmen
abweichen, gilt die Aufnahme.

**Die 3.4-Aufnahmen unter `artifacts/` werden nicht mehr abgespielt.**
`2026-08-05-doku-verifikationstest/` (sechs Inline-Antworten) und
`2026-08-08-erster-tarball-lauf/leere_seite.tarball.response.json` tragen
MinerU-3.4-Dateien (`content_list`, `doc.md`, `doc_middle.json`), die es unter
MinerU 4 nicht gibt; sie sind Geschichte, ersetzt durch die Aufnahmen des
Endpoints. Die zwei personenbezogenen Aufnahmen (`gute_nacht`,
`mehrteilig_*`) sind vor dem ersten Push entfernt worden (Mineroo#2), und die
Tests, die sie lasen, gibt es mit dem Umbau auf MinerU 4 nicht mehr. Die
erzwungene 3.4-Fehlerantwort daneben (`fehlerantwort.response.json`) ist durch
die des Endpoints abgelöst; sie zeigte zuerst, wie RunPod eine `FAILED`-Antwort
umschlägt (`error` auf oberster Ebene, der Rest unter `output`).

---

## Wo die Wahrheit steht

- **Die Spec [Mineroo#1](https://github.com/ConstantinLevin/Mineroo/issues/1)** —
  der Umbau auf MinerU 4 und den neuen Endpoint, mit den Entscheidungen des
  Eigentümers. **Wo sie den August-Plan ablöst, gilt sie.** Was sie ablöst, steht
  dort unter „Was sich daran ändert", dazu alles, was der Umzug auf MinerU 4
  ändert (Backend, Ausgabedateien, Antwortformat).
- **Der Job-Vertrag des Forks**, `docs/content/docs/reference/api.mdx` in
  `ConstantinLevin/mineru-runpod` — was das Paket an den Worker schickt und was
  zurückkommt. Eigentümer ist der Fork; im Zweifel gilt dessen Code.
- **`plans/2026-08-05-ocr-service-plan.md`** — der Entwurf mit allen Begründungen.
  Maßgeblich für „warum ist das so gebaut", soweit die Spec nichts ändert.
- **`plans/2026-08-06-procrastinate-feature-verifikation.md`** — 19 einzeln
  belegte Procrastinate-Stützen, zwei davon am Objekt geprüft (SQL-Schema,
  Quellcode). **Vor jedem Procrastinate-Upgrade durchzugehen**, besonders Punkt 12
  (undokumentierte Heartbeat-Tabelle) und Punkt 19 (`retry_job_by_id`).
- **`docs/README.md`** — der Einstieg mit null Vorkontext, dazu die 20
  übertragbaren Lehren des Projekts und die Pflegeregeln des Dokumentensatzes.
  Dieses Dokument und die drei Fachdokumente darunter sind **Geschichte**: Sie
  beschreiben den Stand bis MinerU 3.4 und den alten Endpoint, tragen oben einen
  Hinweis dazu und werden nicht umgeschrieben. Aktuell gelten `README.md` und die
  Spec. Die Bead-IDs (`jurgen-…`) darin verweisen auf das frühere
  Aufgabensystem.
- **`docs/MinerU-RunPod-Betriebshandbuch.md`** — Betrieb der Engine,
  Job-Contract, Lifecycle, Abrechnung. Abschnitt 17 ist eine Tabelle
  „Nachprüf-Orte": Behauptung → wo man den echten Zustand prüft.
- **`docs/MinerU-RunPod-Geschichte-und-Entscheidungen.md`** — enthält das Glossar
  der Werkzeugbegriffe. Wer die anderen Dokumente ohne dieses Glossar liest,
  stolpert ab der ersten Quellenangabe.

`scripts/ocr_run.py` und `scripts/ocr_pdfs.py` sind die beiden Ad-hoc-Treiber der
früheren Produktionsläufe, mit fest verdrahteten Pfaden und mehreren benannten
Fehlern. Sie sind **Vorlagen und Messlatte, keine Bausteine** — `mineru_ocr` wird
an ihnen gemessen, nicht aus ihnen gepatcht.

---

## Agent skills

### Issue tracker

Die Engineering-Skills führen Issues als GitHub Issues in `ConstantinLevin/Mineroo`, angesprochen über `gh`. Siehe `docs/agents/issue-tracker.md`.

### Triage labels

Die sieben Standard-Labels, jedes heißt wie seine Rolle (`bug`, `enhancement`, `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). Siehe `docs/agents/triage-labels.md`.

### Domain docs

Single-context: ein `GLOSSARY.md` an der Wurzel, ADRs in `docs/adr/`, sobald die erste geschrieben ist. Siehe `docs/agents/domain.md`.

### External peers

Modus `auto`: Claude-Peer auf `claude-opus-5-5` mit Effort `high`, Codex-Peer mit Standardmodell und Effort `high`. Siehe `docs/agents/cross-model.json`.

### Kein Autostart

`mineru-ocr daemon start X` startet ausdrücklich X lokale, vom Terminal gelöste
Prozesse mit je OCR- und Wartungs-Worker. Endpoint, Schlüssel und Schema werden
vor dem Start geprüft. `--concurrency N` stellt die OCR-Plätze je Prozess ein.
`mineru-ocr daemon stop X` beendet bis zu X, die am wenigsten belegten zuerst,
und wartet auf das ordentliche Ende mit `shutdown_graceful_timeout` und der
bestehenden Wiederholungsregel. Sind weniger da, nennt das JSON `requested`,
`stopped` und `running_before`. X muss eine ganze Zahl ab 1 sein.

`mineru-ocr daemon list` liefert eingerücktes JSON: `count` zählt Prozesse,
`daemons` nennt je Prozess `pid`, `occupied`, `available`, `log_path` und den
Kontrollsocket; `slots` summiert belegte und verfügbare OCR-Plätze.
`queue.waiting`/`queue.running` zählen OCR-Jobs in Procrastinate (`todo`/`doing`,
einschließlich für später vorgemerkter Jobs). Ohne Daemon ist `count` null.
Die Wartungsplätze zählen nicht zur OCR-Auslastung.

Lebende Kontrollsockets im privaten Verzeichnis `/tmp/mineru-UID-DBKENNUNG/`
identifizieren die lokalen Prozesse; es gibt keine PID-Kartei. Jobbelegung und
Herzschläge kommen ausschließlich aus Postgres. Ein nicht antwortender oder
veralteter Prozess zählt nicht; alte Socketdateien nach einem Absturz sind
bedeutungslos. Das Verzeichnis enthält auch die eigenen `.log`-Dateien je
Daemon. `list` und `stop` verwalten Daemons auf diesem Rechner für die gewählte
Datenbank; die OCR-Warteschlange gilt datenbankweit. Nach einem Rechnerneustart
startet nichts von selbst. Weder Bibliothek noch Dateiaufruf starten Daemons.
