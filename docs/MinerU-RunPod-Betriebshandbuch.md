# MinerU-OCR auf RunPod — Betriebshandbuch für Automation und Lifecycle-Management

**Stand:** 05.08.2026.
**Zweck:** Dieses Dokument enthält alles, was jemand — Mensch oder Agent — mit null Vorkontext braucht, um die MinerU-OCR-Pipeline auf RunPod zu betreiben, zu automatisieren und ihren Lebenszyklus (Anlegen, Befüllen, Beobachten, Skalieren, Herunterfahren) zu steuern, ohne die verstreuten Quellartefakte noch einmal lesen zu müssen. Es ist bewusst maximal ausführlich; Kürze wird in diesem Projekt nie belohnt.
**Einstieg für Leser ohne Vorkontext:** [README.md](README.md) — Landkarte des Dokumentensets, die übertragbaren Lehren des Projekts und die Pflegeregeln dieser Dateien.
**Schwesterdokumente:** [MinerU-RunPod-Geschichte-und-Entscheidungen.md](MinerU-RunPod-Geschichte-und-Entscheidungen.md) (warum die Dinge so entschieden wurden, Chronik, Lehren), [MinerU-OCR-Kalibrierung-und-Backend-Vergleich.md](MinerU-OCR-Kalibrierung-und-Backend-Vergleich.md) (Schadensklassen, Engine-Verhalten je Klasse, Evaluations-Methodik) und [MinerU-DEVONthink-Integrationsstrecke.md](MinerU-DEVONthink-Integrationsstrecke.md) (die Stufen VOR und NACH der Engine: Erkennung, Export, Write-back, Statusführung).

**Wo dieses Dokument anfängt und aufhört:** Es behandelt Stufe 3 der Gesamtpipeline — Seiten rendern, Jobs submittieren, Markdown einsammeln, den GPU-Lebenszyklus steuern. Was DAVOR passiert (entscheiden, welche Dokumente überhaupt in die Queue gehören; sie aus DEVONthink herausbekommen) und was DANACH passiert (den Text zurück auf den Record bringen, Zustand führen, verifizieren), steht in der Integrationsstrecke. Diese Trennung ist Absicht: die Engine-Seite ist entschieden und produktionsbewiesen, die Integrationsstrecke ist überwiegend entworfen und offen.

**Quellen dieses Dokuments** (alle in der Session vom 31.07./01.08.2026 vollständig gelesen):
- Skill `recreate-mineru-serverless`: `/Users/jurgen/CodingProjects/DevonThink886/.claude/skills/recreate-mineru-serverless/SKILL.md` (neu geschrieben 2026-07-12, Driver-Abschnitt ergänzt 2026-07-31) samt `scripts/poll_ready.py`, `scripts/preflight.py`, `scripts/probe.py`.
- Die vier weiteren Skills desselben Verzeichnisses, vollständig gelesen 2026-08-02: `recreate-mineru-pod` (Pod-Fallback, EV-Regel, Create-fresh-Lehre, EU-DC-Liste), `runpodctl` (allgemeiner CLI-Skill v2.3 — der MinerU-Skill nennt ihn als Pflichtlektüre), `companion-clis` (hf/gh/docker/aws samt Docker- und S3-Regeln), `flash` (SDK; GPU-Gruppen, Auto-Switching-Grenze, Endpoint-Parameter).
- gbrain-Seiten: `projects/mineru-ocr/architecture-decision-qb-daemon` (Entscheidungsdokument, settled 2026-07-03, validiert 2026-07-12), `projects/mineru-ocr/mineru-runpod-repo-dossier` (Volllektüre des Worker-Repos, 2026-07-03), `projects/mineru-ocr/runpod-platform-gotchas` (systematischer Docs-Sweep von docs.runpod.io, 2026-07-03), `projects/mineru-ocr/procrastinate-verdict` (2026-07-03), `projects/mineru-ocr/2026-07-12-lb-relapse-incident` (verifizierte Plattform-Fakten), `projects/mineru-ocr/eur30-lesson-lb-vs-qb` (Kosten-Fakten samt Retraction), `projects/mineru-ocr/lb-era-operational-record` (historische Messbasis), `wiki/personal/patterns/runpod-operational-discipline` (die 10 Betriebsregeln).
- Beads: `jurgen-09v` (30k-Lauf, Fassung nach der Korrektur vom 2026-07-31), `jurgen-egm` (OCR-Voraussetzung der Klassifikation), `jurgen-dmg` (Quota-Erhöhung).
- Die beiden Driver-Skripte: `/Users/jurgen/AgentRuns/NewOrderDevon/scripts/ocr_run.py` und `/Users/jurgen/AgentRuns/NewOrderDevon/scripts/ocr_pdfs.py` (byte-identische Dauerkopien der Originale aus `/Users/jurgen/.claude/jobs/0fba4d0a/tmp/`, kopiert 2026-07-31, per `diff` verifiziert laut Skill).
- gbrain `projects/mineru-ocr/born-digital-backend-benchmark` (die born-digital-Messung, der Nachrechen-Test, die Scoping-Lektion, die pypdf-Messwerte — vollständig gelesen 01.08.2026).
- **Primärquellen, erstmals am 05.08.2026 selbst gelesen** (vorher stützten sich diese Dokumente an mehreren Stellen auf Zitate aus zweiter Hand — mit einem belegten Fehlgriff, siehe Geschichte 4.3 Punkt 4): RunPods eigene Doku `docs.runpod.io/serverless/workers/github-integration`; die Doku-Seiten des Worker-Repos `sergeyshmakov/mineru-runpod` unter `docs/src/content/docs/` — `getting-started/deploy`, `reference/api`, `guides/output-modes`, `guides/input-formats`, `guides/scaling`, `guides/concurrency`, `guides/troubleshooting` — sowie der Blog-Eintrag `blog/2026-06-03-runpod-machine-does-not-have-the-resources-fix`; und die beiden Driver-Skripte im Quelltext.
- **Noch nicht gelesen und deshalb als ungeprüft zu behandeln:** die Worker-Doku-Seiten `guides/choosing-gpu`, `guides/network-volumes`, `guides/observability`, `getting-started/overview`, `getting-started/clients`, `getting-started/migrate-from-mineru-api`, die übrigen sieben Blog-Einträge, `.runpod/hub.json`, `.runpod/tests.json`, `handler.py`, `worker/`, `deploy.py`/`destroy.py` im Volltext, das Repo-`CHANGELOG.md`; RunPods Hub-Publishing-Guide und die Endpoint-Konfigurationsseite.

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
| GPU-Wahl | `ADA_24` (RTX 4090) primär — niedrigster $/Seite trotz höherem $/h; als Stock-Reserve `AMPERE_24`/`AMPERE_48`; **nie `ADA_48_PRO`** (RunPod mischt dort Blackwell-Karten ein, denen Kernel fehlen → VLM-Backend crasht). **Der Blackwell-Teil dieser Begründung ist am 05.08.2026 am Objekt erschüttert — siehe die Widerlegung in 5a.5** | Repo-Dossier + Skill; Widerlegung: `artifacts/2026-08-05-doku-verifikationstest/` |
| MinerU-Modell | `MinerU2.5-Pro-2605-1.2B` (2026-05-21) — es gibt **keinen Nachfolger**; „2.5" ist die Modell-Linie, „3.4.x" die Python-Paket-Linie; MinerUs eigene Changelog-Seite ist veraltet, maßgeblich sind PyPI + HuggingFace | huggingface.co/opendatalab, pypi.org/project/mineru |
| Lokale Python-Umgebung | **Präzisiert 05.08.2026:** Jurgens Arbeitsumgebung ist die **Miniconda-Base** (`/opt/homebrew/Caskroom/miniconda/base/bin/python3`, Python 3.12) — er benutzt keine venvs; dort liegen `httpx`, `pypdfium2`, `rocksdict` bereits. Das früher als „kanonisch, das EINZIGE" geführte Venv `/Users/jurgen/CodingProjects/DevonThink886/.venv` (Python 3.12, `mineru[core]`) existiert weiterhin und ist ein **Agenten-Artefakt** früherer Sessions — nützlich, weil dort `mineru` 3.4.0 samt Quelltext liegt (für Referenz-Zwecke), aber nicht Jurgens Arbeitsweise und keine Vorgabe für neue Werkzeuge. `~/.venvs/mineru-client` bleibt tot. Homebrew-Python (3.14) ist PEP-668-gesperrt und nimmt keine pip-Installationen an | Dateisystem; `pip3 list` je Umgebung |
| Seiten-Renderer | `pdftoppm` aus poppler (`brew install poppler`) | `which pdftoppm` |
| API-Key | `RUNPOD_API_KEY`, liegt in `~/.runpod/config.toml` (Feld `apikey`) | Datei |

**Warnung zur Endpoint-Lebensdauer:** Endpoint und Template bei Nichtgebrauch **stehen lassen**. Bei `workersMin=0` ohne Jobs kostet der Endpoint $0/h (wiederholt beobachtet); Löschen erzwingt beim nächsten Mal einen frischen 27-GB-Pull. **Aber:** RunPod skaliert unbenutzte Endpoints selbst herunter — 3 Tage ohne Requests → `workersMax=2` (plus E-Mail), 7 Tage → `workersMax=0`, und es gibt **keine automatische Wiederherstellung**. Jede Automation muss beim Adoptieren eines bestehenden Endpoints die Konfiguration neu durchsetzen statt sie vorauszusetzen. [Quelle: gbrain `runpod-platform-gotchas`, Docs-Sweep 2026-07-03]

**Gegenbeobachtung 05.08.2026 (Doku-Verifikationstest):** Der Endpoint `l79lebhinxaqjz` stand zu diesem Zeitpunkt **24 Tage** ohne einen einzigen Request (letzter Lauf 12.07.2026), und `workersMax` stand unverändert auf **10** — der beschriebene Auto-Downscale war **nicht** eingetreten. Die 3/7-Tage-Regel wird nicht gestrichen (sie stammt aus RunPods eigener Doku), aber sie ist damit als **nicht zuverlässig vorhersagbares Verhalten** markiert: Sie kann eintreten, muss aber nicht. Die praktische Konsequenz bleibt dieselbe und wird eher stärker — der Adoptions-Schritt (Konfiguration lesen und neu durchsetzen, 9.8 Schritt 3) ist Pflicht, gerade WEIL man nicht weiß, ob die Plattform eingegriffen hat. Nachprüf-Ort: `GET https://rest.runpod.io/v1/endpoints/l79lebhinxaqjz` → `workersMax`.

---

## 2a. Arbeitsumgebung herstellen (der Schritt vor allen anderen)

Dieser Abschnitt existiert, weil die Dokumentation lange voraussetzte, dass die Umgebung schon steht — man konnte mit ihr weiterarbeiten, aber nicht anfangen. Was gebraucht wird, in der Reihenfolge, in der man es braucht:

1. **Zugangsschlüssel.** Der RunPod-API-Key liegt in `~/.runpod/config.toml` unter dem Feld `apikey`; die Driver-Skripte lesen ihn von dort, die REST-Aufrufe erwarten ihn als `Authorization: Bearer`-Header. **Er gehört nie in eine URL** (die Hilfsskripte des Skills setzen ihn ausdrücklich nur als Header). Fehlt die Datei oder ist der Key abgelaufen, ist das Symptom ein HTTP 401 — das aber genauso aussieht, wenn das Ziel gar nicht mehr existiert (Symptomtabelle, Abschnitt 14).
2. **`runpodctl`.** Installation nach RunPods eigener Anleitung; der allgemeine `runpodctl`-Skill ist die Referenz für Kommandos und Entscheidungsregeln, **die Live-Hilfe (`runpodctl --help`, `runpodctl <befehl> --help`) ist autoritativ für Flags und Syntax** — dieses Dokument beschreibt Abläufe, nicht die jeweils aktuelle Flag-Liste. `runpodctl doctor` diagnostiziert Erstinstallation, API-Key und SSH. Erfolgsprobe: `runpodctl user` liefert JSON mit `clientBalance`.
3. **`poppler`** für den Seiten-Renderer: `brew install poppler`, Erfolgsprobe `which pdftoppm`. Ohne ihn gibt es keinen Weg von einer PDF zu Job-Bildern, der nicht gegen das pypdf-Verbot aus Abschnitt 7 verstößt.
4. **Die lokale Python-Umgebung** — präzisiert 05.08.2026: Jurgen benutzt **keine venvs**; seine Arbeitsumgebung ist die Miniconda-Base (`/opt/homebrew/Caskroom/miniconda/base/bin/python3`, Python 3.12, `httpx`/`pypdfium2`/`rocksdict` vorhanden). Neue Werkzeuge zielen dorthin. Das Venv `/Users/jurgen/CodingProjects/DevonThink886/.venv` (mit `mineru` 3.4.0) ist ein Agenten-Artefakt früherer Sessions — als MinerU-Quelltext-Referenz nützlich, keine Vorgabe. `~/.venvs/mineru-client` bleibt tot. Homebrew-Python (3.14) ist PEP-668-gesperrt.
5. **Werkzeugflächen der Agenten-Umgebung.** Die RunPod-MCP-Werkzeuge (`mcp__runpod__*`) und die DEVONthink-MCP-Werkzeuge müssen in der Session verfügbar sein. **Ihre Flächen ändern sich zwischen Versionen** — deshalb steht bei jeder Aussage über sie in diesen Dokumenten ein Datum, und deshalb gilt die Disziplin, das Toolset vollständig zu laden statt stückweise (Regel 9, Abschnitt 15). Fehlt ein Werkzeugsatz, der vorhanden sein soll, ist das ein Grund anzuhalten und es zu sagen — nicht, darum herumzuarbeiten.
6. **Die Wissensquellen.** Vor der ersten Berührung mit RunPod die Projekt-Seiten im Wissensspeicher lesen (Regel 0). Ohne diesen Schritt sind die teuersten Lektionen dieses Projekts zweimal bezahlt worden, beide Male nicht wegen fehlender, sondern wegen unkonsultierter Dokumentation.

**Was NICHT gebraucht wird:** das Flash-SDK (nur für die abgelöste LB-Architektur nötig), ein Network-Volume (Abschnitt 3, Punkt 6), ein Docker-Login (das Image liegt in RunPods eigener Registry im eigenen Konto).

---

## 3. Architektur und ihre Begründung

1. **Queue-based statt Load-Balancing.** Die Queue hält Jobs durch Kaltstarts (TTL default 24 h). Auf dem LB-Endpoint wurden Requests nach 2 Minuten ohne Worker verworfen (`400 timed out waiting for worker`) — ein 15-Dokumente-Burst schlug damit zu 100 % fehl. Diese Fehlerklasse existiert auf QB nicht; deshalb braucht QB auch kein Pre-Warming. [Quelle: gbrain `eur30-lesson-lb-vs-qb`, `architecture-decision-qb-daemon`]
2. **Ganze Pipeline im Worker** (`vlm-auto-engine` = In-Process-vLLM-Engine). Der Worker macht Fitness-Checks → eifriges Modell-Warmup mit echtem Parse → **erst dann** Heartbeat. Ein Worker kann keine Jobs bekommen, bevor die Engine wirklich bedient — das „ready lügt"-Problem ist an der Quelle gelöst. [Quelle: gbrain `mineru-runpod-repo-dossier`]
3. **Ein Job = eine Seite** (beim Buch-Lauf) bzw. ein Dokument. Macht Verlust unmöglich sichtbar (fehlende Seite = fehlender Job) und hält Payloads klein.
4. **Lokale Seite = Procrastinate-Daemon auf PostgreSQL** (entworfen, **noch nicht gebaut** — Abschnitt 12). Der 2026-07-12-Lauf nutzte stattdessen einfache Submit/Poll-Skripte (Abschnitt 11).
5. **Einziger Retry-Eigentümer ist der lokale Treiber/Daemon.** RunPods `/retry` wird bewusst nicht benutzt (Doppel-Eigentümerschaft), das Worker-Repo retryt nichts (fail-loud, per grep verifiziert). Dazu die **Adopt-statt-Resubmit-Regel**: Ein Task-Versuch prüft zuerst, ob für diese Einheit schon eine lebende RunPod-Job-ID vorliegt, und pollt die weiter, statt neu zu submitten — doppelte GPU-Ausgaben werden strukturell unmöglich. [Quelle: gbrain `architecture-decision-qb-daemon`, `procrastinate-verdict`]
6a. **Die Volume-Entscheidung deckt nur EINEN der drei Anwendungsfälle ab — der für den 30k-Lauf wichtigste wurde nie gestellt.** Die Worker-Doku trennt drei Zwecke eines Network-Volumes: (1) **Eingabe-Korpus vorlagern**, (2) Modellgewichte cachen, (3) Ausgaben zwischen Jobs persistieren. Unsere Entscheidung „kein Volume" (Punkt 6) begründet ausschließlich **(2)** — und dort zu Recht. **(1) ist eine völlig andere Frage und für 30.000 Dokumente die naheliegendste:** Ein Volume einmal befüllen und die Jobs per `volume_path` darauf zeigen lassen bedeutet **kein Base64, keine 10-MB-Gateway-Grenze, kein Upload je Job**. Kosten rund $0,07/GB-Monat. Gegenargumente, die vor einer Entscheidung zu klären sind: Volumes sind **regional** — der Endpoint muss auf die Region des Volumes gepinnt werden, sonst landen Worker in Regionen ohne Volume und scheitern mit `volume_path not found`; das kollidiert mit der EU-Datacenter-Pflicht nur scheinbar (EU-Region wählen), verkleinert aber den Pool und damit die Verfügbarkeit. Und der Bestand müsste zuerst aus DEVONthink heraus und aufs Volume — das ist die Export-Stufe der Integrationsstrecke. **Offen, nicht entschieden.**

