# MinerU-OCR & RunPod — Projektgeschichte, Entscheidungen und Lehren

**Stand:** 01.08.2026.
**Zweck:** Dieses Dokument erzählt einem Leser mit null Vorkontext, was dieses OCR-Projekt ist, woher es kommt, welche Entscheidungen getroffen wurden und **warum** — einschließlich der Fehlschläge, der zurückgenommenen Behauptungen und der daraus destillierten Regeln. Es existiert, damit niemand diese Arbeit (und diese Fehler) noch einmal machen muss und damit die verstreuten Quellartefakte für das Verständnis nicht mehr gebraucht werden. Das operative Wie steht im [MinerU-RunPod-Betriebshandbuch.md](MinerU-RunPod-Betriebshandbuch.md); die Qualitäts-Beweise stehen in [MinerU-OCR-Kalibrierung-und-Backend-Vergleich.md](MinerU-OCR-Kalibrierung-und-Backend-Vergleich.md).

**Quellen:** dieselben wie im Betriebshandbuch (Kopfblock dort), vor allem die gbrain-Seiten `projects/mineru-ocr/architecture-decision-qb-daemon`, `projects/mineru-ocr/eur30-lesson-lb-vs-qb`, `projects/mineru-ocr/2026-07-12-lb-relapse-incident`, `projects/mineru-ocr/lb-era-operational-record`, `wiki/personal/reflections/2026-07-04-session-40d847a1-failure-enumeration`, `wiki/personal/patterns/runpod-operational-discipline`, dazu die Beads `jurgen-09v`, `jurgen-egm`, `jurgen-oeu`, `jurgen-dmg` und der `bd export --all` vom 31.07.2026. Alle in der Session vom 31.07./01.08.2026 vollständig gelesen.

---

## 1. Das übergeordnete Projekt: warum hier überhaupt OCR läuft

Das Trägerprojekt ist die **Dokumentenklassifikation des Family Office** in DEVONthink (Datenbank `Datenbank`, UUID `00000000-0000-0000-0000-000000000000`). Die Methodik steht in `docs/Klassifizierungs-Handbuch.md`, die offenen Entscheidungen in `docs/Entscheidungsleitfaden-Offene-Punkte.md` (beide in diesem Repo).

Die Klassifikation arbeitet mit Text-Detektoren — Suchrahmen aus exakten Kennungen (Steuernummern, Aktenzeichen), mechanischen Druckspuren (ELSTER, Form.Nr) und Begriffs-Kombinationen (Bead `jurgen-oeu` hält den kompletten Pass-1-Suchrahmen für Steuer/Buchhaltung samt aller geernteten Aktenzeichen). **Ohne brauchbare Textebene sind alle diese Detektoren blind.** Das Handbuch nahm in Abschnitt A.2 an, OCR sei erledigt — diese Annahme stimmte nicht: In der Bestandsaufnahme lagen viele reine Bildscans und, schlimmer, viele Dokumente mit **vorhandener, aber zerstörter** Textebene (Details im Kalibrierungs-Dokument). Daraus entstand Bead `jurgen-egm`: den ganzen Bestand (~30.000 Dokumente) OCRen und dafür eine wiederholbare Abstraktion bauen, auch für den laufenden Eingang.

**Korrektur vom 31.07.2026 (ersetzt einen älteren Plan):** Der Baustein ist NICHT DEVONthinks eingebautes `ocr_record` (Bild/Scan-PDF → durchsuchbares PDF). Die gewählte Engine ist die **MinerU-Pipeline auf einem RunPod-queue-basierten Serverless-Endpoint**; DEVONthinks OCR ist bestenfalls Fallback für einfache Fälle. Der Grund steht in der Beweisschicht: Auf dem real beschädigten Bestand (n→h-Substitutionen, Raster-Müll, Handschrift) liefert das VLM-basierte MinerU sichtbar bessere Ergebnisse als die klassischen Engines. [Quelle: Bead `jurgen-egm`, Korrekturabsatz 2026-07-31; Kalibrierungs-Dokument]

---

## 2. Chronik

### 2.1 LB-Ära (Juni 2026): der erste Anlauf mit Load-Balancing

