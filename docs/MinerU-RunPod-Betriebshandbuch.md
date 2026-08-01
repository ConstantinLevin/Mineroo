# MinerU-OCR auf RunPod — Betriebshandbuch für Automation und Lifecycle-Management

**Stand:** 01.08.2026.
**Zweck:** Dieses Dokument enthält alles, was jemand — Mensch oder Agent — mit null Vorkontext braucht, um die MinerU-OCR-Pipeline auf RunPod zu betreiben, zu automatisieren und ihren Lebenszyklus (Anlegen, Befüllen, Beobachten, Skalieren, Herunterfahren) zu steuern, ohne die verstreuten Quellartefakte noch einmal lesen zu müssen. Es ist bewusst maximal ausführlich; Kürze wird in diesem Projekt nie belohnt.
**Schwesterdokumente:** [MinerU-RunPod-Geschichte-und-Entscheidungen.md](MinerU-RunPod-Geschichte-und-Entscheidungen.md) (warum die Dinge so entschieden wurden, Chronik, Lehren) und [MinerU-OCR-Kalibrierung-und-Backend-Vergleich.md](MinerU-OCR-Kalibrierung-und-Backend-Vergleich.md) (die Qualitäts-Beweisschicht: welches OCR was auf den echten Dokumenten leistet).

**Quellen dieses Dokuments** (alle in der Session vom 31.07./01.08.2026 vollständig gelesen):
- Skill `recreate-mineru-serverless`: `/Users/jurgen/CodingProjects/DevonThink886/.claude/skills/recreate-mineru-serverless/SKILL.md` (neu geschrieben 2026-07-12, Driver-Abschnitt ergänzt 2026-07-31) samt `scripts/poll_ready.py`, `scripts/preflight.py`, `scripts/probe.py`.
- gbrain-Seiten: `projects/mineru-ocr/architecture-decision-qb-daemon` (Entscheidungsdokument, settled 2026-07-03, validiert 2026-07-12), `projects/mineru-ocr/mineru-runpod-repo-dossier` (Volllektüre des Worker-Repos, 2026-07-03), `projects/mineru-ocr/runpod-platform-gotchas` (systematischer Docs-Sweep von docs.runpod.io, 2026-07-03), `projects/mineru-ocr/procrastinate-verdict` (2026-07-03), `projects/mineru-ocr/2026-07-12-lb-relapse-incident` (verifizierte Plattform-Fakten), `projects/mineru-ocr/eur30-lesson-lb-vs-qb` (Kosten-Fakten samt Retraction), `projects/mineru-ocr/lb-era-operational-record` (historische Messbasis), `wiki/personal/patterns/runpod-operational-discipline` (die 10 Betriebsregeln).
- Beads: `jurgen-09v` (30k-Lauf, Fassung nach der Korrektur vom 2026-07-31), `jurgen-egm` (OCR-Voraussetzung der Klassifikation), `jurgen-dmg` (Quota-Erhöhung).
- Die beiden Driver-Skripte: `/Users/jurgen/AgentRuns/NewOrderDevon/scripts/ocr_run.py` und `/Users/jurgen/AgentRuns/NewOrderDevon/scripts/ocr_pdfs.py` (byte-identische Dauerkopien der Originale aus `/Users/jurgen/.claude/jobs/0fba4d0a/tmp/`, kopiert 2026-07-31, per `diff` verifiziert laut Skill).

**Grundsatz für jede Angabe hier:** Konfigurations- und Zustandsangaben (Endpoint-IDs, Worker-Zahlen, Preise, Kontostand) sind Momentaufnahmen. Der Ort, an dem man den echten Zustand nachprüft, steht jeweils dabei — nachprüfen schlägt glauben, immer.

---

## 1. Was dieses System tut, in einem Absatz

Ein RunPod-**queue-basierter** Serverless-Endpoint führt die komplette MinerU-OCR-Pipeline im Worker aus (Backend `vlm-auto-engine`, Modell `MinerU2.5-Pro-2605-1.2B`, beide Modelle ins Image eingebacken). Ein HTTP-Job = eine Seite (oder ein Dokument). Der Worker wärmt das Modell auf, **bevor** er sich als bereit meldet, und liefert Markdown inline in der Antwort zurück. Jobs warten in RunPods Queue durch Kaltstarts hindurch (TTL default 24 h) — die Fehlerklasse „Request abgefeuert, bevor der Worker bereit war" existiert auf diesem Weg nicht. Worker skalieren bei Leerlauf auf null und kosten dann nichts. Bewiesen am 2026-07-12 an zwei echten Läufen: **593 Buchseiten, 592 mit Text, 1 echt leere Seite, null fehlgeschlagene Jobs, $0,58 gesamt, ~4,3 s/Seite auf einer RTX 4090** und (zweiter Lauf, gleiche Session) **932/932 Seiten über 39 PDFs, $1,38, 0 Fehler**. [Quelle: Skill-Kopf; gbrain `2026-07-12-lb-relapse-incident`; Session-Job-State `/Users/jurgen/.claude/jobs/0fba4d0a/state.json`]

Der Anlass ist das DEVONthink-Klassifikationsprojekt (Datenbank `Datenbank`): ~30.000 Dokumente brauchen eine brauchbare Textebene, bevor die Text-Detektoren der Klassifikation (siehe `Klassifizierungs-Handbuch.md`) überhaupt sehen können. Details und Vorgeschichte im Schwesterdokument zur Geschichte.

**Was dieses System ausdrücklich NICHT ist:** kein Load-Balancing-Endpoint. Die LB-Architektur wurde am 2026-07-03 aufgegeben, ihr Endpoint `sbdpcrzwlndrgb` am 2026-07-01 gelöscht. Jede Anleitung, die einen LB-Endpoint, das Image `ghcr.io/constantinhvber/mineru-vllm`, das Skript `mineru_runner.py`, das Venv `~/.venvs/mineru-client`, Pre-Warming/`workersStandby`-Tänze oder die Datei `IMAPIECEOFSHITHOWYOUCANAVOIDBEINGONE.md` beschreibt, ist tot und darf nicht befolgt werden. [Quelle: gbrain `architecture-decision-qb-daemon`; Bead `jurgen-09v`]

---

## 2. Feste Fakten der stehenden Infrastruktur

Alle Werte verifiziert am 2026-07-12, dokumentiert im Skill; live nachprüfbar über `runpodctl` bzw. die RunPod-MCP-Tools (siehe Abschnitt 10).

| Was | Wert | Nachprüf-Ort |
|---|---|---|
| GitHub-Fork (Quelle des Images) | `github.com/ConstantinHvber/mineru-runpod` (Fork von `sergeyshmakov/mineru-runpod`) | GitHub |
| Endpoint-ID | `l79lebhinxaqjz` (Name `mineru-runpod`), Typ **Queue** | `runpodctl` / MCP `get-endpoint` |
| Template-ID | `5wv9rko6zv` | MCP `get-template` |
| Image | `registry.runpod.net/constantinhvber-mineru-runpod-main-dockerfile:<9-Zeichen-Commit-SHA>`, Stand 07/2026: `:17d4955b2`; **~27 GB**; keine Registry-Auth nötig (RunPods eigene Registry, in unseren Account gebaut) | Template |
| Namensschema der RunPod-Builds | `<owner-kleingeschrieben>-<repo>-<branch>-dockerfile:<sha9>` | Console → Builds |
| Kaltstart | ~8,5 min (27-GB-Pull + VLM-Load in den GPU-Speicher); Warm-Restart ~1–2 min (nur VLM-Load), Image dann auf dem Host gecacht | Worker-Liste, `startedAt` |
| Durchsatz | ~4,3 s/Seite/Worker (Ausführungszeit); 3 Worker ≈ 30 Seiten/min Ende-zu-Ende inkl. Abhol-Roundtrip | Messung 2026-07-12 |
| Kosten | $0,69/h pro RTX-4090-Worker; 593 Seiten auf 2 effektiven Workern = $0,58 ≈ **$0,001/Seite** | `runpodctl user`, `get-billing` |
| GPU-Wahl | `ADA_24` (RTX 4090) primär — niedrigster $/Seite trotz höherem $/h; als Stock-Reserve `AMPERE_24`/`AMPERE_48`; **nie `ADA_48_PRO`** (RunPod mischt dort Blackwell-Karten ein, denen Kernel fehlen → VLM-Backend crasht) | Repo-Dossier + Skill |
| MinerU-Modell | `MinerU2.5-Pro-2605-1.2B` (2026-05-21) — es gibt **keinen Nachfolger**; „2.5" ist die Modell-Linie, „3.4.x" die Python-Paket-Linie; MinerUs eigene Changelog-Seite ist veraltet, maßgeblich sind PyPI + HuggingFace | huggingface.co/opendatalab, pypi.org/project/mineru |
| Kanonisches lokales Venv | `/Users/jurgen/CodingProjects/DevonThink886/.venv` (Python 3.12, `mineru[core]`) — das EINZIGE; `~/.venvs/mineru-client` ist tot | Dateisystem |
| API-Key | `RUNPOD_API_KEY`, liegt in `~/.runpod/config.toml` (Feld `apikey`) | Datei |