6. **Kein Network-Volume FÜR MODELLGEWICHTE — mit einem offenen Punkt seit 02.08.2026.** Drei unabhängige, verifizierte Gründe: (a) Ein Volume kann kein Container-Image beherbergen — das Image ist ein Docker-Layer-Pull auf den Host, **bevor** ein Container existiert; das Volume mountet erst danach hinein. (b) Die Modellgewichte werden zur Laufzeit gar nicht geladen — beide Modelle sind ins Image gebacken (`snapshot_download` beim Build) und `HF_HUB_OFFLINE=1` verbietet Downloads; das Repo-eigene Volume-Guide sagt wörtlich: „a volume for model weights duplicates that work". (c) Die Gewichte sind nur ~4 GB der ~27 GB — der Rest ist die CUDA/vLLM-Basis, die sowieso gepullt werden muss. Warum der Autor backt statt cacht: RunPods Cached-Models-Feature kann **ein** Modell pro Endpoint, MinerU braucht **zwei** (VLM + PDF-Extract-Kit).

   **Offener Punkt (02.08.2026):** Der `runpodctl`-Skill beschreibt ein Flag `--model-reference`, das ein HuggingFace-Modell auf dem Host in den Standard-HF-Cache legt (`/runpod-volume/huggingface-cache/hub/`), sodass der Worker es direkt lädt — „no need to bake the model into the image or attach a network volume" —, und sagt ausdrücklich: **„The flag is repeatable, so you can attach multiple models."** Das widerspricht der Ein-Modell-Begrenzung, auf der die Bake-Entscheidung des Repo-Autors beruht. Bedingungen laut Skill: nur mit `--compute-type GPU`, erfordert runpodctl v2.4.0+. **Ungeprüft**, ob das für unseren Fall wirklich zwei Modelle abdeckt und ob es die Pull-Zeit tatsächlich verkürzt (die ~27 GB sind zu ~23 GB CUDA/vLLM-Basis, nur ~4 GB Modelle — selbst ein perfekter Modell-Cache spart also höchstens ~15 %). Solange das nicht gemessen ist, bleibt die Bake-Architektur unverändert gültig; wer sie anfassen will, misst zuerst. [Quelle: gbrain `2026-07-12-lb-relapse-incident`, Abschnitt „RESOLVED"; Skill `runpodctl` für das Flag]
7. **EU-Platzierung ist eine Pro-Job-Entscheidung, keine Endpoint-Konstante.** GDPR erzwingt EU-Datacenter **für den deutschen Steuer-PII-Korpus** (also für den 30k-Lauf). Der 593-Seiten-Buchlauf hatte keine PII und lief auf US-Datacentern auf ausdrückliche Anweisung von Jurgen. Regel: die Datacenter-Beschränkung aus den Daten ableiten, pro Job/Lauf.

   **Die EU-Datacenter-Liste ist zwischen zwei Quellen widersprüchlich — vor Gebrauch live prüfen.** Der Docs-Sweep (03.07.2026) nennt **9**: EU-RO-1, EU-SE-1, EU-CZ-1, EU-NL-1, EU-FR-1, EUR-NO-1, EUR-IS-1, EUR-IS-2, EUR-IS-3. Der Pod-Skill nennt **13**: EU-CZ-1, EU-DK-1, EU-NL-1, EU-RO-1, EU-SE-1, EU-SE-2, EUR-IS-1, EUR-IS-2, EUR-IS-3, EUR-IS-4, EUR-IS-5, EUR-NO-1, EUR-NO-2. Die Differenz ist beidseitig: EU-FR-1 steht nur in der ersten Liste, EU-DK-1/EU-SE-2/EUR-IS-4/EUR-IS-5/EUR-NO-2 nur in der zweiten. Beide Listen sind Momentaufnahmen unterschiedlicher Tage; welche stimmt, entscheidet **`runpodctl datacenter list`** zum Zeitpunkt des Laufs — nicht dieses Dokument. Für S3-Zugriff auf Network-Volumes gilt ohnehin eine kleinere Teilmenge (CZ-1, RO-1, IS-1, NO-1).

   **Falle bei der Umsetzung:** `runpodctl pod create --data-center-ids` honoriert **nur die ERSTE ID** — eine Liste dort ist keine Auswahl, sondern eine stille Festlegung auf den ersten Eintrag. [Quellen: gbrain `architecture-decision-qb-daemon`, `runpod-platform-gotchas`; Skill `recreate-mineru-pod`; Skill `companion-clis` für die S3-Teilmenge]

---

## 4. Deploy von Null (nur nötig, wenn der Endpoint weg ist)

**Vorher: die Deploy-Seite des Worker-Repos lesen.** Quelldatei `docs/src/content/docs/getting-started/deploy.mdx` in `sergeyshmakov/mineru-runpod`, gerendert unter `https://sergeyshmakov.github.io/mineru-runpod/getting-started/deploy/`. (Der Skill und eine frühere Fassung dieses Abschnitts nannten den Pfad `docs/getting-started/deploy.mdx` — **den gibt es nicht**; das `docs/`-Verzeichnis ist eine Astro-Site, der Inhalt liegt unter `docs/src/content/docs/`. Am 05.08.2026 am Repo geprüft.) Diese Seite nicht gelesen zu haben, kostete am 12.07.2026 eine Stunde und echtes Geld; die Regel „gibt es Doku, wird sie erforscht und gelesen — als Erstes" ist an diesem Tag daraus entstanden.

**⚠️ Und weil dieser Verweis verbindlich ist, muss dabeistehen, was von jener Seite für UNS nicht gilt — sonst schickt die Pflichtlektüre den Leser in eine Anweisung, die dieses Handbuch verbietet.** Die Seite nennt drei Wege (Stand 05.08.2026, im Wortlaut geprüft):

| Upstream-Option | Für uns |
|---|---|
| **A — Deploy aus dem RunPod Hub** („easiest", ausdrücklich als empfohlener Weg markiert: *„This is the recommended path if your goal is parsing PDFs, not customising the worker."*) | **Nicht benutzen** — siehe das `--hub-id`-Verbot unten und den benannten Widerspruch dazu. |
| **B — Fork und Auto-Build** (Import Git Repository → Template → Endpoint) | **Das ist unser Weg.** Deckt sich mit den Schritten 1–5 oben. |
| **C — Bring your own image** (selbst bauen und pushen, dann `deploy.py --image …`) | Nicht in Gebrauch; relevant nur, falls je ein eigenes Image gebaut wird (Docker-Regeln am Ende dieses Abschnitts). |

**Zwei Aussagen jener Seite widersprachen diesem Handbuch. Die zweite ist seit dem 05.08.2026 aufgelöst — gegen uns; die erste bleibt offen.** Beide stehen hier vollständig, weil eine ausgeschriebene Fehlannahme mehr lehrt als eine stillschweigend korrigierte (Register: Geschichte, Abschnitt 4.3):

1. **Was der Hub-Weg technisch tut.** Die Seite sagt wörtlich: *„The template is the source, not a prebuilt image: there is no public image to pull, and RunPod builds one into your own account's registry the first time you deploy"* — und schreibt unsere Fehlermeldung einer **anderen** Ursache zu: *„If you create an endpoint by hand and type an image name instead, the pull fails with an access error."* Unser Befund vom 12.07.2026 lautete dagegen, `--hub-id` erzeuge ein Template auf das **private Image des Publishers** mit `containerRegistryAuthId: null`. **Was gesichert ist: die Beobachtung** — siebzehn Fehlversuche der Registry-Auth, vierzig Minuten, null Bytes bewegt, real bezahlt. **Was jetzt bestritten ist: die Erklärung dafür.** Es ist gut möglich, dass nicht der Hub-Weg selbst, sondern der CLI-Pfad `runpodctl serverless create --hub-id` die Ursache war und die Console-Variante desselben Wegs funktioniert hätte. Damit stehen sich drei Quellen gegenüber: unser Vorfall, der allgemeine `runpodctl`-Skill (beschreibt `--hub-id` als funktionierend) und jetzt die Doku des Worker-Repos. **Bis das jemand am Objekt entscheidet, bleibt die Console-Fork-Route (Option B) verbindlich** — sie ist bewiesen, kostet nichts extra und macht die Frage praktisch gegenstandslos.
2. **Was einen Rebuild auslöst — die Upstream-Seite ist hier NICHT verlässlich.** Sie sagt: *„Subsequent pushes to `main` on your fork rebuild the image automatically."* **Das widerspricht RunPods eigener Doku**, die für die Serverless-GitHub-Integration ausdrücklich festhält, Änderungen würden *nicht* automatisch ausgerollt und ein **Release** sei der Auslöser (Primärquelle am 05.08.2026 gelesen, Zitat unter „Verbote und Fallen"). Es widerspricht außerdem unserer eigenen Erfahrung vom 12.07.2026. **Hier gilt daher RunPods Doku plus unsere Messung gegen die Upstream-Seite — zwei unabhängige Quellen gegen eine.** Denkbare Erklärungen, keine geprüft: Die Upstream-Seite ist vom Juni und könnte veraltet sein, oder ihr Autor hat den Hub-Weg mit dem Serverless-Weg verwechselt. **Konsequenz für die Pflichtlektüre oben: Diese eine Aussage jener Seite nicht übernehmen.**

**Was die Seite außerdem enthält und in diesem Handbuch bisher fehlte: das Repo bringt Deploy-as-Code mit.** Im Wurzelverzeichnis liegen `deploy.py` und `destroy.py`; `deploy.py` ist ein argparse-Skript mit Gruppen für Image-Quelle, Endpoint-Identität, Skalierung, Hardware und Job-Limits, dessen Flags laut Seite eins zu eins den Dashboard-Einstellungen entsprechen (`pip install -e .[deploy]`, Konfiguration über `.env`). **Das schränkt unsere Aussage „Console-only" ein:** Console-only ist das **Anstoßen eines Image-Builds**. Das **Anlegen des Endpoints aus einem fertigen Template** ist sehr wohl skriptbar — und zwar reproduzierbar, was für einen wiederholbaren Aufbau interessanter ist als Klicken. Ungeprüft, ob `deploy.py` mit unserem Template und unseren Anforderungen (EU-Datacenter, `RUNPOD_INIT_TIMEOUT`, Container-Disk 50 GB) umgehen kann — die Seite erwähnt Container-Disk gar nicht.

**Und die Voreinstellungen der Seite sind nicht unsere:** Sie nennt `idle_timeout = 10 s` (wir: 60 s, wegen der Idle-Falle mitten im Batch) und `workers_max = 3` als Default — genau der Wert, vor dem dieses Handbuch als stille Dreifach-Ausgabe warnt. Wer die Upstream-Tabelle abschreibt, übernimmt beides.

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
| FlashBoot | an | Schnellere Warm-Restarts (Mechanik in Abschnitt 8). **Live-Befund 05.08.2026: der stehende Endpoint meldet per REST `flashboot: false`** — entweder wurde die Empfehlung beim Anlegen nie umgesetzt, oder das REST-Feld bildet die Console-Einstellung nicht ab; nicht geklärt. Nachprüf-Ort: Console bzw. `GET /v1/endpoints/l79lebhinxaqjz`. Der Test lief trotzdem mit ~3,5 min Warm-Restart durch. |
| GPU | `ADA_24`, optional + `AMPERE_24`/`AMPERE_48` | 4090 = niedrigster $/Seite; nie `ADA_48_PRO`. |
| Env `RUNPOD_INIT_TIMEOUT` | **3600** | Default ~7 min. Ein 27-GB-Pull dauert länger; feuert der Timeout, wird der Worker als unhealthy gekillt und pullt erneut — die Churn-Schleife. Ausdrücklich NICHT den alten Wert `1200` kopieren: 20 Minuten waren kürzer als der real gemessene Pull. |
| Env `MINERU_MAX_CONCURRENCY` | 1 | Überall sicher (vLLM auf kleineren GPUs nicht nebenläufig ansteuern). |

5. **Builds** beobachten: Pending → Building → Uploading → Testing → Completed. Ein fehlgeschlagener **Testing**-Schritt heißt NICHT, dass das Image kaputt ist — es ist zu dem Zeitpunkt bereits in die Registry gepusht; meist konnte nur der Test-Pod nicht platziert werden (Container-Disk-Falle oben).

**Verbote und Fallen:**
- **NIE `runpodctl serverless create --hub-id`.** Das erzeugt ein Template, das auf das **private Hub-Image des Publishers** zeigt, mit `containerRegistryAuthId: null`. Der Worker loopt dann ewig auf `Failed to get Hub registry auth. Is this a template you have access to?` (17-mal im Log, 40 Minuten, null Bytes bewegt, am 2026-07-12 real bezahlt). Die damalige Schlussfolgerung lautete: Es gebe keinen Weg, Credentials für den Hub-Build eines fremden Accounts zu bekommen. Eine frühere Fassung der Incident-Seite behauptete kurz, der Hub-Weg funktioniere — das wurde zurückgenommen.

  **Benannter Quellen-Widerspruch, seit 05.08.2026 mit DREI Beteiligten und deshalb ernster als zuvor:** (a) unser Vorfall vom 12.07.2026; (b) der generische `runpodctl`-Skill, der `--hub-id` als funktionierenden Weg beschreibt („resolves the hub listing, extracts the build image and config, creates an inline template, and deploys"); (c) **die Deploy-Seite des Worker-Repos selbst**, die dem Hub-Weg genau das Gegenteil unserer Diagnose zuschreibt — das Template sei die *Quelle* und RunPod baue das Image *in den eigenen Account des Deployenden*, während unsere Fehlermeldung dort einem anderen Vorgehen zugeordnet wird (Endpoint von Hand anlegen und einen Image-Namen eintippen). **Gesichert bleibt die Beobachtung, bestritten ist die Erklärung.** Die sparsamste Lesart, die alle drei verträgt: Der Hub-Mechanismus als solcher funktioniert, und was bei uns scheiterte, war der CLI-Pfad `--hub-id` — nicht der Hub. Bewiesen ist das nicht. **Für dieses Projekt bleibt es dabei: nicht benutzen**, weil die Fork-Route über die Console bewiesen ist und nichts kostet. Wer den Weg je wieder erwägt, klärt zuerst am Objekt, wo das Image des Listings tatsächlich landet — das ist ein Ein-Deploy-Experiment, kein Aktenstudium.
- **Image-Updates laufen über ein GitHub-RELEASE. Ein Push allein rollt nichts aus.** Primärquelle, am 05.08.2026 selbst gelesen — `docs.runpod.io/serverless/workers/github-integration`, Abschnitt „Update your endpoint", wörtlich: *„When you make changes to your GitHub repository, they won't automatically be pushed to your endpoint. To trigger an update for the workers on your endpoint, create a new release for the GitHub repository."* Bestätigt im Rollback-Abschnitt derselben Seite: *„Your endpoint remains on the rolled-back version until you deploy a new release from GitHub."* Also: pushen, dann Release anlegen (`gh release create <tag> --title … --notes …`). Keine Versionsnummer erfinden, um einen Rebuild zu erzwingen — ist das Image schon gebaut und gepusht, braucht man keinen.

  **Achtung, die Worker-Doku behauptet hier das Gegenteil** (*„Subsequent pushes to `main` on your fork rebuild the image automatically"*). Das deckt sich weder mit RunPods Doku noch mit der Erfahrung vom 12.07.2026 — zwei unabhängige Quellen gegen eine. **Diese eine Aussage der Worker-Doku nicht übernehmen.**

**Harte Grenzen der GitHub-Integration — aus RunPods eigener Doku, am 05.08.2026 gelesen, und in unseren Dokumenten bisher nirgends verzeichnet.** Mehrere davon sind für ein 27-GB-Image mit zwei eingebackenen Modellen unmittelbar relevant:

| Grenze | Wert | Was das für uns heißt |
|---|---|---|
| **Docker-Build-Schritt** | **30 Minuten** | Das ist die scharfe Grenze, nicht die große. Unser Dockerfile lädt beim Bauen **beide Modelle** herunter (`snapshot_download`) — ein Rebuild kann daran auflaufen. RunPods eigener Rat für genau diesen Fall: das Image lokal vorbauen und in eine Registry pushen. Fehlermeldung: `Build exceeded maximum time limit of 1800 seconds (30.0 minutes)`. |
| **Gesamtes Build-Fenster** | 160 Minuten (2,5 h) | Klonen, Bauen, Hochladen und Testen zusammen. |
| **Maximale Image-Größe** | **80 GB** | Unsere ~27 GB liegen sicher darunter; die Grenze zu kennen ist trotzdem nötig, falls je Modelle dazukommen. |
| **Private Base-Images** | nicht unterstützt | Wesentliche Bestandteile gehören ins eigene Dockerfile. |
| **GPU beim Bauen** | nicht verfügbar | Builds, die zur Bauzeit eine GPU brauchen (etwa GPU-kompilierte Bibliotheken), gehen über diesen Weg nicht. |
| **Plattform-Exklusivität** | Images aus RunPods Builder laufen **nur** auf RunPod | Sie lassen sich **nicht** anderswo ziehen oder ausführen. Damit ist ein Off-Machine-Backup dieses Images unmöglich — anders als beim alten LB-Image, das auf einer öffentlichen Registry lag. Was gesichert werden muss, ist folglich **der Fork**, nicht das Artefakt. |
| **GitHub-Verbindungen** | genau **eine** pro RunPod-Konto | Nicht im Team teilbar; Mitarbeit erfordert eigene RunPod-Konten. |

**Zwei Fähigkeiten derselben Quelle, die wir noch nicht kannten und die operativ etwas wert sind:**

- **Rollback auf einen früheren Build, ohne auf ein neues Release zu warten.** Endpoint → Reiter **Builds** → Drei-Punkte-Menü am gewünschten Build → **Rollback** → bestätigen. Der Endpoint schaltet sofort auf das Docker-Image jenes Builds um, ein Banner zeigt den zurückgerollten Zustand an, und er bleibt darauf, **bis ein neues Release** ihn ablöst. Das ist der schnellste Ausweg, wenn ein frisch ausgerollter Build den Lauf blockiert — schneller als jeder Fix im Code.
- **Mehrere Umgebungen aus einem Repo:** ein Endpoint auf `main`, ein zweiter auf einem Entwicklungsbranch. Weg dorthin: den produktiven Endpoint über **Clone Endpoint** duplizieren und in der Repository-Konfiguration den anderen Branch wählen. Beide behalten unabhängige GPU- und Worker-Einstellungen. Für einen Testlauf des 30k-Drivers gegen eine geänderte Worker-Version ist das der saubere Weg, statt am produktiven Endpoint zu schrauben.

**Wenn je ein eigenes Image außerhalb dieser Integration gebaut wird, drei Regeln aus der Werkzeug-Ebene:** (1) **Immer einen expliziten Versions-Tag, nie `latest`** — `latest` zeigt NICHT automatisch auf den neuesten Build, sondern nur auf den letzten ohne expliziten Tag gepushten; ein Worker, der auf `latest` zeigt, zieht still die falsche Version. (Genau so war das alte, inzwischen tote LB-Image getaggt.) (2) **`docker build --platform=linux/amd64`** — RunPod-Worker laufen auf x86-Linux; ein auf Apple Silicon ohne dieses Flag gebautes Image ist unbrauchbar. (3) Für private Registries akzeptiert RunPod nur Credentials vom Typ `docker login` (Benutzername + Access-Token), einmal in den Account-Einstellungen hinterlegt und dann im Template auswählbar. [Quelle: Skill `companion-clis`]

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
    "transport": "inline"
  }
}
// formats WEGLASSEN = alle vier Ausgaben (Voreinstellung) — so gehört es.
// Die bisherigen Läufe setzten hier "formats": ["markdown"]; genau diese
// Einschränkung hat die Aktenzeichen gekostet. Siehe Abschnitt 14a.
→ {"id": "...", "status": "IN_QUEUE"}

