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
beschreibt ein installierbares Paket samt Kommando `mineru-ocr`, aber gearbeitet
wird aus dem Verzeichnis heraus.

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
mineru-ocr setup        # Schema anlegen (documents + Procrastinates Tabellen)
mineru-ocr daemon       # EIGENES Fenster: hier läuft der Exekutor
mineru-ocr ocr ~/Scans  # einreihen
mineru-ocr status
```

`preflight` verändert nichts und darf jederzeit laufen. Die Exit-Codes sind
belegt (0/1/2/3) und werden von `cli.py` bewusst gesetzt.

---

## Aufbau — was man nicht aus einer einzelnen Datei sieht

### Der Daemon ist ein eigener Prozess

Einreihen und Abarbeiten sind getrennt. `ocr_start` bzw. `mineru-ocr ocr` legt
Jobs an und kehrt sofort zurück; läuft kein `mineru-ocr daemon`, passiert danach
gar nichts, und es sieht aus wie ein Hänger. Das ist die häufigste Überraschung
dieses Aufbaus — `preflight` sagt es einem ausdrücklich, statt es einen suchen zu
lassen.

### Ein Buch, nicht zwei

Die Tabelle `documents` trägt **ausschließlich Endzustände**:
`new | done | empty | failed | input_error`. Es gibt bewusst **kein
`processing`**. „Läuft gerade" wird stattdessen aus dem jüngsten verknüpften
Procrastinate-Job **gelesen** (`queueing_lock = sha256`, `ORDER BY id DESC LIMIT
1`) und nirgends gespiegelt. Wer das nicht weiß, sucht vergeblich nach dem
Feld — oder, schlimmer, führt es ein und erzeugt genau die zwei Bücher, die
auseinanderlaufen.

Daraus folgt: Es gibt keinen Aufräum-Wächter, der verwaiste `processing`-Zeilen
zurücksetzt, weil es keine gibt. Zuständig sind `db.DocumentStatus`,
`db.derive_status` und `db.Phase`; die Verknüpfung entsteht in `tasks.py`.

`empty` ist ein **gültiges** Ergebnis, kein Fehler: Der Gegenstand war leer. Als
Erfolg geführt wäre es unsichtbarer Verlust, als Fehler geführt erzeugte es
endlose Wiedervorlage und Doppelkosten.

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
- `db.py` ist die **einzige** Stelle mit SQL auf `documents`. Wer anderswo eine
  Abfrage schreibt, umgeht den Vertrag; für den Lesezugriff aus dem Exekutor gibt
  es `db.get_document_async`.
- `runpod.py` ist die einzige Stelle, die HTTP mit RunPod spricht. `RunPodAccess`
  ist das Protokoll dazu — Tests setzen dort eine eigene Umsetzung ein, statt mit
  `type: ignore` an der Klasse vorbeizuarbeiten.
- `pdf.py` ist die einzige Stelle, die `pypdfium2` importiert. Die mypy-Ausnahme
  in `pyproject.toml` gilt genau dafür; importiert es jemand woanders, meldet
  mypy es sofort.
- `client.py` und `cli.py` sind zwei Zugänge auf **dieselbe** Fläche. Das
  Kommando enthält keine eigene Logik.

---

## Harte Regeln dieses Codes

Jede steht wegen eines bezahlten Fehlers, nicht wegen einer Vorliebe.

- **`pypdf` ist im ganzen Projekt verboten.** Es bläht beim Schneiden auf: 360 MB
  Quelle wurden zu 4,1 GB Einzelseiten. Geschnitten wird mit `pypdfium2`
  (gemessen 1,00×). Die Begründung steht im Kommentar an der Abhängigkeit.
- **Niemals `/runsync`.** Der Endpoint ist warteschlangenbasiert; `/runsync`
  wartet 60 Sekunden, ein Kaltstart dauert rund 8,5 Minuten. Abgesendet wird über
  `/run`, abgefragt über `/status/<job-id>`.
- **Der API-Schlüssel steht nur im `Authorization`-Header**, nie in einer URL,
  und wird nie ausgegeben. Er liegt in `~/.runpod/config.toml` im Feld `apikey`.
- **PDFium ist nicht threadsicher.** `asyncio.to_thread` um `pdf.py` herum ist
  deshalb ausgeschlossen; dass der Schnitt die Ereignisschleife blockiert, ist in
  `pdf.py` bewusst dokumentiert und kein Versehen.
- **`procrastinate.App.schema_manager.apply_schema()` ist nicht idempotent.**
  `db.setup` prüft deshalb erst auf die Existenz von `procrastinate_jobs`.
- **Was an RunPod geht, geht in ein EU-Rechenzentrum.** Ein allgemeiner Wrapper
  weiß nicht, ob die Daten personenbezogen sind, und muss deshalb vom Schlimmsten
  ausgehen. Der `s3`-Transport ist nicht die Voreinstellung.

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
bezahlten Läufen aufgenommen worden und decken die Lagen ab, für die es vorher
nichts gab:

- `gute_nacht.tarball.response.json` — der gewöhnliche Erfolg, einseitig;
  **vor dem ersten Push entfernt**, weil der Tarball den Text einer privaten
  E-Mail trug (Mineroo#2). Die zwei Tests, die sie lasen
  (`test_echte_tarball_antwort_wird_vollstaendig_ausgepackt`,
  `…_traegt_zwei_content_list_generationen` in `tests/test_assembly.py`), sind
  **ausgesetzt**: Die Vorrichtung `echte_tarball_antwort` überspringt sie mit
  Grund (`pytest -rs`), bis eine Ersatzaufnahme aus Material ohne Personenbezug
  unter demselben Namen liegt.
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
  ohne Personenbezug unter denselben Namen ablegt. Bis dahin läuft der
  Seitenversatz nur gegen erfundene Antworten.

**Was weiterhin nur gegen erfundene Antworten läuft:** eine echte
**Namenskollision** zwischen Bildern zweier Teilstücke (`bild_namenskollisionen`
in `assembly.assemble`). Die Namen sind Inhalts-Hashes, eine Kollision heißt also
dasselbe Bild in zwei Stücken — möglich, aber in diesem Material nicht
vorgekommen.

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
