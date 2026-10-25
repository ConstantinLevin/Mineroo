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
/opt/homebrew/Caskroom/miniconda/base/bin/python3 -m pytest -q                      # alle Tests
/opt/homebrew/Caskroom/miniconda/base/bin/python3 -m pytest tests/test_chunking.py -q   # eine Datei
/opt/homebrew/Caskroom/miniconda/base/bin/python3 -m pytest -q -k "seitenversatz"       # ein Test nach Namensteil
/opt/homebrew/Caskroom/miniconda/base/bin/python3 -m pytest -q -m "not db"              # ohne Datenbank
/opt/homebrew/Caskroom/miniconda/base/bin/python3 -m mypy mineru_ocr tests              # Typprüfung
```

Die Typprüfung läuft **über `mineru_ocr` UND `tests`**, nicht nur über das Paket.
Der Grund steht in `pyproject.toml` an der zweiten mypy-Ausnahme: `tests/` hat
kein `__init__.py`, mypy führt die Dateien deshalb als Module der obersten Ebene,
und eine Ausnahme, die auf `tests.test_pdf` lautete, ginge still ins Leere. Nur
der Lauf über beide Pfade deckt das auf.

Abdeckung, falls gebraucht:

```bash
/opt/homebrew/Caskroom/miniconda/base/bin/python3 -m coverage run -m pytest -q
/opt/homebrew/Caskroom/miniconda/base/bin/python3 -m coverage report --include="mineru_ocr/*"
```

Es gibt keinen Build-Schritt und keinen Linter außer mypy. `pyproject.toml`
definiert das Kommando `mineru-ocr` unter `[project.scripts]`; es ist als Befehl in
die Miniconda-Base installiert, in der seine Abhängigkeiten liegen (siehe „Das
Kommando selbst“). Gearbeitet und getestet wird trotzdem aus dem Verzeichnis
heraus.

**Testausgabe immer vollständig in eine Datei schreiben, bevor gefiltert wird.**
Also `python3 -m pytest -q > /pfad/zur/ausgabe.txt 2>&1`, dann die Datei lesen
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

Neun Unterkommandos in `mineru_ocr/cli.py`: `setup`, `daemon`, `ocr`, `status`,
`show`, `wait`, `markdown`, `retry`, `preflight`. Der übliche Ablauf:

```bash
mineru-ocr setup        # Schema anlegen (documents, inputs + Procrastinates Tabellen)
mineru-ocr daemon       # EIGENES Fenster: hier läuft der Exekutor
mineru-ocr ocr ~/Scans  # einreihen; nennt je Datei Pfad und Hash
mineru-ocr status
```

Nur `setup` legt das Schema an. Daemon und jeder `OCRClient` prüfen es beim
Öffnen rein lesend (`db.check_schema`): Fehlt es oder stammt die Tabelle aus der
Zeit vor dem letzten Update (mit `new`), endet der Start mit `DatabaseError` und
dem Weg, statt dass ein Job an einer alten Zeile ohne Ergebnis endet. Ausgenommen
ist `preflight` (`OCRClient(schema_check=False)`): Er liest beide Tabellen nicht
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

`preflight` verändert nichts und darf jederzeit laufen. Er prüft, was tatsächlich
läuft: das Image von Template und Workern gegen den erwarteten Tag, die
Endpoint-Variablen nach Wert an der Umgebung aller zuletzt gelaufenen Worker (auch
der beendeten), dass alle Rechenzentren in der zulässigen Liste liegen, und die
Sollwerte des Endpoints; FlashBoot wird berichtet. Was sich nicht feststellen lässt,
heißt „nicht prüfbar“, nie „in Ordnung“. Die Exit-Codes sind belegt (0/1/2/3/4,
dazu 130 bei Strg-C) und werden von `cli.py` bewusst gesetzt.

`wait ZIEL… [--timeout S] [--json]` wartet auf viele (`OCRClient.as_completed`)
und schreibt je Dokument eine Zeile, sobald es endgültig ist (mit `--json` eine
JSON-Zeile ohne `raw`); war ein Ziel ein Pfad, nennt die Zeile ihn mit (in JSON
`paths`), denn die Zeilen kommen in der Reihenfolge, in der die Dokumente fertig
werden. Exit-Code 0, wenn alle `done` oder `empty` sind; 3, wenn
eines `failed` oder `input_error` ist; **4, wenn nach Ablauf der Frist noch eines
offen ist** (die offenen nennt stderr); 1 bei einem Betriebsfehler, auch einem
unbekannten Hash; 2 bei einer Frist, die keine Zahl ab null ist (`nan`, negativ);
130 bei Strg-C. Bei mehreren gilt der schwerste, 1 vor 4 vor 3
vor 0: 4 verlangt vom Aufrufer eine Handlung (später erneut warten), die
Fehlschläge stehen ohnehin in den Zeilen. Ohne `--timeout` wartet `wait`, bis die
Datenbank zurück ist, wenn sie unterwegs wegbricht, auch wenn sie gelöscht oder
der Zugang entzogen wurde; in Skripten deshalb `--timeout` setzen.

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
| `setup`, `get`, `wait`, `show`, `markdown`, `retry` | nein | nein |
| Abschicken (`ocr_start`, `ocr`) | nein (den Endpoint trägt erst die Ergebniszeile, die der Job anlegt) | nein |
| `daemon`, `preflight`, `workers` | ja | ja |
| `status`, `credit`, `account` | nein | ja |

Den Schlüssel verlangt `RunPodClient` beim Bauen, die Lesefragen des Clients
vor dem Fragen; Endpoint und Schlüssel prüft `daemon.run` vor dem Start der Worker,
auch für Aufrufer aus Python. Der Preflight beim Öffnen eines `OCRClient` läuft nur,
wenn ein Schlüssel eingestellt ist; ohne entfällt er still. Die Sollwerte des Preflights (`expected_image_tag`, `workers_min`,
`workers_max`, `idle_timeout`, `execution_timeout_ms`, `allowed_data_centers`, die
Tabelle `[endpoint_env]`) sind Felder von `Settings` mit den Voreinstellungen aus
Mineroo#3 (0, 10, 60, 600000). Ein unbekannter Schlüssel in der Datei, eine
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
`as_completed`, `get`, `document`);
`cli.py` liest Dateien und Ordner, hasht sie und nennt die Zuordnung von Pfad zu
Hash. `ocr --json` gibt je Dokument Hash, Zustand (`null`, solange es offen ist)
und ob eingereiht, dazu die übersprungenen Dateien. `show` zeigt, was `document()`
sagt, ohne `raw`; `show --raw` gibt die vollständige Antwort als JSON aus. Eine ausdrücklich genannte, nicht unterstützte oder zu große
Datei hält die übrigen nicht auf, `ocr` endet dann mit Exit-Code 1; in Ordnern
werden solche Dateien übersprungen und gezählt. Auf stdout stehen nur Daten,
Meldungen und Warnungen (auch die der Bibliothek) gehen auf stderr. Es gibt in der
Bibliothek keinen Parameter, der ein Pfad ist; `ocr_path`, `OCRWrapper.from_path`,
`from_bytes` und `OCRMegaWrapper` gibt es nicht mehr.

---

## Aufbau — was man nicht aus einer einzelnen Datei sieht

### Der Daemon ist ein eigener Prozess

Einreihen und Abarbeiten sind getrennt. `ocr_start` bzw. `mineru-ocr ocr` legt
Jobs an und kehrt sofort zurück; läuft kein `mineru-ocr daemon`, passiert danach
gar nichts, und es sieht aus wie ein Hänger. Das ist die häufigste Überraschung
dieses Aufbaus — das Abschicken warnt, und `status` und `preflight` sagen es einem
ausdrücklich, statt es einen suchen zu lassen.

**Abschicken hängt nicht vom Daemon ab und warnt nur.** `ocr_start` und
`mineru-ocr ocr` reihen auch ohne laufenden Daemon ein; die Warnung steht im
Logger `mineru_ocr.client`, die Kommandozeile schreibt sie auf stderr. Abgebrochen
wird nichts, und weder Bibliothek noch Kommandozeile starten selbst einen Daemon —
den startet der Betreiber. Es gibt keinen `strict`-Abbruch und keine
`--strict`-Option am Kommando `ocr`; `strict` betrifft nur Konfigurations-
Abweichungen im Preflight. Eingabe und Job entstehen je Dokument in einer
Transaktion (die Ergebniszeile erst am Ende des Jobs); ohne Daemon bleibt deshalb
zu jeder Eingabe ein Job zurück. Gewarnt
wird einmal je daemonloser Phase eines Clients und nur, wenn nach dem Aufruf
Dokumente offen sind, eingereiht oder schon unterwegs (ein erneuter Lauf, der nichts
mehr einreiht, sähe ohne Hinweis wie ein Hänger aus). Ein `--no-start`, das nur
ablegt, gibt es nicht mehr.

Ein Daemon **läuft**, wenn ein Worker einen Herzschlag hat, der jünger ist als
`config.DAEMON_HEARTBEAT_MAX_AGE_SECONDS` (30 s, Procrastinates
`stalled_worker_timeout`), gemessen an der Uhr der Datenbank
(`db.worker_heartbeats`). Die Zeile eines abgestürzten Daemons bleibt bis zum
nächsten Start stehen; ihr Alter ist das Merkmal, und abgeräumt wird sie dann von
Procrastinate selbst. Die Lebendigkeit ist nur Auskunft für `status` und
`preflight`; ist sie nicht lesbar (die undokumentierte Worker-Tabelle fehlt),
meldet `status` den Daemon als „unbekannt“, und das Abschicken warnt und reiht
trotzdem ein.

### Welcher Prozess welche App hält

Procrastinate wird so benutzt, wie es vorgesehen ist: **jeder Prozess baut seine
App einmal und behält sie.** Es gibt keine App auf Modulebene (`tasks.py` hat
keine), und nirgends wird ein Konnektor zur Laufzeit umgeschaltet — kein
`replace_connector`, kein `with_connector`, kein Zuweisen an `app.connector`.

| Prozess | App | Gebaut von | Pool |
|---|---|---|---|
| `mineru-ocr daemon` | **genau eine**: `PsycopgConnector`, die Tasks samt Retry-Strategie und der Waechter angemeldet | `tasks.build_daemon_app` | der des Daemons (`AsyncConnectionPool`, `check=AsyncConnectionPool.check_connection`), per `app.open_async(pool=…)` uebergeben |
| jeder `OCRClient` (auch hinter dem Kommando) | **eine eigene**: `SyncPsycopgConnector`, **keine** angemeldeten Tasks | `tasks.build_client_app`, geoeffnet in `OCRClient.__enter__` | `NullConnectionPool(max_size=1, num_workers=1)`, vom Client angelegt **und** geschlossen |
| `mineru-ocr setup` | eine fluechtige, nur fuers Schema | `db._procrastinate_schema_anwenden_falls_noetig` | der Konnektor-eigene |

Was daraus folgt und nicht aus einer einzelnen Datei hervorgeht:

- **Der Daemon-Prozess baut keine zweite App.** Jede weitere App im selben Prozess
  benennt Procrastinates eingebauten Task `remove_old_jobs` um. (Die Tests haben
  zwei Apps im Prozess, das ist harmlos; dieses Paket benutzt `remove_old_jobs`
  nicht.)
- **Der Client reiht per Namen und mit ausdruecklicher Queue ein**, Name und Queue
  aus denselben `config`-Konstanten wie die Anmeldung des Daemons:
  `tasks.enqueue_document` ruft `app.configure_task(name=…, queue=…, lock=…,
  connection=…).defer(sha256=…)` und gibt die Kennung des Jobs zurueck. Ohne
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
  mit je einer kurzlebigen Verbindung. Je Agent bleiben so ein bis zwei
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
(`wait`, `wait_all`, `as_completed`) liefert daraus Hüllen.

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
Zeitstempel-Vergleich gibt es dafür nicht. Gleichzeitige `retry` auf dasselbe
Dokument laufen nacheinander (`db.retry_lock`, eine Advisory-Sperre der Sitzung um
Lesen und Handeln): Ohne sie läsen zwei Aufrufer beide „kein Job“ und reihten
zwei Jobs ein, die beide bezahlt würden. Fehler von Procrastinate gehen nach
oben, keiner wird als „nicht wiederholt" verschluckt; ein abgelehntes
Zurücksetzen kommt als `DatabaseError` des Pakets an. Bekannte Grenze: Setzt
jemand denselben Job im selben Moment von Hand außerhalb des Pakets zurück
(Procrastinate-Shell oder Admin), kann `retry_job_by_id` einen schon wieder
laufenden Job noch einmal starten (`procrastinate_retry_job_v2` nimmt auch
`doing` an); `db.retry_lock` schützt nur vor Aufrufern des Pakets. Das Kommando `retry` (Ziel
oder `--all`) fragt vorher nach, außer mit `--yes`, und ruft genau eine der
beiden Funktionen.

`empty` ist ein **gültiges** Ergebnis, kein Fehler: Der Gegenstand war leer. Als
Erfolg geführt wäre es unsichtbarer Verlust, als Fehler geführt erzeugte es
endlose Wiedervorlage und Doppelkosten.

### Die Eingabe liegt als Bytes in der Datenbank, nicht als Pfad

Die Bytes eines Dokuments stehen in der Tabelle `inputs` (`sha256 text PRIMARY
KEY`, `content bytea NOT NULL`, `job_id bigint`), **solange das Dokument kein
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

### Teilstücke sind vor Postgres vollständig abstrahiert

Hinein geht das ganze Dokument, heraus kommt das ganze Dokument. Dass ein PDF
unterwegs in Stücke zu 20 Seiten zerfällt (`chunking.plan_chunks`), sieht die
Datenbank nie: Der Exekutor setzt die Teilergebnisse **vor** dem Schreiben mit
`assembly.assemble` zusammen, und `raw` ist **ein** Objekt auf Dokumentebene. Es
gibt keine Chunk-Tabelle und keine Teil-Zeilen.

`chunking.verify_page_coverage` prüft dabei strukturell und in dieser Reihenfolge:
Beginn bei 0 → lückenlose Nachbarschaft → Ende bei `page_count - 1`. Die frühere
Summenprüfung ist entfallen, weil sie von den drei Strukturprüfungen impliziert
wird und, zuerst geprüft, die unbrauchbarste Fehlermeldung erzeugte („falsche
Summe" statt „Lücke zwischen Stück 2 und 3").

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
- `client.py` und `cli.py` sind zwei Zugänge auf **dieselbe** Fläche. Das
  Kommando enthält keine eigene Logik; jedes ruft genau eine Funktion der
  Bibliothek.
- `core.py` ist der Kern des Abschickens ohne Ein- und Ausgabe (Hash,
  Typ- und Größenprüfung, `Submitted`, dazu `RetryOutcome`, das Bibliothek und
  Kommando teilen), und `client.py` ist die dünne Schale darüber
  (psycopgs `Connection`; die asynchrone Schale über `AsyncConnection` kommt später).
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
  Warteschlange hat keine Obergrenze. Fehlschlagende Abfragen haben dieselbe
  Frist. Vor dem Aufgeben fragt das Paket ein letztes Mal ab.
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

Es gibt **zwei** Sätze echter Antworten, und der Unterschied zwischen ihnen ist
der Unterschied zwischen dem Weg, den der Betrieb geht, und dem, den er nicht
geht. Beide sind ausführlich in `tests/conftest.py` beschrieben, jeder an seiner
eigenen Konstante.

**`artifacts/2026-08-05-doku-verifikationstest/` — sechs Antworten mit
`transport: "inline"`.** Der Betrieb versendet `tarball_b64`; diese sechs zeigen
also nicht den Produktionspfad. Alle sechs sind einseitige Bilder mit
`page_idx == 0`.

**`artifacts/2026-08-08-erster-tarball-lauf/` — Antworten mit `tarball_b64`,
also der Produktionspfad.** Sie sind am 08./09.08.2026 in
bezahlten Läufen aufgenommen worden und deckten die Lagen ab, für die es vorher
nichts gab. Zwei der Aufnahmen (vier Antwortdateien) sind vor dem ersten Push
entfernt worden; im Repository liegen noch `leere_seite` und `fehlerantwort`:

- `gute_nacht.tarball.response.json` — der gewöhnliche Erfolg, einseitig;
  **vor dem ersten Push entfernt**, weil der Tarball den Text einer privaten
  E-Mail trug (Mineroo#2). Die zwei Tests, die sie lasen
  (`test_echte_tarball_antwort_wird_vollstaendig_ausgepackt`,
  `…_traegt_zwei_content_list_generationen` in `tests/test_assembly.py`), sind
  **ausgesetzt**: Die Vorrichtung `echte_tarball_antwort` überspringt sie mit
  Grund (`pytest -rs`), bis das Ticket zum MinerU-4-Vertrag eine Ersatzaufnahme
  aus Material ohne Personenbezug unter demselben Namen ablegt.
- `leere_seite.tarball.response.json` — die **leere Seite**. Sie hat das
  `empty`-Kriterium am echten Verhalten festgestellt und zwei Fehler ans Licht
  gebracht, die einander verdeckten.
- `fehlerantwort.response.json` — eine echte `FAILED`-Antwort samt Traceback des
  Workers. Nebenbei hat sie die Liste der gültigen Backend-Namen geliefert, die
  jetzt in `config.GUELTIGE_BACKENDS` steht.
- `mehrteilig_0_seiten_0-4` / `_1_seiten_5-9` / `_2_seiten_10-11` — **ein**
  zwölfseitiges Dokument über drei Teilstücke, **vor dem ersten Push entfernt**,
  weil es den Inhalt eines Steuerdokuments mit personenbezogenen Daten trug
  (Mineroo#2). Die zwei Tests, die es lasen (Seitenversatz über drei Teilstücke
  und Randblöcke in `tests/test_assembly.py`), sind **ausgesetzt**: Die
  Vorrichtung `echte_mehrteilige_antworten` überspringt sie mit Grund
  (`pytest -rs`), bis das Ticket zum MinerU-4-Vertrag neue Aufnahmen aus Material
  ohne Personenbezug unter denselben Namen ablegt. Bis dahin laufen der
  Seitenversatz und die Randblöcke nur gegen erfundene Antworten.

**Was nur gegen erfundene Antworten läuft:** eine echte
**Namenskollision** zwischen Bildern zweier Teilstücke (`bild_namenskollisionen`
in `assembly.assemble`). Die Namen sind Inhalts-Hashes, eine Kollision heißt also
dasselbe Bild in zwei Stücken — möglich, aber in diesem Material nicht
vorgekommen. Bis die Ersatzaufnahmen liegen, außerdem alles, was die zwei
entfernten Aufnahmen trugen: der gewöhnliche, nicht leere Tarball-Erfolg (Bilder
und Inhalt in beiden `content_list`-Generationen; Layout-PDF, zweite Generation,
fehlendes Span-PDF, Umschlag und `source` prüft weiter `leere_seite`), der
Seitenversatz über mehrere Teilstücke und dass ein Randblock, dessen Text schon
im Markdown steht, nicht noch einmal angehängt wird.

---

## Wo die Wahrheit steht

- **Die Spec [Mineroo#1](https://github.com/ConstantinLevin/Mineroo/issues/1)** —
  der Umbau auf MinerU 4 und den neuen Endpoint, mit den Entscheidungen des
  Eigentümers. **Wo sie den August-Plan ablöst, gilt sie.** Was sie ablöst, steht
  dort unter „Was sich daran ändert", dazu alles, was der Umzug auf MinerU 4
  ändert (Backend, Ausgabedateien, Antwortformat).
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