GET /status/<job_id>
→ {"status": "COMPLETED", "executionTime": 4338,
   "output": {"results": [{"markdown": "..."}]}}
```

### 5.2 Eingabefelder

- **Genau eines von** `file_url` | `file_b64` | `volume_path` (XOR). Caps: `file_url` 200 MB, 120 s Fetch-Timeout; `file_b64` 20 MB Handler-seitig, aber Gateway-seitig **10 MB auf `/run`** und 20 MB auf `/runsync`. Ein 1,4-MB-PNG ist als Base64 ~1,9 MB — bequem darunter.
- **NIE JSON-`null` für ein optionales Feld senden** — RunPods `rp_validator` lehnt `null` auch für optionale Felder ab. Nicht gesetzte Felder weglassen (der Repo-eigene Client baut Payloads Feld für Feld auf).
- `backend`: 5 gültige Werte — `pipeline` | `vlm-auto-engine` | `vlm-http-client` | `hybrid-auto-engine` | `hybrid-http-client` — pro Job auf demselben Worker umschaltbar. Wahlbegründung in Abschnitt 6.
- `formats`: Voreinstellung sind ALLE vier (markdown, content_list, middle, images) — **und dabei bleibt es. Nie einschränken (Abrufregel, Abschnitt 14a).** Die bisherigen Läufe setzten `["markdown"]`; genau das hat die Kennungen gekostet.

  **Was das Markdown weglässt — und warum das UNSER Fehler ist, nicht der der Engine:** Das Markdown enthält die Blockklasse `text` (plus Tabellen, Listen, Bildreferenzen). Alles, was die Engine als Seitenmöblierung einstuft, fehlt darin: gemessen über alle 15 Kalibrierungs-Dokumente die Klassen `header`, `footer`, `aside_text`, `page_number` und `page_footnote` — fünf Klassen, nicht eine. Darin stehen Kennungen, Bescheiddatum, Veranlagungsjahr, Formular- und Druckkennungen, Aussteller-Kontaktdaten und die Blattzählung. Die Engine erkennt das alles fehlerfrei, legt es typisiert ab und **protokolliert in einem eigenen Feld (`discarded_blocks`), dass sie es weglässt.** Sie hat also nichts falsch gemacht und es sogar angesagt; wir haben ein Format bestellt statt aller und nie nachgesehen, was dieses Format weglässt. **Und der Abgleich über die lokalen Engines zeigt, wie teuer das ist:** Textebene, tesseract, paddle-ocr und easyocr führen diese Kennungen alle im Fließtext — ein Write-back aus dem nackten Markdown wäre also schlechter als *jede* Alternative einschließlich des heutigen kaputten Ist-Zustands und würde die Auffindbarkeit genau der Dokumente verschlechtern, die der Lauf verbessern soll. Abhilfe: alle Formate bestellen und die Blöcke aller fünf Möblierungsklassen an den Text hängen — mit einem Fremdtext-Filter davor, weil in diesen Blöcken stellenweise englische Modell-Prosa statt Dokumentinhalt steht. Belege im Kalibrierungs-Dokument 5.1-2 und 5.4; die Ausgestaltung gehört zur Write-back-Frage (Integrationsstrecke, Abschnitt 4).
- `transport`: `inline` legt das Markdown direkt in die Antwort. Default ist `tarball_b64` (müsste man auspacken); außerdem existiert `s3` (BUCKET_*-Env; R2/Hetzner-kompatibel). **Antwort-Cap 20 MB** (gemessen: ein 82-Seiten-PDF mit Bildern sprengt es auf beiden Wegen — bei Ein-Seiten-Jobs irrelevant).
- `start_page`/`end_page`: Client-seitiges Batching großer Dokumente; Seitenzahl erhöht den VRAM NICHT (der Repo-Autor hat 5.039 Seiten auf einer 24-GB-A5000 für ~$1,15 geparst).
- `lang`: nur vom `pipeline`-Backend beachtet; das VLM ignoriert es (Deutsch = latin, unproblematisch).
- `basename` (Muster `[a-zA-Z0-9_-]+`), `archive_format`, `probe` (Diagnose-Modus) existieren ebenfalls.
- **Bilder sind Eingabe erster Klasse:** PNG/JPEG/GIF/BMP/TIFF/WebP werden per Magic-Bytes erkannt und intern in ein Ein-Seiten-PDF konvertiert. Es gibt KEIN `format`-Eingabefeld. Auch DOCX/PPTX/XLSX werden per Auto-Detect akzeptiert.

### 5.3 Antwort- und Fehlersemantik

- Job-Zustände: `IN_QUEUE`, `IN_PROGRESS`/`RUNNING` (mutmaßlich derselbe Wire-State), `COMPLETED`, `FAILED`, `CANCELLED`, `TIMED_OUT`.
- **`/runsync` hat ein 60-Sekunden-Zeitfenster** — ein Kaltstart überschreitet das regelmäßig. Für den ersten Job nach Ruhe deshalb `/run` + Statusabfrage nehmen (so arbeiten auch beide Driver), nicht `/runsync`. [Quelle: Skill `flash`, Gotcha 9]
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

## 5a. Der vollständige Contract und die Betriebsknöpfe (Worker-Doku, gelesen 05.08.2026)

Die Abschnitte 5.1–5.4 stammen aus dem Skill und einem Repo-Dossier vom 03.07.2026. Am 05.08.2026 wurden die Doku-Seiten des Worker-Repos selbst gelesen — `reference/api`, `guides/output-modes`, `guides/input-formats`, `guides/scaling`, `guides/concurrency`, `guides/troubleshooting`. Was dabei herauskam, steht hier; es ergänzt Abschnitt 5 und korrigiert an zwei Stellen unsere Empfehlung.

### 5a.1 Transport ist der eigentliche Hebel für „alles holen" — und unsere Wahl ist die anfälligste

**`formats` wirkt NUR bei `transport: "inline"`.** Für `tarball_b64` (die Voreinstellung) und `s3` ist das Feld ein **No-op**: Diese beiden Transporte liefern **immer** das vollständige Ausgabeverzeichnis mit allen vier Artefakten. Wörtlich: *„those transports always ship a self-contained archive with all four artifacts"*.

**Das ist für die Abruf-Regel (Abschnitt 14a) entscheidend.** Wir haben `inline` gewählt und müssen dort *daran denken*, nicht zu filtern — genau das ist am 12.07.2026 schiefgegangen. Bei `tarball_b64` oder `s3` **kann** man gar nicht filtern. Eine Regel, die von Disziplin abhängt, ist schwächer als eine, die von der Mechanik erzwungen wird. **Für den 30k-Lauf ist die Transport-Wahl deshalb neu zu entscheiden, nicht aus den Altläufen zu übernehmen.**

| Transport | Liefert | Größengrenze | Kostet |
|---|---|---|---|
| `tarball_b64` (Default) | immer alle vier Artefakte als base64-`.tar.gz` | ~20 MB Gateway-Cap | Entpacken beim Aufrufer |
| `inline` | die Felder einzeln, **filterbar** — und damit verlierbar | ~20 MB Gateway-Cap | Disziplin |
| `s3` | immer alle vier, als presignierte URL | **keine praktische Grenze** | eigener Bucket + PII-Entscheidung |

**Der 20-MB-Cap ist real und hat eine hässliche Fehlersignatur.** Gemessen an einem 82-seitigen PDF: Der Worker parst erfolgreich, protokolliert `done`, und scheitert dann beim Zurückliefern — `Failed to return job results. | 400, message='Bad Request'` gegen `/job-done`. Der Aufrufer sieht kein Ergebnis, nur `None`. **Ein Erfolg im Log und trotzdem kein Ergebnis** — wer das nicht kennt, sucht den Fehler im Parse. Betroffen sind `inline` **und** `tarball_b64` (Bilder sind schon Rasterbytes, gzip hilft nicht). Bei einem Job pro Seite ist das unkritisch; bei einem Job pro Dokument nicht.

**`s3` löst es — und ist gleichzeitig eine PII-Entscheidung.** Vier Env-Variablen am Endpoint (`BUCKET_ENDPOINT_URL`, `BUCKET_NAME`, `BUCKET_ACCESS_KEY_ID`, `BUCKET_SECRET_ACCESS_KEY`, optional `BUCKET_REGION`/`BUCKET_PREFIX`), presignierte URL mit 3600 s Gültigkeit, Objektschlüssel `{prefix}{basename}-{uuid}.tar.gz`. **Der Worker löscht nichts** — ohne Lifecycle-Regel am Bucket wachsen die Tarballs unbegrenzt. Und: Für den Steuer-Korpus wäre ein solcher Bucket **ein weiterer externer Dienst mit PII** und fällt damit unter die Regel aus Abschnitt 9.1, Punkt 6 — das ist Jurgens Entscheidung, nicht eine Konfigurationsfrage.

### 5a.2 Felder, die unser Contract-Abschnitt nicht kannte

- **`effort`** — nur für die `hybrid-*`-Backends, `"medium"` (MinerUs Default) oder `"high"`; auf anderen Backends **abgelehnt**. Damit ist die Warnung aus Abschnitt 6 präzisierbar: „hybrid bei seinem Default" heißt `effort=medium`, und `high` schaltet die Bild-/Diagramm-Analyse zu.
- **`formula_enable`** (Default `true`) und **`table_enable`** (Default `true`) — LaTeX-Formeln bzw. strukturierte HTML-Tabellen. Beide standen nie in unseren Dokumenten, obwohl sie an der Voreinstellung ausgeschaltet werden **könnten**. Für uns gilt: nicht anfassen (Abruf-Regel).
- **`server_url`** — Pflicht für die `*-http-client`-Backends (externer vLLM-Server).
- **`probe: true`** — Diagnosemodus: überspringt das Parsen und liefert stattdessen das Dateisystem-Layout des Workers und die laufende MinerU-Version. Der billigste Weg, „welches Modell liegt wirklich im Image" zu beantworten, ohne Console-Logs.
- **`archive_format`** — `"tar.gz"` (Default) oder `"zip"`; No-op für `inline`.
- **`start_page`/`end_page`** sind **0-basiert und inklusiv**; `end_page: -1` (Default) heißt „bis zum Ende".

### 5a.3 Die Antwortstruktur vollständig — und was das für die Persistenz-Anforderung heißt

Anforderung 10 der Driver-Liste („die ganze Antwort persistieren") ist jetzt konkret benennbar. Auf **Job-Ebene**: `ok`, `elapsed_seconds` (reine Handler-Zeit, **ohne** Kaltstart und Transport), `mineru_version`, `results[]`, `debug`, und — nur wenn eine Recycling-Schwelle gerissen ist — `refresh_worker`. Auf **Eintrags-Ebene**: `basename`, `source` (`url:…`/`b64`/`volume:…`), `pages_requested`, plus die transportabhängigen Felder.

Der `debug`-Block trägt: `backend`, `input_format`, `model_dir` (welcher Modell-Snapshot geladen wurde), `gpu` mit `name`, **`compute_capability`** und `total_memory_gb`, sowie `phase_ms` mit `fetch_input`, `mineru_parse`, `package`. **Im Fehlerfall gibt es kein `results`** — dafür `error`, `traceback` (die letzten fünf Frames) und denselben `debug`-Block.

**`compute_capability` ist dabei kein Zierrat, sondern der Blackwell-Detektor** (siehe 5a.5): 8.6 = Ampere, 8.9 = Ada, 9.0 = Hopper, **12.0 = Blackwell**.

**Und es gibt einen Fortschritts-Kanal, den wir nicht kannten:** `GET /v2/<endpoint_id>/stream/<job_id>` liefert Phasen-Ereignisse (`fetching_input`, `parsing` mit Eingabegröße, Format und Seitenbereich). Ein `packaging`-Ereignis fehlt **absichtlich** — es käme so kurz vor dem Ergebnis, dass es den `COMPLETED`-Status überschreiben und den Job scheinbar auf `IN_PROGRESS` festnageln könnte.

### 5a.4 Env-Knöpfe am Endpoint, die wir nicht gesetzt haben — und einer davon ist eine Falle

| Knopf | Default | Bedeutung für uns |
|---|---|---|
| `MINERU_MAX_CONCURRENCY` | 1 | Bei uns 1. Höher nur auf ≥24 GB mit VLM; die Begründung ist der vLLM-KV-Cache, der bei zwei parallelen Parses geteilt wird. |
| `REFRESH_WORKER_AFTER_JOBS` | **0 (aus)** | Unser Skill empfiehlt „~100 für große Batches" — der Knopf ist aber **standardmäßig aus** und muss gesetzt werden. |
| `REFRESH_WORKER_AFTER_PAGES` | **0 (aus)** | **Falle:** Parses ohne Seitenbegrenzung (`end_page=-1`, also unser Normalfall) zählen **0** auf den Seiten-Zähler. Wer auf diese Schwelle setzt, bekommt bei uns **nie** ein Recycling. Für unseren Fall ist die **Jobs**-Schwelle die richtige. Fehler und Probes zählen auf keinen der beiden Zähler. |
| `MINERU_SKIP_WARMUP` | aus | Anlassen. Eingeschaltet zahlt **jeder** Kaltstart ~110–130 s ohne jede Amortisierung. |
| `MINERU_WARMUP_BACKEND` | `vlm-auto-engine` | **Muss zu dem Backend passen, das die Aufrufer wirklich benutzen** — wer `vlm` aufwärmt und `pipeline` anfragt, zahlt beim ersten Pipeline-Aufruf trotzdem den Kaltstart. |
| `MINERU_PDF_RENDER_TIMEOUT` | 300 s | MinerUs eigener Abbruch für hängende Seiten-Renderings; für sehr komplexe PDFs auf 600 erhöhen. |
| `MINERU_PDF_RENDER_THREADS` | 4 | CPU-Threads fürs Rastern; auf vCPU-starken Workern erhöhbar. |
| `MINERU_PROCESSING_WINDOW_SIZE` | 64 | **Nur `pipeline`-Backend.** Kein Nebenläufigkeits-Knopf. |
| `LOG_FORMAT` | `json` | Jede Zeile trägt `job_id` — der Schlüssel, um Zeilen eines Jobs zu korrelieren. `msg` ist ein stabiler Bezeichner, auf den man alarmieren kann. |

**Nebenläufigkeit, korrekt gerechnet:** effektive Parallelität = `workersMax × MINERU_MAX_CONCURRENCY`. Alles darüber **wartet in der Queue** — mehr Requests zu feuern macht keinen einzigen schneller. Unser rollierendes Fenster von 120 gleichzeitigen Jobs ist damit kein Parallelitäts-, sondern ein Retention-Werkzeug (Abschnitt 5.4): Es hält die Queue gefüllt und die Abholung nah an der Fertigstellung.

**Und zwei Log-Wahrheiten, die die Audit-Pflicht verschärfen:** RunPod **drosselt und verwirft** Logs von Workern, die zu viel ausgeben. Und **Worker-Logs verschwinden, wenn der Worker endet** — nur die Endpoint-Logs bleiben 90 Tage. Der eigene Audit-Store ist also nicht bloß bequemer, er ist für die Worker-Ebene der einzige Beleg überhaupt.

### 5a.5 Zwei Fehlerklassen, die wir zu grob beschrieben hatten

**Blackwell — jetzt mit Mechanismus statt Faustregel.** Unsere Regel lautete „nie `ADA_48_PRO`". Der Grund: xformers/flash-attention im Basis-Image (vLLM 0.11.2) bringt Kernel für Ampere, Ada und Hopper mit, **aber keinen SM120-Pfad für Blackwell**; auf einer Blackwell-Karte greift xformers zum Hopper-Kernel und stürzt bei der VLM-Initialisierung ab (`CUDA error … flash_fwd_launch_template.h`). **Das ist strukturell und nicht durch ein Update behebbar:** MinerU 3.2.x pinnt `vllm<0.12`, und die erste vLLM-Version mit Blackwell-Bezug ist 0.13. Erkennbar an `debug.gpu.compute_capability == "12.0"`. **Wichtig für uns:** Das `pipeline`-Backend benutzt xformers/flash-attn **nicht** und läuft auf Blackwell problemlos — die Sperre gilt also nur für VLM- und Hybrid-Backends.

**⚠️ AM OBJEKT WIDERLEGT — mindestens für Blackwell-MIG-Scheiben (05.08.2026, Doku-Verifikationstest).** Sechs Test-Jobs (Kursmaterial-Seiten, 150-dpi-Renderings) liefen mit dem **VLM-Backend** (`vlm-auto-engine`) auf **demselben Image `:17d4955b2`** über drei Worker — und **alle drei Worker saßen auf `compute_capability: "12.0"`**: `debug.gpu.name = "NVIDIA RTX PRO 6000 Blackwell Server Edition MIG 1g.24gb"`. Alle sechs Jobs **erfolgreich**, 2,6–5,2 s Ausführungszeit, Formeln als LaTeX, Nachrechen-Test bestanden. Der oben beschriebene Crash trat nicht ein, obwohl das Image unverändert ist. **Was gesichert ist: die Beobachtung** (sechs volle Antwort-JSONs mit `debug`-Block unter `artifacts/2026-08-05-doku-verifikationstest/`). **Was offen ist: die Erklärung** — Kandidaten, keiner geprüft: (a) auf einer MIG-Scheibe verhält sich der Kernel-Dispatch anders als auf der Vollkarte; (b) xformers wählt inzwischen einen funktionierenden Fallback-Pfad; (c) der alte Crash hing an einer anderen Karten-/Treiberkonstellation der `ADA_48_PRO`-Vollkarten. Die alte Crash-Beobachtung vom Juli bleibt stehen; keine der beiden wird verallgemeinert. **Praktische Konsequenz:** Die Symptomtabellen-Zeile „compute_capability 12.0 ⇒ Crash erwartbar" gilt nicht mehr als Vorhersage — Blackwell ist auf diesem Endpoint inzwischen ein **beobachteter Normalfall** (siehe 9.5: RunPod liefert Blackwell-MIG in den Standard-Pools), und ob eine konkrete Karte crasht, entscheidet der Testjob, nicht die Compute-Capability.

**Der CUDA-Boden — eine Falle, die wir gar nicht kannten.** Die Feld-Liste `allowedCudaVersions` sagt RunPod, welche Treiber-CUDA-Versionen der Worker akzeptiert. Steht dort etwas Älteres, als das Image wirklich braucht (aktuell 12.9, geerbt von `vllm/vllm-openai:v0.11.2`), darf RunPod auf einen älteren Host planen — und der Container lehnt ihn erst beim Prestart-Hook ab: `nvidia-container-cli: requirement error: unsatisfied condition: cuda>=12.9`. Ergebnis sind **sporadische** Fehlschläge, je nachdem welcher Host erwischt wurde. Regel: Die Liste exakt auf den echten Boden setzen und **nicht** mit älteren Werten „auffüllen", weil sie harmlos aussehen. Bei einem vLLM-Sprung den Boden neu aus den Release-Notes lesen.

### 5a.6 SIGTERM: was mit laufenden Jobs beim Herunterskalieren passiert — und eine Versionsfrage, die wir klären müssen

Beim Herunterskalieren (Idle-Timeout, Recycling, manueller Neustart) schickt RunPod SIGTERM. Die frühere Fassung dieses Absatzes trug einen **internen Widerspruch** („ab 1.8.0 drain" und zugleich „bis einschließlich 1.8.0 scheitern") — **am 05.08.2026 im Quelltext beider Versionen entschieden:** **Bis einschließlich v1.8.0 wirft der Handler** zwischen den Phasen `RuntimeError: worker shutting down, refusing further work` (in `handler.py` des Commits `17d4955b2`, Funktion `_check_shutdown`, raist ausdrücklich) — und weil RunPod einen vom Handler zurückgegebenen Fehler als **terminal** behandelt (kein Retry), sehen ganz normale Skalierungsvorgänge wie dauerhafte Job-Fehler aus. **Der Fix kam mit v1.8.4 (01.08.2026):** Dort loggt `_check_shutdown` nur noch und wirft **nie** — der laufende Job wird zu Ende geparst und normal zurückgeliefert (drain); der Code-Kommentar dort benennt exakt die alte Fehlerklasse als Grund.

**Die offene Versionsfrage ist am 05.08.2026 GEKLÄRT — ungünstig:** Der Image-Commit `17d4955b2` **ist der Release-Commit von v1.8.0** (die GitHub-Tags v1.8.0 bis v1.8.3 zeigen alle auf genau diesen Commit; v1.8.1–1.8.3 waren Doku-only-Releases ohne Code-Änderung). Das Endpoint-Image trägt den Scale-down-Bug also. **Stand 05.08.2026: Fork gesynct, Release erfolgt, Rebuild + Verifikation OFFEN.** Der Fork `ConstantinHvber/mineru-runpod` ist einmalig auf den Upstream-Head gesynct (`dfab1e35b…`, enthält den v1.8.4-Fix), und Jurgen hat den Release angelegt (`releases/tag/v1.8.4` — der Agent durfte das nicht, Permission-Grenze). **Unmittelbar danach zeigte das Template weiterhin `:17d4955b2` — die Abhilfe ist erst vollzogen, wenn drei Dinge bestätigt sind:** der RunPod-Rebuild (Fehlschläge nur in Console → Builds sichtbar; Risiko: 30-min-Docker-Grenze, das Dockerfile lädt beim Bauen beide Modelle), der **neue Image-Tag im Template `5wv9rko6zv`**, und ein **Testjob** auf dem neuen Image. Bis dahin läuft der Endpoint auf dem fehlerhaften v1.8.0-Image, und ein Lauf mit Scale-to-zero-Zyklen bleibt dem terminalen Scale-down-Fehler ausgesetzt. Nachprüf-Ort für den Tag: GraphQL `myself { endpoints { template { imageName } } }` (REST `/v1/templates` listet GitHub-Integrations-Templates nicht). **Nebenbefund zum Versions-Weg:** Die Job-Antworten melden `mineru_version: "unknown"` (alle sechs Test-JSONs) — Ursache im Worker-Quelltext gefunden: `worker/parse.py` liest `getattr(mineru, "__version__", "unknown")`, und das installierte mineru-Paket trägt kein `__version__`-Attribut. Das Feld beantwortet also die **MinerU**-Version nicht zuverlässig und die **Worker**-Version gar nicht; der Commit-Tag im Template ↔ GitHub-Tags-Abgleich ist der verlässliche Weg.

Unabhängig davon gilt weiter: **Ein Abbruch mitten im Parse ist unmöglich** (blockierender GPU-Aufruf); SIGTERM wirkt nur zwischen den Phasen.

---

## 6. Backend-Wahl: pures `vlm-auto-engine` — auf Scans UND auf Born-Digital, aus entgegengesetzten Gründen

- **Auf beschädigten Scans nie `hybrid` mit seinem Default (`effort=medium`):** Hybrid ersetzt den Layout-Pass des VLM durch einen CV-Detektor und kann Text **stumm fallen lassen**.
- **Auf Born-Digital-Material auch nicht `hybrid`**, obwohl dessen Kernversprechen (eingebetteten Text wiederverwenden, kann nicht halluzinieren) dort echt gilt. Gemessen an denselben 5 formeldichten Kursfolien:

| | `vlm-auto-engine` | `hybrid-auto-engine` (effort=high) |
|---|---|---|
| Inline-Formeln erkannt | **41** | 28 |
| Ausführungszeit | **4,1 s** | 8,9 s |

Hybrid verliert 13 von 41 Inline-Formeln und lässt Subskripte stumm fallen — der risikofreie Zins `$R_{f}$` kommt als nacktes `R` zurück. Es ist besser in Typographie und 2,2-mal langsamer. Bei mathematischem Material den Tausch nicht wert.

**⚠️ Eine Lücke in dieser Entscheidung, sichtbar geworden am 05.08.2026: Wir haben `vlm` nie gegen `pipeline` gemessen, sondern nur gegen `hybrid`.** Beide bisherigen Messungen — die auf beschädigten Scans und die auf born-digital-Material — vergleichen `vlm-auto-engine` mit `hybrid-auto-engine`. Das dritte Backend blieb außen vor, und die Worker-Doku empfiehlt es ausgerechnet für unseren Materialtyp: *„`pipeline`: native multi-language OCR (109 languages). **Best for images and scanned PDFs without an embedded text layer.**"* Während sie das VLM als *„Best for PDF"* führt. Weitere Eigenschaften, die für unseren Bestand sprechen könnten: `pipeline` beachtet als einziges Backend den `lang`-Parameter (Skriptfamilien-Codes; `latin` deckt Deutsch, `east_slavic` das Kyrillische), es braucht nur ~4 GB VRAM statt ~13 GiB, es läuft mit ~3–5 s/Seite konstant über alle GPU-Typen, und es ist von der Blackwell-Sperre nicht betroffen. Dagegen spricht der empirische Befund: Auf unseren 15 Kalibrierungs-Dokumenten hat **`vlm` nachweislich gut abgeschnitten** (Kalibrierungs-Dokument 5.1), und für `pipeline` gibt es auf diesem Material **keine einzige Messung**.

**Das ist keine Umkehrung der Entscheidung, sondern eine benannte Lücke.** `vlm-auto-engine` bleibt gesetzt, weil es gemessen ist und `pipeline` nicht. Aber die Behauptung „wir haben das beste Backend gewählt" ist so nicht gedeckt — gewählt wurde das bessere von zweien. Der Test wäre billig: dieselben 15 Fixture-Dokumente durch `pipeline` mit `lang: "latin"`, gegen dieselbe Zielwortliste ausgewertet (Verfahren im Kalibrierungs-Dokument, Abschnitt 3). Besonders interessant für zwei Klassen, in denen das VLM erfindet: dichte Formulare und Handschrift — und für das französische Dokument, wo `lang` überhaupt erst greift.

**Die beiden Begründungen sind entgegengesetzt, und das ist der Merksatz:** Auf beschädigten Scans gewinnt das VLM, weil kein CV-Detektor Text verschlucken kann; auf born-digital gewinnt es, weil Hybrid die Mathematik plättet. Dass beide Kompartimente dieselbe Antwort geben, darf nicht verdecken, dass es zwei verschiedene Fragen sind — wer die Backend-Wahl auf eine dritte Materialart überträgt, muss sie dort neu herleiten. [Quelle: gbrain `projects/mineru-ocr/born-digital-backend-benchmark`, vollständig gelesen 01.08.2026; Klassen-Einordnung im Kalibrierungs-Dokument]

---

## 7. PDFs zuführen: rendern, niemals mit pypdf splitten

**Der Weg:** `pdftoppm` (poppler), 150 dpi, eine Seite = ein Job.

```bash
pdftoppm -png -r 150 input.pdf outdir/prefix__      # ~278 KB pro Seite
```

Gemessen bei 150 dpi: ~278 KB pro Seite, ~303 MB für 932 Seiten. `pdftoppm` kommt aus poppler (Homebrew: `brew install poppler`).

**Verbote, beide real bezahlt:**
- **NIE Seiten mit `pypdf` extrahieren.** `PdfWriter.add_page()` kopiert das komplette `/Resources`-Dictionary der Seite — einschließlich der über das GANZE Dokument geteilten XObject-Bilder. Gemessen 2026-07-12: **360 MB Quell-PDFs wurden 4,1 GB „Einzelseiten"**, im Schnitt 1,5 MB, schlimmster Fall 5,92 MB — gegen ein 10-MB-Payload-Cap, und aus einem Fünf-Minuten-Upload wären geschätzt vier Stunden geworden.

  **Das Verbot ist pypdf-spezifisch — pypdfium2 bläht NICHT auf (gemessen 05.08.2026):** Der 17-MB-16-Seiten-Fixture-Bescheid, geschnitten mit **MinerUs eigener Schneidefunktion** (`mineru.cli.common.convert_pdf_bytes_to_bytes`, intern pypdfium2 — dieselbe Bibliothek, mit der der Worker serverseitig `start_page`/`end_page` umsetzt): Einzelseiten-Summe **1,00× Original** (17,51 MB, größte Seite 1,27 MB), 3-Seiten-Chunks ebenso 1,00×. **Lokales PDF-Splitting ist damit ein gangbarer Weg**, echte PDF-Chunks unter die Payload-Caps zu bekommen, ohne Seiten zu rastern — als Alternative zum `pdftoppm`-Rendering, nicht als Ersatz der Wahlfreiheit. **Eine geerbte Falle dabei:** MinerUs Schnitt-Fallback überspringt kaputte Seiten **still** („Skipped broken PDF pages") — wer lokal schneidet, muss die Seitenzahlen der Chunks gegen die Gesamtseitenzahl gegenrechnen und bei Differenz hart abbrechen, statt diesen Pfad zu erben.
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
5. **Auf EINER Seite testen, Output mit eigenen Augen lesen, Zahlen prüfen.** Der billige Realitäts-Check gegen VLM-Zahlendreher: eine Rechenkette im OCR finden und **nachrechnen** — eine halluzinierte Zahl erfüllt ihre eigene Gleichung nicht. Das ist der einzige Test, den ein halluzinierendes Modell nicht bestehen kann, und er ist billig: Auf born-digital-Material wurden vier zusammenhängende Werte einer Optionspreis-Rechnung nachgerechnet, inklusive zweier Verteilungsfunktions-Werte an exakt diesen Stellen — vier gleichzeitig konsistente Zahlen sind gelesen, nicht erfunden. Auf Steuer-Bescheiden bieten sich die eingebauten Ketten an (Äquivalenzbetrag × Steuermesszahl = Messbetrag; Einheitswert × Promille = Messbetrag; DM/€-Umrechnung). **Dass dieser Test nötig ist, ist empirisch belegt**, nicht vorsichtshalber: auf dichten Formularen und Handschrift erfindet das VLM plausiblen Text (Belege im Kalibrierungs-Dokument).
6. Für PII-Korpora (der 30k-Lauf): EU-Datacenter-Beschränkung setzen (Liste in Abschnitt 3, Punkt 7). **Und die zweite PII-Regel:** PII verlässt die Maschine Richtung Dritt-Dienste nur durch Jurgens eigene Hand und Entscheidung — der Agent ist für solche Uploads hart gesperrt (belegt am Ground-Truth-Skript und an der bewusst nicht gefahrenen externen VLM-Option; Kalibrierungs-Dokument, Abschnitt 6). RunPod selbst ist durch die EU-Platzierung abgedeckt; jeder weitere externe Dienst ist eine neue Entscheidung.
7. **Den Auftrag am Material prüfen, nicht am Ordner.** „OCR alles hier drin" wörtlich zu nehmen kostete beinahe das Dreifache: von 2733 Seiten im Zielordner waren 1801 (66 %) Geschäftsberichte mit makelloser Textebene, für die OCR nichts hinzufügt — nur 932 Seiten waren echtes Zielmaterial. Eine Anweisung wörtlich auszuführen, ohne zu prüfen, ob sie das Gemeinte trifft, ist keine Gehorsamkeit, sondern Faulheit. Für den 30k-Lauf ist genau das die Ziellisten-Frage (Integrationsstrecke, Abschnitt 2).

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
- **Account-Quota: 10 Worker über ALLE Endpoints.** Für den vollen 30k-Lauf ist eine Erhöhung bei RunPod zu beantragen (Support/Console) — offenes Bead `jurgen-dmg`.
- **Die vier Durchsatz-Basen, und warum man sie nicht mischen darf.** Es kursieren vier Zahlen, jede aus einem anderen Setup:
  | Basis | Wert | Herkunft |
  |---|---|---|
  | pro **Dokument** | ~21,5 s/Dokument | **am 03.08.2026 aus der Quelle geklärt:** 8 Dokumente in 172 s auf EINEM warmen Worker, darunter die beiden größten Grundsteuer-Bescheide (17 MB und 10 MB) — bewusst konservativ gewählt. Dieselben 172 s stehen in der LB-Messbasis als „68 Seiten / 172 s"; **die 8 Dokumente sind also 68 Seiten**, macht rechnerisch 8,5 Seiten/Dokument und 2,5 s/Seite. Das ist die Zahl hinter der 18-Stunden-Schätzung |
  | pro Seite, große Screenshot-Bilder | ~4,3 s/Seite | 593-Seiten-Buchlauf (~1 MB Bilder je Seite) |
  | pro Seite, gerenderte 150-dpi-Seiten | **~2,35 s/Seite** | 932-Seiten-Lauf — die realistischere Basis für den 30k-Lauf, weil dort ebenfalls `pdftoppm`-Renderings verarbeitet werden |
  | pro Seite, LB-Ära unsaturiert | 0,40 Seiten/s (= 2,5 s/Seite) | 68 Seiten in 172 s auf einem warmen 4090-Klasse-Worker; Formel `Gesamtseiten/(0,4·N)` Sekunden |
  Eine Dokument-Rate lässt sich nur in eine Seiten-Rate übersetzen, wenn man die Seiten pro Dokument kennt — für die Messgruppe waren es 8,5, **für den 30k-Bestand ist diese Zahl nirgends erhoben.** Die 18-Stunden-Schätzung ist damit eine Ableitung aus der Dokument-Rate (30.000 × 21,5 s ÷ 10 Worker ≈ 18 h), keine Messung; die daraus folgende GPU-Kostenschätzung (10 Worker × 18 h × $0,69/h ≈ **$124**) ist ebenfalls eine Ableitung und ausdrücklich als solche zu behandeln. Wer belastbarer planen will, erhebt zuerst die Seitenverteilung des Zielbestands. [Quellen: Bead `jurgen-dmg`; gbrain `lb-era-operational-record`; `born-digital-backend-benchmark`; Skill; die 21,5-s-Zahl aus der Kalibrierungs-Bewertung via gbrain `ocr-integration-spec` §5]
- **⚠️ Der Stundensatz, auf dem die Kostenschätzung ruht, ist zwischen den Quellen uneins — und der Unterschied ist groß.** Dieses Handbuch führt seit dem 12.07.2026 **$0,69/h für einen RTX-4090-Worker** (aus der eigenen Abrechnung des 593-Seiten-Laufs). Die Preistabelle der Worker-Doku ordnet **$0,69/h dem Pool `AMPERE_24`** (A5000/3090) zu und nennt für `ADA_24` (4090) **~$1,10/h** — als „indicative, subject to change". Beide können stimmen (andere Zeit, anderes Konto, Spot gegen On-Demand), aber sie können auch heißen, dass der damalige Lauf gar nicht auf einer 4090 lief. **Folge für die Planung: Die 30k-Hochrechnung von ~$124 beruht auf $0,69/h; bei $1,10/h wären es rund $200.** Vor dem Lauf am echten Konto prüfen (`runpodctl gpu list` bzw. die Abrechnung eines Testjobs), nicht aus einer der beiden Tabellen übernehmen. **Dritte gemessene Zahl (05.08.2026): $0,49/h je Worker** — alle drei Worker des Doku-Verifikationstests trugen `costPerHr: 0.49` in der Worker-Liste, und alle drei waren **Blackwell-MIG-Scheiben** (`RTX PRO 6000 … MIG 1g.24gb`). Naheliegende, ungeprüfte Deutung: MIG-Scheiben sind billiger als Vollkarten. Die Spanne der real gesehenen Sätze ist damit **$0,49–$1,10 (Faktor 2,2)** — was die Anweisung „am echten Konto messen" nicht schwächt, sondern verschärft: Welcher Satz greift, hängt offenbar davon ab, welche Hardware der Pool gerade liefert. Kosten des Sechs-Seiten-Tests inkl. Warm-Restart und drei kurz laufender Worker: **Balance-Delta $0,21 zum Teardown-Zeitpunkt, ~35 min später $0,275 — die Abrechnung bucht nach, obwohl `currentSpendPerHr` längst 0 zeigt.** Ein Teardown-Balance-Delta unterschätzt also; endgültig zählt die Tageszeile von `runpodctl billing serverless` (Beleg-Snapshot: `artifacts/2026-08-05-doku-verifikationstest/user-snapshot.json`).
- **Dokumentlänge erhöht den VRAM NICHT — und daraus folgt eine Alternative zu unserem Seiten-Rendering.** Ein sequenzieller Parse bleibt bei ~13 GiB, unabhängig von der Seitenzahl (aufgeschlüsselt: Modellgewichte 2,16 GiB, Aktivierungen 0,75, Non-Torch 0,02, CUDA-Graph-Cache 0,23, **KV-Cache 9,50** — der KV-Cache dominiert und wird vorab nach `gpu_memory_utilization=0.5` belegt). Belegt an einem **5.039-seitigen** PDF auf einer 24-GB-Karte. Was bei großen Dokumenten bricht, ist deshalb nie der VRAM, sondern: Alles-oder-nichts-Fehlschlag, das 20-MB-Antwort-Cap und fehlende Wiederaufnahme. **Die Abhilfe der Worker-Doku ist serverseitiges Seiten-Slicing:** dieselbe hochgeladene Datei mehrfach mit unterschiedlichen `start_page`/`end_page` anfragen — **das Dokument wird nie vorab zerlegt**. Für unseren 30k-Lauf ist das eine ernsthafte Alternative zum Rendern von rund einer Viertelmillion PNG-Dateien: kein Renderschritt, keine Bilddateien, kein pypdf-Problem. Dagegen steht der Grund, aus dem wir „ein Job = eine Seite" gewählt haben — eine fehlende Seite ist dann unmöglich zu übersehen. **Beides gegeneinander abzuwägen ist Teil der Driver-Entscheidung; entschieden ist es nicht.**
- **GPU-Pool-Kennungen sind historisch uneinheitlich** — `ADA_48` gegen `ADA_48_PRO` war für dieses Projekt schon einmal eine Falle. Vor dem Aufnehmen eines nicht voreingestellten Pools die exakte Zeichenkette in RunPods GPU-Typ-Referenz prüfen. `AMPERE_80` (A100, Compute 8.0) ist laut Worker-Doku unterstützt, aber von niemandem getestet; sein Stundensatz (~$1,89) macht ihn pro Seite meist **teurer** als `ADA_24`.
- **EU-Stock-Realität:** 24-GB-Karten sind in EU-Datacentern häufig knapp („Low"); 16-GB-Tiers hatten besseren Stock, scheiden aber aus, weil die Default-Konfiguration des Workers 24 GB braucht. **Auf QB ist das kein Problem, sondern nur Wartezeit** — die Queue hält die Jobs, bis Kapazität da ist; genau diese Eigenschaft war auf dem alten LB-Endpoint der 2-Minuten-Killer. Für den 30k-Lauf heißt das: Laufzeit kann sich durch Stock strecken, Jobs gehen dabei nicht verloren.
- **Der Stock-Anzeiger lügt nicht, er antwortet nur auf eine andere Frage:** Der `stockStatus` aus der GPU-Typ-Liste ist **global**, nicht EU-secure-spezifisch — er ist ein Hinweis auf die Wahrscheinlichkeit, keine Zusage für den gewünschten Datacenter. „High" verbessert die Chancen, garantiert nichts.
- **Automatischer GPU-Typ-Wechsel greift erst ab 5 Max-Workern.** Gibt man mehrere GPU-Typen an, wechselt die Plattform nur dann angebotsabhängig zwischen ihnen, wenn `workersMax >= 5` ist. Für einen Lauf mit knappem EU-Stock heißt das: eine GPU-Liste anzugeben, aber `workersMax` niedrig zu lassen, schaltet genau die Ausweich-Mechanik ab, für die man die Liste angegeben hat. [Quelle: Skill `flash`, Gotcha 8]
- **GPU-Gruppen sind Angebots-Pools, keine Modelle** — das erklärt die Blackwell-Falle bei `ADA_48_PRO` (der Pool umfasst RTX 6000 Ada, L40 und L40S, und RunPod mischt dort Karten ein, denen die Kernel fehlen) und die Zusammensetzung der übrigen Pools: `AMPERE_16` = RTX A4000/A4500/RTX 4000 Ada/RTX 2000 Ada, `AMPERE_24` = A5000/L4/RTX 3090, `AMPERE_48` = A40/A6000, `AMPERE_80` = A100, `ADA_24` = RTX 4090, `ADA_80_PRO` = H100-Varianten. Wer einen bestimmten Chip braucht statt eines Pools, pinnt den GPU-Typ explizit (dafür existiert das MCP-Werkzeug `set-endpoint-gpus` mit Ausschlussliste). [Quelle: Skill `flash`, GPU-Tabelle]
  **Diese Pool-Tabelle ist eine Momentaufnahme und am 05.08.2026 nachweislich überholt:** Der stehende Endpoint fordert `RUNPOD_GPU_SIZE: "AMPERE_24,ADA_24"` an — geliefert wurden **Blackwell-MIG-Scheiben** (`NVIDIA RTX PRO 6000 Blackwell Server Edition MIG 1g.24gb`, `compute_capability 12.0`), auf allen drei Workern des Doku-Verifikationstests. RunPod mischt Blackwell also inzwischen in die **Standard**-24-GB-Pools, nicht nur in `ADA_48_PRO`. Damit schützt „nie `ADA_48_PRO`" nicht mehr vor Blackwell — was nach dem Widerlegungs-Befund in 5a.5 (VLM lief auf diesen Scheiben fehlerfrei) derzeit kein Schaden ist, aber die Karten-Erwartung jeder Planung ändert. Nachprüf-Ort: `debug.gpu` in den eigenen Job-Antworten.
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
- Serverless bei `workersMin=0` ohne Jobs: **$0/h**, wiederholt beobachtet. Auf QB ist kein Standby-Mechanismus dokumentiert — anders als die LB-`workersStandby`-Falle, die in der €30-Session der eine zweifelsfreie Kostentreiber war (Warm-Pool kostete trotz `workersMin=0`; das Feld war via `update-endpoint` nicht einmal setzbar). **Erste QB-Standby-Beobachtung (05.08.2026):** Der QB-Endpoint trägt live `workersStandby: 3` (REST) — und stand damit **im Ruhezustand trotzdem auf $0/h** (`currentSpendPerHr: 0` vor dem Test, 24 Tage inaktiv, null laufende Worker). Das Feld existiert auf QB also, war aber in Ruhe kostenneutral — die LB-Falle reproduziert dort in dieser Konstellation nicht. Über das Verhalten unter oder nach Last sagt die Beobachtung nichts; nach dem Teardown bleibt die leere Worker-Liste plus `currentSpendPerHr: 0` das Erfolgskriterium, nicht das Feld.
- Kostenfragen mit der echten Abrechnungs-API klären (`get-billing`), nicht mit Wanduhr-Schätzungen (eine solche Schätzung wich 2026-07-07 deutlich vom Istwert ab).

### 9.8 Kochbuch: ein Batch von Anfang bis Ende

Die Phasen aus 9.1–9.6 als eine durchgehende Sequenz. Jeder Schritt ist ein einzelnes Kommando mit einem Zwischen-Urteil — keine Auto-Retry-Leiter, kein Skript, das die Entscheidungen selbst trifft.

1. **Brain-first + Doku:** die gbrain-Seiten zum Thema lesen, dieses Handbuch und den Skill. (Regel 0; zweimal teuer bezahlt.)
2. **Balance:** `python3 <skill>/scripts/preflight.py` (oder `runpodctl user`). Unter dem Minimum: aufladen, nicht weiterprobieren.
3. **Endpoint adoptieren, nicht voraussetzen:** `get-endpoint` auf `l79lebhinxaqjz` — existiert er? Stehen `workersMax`, `idleTimeout`, `RUNPOD_INIT_TIMEOUT`, Container-Disk noch richtig? (Auto-Downscale nach 3/7 Tagen Inaktivität setzt die Werte still herunter, ohne sie je wiederherzustellen.) Konfiguration bei Bedarf neu durchsetzen.
4. **Ist-Zustand der Worker lesen:** `list-endpoint-workers` — Alt-Kohorten aus früheren Läufen? Die räumt niemand außer dir.
5. **Kostenrahmen benennen und Zustimmung einholen:** Anzahl × Stufe × Grund, plus die Ableitung, wie lange es dauern und was es kosten soll.
6. **Zielmenge prüfen** (nicht den Ordner, das Material — 9.1 Punkt 7) und, bei PII, die EU-Datacenter-Beschränkung setzen.
7. **Seiten rendern:** `pdftoppm -png -r 150 <pdf> <outdir>/<stem>__` je Quelldokument; Renderfortschritt per Marker-Datei festhalten, damit ein Neustart nicht neu rendert.
8. **EINE Seite testen:** einen einzelnen `/run`-Job, Antwort abholen, **Output mit eigenen Augen lesen**, eine Rechenkette nachrechnen. Erst wenn das stimmt, geht der Batch los.
9. **`.bak` der Zieldatei/-datenbank anlegen** (die Driver tun das nicht von selbst).
10. **Batch fahren:** rollierendes In-Flight-Fenster (bewährt: 120), Job-ID **vor** dem nächsten Submit ins Ledger schreiben, Ergebnisse inkrementell speichern, Balance-Wache mit harten Abbruchschwellen, Abholung überlappend zur Submission (30-Minuten-Retention!).
11. **Während des Laufs beobachten:** `list-endpoint-workers` (nie `endpoint-health`), `runpodctl user` für die Ausgabenrate. Auf Anomalien subtraktiv reagieren: anhalten, Primärquelle lesen.
12. **Ergebnis verifizieren:** Vollzähligkeit (jede Seite ein Ergebnis), leere Outputs am Bild prüfen, Stichprobe lesen, Halluzinations-Check auf den schlechtesten Scans.
13. **Teardown:** `/cancel` offener Jobs → `/purge-queue` → `workersMin=0` + kurzer Idle-Timeout → **Worker-Liste pollen, bis sie leer ist** → `currentSpendPerHr` muss 0 sein → Endpoint und Template stehen lassen.
14. **Nachbereiten:** Lauf-Kennzahlen (Seiten, Fehler, Kosten, Dauer) festhalten und offene Punkte notieren — der eigene Audit-Store ist der einzige dauerhafte Beleg, RunPod hält Logs nur 90 Tage und bietet keinen API-Abruf dafür.

### 9.8a Kommando-Referenz zum Kochbuch

Die Schritte aus 9.8 als konkrete Aufrufe. **Zwei Vorbehalte, die immer gelten:** Für `runpodctl` ist die Live-Hilfe autoritativ für Flags — die Flag-Oberfläche hat sich zwischen CLI-Versionen bereits geändert (Abschnitt 10.1), und ein hier abgeschriebenes Flag kann veraltet sein. Und jedes Kommando, das etwas verändert, fällt unter die Zustimmungsregel (Anzahl × Stufe × Grund).

**Kontostand und Ausgabenrate (Schritt 2, 11).** Der einzige Live-Kanal; die Historie liegt woanders.
```bash
runpodctl user                      # clientBalance, currentSpendPerHr, spendLimit
python3 /Users/jurgen/CodingProjects/DevonThink886/.claude/skills/recreate-mineru-serverless/scripts/preflight.py 10
```
`preflight.py` ist derselbe Check mit Schwelle und Exit-Code — Exit 1 unter dem Minimum, Exit 2, wenn er die Ausgabe nicht lesen konnte.

**Endpoint adoptieren statt voraussetzen (Schritt 3).** Über das MCP-Werkzeug `get-endpoint` auf `l79lebhinxaqjz`. Zu prüfen: existiert er, und stimmen `workersMax`, `idleTimeout`, Container-Disk und die Env-Variablen `RUNPOD_INIT_TIMEOUT` / `MINERU_MAX_CONCURRENCY` noch? Nach 3 bzw. 7 Tagen ohne Requests senkt die Plattform die Worker-Zahl selbsttätig und stellt sie nie wieder her. **Env-Variablen leben auf dem Template, nicht am Endpoint** — sie zu ändern heißt, das Template zu ändern und den Endpoint umzuhängen.

**Worker-Ist-Zustand (Schritt 4, 11).** MCP `list-endpoint-workers`, oder direkt:
```bash
curl -s -H "Authorization: Bearer $RUNPOD_API_KEY" \
  "https://rest.runpod.io/v1/endpoints/l79lebhinxaqjz?includeWorkers=true"
