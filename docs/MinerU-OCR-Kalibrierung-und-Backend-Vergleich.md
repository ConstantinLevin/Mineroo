# OCR-Qualität am echten Bestand: Beispielset, Backend-Vergleich und MinerU-Kalibrierung

**Stand:** 01.08.2026.
**Zweck:** Dieses Dokument ist die Beweisschicht des OCR-Projekts. Es hält fest, (1) wie der beschädigte Dokumentenbestand konkret aussieht (das 15-Beispiel-Kalibrierset mit wörtlichen Müll-Zitaten), (2) was lokale OCR-Backends darauf leisten (der Kreuzberg-Bake-off vom 25.06.2026 samt seiner Integritätsfallen), (3) was die gewählte MinerU-Pipeline darauf leistet (die 15/15-Kalibrierung, Outputs unter `ocr_comparison/_mineru_runpod_vllm/`), und (4) welche Qualitätsfragen offen sind. Wer entscheidet, welche Dokumente OCR brauchen (Zielliste) oder wie der Text zurück nach DEVONthink kommt (Write-back), entscheidet auf dieser Basis.
**Schwesterdokumente:** [MinerU-RunPod-Betriebshandbuch.md](MinerU-RunPod-Betriebshandbuch.md) (das Wie des Betriebs), [MinerU-RunPod-Geschichte-und-Entscheidungen.md](MinerU-RunPod-Geschichte-und-Entscheidungen.md) (das Warum).

**Quellen** (Session vom 31.07./01.08.2026, vollständig gelesen soweit nicht anders vermerkt):
- `/Users/jurgen/CodingProjects/DevonThink886/Examples/OCR-Befunde.md` (das Kalibrierset; gespiegelt als gbrain `projects/devonthink-klassifikation/examples-ocr-befunde`, Quelle dort `file:///Users/jurgen/CodingProjects/DevonThink886/Examples/OCR-Befunde.md`, erfasst 03.07.2026).
- `/Users/jurgen/CodingProjects/DevonThink886/ocr_comparison/`: `SUMMARY.md` (25.06.2026), `_metrics.md`, `_recovery.md`, `results.jsonl` (147 Läufe), `gemini_truth.sh`, `_examples_minerutest/_failures.log`, `_examples_retest/_failures.log`, alle 15 Markdowns unter `_mineru_runpod_vllm/out_all/*/vlm/*.md`, das Markdown unter `_mineru_baked_test/`, alle 15 Dateien unter `candle-trocr/`.
- Eigene Prüfungen dieser Session (jeweils als solche markiert): byte-Vergleiche per `diff`, Muster-Suchen per `grep`.

**Rohdaten-Bestände (Beweismaterial, keine Wissensquellen — muss niemand lesen):** Unter `ocr_comparison/` liegen außerdem die Roh-OCR-Dumps je Backend (`candle-glm-ocr/`, `easyocr/`, `paddle-ocr/`, `tesseract/`, `textlayer/`, `_unavailable/`, `candle-deepseek-ocr/`, `candle-hunyuan-ocr/`), die beiden Friendli-`_run.log`, die stderr-Logs unter `_logs/` sowie Bild-/PDF-/Layout-JSON-Artefakte. Das sind die vermessenen Gegenstände, nicht die Messungen: ihr gesamter Erkenntnisgehalt steckt in der oben gelesenen Urteils-Schicht (SUMMARY/_metrics/_recovery/results.jsonl) und den Byte-Vergleichen dieser Session und ist in diesem Dokument konsolidiert. Gebraucht werden die Dumps nur noch, falls jemand Scores neu berechnen oder anzweifeln will (Beweissicherung — deshalb liegen sie noch da). Die Friendli-`_run.log`s wurden am 01.08.2026 per Fehlerzeilen-Extraktion nachdiagnostiziert und ihr Befund ist in Abschnitt 4 konsolidiert (Endpoint-Nichtverfügbarkeit, kein MinerU-Qualitätsbefund).

---

## 1. Das Kernproblem, das alles hier begründet

Fast alle harten Fälle des Bestands haben eine **vorhandene** Textebene (`kind: PDF+Text`, `word count > 0`) — das naive Signal „word count == 0" winkt sie als „hat Text" durch. **Trotzdem ist der Text Müll.** Dazu kommt: DEVONthink indexiert den zerschossenen Text oft **gar nicht** — die härtesten Fälle (Handschrift, Punkt-Insertion, Fremdsprache) findet man nicht per Müll-Stichwortsuche, nur per Volltext-Lesen. Und: Bei den Finanzamt-Bescheiden überleben die **Aktenzeichen-Ziffern** den OCR-Schaden, während die Wörter zerfallen — Ziffern sind der robusteste Fang für beschädigte Behördenpost. [Quelle: OCR-Befunde.md, Kopfabschnitt]

Konsequenz für die Klassifikation (Bead `jurgen-oeu`): die als am stärksten eingestuften Suchterme sind exakte Kennungen — und die Erkennungs-Frage „welche Dokumente brauchen OCR" ist nicht mit einem Wortzähler beantwortbar (deshalb die entworfene, nie kalibrierte klassenweise Defekt-Jagd, gbrain `wiki/originals/ideas/2026-06-25-ocr-defect-calibration-compartments-43f26d`).

---

## 2. Das 15-Beispiel-Kalibrierset (vollständig)

