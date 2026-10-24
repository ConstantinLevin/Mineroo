# Plan: `mineru_ocr` — der OCR-Service auf Postgres + Procrastinate

> **Hinweis (Mineroo#2):** Dieser Plan beschreibt den Stand bis MinerU 3.4 und den alten Endpoint. Er bleibt maßgeblich für das, was die Spec [Mineroo#1](https://github.com/ConstantinLevin/Mineroo/issues/1) nicht ändert. Abgelöst ist alles, was dort unter „Was sich daran ändert“ steht, dazu alles, was der Umzug auf MinerU 4 ändert (Backend, Ausgabedateien, Antwortformat). Die Liste steht in der Spec und wird hier nicht wiederholt. Aktuell gelten das [README](../README.md) des Repositories und die Spec.

**Stand:** 06.08.2026, vor Implementierung — **mit Nachträgen vom 07.–09.08.2026, siehe unten.**

> **DIESER PLAN IST NICHT MEHR DER STAND DES CODES.** Er ist gebaut worden, und beim Bauen und beim ersten Betrieb gegen den echten Endpoint sind mehrere seiner Festlegungen zurückgenommen worden — teils durch Jurgen, teils am Material. Zurückgenommene Stellen sind an Ort und Stelle als solche gekennzeichnet und **nicht überschrieben** (Pflegeregel 2 des Dokumentensatzes: die Anatomie eines Irrtums ist wertvoller als die zurückgenommene Aussage). Wer diesen Plan als Auftrag liest, muss die Kästen mitlesen.
>
> Die tragenden Rücknahmen im Überblick:
> - **4.1 / 6 — `raw` ist EIN zusammengesetztes Objekt**, keine Liste voller Antworten; zusammengesetzt wird im Executor vor dem Schreiben (Jurgen, 07.08.2026).
> - **6 — kein Span-PDF.** Der Tarball trägt es nicht, in keinem Transport; die Begründung stand auf einem Fehlschluss über einen Signatur-Default (am MinerU-Quelltext geklärt, 09.08.2026).
> - **7 — die Getter-Schicht, in zwei Schritten zurückgenommen.** Beim Bau: `assembled_text` hieß `rendered_text`, `artifacts_per_chunk` und `raw_responses` entfielen. Am 09.08.2026 hat Jurgen die Schicht als Ganzes zurückgenommen: `markdown` und `content_list` gibt es nicht mehr, an ihrer Stelle steht der generische Zugang `huelle["<feld>"]` auf `raw`, und `rendered_text` heißt `rendered_markdown` — eine Eigenschaft ohne jeden Schalter (Kasten in 7).
> - **10.2 — das `empty`-Kriterium ist inzwischen am echten API-Verhalten festgestellt** worden, wie es die Vorgabe verlangte; es steht in `assembly.is_empty` samt Messung. **Status: Erste Kaltleser-Messrunde (plan-intention-review, 3 Sonden) gelaufen; Befund-Inventur von Jurgen korrigiert und eingearbeitet (Abschnitt 2.4). Am 06.08. zusätzlich auf Jurgens Festlegung „ein Buch, nicht zwei" umgestellt: kein `processing`-Status, kein Abgleich-Wächter — der Dokument-Zustand während der Arbeit wird aus dem jüngsten verlinkten Procrastinate-Job GELESEN statt doppelt geführt (2.4/4.2). Ebenfalls am 06.08.: der Widerspruch zwischen Terminal-Guard und `retry_failed` aufgelöst (der Guard deckt nur noch `done|empty|input_error` — ein gescheitertes Dokument wird beim Retry wirklich neu verarbeitet), die Restformulierungen des alten Modells (`processing`, `reconcile`) bereinigt und zwei nachgezogene Procrastinate-Belege eingearbeitet: `lock`-Freigabe bei jedem Job-Ende und `retry_job_by_id` nimmt nur `doing|failed` bei `attempts+1` (Feature-Verifikation Punkte 18/19, teils am Objekt der installierten 3.9.0). Wiederholungs-Messung ausstehend; nichts hieraus ist gebaut.**
**Auftraggeber:** Jurgen. **Verfasser:** Agent (Konsolidierung aus Jurgens Chat-Vorgaben vom 05.08.2026, der bestehenden Betriebsdokumentation und der Procrastinate-Doku-Erkundung vom selben Tag).

---

## 0. Wozu dieses Dokument, und wie man es liest

Dieses Dokument beschreibt für einen Leser **ohne jeden Vorkontext**, was gebaut werden soll, und — das ist sein eigentlicher Zweck — **warum jede Entscheidung so gefallen ist und nicht anders**. Es existiert, weil der Weg hierher über drei verworfene Architektur-Ideen führte und sich ein Implementierer ohne die Begründungen an genau denselben Stellen wieder verrennen würde. Wo eine Entscheidung offen ist, steht das ausdrücklich; nichts Offenes ist stillschweigend zugunsten einer Variante entschieden.

Leserichtung: Abschnitt 1 sagt, worum es geht. Abschnitt 2 hält die Vorgaben des Auftraggebers **im Wortlaut** fest und erzählt, wie die Architekturwahl sich entwickelt hat — wer nur Abschnitt 2 liest, versteht bereits, was das Ding werden soll und was ausdrücklich nicht. Abschnitte 3–7 sind das technische Zielbild. Abschnitt 8 listet die harten Plattform-Regeln, die jede Implementierung einbauen MUSS (alle aus teuer bezahlten Fehlern). Abschnitte 9–13: Umgebung, Tests, Nicht-Ziele, offene Entscheidungen, Bau-Reihenfolge.

**Vorausgesetzte Hintergrund-Dokumente** (in diesem Repo unter `docs/`; dieses Plandokument wiederholt daraus nur, was der Implementierer unmittelbar braucht):
- `MinerU-RunPod-Betriebshandbuch.md` — die Engine-Seite: Job-Contract, Kosten, Lifecycle, harte Regeln. **Pflichtlektüre vor dem Bau.**
- `MinerU-RunPod-Geschichte-und-Entscheidungen.md` — warum die Engine so ist, wie sie ist; Entscheidungsregister.
- `MinerU-OCR-Kalibrierung-und-Backend-Vergleich.md` — was die Engine auf welchem Material leistet.
- Dieser Service ist **Stufe 3 einer größeren Pipeline und nur Stufe 3** — er kennt keine DEVONthink-Records, nur Dateien rein, Text/Struktur raus; die Stufen davor und danach liegen nicht in diesem Repository.
- `plans/2026-08-06-procrastinate-feature-verifikation.md` — **jede Procrastinate-Feature-Stütze dieses Plans, einzeln belegt** (19 Punkte: 17 gegen die offizielle Doku, zwei Nachträge vom 06.08. teils am Objekt — Quellcode und SQL-Schema der installierten 3.9.0; je BESTÄTIGT/PRÄZISIERT/NICHT BELEGT, mit Zitat und Quellseite). Wo unten „Verifikation Punkt N" steht, ist das der Verweis dorthin; die NICHT-BELEGT-Punkte sind als Objekt-Tests im Testplan (10.2) eingeplant.

---

## 1. Ausgangslage und Auftrag

Es existiert eine produktionsbewiesene OCR-Engine: **MinerU** (VLM-basiert, Modell `MinerU2.5-Pro-2605-1.2B`) auf einem **queue-basierten RunPod-Serverless-Endpoint** (`l79lebhinxaqjz`). Ein HTTP-Job = eine Datei (oder ein Seitenbereich davon) als Base64; die Antwort trägt Markdown plus Strukturdaten. Der Endpoint skaliert selbst von 0 auf N Worker und zurück; zwischen Läufen kostet er nichts. Zweifach in Produktion bewiesen (593 und 932 Seiten, null Fehler), am 05.08.2026 zusätzlich durch einen 6-Seiten-Doku-Verifikationstest bestätigt.

Was fehlt, ist die **lokale Seite**: ein wiederverwendbares Stück Software, mit dem man Dateien zur OCR gibt und Ergebnisse zurückbekommt — statt der bisherigen Ad-hoc-Skripte mit hartkodierten Pfaden (`scripts/ocr_run.py`, `scripts/ocr_pdfs.py`, dokumentierte Fehlerlisten in Betriebshandbuch 11). Dieses Stück Software ist der Auftrag.

Es muss zwei Betriebsarten tragen: den **Einmal-Bulk** (~30.000 Dokumente des DEVONthink-Bestands — Zahl unbelegt, siehe Geschichte 6.15) und den **laufenden Eingang** (~1.000 Neudokumente/Jahr, tröpfelnd). Und es muss den Abbruch/Wiederanlauf als Normalfall behandeln: Bei einem Lauf über Stunden ist der Crash kein Randfall, sondern Designannahme.

---

## 2. Die Vorgaben des Auftraggebers im Wortlaut — und wie die Architektur sich entwickelt hat

Die folgenden Zitate sind roh und unkorrigiert aus dem Chat vom 05.08.2026 übernommen (Tippfehler inklusive), weil sie der Maßstab sind, an dem das Gebaute gemessen wird. Die Reihenfolge ist chronologisch; sie zeigt drei Architektur-Stufen, von denen die ersten beiden verworfen wurden — **von Jurgen selbst, nicht vom Agenten**.

### 2.1 Der ursprüngliche Braindump (Stufe 1: In-Process-Modul mit RocksDict)

> „also ich dachte wir machen das so dass es einen python client gibt wenn der startet schuat der ob alles passt und der python client checkt z.b. ob bei runpod konfiguration alles so ist wie es sein muss. das client objekt muss auch dynmiasche attribute wie runpodcredit haben die per getter direkt von der api gegettet werden wenn man darauf zugreift."

> „.ocr(BYTES OR OCRWRAPPER) .ocr_path(PATH), .ocr_batch(OCRWRAPPER) (natürlich wird der MIME type zuerst analysiert und rejected falls nicht unterstüzter type. es wird immer ein objekt der klasse OCRWRAPPER zurückgegeben. OCRWRAPPER kann auch input sein aber es ist so designed wenn ocrwrapper bereits OCR'd ist dann ist es eine noop"

> „die ocrwrapper selber speichern memetyp, pfad (damit die inputbytes nicht alle im ram liegen werden sie wenn gebraucht vom pfad kurz gelesen), ocrstatus (dynamisch returned True wenn es output gibt), ocroutput (der validiert wird) und was sonst noch nützlich ist."

> „vor einem run wird auch immer die inputdatei von OCRWRAPPER initialisierung gehashed und geschaut ob es im rocksdict bereits dafür einen eintrag gibt […] das OCRMEGAWRAPPER muss automatisch bei start die hashes von allen inputpfaddatein mit rocksdict abgleichen und statt neue ocrwrapper zu erstellen für die für die es bereits einen eintrag gibt ruft es den ocrwrapper aus dem rocksdict ab, falls der fertig ist perfekt dann ist eh fertig wenn nciht dann macht er ihn fertig"

**Präzisierungen, die dabei fielen und weiterhin gelten:**
- Zu `ocrstatus`: > „\"success\" ist eher gemeint also ob das ocr geklappt hat" — plus zur Metadaten-Frage: > „ja alles an metadaten etc was speicherbar ist in den objekten speichern. maximalismus."
- Zur Ausgabe-Formatierung: > „wir speicehrn lediglich […] den ocrwrapper samt raw output darin und die getter formatieren nur on the fly (von mir aus auch gerne gecached) den output in die gebracuhten funktionen. oder es kann methoden geben die prüfungen machen alles convenience zeugs"
- Zum MegaWrapper: > „der megarapper braucht natürlich auch statusübersicht etc. also er braucht methoden die den status z.b. aller in ihm aggregiert (programmatisch und per text ausgebbar)" und das Ordner-Szenario: > „stell dir vor man hat einen OCRMEGAWRAPPER aus einem ordner initialisiert und dann lässt man laufen, dann später kommt zum ordner noch ein pfad hinzu den tut man einfach dazu und lässt nochmal durchlaufen weil eh nur läuft was noch fehlt"
- Zum transparenten Zerteilen großer Dokumente: > „es [muss] so gehandled werden dass es in genügend kleine stücke geteilt wird beim senden und das was zurückkommt dann richtig zusammengesetzt wird so das es für den user nicht bemerkbar ist dass überhaupt was passiert ist."

### 2.2 Die Async-Frage (Stufe 2: verworfen zugunsten von Stufe 3)

Jurgen fragte zunächst nach einem async-Client („natürlich wird es async"), dann — nach der Recherche, dass RunPod kein Batch-Submit kennt und die Parallelität serverseitig in der Queue lebt — selbst: > „Was ich mir auch gedacht habe wie wäre es wenn wir nicht draufloßbomben mit dem await sondern sauber schaeun wie batching und queue bei mineru funktioniert damit wir kein async gefidel haben später sondern es sauber auf einmal machen". Auf den Einwand, ein In-Memory-Future überlebe keinen Prozess-Crash, kam die entscheidende Wende:

### 2.3 Die gewählte Architektur, in Jurgens Worten (Stufe 3: Postgres + Service)

> „achso das war aber ein komplett anderes design in dem der executor komplett unabhängig läuft von allem andern und nur jobs in postgres entgegennimmt und dann asynct man einfach bis ein status einer line anders ist. eigentlich ist das echt das perfekte. also es gibt die postgres (evlt mit procrastinate als queue ohne das wir viel homebrewen müssen) dann die schleife die als service läuft und immer wenn was auf postgres geht geht es los und es relayed die outputs dann alle in die raw section und setzt job und status auf fertig. dann das losschicken ist einfach einen job erstellen und das warten auf eine antwort kann dann entweder darin oder am ende eine seperate funktion das awaiten des resultats sein."

**Warum diese Wahl trägt (die Intention, ausgeschrieben):**
1. **Das „Promise" ist eine Datenbank-Zeile, kein In-Memory-Future.** Es überlebt jeden Crash, jeden Neustart, jede Session. „Warten" heißt: auf den Statuswechsel der Zeile schauen — von überall, jederzeit, auch Tage später. Das ist die einzige Await-Semantik, die zur Designannahme „Abbruch ist Normalfall" passt.
2. **Die Nebenläufigkeit lebt an genau einer Stelle: im Daemon.** Producer sind trivial synchron (eine Zeile einfügen). Das von Jurgen benannte „async gefidel" — farbige Funktionen überall, Event-Loop-Zustand im Client — entfällt vollständig.
3. **Beide Betriebsarten mit demselben Stück:** Der 30k-Bulk ist „viele Zeilen einfügen"; der laufende Eingang ist „gelegentlich eine Zeile einfügen". Der Daemon behandelt beides identisch.
4. **Wenig Eigenbau** („ohne das wir viel homebrewen müssen"): Retry mit Backoff, Crash-Erkennung (Worker-Heartbeats + Stalled-Job-Wiederaufnahme), Dedup beim Einreihen (queueing_lock), periodische Wächter-Tasks, LISTEN/NOTIFY, graceful Shutdown — alles fertige, dokumentierte Procrastinate-Funktionen (Doku-Erkundung 05.08.2026, alle einzeln verifiziert).
5. **Es ist Jurgens eigenes Juli-Design.** Das Entscheidungsregister (Geschichte, Punkte 3–5) hält seit dem 03.07.2026 fest: lokale Seite = Procrastinate-Daemon auf PostgreSQL, `to_md(path)`-Abstraktion, „Batch-Nutzung awaitet nie — Fertigstellung ist eine SQL-Abfrage". Dieses Dokument setzt das um; neu ist nur die Convenience-Schicht aus dem Braindump (2.1) obendrauf.

**Was aus Stufe 1 überlebt und was ersetzt wird:** Die komplette Wrapper-/MegaWrapper-Fassade, die Hash-Identität, die Noop-Semantik, der Maximalismus, das Ordner-Szenario — alles bleibt, wörtlich wie in 2.1 gefordert. Ersetzt wird ausschließlich der **Speicher-Unterbau**: statt RocksDict die Postgres-`documents`-Tabelle (Jurgens „raw section" aus 2.3). Grund: RocksDB ist single-writer — genau das Mehr-Produzenten-Szenario (Daemon läuft, eine zweite Session fragt Status ab oder reiht nach) wäre damit hart gesperrt gewesen. Postgres macht es zum Kernfeature.

### 2.4 Nachgezogene Festlegungen aus der Plan-Review vom 05.08.2026 (wörtlich)

Die erste Fassung dieses Plans wurde durch drei Kaltleser-Sonden gemessen; die Befund-Inventur legte Jurgen mehrere vom Verfasser erfundene Strukturen vor. Seine Korrekturen, wörtlich, weil sie das Datenmodell und die Fehlerbehandlung festlegen:

> „ES KOMEMN KEINE CHUNKS IM POSTGRES AN DU KEK Der EXEKUTOR MANAGED DOCH ALLES UND SPEICHERT NUR FERTIG WEN JUCKEN CHUNKS SPÄTER"

> „das ist doch alles abstrahiert im postgres kommt der pfad zum original hin. was damit passiert hast nichts mit den rows zu tun"

> (zu gescheiterten Dokumenten:) „sie werden ja einfach von procrastinate dann zurückgesetzt das hat ja tooling dafür"

> (zum Crash mitten in einem großen, gechunkten Dokument — bewusste Entscheidung gegen jede Chunk-Persistenz:) „darüber machst du dir jetzt gedanken um einen edgecase in einem edgecase? wtf alter es wird dann ncohmal und das wars wenns nicht fertig ist ists gefailed"

> (zu übergroßen, nicht zerlegbaren Dateien:) „error fürs erste aber so dass man einen hinweis bekommt es nachträglich aufzurüsten und es auch als TODO reintun"

> (zum Kriterium für „leeres Ergebnis":) „das weiß ich ja nicht das muss ja rausgefudnen werden dann vorm bauen. es wird ja nicht ohne interaktion gebaut es ist ja consigment gegen die api"

> (zur Zustandsführung, 06.08. — ein Buch, nicht zwei:) „DAS IST POSTRGRES DER JOB LINKED AUF DAS DOKUMENT DAS ER PROCESSED UND WENN MAN DEN STATUS EINES DOKUMENTS HABEN WILL QUERIED MAN NACH DE LETZTEN JOB DER AUF DAS DOKUMENT LINKT"

**Ausdrücklich NICHT gemeint (bestätigte Fehllesarten der ersten Fassung — damit kein Leser sie wieder einbaut):**
- **Chunk-Zustände oder ein `chunk_plan` in der Datenbank** — nicht gemeint, weil der Executor alles Innere selbst managt und die Zeile nur den Endzustand speichert. Chunking ist ein Laufzeit-Detail des Daemons, kein Datenmodell.
- **Eine „Steuer-Zeile"/Kontroll-Tabelle für die Balance-Pause** — nicht gemeint; die Pause ist eine Warteschleife im Task selbst (5.1), ohne jeden zusätzlichen Zustand. (Eine Zwischenfassung wollte `RetryDecision(retry_in)` benutzen — verworfen: Jeder Retry erhöht den Versuchszähler (Verifikation Punkt 8), und ihn für Pausen sauber vom Budget echter Fehler zu trennen hieße, einen Ausnahme-Sonderpfad in die Retry-Strategie zu bauen. Der tragende Grund ist die Einfachheit der Warteschleife — kein Sonderpfad, kein Job-Karussell —, nicht der Zähler allein.)
- **Ein per-Chunk fortgeschriebenes `raw`-Feld** — nicht gemeint; geschrieben wird EINMAL, wenn das Dokument fertig ist. (Das löst nebenbei das Schreiblast-Problem, das die Messung an der fortgeschriebenen Variante fand.)
- **Ein `pii`-Flag im Client** — nicht gemeint; der Preflight meldet die Datacenter-Konfiguration des Endpoints schlicht mit.
- **Ein `processing`-Status in der Zeile und ein Zeilen-↔-Job-Abgleich-Wächter** — nicht gemeint, weil das ZWEI Buchführungen über denselben Zustand wären, die man synchron halten müsste. Der Job linkt auf sein Dokument (`queueing_lock` = sha256 + Task-Argument); „in Arbeit" wird aus dem jüngsten verlinkten Job GELESEN, nie in die Zeile gespiegelt. Eine Zwischenfassung hatte `status='processing'` plus einen `reconcile`-Abgleich — beides existierte nur, um die zweite Buchführung mit der ersten zu versöhnen, und beides ist mit ihr gestorben.

---

## 3. Zielbild: die Komponenten

```
                        EINE Maschine (Jurgens Mac)
┌─────────────────────────────────────────────────────────────────────┐
│                                                                     │
│  Producer (beliebig viele, sync, trivial):                          │
│  ┌──────────────────────────┐    ┌──────────────────────────────┐  │
│  │ OCRClient (Fassade)      │    │ irgendein späterer Aufrufer  │  │
│  │  .ocr_start(mega)        │    │  to_md("pfad.pdf")           │  │
│  │  .status(), .wait(sha)   │    │                              │  │
│  └───────────┬──────────────┘    └───────────┬──────────────────┘  │
│              │ INSERT documents-Zeile + defer│ (queueing_lock=sha)  │
│              ▼                               ▼                      │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │ Postgres 18.4 (Postgres.app, Port 5437, DB "mineru_ocr")    │   │
│  │  - procrastinate-Schema (Jobs, Events, Worker-Heartbeats)   │   │
│  │    → der Job LINKT auf sein Dokument (queueing_lock = sha); │   │
│  │      "in Arbeit" = jüngster verlinkter Job, nie gespiegelt  │   │
│  │  - documents-Tabelle: Pfad + ENDergebnis (raw + Terminal-   │   │
│  │    Status; zwischen Anlage und fertig wird sie nie berührt) │   │
│  │  - NOTIFY-Kanal "doc_done" (feuert bei JEDEM Terminal-Write)│   │
│  └───────────┬─────────────────────────────────────────────────┘   │
│              │ LISTEN/NOTIFY + Poll-Fallback                        │
│              ▼                                                      │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │ Daemon: procrastinate-Worker (EIN Prozess, async intern)    │   │
│  │  Task ocr_document(sha):                                    │   │
│  │    Datei lesen → intern chunken (pypdfium2, nur im RAM) →   │   │
│  │    je Chunk POST /run + poll /status (Antworten sammeln) →  │   │
│  │    wenn ALLES da: EINMAL schreiben — raw + status + NOTIFY  │   │
│  │  Periodisch (eigene Queue "maintenance"): retry_stalled —   │   │
│  │    das 4-Zeilen-Doku-Muster, sonst nichts                   │   │
│  └───────────┬─────────────────────────────────────────────────┘   │
└──────────────┼──────────────────────────────────────────────────────┘
               │ HTTPS (httpx, async — der Task ist async)
               ▼
   RunPod QB-Endpoint l79lebhinxaqjz  (skaliert selbst 0→N→0;
   MinerU-Worker, Antwort = tarball_b64 mit ALLEN Artefakten)
```

**Intention hinter dem Schnitt:** Der Daemon ist der EINZIGE Ort, der mit RunPod spricht und der EINZIGE Ort mit Nebenläufigkeit. Alles andere (Client-Fassade, Wrapper, künftiger DEVONthink-Write-back) liest und schreibt nur Postgres-Zeilen. Damit ist jede Komponente einzeln tot-startbar, und der Zustand liegt zu jedem Zeitpunkt vollständig an einem Ort, den man mit SQL ansehen kann. **Und (Festlegung 2.4): Alles zwischen „Job angenommen" und „Dokument fertig" ist Privatsache des Executors** — die Datenbank sieht davon nichts; sie speichert nur fertig.

---

## 4. Datenmodell

### 4.1 Die `documents`-Tabelle (die Wahrheit über jedes Dokument)

```sql
CREATE TABLE documents (
    sha256        text PRIMARY KEY,      -- roher Byte-Hash der Quelldatei = Identität
    source_path   text NOT NULL,         -- absoluter Pfad zum Original (oder Spool-Pfad bei Bytes-Input)
    source_name   text NOT NULL,
    size_bytes    bigint NOT NULL,
    mime          text NOT NULL,
    page_count    integer,               -- nur PDF; NULL sonst
    status        text NOT NULL DEFAULT 'new',
        -- 'new' | 'done' | 'empty' | 'failed' | 'input_error'
        -- KEIN 'processing': "in Arbeit" wird aus dem jüngsten verlinkten
        -- Procrastinate-Job GELESEN, nie hierher gespiegelt (2.4/4.2)
    raw           jsonb,                 -- DIE RAW SECTION: geschrieben nur beim Terminal-
        -- Schreiben eines Durchlaufs, nie chunk-weise; ein Failed-Retry (4.2)
        -- ueberschreibt sie bei SEINEM Terminal-Schreiben.
        -- Inhalt: die Liste der VOLLEN /status-Antworten aller Chunks
        -- (inkl. tarball_b64, debug-Block; bei failed: die Fehler-Envelopes).
        -- Nichts wird reduziert, nichts wird beim Schreiben entpackt.
    error         text,                  -- Kurzform bei failed/input_error
    endpoint_id   text NOT NULL,
    image_tag_at_submit text,            -- Provenienz: welches Worker-Image lief
    notes         jsonb NOT NULL DEFAULT '[]',  -- Ereignisliste (Hinweise, Aufrüst-TODOs)
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()  -- Pflege im Anwendungscode
);
```

**Intentionen:**
- **`sha256` als Primärschlüssel** setzt Jurgens Dedup-Vorgabe (2.1) auf DB-Ebene um: dieselbe Datei kann nie zweimal existieren, egal wie viele Producer sie einreihen. Roher Byte-Hash, ausdrücklich **kein** Metadata-Stripping — ein Normalisierungsfehler würde falsche Dedup-Treffer erzeugen („gilt als fertig, wurde nie geOCRt"), den unsichtbarsten aller Fehler; und Dedup ist im DEVONthink-Vorlauf ohnehin erledigt. Liegt dieselbe Datei unter zwei Pfaden, gewinnt der zuerst gesehene `source_path`; der zweite wird als note angehängt.
- **Die Zeile speichert nur fertig — jetzt ohne jede Ausnahme** (Festlegungen 2.4): Zwischen ihrer Anlage (`new`) und dem Terminal-Schreiben fasst NIEMAND sie an. Chunks, Job-IDs, Zwischenstände, „läuft gerade" — alles davon ist entweder Laufzeit-Inneres des Executors oder steht ohnehin in Procrastinates eigenen Tabellen. **Der Dokument-Status ist eine abgeleitete Größe:** Zeile terminal → das ist der Status; sonst jüngster Job mit `queueing_lock = sha256` (`ORDER BY id DESC LIMIT 1` — „jüngster", weil bei `delete_jobs="never"` über die Zeit mehrere Jobs je Dokument existieren) → wartend/laufend/Versuch n samt Procrastinate-Details; gar kein Job → registriert, nie eingereiht. Konsequenz: `raw` wird nur beim Terminal-Schreiben geschrieben — nie chunk-weise fortgeschrieben, überschrieben nur, wenn ein gescheitertes Dokument erneut verarbeitet wird (4.2) —, und es gibt keine zwei Buchführungen, die je auseinanderlaufen könnten.
- **`raw` = Liste der VOLLEN Antworten** ist Jurgens „raw section" (2.3) und die Abruf-Regel des Betriebshandbuchs (14a): alles geholt, alles persistiert; jede Formatierung passiert später on the fly in den Gettern (2.1). `tarball_b64` bleibt als Base64-String darin — KEINE Zerlegung beim Schreiben (jede Zerlegung wäre eine Auswertungs-Entscheidung zum falschen Zeitpunkt). Mengengerüst: ~0,5–1 MB je Seite, bei 30k Dokumenten zweistellige GB — TOAST-Speicherung, Plattenplatz vor dem Bulk prüfen (12.5). Die Juli-Empfehlung „Pfad+Hash statt Bytes-in-DB" betraf die INPUT-Bytes (bleiben als Pfad); die OUTPUTS gehören in die DB, weil sie die einzige dauerhafte Kopie sind (RunPod hält Ergebnisse 30 Minuten).

  > **ZURÜCKGENOMMEN am 07.08.2026 durch Jurgen — `raw` ist EIN zusammengesetztes Objekt, keine Liste von Antworten.** Der Absatz darüber bleibt nach Pflegeregel 2 stehen (*Widerlegtes wird als widerlegt gekennzeichnet, nicht überschrieben*); was ihn ersetzt, ist Jurgens Festlegung im Wortlaut:
  >
  > > „DIE CHUNKS SIND ABSTRAHIERT ES LANDET IN POSTGRES NUR DAS FERTIG ZUSAMMENGESETZTE UND DADURCH GEHT KEINE INFORMATION VERLOREN WEIL INPUT IST DAS GANZE DOKUMENT UND OUTPUT IST DAS GANZE DOKUMENT IN OCR, das es dazwischen chunks gab in der arbeit is irrelevant"
  >
  > **Was das konkret ändert:** Der Executor packt jede Chunk-Antwort mit `assembly.unpack_response` aus und setzt sie mit `assembly.assemble` zusammen, **bevor** geschrieben wird. In `documents.raw` steht danach ein Objekt auf Dokumentebene (`markdown`, `content_list` mit den Seitenzahlen des ORIGINALS, `images`, `middle`, `layout_pdfs`, `envelopes`, `extras`) — keine Chunk-Struktur, keine Liste, kein `tarball_b64` mehr.
  >
  > **Warum vor und nicht nach dem Schreiben** (der Punkt, an dem Abschnitt 6 hängt): Der Seitenversatz ist nur im Executor bekannt. Nur dort ist zur Laufzeit greifbar, welcher `Chunk` welchen Seitenbereich trug; ein Getter, der später zusammensetzt, hätte diese Zuordnung nicht mehr — sie stünde nirgends, weil die Zeile ja gerade keine Chunk-Struktur führen soll.
  >
  > **Die Abruf-Regel bleibt dabei unangetastet, und das ist der Kern der Begründung:** Zusammensetzen ist kein Filtern. Alles, was die Antwort trug, steht danach in einem Feld — auch das Unbenannte, dafür gibt es `ChunkArtifacts.extras`. Am 08./09.08.2026 an echten Antworten geprüft: Die im Archiv liegende zweite `content_list`-Fassung landet vollständig in `extras`, eine unauspackbare bezahlte Antwort unter `unverwertbare_antworten`, und eine gescheiterte Zusammensetzung erzeugt eine Rohfassung aller bezahlten Teilstücke statt eines Verlusts.
  >
  > **Mitbetroffen und ebenfalls überholt:** die Getter-Namen aus Abschnitt 7. `assembled_text` hieß im Code zunächst `rendered_text`; `artifacts_per_chunk` und `raw_responses` gibt es nicht mehr — beide setzten die Chunk-Struktur in der Zeile voraus. Am 09.08.2026 ist die Getter-Schicht dann noch einmal und weitergehend zurückgenommen worden; der Kasten in Abschnitt 7 sagt, was an ihre Stelle getreten ist.
  >
  > **Nachprüf-Ort:** `mineru_ocr/assembly.py` (Modul-Docstring und `assemble`), `mineru_ocr/db.py` am Feld `DocumentRow.raw`, sowie `tests/test_assembly.py::test_seitenversatz_gegen_ECHTE_mehrteilige_antworten`.
- **`empty` und `input_error` als eigene Terminal-Zustände:** Ein leeres OCR-Ergebnis ist gültig (leere Seite) — als Erfolg getarnt unsichtbarer Verlust, als Fehler getarnt endlose Wiedervorlage mit Doppelkosten (Betriebshandbuch 11.3-12; real: beide Ad-hoc-Skripte machten je einen dieser Fehler). **Das Erkennungs-Kriterium für „leer" wird beim Bauen am echten API-Verhalten festgestellt, nicht vorab erfunden** (Festlegung 2.4: „das muss ja rausgefudnen werden dann vorm bauen … es ist ja consigment gegen die api"). `input_error` (nicht zerlegbare Datei über dem Payload-Cap, kaputtes PDF, Datei zwischen Einreihen und Verarbeitung verändert) ist terminal OHNE RunPod-Kontakt — **nicht unterstützte Typen erreichen die DB dagegen nie:** Die Fassade wirft `UnsupportedTypeError` vor jedem INSERT (7), und `from_dir` überspringt sie zählend; bei aufrüstbaren Fällen trägt `notes` den Hinweis (Festlegung 2.4: „error fürs erste aber so dass man einen hinweis bekommt es nachträglich aufzurüsten und es auch als TODO reintun"). **Den Aufgaben-Eintrag (Bead) legt dabei NICHT der Daemon an** (kein Subprozess-Seiteneffekt aus einem Task heraus) — `report()`/`status()` der Fassade weisen offene Aufrüst-Fälle aus, und das Bead legt der Mensch bzw. die Session an, die den Report liest.
- **`image_tag_at_submit`:** Provenienz. Wenn später eine Qualitätsfrage auftaucht („war das noch das v1.8.0-Image?"), steht die Antwort an jedem Dokument. Einmal je Task-Lauf per GraphQL geholt.

### 4.2 Zustandsmaschine (nur Dokument-Ebene — mehr gibt es in der DB nicht)

```
 Zeile:   new ──▶ done         (Ergebnis vollständig da, mind. ein Chunk mit Inhalt)
            │ └─▶ empty        (alles leer — "am Bild prüfen")
            │ └─▶ failed       (terminal: nicht-retrybar ODER Versuche erschöpft)
            └──▶ input_error   (vor jedem RunPod-Kontakt aussortiert)

 "In Arbeit" ist KEIN Zeilen-Zustand: es ist der Zustand des jüngsten
 verlinkten Procrastinate-Jobs (todo/doing/attempts) — gelesen, nie gespiegelt.
```

**Fehler-Semantik (Festlegung 2.4, wörtlich: „es wird dann ncohmal und das wars wenns nicht fertig ist ists gefailed"):** Scheitert oder crasht der Task mitten in einem Dokument, wiederholt Procrastinate den Task nach seiner Retry-Strategie — **das Dokument wird dann von vorn verarbeitet**, einschließlich erneuter Bezahlung bereits gelaufener Chunks. Das ist eine bewusste Entscheidung gegen jede Chunk-Persistenz (Edge-Case im Edge-Case; die Einfachheit des Datenmodells ist mehr wert). Ist der Fehler terminal (nicht-retrybar oder Versuche erschöpft), ist das Dokument `failed` — die Entscheidung „retrybar oder terminal" lebt in GENAU EINER Funktion, die sowohl die `OCRRetryStrategy` als auch der Exception-Handler des Tasks benutzen; der Task schreibt `status='failed'` + `error` + `raw` (bisherige Antworten samt Fehler-Envelope), BEVOR er terminal wirft (deckt auch den nicht-retrybaren Fehler im ersten Versuch ab). **Einen Abgleich-Wächter gibt es nicht** (2.4: eine Buchführung): Endet ein Job terminal, ohne dass der Task die Zeile schreiben konnte (harter Kill im Schreibfenster, Shutdown-Abort), bleibt die Zeile schlicht `new` — sichtbar in `status()`/`report()` als „registriert, jüngster Job terminal ohne Ergebnis", und beim nächsten `ocr_start`/Durchlaufenlassen wird sie einfach wieder eingereiht („es wird dann ncohmal"). **Wiederanlauf gescheiterter Dokumente ist Procrastinate-Territorium:** Failed-Jobs lassen sich mit Bordmitteln erneut vorlegen — `job_manager.retry_job_by_id` (am Objekt geprüft: akzeptiert nur Jobs in `doing|failed`, setzt zurück auf `todo` mit `attempts+1`, Event-Historie bleibt; `aborted` nimmt es NICHT an — Verifikation Punkt 19) und obendrauf das fertige Admin-Werkzeug `procrastinate shell` mit den Kommandos `retry`/`cancel`/`list_jobs`; die Client-Fassade reicht das als dünnen Helfer durch (7). Der Terminal-Guard (5.1) lässt `failed` deshalb ausdrücklich durch: Der wieder vorgelegte Job verarbeitet das Dokument wirklich neu und überschreibt `raw` bei seinem Terminal-Schreiben. Für ABGEBROCHENE Jobs (`aborted`, etwa beim Shutdown) gibt es dagegen ausdrücklich KEIN Retry-Bordmittel; ihr Weg ist das erneute Einreihen über `ocr_start` (5.3). Die Adopt-statt-Resubmit-Regel des Juli-Registers wirkt auf Dokument-Ebene weiter (sha-Dedup + `queueing_lock`: dasselbe Dokument ist nie doppelt in der Warteschlange); innerhalb eines Dokuments gilt sie per Festlegung 2.4 bewusst nicht.

---

## 5. Der Daemon

### 5.1 Procrastinate-App und -Task

```python
# mineru_ocr/daemon.py  (Skizze — Signaturen verbindlich, Körper indikativ)
app = procrastinate.App(connector=procrastinate.PsycopgConnector(conninfo=DSN))

@app.task(queue="ocr", retry=OCRRetryStrategy(), pass_context=True)
async def ocr_document(context, sha256: str) -> None:
    # 0. TERMINAL-GUARD: Zeile bereits in done|empty|input_error ->
    #    sofort return, nichts anfassen. Das ist die Idempotenz, die
    #    Procrastinate voraussetzt — Tasks KOENNEN mehrfach laufen (Retries,
    #    stalled-Wiederaufnahme; Verifikation Punkt 4: belegt ueber die
    #    Mechanik, nicht als Doku-Zitat). Ohne den Guard wuerde ein Re-Run
    #    nach dem finalen Schreiben (Crash im Fenster zwischen Zeilen-Write
    #    und Job-Verbuchung) das fertige Dokument komplett neu bezahlen.
    #    'failed' gehoert AUSDRUECKLICH NICHT in den Guard: Ein per
    #    retry_job_by_id wieder vorgelegter Job (4.2/7) muss das Dokument
    #    wirklich neu verarbeiten — stuende 'failed' im Guard, waere jeder
    #    manuelle Retry ein Noop, der als Erfolg verbucht wuerde, und ein
    #    gescheitertes Dokument koennte NIE wieder laufen.
    # 1. documents-Zeile laden — sie wird bis zum Terminal-Schreiben NICHT
    #    angefasst (kein Zwischen-Update; "läuft" sieht man am Job selbst, 4.2)
    # 2. Datei von source_path lesen; Hash gegenprüfen
    #    (Datei verändert? -> input_error + note; bezahlte Ergebnisse gibt es
    #    an dieser Zeile nicht, weil nur fertig geschrieben wird)
    # 3. INTERN (nur im RAM): chunken (pypdfium2), je Chunk sequenziell:
    #    POST /run -> poll GET /status (~10 s Intervall) bis terminal;
    #    volle Antwort in eine lokale Liste
    #    Der kanonische Aufruf (Job-Contract, Betriebshandbuch 5.1/5.2):
    #      POST https://api.runpod.ai/v2/l79lebhinxaqjz/run
    #      {"input": {"file_b64": "<Base64 des Chunks>",
    #                 "backend": "vlm-auto-engine",
    #                 "transport": "tarball_b64"}}
    #      -> {"id": "<job_id>", "status": "IN_QUEUE"}
    #    KEIN formats-Feld (bei tarball_b64 ohnehin No-op — es kommt immer alles);
    #    kein JSON-null fuer optionale Felder; nie /runsync.
    # 4. Wenn ALLE Chunks erfolgreich terminal: EIN Schreibvorgang —
    #    raw=Antwortliste, status=done|empty, Metriken/Hinweise in notes;
    #    NOTIFY doc_done,'<sha>' — das NOTIFY feuert bei JEDEM Terminal-
    #    Schreiben, auch bei failed/input_error, damit wait() immer aufwacht.
    #    Fehler laufen IMMER über den Exception-Pfad: transient -> werfen
    #    (Procrastinate retryt, Dokument von vorn); terminal -> raw=bisherige
    #    Antworten+Fehler-Envelope schreiben, status='failed', NOTIFY, DANN
    #    werfen (4.2) — so tragen Job-Endzustand und Zeile dieselbe Wahrheit.
```

**Intentionen:**
- **Task-Granularität = EIN DOKUMENT** (nicht ein Chunk). Das Dokument ist die Bedeutungseinheit des Aufrufers; der Chunk ist ein Engine-Detail, das laut Vorgabe (2.1) „für den User nicht bemerkbar" sein soll — so unbemerkbar, dass es nicht einmal die Datenbank sieht (Festlegung 2.4).
- **Chunks sequenziell innerhalb des Tasks; Parallelität über Worker-`concurrency`.** Zwei Regler wären einer zu viel: `concurrency=N` (Dokumente gleichzeitig „in Betreuung") IST das In-Flight-Fenster, denn die eigentliche Parallelität liegt serverseitig bei RunPod (`workersMax × MINERU_MAX_CONCURRENCY`); mehr Client-Gleichzeitigkeit macht keinen Job schneller. Startwert `concurrency=24`, so hergeleitet: 10 RunPod-Worker schaffen zusammen ~2 Seiten/s; ein Poll-Zyklus von 10 s heißt bis zu ~20 fertige Seiten zwischen zwei Polls — 24 gleichzeitig betreute Dokumente halten die Queue gefüllt, ohne hunderte offene Ergebnisse nahe der 30-Minuten-Retention zu parken. Nach dem ersten echten Batch nachmessen (12.1).
- **Der Task ist async** (I/O-Warten auf RunPod), damit `concurrency` greift — der eine Ort, an dem async lebt (2.2/2.3).
- **`queueing_lock=sha256` UND `lock=sha256` beim Defer — beide, aus verifiziertem Grund:** `queueing_lock` verhindert nur ein zweites WARTENDES Duplikat und ist schon frei, sobald der Job läuft (Verifikation Punkt 5: „allows a single job in todo status … allows multiple jobs to be in doing status"). Ein während der Ausführung nachgeschobenes Duplikat würde also angenommen und parallel laufen — doppelte Bezahlung. Der zusätzliche `lock` (dokumentierte Kombination genau für diesen Fall) serialisiert Jobs gleichen Locks: Das Duplikat startet erst NACH dem laufenden Job und endet dann sofort am Terminal-Guard (Noop). Der `lock` wird bei JEDEM Job-Ende frei — „the second task cannot run before the first one has ended (successfully or not)" (Verifikation Punkt 18) —, ein gescheiterter Job kann die Kette also nie dauerhaft sperren. Endet der laufende Job terminal-`failed`, ist das nachgereihte Duplikat KEIN Noop, sondern verarbeitet das Dokument neu — gewollt: Das ist genau der eine weitere Versuch („es wird dann ncohmal"), keine Doppelzahlung eines fertigen Ergebnisses. `AlreadyEnqueued` wird weiter gefangen = „läuft schon" (das Ordner-Szenario aus 2.1). **Konsequenz daraus: `ocr_start` deferrt EINZELN, nie per `batch_defer`** — batch_defer mit queueing_lock-Tasks ist ausdrücklich untersagt, und ein Konflikt rollt den GESAMTEN Batch zurück (Verifikation Punkt 9); 30k einzelne Deferrals sind billig.
- **Retry-Politik (`OCRRetryStrategy`, Procrastinate-`BaseRetryStrategy`):** Transportfehler/5xx/`TIMED_OUT` → bis 3 Versuche exponentiell (Basis ~30 s, Faktor 4: ~30 s/2 min/8 min); Schema-/Validierungsfehler → kein Retry, terminal (der Task schreibt `failed` vor dem Wurf — 4.2). Die Retrybar-oder-terminal-Entscheidung ist eine gemeinsame Funktion von Strategie und Task-Handler (4.2), damit beide nie auseinanderlaufen; „kein Retry" signalisiert die Strategie per `return None` aus `get_retry_decision` — NICHT per `should_retry=False`, das nur in einem Howto-Beispiel existiert und in der `RetryDecision`-Referenz fehlt (Verifikation Punkt 8). **`attempts` zählt jede Wiedervorlage, nicht nur Strategie-Retries:** Die SQL-Funktion `procrastinate_retry_job_v2` erhöht `attempts` bei jedem Zurücksetzen auf `todo`, und sowohl der stalled-Wächter (`retry_job`) als auch der manuelle Retry (`retry_job_by_id`) laufen durch genau diese Funktion (Verifikation Punkt 19). Ein manueller Retry eines erschöpften Jobs ist damit genau EIN weiterer Versuch, keine neue 3er-Runde, und eine stalled-Wiederaufnahme zehrt am selben Budget — das steht als Kommentar an der Strategie, damit es niemand für einen Bug hält. **Konto unter der Pause-Schwelle oder HTTP 402 → der Task WARTET selbst** (async-Schleife, Kontostand-Check alle 15 min) statt zu werfen — so bleibt der Procrastinate-Versuchszähler ein ehrliches Maß echter Fehlversuche (eine `RetryDecision`-Pause würde ihn mitzählen und das 3-Versuche-Budget verfälschen); ein Concurrency-Slot ist im async-Worker billig, und es entsteht weiterhin kein zusätzlicher Zustand (Festlegung 2.4: keine Kontroll-Zeile). Zur Einordnung der Geld-Schwellen: Preflight verlangt ≥ $1 (Arbeitsbeginn nur mit Puffer), die Lauf-Pause greift unter $0,50 (früher als der historische $0,15-Abbruch der Ad-hoc-Skripte, weil Pausieren nichts kostet und der Puffer die laufenden Chunks noch zu Ende bezahlt). RunPods eigenes `/retry` bleibt tabu (ein einziger Retry-Eigentümer, Register 7).

### 5.2 Periodischer Wächter — NUR das Procrastinate-Doku-Muster, sonst nichts

```python
@app.periodic(cron="*/10 * * * *")
@app.task(queue="maintenance", queueing_lock="retry_stalled", pass_context=True)
async def retry_stalled(context, timestamp):
    # Wörtlich das Doku-Muster: get_stalled_jobs() -> retry_job(). Fängt den
    # Fall "Daemon hart gestorben, Job hing in 'doing'" — das Dokument wird
    # dann schlicht von vorn verarbeitet (Festlegung 2.4).
```

**Intention:** Das ist Procrastinates eigene, dokumentierte Antwort auf „Daemon gekillt, was nun?" — vier Zeilen Bedienung, kein Eigenbau. **Einen Zeilen-Abgleich gibt es nicht mehr** (Festlegung 2.4, ein Buch): Da die Zeile zwischen Anlage und Terminal-Schreiben nie angefasst wird, kann sie mit nichts auseinanderlaufen; der seltene Fall „Job terminal, Zeile blieb `new`" ist im abgeleiteten Status sichtbar und heilt durch erneutes Durchlaufenlassen. Die Balance-Behandlung braucht ebenfalls keinen Wächter — sie ist eine Warteschleife im Task selbst (5.1); der Task prüft den Kontostand zusätzlich einmal vor dem ersten Chunk-Submit (ein GraphQL-Call je Dokument, vernachlässigbar).

**Warum eigene Queue, und wie sie eigene Slots bekommt:** Periodische Tasks sind gewöhnliche Jobs und teilen den `concurrency`-Pool des Workers (Verifikation Punkt 2 — die Doku empfiehlt für genau dieses Problem Queue-Trennung). Hingen alle 24 OCR-Slots in Balance-Warteschleifen, würde ein `retry_stalled` auf derselben Queue nie drankommen. Deshalb Queue `maintenance` — und der Daemon startet dafür im selben Prozess eine **zweite Worker-Koroutine** (`run_worker_async(queues=["maintenance"], concurrency=1)`) neben der OCR-Worker-Koroutine. **Ehrlichkeits-Vermerk: Diese Zwei-Koroutinen-Konstruktion ist in der Procrastinate-Doku weder erlaubt noch verboten — dokumentiert ist „mehrere Worker = mehrere Prozesse"** (Verifikation Punkt 1, NICHT BELEGT). Sie wird deshalb im Integrationstest ausdrücklich geprüft (10.2 h); trägt sie nicht, ist der dokumentierte Fallback ein zweiter Prozess (`mineru-ocr daemon --maintenance`), der nur die maintenance-Queue bedient. **Beide Koroutinen laufen mit `install_signal_handlers=False`** — zwei Worker dürfen nicht beide Signal-Handler installieren; der Daemon-Hauptprozess fängt SIGTERM/SIGINT selbst und cancelt beide Worker-Tasks (das ist exakt das dokumentierte „Worker in größerer Anwendung"-Muster der Doku).

### 5.3 Start, Stop, Betrieb

- **Start (v1, bewusst manuell):** `python -m mineru_ocr.daemon` — startet im selben Prozess zwei Worker-Koroutinen: OCR (`queues=["ocr"], concurrency=24`) und Wartung (`queues=["maintenance"], concurrency=1`), beide mit `shutdown_graceful_timeout=30, delete_jobs="never"` (Zwei-Koroutinen-Vorbehalt und Fallback: 5.2). Ein Wrapper-Kommando `mineru-ocr daemon` kommt mit; **keine launchd-Automatik im ersten Wurf** — Intention: erst beobachten, wie sich der Daemon im echten Bulk verhält, bevor er zum Autostart-Hintergrundprozess wird, den man vergisst (die Doku-Lehre über stehende Dinge, die keiner mehr ansieht).
- **Stop:** Ctrl-C/SIGTERM → der Hauptprozess cancelt beide Worker-Koroutinen (graceful; laufende Dokumente werden zu Ende betreut oder nach `shutdown_graceful_timeout` abgebrochen). Ein abgebrochener Job endet bei Procrastinate als `aborted` — terminal, KEIN automatischer Retry; die Zeile bleibt dann `new`, der abgeleitete Status zeigt „jüngster Job aborted", und das Dokument läuft beim nächsten `ocr_start`/Durchlaufenlassen schlicht von vorn (Festlegung 2.4 — kein Wächter nötig).
- **`delete_jobs="never"` + `procrastinate_events`-Tabelle bleiben:** volle Nachlesbarkeit („tidiness = full legibility", Jurgens Teardown-Festlegung im Register). Aufräumen alter Job-Zeilen ist ein dokumentiertes Später (`remove_old_jobs`), kein Default.

---

## 6. Chunking und Merge

**Wann wird gechunkt** (nur PDF; Bilder sind einseitig, Office-Formate nicht seitenweise zerlegbar) — alles davon ist **Laufzeit-Logik im Executor, nichts davon Datenmodell** (Festlegung 2.4):
- Datei > **7,0 MB** roh (Intention: 10-MB-Gateway-Cap auf `/run`, Base64 bläht ×1,33, Rest ist Envelope-Puffer), **oder** `page_count` > **20** (Intention: ~20-MB-Antwort-Cap; gemessene Seiten-Antworten 23–152 KB inline, Tarball trägt zusätzlich Layout-/Span-PDFs — 20 Seiten × konservativ 0,75 MB ≈ 15 MB < 20 MB. Die Schwelle ist eine Konstante mit Begründungskommentar und wird beim ersten echten Riss nachgemessen, nicht geraten erhöht).
- **Schnitt-Regel:** gierige 20-Seiten-Blöcke in Dokumentreihenfolge; ist ein geschnittener Block > 7,0 MB, wird er rekursiv halbiert, bis er passt oder nur noch eine Seite ist.
- Geschnitten wird **lokal mit pypdfium2 direkt** (`PdfDocument.new()` + `import_pages`) — derselben Bibliothek, mit der MinerU serverseitig schneidet. Gemessen am 17-MB-Fixture-Bescheid: Aufblähfaktor **1,00×** (das pypdf-Verbot des Betriebshandbuchs ist pypdf-spezifisch; pypdf bleibt trotzdem verboten). **Ausdrücklich NICHT MinerUs eigene Wrapper-Funktion**, denn deren Fallback überspringt kaputte Seiten **still** — stattdessen: nach dem Schnitt Summe der Chunk-Seitenzahlen gegen `page_count` prüfen, bei Differenz `input_error` (fail-loud).
- Einzelseite immer noch > 7 MB → `input_error` **mit Aufrüst-Hinweis in `notes`** — Festlegung 2.4: „error fürs erste aber so dass man einen hinweis bekommt es nachträglich aufzurüsten und es auch als TODO reintun". Der Aufgaben-Eintrag (Bead) entsteht über den Report-Weg (4.1: `report()`/`status()` weisen offene Aufrüst-Fälle aus, der Mensch legt das Bead an — der Daemon selbst legt nie Beads an). Kein automatischer Rendering-Fallback im ersten Wurf (der Fall kam im gesamten Fixture-Set nicht vor: größte Einzelseite 1,27 MB).
- **Nicht-PDF über dem Cap** (etwa eine 12-MB-DOCX oder ein 9-MB-TIFF — nicht seitenweise zerlegbar): gleiche Behandlung, `input_error` + Hinweis in `notes`; Bead über den Report-Weg; nicht durchreichen-und-hoffen.
- **ZIP, das kein Office-Dokument ist** (gleiches Magic-Byte `PK\x03\x04` wie DOCX/PPTX/XLSX): wird rejected — die Typprüfung schaut dafür in die ZIP-Struktur (`word/`, `xl/`, `ppt/`).

**Merge — die Regeln gelten unverändert, der ORT hat sich geändert.**

> **ZURÜCKGENOMMEN am 07.08.2026:** Die Überschrift lautete „ausschließlich in den Gettern der Fassade, nie beim Schreiben". Sie steht nach Pflegeregel 2 unverändert unten, ist aber überholt: Zusammengesetzt wird jetzt **im Executor, vor dem Schreiben** (volle Begründung samt Jurgens Wortlaut an `raw` in Abschnitt 4.1). Die vier Merge-Regeln selbst sind wörtlich so umgesetzt — es ist derselbe Vorgang an einer anderen Stelle.
>
> **Die Vollständigkeits-Pflicht ist damit gegenstandslos geworden, nicht gestrichen:** Sie sollte verhindern, dass ein Getter ein Ergebnis aus unvollständigen Chunks zusammensetzt. Wenn nur das fertig Zusammengesetzte in der Zeile steht, kann dieser Zustand nicht mehr entstehen — eine Zeile ist entweder endgültig, dann ist das Ergebnis ganz, oder sie ist es nicht, dann gibt es keins. `IncompleteResultError` gibt es weiterhin und wird geworfen, wenn ein Getter auf eine Zeile ohne Ergebnis trifft.
>
> **Eine der vier Regeln ist am Material nachgeschärft worden:** Die Bild-Union verließ sich darauf, dass Dateinamen Inhalts-Hashes sind. Das ist nur für den Inline-Transport geprüft; der Betrieb fährt Tarball und benutzt dort den nackten Archiv-Dateinamen. `assemble` prüft die Annahme deshalb, statt sie zu glauben — bei Kollision bleiben beide Bilder erhalten und der Fall steht als eigener Eintrag im Ergebnis.

**Merge (ausschließlich in den Gettern der Fassade, nie beim Schreiben):**
- `markdown`: Konkatenation in Chunk-Reihenfolge.
- `content_list`: Konkatenation mit **`page_idx` + `start_page` des Chunks** — MinerU schneidet vor dem Parsen, `page_idx` startet je Chunk bei 0 (am Quelltext verifiziert, 05.08.2026).
- `images`: Dict-Union — Dateinamen sind Inhalts-Hashes (kollisionsfrei; empirisch am 05.08.2026).
- `middle`, Layout-/Span-PDFs: **unfusioniert als Liste je Chunk** — `middle` ist MinerU-intern versioniert; ein erfundener Merge wäre schlechter als die ehrliche Rohform, und der Write-back braucht ohnehin die Blöcke, nicht ein fusioniertes middle.
- **Vollständigkeits-Pflicht:** Getter, die Gesamtergebnisse liefern, werfen `IncompleteResultError`, solange nicht alle Chunks terminal-erfolgreich sind — still einen Teil zu liefern wäre exakt der „äußerlich vollständig"-Fehler aus Betriebshandbuch 11.2a-2.

**Transport: `tarball_b64`, fest.** Intention (Betriebshandbuch 5a.1/14a): Bei diesem Transport ist `formats` ein No-op — es kommt IMMER alles, die Abruf-Regel ist Mechanik statt Disziplin (an Disziplin ist sie am 12.07. gescheitert). Zusätzlich trägt der Tarball die Layout-/Span-PDFs, die Inline gar nicht liefert. `s3` wird nicht implementiert (neuer externer PII-Dienst = Jurgens Entscheidung, nicht Modul-Default).

> **TEILWEISE ZURÜCKGENOMMEN am 09.08.2026:** Der Tarball trägt das **Layout**-PDF und die Bilder, die Inline nicht liefert — insoweit stimmt der Satz und die Entscheidung für Tarball bleibt richtig. **Ein Span-PDF gibt es nicht**, in keinem Transport: `mineru/cli/common.py` setzt `f_draw_span_bbox = False` in jedem VLM-Pfad bedingungslos, und die offizielle Doku sagt zu `span.pdf` „Only applicable to pipeline backend". Wir fahren `vlm-auto-engine`. Die volle Anatomie des Fehlschlusses — ein Default in einer Funktions-Signatur wurde für eine Zusicherung über das Verhalten gehalten — steht im Betriebshandbuch, Abschnitt 14a.

---

## 7. Die Client-Fassade (Jurgens Convenience-Schicht, wörtlich aus 2.1)

Alle drei Klassen sind **reine Postgres-Leser/-Schreiber** — sie berühren RunPod nie (einzige Ausnahme: `OCRClient.credit()`/`workers()`/`preflight()`, die RunPod nur LESEN).

> **ZURÜCKGENOMMEN — die Getter-Schicht des `OCRWrapper` (Jurgen, 09.08.2026).** Der Codeblock unten steht unverändert, weil er zeigt, wogegen entschieden wurde; alles außerhalb der Getter-Schicht ist von diesem Kasten nicht berührt. Was an ihre Stelle getreten ist:
>
> - **`markdown` und `content_list` gibt es nicht mehr.** An ihrer Stelle steht ein generischer Zugang: `huelle["markdown"]` **ist** `huelle.raw["markdown"]`, `huelle["content_list"]` ist `huelle.raw["content_list"]`, und ebenso für jedes weitere Element, das `assembly.assemble` erzeugt (`images`, `middle`, `layout_pdfs`, `envelopes`, `extras`) — auch für die, die später dazukommen, ohne dass am Wrapper etwas nachzutragen wäre. Dazu `__contains__`, damit `if "layout_pdfs" in huelle` möglich ist, ohne den `KeyError` aus `__getitem__` als Steuerfluss zu benutzen. **Die Begründung ist, dass eine handverlesene Auswahl aus dem, was ohnehin vollständig da ist, eine Aussage darüber ist, was wichtig sei — und diese Aussage hat der Wrapper nicht zu treffen.** Jurgens Wortlaut: „wrapper.markdown doesnt exists. why would it it the wrapper must surface wrapper['markdown']=wrapper.raw['markdown'] mit wildcard wenn das geht".
> - **`artifacts_per_chunk` und `raw_responses` entfallen** — beide setzten die Chunk-Struktur in der Zeile voraus, die es seit dem Kasten in 4.1 nicht mehr gibt.
> - **`assembled_text(furniture=…, foreign_filter=…)` heißt `rendered_markdown` und ist eine Eigenschaft ohne jeden Schalter.** Zwischenstand war `rendered_text` mit denselben zwei Parametern. Beide sind weg, weil beide dieselbe Frage stellten — „was gehört zum Text?" — und sie mit einer Voreinstellung beantworteten; das ist eine redaktionelle Entscheidung, und sie gehört dem, der das Ergebnis benutzt, nicht dem, der es aufbereitet. Wer weniger will als alles, baut es aus `huelle["content_list"]` (jeder Block einzeln, samt `type` und `page_idx`) und `huelle["markdown"]` — an der Stelle, an der jemand weiß, wofür. Jurgens Festlegung: „nur wrapper.rendered_markdown und das ist immer das gesamte dokument alles was auf dem dokument ist in einer markdown form dargestellt raw markdown ist natürlich eine große komponente".
> - **`images` bleibt eine eigene Eigenschaft**, und zwar als einzige, weil sie mehr tut als durchreichen: Sie wandelt aus Base64 zurück nach Bytes, was ein generischer Zugang nicht kann. `huelle["images"]` liefert die gespeicherten Zeichenketten.
>
> **Nachprüf-Ort:** `mineru_ocr/wrapper.py` (`__getitem__`, `__contains__`, `rendered_markdown`, `images`), `mineru_ocr/assembly.py::rendered_markdown`, `tests/test_wrapper.py`, `tests/test_assembly.py`.

```python
class OCRWrapper:
    @classmethod from_path(path, db) / from_bytes(data, name, db)
        # from_bytes: Spool-Datei <spool_dir>/{sha256}{ext}, dann wie from_path
        # Init: streamen->sha256; MIME per Magic Bytes (PDF/PNG/JPEG/GIF/BMP/TIFF/
        # WebP/DOCX/PPTX/XLSX, sonst UnsupportedTypeError SOFORT); PDF: page_count;
        # documents-Zeile adoptieren oder anlegen (INSERT ... ON CONFLICT DO NOTHING)
    # Attribute (aus der Zeile): sha256, source_path, source_name, size_bytes,
    #   mime, page_count, status, notes, endpoint_id, image_tag_at_submit
    # Properties (on the fly aus raw, mit In-Memory-Cache je Objekt):
    #   success: bool          # status in ('done','empty') — "hat das OCR geklappt"
    #   markdown, content_list, images, artifacts_per_chunk, raw_responses
    #   bytes                  # lazy vom source_path
    # Methoden: refresh() (Zeile neu laden + Cache invalidieren),
    #   assembled_text(furniture=True, foreign_filter=None),  # Möblierungsblöcke
    #   to_dict(), __repr__

class OCRMegaWrapper:
    @classmethod from_paths(paths, db) / from_dir(dir, db, glob="**/*")
        # from_dir überspringt nicht unterstützte Dateien (z. B. .DS_Store) und
        # ZÄHLT sie im Rückgabe-Report, statt an der ersten zu sterben —
        # from_path wirft bei Direktaufruf weiterhin sofort (2.1: "rejected")
    paths = from_paths                       # Alias aus dem Braindump
    add(x) -> OCRWrapper                     # dedupliziert über sha256
    extend(paths) / refresh_dir(dir, glob)   # Ordner-Szenario: nur Neues kommt dazu
    status_counts() -> dict                  # programmatisch
    stats() -> dict                          # counts + bytes + seiten + fehler + empties
    report(verbose=False) -> str             # per Text ausgebbar
    pending / done / failed                  # Sichten (Property)
    by_state(state) -> list[OCRWrapper]
    __iter__, __len__, __getitem__, __repr__

class OCRClient:
    def __init__(db_dsn=None, endpoint_id="l79lebhinxaqjz", api_key=None, *,
                 preflight=True, strict=True, min_balance=1.0,
                 backend="vlm-auto-engine")   # wird beim Defer als Task-Argument
                                              # mitgegeben (11: kein pipeline-Default,
                                              # aber durchgereicht)
    # Konfigurations-Quellen (Defaults): db_dsn None -> "host=localhost port=5437
    #   dbname=mineru_ocr user=jurgen"; api_key None -> ~/.runpod/config.toml
    #   Feld "apikey" (der im Projekt bewaehrte Ort); spool_dir = ~/.mineru_ocr/spool/
    #   (dauerhaft, wird NIE automatisch geloescht — source_path referenziert ihn;
    #   derselbe Default gilt auch bei OCRWrapper.from_bytes OHNE Client).
    # Producer-Seite deferrt ueber eine eigene Sync-App (SyncPsycopgConnector,
    #   nur defer) auf dieselbe DB — dokumentiertes Grundmuster; wichtig ist nur,
    #   dass Producer- und Daemon-App dieselben TASK-NAMEN kennen (Verifikation
    #   Punkt 13) — deshalb ist die Task-Definition ein gemeinsames Modul.
    # Context-Manager (sync). __enter__ = preflight:
    #   Endpoint existiert; Soll/Ist gegen die Konstanten aus Betriebshandbuch 2/4
    #   (workersMax>=1, workersMin==0, idleTimeout=60, executionTimeoutMs=600000;
    #   Env nur prüfbar wenn Worker-Einträge da — sonst "nicht prüfbar",
    #   nie stillschweigend OK); Image-Tag (GraphQL) — alter v1.8.0-Tag = WARNUNG,
    #   kein strict-Blocker; Datacenter-Konfiguration wird mitgemeldet (EU-Pflicht
    #   fuer den Steuer-Korpus, Betriebshandbuch 3.7);
    #   Balance >= min_balance; Daemon-Lebenszeichen: Procrastinate haelt
    #   Worker-Heartbeats in der DB, exponiert dafuer aber KEINE dokumentierte
    #   Lese-API oder Tabelle (Verifikation Punkt 12 — nur das Django-Modell
    #   ProcrastinateWorker(id, last_heartbeat) ist dokumentiert). Der Check
    #   kapselt deshalb einen Direktzugriff auf die beim Bau am Objekt
    #   festzustellende Heartbeat-Tabelle in EINER Funktion mit Warnkommentar
    #   (undokumentierte, versionsabhaengige Flaeche — bei jedem
    #   Procrastinate-Upgrade neu pruefen).
    #   Drift: strict -> ConfigDriftError, sonst Warnung. MUTIERT NIE (Kosten-Regel).
    def ocr_start(mega_oder_wrapper) -> selbes Objekt
        # deferrt jede Zeile OHNE Terminal-Status (also alle 'new' — auch die,
        # deren juengster Job terminal starb: "es wird dann ncohmal");
        # queueing_lock=sha256 faengt Wartende (AlreadyEnqueued -> "läuft schon"),
        # lock=sha256 serialisiert gegen Laufende (Duplikat endet am Guard als Noop)
    def ocr(x: bytes | OCRWrapper) -> OCRWrapper        # Einzelstück: defer + wait
    def ocr_path(path) -> OCRWrapper                    # dito
    def wait(sha_oder_wrapper, timeout=None) -> OCRWrapper
        # "das awaiten des resultats als seperate funktion" (2.3):
        # LISTEN doc_done + Status-Erstabfrage (kein Lost-Wakeup-Fenster), Poll-Fallback.
        # doc_done ist EIGENBAU (NOTIFY aus dem Task beim finalen Schreiben) —
        # Procrastinate hat kein Ergebnis-Notify an externe Warter (Verifikation Punkt 10).
        # Gibt bei JEDEM Terminal-Zustand den Wrapper zurueck (Aufrufer prueft
        # .success); timeout=None = unbegrenzt, sonst TimeoutError; unbekannter
        # sha -> Fehler. Poll-Fallback: alle 5 s die Zeile lesen.
    def retry_failed(mega_oder_wrapper)                 # duenner Durchgriff auf
        # Procrastinates eigenes Failed-Retry-Tooling (retry_job_by_id; nimmt nur
        # doing|failed an — aborted heilt ueber ocr_start; Verifikation Punkt 19)
    def status() -> dict
        # der ABGELEITETE Status (4.1): Terminal-Zaehler aus documents;
        # Wartend/Laufend/Versuche aus Procrastinate (list_jobs per
        # queueing_lock-Link, list_tasks-Statistiken) — nie gespiegelt
    def credit() -> float                               # GraphQL, live (Braindump-Vorgabe)
    def account() -> dict / workers() -> list[dict]     # RunPod-Lesezugriffe
```

**Intentionen:**
- **`ocr_start` blockiert nicht** — es reiht ein und kehrt zurück (der Daemon arbeitet). Wer blockieren will, nimmt `ocr`/`wait`. Das trennt sauber „losschicken ist einfach einen job erstellen" von „das warten … eine seperate funktion" (2.3, wörtlich).
- **Der Preflight prüft auch den Daemon** (über Procrastinates Worker-Heartbeat-Tabelle): Das häufigste Betriebs-Missverständnis dieser Architektur ist „nichts passiert" bei totem Daemon — der Client sagt es einem, statt es einen suchen zu lassen.
- **Getter cachen im Objekt, nie in der DB** (2.1: „von mir aus auch gerne gecached") — Invalidierung über `refresh()`; gespeichert wird ausschließlich Raw.
- ~~Der `foreign_filter`-Hook in `assembled_text` adressiert die dokumentierte Falle, dass MinerU englische Modell-Prosa in Möblierungsblöcke legt (Kalibrierung 5.1) — die Filter-ENTSCHEIDUNG gehört zum Write-back, der Hook hält nur die Stelle offen.~~ **ZURÜCKGENOMMEN (Jurgen, 09.08.2026; Kasten zu Beginn dieses Abschnitts).** Den Hook gibt es nicht mehr; `rendered_markdown` hat keine Schalter. Die MESSUNG dahinter steht weiterhin und ist jetzt eine Eigenschaft des Ergebnisses statt der Anlass für einen Parameter: Über die 15 Kalibrierungs-Dokumente liegen 30 Fundstellen englischer Modell-Prosa in genau den Randblock-Klassen, die angehängt werden — „im selben Feld und mit demselben Typ wie echter Inhalt". Wer `rendered_markdown` benutzt, bekommt sie MIT, und sie ist über `huelle["content_list"]` als eigener Eintrag samt `type` und `page_idx` wiederauffindbar. Nachprüf-Ort: `tests/test_assembly.py::test_modell_prosa_kommt_MIT_und_ist_auffindbar`.

---

## 8. Harte Plattform-Regeln, die eingebaut sein MÜSSEN

Vollform und Belege im Betriebshandbuch; hier die Checkliste, gegen die die Implementierung und ihr Review geprüft werden:

| # | Regel | Wo im Design |
|---|---|---|
| 1 | Abruf-Regel: alles holen, alles persistieren, nie filtern | Transport tarball_b64 (formats = No-op) + volle Antwort in `raw` |
| 2 | `/run`+`/status`, nie `/runsync` nach Ruhe (60 s vs. 8,5 min Kaltstart) | Daemon-Task |
| 3 | Kein JSON-`null` für optionale Felder; Payload Feld für Feld | Submit-Helfer |
| 4 | Adopt-statt-Resubmit auf Dokument-Ebene: dasselbe Dokument nie doppelt in der Queue | sha-Primärschlüssel + `queueing_lock` (4.1/5.1). Innerhalb eines Dokuments per Festlegung 2.4 bewusst NICHT (Crash = Dokument von vorn) |
| 5 | 30-min-Retention: Ergebnisse zeitnah abholen | Chunks werden sequenziell submittet UND gepollt — kein Ergebnis wartet länger als seine eigene Poll-Schleife (5.1) |
| 6 | Leeres Ergebnis = eigener gültiger Zustand | `empty` (4.1/4.2); Kriterium wird beim Bauen am API-Verhalten festgestellt |
| 7 | Einziger Retry-Eigentümer lokal; RunPod-`/retry` tabu | OCRRetryStrategy (5.1) |
| 8 | Balance-Wache; leeres Konto pausiert statt scheitert | Warteschleife im Task, Versuchszähler unberührt (5.1) |
| 9 | pypdf-Verbot; Seitenzahl-Abgleich nach jedem Schnitt | Chunking (6) |
| 10 | Bezahlte Ressourcen nie eigenmächtig anlegen/ändern | Preflight prüft nur (7) |
| 11 | Aggregat-Zähler lügen; Worker-Liste ist die Wahrheit | `workers()` liest per-Worker-Liste |
| 12 | API-Key nie in URLs; GraphQL braucht Browser-User-Agent | credit()/account() |
| 13 | PII: EU-DC für Steuer-Korpus; kein s3/kein externer Dienst als Default | 6 (kein s3); Preflight meldet die Datacenter-Konfiguration mit (7) |
| 14 | Test-before-bulk: erst 1 Dokument, Output mit Augen lesen, Rechenkette nachrechnen | Teststrategie (10) |

---

## 9. Umgebung (live verifiziert am 05.08.2026; Rebuild-Nachprüfung am 06.08.2026)

- **Python:** Miniconda-Base `/opt/homebrew/Caskroom/miniconda/base/bin/python3` (3.12). Jurgen benutzt **keine venvs**. Homebrew-Python (3.14) ist PEP-668-gesperrt.
- **Bereits installiert:** `procrastinate 3.9.0` (am 05.08.2026 von 3.7.3 aktualisiert), `psycopg 3.3.3` (+pool), `httpx 0.28.1`, `pypdfium2 4.30.0`. **Es fehlt nichts.** (`rocksdict` liegt auch dort — wird nicht mehr gebraucht.)
- **Postgres:** 18.4 (Postgres.app), Port 5437, läuft; Verbindung als `postgres` und `jurgen` bestätigt. Vorhandene DBs: `postgres`, `jurgen`, `gbrain`, Templates.
- **Eigene Datenbank `mineru_ocr` auf dieser Instanz** (per `CREATE DATABASE`, beim Setup). Intention: dieselbe laufende Instanz (kein zweiter Server, nichts Neues zu betreiben), aber strikte Trennung von gbrain-Daten — eigene DB statt eigenem Schema in fremder DB, damit Backup/Drop/Migrationen sich nie berühren.
- **Schema-Setup:** `procrastinate schema --apply` (gegen `mineru_ocr`) + eigenes `documents`-DDL; beides idempotent in einem `mineru_ocr setup`-Kommando.
- **RunPod-Endpoint:** `l79lebhinxaqjz`, läuft auf Image `:17d4955b2` = Worker **v1.8.0** mit dem Scale-down-Terminalfehler. Fork ist auf den v1.8.4-Fix gesynct, Release `v1.8.4` ist angelegt (05.08.) — **aber auch ~14 h später (Nachprüfung 06.08. per GraphQL) zeigt das Template unverändert `:17d4955b2`: Der Rebuild ist mutmaßlich fehlgeschlagen oder wurde nie angestoßen.** Klären kann das nur Console → Builds (Build-Fehlschläge sind API-seitig unsichtbar; Verdachtskandidat laut Betriebshandbuch 4: die 30-min-Docker-Build-Grenze, weil das Dockerfile beim Bauen beide Modelle lädt). Das Daemon-Design ist dagegen robust (der `retry_stalled`-Wächter legt hängende Jobs neu vor, das Dokument läuft von vorn — Festlegung 2.4), aber der 30k-Lauf wartet auf das neue Image (Betriebshandbuch 5a.6).

---

## 10. Teststrategie

1. **Unit, offline, kostenlos:** MIME-Magic (je Format ein Fixture-Kopf, inkl. Nicht-Office-ZIP-Reject), Hashing, Chunk-Schnitt-Logik (synthetische Größen/Seitenzahlen, Grenzwerte, rekursives Halbieren), pypdfium2-Schnitt + Seitenzahl-Abgleich, Merge (page_idx-Offset, Bild-Union) **gegen die sechs konservierten echten Antworten** aus `artifacts/2026-08-05-doku-verifikationstest/` als Fixtures, Dokument-Zustandsübergänge, `AlreadyEnqueued`-Behandlung. Procrastinate-Anteile mit `testing.InMemoryConnector` (offizielles Testwerkzeug).
2. **Integration lokal:** ephemere Test-DB auf der 5437-Instanz (`mineru_ocr_test`), Daemon mit gemocktem RunPod-HTTP (httpx-MockTransport). Pflicht-Szenarien, je mit Zeilen- UND Job-Endzustand als Assertion: (a) Crash-Simulation — Task mitten im Dokument killen → `retry_stalled` legt neu vor → Dokument von vorn, endet `done`; (b) **nicht-retrybarer Fehler im ERSTEN Versuch** → Zeile `failed` sofort, jüngster Job `failed`; (c) **Retry-Erschöpfung** nach 3 transienten Fehlern → Zeile `failed`; (d) **Balance-Pause** → Task wartet, Versuchszähler bleibt unverändert, nach „Aufladen" läuft er durch; (e) **Shutdown-Abort** mitten im Dokument → Job `aborted` (terminal, kein automatischer Retry — und `retry_job_by_id` nimmt `aborted` nicht an, Verifikation Punkt 19), Zeile bleibt `new`, abgeleiteter Status zeigt „jüngster Job aborted" → erneutes `ocr_start` reiht neu ein → endet `done`; (f) Doppel-Einreihungs-Schutz (`AlreadyEnqueued`); (g) Failed-Retry über Procrastinate-Tooling (`retry_job_by_id` → Job wieder `todo`, Event-Historie bleibt — UND das Dokument wird wirklich neu verarbeitet: endet `done`, `raw` überschrieben; genau diese Assertion fängt einen Terminal-Guard, der fälschlich `failed` deckt); (h) **Zwei-Koroutinen-Isolation** (Verifikation Punkt 1, NICHT BELEGT): alle OCR-Slots blockiert → `retry_stalled` auf der maintenance-Queue läuft trotzdem — schlägt das fehl, wird auf den Zwei-Prozess-Fallback umgeplant (5.2); (i) **Duplikat-Defer während `doing`** → per `lock` serialisiert, Duplikat endet am Terminal-Guard als Noop, keine Doppelzahlung; (j) **attempts-Verhalten bei Wiedervorlagen** als Assertion bestätigen (Verifikation Punkt 19, am Objekt aus dem SQL-Schema belegt: jedes Zurücksetzen auf `todo` — stalled-Wiederaufnahme wie manueller Retry — erhöht `attempts` und zehrt am `max_attempts`-Budget), Ergebnis als Kommentar an die Retry-Strategie.
3. **Live-Smoke (Cent-Bereich, zugleich v1.8.4-Verifikation, mit Zustimmung):** 1 kleines PDF ganz; 1 PDF mit erzwungenem Chunking (Schwelle temporär klein); 1 Bild; 1 „Ordner wächst"-Durchlauf; Ergebnis mit eigenen Augen lesen + eine Rechenkette nachrechnen (Halluzinations-Check, Betriebshandbuch 9.1-5).

---

## 11. Was ausdrücklich NICHT gebaut wird (mit Intention)

- **Kein `s3`-Transport** — wäre ein weiterer externer Dienst mit Steuer-PII; das ist Jurgens Entscheidung, kein Default (PII-Regel).
- **Kein Rendering-Fallback** für Einzelseiten > 7 MB und keine Zerlegung übergroßer Nicht-PDFs — Fall ohne belegtes Vorkommen; ehrlicher `input_error` **mit Aufrüst-Hinweis und Aufgaben-Eintrag** statt stiller Komplexität (Festlegung 2.4).
- **Keine Endpoint-Mutation** (workersMax setzen usw.) — Kosten-Zustimmungsregel; der Preflight berichtet nur.
- **Keine launchd-/Autostart-Automatik im ersten Wurf** — erst den Daemon im echten Betrieb beobachten (5.3).
- **Keine DEVONthink-Integration** — Erkennung/Export/Write-back sind eigene, überwiegend offene Stufen außerhalb dieses Repositorys; dieser Service liefert ihnen nur die saubere Schnittstelle (documents-Zeile + Getter).
- **Kein eigenes Web-/Status-UI** — `report()`/`status()` + SQL reichen; die `procrastinate_events`-Tabelle hält die Historie.
- **Kein Metadata-normalisierter Hash** — Begründung in 4.1.
- **Kein `pipeline`-Backend als Default** — `vlm-auto-engine` ist das gemessene; `backend` bleibt als Parameter durchgereicht (die benannte Messlücke aus Betriebshandbuch 6 bleibt eine Messlücke, keine stille Umentscheidung).

## 12. Offene Entscheidungen (klein, benannt, blockieren den Baubeginn nicht)

1. **`concurrency`-Startwert 24** — begründete Setzung, keine Messung; nach dem ersten echten Batch gegen die RunPod-Worker-Auslastung nachjustieren.
2. **Chunk-Schwelle 20 Seiten / Antwort-Budget** — konservative Herleitung aus Inline-Messwerten; die Tarball-Realität (Layout-PDFs) wird beim Live-Smoke einmal nachgemessen.
3. **`remove_old_jobs`-Aufräum-Cron** — erst nach dem Bulk entscheiden (Datenlage ansehen).
4. **Daemon-Autostart (launchd)** — nach Beobachtungsphase.
5. **DB-Backup-Regime für `mineru_ocr`** — vor dem 30k-Lauf festlegen (die Raw-Section wird dann zweistellig GB und ist die einzige Kopie der Ergebnisse).

## 13. Bau-Reihenfolge

1. `mineru_ocr/`-Paket: `errors.py`, `mime.py` (Magic Bytes), `chunking.py` (Schnitt + Merge + Schwellen) — mit Unit-Tests gegen die konservierten Antworten.
2. `db.py`: DSN, `setup` (DB anlegen, procrastinate-Schema, documents-DDL, idempotent).
3. `daemon.py`: App, `ocr_document` (inkl. Balance-Warteschleife), Retry-Strategie + gemeinsame Terminal-Entscheidungsfunktion, `retry_stalled`-Wächter, Start-Kommando.
4. `client.py`: `OCRClient` (Preflight, ocr_start/ocr/wait/status/credit/workers).
5. `wrapper.py`: `OCRWrapper`, `OCRMegaWrapper`.
6. Integrationstests (Mock-RunPod, Crash-Szenarien).
7. Live-Smoke (10.3) — zugleich v1.8.4-Image-Verifikation, sofern der Rebuild bis dahin da ist.