```
Das ist die einzige Wahrheit über Worker. **`endpoint-health` nie als Beleg nehmen** — der Aggregat-Zähler hat zweimal „bereit" gemeldet, während nichts bediente.

**Seiten rendern (Schritt 7).**
```bash
pdftoppm -png -r 150 "<quelle>.pdf" "<ausgabeverzeichnis>/<stem>__"
```
Ein Job pro Seite, ~278 KB pro Seite. Niemals mit `pypdf` splitten (Abschnitt 7).

**Einen Job submitten und abholen (Schritt 8, 10).**
```bash
# Submit: Base64 des Seitenbilds, alle Formate (formats-Feld weglassen!)
curl -s -X POST "https://api.runpod.ai/v2/l79lebhinxaqjz/run" \
  -H "Authorization: Bearer $RUNPOD_API_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"input\":{\"file_b64\":\"$(base64 < seite.png)\",\"backend\":\"vlm-auto-engine\",\"transport\":\"inline\"}}"
# → {"id":"<job-id>","status":"IN_QUEUE"}   ← diese ID SOFORT ins Ledger, vor dem nächsten Submit

# Status/Ergebnis
curl -s -H "Authorization: Bearer $RUNPOD_API_KEY" \
  "https://api.runpod.ai/v2/l79lebhinxaqjz/status/<job-id>"