Alle Dateien sind aus DEVONthink (`Datenbank`) exportierte Kopien unter `/Users/jurgen/CodingProjects/DevonThink886/Examples/`; Originale unverändert. Format je Eintrag: `Müll-Snippet` → *(soll heißen)*. [Quelle: OCR-Befunde.md, hier vollständig übernommen, damit dieses Dokument allein trägt]

### Klasse: Finanzamt-Bescheide, n→h-Substitution (total)

**2017_Privat_Grundsteuer_Bescheid_Einheitswert_Musterberg.pdf** — UUID `00000000-0000-0000-0000-000000000000` · /Steuer/Grundsteuer/ · FA Beispielberg, Einheitswertbescheid 2017
- `F'ihahzamt Beispielbepg` → *Finanzamt Beispielberg*
- `Eihheitswept 14.827 €` → *Einheitswert*
- `Aktehzeicheh` → *Aktenzeichen* (Ziffern `123/456/7890/012/345/7` dagegen sauber)
- `Grundsteuepmessbescheid Neuveranlaguhg` → *Grundsteuermessbescheid Neuveranlagung*

**2022_Privat_Grundsteuer_Bescheid_Hauptfestellung_MusterhofHaus.pdf** — UUID `00000000-0000-0000-0000-000000000000` · /Steuer/Grundsteuer/ · FA Musterstadt, Äquivalenzbeträge-Hauptfeststellung 2022
- `rinahzant rnustcrstadt` / `F[ihahzaht Musterstadt` → *Finanzamt Musterstadt*
- `Crundsteueräquivalehzbeträge Ilauptfeststelluhg auf deh 1.1.2022` → *Grundsteueräquivalenzbeträge Hauptfeststellung auf den*
- `Art. 3 Abs. 1 Nr. 1 Bay6rstc` → *BayGrStG*
- `XK4000at "07¥ZZ3t00 3ZI Z3( iiHH` → *(Barcode-Zone als Textsalat)*

**2022_Privat_Grundsteuer_Bescheid_Hauptfestellung_HausMusterberg.pdf** — UUID `00000000-0000-0000-0000-000000000000` · /Steuer/Grundsteuer/ · FA Beispielberg, Äquivalenzbeträge 2022
- `F'inahzamt Beispielberg` / `PirLarLzamt Beispielbepg` → *Finanzamt Beispielberg*
- `Grundsteueräquivalehzbeträge Hauptfeststelluhg` → *…Hauptfeststellung*
- `Bayerischen Grundst®uerges®tz (Bay6rstc)` → *Grundsteuergesetz (BayGrStG)*
- `Cruhdsteuermessbetrag Hauptverahlaguhg` → *Grundsteuermessbetrag Hauptveranlagung*

**2021_KG_Grundsteuer_Bescheid_Einheitswert_Musterstraße_ETW1.pdf** — UUID `00000000-0000-0000-0000-000000000000` · /Steuer/Grundsteuer/ · FA München, Einheitswert-Nachfeststellung 2021
- `rinarzamt Hünchen` → *Finanzamt München*
- `Aktehzeicherl 234/567/8901/234/567/8` → *Aktenzeichen* (Ziffern sauber)
- `Nachfeststelluhg auf deh 1.1.2021` → *Nachfeststellung auf den*
- `Z::#:?n!8Zg 3` → *(Adressfeld komplett zerschossen)*; Rechtsbehelfsseiten in reinem Raster-Müll

**2022_Privat_Grundsteuer_Bescheid_Hauptfestellung_BeispielStraße_WHG1.pdf** — UUID `00000000-0000-0000-0000-000000000000` · /Steuer/Grundsteuer/ · FA München, Äquivalenzbeträge 2022
- `Fihahzabt Nüncheh` → *Finanzamt München* (doppelt korrupt: n→h **und** m→b)
- `Alctehzeichen 345/678/9012/345/678/9` → *Aktenzeichen*
- `iiber die Cruhdsteueräquivalenzbeträge Hauptfeststelluhg auf deh 1.1.2022` → *über die Grundsteueräquivalenzbeträge…*
- `14 pso patExchnlpagocoananapsuchs notraptoanon Honat.` → *(Einspruchsfrist-Satz vollständig Kauderwelsch)*

### Klasse: andere Steuerarten (total)

**Grunderwerbsteuer Muster 1.pdf** — UUID `00000000-0000-0000-0000-000000000000` · /_NochUnsortiert/Immobilien/Musteranwesen/ · FA Musterstein, Grunderwerbsteuerbescheid
- `Vertragspartner-Gesellschaft` → *Erwerber-Gesellschaft* — **zu diesem Eintrag siehe den benannten Widerspruch in Abschnitt 5.4**
- `BLATT001234` / `XB16x14X001234*` → *(Barcode/Blattnummer als Text im Adressfeld)*
- `Rt. 06.10.2016 GrESt` → *(Eingangsstempel)*

**2016_Privat_Bescheid_Einkommensteuer.pdf** — UUID `00000000-0000-0000-0000-000000000000` · /Steuer/Privat/Bescheide/ · FA Beispielberg, Einkommensteuer 2016
- `Bescheid fiEp =016` → *Bescheid für 2016* (Jahreszahl zerschossen)
- `B:eispielbei]g` / `Finanzamt Beispiolberg` → *Beispielberg / Finanzamt Beispielberg*
- `ä=:::=E::gtLe::=u::::P::=±::d:` → *(ganze Seiten in Raster-Müll aufgelöst)*