**Warnung zur Endpoint-Lebensdauer:** Endpoint und Template bei Nichtgebrauch **stehen lassen**. Bei `workersMin=0` ohne Jobs kostet der Endpoint $0/h (wiederholt beobachtet); Löschen erzwingt beim nächsten Mal einen frischen 27-GB-Pull. **Aber:** RunPod skaliert unbenutzte Endpoints selbst herunter — 3 Tage ohne Requests → `workersMax=2` (plus E-Mail), 7 Tage → `workersMax=0`, und es gibt **keine automatische Wiederherstellung**. Jede Automation muss beim Adoptieren eines bestehenden Endpoints die Konfiguration neu durchsetzen statt sie vorauszusetzen. [Quelle: gbrain `runpod-platform-gotchas`, Docs-Sweep 2026-07-03]

---

## 3. Architektur und ihre Begründung

1. **Queue-based statt Load-Balancing.** Die Queue hält Jobs durch Kaltstarts (TTL default 24 h). Auf dem LB-Endpoint wurden Requests nach 2 Minuten ohne Worker verworfen (`400 timed out waiting for worker`) — ein 15-Dokumente-Burst schlug damit zu 100 % fehl. Diese Fehlerklasse existiert auf QB nicht; deshalb braucht QB auch kein Pre-Warming. [Quelle: gbrain `eur30-lesson-lb-vs-qb`, `architecture-decision-qb-daemon`]
2. **Ganze Pipeline im Worker** (`vlm-auto-engine` = In-Process-vLLM-Engine). Der Worker macht Fitness-Checks → eifriges Modell-Warmup mit echtem Parse → **erst dann** Heartbeat. Ein Worker kann keine Jobs bekommen, bevor die Engine wirklich bedient — das „ready lügt"-Problem ist an der Quelle gelöst. [Quelle: gbrain `mineru-runpod-repo-dossier`]
3. **Ein Job = eine Seite** (beim Buch-Lauf) bzw. ein Dokument. Macht Verlust unmöglich sichtbar (fehlende Seite = fehlender Job) und hält Payloads klein.
4. **Lokale Seite = Procrastinate-Daemon auf PostgreSQL** (entworfen, **noch nicht gebaut** — Abschnitt 12). Der 2026-07-12-Lauf nutzte stattdessen einfache Submit/Poll-Skripte (Abschnitt 11).
5. **Einziger Retry-Eigentümer ist der lokale Treiber/Daemon.** RunPods `/retry` wird bewusst nicht benutzt (Doppel-Eigentümerschaft), das Worker-Repo retryt nichts (fail-loud, per grep verifiziert). Dazu die **Adopt-statt-Resubmit-Regel**: Ein Task-Versuch prüft zuerst, ob für diese Einheit schon eine lebende RunPod-Job-ID vorliegt, und pollt die weiter, statt neu zu submitten — doppelte GPU-Ausgaben werden strukturell unmöglich. [Quelle: gbrain `architecture-decision-qb-daemon`, `procrastinate-verdict`]
6. **Kein Network-Volume.** Drei unabhängige, verifizierte Gründe: (a) Ein Volume kann kein Container-Image beherbergen — das Image ist ein Docker-Layer-Pull auf den Host, **bevor** ein Container existiert; das Volume mountet erst danach hinein. (b) Die Modellgewichte werden zur Laufzeit gar nicht geladen — beide Modelle sind ins Image gebacken (`snapshot_download` beim Build) und `HF_HUB_OFFLINE=1` verbietet Downloads; das Repo-eigene Volume-Guide sagt wörtlich: „a volume for model weights duplicates that work". (c) Die Gewichte sind nur ~4 GB der ~27 GB — der Rest ist die CUDA/vLLM-Basis, die sowieso gepullt werden muss. Warum der Autor backt statt cacht: RunPods Cached-Models-Feature kann **ein** Modell pro Endpoint, MinerU braucht **zwei** (VLM + PDF-Extract-Kit). [Quelle: gbrain `2026-07-12-lb-relapse-incident`, Abschnitt „RESOLVED"]
7. **EU-Platzierung ist eine Pro-Job-Entscheidung, keine Endpoint-Konstante.** GDPR erzwingt EU-Datacenter **für den deutschen Steuer-PII-Korpus** (also für den 30k-Lauf). Der 593-Seiten-Buchlauf hatte keine PII und lief auf US-Datacentern auf ausdrückliche Anweisung von Jurgen. Regel: die Datacenter-Beschränkung aus den Daten ableiten, pro Job/Lauf. Die 9 EU/EWR-Datacenter: EU-RO-1, EU-SE-1, EU-CZ-1, EU-NL-1, EU-FR-1, EUR-NO-1, EUR-IS-1, EUR-IS-2, EUR-IS-3. [Quelle: gbrain `architecture-decision-qb-daemon`; `runpod-platform-gotchas`]

---

## 4. Deploy von Null (nur nötig, wenn der Endpoint weg ist)

**Der einzige Weg zu einem pullbaren Image ist RunPods GitHub-Integration, und die ist Console-only.** Weder `runpodctl` noch das RunPod-MCP noch die REST-API können einen Build anstoßen — alle nehmen nur ein fertiges `imageName` entgegen. [Quelle: Skill; gbrain `2026-07-12-lb-relapse-incident`]

Schritte:
1. Fork `sergeyshmakov/mineru-runpod` in den GitHub-Account (bereits geschehen: `ConstantinHvber/mineru-runpod`).
2. GitHub einmalig mit RunPod verbinden: Console → Settings → Connections → GitHub → Connect.
3. Console → **Serverless → New Endpoint → Import Git Repository** → Fork wählen → Branch `main`, Dockerfile-Pfad `Dockerfile` → Next.
4. Endpoint-Einstellungen — **jede einzelne davon trägt**:

| Einstellung | Wert | Warum |
|---|---|---|
| Endpoint Type | **Queue** | Nicht Load Balancer — LB ist die aufgegebene Architektur. |
| Container Disk | **50 GB** | Die Console defaultet auf **5 GB**. Ein 27-GB-Image passt da nicht hinein, und RunPod meldet das als `This machine does not have the resources to deploy your pod` — das sieht aus wie GPU-Knappheit und ist keine. Das Repo-eigene `.runpod/hub.json` sagt 50 GB. |
| Max Workers | **explizit setzen** | Der Default ist **3** — eine stille 3-fache Ausgabenfreigabe. |
| Active / Min Workers | **0** | Scale-to-zero. |
| Idle Timeout | 60 s | Siehe die Falle „nie einen Worker mitten im Batch idlen lassen" in Abschnitt 9.2. |
| Execution Timeout | 900 s | Pro Job. |
| FlashBoot | an | Schnellere Warm-Restarts (Mechanik in Abschnitt 8). |
| GPU | `ADA_24`, optional + `AMPERE_24`/`AMPERE_48` | 4090 = niedrigster $/Seite; nie `ADA_48_PRO`. |
| Env `RUNPOD_INIT_TIMEOUT` | **3600** | Default ~7 min. Ein 27-GB-Pull dauert länger; feuert der Timeout, wird der Worker als unhealthy gekillt und pullt erneut — die Churn-Schleife. Ausdrücklich NICHT den alten Wert `1200` kopieren: 20 Minuten waren kürzer als der real gemessene Pull. |
| Env `MINERU_MAX_CONCURRENCY` | 1 | Überall sicher (vLLM auf kleineren GPUs nicht nebenläufig ansteuern). |

5. **Builds** beobachten: Pending → Building → Uploading → Testing → Completed. Ein fehlgeschlagener **Testing**-Schritt heißt NICHT, dass das Image kaputt ist — es ist zu dem Zeitpunkt bereits in die Registry gepusht; meist konnte nur der Test-Pod nicht platziert werden (Container-Disk-Falle oben).

**Verbote und Fallen:**
- **NIE `runpodctl serverless create --hub-id`.** Das erzeugt ein Template, das auf das **private Hub-Image des Publishers** zeigt, mit `containerRegistryAuthId: null`. Der Worker loopt dann ewig auf `Failed to get Hub registry auth. Is this a template you have access to?` (17-mal im Log, 40 Minuten, null Bytes bewegt, am 2026-07-12 real bezahlt). Es gibt keinen Weg, Credentials für den Hub-Build eines fremden Accounts zu bekommen. Eine frühere Fassung der Incident-Seite behauptete kurz, der Hub-Weg funktioniere — das wurde zurückgenommen.
- **Image-Updates laufen über GitHub-Releases, nicht Commits.** Push auf den Fork, dann Release anlegen. Keine Versionsnummer erfinden, um einen Rebuild zu erzwingen — wenn das Image schon gebaut und gepusht ist, braucht man keinen.