```
**Nie JSON-`null` für ein nicht gesetztes Feld senden** — weglassen. Ergebnisse werden nur ~30 Minuten vorgehalten, danach antwortet `/status` mit 404: Abholung muss die Submission überlappen. Und `/runsync` ist für den ersten Job nach Ruhe ungeeignet (60-Sekunden-Fenster gegen ~8,5 min Kaltstart).

**Teardown (Schritt 13), in dieser Reihenfolge.**
```bash
curl -s -X POST -H "Authorization: Bearer $RUNPOD_API_KEY" \
  "https://api.runpod.ai/v2/l79lebhinxaqjz/cancel/<job-id>"     # offene Jobs, auch laufende
curl -s -X POST -H "Authorization: Bearer $RUNPOD_API_KEY" \
  "https://api.runpod.ai/v2/l79lebhinxaqjz/purge-queue"          # nur wartende; Limit 2 Aufrufe/10 s
```
Danach `workersMin=0` und kurzer Idle-Timeout setzen (MCP `update-endpoint`) — und dann **die Worker-Liste pollen, bis sie nachweislich leer ist**. Nicht die Konfiguration glauben: Konfigurationsänderungen evakuieren keine laufenden Worker. Abschluss: `runpodctl user` muss `currentSpendPerHr` 0 zeigen. Endpoint und Template stehen lassen.

**Abrechnungs-Historie (Schritt 14).** MCP `get-billing` bzw. `runpodctl billing serverless` — Zeitreihen, kein Live-Wert. Kostenfragen mit dieser Quelle klären, nie mit Wanduhr-Schätzungen.

**Was es NICHT gibt:** kein `run-batch` (das war eine Halluzination eines Research-Reports), keinen API-Abruf für Worker-Logs (Console-only, Stand 07/2026; das MCP führt inzwischen `stream-worker-logs`, ungeprüft), keinen Weg, einen Image-Build per CLI, MCP oder API anzustoßen (Console-only).

---

### 9.9 Der Pod-Fallback

Wenn Serverless nicht verfügbar ist (kein Stock, Plattformstörung, oder ein Fall, der eine interaktive Shell braucht), existiert als Rückfallebene ein **einzelner GPU-Pod** mit demselben MinerU-Stack; sein Runbook ist der Skill `recreate-mineru-pod` unter `/Users/jurgen/CodingProjects/DevonThink886/.claude/skills/`. Was man dazu wissen muss, bevor man ihn anfasst:

- **Ein Pod kostet ab Erzeugung, durch den gesamten Image-Pull hindurch, bis zum Stop** (gemessen: $0,697/h über 40 Minuten Pull, ohne laufenden Container). Anders als Serverless gibt es kein Scale-to-zero — ein vergessener Pod läuft und kostet. Deshalb schreibt der Skill ein **Sicherheitsnetz** vor: `--stop-after <ISO8601-Zeitpunkt>` beim Anlegen setzen (und ausdrücklich **nicht** `--terminate-after`, das den Pod samt Zustand vernichtet statt ihn anzuhalten).
- **Die dort einst stehende HARD RULE „RunPod vermietet nicht unter ~$10" ist im Skill selbst bereits durchgestrichen und korrigiert** — ein 4090-Pod wurde bei $2,13 Balance gemietet. Der Skill zieht daraus die generellere Lehre, die über RunPod hinausgeht: *eine einzelne Beobachtung wurde zu einer harten Grenze verallgemeinert, und diese falsche Regel hätte beinahe den einen Weg verhindert, der funktionierte.* Was bleibt: ein leeres Konto scheitert auf verwirrende Weise (Fehler, die wie GPU-Knappheit aussehen und Kontostand sind) — **also die Zahl kennen, nicht die Schwelle annehmen.**
- **Pro Session neu anlegen, am Ende löschen — nicht stoppen und wieder starten.** Ein *gestoppter* Pod ist an seinen Host geschweißt; beim Neustart kann dieser Host keine freie GPU mehr haben, und der Pod ist blockiert (genau so passiert). Frisch anlegen erlaubt, das zu nehmen, was gerade verfügbar ist — bei ~7 min Kaltstart aus einem gebackenen Image ist das für gelegentliche Läufe der günstigere Weg.
- **GPU-Wahl bei knappem Angebot ist eine Erwartungswert-Entscheidung, keine Leiter.** Der Skill schreibt sie ausdrücklich als wiederholte Abwägung vor: pro Versuch **Wert** (Verfügbarkeit/Kosten/Tempo) × **Wahrscheinlichkeit, dass es diesmal klappt** (durch frühere Fehlschläge gesenkt, nicht auf null gesetzt) − **Zeitkosten des Versuchs**. Daraus kann folgen, dieselbe erste Wahl noch einmal zu versuchen, obwohl sie eben scheiterte — oder zu wechseln, sobald ihr Erwartungswert unter den der Alternative fällt. Derselbe beobachtete Fehlschlag führt je nach Wertabstand zu verschiedenen Entscheidungen; **deshalb kann keine Schleife das übernehmen.** Und: die Wahrscheinlichkeiten werden nur real, wenn man faire Zeitfenster gibt (volles Kaltstart-Fenster abwarten, bevor eine Karte als gescheitert gilt). Kein sicherer Zug in Sicht: anhalten und fragen.
- Bereitschaft ist auch hier nur der antwortende Dienst: `/v1/models` → 200. Dafür sind die Skill-Skripte `probe.py` und `poll_ready.py` gebaut (Abschnitt 13). **Diagnose-Detail:** RunPods Proxy sitzt hinter Cloudflare — ein Default-User-Agent bekommt **403**, was wie ein Auth-Problem aussieht und keins ist; mit echtem Browser-User-Agent antwortet ein noch nicht bereiter Port stattdessen mit **404**. Der Pod-Proxy braucht **keinen** API-Key.
- Der Pod-Weg war die **erste** Architektur des Projekts (vor LB und QB); die dort gemessene Rate (~0,41 Seiten/s) ist praktisch identisch mit der LB-Rate, weil Container, Modell und GPU dieselben waren — der Unterschied zwischen den Architekturen war immer Parallelität, nie Einzelgeschwindigkeit.

---

## 10. Werkzeugflächen: womit man was steuert

[Quellen: gbrain `runpod-platform-gotchas` (Docs-Sweep 2026-07-03), `runpod-operational-discipline` Regel 8, Incident-Seite 2026-07-12]

| Fläche | Wofür | Bekannte Fallen |
|---|---|---|
| `runpodctl` (CLI) | `user` (= `me`): live `clientBalance`/`currentSpendPerHr`/`spendLimit` (GraphQL-gestützt); `billing pods`/`billing serverless`/`billing network-volume`; `datacenter list` (die einzige verlässliche DC-Liste); `gpu list`; `doctor` (Erstinstallation/Diagnose von API-Key und SSH); `pod list --status/--since/--created-after`; `send`/`receive` für Dateitransfer | `serverless create` defaultet `--workers-max 3`; `--workers-min 0` schluckte die 0 still (Beobachtung 2026-07-12); `pod create --data-center-ids` honoriert nur die ERSTE ID; kann keine Builds anstoßen. **Und die Meta-Regel des Skills: `runpodctl --help` ist autoritativ für Flags und Syntax** — dieses Dokument (und jeder Skill) beschreibt Abläufe und Entscheidungen, nicht die jeweils aktuelle Flag-Liste; vor unbekannten Kommandos die Live-Hilfe lesen |
| RunPod-MCP-Tools (in dieser Claude-Umgebung als `mcp__runpod__*`) | `list-endpoint-workers` (die per-Worker-Wahrheit), `get-endpoint`, `update-endpoint`, `get-billing` (v2, historisch), `run-endpoint`/`runsync-endpoint`, `purge-endpoint-queue`, `cancel-job`, `get-job-status` | `endpoint-health` ist der lügende Aggregat-Zähler — nie als Beleg nehmen; `workersMin: 0` gab am 2026-07-12 einen 500; `stream-worker-logs`/`stream-pod-logs` existieren inzwischen in der Tool-Oberfläche (v2), Funktionsfähigkeit ungeprüft |
| REST-API | Worker-Objekte via `GET /endpoints/{id}?includeWorkers=true`; Billing-Zeitreihen `/billing/*` | **Env-Variablen leben auf TEMPLATES, nicht Endpoints** (PATCH /endpoints hat kein env-Feld) — Env ändern = Template neu/ändern + `templateId` umhängen; ein Serverless-Template bindet an genau EINEN Endpoint. REST kennt **keine GPU-Pools** (`gpuTypeIds` = explizite Modellnamen); Pool-Kürzel wie `ADA_24` sind GraphQL/Hub-only. Kein Log-/Metrik-Abruf per API (Stand Sweep 2026-07-03; Console-Logs 90 Tage) → **der eigene Audit-Store ist der einzige dauerhafte Beleg** |
| GraphQL | `myself { clientBalance currentSpendPerHr spendLimit }` — die einzige Live-Kostenquelle | `locations`-Enum ist grob (Ländercodes inkl. GB) und NICHT äquivalent zu REST-`dataCenterIds`; das LB/QB-Flag ist das interne `EndpointInput.type`-Enum (historisches Wissen, im QB-Zielbild irrelevant) |
| Console (nur dort) | GitHub-Integration-Builds; Worker-Logs (Stand 07/2026); Hub-Deploys | — |
| Flash SDK | Im QB-Zielbild **nicht mehr gebraucht** (war nur zur LB-Erzeugung nötig); liest `RUNPOD_API_KEY` aus der Env, kann `~/.runpod/config.toml` (runpodctl-Format `apikey=`) NICHT parsen | — |

### 10.1 CLI-Fallen aus den mitgelieferten Prüffällen der offiziellen Skills (gelesen 03.08.2026)

Die drei allgemeinen RunPod-Skills stammen als offizielle Skills von RunPod selbst (`runpod/skills` auf GitHub, per Hash in `skills-lock.json` gepinnt) und bringen ausführbare Prüffälle mit. Diese Prüffälle sind die dichteste Quelle für Schnittstellen-Fallen, weil jeder von ihnen einen real beobachteten Fehler festhält:

- **Die Autoscaler-Flags der CLI haben sich in Version 2.3 geändert — und weichen von der API ab.** `--scaler-type` und `--scaler-value` sind **entfernt**; an ihre Stelle treten `--scale-by requests` und `--scale-threshold <N>`. Auch die Werte `REQUEST_COUNT` und `QUEUE_DELAY` gehören nicht mehr in die CLI. **Achtung, das ist keine Umbenennung auf allen Ebenen:** Die REST-/MCP-Oberfläche führt `scalerType`/`scalerValue` mit genau diesen Werten weiter (siehe die Werkzeugtabelle oben) — CLI und API sprechen an dieser Stelle unterschiedliche Sprachen. Wer eine Anleitung von der einen Ebene auf die andere überträgt, erzeugt einen Fehler, den keine Fehlermeldung erklärt.
- **`serverless create` kennt kein `--image`.** Ein Endpoint entsteht immer aus einem Template oder einem Hub-Listing. Aus einem eigenen Image wird also zuerst ein Template (`template create --name … --image … --serverless`), dann der Endpoint (`serverless create --template-id <id>`). Die Reihenfolge ist zwingend.
- **`--gpu-id` verlangt auf dem Hub-Pfad GPU-Pool-Kennungen, auf dem Template-Pfad auch Anzeigenamen.** Auf dem Hub-Pfad lehnt die API einen Anzeigenamen (wie er aus `gpu list` und der Hilfe kommt) mit `Invalid GPU Pool ID` ab; dort gehören Pool-Kürzel hin. Der sichere Weg auf dem Hub-Pfad ist, `--gpu-id` wegzulassen und die Voreinstellung des Listings greifen zu lassen. Der Widerspruch ist bei RunPod als Ticket `runpod/runpodctl#287` erfasst — also ein bekannter Fehler, keine Eigenart der eigenen Umgebung.
- **`--terminate-after` und `--stop-after` sind nicht dasselbe.** Ersteres **löscht** den Pod zum Zeitpunkt, letzteres **hält ihn nur an**. Für ein Kostennetz, das den Zustand behalten soll, ist `--stop-after` richtig (so schreibt es der Pod-Skill vor); wer wirklich löschen will, nimmt bewusst `--terminate-after`.
- Kleinigkeiten, die Zeit sparen: GPU ist die Voreinstellung beim Anlegen eines Pods (kein Flag nötig); ein CPU-Pod verträgt **keine** GPU-Flags; ein Netzwerk-Volume wird beim Anlegen mit `--network-volume-id` angehängt; für SSH liefert `ssh info <pod-id>` bzw. `pod get <pod-id>` die Verbindungsdaten, während die früheren interaktiven SSH-Unterbefehle abgelöst sind.

S3-kompatible API auf Network-Volumes: nur 4 EU-Datacenter (EU-CZ-1, EU-RO-1, EUR-IS-1, EUR-NO-1) — eine strikte Teilmenge der 9 Compute-DCs. (Für diese Architektur ohne Volume nur relevant, falls je `transport: "s3"` erwogen wird.)

---

## 11. Die vorhandenen Skripte (Vorlagen — NICHT der Driver dieses Projekts)

> **Zur Einordnung, damit hier kein falscher Eindruck entsteht: Für das DEVONthink-Projekt existiert noch kein Code.** Die beiden Skripte unten sind Ad-hoc-Werkzeuge aus einer einzigen Sitzung am 12.07.2026, geschrieben für **zwei andere Korpora** — ein eingescanntes Buch und einen Satz Uni-PDFs. Sie haben nie einen DEVONthink-Record gesehen, kennen weder Erkennung noch Export noch Write-back, und ihre Pfade sind auf jene Korpora fest verdrahtet. Ihr Wert ist, dass sie den Job-Contract **erwiesenermaßen richtig bedienen** und dass ihre Schwächen dokumentiert sind: Sie sind die Vorlage, an der sich der künftige Driver messen lässt, und die Liste der Fehler, die er nicht erben darf. Der 30k-Driver wird neu geschrieben (Anforderungen in 11.3), nicht aus diesen beiden gepatcht.
>
> Beide Skripte wurden am 03.08.2026 **im Quelltext gelesen**; alle Aussagen unten stammen aus dem Code, nicht aus einer Beschreibung.

Beide Skripte entstanden und liefen in Claude-Code-Session `0fba4d0a-f13a-47f0-b59b-a8f4d9e811a6` (2026-07-12, Projekt `/Users/jurgen/CodingProjects/DevonThink886`). **Von hier aus starten — Dauerkopien:** `/Users/jurgen/AgentRuns/NewOrderDevon/scripts/ocr_run.py` und `/Users/jurgen/AgentRuns/NewOrderDevon/scripts/ocr_pdfs.py` (byte-identisch zu den Session-Scratch-Originalen unter `/Users/jurgen/.claude/jobs/0fba4d0a/tmp/`, die jederzeit weggeräumt werden können). [Quelle: Skill, Abschnitt „The driver"; gbrain-Incident-Seite, Nachtrag 2026-07-31]

### 11.1 `ocr_run.py` — der Lauf, der die $0,58-Zahl erzeugte

- **Verifiziert durch Öffnen der Artefakte am 03.08.2026, nicht aus der Beschreibung übernommen:** Das Job-ID-Ledger `/Users/jurgen/.claude/jobs/0fba4d0a/tmp/ocr_jobs.json` hält exakt 593 Einträge (`{record_index: runpod_job_id}`, Beispiel `"0" -> "089d5000-ad95-48b4-af3d-5884c47baf6a-e1"`). Die Zieldatei `/Users/jurgen/AgentRuns/B219Buch/ocr_values.json` hält exakt 593 Records mit den Feldern `chapter`, `chapter_page`, `cropped`, `ocr`, `path`, `value` — davon **592 mit Text und einer mit leerem String**, kein einziger auf `null`. Die Sicherung `ocr_values.json.bak` hält dieselben 593 Records mit null Treffern im `ocr`-Feld, ist also der Vorzustand.
- **Ein Detail, das nur der Abgleich von Code und Artefakt zeigt:** Der Code kann den leeren String gar nicht geschrieben haben — er setzt das Feld nur, wenn Text zurückkam (`if md:`), und lässt es sonst unberührt. Dass die blanke Seite dennoch als `""` und nicht als `null` dasteht, heißt: Sie wurde **nach dem Lauf von Hand nachgetragen**. Genau dieser manuelle Eingriff hat verhindert, dass der in 11.2a-1 beschriebene Endlos-Wiedervorlage-Fehler praktisch wirksam wurde.
- **Fluss:** Rendert NICHTS selbst — liest pro Record einen vorhandenen `"cropped"`-PNG-Pfad (Dateien unter `/Users/jurgen/AgentRuns/B219Buch/cropped/`). **Die Herkunft dieser Zuschnitte galt bis zum 03.08.2026 als ungeklärt und war es nie — sie wurde nur nicht nachgeschlagen:** Bead `jurgen-p06` (geschlossen 2026-07-12) spezifiziert den Schritt vollständig, inklusive der exakten Pixel-Box, der Ablage der Originale in einem eigenen Verzeichnis und der Ergänzung der JSON-Felder; sein Abschlussvermerk hält die verifizierten Stückzahlen und Bildmaße fest. **Lehre, die über den Einzelfall hinausgeht: Ein „ungeklärt"-Vermerk ist selbst eine Behauptung und altert — vor dem Zitieren nachsehen, ob er noch stimmt.** (Derselbe Fehler wie beim Offene-Punkte-Eintrag in der Geschichte, Abschnitt 6.7.) **Phase 1:** submittet jeden noch nicht submitteten Record als einen `/run`-Job (`file_b64`, `backend: vlm-auto-engine`, `transport: inline`, `formats: ["markdown"]`) und schreibt die Job-ID **nach jedem einzelnen Submit** ins Ledger (Restart kann nicht doppelt submitten); prüft alle 50 Submits die Balance via `runpodctl user`, Abbruch unter $0,15. **Phase 2:** pollt `GET /status/<job_id>` für alle offenen Jobs, schreibt fertiges Markdown ins `"ocr"`-Feld des Records, speichert die Zieldatei alle 20 s, Abbruch unter $0,05 Balance.
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

### 11.2a Vier Befunde, die erst der Quelltext zeigte (03.08.2026)

Diese vier standen in keiner Beschreibung und sind für den künftigen Driver wichtiger als alles andere in diesem Abschnitt:

1. **Die beiden Skripte behandeln ein leeres Ergebnis genau entgegengesetzt — und beide falsch.** `ocr_pdfs.py` schreibt bei leerer Antwort eine **leere Datei** und zählt die Seite als erledigt; da sein Wiederaufnahme-Test „existiert die Ausgabedatei?" lautet, ist eine echte Leerseite von einer misslungenen Extraktion **nicht mehr unterscheidbar** und wird nie wiederholt. `ocr_run.py` macht das Gegenteil: Es wertet leeres Markdown als Fehler und lässt das Feld leer — und da sein Wiederaufnahme-Test „Feld noch leer?" lautet, wird eine **echt leere Seite bei jedem Neustart erneut submittet und erneut bezahlt**. Richtig ist keines von beidem: Ein leeres Ergebnis ist ein gültiges Ergebnis, das als solches festgehalten und **einmal am Bild geprüft** werden muss (die eine blanke Seite des 593er-Laufs ist genau dieser Fall).
2. **Der Zusammenbau prüft die Vollständigkeit nicht.** `ocr_pdfs.py` fügt am Ende die Seiten-Markdowns je Dokument zusammen und überspringt dabei nur den Fall „gar keine Seite vorhanden". Fehlen 3 von 20 Seiten, entsteht trotzdem ein Dokument — **ohne Warnung, äußerlich vollständig**. Das ist exakt die Falle, vor der der eigene Docstring des Skripts warnt („Missing pages produce no error"): Sie wurde beim Rendern vermieden und beim Zusammenbauen wieder eingebaut.
3. **Die Antwort wird auf ein einziges Feld reduziert.** Beide lesen nur `output.results[0].markdown`. Der Rest der Antwort — Ausführungszeit, der `debug`-Block des Workers mit GPU-Name, Compute-Capability und Millisekunden je Phase, bei Fehlern der vollständige Traceback — wird verworfen. Damit fehlt jede Grundlage, hinterher zu sagen, welche Seite wie lange auf welcher Karte lief oder woran ein Job scheiterte. **Das ist dieselbe Regelverletzung wie beim Format-Filter** (Abschnitt 14a), nur eine Ebene tiefer.
4. **`ocr_run.py` verlässt sich auf einen Bildpfad, den es nicht selbst erzeugt.** Es liest je Record ein Feld `cropped` mit einem fertigen PNG-Pfad. Der Zuschnitt-Schritt ist dokumentiert (Bead `jurgen-p06`, siehe 11.1), steht aber **außerhalb des Skripts** — für ein reproduzierbares Verfahren bleibt das ein Loch: „aus dem Original wird das Bild, das in den Job geht" existiert als Aufgabenbeschreibung und als Ergebnis auf der Platte, nicht als ausführbarer Teil der Strecke. Anforderung 14 der Liste unten schließt es.

### 11.3 Anforderungsliste an den 30k-Driver (abgeleitet aus den belegten Schwächen beider Skripte)

1. Job-Ledger mit Write-Through **vor** jedem Submit (aus `ocr_run.py` übernehmen) + Adopt-statt-Resubmit beim Restart (offene Job-ID zuerst weiterpollen).
2. Rollierendes Fenster mit überlappender Abholung (aus `ocr_pdfs.py` übernehmen) — Pflicht wegen der 30-Minuten-Retention.
3. Retry-Politik nach der Ausnahme-Landkarte des Procrastinate-Entwurfs (Abschnitt 12), inkl. Per-Job-Poll-Timeout.
4. Balance-Wache behalten (Submits gedrosselt prüfen, harte Abbruchschwellen), plus 402 als terminalen Kill-Switch behandeln.
5. `.bak`/Snapshot der Zielbestände automatisieren statt manuell.
6. Parametrisierung statt hart codierter Konstanten.
7. Für den PII-Korpus: EU-Datacenter-Restriktion setzen (Abschnitt 3 Punkt 7).
8. Vorher klären, was offen ist: Zielliste, Write-back, Driver-Entscheidung (Abschnitt 16).
9. **Kein Format-Filter** — das `formats`-Feld weglassen, damit alle Ausgaben zurückkommen, und sie auch alle wegschreiben (Abrufregel, Abschnitt 14a). Beide Vorlagen setzen `formats: ["markdown"]`; das ist die Ursache dafür, dass in den bisherigen Läufen die Kennungen fehlen. **Und danach nachsehen, ob wirklich alles ankam** — auf der Platte des Kalibrierungs-Laufs liegen mehr Artefakt-Arten, als der Job-Contract Formate kennt; ob der Worker sie ausliefert, ist ungeprüft (Abschnitt 14a).
10. **Die ganze Antwort persistieren, nicht ein Feld daraus** — Ausführungszeit, `debug`-Block und bei Fehlern der volle Traceback (Befund 11.2a-3). Ohne sie gibt es hinterher keine Kosten-, Leistungs- oder Fehleranalyse. Das ist dieselbe Regel wie Punkt 9, eine Ebene tiefer: nicht nur alle Formate holen, sondern von jedem auch alles wegschreiben.
11. **Beim Zusammenbau des Textes einen Fremdtext-Filter vorsehen.** Die Möblierungsblöcke, die für die Kennungen mit übernommen werden müssen, enthalten stellenweise englische Kommentare des Modells über den Bildinhalt statt Dokumentinhalt (Kalibrierungs-Dokument 5.1). Ungefiltert landet Werkzeug-Prosa im Zielbestand. Der Filter gehört in den Zusammenbau, **nicht** in den Abruf — geholt wird alles.
12. **Leeres Ergebnis als eigenen Zustand führen** — nicht als Erfolg (sonst unsichtbarer Verlust) und nicht als Fehler (sonst endlose Wiedervorlage und Doppelkosten), sondern als „leer, am Bild zu prüfen" (Befund 11.2a-1).
13. **Vollständigkeit beim Zusammenbau erzwingen** — je Dokument die erwartete Seitenzahl gegen die vorhandenen Teile prüfen und bei Lücken hart abbrechen statt still zu mergen (Befund 11.2a-2). **Und zuerst nachsehen, ob der Gegenstand die Sollzahl selbst mitbringt:** Die Bescheide tragen ihre Blattzählung in den Möblierungsblöcken, die mit der vollständigen Formatbestellung ohnehin ankommen. Eine mitgelieferte Sollzahl schlägt jede von außen konstruierte, weil sie pro Objekt gilt statt im Mittel.
14. **Den Bildaufbereitungs-Schritt mit in den Driver nehmen** — vom Original bis zum Job-Bild reproduzierbar, statt auf vorhandene Dateien zu zeigen (Befund 11.2a-4).
15. **Die Transport-Wahl neu treffen, nicht aus den Altläufen übernehmen** (Abschnitt 5a.1). `inline` verlangt Disziplin beim Nicht-Filtern; `tarball_b64` und `s3` machen Filtern unmöglich. Bei `s3` zusätzlich die PII-Entscheidung einholen und eine Lifecycle-Regel am Bucket setzen, weil der Worker nichts löscht.
16. **Die Worker-Version des Images kennen** (Abschnitt 5a.6). Bis einschließlich 1.8.0 ließen Worker laufende Jobs beim Herunterskalieren mit einem terminalen Fehler scheitern, den RunPod **nie** wiederholt. **Geklärt 05.08.2026:** `:17d4955b2` = v1.8.0 (betroffen). Fork auf den Fix-Stand gesynct; **Release/Rebuild/Verifikation stehen AUS — bis dahin läuft der Endpoint auf dem fehlerhaften Image** (5a.6). Der verlässliche Versions-Weg ist der Commit-Tag im Template gegen die GitHub-Tags des Repos — NICHT das Antwortfeld `mineru_version` (meldet „unknown", Ursache in 5a.6).
17. **Beim Worker-Recycling die JOBS-Schwelle setzen, nicht die Seiten-Schwelle** (Abschnitt 5a.4). Parses ohne Seitenbegrenzung — unser Normalfall — zählen **0** auf den Seiten-Zähler; diese Schwelle würde bei uns nie greifen. Beide sind außerdem standardmäßig **aus**.

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

## 13. Die Skill-Landschaft und ihre Hilfsskripte

**Fünf Skills liegen unter `/Users/jurgen/CodingProjects/DevonThink886/.claude/skills/`** (alle fünf vollständig gelesen, 02.08.2026); drei davon sind Symlinks auf allgemeine RunPod-Skills unter `~/.agents/skills/`:

| Skill | Was er ist | Verhältnis zu diesem Dokument |
|---|---|---|
| `recreate-mineru-serverless` | das operative Original dieses Projekts (QB-Endpoint, Job-Contract, harte Regeln, Driver-Abschnitt) | **⛔ Stand 05.08.2026 trägt der Skill eine widerlegte Anweisung: sein Job-Contract-Beispiel und seine Empfehlung setzen beide `formats: ["markdown"]`, mit der widerlegten Bandbreiten-Begründung — die Ursache des Kennungs-Verlusts (Abschnitt 14a). Bis er korrigiert ist, gilt bei jedem Widerspruch DIESES Dokument, auch für Betriebsdetails.** Die frühere Präzedenzregel („bei Widerspruch gewinnt der Skill für Betriebsdetails") ist damit ausgesetzt: Sie zeigte ausgerechnet auf die eine Stelle, die falsch ist. Ein Skill wird geladen und befolgt, nicht bei Gelegenheit gelesen — ein veralteter Skill ist deshalb keine tote Datei, sondern die Landmine, die am 12.07. den Rückfall auslöste |
| `recreate-mineru-pod` | der Pod-Fallback (Abschnitt 9.9) | nur benutzen, wenn Serverless ausfällt oder eine Shell im Container gebraucht wird |
| `runpodctl` | allgemeiner CLI-Skill von RunPod (v2.3) — Kommandos, Entscheidungsregeln, URL-Schemata | **der MinerU-Skill schreibt vor, ihn VOR dem Handeln zu lesen**: dieses Projekt ist die MinerU-Schicht auf ihm. Seine Kernregel: die Live-Hilfe ist autoritativ für Flags, ein Skill für Abläufe |
| `companion-clis` | die vier Nachbar-Werkzeuge (`hf`, `gh`, `docker`, `aws`) samt Anmeldung und Fallen | relevant, wenn je wieder ein eigenes Image gebaut (Docker-Regeln, Abschnitt 4), ein Release erzeugt (`gh`) oder auf ein Network-Volume per S3 zugegriffen wird (Zugangsschlüssel = RunPod-Benutzer-ID `user_…`, Secret = S3-Key `rps_…`, `--region` = Datacenter-ID) |
| `flash` | das runpod-flash-SDK/CLI (lokal schreiben, remote ausführen) | **im QB-Zielbild nicht gebraucht** (es war nur zur LB-Erzeugung nötig). Bleibt wertvoll als Referenz für die GPU-Gruppen-Tabelle, das Auto-Switching-Limit und die Endpoint-Parameter |

Die drei Hilfsskripte unter `recreate-mineru-serverless/scripts/` (alle vollständig gelesen; sie stammen aus der LB-/Pod-Ära vom 2026-06-29 — Herkunftshinweis steht in ihren Docstrings):

- **`preflight.py`** — Schritt 0 vor JEDER GPU-Miete: Balance-Check via `runpodctl user` (kein roher API-Key, nichts in URLs); Exit 1 unter dem Minimum (Default $10). Weiterhin uneingeschränkt gültig und der richtige erste Aufruf jeder Automation.
- **`probe.py`** — Ein-Schuss-Probe `/ping`, `/health`, `/v1/models` mit Statuscode + Latenz; unterscheidet „gar kein Worker" / „Worker da, vLLM lädt/hängt" / „Port da, Route noch nicht". Auth als Bearer-HEADER; `User-Agent: Mozilla/5.0` nötig, weil RunPod Cloudflare-frontend ist (Default-UAs bekommen 403/1010). **Einordnung:** direkt sinnvoll für Pods und HTTP-erreichbare Endpoints; auf dem QB-Endpoint ist die Bereitschafts-Wahrheit stattdessen „Worker heartbeatet nach Warmup und Jobs wandern von `inQueue` nach `inProgress`" (eigene Einordnung, abgeleitet aus Abschnitt 8 — nicht separat gemessen).
- **`poll_ready.py`** — pollt `<base_url>/v1/models` bis HTTP 200 („RunPod ready/RUNNING heißt nur: Container geplant, nicht: vLLM bedient"); gleiche QB-Einordnung wie bei `probe.py`.

---

## 14. Symptom → Ursache

[Quelle: Skill, erweitert um die Failure-Logs unter `/Users/jurgen/CodingProjects/DevonThink886/ocr_comparison/`]

| Symptom | Ursache |
|---|---|
| `Failed to get Hub registry auth. Is this a template you have access to?` | Template zeigt auf das Publisher-Hub-Image. Es wurde `--hub-id` benutzt. Über die Console aus dem Fork neu bauen. |
| `This machine does not have the resources to deploy your pod` | **Zwei Ursachen, dieselbe Meldung — erst unterscheiden, dann handeln.** (a) **Deterministisch:** Die Anforderung passt auf keinen Host — bei uns Container-Disk 5 GB gegen ein 27-GB-Image. Kennzeichen: sofort, reproduzierbar, auf jedem GPU-Typ. Abhilfe ist Konfiguration, Wiederholen hilft nie. (b) **Transient:** Der Scheduler traf einen Host ohne freie GPU des angeforderten Typs. Kennzeichen: nicht-deterministisch, dieselbe Konfiguration klappt Minuten später. Abhilfe ist ein neuer Versuch (Push/Rebuild), oder bei dauerhaftem Fehlen ein anderer GPU-Pool bzw. eine andere Region. Der Blog des Worker-Repos beschreibt (b) als den häufigen Fall und nennt als dritten Hebel, **überzogene Disk-/Volume-Anforderungen zu kürzen**, weil sie die Menge passender Hosts schrumpfen lassen — dieselbe Mechanik wie (a), nur aus der anderen Richtung. |
| Worker `INITIALIZING` 20+ min, `startedAt` konstant | Normal beim ersten 27-GB-Pull. Warten. (Ob Serverless dabei Geld kostet: ungemessen — Abschnitt 9.7.) |
| Worker `INITIALIZING`, `startedAt` springt | Der Init-Timeout killt ihn. `RUNPOD_INIT_TIMEOUT` erhöhen. |
| Worker `running`, Jobs `inQueue`, `inProgress: 0` | Worker wärmt das VLM vor dem Heartbeat. Normal, 1–2 min. |
| Job `COMPLETED`, Markdown leer | Seite ansehen — vermutlich echt leer. |
| Drei Worker trotz `workersMax=1` | Alt-Kohorten einer früheren Endpoint-Version. Konfiguration evakuiert nicht. Selbst aufräumen, Liste zurücklesen. |
| HTTP 402 `Insufficient Balance` auf jedem Submit | Konto leer/unter Boden. Aufladen; Driver-Kill-Switch greift zu Recht. (Real: `_examples_minerutest/_failures.log`, 15/15 Dokumente.) |
| HTTP 401 `invalid api key` gegen eine `*.api.runpod.ai`-URL | Falscher/abgelaufener Key — ODER die URL zeigt auf einen gelöschten Endpoint (real: `_examples_retest/_failures.log` gegen den toten LB-Endpoint `sbdpcrzwlndrgb`). Erst prüfen, ob das Ziel noch existiert. |
| `endpoint-health` sagt `ready: N`, nichts wird bedient | Der Aggregat-Zähler lügt (stale/Standby-Worker). `list-endpoint-workers` ist die Wahrheit. |
| Worker meldet dauerhaft `running`, wird nie ready; direkter `/ping` gibt `000` | Handler-loses Image auf einem QB-Endpoint. Ein QB-Worker braucht den RunPod-SDK-Handler; ein Image ohne ihn bleibt ewig „läuft, aber bedient nie". (Erkenntnis aus der LB/QB-Analyse; im heutigen Zielbild nur relevant, wenn jemand ein fremdes Image auf den QB-Endpoint hängt.) |
| Kreuzberg-CLI „ok=True" von einem Backend, das gar nicht installiert ist | Stiller Fallback auf tesseract→paddle (siehe Kalibrierungs-Dokument) — betrifft das lokale Kreuzberg-Werkzeug, nicht den RunPod-Worker. Generelle Lehre: byte-Vergleich zwischen angeblich verschiedenen Engines gehört zur Auswertung. |
| Worker-Log sagt `done`, der Aufrufer bekommt **kein Ergebnis** (`None`); im Log steht `Failed to return job results. \| 400 … /job-done` | Die Antwort überschritt den ~20-MB-Gateway-Cap und wurde verworfen — der Parse war erfolgreich. Betrifft `inline` **und** `tarball_b64`. Abhilfe: `transport: "s3"`, oder per `start_page`/`end_page` in kleinere Stücke schneiden. **Nicht** über `formats` „lösen" (Abschnitt 14a). |
| `nvidia-container-cli: requirement error: unsatisfied condition: cuda>=12.9` — sporadisch, nicht bei jedem Versuch | `allowedCudaVersions` listet ältere Treiber-Versionen, als das Image wirklich braucht. RunPod plant dann auf einen zu alten Host, und der Container lehnt beim Prestart ab. Liste exakt auf den echten Boden setzen, nicht auffüllen (Abschnitt 5a.5). |
| VLM lädt das Modell erfolgreich und stürzt dann mit `CUDA error … flash_fwd_launch_template.h` ab | Blackwell-Karte. `debug.gpu.compute_capability` steht auf `12.0`. Ursprüngliche Einordnung: kein Update behebt das (MinerU pinnt `vllm<0.12`, Blackwell-Support ab 0.13); GPU-Pool beschränken oder auf `pipeline` wechseln. **Achtung, 05.08.2026: `compute_capability 12.0` allein sagt den Crash nicht mehr voraus** — auf Blackwell-**MIG**-Scheiben (`RTX PRO 6000 … MIG 1g.24gb`) lief das VLM-Backend mit demselben Image nachweislich fehlerfrei (Widerlegung in 5a.5). Tritt der Fehlertext auf, gilt die Zeile; aus der bloßen 12.0 im `debug`-Block folgt er nicht. |
| Jobs schlagen beim Herunterskalieren mit `RuntimeError: worker shutting down, refusing further work` fehl und werden nie wiederholt | Worker-Version ≤ 1.8.0. RunPod behandelt einen Handler-Fehler als terminal. Image aktualisieren (Abschnitt 5a.6). |
| Alle Seiten kommen zurück, aber jede wirft einen Layout-Format-Fehler und die Markdowns sind 0 Byte | Ein fremdgehostetes VLM beherrscht MinerUs Layout-Detection-Schritt nicht (real passiert bei einem Fremd-Hosting-Versuch). Ein Erreichbarkeits-Ping beweist die Kompatibilität nicht — den Zwei-Stufen-Ablauf testen. |

---

## 14a. DIE ABRUF-REGEL: alles holen — und wenn etwas fehlt, haben WIR verkackt

**Beim Abruf wird alles geholt. Immer, ohne Begründungspflicht, ohne Abwägung, ohne Ausnahme.** Alle Ausgabeformate, alle Felder, alle Ebenen. **Was davon verwendet wird, ist eine völlig getrennte, spätere Entscheidung**, die man jederzeit ändern kann. Der Abruf ist es nicht: Was beim Holen weggelassen wurde, ist weg.

Es gibt dabei nichts abzuwägen und nichts zu wählen. Auch „das strukturierteste Format nehmen" oder „prüfen, welche Relevanzurteile das Werkzeug eingebaut hat" sind schon wieder Entscheidungen — und jede Entscheidung, die man treffen kann, kann man falsch treffen. Die Regel existiert genau deshalb ohne Ermessen.

**Die Schuldzuweisung gehört zur Regel, sonst wirkt sie nicht: Fehlt hinterher etwas, haben WIR verkackt — nie das Werkzeug.** Das Werkzeug tut, was es tut, und sagt es meistens sogar. Wer den Verlust als Werkzeug-Eigenart erzählt („die Engine verwirft Kopfzeilen"), lernt eine Eigenart und verengt die Lehre auf dieses eine Werkzeug; beim nächsten Werkzeug beginnt das Lernen von vorn. Wer ihn sich zuschreibt, lernt ein Verfahren, das überall greift. Der Fall in diesem Projekt hatte **drei eigene Versäumnisse und null Werkzeugfehler:** Wir haben ein Format bestellt statt aller. Wir haben nicht nachgesehen, was dieses Format weglässt. Und wir haben sechs Wochen lang nicht geprüft, ob wir haben, was wir zu haben glaubten.

**Daraus folgt eine Pflicht, keine Erkenntnis: nachweisen, dass alles da ist — gegen eine Referenz von AUSSERHALB der Kette.** Zwei Ausgaben desselben Werkzeugs gegeneinander zu halten misst interne Konsistenz, nicht Vollständigkeit. Solange die externe Referenz fehlt, ist „wir haben alles" eine Behauptung, auch wenn Zahlen daran hängen. Für OCR ist diese Referenz das Seitenbild selbst bzw. die vorab benannten Sollwerte des Fixture-Sets (Kalibrierungs-Dokument, Abschnitt 3); für jeden anderen Abruf eine Quelle, die nicht dasselbe Werkzeug erzeugt hat.

**Es war sogar eine Regression, keine ursprüngliche Lücke.** Der Runner der abgelösten LB-Ära (`mineru_runner.py`, am 03.08.2026 im Quelltext gelesen) setzte `f_dump_content_list=True` — er schrieb die Struktur-Ausgabe also **mit**. Die späteren Skripte ließen sie weg. Das ältere, inzwischen als „tot" abgelegte Werkzeug machte an dieser Stelle das Richtige, und die Verbesserung hat es weggeworfen. Wer alte Artefakte pauschal als überholt behandelt, verliert genau solche Details.

**Der Fall, der diese Regel erzwungen hat (02.08.2026):** Der Job-Contract kennt vier Ausgabeformate (`markdown`, `content_list`, `middle`, `images`); die API-**Voreinstellung ist alle vier**. Der Skill empfahl am 12.07.2026 ausdrücklich die Einschränkung auf `["markdown"]` mit der Begründung, die übrigen Formate seien Bandbreitenverschwendung, und **beide Driver-Skripte übernahmen das**. Erst am 02.08.2026 stellte sich heraus, dass das Markdown nur die Blockklasse Fließtext enthält: Alles, was die Engine als Seitenmöblierung einstuft — fünf Klassen — fehlt darin, und bei deutschen Behördenbescheiden steht genau dort die Identität des Dokuments: Kennungen, Bescheiddatum, Veranlagungsjahr, Formular- und Druckkennungen. Die Daten waren erkannt, sauber und typisiert vorhanden, und die Engine führt die Weggelassenen sogar in einer eigenen Liste; **sie wurden schlicht nicht mitbestellt und niemand hat nachgesehen.** Für die beiden Produktionsläufe sind sie damit **unwiederbringlich weg** (Ergebnis-Aufbewahrung 30 Minuten), folgenlos nur deshalb, weil Buchseiten und Kursfolien keine Briefköpfe haben. Bei einem Bescheid-Korpus wäre der ganze Lauf zu wiederholen gewesen.

**Warum die Regel so absolut ist:**
- **Der Verlust ist asymmetrisch.** Zu viel geholt kostet Bandbreite, Platz und ein paar Sekunden — alles rückgängig zu machen. Zu wenig geholt kostet den ganzen Lauf noch einmal, samt GPU-Zeit und Geld; auf einem 30.000-Dokumente-Lauf ist das keine Korrektur mehr, sondern ein neues Projekt.
- **Der Filter wirkt zum falschen Zeitpunkt.** Beim Abruf weiß niemand, was später gebraucht wird — die Kennungs-Frage entstand sechs Wochen nach der Formatwahl. Beim Auswerten weiß man es; dort ist Filtern kostenlos und reversibel.
- **Der Verlust ist unsichtbar.** Fehlende Daten erzeugen kein Fehlersignal. Der Markdown-Output sah vollständig aus, die Läufe meldeten null Fehler, und niemand konnte bemerken, dass eine ganze Zone fehlt — bis jemand gezielt nach einer Kennung suchte.
- **Bandbreite ist nie das Argument.** Die eingesparte Größe ist gegen die Kosten eines Wiederholungslaufs bedeutungslos, und der Job zahlt ohnehin GPU-Zeit, nicht Transfer.

**Konkret für diese Pipeline — und seit dem 05.08.2026 schärfer, weil die Mechanik es hergibt:** `formats` weglassen, dann greift die Voreinstellung „alles". **Besser noch: einen Transport wählen, bei dem sich gar nicht filtern lässt.** `formats` ist für `tarball_b64` und `s3` ein No-op — diese beiden liefern immer alle vier Artefakte (Abschnitt 5a.1). Eine Regel, die von Disziplin abhängt, ist schwächer als eine, die von der Mechanik erzwungen wird; genau an der Disziplin ist es am 12.07.2026 gescheitert. Wenn Antwortgröße ein Thema wird — das 20-MB-Cap —, wird das über `transport` gelöst (Tarball oder S3), **nie** über das Weglassen von Ausgaben. Und dieselbe Regel gilt für jeden anderen Abruf dieser Strecke: DEVONthink-Properties vollständig holen (auch die Felder, die im Sammelabruf fehlen und einzeln nachgeladen werden müssen), Logs vollständig sichern, API-Antworten ungefiltert persistieren.

**Die Regel erzeugt eine Mengenfolge — die ist zu beziffern, nicht zu diskutieren.** Gemessen an einem sechsseitigen Bescheid des Kalibrierungs-Sets: rund 11 KB Markdown gegen zusammen etwa ein halbes Megabyte Struktur-Ausgaben, dazu die Bild-Crops — der Textanteil wächst also grob um den Faktor 45. Über alle 15 Kalibrierungs-Dokumente: 226 KB Markdown gegen 7,9 MB JSON und 5,1 MB Bilder. Auf einen 30k-Lauf hochgerechnet liegt allein die größte Struktur-Datei in der Größenordnung mehrerer zehn Gigabyte. **Das ändert die Regel nicht, es ändert die Planung:** Plattenplatz, Ablage-Layout und Backup-Umfang gehören vor den Lauf bestimmt (offene Ablage-Frage, Abschnitt 16).

**Der offene Punkt aus derselben Richtung ist am 05.08.2026 GEKLÄRT — im Worker-Quelltext (`worker/parse.py`, `worker/package.py`), nicht in der Selbstauskunft:**
- **`model.json` und `origin.pdf` liefert der QB-Worker NIE:** `parse.py` setzt `f_dump_model_output=False` und `f_dump_orig_pdf=False` ausdrücklich („bloat the response tarball"). Die Exemplare auf der Kalibrierungs-Platte sind Nebenprodukte des damaligen **lokalen** CLI-Laufs.
- **Layout- und Span-PDF kommen dagegen MIT — aber nur im Tarball-/S3-Transport:** MinerUs `aio_do_parse` hat `f_draw_layout_bbox=True` und `f_draw_span_bbox=True` als Default, der Worker überschreibt sie nicht; die PDFs landen im Ausgabeverzeichnis, und `tarball_b64`/`s3` packen das **ganze Verzeichnis**. Der Inline-Transport liefert nur die vier benannten Formate. **Tarball trägt also strikt mehr als Inline** — ein weiteres Argument für Tarball als Default.
- **Die „zweite `content_list`-Fassung" ist ein Versions-Fallback:** `package.py` sucht `{basename}_content_list.json` und fällt auf `{basename}_content_list_v2.json` zurück — zwei Dateinamens-Generationen derselben Ausgabe, kein zusätzliches Format.

**Beide Driver-Skripte tragen den alten Filter im Code und müssen vor dem 30k-Lauf geändert werden** (Anforderungsliste 11.3, Punkt 9).

---

## 15. Harte Regeln — jede einzelne ist ein bereits bezahlter Fehler

Kondensat aus Skill („HARD RULES") und gbrain `runpod-operational-discipline` (dort mit vollen Begründungen und Belegen):

0. **BEIM ABRUF ALLES HOLEN — und fehlt hinterher etwas, haben WIR verkackt, nicht das Werkzeug.** Alle Formate, alle Felder, alle Ebenen; was davon verwendet wird, ist eine getrennte und jederzeit änderbare Entscheidung, der Abruf ist es nicht. Dazu die Nachweispflicht: Dass alles da ist, wird gegen eine Referenz von **außerhalb** der Kette belegt — nie durch den Vergleich zweier Ausgaben desselben Werkzeugs, der nur interne Konsistenz misst. Volle Begründung und der Fall, der die Regel erzwungen hat, in Abschnitt 14a.
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

**Drei offene Voraussetzungen — der Lauf kann nicht „einfach gestartet" werden, solange sie offen sind.** Die ersten beiden sind Stufen der Integrationsstrecke und dort vollständig ausgearbeitet ([MinerU-DEVONthink-Integrationsstrecke.md](MinerU-DEVONthink-Integrationsstrecke.md)); hier steht, was sie für den Lauf bedeuten:

1. **Erkennung/Zielliste:** Welche der ~30k Records brauchen überhaupt OCR? Die Methodik ist vollständig entworfen (Zwei-Achsen-Modell, Fünf-Ausgänge-Triage, BELEGT/INFERENZ-Leiter, sdef-verifiziertes Signal-Inventar, Zwei-Stufen-Batch-Strategie — Integrationsstrecke Abschnitt 2), aber **nie kalibriert, nie gefahren; es existiert nirgends eine Zielliste.** „Einfach alles OCRen" ist NICHT die kalibrierte Antwort. Warum die Erkennung schwer ist: die harten Fälle haben alle `word count > 0` — Zähler allein sehen sie nicht.
2. **Write-back:** Wie kommt das OCR-Ergebnis als durchsuchbarer Text auf den DEVONthink-Record? **Offene Entscheidung mit vier recherchierten Optionen und je einem definierten Ein-Dokument-Experiment** (Integrationsstrecke Abschnitt 4). Zwei Randbedingungen, die direkt aus diesem Handbuch hineinspielen: der Markdown-Rendering-Verlust der Rand-Kennungen (Abschnitt 5.2 — es muss nicht das nackte Markdown sein, was zurückgeschrieben wird), und die Tatsache, dass DEVONthinks eigene OCR-Kommandos zwar existieren, aber eine **andere Engine** fahren und MinerUs Output damit verwerfen würden.
3. **Driver:** gehärteter einfacher Submit/Poll-Driver (Anforderungsliste in 11.3) oder der Procrastinate-Daemon (Abschnitt 12)? Unentschieden. **Mitzudenken:** Der Bestand braucht neben dem Einmal-Bulk auch einen Modus für den laufenden Eingang (Integrationsstrecke Abschnitt 6) — ein Driver, der nur den Bulk kann, ist ein Einwegkonstrukt.

4. **Ablage-Layout der Ergebnisse — undesignt, und das Akzeptanzkriterium des Beads ist Era-veraltet.** `jurgen-09v` fordert wörtlich `<OUT>/<stem>/vlm/<stem>.md` je Dokument. Das ist das Verzeichnisschema der **MinerU-CLI aus der Pod-/LB-Ära** — genau die Struktur, die im Kalibrierungs-Verzeichnis liegt. Der QB-Worker liefert Markdown inline; welche Dateien überhaupt entstehen, bestimmt allein der Driver. Und mit der Abruf-Regel sind es pro **Seite** mehrere Artefakte statt einer `.md` pro Dokument. Das Kriterium ist damit so nicht mehr erfüllbar, und es existiert keine Ablage-Konvention: Namensschema, Seiten- gegen Dokumentebene, wohin die Struktur-Ausgaben gehören, wie der Merge aussieht, wie das Ganze zum Statusmodell der Integrationsstrecke passt. Vor dem Lauf zu entscheiden — zusammen mit dem Mengengerüst aus 14a.

**Flankierend:** Worker-Quota-Erhöhung beantragen (Bead `jurgen-dmg`, siehe 9.5); EU-Datacenter-Pflicht, weil dieser Korpus deutsche Steuer-PII ist (Abschnitt 3 Punkt 7); Zeitschätzungs-Grundlagen in 9.5; **und das Datenbank-Backup vor dem Write-back, das nur Jurgen anlegen kann** (Bead `jurgen-22v`, offen — Integrationsstrecke, Abschnitt 8).

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
| Pod-Fallback-Runbook | `/Users/jurgen/CodingProjects/DevonThink886/.claude/skills/recreate-mineru-pod/` (Abschnitt 9.9; seine $10-Regel ist widerlegt) |
| Doku-Verifikationstest 05.08.2026 (6 Seiten; Blackwell-, Preis-, Downscale-, Standby-Befunde) | `/Users/jurgen/AgentRuns/NewOrderDevon/artifacts/2026-08-05-doku-verifikationstest/` (volle Antwort-JSONs mit `debug`-Block, Markdown, Ledger) + Test-Driver `scripts/ocr_test.py`; Chronik: Geschichte, Abschnitt 2.9 |
| Stufen davor/danach (Erkennung, Export, Write-back, Statusführung) | [MinerU-DEVONthink-Integrationsstrecke.md](MinerU-DEVONthink-Integrationsstrecke.md) — inkl. Experiment-Warteschlange |
| Was die Engine je Schadensklasse leistet | [MinerU-OCR-Kalibrierung-und-Backend-Vergleich.md](MinerU-OCR-Kalibrierung-und-Backend-Vergleich.md) |