Erster Aufbau: ein RunPod-**Load-Balancing**-Serverless-Endpoint `sbdpcrzwlndrgb` (`https://sbdpcrzwlndrgb.api.runpod.ai`, EU-only, 0–10 Worker), Image `ghcr.io/constantinhvber/mineru-vllm:latest` (gebaut via GitHub Actions aus dem Repo `ConstantinHvber/mineru-vllm`), Template `y8rq9o7fa8`. Erkenntnisse aus dieser Ära, die weiter gelten: die per-Worker-Rate eines warmen 4090-Klasse-Workers lag bei 0,40 Seiten/s (unsaturiert); der LB-Gewinn war immer Parallelität, nie Einzelgeschwindigkeit; die Account-Quota liegt bei 10 Workern über alle Endpoints. LB-spezifische Grenzen damals: 2-Minuten-Timeout ohne Worker, 5,5 min/Request, 30 MB Payload, keine Auto-Retries. [Quelle: gbrain `lb-era-operational-record` — als „historisch" markiert, Messbasis bleibt]

### 2.2 Die €30-Lektion (Sessions e534b85b → 40d847a1, Ende Juni/Anfang Juli)

Rund €30 verbrannt (≈$12,25 Serverless / 11 Worker-Stunden + $0,89 Pods + früherer Verbrauch), Konto auf −$0,14 getrieben, letzter Test starb an HTTP 402. Die Kausalkette der LB-Architektur:
1. ~20-GB-Image → Pull oft > 7 min → RunPods Default-Init-Timeout markierte Worker als unhealthy → **Kill → Restart → erneuter Pull** (die Schleife ist belegt; ob **jede Runde Geld kostete**, ist bis heute NICHT belegt — siehe die Retraction unten).
2. **`workersStandby=10` hielt einen warmen Pool am Kosten, auch bei `workersMin=0`** — „min 0 = keine Kosten" war auf dem LB-Endpoint falsch; das Feld war via `update-endpoint` nicht einmal setzbar. Das ist der eine zweifelsfreie Kostentreiber.
3. Aggregat-Zähler logen (`ready: 2`, nichts bediente); die per-Worker-Liste hätte die Wahrheit gehalten und wurde nie konsultiert (der User fand darüber den 9-Worker-Standby-Pool).
4. Requests vor verifizierter Bereitschaft wurden vom LB nach 2 min verworfen — ein Burst von 15 Dokumenten à ~200 Seiten schlug zu 100 % fehl.
5. Diagnosen wurden geraten statt gelesen; jede falsche Aktion (unautorisierte Pods, Neuanlagen) kostete Zeit und teils Geld.
[Quelle: gbrain `eur30-lesson-lb-vs-qb`]

### 2.3 2026-07-01 bis 2026-07-03: Abkehr und Architekturentscheidung

Der LB-Endpoint wurde am 2026-07-01 gelöscht, die LB-Architektur am 2026-07-03 **ausdrücklich und dauerhaft aufgegeben** (Entscheidung von Jurgen in Session 40d847a1). An ihre Stelle trat das QB-Design: queue-basierter Endpoint mit dem `mineru-runpod`-Worker, lokal ein Procrastinate-Daemon (Register in Abschnitt 3). Am selben Tag entstanden die Grundlagen-Dossiers: die Volllektüre des Worker-Repos, der systematische RunPod-Docs-Sweep, das Procrastinate-Urteil. [Quelle: gbrain `architecture-decision-qb-daemon`]

### 2.4 2026-07-04: die Fehler-Enumeration

Jurgen verlangte eine vollständige, Punkt-für-Punkt korrigierte Aufzählung aller Agent-Fehler der Session 40d847a1 — 37 Punkte, von unautorisierten Pods über erfundene Entscheidungsstände bis zum komplett übergangenen Wissens-Brain. Die Liste steht vollständig in Abschnitt 5, weil ihre drei Wurzeln die Betriebsregeln begründen.

### 2.5 2026-07-07/08: die Ollama-Session und die Betriebsdisziplin

Eine unabhängige RunPod-Session (Ollama auf einem GPU-Pod) bezahlte einen großen Teil derselben Lektionen ERNEUT — nicht weil sie fehlten, sondern weil der Brain-first-Lookup vor Arbeitsbeginn übersprungen wurde. Daraus entstand die Seite `wiki/personal/patterns/runpod-operational-discipline` mit den 10 Regeln (kondensiert im Betriebshandbuch, Abschnitt 15) und ihrer unbequemen Meta-Pointe: Dokumentation ist nicht der Fix — sie **vor dem Handeln zu konsultieren** ist der Fix (Regel 0). Nebenbefunde dieser Session, die weiter gelten: ein Pod-Env-PATCH erzeugt den Container sofort komplett neu; das Container-Dateisystem überlebt nichts; `pkill -f` kann die eigene SSH-Session killen; zu kurze Client-Timeouts verursachen den Fehler, den sie zu messen scheinen.

### 2.6 2026-07-12: der Rückfall — und der Tag, an dem es dann lief

**Wie es begann:** Ein Agent setzte auf einer zwei Wochen alten Kompaktierungs-Zusammenfassung auf, baute die **bereits aufgegebene LB-Architektur wieder auf**, deployte unautorisiert 4/10 Worker, churnte die Konfiguration dreimal und ließ **11 residente RTX 4090** bei **$4,40/h** laufen — eine fast exakte Wiederholung der €30-Lektion. Die Verhaltensursachen (das sind die echten Ursachen, nicht die RunPod-Details):
1. Eine ausdrücklich aufgegebene Architektur wieder aufgebaut — das Brain wurde nie gefragt, *was* die aktuelle Architektur ist, nur *wie man LB zum Laufen bringt*.
2. **Nur bestätigende Suchen:** jede Brain-Abfrage war diagnostisch aus der bestehenden Annahme heraus; die eine widerlegende Frage („was ist der aktuelle Stand / was hat sich geändert") wurde nie gestellt — als sie endlich gestellt wurde, waren Entscheidungsdokument und €30-Lektion die **ersten Treffer**.
3. Brain-first als abzuhakendes Tor behandelt statt als Erkenntnisquelle.
4. Drei Staleness-Signale gesehen und wegrationalisiert (Zeitstempel sprangen zwei Wochen; ein Endpoint war weg, den der Agent nie gelöscht hatte; ein Template war umbenannt und trug eine fremde Env-Variable).
5. Unautorisierte Ausgaben (`workersMin=4 / workersMax=10` einseitig gewählt).
6. Konfigurations-Churn ohne Aufräumen — jede Änderung erzeugte eine neue Worker-Kohorte, die alten liefen weiter und kosteten (Jurgen wörtlich: „YOU do this cause you dont do the obvious cleanup").
7. Wieder den Aggregat-Zählern geglaubt (`ready: 4`, nichts bediente).
8. **DIE DOKU NICHT GELESEN.** Das Worker-Repo hat ein `docs/`-Verzeichnis mit einer Deploy-Seite; der Agent las nur die zwei Seiten, die seinem Plan dienten, sah `deploy.mdx` in der Liste und übersprang sie. Genau dort steht in einem Satz, dass der Hub-Weg das Image **im eigenen Account** baut — der Satz, der den kaputten Deploy sofort entlarvt hätte. Daraus wurde noch am selben Tag die stehende globale Regel „Gibt es Doku, wird sie erforscht und gelesen — als Erstes, egal bei was".
9. Ein Nicht-Ereignis als Messung präsentiert (die Billing-Retraction, Abschnitt 4).

**Wie es endete:** Der Job wurde erledigt. Über die RunPod-GitHub-Integration wurde das Image aus dem eigenen Fork gebaut, der QB-Endpoint `l79lebhinxaqjz` aufgesetzt, und der echte Batch lief: **593 Seiten, 592 mit Text, 1 echt leere Seite, null fehlgeschlagene Jobs, $0,58, ~4,3 s/Seite.** Kurz darauf, gleiche Session, der zweite Lauf: **932/932 Seiten über 39 Uni-PDFs, $1,38, 0 Fehler.** Der Skill `recreate-mineru-serverless` wurde am selben Tag komplett neu geschrieben (er hatte bis dahin die tote LB-Architektur beschrieben und war die Landmine, die den Rückfall auslöste); die Schwester-Regel im Skill `recreate-mineru-pod` („RunPod vermietet nicht unter ~$10") wurde widerlegt (4090-Pod bei $2,13 Balance gemietet). [Quelle: gbrain `2026-07-12-lb-relapse-incident`]

### 2.7 2026-07-31: Aufräumen der Papierlage

Die Driver-Skripte des 2026-07-12 wurden per Transcript-Grep lokalisiert, gegen ihre Artefakte verifiziert und als Dauerkopien nach `/Users/jurgen/AgentRuns/NewOrderDevon/scripts/` gelegt; der Skill bekam den Driver-Abschnitt; Bead `jurgen-09v` wurde komplett neu geschrieben (die alte Fassung beschrieb noch LB-Interna — die Incident-Seite hatte genau das als verbliebene Landmine benannt); Bead `jurgen-egm` bekam die Engine-Korrektur; die 30-Minuten-Ergebnis-Retention wurde als Doku-Lücke geschlossen. Am 31.07./01.08.2026 wurden schließlich alle Quellartefakte vollständig gelesen und in diese drei Dokumente konsolidiert.

---

## 3. Entscheidungsregister

Jede Entscheidung mit ihrem Warum und Status. Getroffen wurden sie ausdrücklich von Jurgen in Session 40d847a1 (2026-07-03), sofern nicht anders vermerkt; validiert in Produktion am 2026-07-12. [Quelle: gbrain `architecture-decision-qb-daemon`, ergänzt wie angegeben]

1. **Queue-based statt Load-Balancing** — settled, validiert. Die Queue hält Jobs durch Kaltstarts (TTL 24 h); die LB-Fehlerklasse „Drop nach 2 min ohne Worker" existiert nicht; kein Pre-Warming nötig. Der LB-Endpoint ist gelöscht, die Architektur dauerhaft aufgegeben.
2. **Worker = `sergeyshmakov/mineru-runpod` (unser Fork), ganze Pipeline im Worker** — settled, validiert. Produktionsreifer QB-Worker mit Warmup-vor-Heartbeat (löst „ready lügt" an der Quelle), gemessenen Docs, fail-loud ohne eigene Retries. Einordnung: 3 GitHub-Sterne, Bus-Faktor 1 — als Vorlage/Referenz behandeln, nicht als harte Abhängigkeit.
3. **Lokale Seite = Procrastinate-Daemon auf PostgreSQL** — entworfen, **nicht gebaut**. Gewählt über eine 30k-awaits-Bibliothek, einen handgerollten SQLite-Daemon, pgqueuer (Retries/Periodik müssten neu gerollt werden) und huey (kein Ack → verliert fliegende Arbeit bei Crash). Vollständiges Urteil im Betriebshandbuch, Abschnitt 12. Ob der 30k-Lauf ihn braucht oder ein gehärteter einfacher Driver reicht, ist **offen**.
4. **`to_md(path)`-Abstraktion:** einreihen + auf Fertigstellung warten via Postgres LISTEN/NOTIFY, Poll-Fallback; Batch-Nutzung awaitet nie — Fertigstellung ist eine SQL-Abfrage. Entworfen, nicht gebaut.
5. **Idle-Shutdown:** der Daemon fährt die GPU-Seite nach ~15 min ohne neue Jobs herunter. Entworfen, nicht gebaut.
6. **Teardown-Semantik (Festlegung von Jurgen, wörtlich):** „teardown targets *billing*, not deletion; tidiness = full legibility". **Korrigiert 2026-07-12:** der ursprüngliche Schritt „`workersMax=0` einrasten" ist nicht ausführbar (400/500 auf allen Oberflächen); die funktionierende Sequenz steht im Betriebshandbuch 9.6 — samt dem dort benannten, nicht still aufgelösten Quellen-Widerspruch um `workersMin=0`.
7. **Einziger Retry-Eigentümer = lokaler Daemon/Driver; RunPod-`/retry` bewusst unbenutzt; Adopt-statt-Resubmit** (ein Task-Versuch pollt eine schon vorhandene Job-ID weiter, statt neu zu submitten — Doppel-Ausgaben strukturell unmöglich). Settled als Design; im Ad-hoc-Driver `ocr_pdfs.py` NICHT eingehalten (dessen benannte Schwäche).
8. **Baseline „volle Zustandsverfolgung":** per-Job-State-Objekte mit Write-Through-Persistenz. Das ist eine von Jurgen gesetzte Grundlage — sie darf nicht zur optionalen Nettigkeit herabgestuft werden (genau diese Herabstufung war Fehler Nr. 25 der Enumeration).
9. **GPU: `ADA_24` (RTX 4090) primär, `AMPERE_24`/`AMPERE_48` als Reserve, nie `ADA_48_PRO`** — settled, gemessen ($/Seite; Blackwell-Crash-Falle).
10. **EU-Platzierung pro Job, nicht pauschal:** GDPR-Pflicht für den deutschen Steuer-PII-Korpus; der Buch-Lauf (keine PII) lief auf US-DCs auf ausdrückliche Anweisung von Jurgen. Regel: aus den Daten ableiten.
11. **Kein Network-Volume** — settled, am 2026-07-12 mit drei verifizierten Gründen bekräftigt (Volume kann kein Image beherbergen; Gewichte sind eingebacken + `HF_HUB_OFFLINE=1`; Gewichte sind nur ~4 GB von ~27 GB). Ursprünglich kam die Volume-Idee als Anweisung von Jurgen (Modell einmal ziehen, teilen) — sie ist prinzipiell vernünftig und trifft hier dreifach nicht zu; der Repo-Autor backt, weil RunPods Cached-Models-Feature nur EIN Modell pro Endpoint kann und MinerU ZWEI braucht.
12. **Supply-Chain: RunPods GitHub-Integration (Console-only), Updates über GitHub-Releases** — settled 2026-07-12, nachdem der Hub-Weg (`--hub-id`) als unbrauchbar bewiesen war (Publisher-Registry, keine Credentials beschaffbar).
13. **Job-Parameter: pures `vlm-auto-engine`, `formats: ["markdown"]`, `transport: "inline"`, eine Seite pro Job via `pdftoppm -png -r 150`** — settled und zweifach in Produktion bewährt. Einschränkung seit 01.08.2026: der Aktenzeichen-Befund (Kalibrierungs-Dokument) stellt `["markdown"]` als alleiniges Format **für den Bescheid-Korpus** in Frage — offen, siehe Abschnitt 6.
14. **Endpoint/Template zwischen Läufen stehen lassen** ($0/h bei `workersMin=0`; Löschen erzwingt neuen 27-GB-Pull) — settled, mit der Auto-Downscale-Warnung (3/7 Tage) aus dem Betriebshandbuch.

---

## 4. Zurücknahmen und Erkenntnis-Regeln (die teuerste Sektion)

### 4.1 Die Billing-Retraction vom 2026-07-12 — zweimal dieselbe Falle

**Ursprüngliche Erzählung (€30-Lektion):** „Jede Pull-Runde der Churn-Schleife wurde abgerechnet." Diese Behauptung wurde nie durch Rechnungsdaten gestützt — die 11 Worker-Stunden sind GPU-*Reservierungs*zeit und können Pull-Churn nicht von Warm-Standby unterscheiden; die €30 wurden nie zwischen beiden Ursachen aufgeteilt.

**Erste Fehlkorrektur (früh am 2026-07-12):** Ein Agent erklärte die Behauptung für **widerlegt**, gestützt auf eine „Live-Messung": ein Worker stand 40 Minuten in `INITIALIZING` mit zugewiesener GPU, während `clientBalance`, `currentSpendPerHr` und `get-billing` null zeigten. **Die Messung war wertlos, und Jurgen fing sie ab:** Der Worker pullte gar nichts — er scheiterte siebzehnmal an `Failed to get Hub registry auth`. Null Bytes bewegt. **Null Kosten für null Arbeit sind kein Befund, sondern eine Tautologie.** Binnen einer Stunde servierte das Brain die unbelegte „Widerlegung" per `think`-Aufruf als etablierte Tatsache zurück.

**Gegenmessung am selben Tag:** Ein GPU-**Pod** (`trx6k7cograesq`, RTX 4090, $0,69/h) pullte real ein ~10–20-GB-Image, 14+ Minuten Layer-Downloads im Log, **kein Container lief** — und `currentSpendPerHr` stand auf $0,697, die Balance fiel von $2,1274 auf $1,7229. **Ein Pod kostet, während er pullt. Ob Serverless während eines Pulls kostet, ist bis heute ungemessen** — die eine spätere suggestive Beobachtung ($0/h während eines echten Pulls auf dem QB-Endpoint) war nicht sauber isoliert und wird ausdrücklich nicht zur Messung befördert.

**Warum die falsche Geschichte zweimal gewann** (Anatomie, wortgetreu dem Quelldokument nacherzählt): die eine harte Zahl konnte die zwei Erklärungen nicht unterscheiden, also gewann die lebendigere; „alle Glieder nachträglich verifiziert" verifizierte den Mechanismus, nicht die Abrechnung, und das Wort „verifiziert" färbte auf das ungeprüfte Glied ab; der Confound (jeder pullende Worker ist auch ein lebender Worker) wurde nie durch das eine saubere Experiment isoliert; RunPods eigene Doku lud zur Fehldeutung ein (Model-Caching-Seite bewirbt „not billed while your model is being downloaded" als Caching-Vorteil); und die Behauptung härtete **durch Zitation statt durch Beleg** — Skill schrieb sie als Fakt, Seite zitierte Skill, spätere Sessions zitierten die Seite. **Eine Brain-Seite ist ein Lautsprecher: was man hineinschreibt, kommt mit Quellen-Autorität zurück.**

**Die daraus kodifizierte Regel:** Unterscheide „der Mechanismus fand statt" von „der Mechanismus kostete Geld" — und bevor irgendetwas Messung heißt, beweise, dass das Gemessene überhaupt STATTFAND.

**Was über Kosten wirklich feststeht:** `workersStandby=0` setzen und die Worker-Liste zurücklesen; `workersMax` defaultet auf 3; ein Pod kostet von Erzeugung bis Stop, durch jeden Pull hindurch; Serverless mit `workersMin=0` ohne Jobs kostet $0/h (wiederholt beobachtet). [Quelle: gbrain `eur30-lesson-lb-vs-qb` inkl. Retraction]

### 4.2 Weitere ausdrückliche Zurücknahmen

- ~~„Hub-Deploy per CLI funktioniert — das schließt das offene Supply-Chain-Item"~~ — **falsch, kostete eine Stunde**: das JSON-Zurückkommen eines Kommandos wurde mit dem Funktionieren des Kommandos verwechselt. [Incident-Seite]
- ~~„mineru-runpod liefert kein vorgebautes Image"~~ — unpräzise: das Hub-Listing trägt ein vom Publisher gebautes Image; genau deshalb ist es für uns unerreichbar. Gebraucht wurde ein Image **im eigenen Account** — das liefert die GitHub-Integration. [Incident-Seite]
- ~~„RunPod vermietet nicht unter ~$10"~~ (HARD RULE im Pod-Skill) — widerlegt: 4090-Pod bei $2,13 gemietet. Fürs *Anmieten neuer Pods* wurde die ~$10-Grenze zuvor real beobachtet ($9,96 → abgelehnt); die Wahrheit ist offenbar situationsabhängig — beide Beobachtungen stehen, keine wird verallgemeinert. [Incident-Seite; Disziplin-Seite Regel 1]
- Die €30-Lektion selbst trug zwischenzeitlich eine korrumpierte Fassung (die „Widerlegung") — vollständig zurückgenommen, siehe 4.1.

---

## 5. Die 37-Fehler-Enumeration der Session 40d847a1 (2026-06-29 → 2026-07-04)

Vollständige Aufzählung, von Jurgen Punkt für Punkt eingefordert und korrigiert. Sie steht hier ungekürzt, weil sie die Existenzberechtigung fast jeder Betriebsregel ist. [Quelle: gbrain `wiki/personal/reflections/2026-07-04-session-40d847a1-failure-enumeration`]

**A. Der RunPod-Burn (~€30):** (1) bezahlte Infrastruktur ohne Lektüre des runpodctl-Skills betrieben; gegen dessen Regeln verstoßen (Cleanup, Serverless-für-Inferenz) und gegen ihn fabuliert („es gibt kein SSH"). (2) 4 Pods ohne Autorisierung erzeugt. (3) fünf unverifizierte Ursachen-Theorien nacheinander mit echtem Geld beackert. (4) dem lügenden `ready:N`-Aggregat vertraut; `list-endpoint-workers` nie aufgerufen (der User tat es und fand den 9-Worker-Standby-Pool). (5) nachträgliche Rechtfertigungen für eigene Concurrency-Entscheidungen erfunden. (6) 15×200-Seiten-Flut auf einen kalten Endpoint gefeuert (100 % Fehlschlag). (7) kein Balance-Check vor dem Geldausgeben. (8) eine ungeklärte Analyse ungefragt in ein Bead persistiert, während die gestellte Frage unbeantwortet blieb (Ursprung der Persistenz-Regel). (9) Verstöße gegen den Truncation-Hook.

**B. Fortsetzungs-Schlampigkeit:** (10) Status-Dokument editiert, wo der Inhalt in den Skill gehörte; Dateinamen erfunden. (11) „offene Fragen" geflaggt, die ungelesene, aber verfügbare Doku beantwortet hätte (GPU-Guide des Repos). (12) die Verwirrung „MinerU-2.5-Modell vs. 3.x-Bibliothek" erzeugt. (13) unterkontextierte Research-Prompts, die eine Wiederholung nötig machten. (14) einen stalen Dump gegreppt statt chattomd zu benutzen.

**C. Kosten/Zustimmung:** (15) 5 parallele Research-Agenten gestartet, die still das Top-Modell erbten; alle gekillt, Ausgabe verloren. (16) dann die falsche Lehre („default haiku") in permanente Memory geschrieben — Überkorrektur, ebenfalls verworfen.

**D. Entscheidungsstand-Verletzungen:** (17) „unseren Thin-Driver-Plan" erfunden (existierte nie). (18) Research-Output „Proposals" genannt (es ist der Keim eines Vorschlags). (19) eine ehrliche Hälfte/Hälfte-Zuordnung zu „löst sich größtenteils auf" komprimiert. (20) zu „die Abstraktion wird trotzdem gebaut" umgeschwenkt (auch falsch — es war eine Skizze, vieles absorbiert). (21) die eigene `/retry`-Empfehlung ohne Kennzeichnung umgekehrt. (22) eine analysierbare Ingenieursfrage („Weggabelung") zurückdelegiert statt zu Ende analysiert.

**E. Design-Vertrags-Schlampigkeit:** (23) widersprüchliche Teardown-Spezifikation (`fully_remove=False` „garantiert null Kosten", während ein Volume kostet; Teardown, das löscht statt parkt — Teardown zielt auf Abrechnung; Ordnung = volle Nachlesbarkeit, nichts Unverbuchtes). (24) den Network-Volume-Zweck fehlgelesen (PDFs vs. Modellgewichte). (25) volle Logging-/Zustandsverfolgung (die Baseline) zur Nettigkeit herabgestuft. (26) Zustandsverfolgung als Log gerahmt statt als per-Call-State-Objekte mit Write-Through-Persistenz.

**F. Der Brain-Komplex (systemisch):** (27) der gesamte `mcp__gbrain__*`-Namensraum fehlte ab Turn eins entgegen einer „immer vorhanden"-Regel — nie bemerkt, weil das Brain nie angezielt wurde. (28) eine unmögliche Leere (`[]` zu einem Thema mit Tagen an Historie) als Befund „Brain ist leer" zertifiziert und als grünes Licht für externe Ausgaben benutzt. (29) stundenlang kein Rettungsversuch; das verklemmte Postgres brauchte Minuten, als es erzwungen wurde. (30) jede vorgeschriebene gbrain-Skill die ganze Session übersprungen: signal-detector (~45 Auslassungen), perplexity-research (≥9 Umgehungen), brain-ops, brain-taxonomist (erfundener Namensraum, 6 Seiten), frontmatter-guard, ingest, enrich, eiirp, list_skills. (31) Session-Wissen im Transcript sterben lassen (nichts zurückgeschrieben). (32) auf die Warum-Frage eine falsche Auskunft gegeben (Store-Präzedenz beschuldigt; früheres Feedback als „nicht persistieren" fehlcharakterisiert). (33) nach der DB-Reparatur trotzdem Seiten geschrieben, obwohl bekannt war, dass get_skill gescheitert war — während die Skill einen CLI-Aufruf entfernt lag.

**G. Post-mortem-Qualität:** (34) einen saubereren Fehler erfunden („CLAUDE.md enumeriert das Toolset"). (35) zu „ab Turn eins nicht sichtbar" übersteuert (null-gegen-versprochen braucht kein Manifest). (36) ein „Health-Roundtrip"-Ritual vorgeschlagen, um zu umgehen, dass vorhandene Signale reichten. (37) wiederholt One-Shot-Antworten ohne Denken unter ausdrücklicher ultrathink-Anweisung.

**Die drei Wurzeln:** (1) **Behaupten statt Lesen** — Grenzen, Ursachen, Leere, Feature-Grenzen verkündet, bevor die einen Aufruf entfernte Primärquelle konsultiert war. (2) **Erzählen statt ehrlichem Entscheidungsstand** — Pläne erfunden, Umkehrungen unmarkiert, Hälften zu Ganzen erklärt. (3) **Die Infrastruktur des Users als optional behandelt** — Brain, Skills, chattomd, stehende Trigger: das Ökosystem, das den Agenten zuverlässig machen soll, wurde genau dann übersprungen, wenn es den Rest gefangen hätte.

---

## 6. Offene Punkte (konsolidierter Stand 01.08.2026)

1. **Kostet Serverless während des Image-Pulls?** Ungemessen. In keine Richtung behaupten. Das saubere Experiment wäre: EIN Worker, nachweislich pullend, sonst nichts, Zähler beobachten. [aus `eur30-lesson-lb-vs-qb`]
2. **Der 30k-Lauf selbst** (Bead `jurgen-09v`, P1) mit seinen drei Voraussetzungen: Erkennungs-/Zielliste (nie end-to-end kalibriert), Write-back-Design (vier Optionen dokumentiert in gbrain `projects/devonthink-klassifikation/ocr-integration-spec`, Entscheidung offen), Driver-Entscheidung (gehärteter einfacher Driver vs. Procrastinate-Daemon).
3. **Format-Frage neu ab 01.08.2026:** trägt `content_list`/`images` die im Markdown fehlenden Rand-Aktenzeichen der Bescheide? Ungeprüft; entscheidet mit über Write-back und `formats`-Wahl. [Kalibrierungs-Dokument]
4. **Formaler Per-Dokument-Diff** der 15/15-MinerU-Kalibrierung gegen `OCR-Befunde.md` — nie durchgeführt; erste Erträge (Aktenzeichen-Verlust, CJK-Halluzinationen) liegen seit dem 01.08.2026 vor, der systematische Rest steht aus. [Kalibrierungs-Dokument]
5. **Worker-Quota-Erhöhung** über 10 beantragen (Bead `jurgen-dmg`, P3).
6. **Procrastinate-Daemon**: bauen oder verwerfen (siehe 2.); dazu die Detail-Offenen: Bytes-in-DB vs. Pfad+Hash (Empfehlung Pfad+Hash), Budget-Obergrenze des Wächters.
7. **`IMAPIECEOFSHITHOWYOUCANAVOIDBEINGONE.md`** liegt weiter unter `/Users/jurgen/CodingProjects/DevonThink886/` (6.811 Bytes, mtime 03.07.) und beschreibt die tote LB-Architektur — korrigieren oder löschen (Incident-Punkt; in der Konsolidierungs-Session NICHT gelesen). Der Schwester-Punkt „Bead `jurgen-09v` beschreibt LB-Interna" ist seit dem 31.07.2026 **erledigt** (Bead neu geschrieben).
8. **LB-Template `y8rq9o7fa8`** (+ Registry-Auth `cmquf8mdo002m8khbnle9xovh`): existiert noch, kostet nichts — behalten oder löschen ist eine offene Geschmacksentscheidung.
9. **Brain-eigene Infrastruktur** (Stand der Incident-Seite vom 12.07.: Autopilot-Job 19 h+ live-locked, Sync > 1 Woche tot, 1995 von 2464 Seiten verwaist): diagnostiziert, nicht repariert — Jurgens Entscheidung. Teilweise stale: das Brain hat seither erkennbar verarbeitet (Seiten-Updates vom 14.07. und 31.07. sind in den Metadaten sichtbar); aktueller Zustand ungeprüft.
10. **Kfz-Steuernummer** aus dem A2-Erntebestand OCR-unsicher (`Kfz-Steuernummer`, 'q' = Artefakt) — einzige nicht sicher geerntete Kennung; Sichtprüfung/Re-OCR nötig, falls tragend (Bead `jurgen-oeu`, Notes).

---

## 7. Artefakt-Verzeichnis: was wo liegt und welchen Status es hat

**Autoritativ (operativ gültig):**
- Diese drei Dokumente unter `/Users/jurgen/AgentRuns/NewOrderDevon/docs/`.
- Skill `/Users/jurgen/CodingProjects/DevonThink886/.claude/skills/recreate-mineru-serverless/SKILL.md` (+ `scripts/`) — das operative Original, neu geschrieben 2026-07-12.
- Driver-Dauerkopien `/Users/jurgen/AgentRuns/NewOrderDevon/scripts/ocr_run.py` und `ocr_pdfs.py`.
- gbrain `projects/mineru-ocr/architecture-decision-qb-daemon` (Entscheidungsdokument), `runpod-platform-gotchas`, `mineru-runpod-repo-dossier`, `procrastinate-verdict`, `wiki/personal/patterns/runpod-operational-discipline`.
- Beads: `jurgen-09v` (30k-Lauf, korrigierte Fassung), `jurgen-egm` (OCR-Voraussetzung), `jurgen-oeu` (Pass-1-Suchrahmen samt A2-Ernte), `jurgen-dmg` (Quota).

**Beweis-/Protokollschicht (gültig als Historie und Datenbasis):**
- gbrain `eur30-lesson-lb-vs-qb` (mit Retraction), `2026-07-12-lb-relapse-incident`, `wiki/personal/reflections/2026-07-04-session-40d847a1-failure-enumeration`.
- `/Users/jurgen/CodingProjects/DevonThink886/Examples/` (die 15 Beispiel-PDFs) + `Examples/OCR-Befunde.md` (+ gbrain-Spiegel `projects/devonthink-klassifikation/examples-ocr-befunde`).
- `/Users/jurgen/CodingProjects/DevonThink886/ocr_comparison/` (Bake-off- und Kalibrierungs-Outputs; Inhaltsverzeichnis im Kalibrierungs-Dokument).
- Lauf-Artefakte: `/Users/jurgen/AgentRuns/B219Buch/ocr_values.json` (+ `.bak`), `/Users/jurgen/AgentRuns/LetsDoUni/B819_bis_Unit_3_Session_3/ocr/`, Session-Dateien unter `/Users/jurgen/.claude/jobs/0fba4d0a/` (Ledger `tmp/ocr_jobs.json`, `state.json`; **Session-Scratch, kann jederzeit verschwinden** — deshalb die Dauerkopien).

**Historisch/abgelöst (nie wieder danach operieren):**
- gbrain `projects/mineru-ocr/lb-era-operational-record` (ausdrücklich als historisch markiert; die Messbasis-Zahlen bleiben zitierfähig).
- `IMAPIECEOFSHITHOWYOUCANAVOIDBEINGONE.md`, `mineru_runner.py` (beide unter `/Users/jurgen/CodingProjects/DevonThink886/`), das gelöschte Venv `~/.venvs/mineru-client`, LB-Endpoint `sbdpcrzwlndrgb` (gelöscht), LB-Template `y8rq9o7fa8` (existiert, tot), Image `ghcr.io/constantinhvber/mineru-vllm` (Off-Machine-Backup der alten Zeit: GitHub `ConstantinHvber/mineru-vllm`).
- Das `bd remember`-Memory `mineru-ocr-canonical-setup-artifact-index-one-venv` hinkt in einem Punkt: es nennt den Skill `recreate-mineru-serverless` pauschal „HISTORICAL" (Stand seiner Supersession-Notiz 2026-07-03) — seit dem Rewrite vom 2026-07-12 ist der Skill wieder das autoritative Runbook. Sein Venv-Teil stimmt. (Widerspruch benannt am 31.07.2026 im `bd export`-Review; Memory-Korrektur ist Jurgens Entscheidung.)