---

## 5. Der Job-Contract (vollständig)

Basis-URL: `https://api.runpod.ai/v2/l79lebhinxaqjz`, Auth: Header `Authorization: Bearer $RUNPOD_API_KEY`. [Quelle: Skill; Feld-Semantik aus gbrain `mineru-runpod-repo-dossier` (gelesen aus `worker/schema.py`, `worker/io.py`, `mineru_client/client.py`); Plattform-Semantik aus gbrain `runpod-platform-gotchas`]

### 5.1 Kanonischer Aufruf (der bewiesene Pfad)

```json
POST /run
{
  "input": {
    "file_b64": "<Base64 des Seitenbilds>",
    "backend": "vlm-auto-engine",
    "formats": ["markdown"],
    "transport": "inline"
  }
}
→ {"id": "...", "status": "IN_QUEUE"}

GET /status/<job_id>
→ {"status": "COMPLETED", "executionTime": 4338,
   "output": {"results": [{"markdown": "..."}]}}
```

### 5.2 Eingabefelder

- **Genau eines von** `file_url` | `file_b64` | `volume_path` (XOR). Caps: `file_url` 200 MB, 120 s Fetch-Timeout; `file_b64` 20 MB Handler-seitig, aber Gateway-seitig **10 MB auf `/run`** und 20 MB auf `/runsync`. Ein 1,4-MB-PNG ist als Base64 ~1,9 MB — bequem darunter.
- **NIE JSON-`null` für ein optionales Feld senden** — RunPods `rp_validator` lehnt `null` auch für optionale Felder ab. Nicht gesetzte Felder weglassen (der Repo-eigene Client baut Payloads Feld für Feld auf).
- `backend`: 5 gültige Werte — `pipeline` | `vlm-auto-engine` | `vlm-http-client` | `hybrid-auto-engine` | `hybrid-http-client` — pro Job auf demselben Worker umschaltbar. Wahlbegründung in Abschnitt 6.
- `formats`: Default sind ALLE vier (markdown, content_list, middle, images) — Bandbreitenverschwendung. Für dieses Projekt `["markdown"]`. **Aber siehe die offene Frage in Abschnitt 16 und im Kalibrierungs-Dokument: Bescheid-Aktenzeichen aus der Randspalte fehlen im Markdown-Output** — ob `content_list`/`images` sie tragen, ist ungeprüft; für den 30k-Lauf kann das die Format-Wahl ändern.
- `transport`: `inline` legt das Markdown direkt in die Antwort. Default ist `tarball_b64` (müsste man auspacken); außerdem existiert `s3` (BUCKET_*-Env; R2/Hetzner-kompatibel). **Antwort-Cap 20 MB** (gemessen: ein 82-Seiten-PDF mit Bildern sprengt es auf beiden Wegen — bei Ein-Seiten-Jobs irrelevant).
- `start_page`/`end_page`: Client-seitiges Batching großer Dokumente; Seitenzahl erhöht den VRAM NICHT (der Repo-Autor hat 5.039 Seiten auf einer 24-GB-A5000 für ~$1,15 geparst).
- `lang`: nur vom `pipeline`-Backend beachtet; das VLM ignoriert es (Deutsch = latin, unproblematisch).
- `basename` (Muster `[a-zA-Z0-9_-]+`), `archive_format`, `probe` (Diagnose-Modus) existieren ebenfalls.
- **Bilder sind Eingabe erster Klasse:** PNG/JPEG/GIF/BMP/TIFF/WebP werden per Magic-Bytes erkannt und intern in ein Ein-Seiten-PDF konvertiert. Es gibt KEIN `format`-Eingabefeld. Auch DOCX/PPTX/XLSX werden per Auto-Detect akzeptiert.

### 5.3 Antwort- und Fehlersemantik

- Job-Zustände: `IN_QUEUE`, `IN_PROGRESS`/`RUNNING` (mutmaßlich derselbe Wire-State), `COMPLETED`, `FAILED`, `CANCELLED`, `TIMED_OUT`.
- Fehler-Envelope des Workers: `ok:false` + `error` + Traceback + `debug`-Block (GPU-Name/Compute-Capability, model_dir, Millisekunden je Phase) auf jeder Antwort, auch Fehlern.
- **Eine leere Seite liefert leeres Markdown, und das ist korrekt**, kein Fehler. Erst das Bild ansehen, dann ggf. retryn. (2026-07-12: genau 1 von 593 Seiten; eine echt leere graue Seite mit nur Kolumnentitel.)
- **Der Worker retryt NICHTS** (fail-loud, grep-verifiziert). Retries gehören dem Aufrufer.
- Mitten im Parse abbrechen ist unmöglich (blockierender GPU-Call); SIGTERM drained nur zwischen Phasen.

### 5.4 Plattform-Randbedingungen, die jede Automation einbauen muss