### Klasse: Handwerker-Rechnungen / Formulare (total, eigene Fehlerprofile)

**Kellerbau_Schlussrechnung-Einmessung.pdf** — UUID `00000000-0000-0000-0000-000000000000` · /_NochUnsortiert/Immobilien/Musterhof/Musterhof_Unterlagen/ · Muster Kellerbau GmbH, Schlussrechnung
- **Fehlerprofil: Punkt-Insertion** — `Am Wie.sen 12`, `12345 Bei.spi.el` → *Wiesen / Beispiel* (Serifen als Satzzeichen gedeutet)
- `IBAN: DE00123456780001234S6789` → *…1234**5**6789* (S statt 5 — Bankdaten unbrauchbar)
- `W&ldweg l2 1S345 Musterdorf` → *Waldweg 12, 12345 Musterdorf*
- `S \`# €- i, - =\`-+ - - i =- =` → *(Trennlinie als Zeichensalat)*

**492064 - Bauplanung Musterplan, Doppelgarage.pdf** — UUID `00000000-0000-0000-0000-000000000000` · /_NochUnsortiert/Immobilien/Musterhof/ · Bauplanung Musterplan, Rechnung
- **Fehlerprofil: Handschrift + Druck-Formular kollabieren ineinander** — `Staat gepr Bautechrifter unt Zimmera méster` → *staatl. gepr. Bautechniker und Zimmermeister*
- `VR Bnuspielsiadt` → *VR Beispielstadt* (Bank-Wasserzeichen als Text erkannt)
- `Baupiarung Mus terp lan'` → *Bauplanung Musterplan*

### Klasse: reine Handschrift (total — OCR-Totalversagen)

**Notizen1.pdf** — UUID `00000000-0000-0000-0000-000000000000` · /_NochUnsortiert/NochUnzuordnbar/Notizen/ · handgeschriebene persönliche Notizen
- **Fehlerprofil: kursive Handschrift durch Drucktext-OCR — kein Substring-Muster, komplettes Lexikon-Versagen**
- `Unthaltskaste landwitshoftlich Fläch` → *Unterhaltungskosten landwirtschaftliche Fläche*
- `Aufjabn Liste Kcifutey` → *Aufgaben-Liste …*
- `Mait an Mustcrbau` → *Mail an Musterbau*
- `Wehen Reing urmich → Kamhan?` → *Wegen Reinigung …*

### Klasse: Fremdsprache

**img20250722_18475997.pdf** — UUID `00000000-0000-0000-0000-000000000000` · /Inbox/Steuer/2025 Umst-Voranmeldung Quartal2/Ausgaben/5. Mai 2025/ · französische SANEF-Mautquittungen
- **Fehlerprofil: fremdsprachiger Text, Zeichensatz-Mismatch + Zahlenfehler**
- `EXENPLE LUNSEIL` → *EXEMPLE CONSEIL*
- `Date 19/05/7076 17:07` → *…/2025…* (Jahr 7076 statt 2025)
- `SIRET 123 uoü 456 00012` → *(Sonderzeichen ü in Ziffernfolge)*

### Klasse: Bank / Versicherung / Notar (mittel–leicht)

**HVB_April2024.pdf** — UUID `00000000-0000-0000-0000-000000000000` · /_NochUnsortiert/NochUnzuordnbar/_Abgeschlossen/Musterakte/ · HVB/UniCredit Selbstauskunft
- `® Hypwereinsbank ME„,LX"f üunicrdit` → *HypoVereinsbank … UniCredit*
- `S E L B S T A U S 1{ U N F T` → *SELBSTAUSKUNFT* (Sperrdruck mit Ziffereinschub)
- `Bifte fügen Sie lhren Angaben` → *Bitte fügen Sie Ihren Angaben*; Ort `Beispielorf` → *Beispielort*

**20161031144820006.pdf** — UUID `00000000-0000-0000-0000-000000000000` · /Bankwesen/AllianzDepot/ · Allianz ParkDepot, Eröffnungsantrag
- Adressblock total zerhackt: `MUSTER INMOBLLIEN / ATSH TCOLIGL / AM WIIESEN 121 / 1,2,345-BEEISPIIEI` → *Erwerber-Gesellschaft … Strassenangabe / PLZ Ort*

**Handelregistereintragung_Urkunde_KG.pdf** — UUID `00000000-0000-0000-0000-000000000000` · /_NochUnsortiert/WeitereVerwaltung/2Erwerber-Gesellschaft/ · Notar Muster, Musterheim, Handelsregister-Anmeldung
- `Amtsgericht Musterstein — F\`egistergericht` → *Registergericht*
- `_cjmKcmmand itgese üsc haft e.[richtet.` → *…Kommanditgesellschaft errichtet.*
- `Must®r lmmobill®n QmbH & Co. Ka` → *Muster Immobilien GmbH & Co. KG* (® statt e durchgehend)

**20161116150357250.pdf** — UUID `00000000-0000-0000-0000-000000000000` · /Darlehen/KG_VR_12345678_1000TE/ · VR-Bank, Darlehensvertrag (Entwurf)
- `Steuamummer baw. Umsatzstever-identifikationsnumner` → *Steuernummer bzw. Umsatzsteuer-Identifikationsnummer*
- `Darishensnehme:` → *Darlehensnehmer*; `Soltzinssatz` → *Sollzinssatz*
- `ー` → *(japanisches Katakana-Zeichen — aus dem ENTWURF-Wasserzeichen fehlinterpretiert)*

---

## 3. Der Kreuzberg-Backend-Bake-off (25.06.2026)

**Setup:** kreuzberg `5.0.0-rc.32` (Homebrew, nativer Rust-Build, `/opt/homebrew/bin/kreuzberg`); Aufruf `kreuzberg extract <pdf> --ocr true --force-ocr true --ocr-backend <B> --ocr-language <L> --format text`; tesseract mit `deu` (tessdata_best, manuell installiert unter `/opt/homebrew/share/tessdata/deu.traineddata`), paddle-ocr mit `german` (Latin-Familie, Mobile-Tier). Erst 3 Grundsteuer-PDFs (Bead `jurgen-0cu`), dann auf alle 15 Beispiele ausgeweitet. [Quelle: SUMMARY.md]

### 3.1 Der Integritätsbefund, der alles andere rahmt: nur 3 Backends waren echt

Das `--ocr-backend`-Argument akzeptiert 9 Namen, aber die Laufzeit-Registry meldete nur drei: `tesseract`, `paddle-ocr`, `vlm`. **Stiller-Fallback-Falle:** Ein nicht registriertes Backend erzeugt KEINEN harten Fehler, sondern fällt still auf die Pipeline `tesseract → paddle-ocr` zurück und meldet „ok=True". Beleg per `--no-cache`: `Pipeline: backend failed, trying next  backend=tesseract  error=... Failed to download language pack 'de' ... http status: 404` → danach paddle-ocr → Ausgabe. Die Schein-Ergebnisse liegen unter `_unavailable/` (zur Beweissicherung nicht gelöscht). [Quelle: SUMMARY.md §1]

**Eigene Verifikation (Session 31.07./01.08.2026, `diff`):** Alle 15 Dateien in `candle-hunyuan-ocr/` und alle 3 in `candle-deepseek-ocr/` sind **byte-identisch** mit den gleichnamigen in `paddle-ocr/`. ⇒ Die `_recovery.md`-Spalten „candle-hunyuan-ocr 76%" und „candle-deepseek-ocr 100%" messen in Wahrheit den Paddle-Fallback, nicht diese Modelle; die „100%" beruhen zudem auf nur 3 Dokumenten (Rest „–"). Später liefen glm/trocr offenbar doch als echte Candle-Backends (results.jsonl zeigt eigene Laufzeiten und winzige, andersartige Outputs) — deren Qualität ist mit 3 % Recovery ohnehin indiskutabel.

### 3.2 Die eingebettete Textebene ist DEFEKT — force-OCR ist Pflicht

Kaputtes ToUnicode/Font-Encoding liefert Mojibake (`F'inahzamt`, `£quivalehzbeträge`, `Must®rb®rg`, ganze Zeilen `£ä::::I::z!`). Messwerte (Mojibake = Encoding-Artefakte; Junk-Zeilen = Zeilen mit 3+ Doppelpunkten; niedriger = sauberer):

| Dokument | Backend | Mojibake | Junk-Zeilen | Alpha-Token |
|---|---|---|---|---|
| 2017 Einheitswert | textlayer | **86** | **33** | 1036 |
| 2017 Einheitswert | tesseract | 2 | 1 | 1390 |
| 2017 Einheitswert | paddle-ocr | 0 | 1 | 1374 |
| 2022 Musterhof | textlayer | **188** | **33** | 681 |
| 2022 Musterhof | tesseract | 9 | 1 | 1335 |
| 2022 Musterhof | paddle-ocr | 9 | 1 | 1303 |
| 2022 HausMusterberg | textlayer | **193** | **28** | 648 |
| 2022 HausMusterberg | tesseract | 0 | 0 | 1295 |
| 2022 HausMusterberg | paddle-ocr | 0 | 0 | 1257 |

Die Textebene birgt ~40 % weniger echte Wort-Token als OCR. **Folge:** eine automatische Genauigkeits-Metrik „gegen die Textebene" wäre wertlos (Vergleich gegen Müll); es gibt für diese Dokumente keine saubere Referenz außer manueller Erfassung oder (potenziell) VLM. Und: `--disable-ocr` NIE verwenden; auf diesen Bescheiden immer force-OCR. [Quelle: SUMMARY.md §2, §4; _metrics.md]

### 3.3 tesseract vs. paddle-ocr (die zwei brauchbaren lokalen Backends)

| Dokument | Backend | Zeichen | Alpha-Token | Sekunden | Mojibake | Junk |
|---|---|---|---|---|---|---|
| 2017 Einheitswert | tesseract | 12151 | 1390 | 8,6 | 2 | 1 |
| 2017 Einheitswert | paddle-ocr | 12391 | 1374 | 6,9 | 0 | 1 |
| 2022 Musterhof | tesseract | 11737 | 1335 | 7,8 | 9 | 1 |
| 2022 Musterhof | paddle-ocr | 11698 | 1303 | 6,6 | 9 | 1 |
| 2022 HausMusterberg | tesseract | 11110 | 1295 | 7,9 | 0 | 0 |
| 2022 HausMusterberg | paddle-ocr | 11140 | 1257 | 6,3 | 0 | 0 |