- **Ergebnis-Aufbewahrung: ~30 Minuten für `/run`-Ergebnisse, 1–5 Minuten für `/runsync`.** Danach (oder nach TTL-Ablauf) liefert `/status` **404**, keinen Terminal-Body. ⇒ Bei jedem Batch, dessen Gesamtverarbeitungszeit ~30 min übersteigen kann, verliert „alles submitten, dann einsammeln" die frühesten Ergebnisse stumm. **Einsammeln muss das Submitten ÜBERLAPPEN** (rollierendes In-Flight-Fenster; `ocr_pdfs.py` nutzt genau deshalb `IN_FLIGHT = 120`). Die Regel „ganzen Batch auf einmal submitten" (Abschnitt 9.2) betrifft das Beschäftigt-Halten der Worker, sie ist keine Lizenz, die Abholung ans Ende zu schieben. [Quelle: Skill, Job-Contract-Abschnitt; RunPod-API-Doku, unabhängig im Docstring von `ocr_pdfs.py` (2026-07-12) belegt]
- Pro-Request-Policy: `executionTimeout` (Default 10 min, Max 7 Tage), `ttl` (Default 24 h, Max 7 Tage — **harte Löschung auch mitten in der Ausführung**), `lowPriority` (löst kein Scaling aus). Andere Prioritätsmechanismen gibt es nicht.
- **`run-batch` existiert NICHT** — das war eine Halluzination eines Research-Reports. Nicht danach suchen.
- Rate-Limits: `/run` 1000/10 s, `/runsync` 2000/10 s, `/status` 2000/10 s, `/cancel` 100/10 s, **`/purge-queue` 2/10 s**; effektives Limit skaliert mit laufenden Workern; darüber 429.
- Queue-Tiefen-Cap: undokumentiert (die „100×max_workers"-Behauptung aus einem Research-Report ist unverifiziert).
- Webhooks: 3 Zustellversuche gesamt (2 Retries à 10 s), Payload-Form undokumentiert, kein HMAC — als alleiniger Abhol-Mechanismus ungeeignet.
- Serverless-Worker haben KEINE dokumentierten injizierten Env-Variablen (die `RUNPOD_POD_ID`-Tabelle gilt nur für Pods); der Handler kennt seine Job-ID nur aus `job["id"]`.

---

## 6. Backend-Wahl: pures `vlm-auto-engine` — auf Scans UND auf Born-Digital, aus entgegengesetzten Gründen

- **Auf beschädigten Scans nie `hybrid` mit seinem Default (`effort=medium`):** Hybrid ersetzt den Layout-Pass des VLM durch einen CV-Detektor und kann Text **stumm fallen lassen**.
- **Auf Born-Digital-Material auch nicht `hybrid`**, obwohl dessen Kernversprechen (eingebetteten Text wiederverwenden, kann nicht halluzinieren) dort echt gilt. Gemessen an denselben 5 formeldichten Kursfolien:

| | `vlm-auto-engine` | `hybrid-auto-engine` (effort=high) |
|---|---|---|
| Inline-Formeln erkannt | **41** | 28 |
| Ausführungszeit | **4,1 s** | 8,9 s |

Hybrid verliert 13 von 41 Inline-Formeln und lässt Subskripte stumm fallen — der risikofreie Zins `$R_{f}$` kommt als nacktes `R` zurück. Es ist besser in Typographie und 2,2-mal langsamer. Bei mathematischem Material den Tausch nicht wert. [Quelle: Skill; Vollprotokoll laut Skill-Verweis in gbrain `projects/mineru-ocr/born-digital-backend-benchmark` — diese Seite wurde in der Konsolidierungs-Session nicht gelesen]

---

## 7. PDFs zuführen: rendern, niemals mit pypdf splitten

**Der Weg:** `pdftoppm` (poppler), 150 dpi, eine Seite = ein Job.

```bash
pdftoppm -png -r 150 input.pdf outdir/prefix__      # ~278 KB pro Seite
```

**Verbote, beide real bezahlt:**
- **NIE Seiten mit `pypdf` extrahieren.** `PdfWriter.add_page()` kopiert das komplette `/Resources`-Dictionary der Seite — einschließlich der über das GANZE Dokument geteilten XObject-Bilder. Gemessen 2026-07-12: **360 MB Quell-PDFs wurden 4,1 GB „Einzelseiten"**, im Schnitt 1,5 MB, schlimmster Fall 5,92 MB — gegen ein 10-MB-Payload-Cap, und aus einem Fünf-Minuten-Upload wären geschätzt vier Stunden geworden.
- **NIE einen Splitter Seiten verwerfen lassen, um unter ein Größen-Cap zu kommen.** Eine erste Version schrieb bei einem übergroßen Chunk nur die erste Hälfte der Seiten und verwarf den Rest **ohne Fehler**. Fehlende Seiten erzeugen kein Fehlersignal — sie fehlen einfach, und das gemergte Dokument sieht trotzdem vollständig aus. Ein Job pro Seite macht das unmöglich.

---

## 8. Worker-Interna, die die Automation kennen muss

[Quelle: gbrain `mineru-runpod-repo-dossier`, aus den Quelldateien des Repos verifiziert, nicht aus dem README]

- **Warmup-vor-Heartbeat:** Boot-Reihenfolge = Fitness-Checks → eifriges Modell-Warmup (echter Parse) → erst dann Heartbeat. Ein Worker im Zustand `running`, während Jobs `inQueue` stehen und `inProgress: 0` ist, wärmt gerade — normal, 1–2 Minuten.
- **FlashBoot** ist ein Prozess-Snapshot pro (Host, Image-SHA): ~7–8 s Kaltstart bei Host-Wiederverwendung, ~110 s frischer Boot pro neuem Host (einmalig), +3–5 min Image-Pull auf brandneuem Host. (Vom Repo-Autor per 4-Request-Untersuchung verifiziert.)
- **VRAM:** ~13 GiB aktiv auf 24 GB bei `gpu_memory_utilization=0.5` (KV-Cache 9,5 GiB dominiert; das Modell selbst 2,16 GiB). Seitenzahl erhöht den VRAM nicht. 16-GB-Karten passen mit der Default-Konfiguration nicht (heruntertunbar, lohnt nicht).
- Warm-Durchsatz ist inhaltsabhängig: ~1 s/Seite (gleichförmige Reports) bis ~10 s/Seite (dichte Formulare) auf einer A5000; auf der 4090 1–6 s.
- **Ops-Knöpfe:** `MINERU_MAX_CONCURRENCY` (Default 1 — vLLM auf kleineren GPUs nicht nebenläufig treiben), `REFRESH_WORKER_AFTER_JOBS`/`REFRESH_WORKER_AFTER_PAGES` (VRAM-Fragmentierungs-Hygiene; für große Batches ~100 setzen), Warmup-Knöpfe, optionaler OTel-Export.

---

## 9. Lifecycle: Phasen und Steuerung

### 9.1 Vorflug (vor jedem Lauf)

1. **Brain-first-Lookup** (Regel 0 der Betriebsdisziplin): vor jeder RunPod-Berührung die gbrain-Seiten zum Thema lesen. Die Lektionen sind zweimal teuer bezahlt worden (€30-Session; Ollama-Session 2026-07-07), beide Male, weil vorhandene Lehren nicht konsultiert wurden. [Quelle: gbrain `runpod-operational-discipline`]
2. **Kontostand prüfen:** `runpodctl user` → `clientBalance`, `currentSpendPerHr`, `spendLimit`. Für **Pods** gilt ein Mietminimum um ~$10 (beobachtet: $9,96 → „Your account balance is too low to rent a pod"); ein „hängender Endpoint" ist oft nur ein Balance-Boden, keine GPU-Knappheit. Serverless-Jobs liefen auch bei niedriger Balance; die Driver brechen selbst ab (Submission < $0,15, Abholung < $0,05). Ein 402 `Insufficient Balance` in den Failure-Logs vom Juni war exakt dieses Symptom.
3. **Endpoint-Zustand lesen, nicht annehmen:** Existiert `l79lebhinxaqjz` noch? Stimmen `workersMax`, `idleTimeout`, `RUNPOD_INIT_TIMEOUT` (Auto-Downscale nach 3/7 Tagen Inaktivität, Abschnitt 2)? Worker-Liste als Basiszustand lesen (`list-endpoint-workers`): Alt-Kohorten von früheren Läufen werden von der Plattform **nie** eingesammelt.
4. **Kostenrahmen benennen, bevor Geld fließt:** Anzahl × Stufe × Grund. Bezahlte Ressourcen anzulegen oder Worker-Zahlen zu erhöhen braucht die ausdrückliche Zustimmung von Jurgen — stehende Regel, zweifach eingebrannt (unautorisierte Pods in der €30-Session; unautorisierte `workersMin=4/workersMax=10` am 2026-07-12).
5. **Auf EINER Seite testen, Output mit eigenen Augen lesen, Zahlen prüfen.** Der billige Realitäts-Check gegen VLM-Zahlendreher: eine Formel im OCR finden und **nachrechnen** — eine halluzinierte Zahl erfüllt ihre eigene Gleichung nicht.
6. Für PII-Korpora (der 30k-Lauf): EU-Datacenter-Beschränkung setzen (Liste in Abschnitt 3, Punkt 7).

### 9.2 Submission

- **Ganzen Batch auf einmal submitten.** Ein Worker, der mitten im Batch idlet, wird nach `idleTimeout` abgebaut, und der nächste Job bezahlt ein frisches VLM-Warmup — und DAS wird abgerechnet. Häppchenweises Füttern kostet mehr als die Arbeit.
- **Jede submittete Job-ID sofort persistieren** (Ledger-Datei), BEVOR der nächste Submit rausgeht. Sonst führt ein Crash zu Doppel-Submits = Doppel-Abrechnung. (`ocr_run.py` macht das richtig; `ocr_pdfs.py` macht es falsch — Abschnitt 11.)
- Kontostands-Checks in die Submission einbauen (Vorbild: alle 50 Submits, Abbruch < $0,15).
- Payload-Regeln aus Abschnitt 5.2 beachten (10-MB-Gateway-Cap auf `/run`, kein `null`).

### 9.3 Abholung (Collection)

- **Muss die Submission überlappen** (30-Minuten-Retention, Abschnitt 5.4). Rollierendes Fenster; bewährter Wert `IN_FLIGHT = 120`.
- **Inkrementell speichern** (Vorbild: Zieldatei alle 20 s auf Platte) und ein `.bak` der Zieldatei anlegen, BEVOR der Lauf beginnt. (Beim 593er-Lauf wurde das `.bak` manuell angelegt — keines der Skripte automatisiert es.)
- Pro Job einen Poll-Timeout führen: `ocr_run.py` hat keinen — eine Exception in der Abhol-Schleife wird still übersprungen und beim nächsten 10-Sekunden-Tick ewig erneut versucht.
- Leeres Markdown ≠ Fehler (leere Seite, Abschnitt 5.3). Erst das Bild ansehen.

### 9.4 Beobachtung während des Laufs

- **Die per-Worker-Liste ist die einzige Wahrheit** (`list-endpoint-workers` bzw. REST `GET /endpoints/{id}?includeWorkers=true` → Status, `startedAt`, Datacenter, GPU, `isStale`, `desiredStatus`, `costPerHr`, `machine.dataCenterId`, `lastStatusChange`). **Aggregat-Zähler lügen:** `endpoint-health` meldete `ready: 2` bzw. `ready: 4`, während nichts bediente — zweimal real passiert.
- `startedAt`-Semantik: **konstant** über Minuten = EIN durchlaufender Pull (normal bei 27 GB, auf Serverless einfach abwarten); **springend** = der Init-Timeout killt Worker → `RUNPOD_INIT_TIMEOUT` erhöhen.
- **Worker-Logs:** Stand des Skills (2026-07-12/31) nur in der RunPod-Console lesbar — kein CLI-/MCP-Werkzeug. **Abweichende Beobachtung 08/2026:** Die RunPod-MCP-Oberfläche dieser Umgebung führt inzwischen `stream-worker-logs` und `stream-pod-logs` (als v2-API-Funktionen). Ob sie real funktionieren, ist ungeprüft — vor Verlass darauf einmal live testen; bis dahin gilt: wer nicht hineinsehen kann, sagt das und fragt, statt eine Geschichte zu erfinden.
- Ausgabenrate live: `runpodctl user` → `currentSpendPerHr` (GraphQL). Historik: `get-billing` (REST, nur Zeitreihen).
- **Auf jede Anomalie subtraktiv reagieren, nie additiv:** anhalten und die Primärquelle lesen (Logs, Worker-Liste) — nicht denselben Request neu feuern, nicht die Concurrency erhöhen, nicht noch einen Worker/Pod starten, nicht nochmal neu starten. Nach ein, zwei gleichförmigen Fehlern gilt: Logs lesen, nicht retryn. [Quelle: gbrain `runpod-operational-discipline`, Regel 4]

### 9.5 Skalierung und Quota

- `workersMax` defaultet überall auf 3 (`runpodctl serverless create` UND der Console-GitHub-Deploy) — **immer explizit setzen.**
- **Account-Quota: 10 Worker über ALLE Endpoints.** Für den vollen 30k-Lauf ist eine Erhöhung bei RunPod zu beantragen (Support/Console) — offenes Bead `jurgen-dmg`. Rechenbasis dort: bei 21,5 s/Dokument/Worker liegt der Boden für 30k Dokumente bei ~18 h mit 10 Workern; 20–30 Worker halbieren/dritteln das, EU-GPU-Angebot vorausgesetzt. (Ältere Messbasis, LB-Ära, unsaturiert: 0,40 Seiten/s pro warmem 4090-Klasse-Worker, 68 Seiten/172 s; Formel `Gesamtseiten/(0,4·N)` Sekunden. QB-Messung 2026-07-12: 4,3 s/Seite Ausführungszeit, Ende-zu-Ende ~6 s/Seite/Worker inkl. Abholung. Die Basen unterscheiden sich — nicht mischen.) [Quellen: Bead `jurgen-dmg`; gbrain `lb-era-operational-record`; Skill]
- **Konfigurationsänderungen evakuieren KEINE bestehenden Worker.** `workersMax=1` setzen, während drei laufen, lässt drei laufen (real: 10 residente Worker trotz `workersMax=1` + `idleTimeout=5`). Nach jeder Änderung die Worker-Liste zurücklesen; Alt-Kohorten selbst aufräumen — die Plattform tut es nie.
- Platform-Backstop: Account-Spend-Limit default $80/h (kein Ersatz für ein eigenes Lauf-Budget).

### 9.6 Teardown

**Semantik (Festlegung von Jurgen, wörtlich aus dem Entscheidungsdokument):** „teardown targets *billing*, not deletion; tidiness = full legibility" — Teardown zielt auf die Abrechnung, nicht aufs Löschen; Ordnung heißt: alles voll nachlesbar, nichts Unverbuchtes läuft weiter.

Die funktionierende Sequenz [Quelle: gbrain `architecture-decision-qb-daemon`, Korrektur vom 2026-07-12]:
1. Ausstehende Job-IDs `/cancel`-n (stoppt dokumentiert auch laufende Jobs; `/purge-queue` räumt nur wartende).
2. `/purge-queue` (Achtung Rate-Limit 2/10 s).
3. **`workersMin=0` + kurzer `idleTimeout`** setzen.
4. **Worker-Liste pollen, bis sie nachweislich leer ist.** `workersMin=0` zu setzen wirkt nicht sofort — erst die leere Liste beendet den Teardown.
5. Inventar-Report persistieren; `runpodctl user` → `currentSpendPerHr` muss 0 sein. Nie den Konfigurationswert glauben, immer den Zustand zurücklesen.
6. Endpoint + Template **stehen lassen** ($0/h; tragen das gebaute Image; Löschen erzwingt den nächsten 27-GB-Pull).

**Benannter Widerspruch in den Quellen (nicht still aufgelöst):** Die Incident-Seite vom 2026-07-12 dokumentiert einerseits „You cannot latch workers to zero: `--workers-min 0` silently ignores the 0; `--workers-max 0` → 400; MCP `workersMin: 0` → 500", empfiehlt andererseits im selben Absatz als Stopp-Weg „`workersMin=0` + short idle timeout". Die plausibelste Lesart: `workersMax=0` ist auf keiner Oberfläche setzbar, und das `runpodctl`-Flag `--workers-min 0` sowie der damalige MCP-Aufruf schluckten/fehlerten — während `workersMin=0` über einen anderen Weg (Console/REST) wirksam war. **Welche Oberfläche `workersMin=0` zuverlässig setzt, ist damit nicht abschließend geklärt.** Die Invariante, die den Widerspruch praktisch entschärft, ist Schritt 4: nicht die Konfiguration, sondern die leere Worker-Liste ist das Erfolgskriterium.

### 9.7 Abrechnungs-Wahrheiten

- **Ein POD kostet ab Erzeugung, durch den gesamten Image-Pull hindurch, bis zum Stop.** Gemessen 2026-07-12: $0,697/h über 40 Minuten Pull, ohne dass ein Container lief; Balance fiel von $2,1274 auf $1,7229.
- **Ob SERVERLESS während eines Pulls kostet, ist UNGEMESSEN — in keine Richtung behaupten.** Der eine Messversuch war wertlos (der Worker pullte gar nichts, er scheiterte 17-mal an der Hub-Registry-Auth; null Kosten für null Arbeit sind kein Befund). Spätere Beobachtungen (`spend/hr = 0` während eines echten 27-GB-Pulls auf `l79lebhinxaqjz`) waren nicht sauber isoliert. [Quelle: gbrain `eur30-lesson-lb-vs-qb`, Retraction]
- **RunPods eigene Doku widerspricht sich** zur Frage, ob Init-/Kaltstart-Zeit abgerechnet wird (Worker-States-Tabelle: „Initializing = Billing: No"; Pricing-Seite: „Start time (container init + model load)" als abgerechnete Phase 1). Nie aus der Doku schließen — aus `GET /billing/endpoints`-Istwerten gegen Worker-Präsenz verifizieren.
- Serverless bei `workersMin=0` ohne Jobs: **$0/h**, wiederholt beobachtet. Auf QB ist kein Standby-Mechanismus dokumentiert — anders als die LB-`workersStandby`-Falle, die in der €30-Session der eine zweifelsfreie Kostentreiber war (Warm-Pool kostete trotz `workersMin=0`; das Feld war via `update-endpoint` nicht einmal setzbar).
- Kostenfragen mit der echten Abrechnungs-API klären (`get-billing`), nicht mit Wanduhr-Schätzungen (eine solche Schätzung wich 2026-07-07 deutlich vom Istwert ab).

---

## 10. Werkzeugflächen: womit man was steuert

[Quellen: gbrain `runpod-platform-gotchas` (Docs-Sweep 2026-07-03), `runpod-operational-discipline` Regel 8, Incident-Seite 2026-07-12]

| Fläche | Wofür | Bekannte Fallen |
|---|---|---|
| `runpodctl` (CLI) | `user` (= `me`): live `clientBalance`/`currentSpendPerHr`/`spendLimit` (GraphQL-gestützt); `billing pods`/`billing serverless` | `serverless create` defaultet `--workers-max 3`; `--workers-min 0` schluckte die 0 still (Beobachtung 2026-07-12); kann keine Builds anstoßen |
| RunPod-MCP-Tools (in dieser Claude-Umgebung als `mcp__runpod__*`) | `list-endpoint-workers` (die per-Worker-Wahrheit), `get-endpoint`, `update-endpoint`, `get-billing` (v2, historisch), `run-endpoint`/`runsync-endpoint`, `purge-endpoint-queue`, `cancel-job`, `get-job-status` | `endpoint-health` ist der lügende Aggregat-Zähler — nie als Beleg nehmen; `workersMin: 0` gab am 2026-07-12 einen 500; `stream-worker-logs`/`stream-pod-logs` existieren inzwischen in der Tool-Oberfläche (v2), Funktionsfähigkeit ungeprüft |
| REST-API | Worker-Objekte via `GET /endpoints/{id}?includeWorkers=true`; Billing-Zeitreihen `/billing/*` | **Env-Variablen leben auf TEMPLATES, nicht Endpoints** (PATCH /endpoints hat kein env-Feld) — Env ändern = Template neu/ändern + `templateId` umhängen; ein Serverless-Template bindet an genau EINEN Endpoint. REST kennt **keine GPU-Pools** (`gpuTypeIds` = explizite Modellnamen); Pool-Kürzel wie `ADA_24` sind GraphQL/Hub-only. Kein Log-/Metrik-Abruf per API (Stand Sweep 2026-07-03; Console-Logs 90 Tage) → **der eigene Audit-Store ist der einzige dauerhafte Beleg** |
| GraphQL | `myself { clientBalance currentSpendPerHr spendLimit }` — die einzige Live-Kostenquelle | `locations`-Enum ist grob (Ländercodes inkl. GB) und NICHT äquivalent zu REST-`dataCenterIds`; das LB/QB-Flag ist das interne `EndpointInput.type`-Enum (historisches Wissen, im QB-Zielbild irrelevant) |
| Console (nur dort) | GitHub-Integration-Builds; Worker-Logs (Stand 07/2026); Hub-Deploys | — |
| Flash SDK | Im QB-Zielbild **nicht mehr gebraucht** (war nur zur LB-Erzeugung nötig); liest `RUNPOD_API_KEY` aus der Env, kann `~/.runpod/config.toml` (runpodctl-Format `apikey=`) NICHT parsen | — |

S3-kompatible API auf Network-Volumes: nur 4 EU-Datacenter (EU-CZ-1, EU-RO-1, EUR-IS-1, EUR-NO-1) — eine strikte Teilmenge der 9 Compute-DCs. (Für diese Architektur ohne Volume nur relevant, falls je `transport: "s3"` erwogen wird.)

---

## 11. Die vorhandenen Driver (Ist-Stand der Automation)

Beide Skripte entstanden und liefen in Claude-Code-Session `0fba4d0a-f13a-47f0-b59b-a8f4d9e811a6` (2026-07-12, Projekt `/Users/jurgen/CodingProjects/DevonThink886`). **Von hier aus starten — Dauerkopien:** `/Users/jurgen/AgentRuns/NewOrderDevon/scripts/ocr_run.py` und `/Users/jurgen/AgentRuns/NewOrderDevon/scripts/ocr_pdfs.py` (byte-identisch zu den Session-Scratch-Originalen unter `/Users/jurgen/.claude/jobs/0fba4d0a/tmp/`, die jederzeit weggeräumt werden können). [Quelle: Skill, Abschnitt „The driver"; gbrain-Incident-Seite, Nachtrag 2026-07-31]

### 11.1 `ocr_run.py` — der Lauf, der die $0,58-Zahl erzeugte

- **Verifiziert, nicht angenommen:** sein Job-ID-Ledger `/Users/jurgen/.claude/jobs/0fba4d0a/tmp/ocr_jobs.json` hält exakt 593 Einträge (`{record_index: runpod_job_id}`); die Zieldatei `/Users/jurgen/AgentRuns/B219Buch/ocr_values.json` hält exakt 593 Records, jeder mit non-null `"ocr"`-Feld.
- **Fluss:** Rendert NICHTS selbst — liest pro Record einen vorhandenen `"cropped"`-PNG-Pfad (Dateien unter `/Users/jurgen/AgentRuns/B219Buch/cropped/`; woher die Crops stammen, hat die Suche vom 2026-07-31 nicht geklärt — unverifiziert). **Phase 1:** submittet jeden noch nicht submitteten Record als einen `/run`-Job (`file_b64`, `backend: vlm-auto-engine`, `transport: inline`, `formats: ["markdown"]`) und schreibt die Job-ID **nach jedem einzelnen Submit** ins Ledger (Restart kann nicht doppelt submitten); prüft alle 50 Submits die Balance via `runpodctl user`, Abbruch unter $0,15. **Phase 2:** pollt `GET /status/<job_id>` für alle offenen Jobs, schreibt fertiges Markdown ins `"ocr"`-Feld des Records, speichert die Zieldatei alle 20 s, Abbruch unter $0,05 Balance.
- **Aufruf:** schlicht `python3 ocr_run.py` — keine Argumente; alle Pfade und die Endpoint-ID sind Konstanten am Dateikopf; API-Key aus `~/.runpod/config.toml`.
- **Grenzen:** Submit-Retry 4× mit kurzem linearem Backoff, aber **kein** Retry-/Timeout-Konzept in der Abholung (Exception → stilles Überspringen → ewiger Neuversuch im 10-s-Takt, ohne Per-Job-Timeout). Streng sequentiell „erst alles submitten, dann abholen" — kollidiert bei großen Batches mit der 30-Minuten-Retention (beim 593er-Lauf ging es gut). Das vorgeschriebene `.bak` (`/Users/jurgen/AgentRuns/B219Buch/ocr_values.json.bak`, 593 Records, alle Felder null) wurde **manuell** vor dem Lauf angelegt — nichts im Skript erzeugt es.

### 11.2 `ocr_pdfs.py` — der nähere Keim für den 30k-Driver

- ~2 h später in derselben Session geschrieben (mtime 16:29 vs. 14:26). Kann, was `ocr_run.py` nicht kann: rendert selbst und merged pro Dokument.
- **Fluss:** rendert jede Quell-PDF via `pdftoppm -png -r 150 <pdf> <IMGS>/<stem>__` nach `/Users/jurgen/.claude/jobs/0fba4d0a/tmp/pageimgs/` (mit `.done__<stem>`-Markerdatei pro PDF, Restart überspringt Gerendertes); fährt dann ein **rollierendes Submissionsfenster** (`IN_FLIGHT = 120` gleichzeitig, laufend nachgefüllt — Submission und Abholung überlappen); schreibt jedes fertige Seiten-Markdown sofort als `<stem>__<seite>.md`; merged am Ende, wenn alle Seiten verbucht sind, je PDF eine `<stem>.md` in Dateinamens-(=Seiten-)Reihenfolge.
- **Sein Lauf:** gegen `/Users/jurgen/AgentRuns/LetsDoUni/B819_bis_Unit_3_Session_3/` — 39 der 44 PDFs; 5 große Geschäftsberichte (Santander, BP, zwei Rolls-Royce-Jahrgänge, Unilever) stehen hart codiert in einem `EXCLUDE`-Set (haben saubere digitale Textebene, Fallstudienmaterial). Ergebnis laut `/Users/jurgen/.claude/jobs/0fba4d0a/state.json`: „932/932 pages processed across 39 PDFs, $1.38 cost", „0 failures, 1434 formulas, 227 tables". Outputs (Fragmente + Merges) unter `/Users/jurgen/AgentRuns/LetsDoUni/B819_bis_Unit_3_Session_3/ocr/`, auf Platte bestätigt.
- **Grenzen — vor dem Skalieren auf 30k zwingend zu beheben:**
  1. **In-Flight-Job-IDs nur im Speicher** (ein nie persistierter `pending`-Dict). Crash mitten im Lauf = alle fliegenden Jobs unbekannt; und weil der Resume-Check „existiert das Output-`.md` schon?" lautet statt „gibt es schon einen offenen Job für diese Seite?", würde ein Restart jede zum Crash-Zeitpunkt laufende oder gerade fertige Seite **erneut submitten und erneut bezahlen**. Das verletzt direkt die Adopt-statt-Resubmit-Regel.
  2. **Kein Submit-Retry:** eine Exception markiert die Seite dauerhaft als failed.
  3. Alle Pfade, `DPI`, `IN_FLIGHT`, `MIN_BALANCE` und das `EXCLUDE`-Set sind hart codierte Konstanten — für einen anderen Korpus von Hand umzuschreiben.

### 11.3 Anforderungsliste an den 30k-Driver (abgeleitet aus den belegten Schwächen beider Skripte)

1. Job-Ledger mit Write-Through **vor** jedem Submit (aus `ocr_run.py` übernehmen) + Adopt-statt-Resubmit beim Restart (offene Job-ID zuerst weiterpollen).
2. Rollierendes Fenster mit überlappender Abholung (aus `ocr_pdfs.py` übernehmen) — Pflicht wegen der 30-Minuten-Retention.
3. Retry-Politik nach der Ausnahme-Landkarte des Procrastinate-Entwurfs (Abschnitt 12), inkl. Per-Job-Poll-Timeout.
4. Balance-Wache behalten (Submits gedrosselt prüfen, harte Abbruchschwellen), plus 402 als terminalen Kill-Switch behandeln.
5. `.bak`/Snapshot der Zielbestände automatisieren statt manuell.
6. Parametrisierung statt hart codierter Konstanten.
7. Für den PII-Korpus: EU-Datacenter-Restriktion setzen (Abschnitt 3 Punkt 7).
8. Vorher klären, was offen ist: Zielliste, Write-back, Driver-Entscheidung (Abschnitt 16).

---

## 12. Der entworfene (nicht gebaute) Daemon: Procrastinate auf PostgreSQL

[Quelle: gbrain `procrastinate-verdict`, 2026-07-03, aus den offiziellen Stable-Docs via Context7; Entscheidungskontext in `architecture-decision-qb-daemon`]

**Status: entworfen, NIE gebaut.** Der 593er-Lauf lief mit den Ad-hoc-Skripten aus Abschnitt 11. Ob der 30k-Lauf den Daemon braucht oder ein gehärteter einfacher Driver reicht, ist eine **offene Entscheidung** (Bead `jurgen-09v`, Voraussetzung 3).

- **Gewählt über:** (a) eine Bibliothek, die 30.000 awaits im Prozess hält; (b) einen handgerollten SQLite-Daemon; (c) pgqueuer (schlanker, aber Retries/Periodik müssten wieder handgerollt werden); (d) huey (disqualifiziert: kein Ack — verliert bei hartem Crash fliegende Arbeit).
- **Verifizierte Fähigkeiten (Docs-Ebene):** `run_worker_async(wait=False)` (Queue leeren, dann zurückkehren) bzw. `wait=True` + `shutdown_graceful_timeout` für Daemon-Betrieb; Job-Abort → `CancelledError`. `RetryStrategy` constant/linear/exponential (`total_wait = wait + linear_wait*attempts + exponential_wait**(attempts+1)`), `max_attempts`, `retry_exceptions`-Filter, voll eigene Strategie via `BaseRetryStrategy.get_retry_decision()` → `RetryDecision(retry_in/retry_at, priority, queue, lock)`. `queueing_lock` = Dedup zur Einreihzeit (derselbe Dokument-Hash kann nicht zweimal in die Queue). Periodische Tasks (cron-artig, `defer_periodic_job` exakt einmal pro Zeitstempel) tragen den Wächter (Balance-Poll via GraphQL, Worker-Listen-Verify, Idle-Check) ohne handgerollten Hintergrund-Lifecycle. LISTEN/NOTIFY eingebaut (sofortige Aufnahme; Poll-Fallback `fetch_job_polling_interval`); Worker-Heartbeats + Stalled-Worker-Pruning (Crash-Erkennung); `delete_jobs="never"` behält Queue-Historie; Task-/Worker-Middleware als Audit-Ansatzpunkt; Statusabfrage `get_job_status(job_id)`; Zustände todo/doing/succeeded/failed/cancelled/aborting/aborted.
- **Was es NICHT liefert (selbst bauen, beides trivial):** keine Ergebnis-Ablage (Ergebnisse in die eigene documents-Tabelle: hash, status, markdown, timestamps — vom Task geschrieben); kein Client-seitiges „auf Fertigstellung warten" (`to_md()` = defer + eigenes `NOTIFY doc_done, <hash>` aus dem Task + psycopg-async-LISTEN, Poll-Fallback; ~30 Zeilen).
- **Retry-Eigentum & Ausnahme-Landkarte (festgelegt):** Procrastinate ist der EINZIGE Retry-Eigentümer; RunPod-`/retry` unbenutzt; der Worker retryt nichts. Transport-Fehler / Worker-Tod / `TIMED_OUT` → retryfähig (exponentiell, ~3 Versuche); Validierungsfehler / zu groß → terminal; CUDA-OOM → genau ein Retry; HTTP 402 / Budget-Fehler → terminal + Kill-Switch. Plus Adopt-statt-Resubmit (gleicher Codepfad wie Crash-Recovery).
- **Weitere Designpunkte aus dem Entscheidungsdokument:** `to_md(path)`-Abstraktion (Batch-Nutzung awaitet nie — Fertigstellung ist eine SQL-Abfrage); Idle-Shutdown der GPU-Seite nach ~15 min ohne neue Jobs; Baseline „volle Zustandsverfolgung": per-Job-State-Objekte mit Write-Through-Persistenz (das ist eine von Jurgen gesetzte Grundlage, keine optionale Nettigkeit).
- **Benannte Kosten:** ein lokaler PostgreSQL-Dienst (das gbrain-Postgres-Muster existiert auf dieser Maschine bereits, Port 5437); ein Daemon, der zum Verarbeiten laufen muss; Wartungs-Flag: Procrastinate v3.9.0 (Juni 2026), MIT, stabil, README sucht „additional maintainers" (Bus-Faktor-Hinweis, kein Blocker).
- **Offen im Entwurf:** Bytes-in-DB vs. Pfad+Hash in der documents-Tabelle (Empfehlung: Pfad+Hash — 30k × Megabytes durch TOAST/WAL für lokale Dateien ist Verschwendung; Bytes nur, falls Produzenten remote werden); die konkrete Budget-Obergrenze für den Wächter.

---

## 13. Die Skill-Hilfsskripte

Unter `/Users/jurgen/CodingProjects/DevonThink886/.claude/skills/recreate-mineru-serverless/scripts/` (alle drei vollständig gelesen; sie stammen aus der LB-/Pod-Ära vom 2026-06-29 — Herkunftshinweis steht in ihren Docstrings):

- **`preflight.py`** — Schritt 0 vor JEDER GPU-Miete: Balance-Check via `runpodctl user` (kein roher API-Key, nichts in URLs); Exit 1 unter dem Minimum (Default $10). Weiterhin uneingeschränkt gültig und der richtige erste Aufruf jeder Automation.
- **`probe.py`** — Ein-Schuss-Probe `/ping`, `/health`, `/v1/models` mit Statuscode + Latenz; unterscheidet „gar kein Worker" / „Worker da, vLLM lädt/hängt" / „Port da, Route noch nicht". Auth als Bearer-HEADER; `User-Agent: Mozilla/5.0` nötig, weil RunPod Cloudflare-frontend ist (Default-UAs bekommen 403/1010). **Einordnung:** direkt sinnvoll für Pods und HTTP-erreichbare Endpoints; auf dem QB-Endpoint ist die Bereitschafts-Wahrheit stattdessen „Worker heartbeatet nach Warmup und Jobs wandern von `inQueue` nach `inProgress`" (eigene Einordnung, abgeleitet aus Abschnitt 8 — nicht separat gemessen).
- **`poll_ready.py`** — pollt `<base_url>/v1/models` bis HTTP 200 („RunPod ready/RUNNING heißt nur: Container geplant, nicht: vLLM bedient"); gleiche QB-Einordnung wie bei `probe.py`.

---

## 14. Symptom → Ursache

[Quelle: Skill, erweitert um die Failure-Logs unter `/Users/jurgen/CodingProjects/DevonThink886/ocr_comparison/`]

| Symptom | Ursache |
|---|---|
| `Failed to get Hub registry auth. Is this a template you have access to?` | Template zeigt auf das Publisher-Hub-Image. Es wurde `--hub-id` benutzt. Über die Console aus dem Fork neu bauen. |
| `This machine does not have the resources to deploy your pod` — sofort, auf drei verschiedenen GPU-Typen | Container-Disk zu klein fürs Image (5 GB vs. 27 GB). Keine GPU-Knappheit. |
| Worker `INITIALIZING` 20+ min, `startedAt` konstant | Normal beim ersten 27-GB-Pull. Warten. (Ob Serverless dabei Geld kostet: ungemessen — Abschnitt 9.7.) |
| Worker `INITIALIZING`, `startedAt` springt | Der Init-Timeout killt ihn. `RUNPOD_INIT_TIMEOUT` erhöhen. |
| Worker `running`, Jobs `inQueue`, `inProgress: 0` | Worker wärmt das VLM vor dem Heartbeat. Normal, 1–2 min. |
| Job `COMPLETED`, Markdown leer | Seite ansehen — vermutlich echt leer. |
| Drei Worker trotz `workersMax=1` | Alt-Kohorten einer früheren Endpoint-Version. Konfiguration evakuiert nicht. Selbst aufräumen, Liste zurücklesen. |
| HTTP 402 `Insufficient Balance` auf jedem Submit | Konto leer/unter Boden. Aufladen; Driver-Kill-Switch greift zu Recht. (Real: `_examples_minerutest/_failures.log`, 15/15 Dokumente.) |
| HTTP 401 `invalid api key` gegen eine `*.api.runpod.ai`-URL | Falscher/abgelaufener Key — ODER die URL zeigt auf einen gelöschten Endpoint (real: `_examples_retest/_failures.log` gegen den toten LB-Endpoint `sbdpcrzwlndrgb`). Erst prüfen, ob das Ziel noch existiert. |
| `endpoint-health` sagt `ready: N`, nichts wird bedient | Der Aggregat-Zähler lügt (stale/Standby-Worker). `list-endpoint-workers` ist die Wahrheit. |
| Kreuzberg-CLI „ok=True" von einem Backend, das gar nicht installiert ist | Stiller Fallback auf tesseract→paddle (siehe Kalibrierungs-Dokument, Abschnitt Bake-off) — betrifft das lokale Kreuzberg-Werkzeug, nicht den RunPod-Worker. |

---

## 15. Harte Regeln — jede einzelne ist ein bereits bezahlter Fehler

Kondensat aus Skill („HARD RULES") und gbrain `runpod-operational-discipline` (dort mit vollen Begründungen und Belegen):

1. **Brain-first vor jeder RunPod-Berührung** (Regel 0) — die Lektionen liegen im gbrain und wurden zweimal teuer neu bezahlt, weil niemand nachschlug.
2. **Kosten zuerst und laufend:** Balance vor jedem Create/Probe; Zähler läuft die GESAMTE Existenzzeit einer Ressource, auch durchs Debuggen; bezahlte Ressourcen nie eigenmächtig anlegen/vervielfachen (Anzahl × Stufe × Grund, mit Zustimmung); am Ende stoppen/nullstellen und den Zustand zurücklesen.
3. **Die volle Konfigurationsoberfläche gehört einem:** vor „fertig konfiguriert" jeden tragenden Knopf absichtlich setzen (hier konkret: `RUNPOD_INIT_TIMEOUT`, Container-Disk, `workersMax`, Idle/Execution-Timeout, `MINERU_MAX_CONCURRENCY`) statt jeden per Wand-Anprall zu entdecken.
4. **Nur gegen das echte Ding verifizieren:** Bereitschaft = der Dienst antwortet wirklich; `RUNNING`/`ready`-Felder sind nie Beleg; Mutationen durch Zurücklesen des Ist-Zustands bestätigen; nie eine Vermutung als Entscheidung erzählen — Unverifiziertes als unverifiziert aussprechen.
5. **Auf Fehler subtraktiv reagieren:** anhalten, Primärquelle lesen; nie durch Mehr-vom-Gleichen antworten (Re-Fire, mehr Concurrency, noch ein Worker, noch ein Restart). Ein abgelehnter/gescheiterter Schritt heißt anpassen, nicht wiederholen; ohne begründeten nächsten Zug: stoppen und fragen.
6. **Mutationen sind zustimmungspflichtig, und viele „Edits" sind Neuanlagen:** Pod-Env-PATCH = sofortige volle Container-Neuerstellung (verifiziert 2026-07-07); Serverless-Env lebt auf Templates; `workersStandby` war via `update-endpoint` gar nicht setzbar.
7. **Wissen, was einen Neustart überlebt:** Container-Dateisystem ist flüchtig, nur Volume/Template-Konfig ist dauerhaft; Fixes dorthin schreiben, wo sie überleben.
8. **Faire Timeouts geben:** zu kurze Client-Timeouts messen nicht nur falsch, sie VERURSACHEN den Fehler (Disconnect bricht die laufende Arbeit ab); volle Kaltstart-Fenster (~7–8 min, hier bis ~8,5 min) abwarten; lange Arbeit detached starten und read-only beobachten.
9. **Werkzeug komplett laden, direkte Kommandos fahren, dazwischen denken:** keine blinden Auto-Retry-Leitern; ein Poll-bis-Bedingung ist erlaubt, eine selbstzyklende Entscheidungs-Schleife nicht; die vom User gesetzte Taktung respektieren.
10. **Remote-Shell-Hygiene (Pods über SSH):** nie `pkill -f <muster>` (killt bei Pech die eigene SSH-Session — zweimal passiert, sah beide Male wie ein Netz-Hänger aus); per PID killen; RunPod-SSH-Proxy braucht `ssh -tt` + Heredoc; direkte Pod-Ports nach jedem Restart neu aus den Live-`runtime.ports` (GraphQL) lesen — das REST-Feld `portMappings` war einmal nachweislich stale.

Dazu die Batch-Regeln aus dem Skill: erst EINE Seite testen und mit eigenen Augen lesen; ganzen Batch submitten (Worker nie mitten im Batch idlen lassen); inkrementell speichern + `.bak`; Job-IDs auf Platte; leere Seite ist kein Fehler.

---

## 16. Der 30k-Lauf: Stand und offene Voraussetzungen

[Quelle: Beads `jurgen-09v` und `jurgen-egm`, Fassungen nach der Korrektur vom 2026-07-31]

**Ziel (Akzeptanzkriterien des Beads `jurgen-09v`):** Alle ~30.000 PDFs haben `<OUT>/<stem>/vlm/<stem>.md`; `_failures.log` leer oder per Re-Run geleert; Stichproben-Qualitätsprüfung inklusive eines Verifikations-Passes gegen halluzinierte Ziffern auf den schlechtesten Scans.

**Die Engine-Entscheidung ist gefallen:** MinerU-QB-Pipeline (dieser Endpoint), NICHT DEVONthinks eingebautes `ocr_record` — das ist höchstens ein Fallback für einfache Fälle, kein Ersatz auf dem beschädigten Bestand (Korrektur vom 2026-07-31 in `jurgen-egm`).

**Drei offene Voraussetzungen — der Lauf kann nicht „einfach gestartet" werden, solange sie offen sind:**
1. **Erkennung/Zielliste:** Welche der ~30k Records brauchen überhaupt OCR? Design existiert (klassenweise kalibrierte OCR-Defekt-Jagd; gbrain-Idea-Seiten, u. a. `wiki/originals/ideas/2026-06-25-ocr-defect-calibration-compartments-43f26d` plus die zugehörigen `wiki/personal/reflections/`-Seiten vom 24./25.06.2026), wurde aber nie Ende-zu-Ende kalibriert oder gegen den vollen Bestand gefahren. „Einfach alles OCRen" ist NICHT die kalibrierte Antwort — das ist eine echte offene Entscheidung. Warum Erkennung schwer ist, zeigt das Kalibrierungs-Dokument: die harten Fälle haben alle eine Textebene mit word count > 0.
2. **Write-back:** Wie kommt MinerUs Markdown aus der Job-Antwort als durchsuchbarer Text auf den DEVONthink-Record (gleicher Record als Plain Text? neuer Record? eingebettete PDF-Textebene?). Unentworfen. Die sdef-seitige Vorarbeit (was DEVONthinks AppleScript/JXA-Oberfläche für OCR-/Textebenen-Operationen hergibt und wo nicht) liegt in gbrain `projects/devonthink-klassifikation/ocr-integration-spec` (zusammengestellt 2026-07-31; dokumentiert vier Write-back-Optionen mit je einem Ein-Dokument-Experiment — die Entscheidung selbst ist offen). **Neu zu berücksichtigen:** der Aktenzeichen-Verlust im Markdown-Output (Kalibrierungs-Dokument, Abschnitt „MinerU-Kalibrierung") spricht direkt in diese Entscheidung hinein.
3. **Driver:** gehärteter einfacher Submit/Poll-Driver (Anforderungsliste in 11.3) oder der Procrastinate-Daemon (Abschnitt 12)? Unentschieden.

**Flankierend:** Worker-Quota-Erhöhung beantragen (Bead `jurgen-dmg`, siehe 9.5); EU-Datacenter-Pflicht, weil dieser Korpus deutsche Steuer-PII ist (Abschnitt 3 Punkt 7); Zeitschätzungs-Grundlagen in 9.5.

---

## 17. Nachprüf-Orte auf einen Blick

| Behauptung/Zustand | Wo nachprüfen |
|---|---|
| Endpoint existiert, Konfiguration | `runpodctl` / MCP `get-endpoint` (Endpoint `l79lebhinxaqjz`), Template `5wv9rko6zv` |
| Worker-Ist-Zustand | MCP `list-endpoint-workers` oder REST `GET /endpoints/{id}?includeWorkers=true` — NIE `endpoint-health` |
| Kontostand / Ausgabenrate live | `runpodctl user` (GraphQL: `clientBalance`, `currentSpendPerHr`, `spendLimit`) |
| Abrechnungs-Historie | MCP `get-billing` / REST `/billing/*` (nur Zeitreihen) |
| Image/Builds | RunPod-Console → Builds; Image-Tag im Template |
| Job-Zustand | `GET /status/<job_id>` — Achtung 404 nach Retention (30 min `/run`) |
| Worker-Logs | RunPod-Console (Stand 07/2026); MCP `stream-worker-logs` existiert inzwischen, ungeprüft |
| Modell-Aktualität | huggingface.co/opendatalab/MinerU2.5-Pro-2605-1.2B; pypi.org/project/mineru (NICHT MinerUs eigene Changelog-Seite — veraltet) |
| Driver-Artefakte des 593er-Laufs | `/Users/jurgen/AgentRuns/B219Buch/ocr_values.json` (+ `.bak`), Ledger `/Users/jurgen/.claude/jobs/0fba4d0a/tmp/ocr_jobs.json` |
| Skill (operatives Original) | `/Users/jurgen/CodingProjects/DevonThink886/.claude/skills/recreate-mineru-serverless/SKILL.md` |