Sichtprüfungs-Befund: **tesseract `deu`** — Fließtext großteils korrekt (Rechtsbehelfsbelehrung, Berechnungsblöcke, Beträge `96,50 €`, `140,35 €`); Fehler v. a. an Stempeln/Barcodes/Handschrift, gelegentlich `§`→`3`, „Finanzant", „gesant". **paddle-ocr `german`** — ebenfalls lesbar, aber systematischere m→n/a/s-Verwechslungen („Finanzant", „Geneinde", „ait", „saßgebenden", „Inforsationsschreiben"). Tempo: paddle ~10–20 % schneller; tesseract ~genauer. Wertung von SUMMARY.md für lokale Zwecke: tesseract `deu` beste verfügbare Wahl. (Für die Produktions-Pipeline wurde später MinerU gewählt — Abschnitt 5; tesseract bleibt der beste LOKALE Fallback.)

### 3.4 Recovery-Scores über alle 15 Beispiele

„Recovery" = wie viele der in OCR-Befunde.md benannten Zielwörter je Dokument korrekt im Output stehen. [Quelle: _recovery.md; Fallback-Korrektur aus 3.1 eingearbeitet]

| Dokument | textlayer | tesseract | paddle-ocr | easyocr | candle-trocr | candle-glm-ocr |
|---|---|---|---|---|---|---|
| 2016 Einkommensteuer | 4/4 | 4/4 | 4/4 | 4/4 | 1/4 | 1/4 |
| 20161031 (Allianz) | 0/5 | 2/5 | 3/5 | 1/5 | 0/5 | 0/5 |
| 20161116 (Darlehen) | 0/4 | 0/4 | 0/4 | 3/4 | 0/4 | 0/4 |
| 2017 Einheitswert Musterberg | 4/5 | 5/5 | 5/5 | 5/5 | 0/5 | 0/5 |
| 2021 KG Musterstraße | 4/4 | 4/4 | 4/4 | 4/4 | 0/4 | 0/4 |
| 2022 Musterhof | 2/5 | 5/5 | 5/5 | 5/5 | 0/5 | 0/5 |
| 2022 HausMusterberg | 2/6 | 6/6 | 6/6 | 6/6 | 0/6 | 0/6 |
| 2022 Beispielstr | 0/5 | 5/5 | 5/5 | 5/5 | 0/5 | 0/5 |
| 492064 Musterplan | 0/5 | 0/5 | 2/5 | 2/5 | 0/5 | 0/5 |
| Grunderwerbsteuer | 0/4 | 4/4 | 4/4 | 3/4 | 0/4 | 0/4 |
| Handelsregister | 4/6 | 6/6 | 6/6 | 6/6 | 0/6 | 0/6 |
| HVB April 2024 | 5/5 | 5/5 | 5/5 | 5/5 | 0/5 | 0/5 |
| img20250722 (SANEF) | 4/4 | 4/4 | 4/4 | 4/4 | 0/4 | 0/4 |
| Kellerbau Schlussrechnung | 2/5 | 1/5 | 1/5 | 4/5 | 1/5 | 1/5 |
| Notizen1 (Handschrift) | 0/4 | 0/4 | 0/4 | 0/4 | 0/4 | 0/4 |
| **GESAMT** | **44%** | **72%** | **76%** | **80%** | **3%** | **3%** |

(Die Original-Tabelle führt zusätzlich „candle-hunyuan-ocr 76%", „candle-paddleocr-vl 0%" und „candle-deepseek-ocr 100%" — hunyuan/deepseek sind Paddle-Kopien (Abschnitt 3.1), paddleocr-vl produzierte 0 Bytes (Validierungsfehler `model_path` erforderlich), die deepseek-„100%" beruhen auf nur 3 Dokumenten. MinerU ist in dieser Tabelle NICHT gescort — das ist eine der Lücken des ausstehenden formalen Diffs, Abschnitt 6.)

Lesarten: easyocr (direkt als Python-Lib gefahren, `pdftoppm` 300 dpi, CPU) ist der beste lokale Allrounder, aber langsam (68–227 s/Dokument); tesseract/paddle sind auf Bescheiden gleichwertig stark und versagen auf Handschrift/Formular-Kollaps (Kellerbau 1/5, Musterplan 0–2/5, Notizen 0/4); **kein lokales Backend löst Handschrift**.

### 3.5 Laufzeit- und Fehlerdaten (aus results.jsonl, 147 Läufe)

- candle-glm-ocr (echt gelaufen): 41–509 s/Dokument, Output winzig (2,3–17,5 KB wo tesseract 11–42 KB liefert) — unbrauchbar langsam UND lückenhaft. candle-trocr: liefert fast nur Kassenzettel-Halluzinationen („ITEM", „TOTAL AMOUNT", „CASHIER") — alle 15 Dateien gelesen, durchgängig so; einmal 60-s-Timeout.
- Reproduzierbare Infrastruktur-Fehler: paddle-ocr einmal an einer HuggingFace-Lock-Datei gescheitert; kreuzberg-easyocr-Backend „not registered" bzw. TypeError (deshalb der Direktlauf); 20161116150357250.pdf hat ein unrenderbares Bild-XObject (`Invalid image dimensions`), an dem mehrere Backends leer ausstiegen; textlayer liefert bei 4 von 15 Dokumenten `rc=0` mit 0 Bytes (Scans ohne jede Textebene: 20161031, 20161116, Musterplan, Grunderwerbsteuer).
- Die `vlm`-Option von kreuzberg (Claude/GPT-4o-Vision) wurde NICHT gefahren: sie würde die privaten Steuerdaten an einen externen Dienst senden — Datenschutz-Sperre, nur mit bewusster Entscheidung. [Quelle: SUMMARY.md §4/§6]

---

## 4. Fehlgeschlagene Zwischenanläufe (Beweissicherung)

- `_examples_minerutest/_failures.log`: alle 15 Beispiele scheiterten mit HTTP 402 `Insufficient Balance` — das Konto war leer (die €30-Session).
- `_examples_retest/_failures.log`: alle 15 scheiterten mit HTTP 401 `invalid api key` gegen `https://sbdpcrzwlndrgb.api.runpod.ai` — den inzwischen **gelöschten** LB-Endpoint; das „401" war in Wahrheit „Ziel existiert nicht mehr".
- `_mineru_friendli_8bit/` und `_mineru_friendli_raw/` (Versuche vom 25.06.2026, MinerU-Backend `vlm-http-client` gegen einen Friendli-gehosteten VLM-Endpoint): **kein einziger brauchbarer Output aus beiden Läufen — aus DREI unterschiedlichen Ursachen** (Logs am 01.08.2026 gelesen: Fehler-Extraktion per grep plus Volllektüre der Abschlusslisten und der Region um die durchgelaufenen Batches):
  1. **Leere Modellantwort (je Lauf genau EIN Task):** In beiden Läufen scheiterte task#1 (`20161031144820006`) nach ~5 min 18 s mit `ServerError: No choices found in the response.` (die drei grep-Treffer pro Log sind Raise-Zeile plus Traceback-Wiederholungen desselben einen Fehlers, keine drei Vorfälle).
  2. **Endpoint-Nichtverfügbarkeit (die Masse):** Der 8-bit-Lauf endet mit `Error: 15 task(s) failed` — task#1 wie oben, tasks #2–#15 mit `ServerError: Unexpected status code: [503], response body: Endpoint is unavailable` (dahinter sichtbar: `Inference request failed: dial tcp 10.104.111.154:6000` bzw. im Raw-Lauf `…111.138:6000: connect: connection refused` — der Serving-Prozess hinter Friendlis Gateway lief nicht). Im Raw-Lauf traf das die tasks #2–#11 (`Error: 11 task(s) failed`).
  3. **Format-Inkompatibilität trotz antwortendem Endpoint (Raw-Lauf, Batches 12–15):** Die letzten vier Batches (Handelsregister, Kellerbau, Notizen1, img20250722) liefen komplett durch (`Completed batch 12/15` … `15/15`, „4 of 15 batches finished") — der Endpoint antwortete wieder, und die Antworten enthielten echten OCR-Text (im Log wörtlich sichtbar, inkl. bemerkenswert guter Handschrift-Lesungen aus Notizen1). Aber JEDE Seite warf `WARNING parse_layout_output: Layout output does not match expected format: <fcel>…`: Auf MinerUs Layout-Detection-Prompt (`'\nLayout Detection:'`) lieferte das Friendli-servierte Modell Inhalts-Zellen im `<fcel>/<lcel>/<ecel>/<nl>`-Tabellenformat statt Layout-Boxen; MinerUs Client verwarf das, und genau diese vier Dokumente sind die 0-Byte-Markdowns auf Platte. Das ist ein echter **Kompatibilitätsbefund MinerU-`vlm-http-client` ↔ Friendli-Hosting** (naheliegende, aber unbewiesene Deutung: das dort servierte Modell/die Quantisierung beherrschte den Layout-Detection-Schritt des Zwei-Stufen-Protokolls nicht) — nicht bloß ein Ausfall.

  [Quelle: `_mineru_friendli_8bit/_run.log` (2758 Zeilen) und `_mineru_friendli_raw/_run.log` (2001 Zeilen), Abschlusslisten und Batch-Region Zeilen ~1840–1960, Session 01.08.2026] Über die OCR-Qualität von MinerU auf dem eigenen Worker sagt all das nichts aus; der Pfad „fremdgehostetes VLM per `vlm-http-client`" wurde danach nicht weiterverfolgt (die gewählte Architektur hält die Engine im eigenen Worker) — wer ihn je wieder erwägt, muss zusätzlich zur Verfügbarkeit die Layout-Detection-Kompatibilität des servierten Modells testen, nicht nur einen Ping.
- `_mineru_baked_test/` (ein Dokument, Grunderwerbsteuer): byte-identisch mit dem RunPod-vLLM-Output — eigene `diff`-Prüfung dieser Session.
- `gemini_truth.sh` (Ground-Truth-Erzeugung via `llm -m openrouter/google/gemini-3.1-pro-preview`, ausdrücklich **vom User selbst** zu starten — der Agent ist für den Upload der Steuer-PII zu OpenRouter/Gemini hart gesperrt): Zielverzeichnis `ocr_comparison/_truth_gemini/` existiert nicht in der Dateiliste ⇒ das Skript hat nie Output erzeugt.

---

## 5. Die MinerU-Kalibrierung: 15/15 Beispiele durch die echte Pipeline

**Bestand:** `ocr_comparison/_mineru_runpod_vllm/out_all/<Dokument>/vlm/<Dokument>.md` — alle 15 vorhanden, alle nicht-leer (665 Bytes bis 41.457 Bytes; Gesamttext ~225 KB), dazu je Dokument `content_list`/`middle`/`model`-JSONs, `layout.pdf`, `origin.pdf` und extrahierte Bild-Crops. Laut Jurgens Korrektur vom 31.07.2026 lief diese Kalibrierung am 29.06.2026 (LB-Ära) mit dem Qualitätsurteil „excellent" auf echten Bescheiden; das Datum selbst wurde in dieser Session nicht aus Dateimetadaten nachgeprüft. Alle 15 Markdowns wurden am 31.07./01.08.2026 vollständig gelesen; die folgenden Abschnitte sind der Ertrag.

### 5.1 Wo MinerU glänzt: die Bescheide

Auf genau den Dokumenten, deren Textebene total zerstört ist (n→h-Klasse), liefert MinerU nahezu fehlerfreien Fließtext: „Finanzamt Beispielberg" statt `F'ihahzamt Beispielbepg`, „Grundsteueräquivalenzbeträge Hauptfeststellung" statt `Ilauptfeststelluhg`, „BayGrStG" statt `Bay6rstc`; komplette Berechnungsketten als Tabellen (Einheitswert-Herleitungen bis in DM/€-Umrechnung, Äquivalenzbetrag-Rechnungen 96,50/72,80/140,35 €, Messbetrags-Formeln `16923 € x 3,5 v.T. = 59,23 €`), Flurstücks- und Miteigentumsanteils-Listen im Darlehensvertrag über Seiten hinweg konsistent, Rechtsbehelfsbelehrungen als sauberer Fließtext. Barcodes/Siegel landen als Bild-Referenzen bzw. `<details>`-Blöcke (`seal: BAYERN FINANZAMT`) statt als Textsalat. Kleinere Restfehler existieren („Steuetberatungs GmbH", „Verwaltungsa- akt"-Trennfehler, `*807*` statt `*B07*` in einer Barcode-Zeile), ändern das Bild nicht.

### 5.2 NEUER BEFUND (01.08.2026): Die Rand-Aktenzeichen fehlen im Markdown-Output

**Per `grep` über alle 15 Markdowns verifiziert:** Kein einziges Grundsteuer-/Einheitswert-Markdown enthält das Wort „Aktenzeichen" oder eines der Aktenzeichen-Ziffernmuster `123/456`, `234/567`, `345/678`, `456/789` — null Treffer in allen 15 Dateien. Die einzige Fundstelle von „ktenzeich" überhaupt: zweimal „EW-Aktenzeichen 12345678901234567" in `2016_Privat_Bescheid_Einkommensteuer.md` — das ist das Musterstadter EW-AZ **im Fließtext der Besteuerungsgrundlagen-Tabelle**, nicht der Randblock. Auch die Steuernummern-Muster (`567/890`, `123/456`, `678/901`) kommen in keinem der 15 Outputs vor.

**Deutung (als Deutung markiert):** Der rotierte Rand-/Kopfblock der bayerischen Bescheide (Aktenzeichen, Steuernummer, Bearbeitungsdaten) landet offenbar in den extrahierten Bild-Crops statt im Markdown-Text. **Ungeprüft:** ob die `content_list`-JSONs oder Bild-Dateien die Aktenzeichen tragen.

**Warum das tragend ist:** Die Pass-1-Suchrahmen (Bead `jurgen-oeu`, Block A2) stützen sich gerade auf exakte Aktenzeichen als robusteste, OCR-schadensfeste Deploy-Terme — und die **kaputte** Textebene enthielt sie (`Aktehzeicherl 234/567/8901/234/567/8`, Ziffern sauber). Ein Write-back, der nur das MinerU-Markdown auf den Record legt, würde für genau diese Dokumentklasse die stärksten Suchterme **verlieren, die vorher da waren**. Das spricht direkt in die offene Write-back-Entscheidung (gbrain `projects/devonthink-klassifikation/ocr-integration-spec`) und in die `formats: ["markdown"]`-Festlegung hinein. Dies ist der erste konkrete Ertrag des nie durchgeführten formalen Diffs (Abschnitt 6).

### 5.3 NEUER BEFUND (01.08.2026): Halluzinations-Artefakte in den schweren Klassen

Per `grep` auf CJK-Zeichen bestätigt — genau zwei der 15 Outputs betroffen:
- `HVB_April2024.md`: mitten im deutschen Bankformular ein komplett erfundener chinesischer Satz („2023年1月1日，公司发布《关于使用部分闲置募集资金进行现金管理的公告》（公告编号：2023-046）。" — Börsenmitteilungs-Boilerplate über Kapitalanlage-Bekanntmachungen). Dazu zerhackte Legendenzeilen (mehrfach wiederholt „P = Kredalejike Investitionen P = Krédite von Phaat R = Konsumenkredit"), Namensfehler („Raffjesbank Bcispielst EG" statt Raiffeisenbank Beispielstadt eG, „Erika Muster") und Zahlenkolonnen, die in Tabellenzellen zusammenlaufen (`12.345.67890.1234.567.89…`). Die Formularstruktur und die tragenden Zahlen (Kreditlinien, Vermögenswerte, 1.000.000, 100.000/200.000) sind dagegen weitgehend sauber.
- `Notizen1.md` (kursive Handschrift): teils erstaunlich nah dran („Mail an Mustertur", „Haushaltsplan", ganze Themenlisten lesbar — deutlich mehr Substanz als tesseract/paddle/easyocr, die hier 0/4 lieferten), aber durchsetzt mit Erfindungen inkl. CJK-Einsprengsel („Die来看") und Zeichenersatz (⑥①②-Nummerierungen). Für Suchzwecke brauchbarer als alles Lokale, als Wiedergabe NICHT vertrauenswürdig.
- Verwandt, ohne CJK: `20161031144820006.md` (Allianz-Formular) — Formularfelder groß korrekt, aber Handschriftfelder verstümmelt („MUSTER INMOBILIENGMBH", „Am Wiesen l2", „12345 Beispie", „Erika Mustcr, Geschäftspühnerin") und eine absurde Tabellenzeile („Anzulegender Betrag insgesamt: 1.000.000 EUR" — ob 1.000.000 auf dem Original steht, ist unverifiziert; gegen das PDF prüfen, bevor man es zitiert). `492064 Musterplan`: „Bauglanung", „Am Mühlbach 9" (mutmaßlich Mühlbachweg — unverifiziert), Handschrift-Reste als `<details>`-Bild-Text. `img20250722` (SANEF): fast vollständig sauber inkl. aller Beträge/SIRET; eine kopfstehende französische Hinweiszeile wird spiegelverkehrt wiedergegeben (`AVT el é fionb yuog yilqmen é noitsmojni`), und eine der vier Quittungen trägt „Date : 19/05/2017 13:48", wo die übrigen 19/05/2025 tragen — Original nicht geprüft, kann Vorlage- oder OCR-Fehler sein.

**Konsequenz, deckungsgleich mit der HARD RULE des Skills:** Auf dichten Formularen und Handschrift kann das VLM plausiblen Text ERFINDEN. Der billige Gegencheck bleibt: eine Formel/Summe im Output nachrechnen — eine halluzinierte Zahl erfüllt ihre eigene Gleichung nicht; für den 30k-Lauf ist der geplante „Halluzinations-Verifikations-Pass auf den schlechtesten Scans" (Bead `jurgen-09v`, Akzeptanzkriterien) damit empirisch begründet, nicht paranoid.

### 5.4 Benannter Widerspruch (nicht still aufgelöst): „Vertragspartner-Gesellschaft"

`OCR-Befunde.md` führt für `Grunderwerbsteuer Muster 1.pdf` den Eintrag: `Vertragspartner-Gesellschaft` → *(soll heißen)* *Erwerber-Gesellschaft*. Das MinerU-Markdown liest den Adressaten/Käufer korrekt als „Erwerber-Gesellschaft" — und führt **separat unter „Vertragspartner"** (Verkäuferseite des beurkundeten Kaufvertrags vom 01.01.2016, Notar Max Muster, URNr./AZ 999, Gemarkung Musterfeld, Flurstück aus 1234 u. a.): „Name: Vertragspartner-Gesellschaft". Entweder machen beide Engines denselben Lesefehler, oder der Verkäufer heißt wirklich „Vertragspartner-Gesellschaft" und die Korrektur-Glosse in OCR-Befunde.md verwechselt Käufer- und Verkäuferfeld. **Ohne Blick ins Original-PDF nicht entscheidbar; bis dahin darf keine der beiden Lesarten als Fakt zitiert werden.**

### 5.5 Leere Seite = leeres Markdown

Aus dem 593-Seiten-Lauf (nicht aus den 15 Beispielen): genau eine Seite kam ohne Text zurück — eine echt leere graue Seite mit Kolumnentitel. Leerer Output ist ein korrektes Ergebnis, kein Fehler; erst das Bild ansehen, dann ggf. retryn. [Quelle: Skill; gbrain-Incident-Seite]

---

## 6. Was noch aussteht

1. **Der formale Per-Dokument-Diff MinerU-Output vs. OCR-Befunde.md** — nie durchgeführt. Methode, die sich aus dem Vorliegenden ergibt: je Dokument (a) jeden Müll-Snippet-Eintrag aus Abschnitt 2 im MinerU-Markdown nachschlagen (korrekt / fehlend / anders falsch), (b) die exakten Kennungen (Aktenzeichen, Steuernummern, IBANs) gesondert prüfen — auch in `content_list`/Bild-Crops, nicht nur im Markdown (Befund 5.2), (c) Zahlen nachrechnen, wo Rechenketten existieren (Befund 5.3), (d) Ergebnis je Klasse verdichten (Bescheide / Formulare / Handschrift / Fremdsprache / Bank-Notar). Die Befunde 5.2–5.4 sind die ersten drei Zeilen dieses Diffs.
2. **Ground Truth:** existiert für kein Dokument (Textebene ist als Referenz wertlos, Abschnitt 3.2; `gemini_truth.sh` lief nie, Abschnitt 4 — Start ist Jurgens Entscheidung wegen PII-Upload).
3. **MinerU in die Recovery-Tabelle aufnehmen** (Abschnitt 3.4 endet vor MinerU) — fällt als Nebenprodukt des formalen Diffs ab.
4. **Die `content_list`/`images`-Frage** aus Befund 5.2 klären, bevor die Write-back-Entscheidung fällt.
5. **Erkennungs-/Ziellisten-Kalibrierung** (welche der ~30k Records brauchen OCR) — Design in den gbrain-Idea-Seiten vom 24./25.06.2026, nie end-to-end gefahren; dieses Dokument liefert die Klassen- und Signal-Grundlage dafür.
