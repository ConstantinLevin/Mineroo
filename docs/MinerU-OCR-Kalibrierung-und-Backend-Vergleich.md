# OCR-Qualität am echten Bestand: Schadensklassen, Engine-Verhalten und Evaluations-Methodik

> **Hinweis (Mineroo#2):** Dieses Dokument beschreibt den Stand bis MinerU 3.4 und den alten Endpoint. Es ist Geschichte und wird nicht umgeschrieben. Aktuell gelten das [README](../README.md) des Repositories und die Spec [Mineroo#1](https://github.com/ConstantinLevin/Mineroo/issues/1). Namen, Anschriften und Kennungen Dritter in den Belegen sind durch erfundene Werte ersetzt; die Schadensmuster der OCR-Lesungen sind dabei erhalten. **Ausnahme ist Abschnitt 8:** Er ist neu, stammt aus dem Messlauf zu [Mineroo#19](https://github.com/ConstantinLevin/Mineroo/issues/19) und gilt für MinerU 4.0.12 auf dem Endpoint `csfbvej3d4cna5`.

**Stand:** 05.08.2026.
**Zweck:** Dieses Dokument ist die Beweis- und Methodikschicht des OCR-Projekts. Es hält generalisiert fest: (1) **welche Schadensklassen** der Bestand enthält und woran man sie erkennt, (2) **was OCR-Engines auf diesen Klassen leisten** — lokale Backends wie MinerU —, (3) **wie man das misst**, ohne sich selbst zu betrügen, und (4) welche Qualitätsfragen offen sind. Es ist bewusst KEIN Dokumenten-Dossier: Einzelbefunde stehen hier nur, wo sie eine generelle Lektion belegen oder auf der Nachprüf-Liste stehen. Die Einzeldokument-Details leben in den Artefakten, auf die Abschnitt 7 zeigt.
**Einstieg für Leser ohne Vorkontext:** [README.md](README.md) — Landkarte des Dokumentensets, die übertragbaren Lehren des Projekts und die Pflegeregeln dieser Dateien.
**Schwesterdokumente:** [MinerU-RunPod-Betriebshandbuch.md](MinerU-RunPod-Betriebshandbuch.md) (Engine-Betrieb und Lifecycle), [MinerU-RunPod-Geschichte-und-Entscheidungen.md](MinerU-RunPod-Geschichte-und-Entscheidungen.md) (Chronik und Entscheidungen).

**Quellen** (Session 31.07.–02.08.2026, vollständig gelesen soweit nicht anders vermerkt): `Examples/OCR-Befunde.md` (das Fixture-Set; gbrain-Spiegel `projects/devonthink-klassifikation/examples-ocr-befunde`); aus `ocr_comparison/`: `SUMMARY.md`, `_metrics.md`, `_recovery.md`, `results.jsonl` (147 Läufe), `gemini_truth.sh`, beide `_failures.log`, alle 15 MinerU-Markdowns unter `_mineru_runpod_vllm/out_all/`, das `_mineru_baked_test`-Markdown, alle 15 `candle-trocr`-Dateien, beide Friendli-`_run.log`; gbrain `projects/mineru-ocr/born-digital-backend-benchmark`; eigene Prüfungen dieser Session (`diff`, `grep`, `stat` — jeweils als solche markiert).

**Rohdaten-Bestände (Beweismaterial, keine Wissensquellen — muss niemand lesen):** Unter `ocr_comparison/` liegen die Roh-OCR-Dumps je Backend (`candle-glm-ocr/`, `easyocr/`, `paddle-ocr/`, `tesseract/`, `textlayer/`, `_unavailable/`, `candle-deepseek-ocr/`, `candle-hunyuan-ocr/`), die stderr-Logs unter `_logs/` sowie Bild-/PDF-/Layout-JSON-Artefakte. Das sind die vermessenen Gegenstände, nicht die Messungen: ihr Erkenntnisgehalt steckt in der Urteils-Schicht (SUMMARY/_metrics/_recovery/results.jsonl) und den Byte-Vergleichen dieser Session und ist hier konsolidiert. Gebraucht werden die Dumps nur, falls jemand Scores neu berechnen oder anzweifeln will.

---

## 1. Das Kernproblem, das alles hier begründet

**Fast alle harten Fälle haben eine vorhandene Textebene** (`kind: PDF+Text`, `word count > 0`) — das naive Signal „word count == 0" winkt sie als „hat Text" durch, und **trotzdem ist der Text Müll.** Drei generelle Eigenschaften dieses Bestands, die jede Erkennung und jede Bewertung prägen:

1. **Kaputt ist nicht leer.** Die schwierige Klasse ist nicht „kein Text", sondern „Text vorhanden, unbrauchbar". Sie ist mit Zählern allein nicht fangbar.
2. **Ziffern überleben, Wörter zerfallen.** Bei Behörden-Bescheiden bleiben Aktenzeichen-Ziffernfolgen sauber lesbar, während der Fließtext ringsum zerbricht. Daraus folgt die Grundregel der Suchrahmen: **exakte Kennungen sind der robusteste Fang auf beschädigtem Material** — robuster als jedes Themenwort.
3. **Müll wird oft gar nicht indexiert.** Die härtesten Fälle (Handschrift, Punkt-Insertion, Fremdsprache) findet man nicht per Müll-Stichwortsuche, sondern nur per Volltext-Lesen — ein Erkenner, der auf Suche baut, sieht sie strukturell nicht.

[Quelle: `OCR-Befunde.md`, Kopfabschnitt]

---

## 2. Die Schadensklassen des Bestands

Der Katalog, gegen den kalibriert wird. Jede Klasse mit ihrem Fehlerprofil, dem generellen Erkennungssignal und einem Beleg-Beispiel aus dem Fixture-Set.

### Klasse A — Totalsubstitution auf Behörden-Bescheiden (n→h, teils zusätzlich m→b)

Ein Buchstabe wird systematisch durch einen anderen ersetzt, über das ganze Dokument. Erkennungssignal: der Text besteht aus fast-Wörtern, die kein Lexikon kennt, aber ein Substring-Muster tragen — ein Ersetzungs-Rücktausch macht sie wieder lesbar. Belege: `F'ihahzamt Beispielbepg` → *Finanzamt Beispielberg*; `Eihheitswept` → *Einheitswert*; `Ilauptfeststelluhg` → *Hauptfeststellung*; `Bay6rstc` → *BayGrStG*. Doppelt korrupt (n→h **und** m→b): `Fihahzabt Nüncheh` → *Finanzamt München*. **Häufigste und wichtigste Klasse des Steuer-Korpus** (5 der 15 Fixture-Dokumente sind Grundsteuer-Bescheide dieser Art). Bemerkenswert: die Aktenzeichen-Ziffern derselben Dokumente sind sauber.

### Klasse B — Raster-/Zonenzerfall

Ganze Seitenbereiche lösen sich in Zeichensalat auf, während andere Bereiche lesbar bleiben — typisch Adressfelder, Barcode-Zonen, Rechtsbehelfsseiten. Erkennungssignal: Zeilen mit hoher Sonderzeichen-Dichte, insbesondere Doppelpunkt-Ketten (das ist die „Junk-Zeilen"-Metrik aus Abschnitt 4). Belege: `Z::#:?n!8Zg 3` (Adressfeld); `XK4000at "07¥ZZ3t00 3ZI Z3( iiHH` (Barcode-Zone); `ä=:::=E::gtLe::=u::::P::=±::d:` (ganze Seiten).

### Klasse C — Punkt-Insertion

Serifen werden als Satzzeichen gedeutet; Wörter zerfallen an falschen Stellen. Erkennungssignal: auffällig viele Ein- bis Dreizeichen-„Wörter" mit eingestreuten Punkten (schlägt auf die Zeichen/Wort-Heuristik durch). Belege: `Am Wie.sen 12`, `12345 Bei.spi.el`. **Warum diese Klasse gefährlich ist:** Sie beschädigt bevorzugt exakte Kennungen — im Fixture-Set wurde eine IBAN unbrauchbar (`S` statt `5` in der Ziffernfolge). Wo Kennungen tragen, ist diese Klasse teurer als Klasse A.

### Klasse D — Handschrift und Formular-Kollaps

Handschrift wird durch Drucktext-OCR gejagt; wo Handschrift auf Formulardruck trifft, kollabieren beide ineinander. Erkennungssignal: **kein Substring-Muster** — komplettes Lexikon-Versagen, kein Rücktausch möglich. Belege: `Unthaltskaste landwitshoftlich Fläch` → *Unterhaltungskosten landwirtschaftliche Fläche*; `Staat gepr Bautechrifter unt Zimmera méster` → *staatl. gepr. Bautechniker und Zimmermeister*; Bank-Wasserzeichen als Text erkannt (`VR Bnuspielsiadt` → *VR Beispielstadt*). **Kein lokales Backend löst diese Klasse** (Abschnitt 3); MinerU liefert hier Substanz, aber mit Erfindungen (Abschnitt 5).

### Klasse E — Fremdsprache und Zeichensatz-Mismatch

Fremdsprachiger Text plus falscher Zeichensatz erzeugt Wort- UND Zahlenfehler. Erkennungssignal: Sonderzeichen mitten in Ziffernfolgen; implausible Zahlenwerte. Belege: `EXENPLE LUNSEIL` → *EXEMPLE CONSEIL*; `SIRET 123 uoü 456 00012` (ü in einer Ziffernfolge); ein Datum als `19/05/7076` statt 2025. **Zahlenfehler sind hier der Normalfall, nicht die Ausnahme** — für einen Bestand, in dem Beträge und Daten tragen, ist das die stille Gefahr.

### Klasse G — Struktur-Zerstörung bei lesbaren Zeichen (die unsichtbarste Klasse)

**Erkannt beim Roh-Dump-Diff am 02.08.2026; vorher in keiner Quelle benannt.** Ein Dokument kann Zeichen weitgehend korrekt liefern und für Suchrahmen trotzdem unbrauchbar sein, weil die **Zuordnung von Beschriftung und Wert** zerstört ist. Beleg aus der Textebene eines Grundsteuer-Bescheids: Die Beschriftungen („Wohnfläche", „x Äquivalenzzahl", „ergibt den Äquivalenzbetrag für die Wohnfläche") stehen im ersten Drittel der Datei, ihre **Werte** („193 m2", „0'50 €,n2", „96'50 €") als zusammenhangloser Block Dutzende Zeilen später. Dieselbe Zerlegung betrifft die gesamte Berechnungskette, in beiden Bescheiden des Dokuments.

**Warum das für dieses Projekt zentral ist:** Ein Suchrahmen, der zwei Dinge in Nachbarschaft erwartet — ein Themenwort und einen Akt, eine Bezeichnung und einen Betrag —, kann auf solchem Text **nicht** greifen, obwohl beide Bestandteile vorhanden und korrekt sind. Es ist kein Zeichenschaden, sondern ein Lesereihenfolge-Schaden, und keine der Kennzahlen aus Abschnitt 4 (Mojibake, Junk-Zeilen, Alpha-Token) schlägt dabei aus. Genau hier gewinnt eine layoutverstehende Engine strukturell: Sie gibt „ergibt den Äquivalenzbetrag für die Wohnfläche 96,50 €" als einen Zusammenhang zurück.

**Zwei Begleitartefakte derselben Quelle, ebenfalls neu belegt:** Das **Dezimalkomma wird zum Apostroph** (`96'50 €`, `0'04 €/m2`) — und zwar inkonsistent, dieselbe Zahl erscheint im selben Dokument einmal als `96'50 €` und einmal als `96,50 €`. Und das **Euro-Zeichen wird zu einem `e`** (`96,50 e`, `72,80, e`). Wer Beträge per Muster sucht, muss beide Varianten abdecken.

### Klasse F — Encoding-/Sperrdruck-Artefakte (Bank, Versicherung, Notar; mittel bis leicht)

Kaputte ToUnicode-/Font-Maps und Sperrdruck erzeugen systematische Zeichenersetzungen bei sonst intakter Struktur. Erkennungssignal: **Mojibake-Zeichen** (`®`, `£`, `±`, `¥` in deutschem Fließtext) — die zweite Kernmetrik aus Abschnitt 4. Belege: `Must®r lmmobill®n QmbH & Co. Ka` (® statt e durchgehend); `S E L B S T A U S 1{ U N F T` (Sperrdruck mit Ziffereinschub); ein japanisches Katakana-Zeichen, aus einem ENTWURF-Wasserzeichen fehlinterpretiert; Adressblöcke total zerhackt (`AM WIIESEN 121 / 1,2,345-BEEISPIIEI`).

---

## 3. Das Fixture-Set: der stehende Prüfstand

**Was es ist:** 15 aus der DEVONthink-Datenbank des Trägerprojekts exportierte Kopien unter `/Users/jurgen/CodingProjects/DevonThink886/Examples/` (Originale unverändert), begleitet von `Examples/OCR-Befunde.md`, das je Dokument UUID, Herkunftsordner, Aussteller und die wörtlichen Müll-Schnipsel mit ihrer korrekten Lesart festhält. **Abdeckung:** alle sechs Klassen aus Abschnitt 2 — 5× Klasse A (Grundsteuer-Bescheide dreier Finanzämter), weitere Steuerarten, 2× Handwerker-Rechnung/Formular (Klassen C und D), 1× reine Handschrift, 1× Fremdsprache, 4× Bank/Versicherung/Notar (Klasse F). **Die entscheidende gemeinsame Eigenschaft:** jedes dieser 15 Dokumente hat `word count > 0` — das Set ist damit exakt der Prüfstand für die schwere Erkennungsklasse „kaputt, aber nicht leer", nicht für die triviale „kein Text".

**Wofür es gebraucht wird — dieses Artefakt bleibt bewusst in Gebrauch:**
1. **Schwellen-Kalibrierung der Erkennung:** jede Zahlengrenze (Wörter/Seite, Zeichen/Wort) wird gegen dieses Set gesetzt, nie geraten — plus Gegenprobe an einer Zufallsstichprobe, weil das Positiv-Set homogen sein könnte.
2. **Engine-Prüfstand:** jede neue oder geänderte OCR-Engine wird zuerst hier durchgefahren, bevor sie den Bestand berührt.
3. **Regressions-Set:** jede Änderung an Write-back oder Erkennung wird hier durchgespielt (test-before-bulk).

**Wie man eine Engine dagegen fährt:** alle 15 Dokumente durch die Engine; je Dokument die in `OCR-Befunde.md` benannten Zielwörter im Output nachschlagen (korrekt / fehlend / anders falsch) → Recovery-Score je Klasse; die exakten Kennungen (Aktenzeichen, Steuernummern, IBANs) **gesondert** prüfen — und zwar in allen Ausgabeformaten, nicht nur im Haupttext (Grund: Abschnitt 5.1); Rechenketten nachrechnen, wo welche existieren; Ergebnis nach Klassen verdichten, nicht als Gesamtzahl.

---

## 4. Evaluations-Methodik: wie man OCR-Qualität misst, ohne sich zu betrügen

Diese Verfahren sind das Übertragbare am ganzen Bake-off — sie gelten für jede künftige Engine.

**Die drei Kennzahlen aus dem Bake-off:**
- **Mojibake-Zahl** — Encoding-Artefakte (`®£±¥…`) pro Dokument. Fängt Klasse F direkt.
- **Junk-Zeilen** — Zeilen mit 3+ Doppelpunkten, also kollabierte Tabellen-/Rasterzeilen. Fängt Klasse B.
- **Alpha-Token** — Anzahl echter Wort-Token. Fängt Substanzverlust, den die anderen beiden nicht sehen.
Niedrig/niedrig/hoch = sauber. Die drei zusammen sind billig zu rechnen und trennen die Klassen bereits gut.

- **Recovery-Score** — der Anteil der vorab benannten Zielwörter, die eine Engine korrekt liefert. Vorteil gegenüber Zeichenzahlen: er misst inhaltliche Rettung statt Textmenge; Voraussetzung ist ein Set mit bekannten Sollwerten (das Fixture).

**Das Protokollformat, das sich bewährt hat — übertragbar auf jeden künftigen Engine-Vergleich.** Der Bake-off schrieb je Lauf **eine JSON-Zeile** mit Backend, Datei, Erfolgs-Flag, Rückgabecode, Laufzeit, Zeichen- und Zeilenzahl und dem Fehlertext; die Kennzahlen und die Recovery-Tabelle entstanden daraus, nicht aus den Rohausgaben. Der Wert des Formats liegt in drei Eigenschaften: Es ist **fortschreibbar** (ein abgebrochener Lauf verliert nichts), es hält **auch die Fehlschläge** (die 147 Zeilen dieses Laufs enthalten mehr Fehler- als Erfolgsfälle, und genau die tragen die Fallen unten), und es trennt **Messung von Urteil** — die Rohausgaben bleiben daneben liegen, die Auswertung ist reproduzierbar. Dazu die Ablage-Disziplin: **Schein-Ergebnisse werden aufgehoben, nicht gelöscht.** Die Ausgaben der Backends, die in Wahrheit gar nicht liefen, liegen in einem eigenen Verzeichnis „zur Beweissicherung" — ohne sie wäre der Fallback-Befund unten nicht mehr belegbar.

**Die vier Fallen, jede real eingetreten:**

1. **Nie gegen die eingebettete Textebene messen.** Sie ist auf diesem Material der defekte Gegenstand, nicht die Referenz: gemessen 86–193 Mojibake-Zeichen und 28–33 Junk-Zeilen pro Dokument gegen 0–9 bzw. 0–1 bei sauberem OCR, und ~40 % weniger echte Wort-Token. Eine Metrik „OCR gegen Textebene" wertet damit **rückwärts**. Praktische Folge: `--disable-ocr` nie verwenden; auf diesem Material immer force-OCR.
2a. **Die Wurzel des Fallbacks war eine tote Download-URL — nachgelesen in den Fehlerprotokollen am 03.08.2026.** Die 99 Fehlerprotokolle des Bake-offs zeigen den Mechanismus vollständig, und er hat **zwei** Ursachen, nicht eine: Neben den gar nicht registrierten Backends scheiterte auch das **registrierte** tesseract reihenweise daran, dass das Werkzeug sein deutsches Sprachpaket zur Laufzeit nachladen wollte — von zwei `tessdata_fast`-Adressen, die beide **HTTP 404** liefern (18-mal protokolliert, jeweils beide Adressen). Erst dieses Scheitern löst den Übergang zur nächsten Stufe der Kette aus, und deshalb landete so vieles bei paddle-ocr. **Das erklärt zugleich, warum der Bake-off das Sprachpaket von Hand installieren musste** — die automatische Beschaffung ist schlicht kaputt. Für jeden künftigen lokalen Lauf heißt das: Sprachdaten vorher lokal bereitstellen und ihre Anwesenheit prüfen, nie darauf vertrauen, dass das Werkzeug sie besorgt.

2b. **Weitere Mechanik aus denselben Protokollen:** Das Werkzeug hat ein **internes Qualitäts-Tor** mit einer Schwelle von 0,5 — bleiben alle Stufen der Kette darunter, meldet es „alle Backends unterhalb der Qualitätsschwelle, nehme das beste Ergebnis" und liefert trotzdem etwas (viermal beobachtet, dabei mit Bewertung 0,0, also nichts Brauchbares). **Ein Ergebnis kann also aus einer Kette stammen, die selbst weiß, dass sie versagt hat** — sichtbar nur im Protokoll, nicht am Ergebnis. Und die Warnung über nicht freigegebene Zwischenspeicher des Texterkenners (45-mal) ist Rauschen: Sie erscheint massenhaft und harmlos, wurde aber in der Ergebnisliste einmal als Fehlerursache eines leeren Laufs vermerkt — ein Fehlschluss, den man nur vermeidet, wenn man Protokoll und Ergebnisliste gegeneinander liest.

2c. **Der Renderer-Ausfall ist häufiger als das eine Totalversagen vermuten lässt.** Die Meldung über nicht darstellbare Bild-Objekte erscheint **68-mal** und betrifft acht verschiedene Objekte in mehreren Dokumenten. Meist wird nur die einzelne Grafik übersprungen und das Dokument läuft weiter; nur in einem Fall führte es zum vollständigen Ausfall aller Wege, die diesen Renderer benutzen. Die Lehre bleibt dieselbe wie in 5.4-3, wird aber breiter: Das Problem ist nicht ein kaputtes Dokument, sondern eine **Klasse von Bild-Objekten**, an denen dieser Renderer generell scheitert.

2. **Stille Fallbacks entlarven, bevor man Zahlen glaubt.** Im Bake-off akzeptierte die CLI neun Backend-Namen, während die Laufzeit-Registry nur drei registrierte (`tesseract`, `paddle-ocr`, `vlm`). Ein nicht registriertes Backend erzeugte **keinen Fehler**, sondern fiel still auf `tesseract → paddle-ocr` zurück und meldete „ok=True". Verifikation dieser Session per `diff`: **alle 15 Dateien von `candle-hunyuan-ocr/` und alle 3 von `candle-deepseek-ocr/` sind byte-identisch mit den gleichnamigen aus `paddle-ocr/`** — ihre Scores maßen Paddle. Generelle Regel: **byte-Vergleich zwischen angeblich verschiedenen Engines gehört zur Auswertung**, sonst misst man Kopien.
3. **Ein Score aus 3 Dokumenten ist kein Score.** Die höchste Zahl der Recovery-Tabelle (die „100 %") beruhte auf drei Dokumenten und war zugleich ein Fallback-Artefakt (Falle 2). Vollständigkeit der Abdeckung immer mitführen.
4. **Ground Truth existiert nicht von selbst.** Für dieses Material gibt es keine saubere Referenz außer manueller Erfassung — oder einem VLM, was PII-Übertragung bedeutet und deshalb Jurgens Entscheidung ist (Abschnitt 6).

**Der Zahlen-Nachrechen-Test — der einzige Test, den ein halluzinierendes Modell nicht bestehen kann.** Wo der Output eine Rechenkette enthält, wird sie nachgerechnet; eine erfundene Zahl erfüllt ihre eigene Gleichung nicht. Auf born-digital-Material wurde genau das durchexerziert: vier zusammenhängende Werte einer Optionspreis-Rechnung wurden nachgerechnet und stimmten inklusive zweier Verteilungsfunktions-Werte an genau diesen Stellen — vier gleichzeitig konsistente Zahlen sind gelesen, nicht erfunden. **Auf dem Steuer-Korpus ist dieser Test noch nicht angewandt worden**, obwohl er dort schwerer wiegt: eine halluzinierte Aktenzeichen-Ziffer oder ein falscher Euro-Betrag ist gravierender als in einem Lehrbuch. Die Bescheide bieten die Ketten dafür (Äquivalenzbetrag × Steuermesszahl = Messbetrag; Einheitswert × Promille = Messbetrag; DM/€-Umrechnung).

---

## 5. Engine-Verhalten je Klasse

### 5.1 MinerU (die gewählte Engine)

**Grundlage:** alle 15 Fixture-Dokumente durch die MinerU-Pipeline, Outputs unter `ocr_comparison/_mineru_runpod_vllm/out_all/`, alle 15 vorhanden und nicht leer (665 Bytes bis 41.457 Bytes, zusammen ~225 KB Text), je Dokument zusätzlich `content_list`/`middle`/`model`-JSONs, `layout.pdf`, `origin.pdf` und extrahierte Bild-Crops. **Datierung:** die Dateizeiten aller 15 Markdowns liegen am **25.06.2026 zwischen 22:06 und 22:23 Uhr** (`stat`, diese Session) — also am selben Abend wie Bake-off und Friendli-Versuche, in der LB-Ära. Eine mündliche Angabe nannte den 29.06.; **der Widerspruch wird hier benannt, nicht aufgelöst** (Dateizeiten sind das härtere Indiz, könnten aber theoretisch einen Kopiervorgang datieren). Für die Qualitätsaussage ist die Ära egal: gleiches Modell, gleicher Container.

**Verhalten nach Klassen — fünf generelle Befunde:**

**(1) Auf den Bescheid-Klassen (A, B) ist die Rettung nahezu vollständig.** Genau dort, wo die Textebene total zerstört ist, liefert MinerU sauberen Fließtext: korrekte Behördennamen statt Substitutions-Müll, vollständige Berechnungsketten als Tabellen, seitenübergreifend konsistente Flurstücks- und Miteigentumslisten, Rechtsbehelfsbelehrungen als lesbaren Text. Barcodes und Siegel landen als Bild-Referenzen bzw. strukturierte Blöcke statt als Textsalat — die Klasse-B-Zonen werden also nicht falsch gelesen, sondern korrekt als Nicht-Text behandelt. Restfehler existieren (vereinzelte Buchstabendreher in Firmennamen, Trennfehler, eine verlesene Barcode-Ziffer) und ändern das Bild nicht.

**(2) BEFUND MIT KONSEQUENZ: Wir haben EIN Ausgabeformat bestellt statt aller — und damit alles verloren, was die Engine nicht als Fließtext einstuft.** Per `grep` über alle 15 Markdowns: **kein** Grundsteuer-/Einheitswert-Markdown enthält das Wort „Aktenzeichen" oder eines der AZ-Ziffernmuster (auch nicht in der zusammengeschriebenen Variante ohne Schrägstriche); auch Steuernummern fehlen durchgängig. Einzige Fundstelle: ein Einheitswert-Aktenzeichen, das im **Fließtext** einer Besteuerungsgrundlagen-Tabelle steht.

**Der Umfang ist größer als „die Kopfzeile" — gemessen am 03.08.2026 über alle 15 content_list-Dateien gegen ihr jeweiliges Markdown.** Betroffen sind **fünf** Blockklassen, nicht eine: `header` (219 von 271 Blöcken nicht im Markdown), `footer` (186 von 205), `aside_text` (94 von 102), `page_number` (29 von 57), `page_footnote` (5 von 5). Die Klasse `text` ist mit 0 von 1214 im Markdown vollständig wiederzufinden. **Diese Null ist ausdrücklich KEIN Freibrief und kein Beleg für Verlustfreiheit** — sie vergleicht zwei Ausgaben derselben Engine miteinander und misst damit interne Konsistenz, nicht Vollständigkeit. Was auf der Seite stand und nie ein Block wurde, kann diese Messung nicht sehen; dafür bräuchte es das Seitenbild als externe Referenz (Abruf-Regel, Nachweispflicht). Und selbst wenn sie es könnte, wäre der Verlust der übrigen vier Klassen kein „begrenzter Schaden": Wir wollen alles.

**Was in diesen Klassen steckt, ist der Kern der Dokumentidentität** — nicht Beiwerk: die Kennungen (Aktenzeichen, Steuernummer, Steuer-Identifikationsnummer), das Aktenzeichen **mit angehängtem Veranlagungsjahr**, das Bescheiddatum, die Formular- und Druckkennungen (`Form.Nr`-Zeilen, Eingangsstempel mit Steuerart und Jahr), die Kontaktdaten des Ausstellers und die Blattzählung des Dokuments. Mehrere dieser Feldarten sind zugleich A1-/A2-Anker des Pass-1-Suchrahmens (Bead `jurgen-oeu`) — der Verlust trifft also nicht irgendeinen Randbereich, sondern gleichzeitig mehrere der stärksten Suchterme des Trägerprojekts. Die konkreten Werte stehen in den Artefakten und im Suchrahmen-Bead; sie gehören nicht in dieses Dokument.

**Der Mechanismus, am 02.08.2026 aus den Layout-JSONs desselben Dokuments bestimmt** — und er ist ein anderer als zunächst vermutet:

```
model.json    → {"type": "header", "angle": 0,
                 "content": "Aktenzeichen 123/456/7890/012/345/6"}
middle.json   → derselbe Text, abgelegt unter:  discarded_blocks[6]
content_list  → {"type": "header", "text": "Aktenzeichen …", "page_idx": 0}
```

Drei Feststellungen daraus: **(a)** Die Erkennung ist fehlerfrei — der Text steht sauber und vollständig da, an fünf Stellen des Dokuments, mit `"angle": 0`, also nicht einmal rotiert. **(b)** Er liegt typisiert vor, nicht als Bild weggeworfen. **(c)** `middle.json` führt ein eigenes Feld **`discarded_blocks`** — die Engine **protokolliert selbst, was sie weglässt**. ⇒ Weder Fehler noch Erkennungsproblem: Sie hat getan, was sie tut, und es angesagt. **Der Fehler war unserer.** Wir haben ein Format bestellt statt aller und nie nachgesehen, was dieses Format weglässt — obwohl das Weggelassene in einer eigens dafür benannten Liste stand, einen Abruf entfernt.

**Der Filter trifft nicht die Kennung, sondern die Zone.** Deshalb fehlen die Bestandteile eines Briefkopfs immer gemeinsam und nie einzeln — und deshalb ist die Frage „welches Feld fehlt?" der falsche Zugriff. Die richtige lautet: **welche Blockklassen enthält diese Ausgabe überhaupt, und welche nicht?** Gegenprobe, die das bestätigt: Absenderzeile, Post-Matrixcode und Barcode-Zeile desselben Kopfbereichs werden sehr wohl gerendert — sie sind als `text` klassifiziert und fallen damit nicht unter den Filter.

**Konsequenz:** Die Daten sind vollständig und typisiert vorhanden — es braucht keinen Workaround und keine Bildanalyse, sondern alle Formate zu bestellen und die Blöcke **aller fünf betroffenen Klassen** (nicht nur `header`) an den Text zu hängen. Damit verschiebt sich die offene Frage von „wie kommen wir an die Kennungen" zu „wie hängen wir die Möblierungs-Blöcke im Write-back an"; der Preis ist mehr Antwort-Volumen pro Job (Größenordnung im Betriebshandbuch, Abschnitt 14a).

**Aber diese Abhilfe hat eine Falle, die man kennen muss, bevor man sie baut: in den Blöcken steht stellenweise Modell-Prosa statt Dokumentinhalt.** Über die 15 Dokumente stehen 30 Fundstellen englischer Erklärungstexte des Modells darüber, warum ein Bildbereich nichts Lesbares enthalte — im selben Feld und mit demselben Typ wie echter Inhalt, überwiegend in `aside_text`, `footer` und `header`. Wer die Blöcke ungefiltert anhängt, schreibt Werkzeug-Kommentare in deutsche Steuerakten und verunreinigt genau den Index, den er reparieren will. **Generell für jede VLM-gestützte Extraktion: ein generatives Werkzeug legt eigene Kommentare in die Datenfelder; jede Übernahme von Rohblöcken braucht deshalb einen Fremdtext-Filter.** Das ist eine eigene kleine Design-Entscheidung des Write-backs, kein Nebensatz.

**Ein zweiter, positiv verwertbarer Fund in derselben Zone:** Die Bescheide tragen dort ihre eigene Blattzählung („Blatt n von m" und Varianten). **Generell: formalisierte Dokumente liefern ihre Vollständigkeitsangabe oft selbst mit** — bevor man eine erwartete Stückzahl von außen konstruiert, lohnt die Frage, ob der Gegenstand sie mitbringt. Wo er es tut, ist sie die bessere Sollgröße, weil sie pro Objekt gilt statt im Mittel (verwertbar für die Vollzähligkeitsprüfung, Betriebshandbuch 11.3 Punkt 12).

**Warum das tragend ist:** Exakte Kennungen sind laut Abschnitt 1 der robusteste Fang auf beschädigtem Material und der Kern der Pass-1-Suchrahmen — und die **kaputte** Textebene enthielt sie (Ziffern sauber, siehe Klasse A). Ein Write-back, das nur das Markdown auf den Record legt, würde für die Bescheid-Klasse die stärksten Suchterme **verlieren, die vorher da waren** — das Ergebnis wäre also schlechter als der heutige beschädigte Ist-Zustand. Konsequenz: Für diesen Korpus ist eine Beschränkung auf das Markdown nachweislich verlustig; es müssen alle Formate bestellt und der Text aus den Layout-Daten angereichert werden. Die Entscheidung selbst gehört zur Write-back-Frage des Trägerprojekts, das nicht in diesem Repository liegt.

**Ungeprüft und deshalb nicht behauptet:** ob die MinerU-Bibliothek selbst eine Option kennt, die Möblierungsklassen im Markdown zu behalten — im Job-Contract des Workers gibt es keine (Nachprüf-Liste, Abschnitt 7).

**(3) Auf dichten Formularen und Handschrift (Klassen D, F) ERFINDET das VLM plausiblen Text.** Belegt per `grep` auf CJK-Zeichen: zwei der 15 Outputs enthalten frei erfundene chinesische Passagen — in einem Fall ein vollständiger Börsenmitteilungs-Satz mitten in einem deutschen Bankformular. Dieselben Dokumente zeigen zerhackte, mehrfach wiederholte Legendenzeilen, verfälschte Eigennamen und in einem Rechnungsdokument zu einer Ziffernkette zusammengelaufene Tabellenspalten. Wichtig für die Einordnung: **die tragende Struktur und die Hauptzahlen dieser Dokumente sind gleichzeitig weitgehend korrekt** — die Erfindungen sitzen in den Rand- und Legendenbereichen. Das macht sie gefährlicher, nicht harmloser: Ein Fehler in einem sonst offensichtlich guten Dokument wird nicht gesucht.
  **Konsequenz (deckungsgleich mit der harten Regel der Engine-Seite):** Der Halluzinations-Verifikationspass auf den schlechtesten Scans ist empirisch begründet, nicht paranoid — und der billige Gegencheck ist der Nachrechen-Test aus Abschnitt 4.

**(4) MinerU rettet exakte Kennungen, die andere Engines zerstören.** Der Beleg aus Klasse C: die von der Textebene als `…1234S6789` verstümmelte IBAN steht im MinerU-Output zweimal korrekt als durchgehende Ziffernfolge. Das ist die positive Kehrseite von (2): wo MinerU den Bereich rendert, sind die Kennungen besser als vorher; das Problem ist ausschließlich, welche Bereiche ins Markdown gelangen.

**(5) Klassen-Sonderverhalten, das man kennen muss.** Kopfstehender oder gespiegelter Druck kommt gespiegelt zurück (im Fixture: eine kopfstehende französische Hinweiszeile). Eine leere Seite kommt als leeres Markdown zurück — **das ist korrektes Verhalten, kein Fehler**; erst das Bild ansehen, dann ggf. neu einreihen (im 593-Seiten-Produktionslauf traf das genau eine Seite, eine echt leere graue Seite mit Kolumnentitel). Handschrift liefert deutlich mehr Substanz als jedes lokale Backend (die ganze Themenliste eines handschriftlichen Notizzettels wird lesbar, wo lokale Engines 0/4 Zielwörter schaffen) — **für Suchzwecke brauchbar, als Wiedergabe nicht vertrauenswürdig.**

### 5.2 Die lokalen Backends (Kreuzberg-Bake-off, 25.06.2026)

**Setup:** kreuzberg `5.0.0-rc.32` (Homebrew, nativer Rust-Build), Aufruf `kreuzberg extract <pdf> --ocr true --force-ocr true --ocr-backend <B> --ocr-language <L> --format text`; tesseract mit `deu` (tessdata_best manuell installiert), paddle-ocr mit `german` (Latin-Familie, Mobile-Tier). Erst 3 Grundsteuer-PDFs (Bead `jurgen-0cu`), dann auf alle 15 Fixture-Dokumente ausgeweitet.

**Engine × Klasse — was womit geht:**

| Engine | Klassen A/B (Bescheide) | Klasse C (Punkt-Insertion) | Klasse D (Handschrift/Formular) | Klasse E (Fremdsprache) | Klasse F (Bank/Notar) | Gesamt-Recovery |
|---|---|---|---|---|---|---|
| eingebettete Textebene | schwach (0–4 von 5) | mittel | 0 | gut | gemischt | **44 %** |
| tesseract `deu` | **stark** (5–6 von 6) | schwach (1/5) | 0 | gut | stark | **72 %** |
| paddle-ocr `german` | **stark** | schwach (1/5) | 0 | gut | stark | **76 %** |
| easyocr (direkt als Lib) | **stark** | **am besten lokal** (4/5) | 0 | gut | stark | **80 %** |
| candle-trocr / candle-glm-ocr | ~0 | ~0 | 0 | ~0 | ~0 | **3 %** |
| MinerU | **stark** (+ Kennungen in JSONs) | **stark** (IBAN gerettet) | **teilweise, mit Erfindungen** | stark | stark, mit Randerfindungen | nicht gescort (Abschnitt 6) |

**Generelle Lesarten:**
- **Kein lokales Backend löst Handschrift.** Alle fünf liefern dort 0 von 4 Zielwörtern. Das ist keine Tuning-Frage, sondern eine Fähigkeitsgrenze.
- **Auf sauber gedruckten Bescheiden sind tesseract, paddle und easyocr praktisch gleichwertig** — die Wahl entscheidet sich an Tempo und Fehlerprofil, nicht an Recovery: tesseract ist ~10–20 % langsamer als paddle, aber genauer; paddle zeigt systematische m→n/a/s-Verwechslungen im Deutschen; easyocr ist der beste lokale Allrounder, aber mit 68–227 s/Dokument um Größenordnungen langsamer als beide (Wert des Vergleichs: 6–9 s).
- **Die Candle-Backends sind unbrauchbar** (3 % Recovery) — und die Roh-Ausgaben zeigen (gelesen 03.08.2026), dass „unbrauchbar" hier zwei **verschiedene Versagensarten** meint, die man auseinanderhalten muss:
  - **trocr halluziniert Kassenzettel:** „ITEM", „TOTAL AMOUNT", „CASHIER", „THANK YOU FOR EVER FREE" — auf allen 15 Dokumenten, gleichgültig was daraufsteht. Das Modell erkennt Text, aber setzt ihn aus dem Vokabular ein, auf das es trainiert wurde.
  - **glm-ocr beschreibt Bilder, statt sie zu lesen** — die interessantere und gefährlichere Art. Seine Ausgabe besteht überwiegend aus Dutzenden **leerer Markdown-Blöcke**, dazwischen frei erfundene Bildunterschriften („Image of a white background with black text that reads **SAVE THE DATE**", an anderer Stelle **BEST BUY NOW**), erfundene Formeln (eine Gleichung, deren rechte Seite das komplette Alphabet ist; bei einem handschriftlichen Notizzettel plötzlich Geometrie-Notation mit Winkeln und Brüchen), **chinesische Tabellenköpfe** (`序号`, `项目`) und Sätze über das Bild statt aus dem Bild („The image contains a single word: …"). **Ein Bildbeschreibungs-Modell, das als Texterkenner eingesetzt wird, liefert Beschreibungen statt Transkriptionen** — und weil die Beschreibungen wohlgeformt sind, sehen sie in einer Ergebnisdatei aus wie Inhalt.
  - **Und beide Versagensarten mischen sich mit Fallback-Ausgaben:** Bei einem der Rechnungsdokumente ist die glm-Datei **byte-identisch** mit der von tesseract und paddle (dasselbe 381-Byte-Fragment), bei einem Bescheid stehen am Ende der glm-Datei **wörtlich die Zeilen der kaputten Textebene**. Eine Ergebnisdatei kann also Halluzination, Fallback und Textebenen-Rest zugleich enthalten — was jede Auswertung wertlos macht, die die Datei als Ausgabe *einer* Engine behandelt.
- **Empfehlung für den lokalen Fallback: tesseract `deu`** (Urteil des Bake-offs). Für die Produktionsstrecke wurde MinerU gewählt.
- **Reproduzierbare Infrastruktur-Fehler, die man wiedererkennen sollte:** HuggingFace-Lock-Datei blockiert paddle-Modell-Download; kreuzbergs Default-Extraktions-Timeout von 60 s reißt bei langsamen Backends; ein PDF mit unrenderbarem Bild-XObject (`Invalid image dimensions`) lässt mehrere Backends leer ausgehen; die reine Textebenen-Extraktion liefert bei 4 von 15 Dokumenten `rc=0` mit 0 Bytes (echte Bild-Scans ohne jede Textebene — die triviale Erkennungsklasse).
- **Nicht gefahren: kreuzbergs `vlm`-Option** (Claude/GPT-4o-Vision). Sie würde die Steuer-PII an einen externen Dienst senden — Datenschutz-Sperre (Abschnitt 6).
- **Nicht geprüft, obwohl naheliegend: die OCR des Betriebssystems (Apple Vision).** Sie ist lokal, kostenlos, ohne Datenschutz-Frage und auf diesem Rechner ohnehin vorhanden — ein Leser fragt zu Recht, warum stattdessen eine GPU-Cloud-Pipeline betrieben wird. **Gemessen wurde sie in diesem Projekt genau einmal, und zwar für eine völlig andere Aufgabe:** Beim Erfassen von Seitenzahlen aus einem festen Bildausschnitt (Bead `jurgen-qku`, 12.07.2026) lieferten Apple Vision und tesseract dasselbe Ergebnis, tesseract war dabei rund viermal schneller (Median 0,06 s gegen 0,26 s über fünf Läufe mit frischem Prozess). **Über den beschädigten deutschen Behördenbestand sagt das nichts** — ein zweistelliger Zahlenzuschnitt ist keine Probe auf Substitutions-Müll, Formular-Kollaps oder Handschrift. Apple Vision ist damit ein **unbewerteter Kandidat**, kein ausgeschiedener: Es lief nie gegen das Fixture-Set. Wer die Frage schließen will, fährt es durch dieselbe Strecke wie jede andere Engine (Abschnitt 3) — der Aufwand ist gering, weil das Verfahren steht.
  **Und die generelle Lehre daran:** Eine Messung trägt nur so weit wie ihr Gegenstand. Dieselbe Engine, dieselbe Zahl, ein anderes Material — und das Ergebnis ist bedeutungslos. Wer eine Messung aus einem Nachbarkontext übernimmt, ohne den Gegenstand zu prüfen, hat nichts gemessen.
- **Nachrüst-Optionen, die der Bake-off offenließ** (heute nur noch für den lokalen Fallback relevant, da MinerU entschieden ist): paddle Server-Tier statt Mobile (~120 MB Download, fairer Vergleich „paddle at its best"), easyocr als Paket statt Direktlauf, ein Candle-Rust-Build mit Features (~13 GB Modelle) für echte lokale VLM-OCR.

### 5.3 Born-digital-Material: warum dieselbe Engine-Wahl aus dem GEGENTEILIGEN Grund gilt

Ein zweiter Korpus (932 Seiten Kursmaterial mit makelloser Textebene: 1195–1317 Zeichen/Seite, korrekte Typographie) beantwortete die Frage, ob die Backend-Entscheidung auch dort trägt — sie war ursprünglich ausdrücklich für **beschädigte Scans** hergeleitet worden, und die damalige Begründung sagte selbst, das Hybrid-Argument gelte für born-digital gerade nicht.

- **Warum dort überhaupt OCR:** nicht für Text, sondern für **Struktur** — Überschriften als Markdown, Tabellen als Tabellen, Formeln als LaTeX. Reine Textextraktion macht aus Formeln Zeichensuppe.
- **Die Messung (gleiche 5 formeldichte Seiten):** `vlm-auto-engine` erkennt **41 Inline-Formeln in 4,1 s**, `hybrid-auto-engine` (effort=high) nur **28 in 8,9 s**. Hybrid verliert 13 von 41 und **lässt Subskripte still fallen** — ein indizierter Zins kommt als nackter Buchstabe zurück. Hybrid ist besser in Typographie und 2,2-mal langsamer.
- **Verdikt:** pures `vlm-auto-engine` auch hier — **aus dem entgegengesetzten Grund**: auf Scans gewinnt VLM, weil kein CV-Detektor Text verschlucken kann; auf born-digital gewinnt es, weil Hybrid die Mathematik plättet. Dass beide Kompartimente dieselbe Antwort geben, darf nicht verdecken, dass es zwei verschiedene Fragen sind.
- **Übertragbare Zusatzbefunde:** Durchsatz auf gerenderten 150-dpi-Seiten ~2,35 s/Seite (gegen ~4,3 s auf großen Screenshot-Bildern) bei ~$0,0015/Seite; 12 Leerseiten, jede am Bild verifiziert; und die Scoping-Lektion — von 2733 Seiten im Zielordner waren **1801 (66 %) Geschäftsberichte mit sauberer Textebene**, für die OCR nichts hinzufügt; nur 932 Seiten waren echtes Kursmaterial. **Eine Anweisung wörtlich auszuführen, ohne zu prüfen, ob sie das Gemeinte trifft, ist keine Gehorsamkeit, sondern Faulheit** — direkt relevant für die Ziellisten-Frage des 30k-Laufs.

[Quelle: gbrain `projects/mineru-ocr/born-digital-backend-benchmark`, vollständig gelesen]

---

## 5.4 Der Roh-Dump-Diff (02.08.2026): was der Vergleich der Engine-Ausgaben ergab

Bis zum 02.08.2026 stützte sich dieses Dokument auf die Urteils-Schicht des Bake-offs (SUMMARY, Kennzahlen, Recovery-Tabelle) und auf die MinerU-Ausgaben. Die **Roh-Ausgaben der lokalen Engines** waren ungelesen. Sie wurden nachgeholt — vollständig für sieben Dokumente über alle Engines (die Handwerker-, Handschrift-, Fremdsprache-, Notar-, Grunderwerbsteuer- und Darlehens-Fälle plus eine Bescheid-Stichprobe), dazu ein systematischer Kennungs-Abgleich über **alle** Dumps. Fünf Befunde, die keine der vorhandenen Tabellen zeigte:

**(1) Alle brauchbaren Engines tragen die Aktenzeichen im Text — MinerUs Markdown nicht, weil sein Header-Filter sie entfernt (Mechanismus in 5.1-2).** Der Abgleich über sämtliche Dumps zeigt: die Aktenzeichen- und Steuernummern-Muster stehen in der **Textebene**, bei **tesseract**, bei **paddle-ocr** und bei **easyocr** — jeweils im Fließtext, unmittelbar neben dem (oft zerschossenen) Wort „Aktenzeichen". Nur `candle-glm-ocr` hat keine einzige Kennung, passend zu seinen 3 % Recovery. **Das verschärft den Befund aus 5.1-2 erheblich:** Der Verlust der Rand-Kennungen im MinerU-**Markdown** ist nicht nur ein Rückschritt gegenüber den Layout-Daten derselben Engine, sondern gegenüber *jeder* Alternative einschließlich des kaputten Ist-Zustands. Für den Bescheid-Korpus ist ein Write-back aus dem nackten Markdown damit nachweislich schlechter als das, was heute schon auf den Records liegt.

**(2) Die Ziffern eines Aktenzeichens überleben auch dort, wo sein eigenes Wort zerfällt — und zwar mehrfach pro Dokument.** In der Stichprobe steht dasselbe Aktenzeichen dreimal (Kopf, Seite 2, Seite 3), die Ziffernfolge jedes Mal vollständig korrekt, während das Wort davor vier verschiedene Verstümmelungen zeigt. Das ist die empirische Grundlage der Regel „exakte Kennungen sind der robusteste Fang" — und zugleich der Grund, warum man nach der **Ziffernfolge** suchen muss und nie nach dem Wort.

**(3) Der PDF-Renderer ist ein eigener Ausfallpunkt, unabhängig von der OCR-Qualität.** Ein Dokument des Sets (ein Darlehensvertrag) liefert bei Textebene, tesseract, paddle-ocr und candle-glm-ocr jeweils **null Bytes** — Ursache ist ein unrenderbares Bild-Objekt im PDF, an dem die gemeinsame Rendering-Bibliothek aussteigt. **easyocr, das über einen anderen Renderer lief, verarbeitete dasselbe Dokument vollständig** (37 KB Text, alle Kontonummern, Flurstücke und Beträge lesbar) — und MinerU ebenfalls, weil es ebenfalls selbst rendert. Konsequenz für die Erkennung: Ein leeres Ergebnis beweist **nicht**, dass ein Dokument kein Text hergibt; es kann auch heißen, dass dieser eine Renderer an dieser einen Datei scheitert. Ein zweiter Renderer ist deshalb ein wirksamer Rettungspfad, kein Luxus.

**(4) Punkt-Insertion ist ein Textebenen-Erbe, kein OCR-Schicksal.** Bei dem Rechnungsdokument mit dem Punkt-Insertions-Profil zeigen Textebene und MinerU die Punkte (eine Straßenangabe mit eingestreutem Punkt), **easyocr liest dieselbe Stelle sauber** (Straße, Hausnummer, PLZ und Ort). Wer das Bild neu erkennt, erbt die Serifen-Fehldeutung nicht. Umgekehrt scheitern tesseract und paddle-ocr an diesem Dokument aus einem ganz anderen Grund: Sie liefern nur ein 381-Byte-Fragment — **nicht falscher Text, sondern fast gar keiner**. Die 1-von-5-Recovery dieser beiden Engines misst also einen Extraktions-, keinen Erkennungsfehler.

**(5) Wo eine Engine Kennungen rettet, tut sie es an manchen Stellen und an anderen nicht.** Die im Schadenskatalog als „unbrauchbar" geführte IBAN steht im selben Dokument zweimal — auf der ersten Rechnung verstümmelt, auf der zweiten **auch in der Textebene korrekt**. Das relativiert die frühere Formulierung, MinerU habe diese Kennung „gerettet": MinerU liefert sie zweimal korrekt, die Textebene einmal von zwei, easyocr zweimal korrekt. Die generelle Lehre bleibt und wird präziser: **Mehrfachvorkommen derselben Kennung ist der eigentliche Rettungsmechanismus** — wer eine Kennung sucht, sollte jedes Vorkommen im Dokument auswerten und nicht das erste nehmen.

**Ungelesen bleiben** die Roh-Ausgaben der großen Bescheide (2017er und 2021er Einheitswert, zwei der drei 2022er), der Einkommensteuer-, HVB- und Depot-Dokumente in den lokalen Engines sowie `candle-glm-ocr` insgesamt — zusammen etwa 450 KB. Für diese Dokumente stützen sich die Aussagen weiter auf die Kennzahlen, die Recovery-Tabelle und den Kennungs-Abgleich, der über **alle** Dateien lief. Der Grund für den Abbruch ist Aufwand gegen Ertrag, nicht Vollständigkeitsverzicht: Die genannten Dokumente sind quantitativ ausgewertet und ihre Klasse ist durch die gelesenen Stichproben belegt.

---

## 6. Ground Truth und die PII-Grenze

**Für keines der 15 Dokumente existiert eine Ground Truth.** Die Textebene scheidet als Referenz aus (Abschnitt 4, Falle 1). Ein Skript zur Erzeugung per externem Vision-Modell existiert (`ocr_comparison/gemini_truth.sh`) und trägt in seinem eigenen Kopf die Regel, die für die ganze Strecke gilt: *„Run by YOU (the user), not the agent — the agent is hard-blocked from uploading the tax PII to OpenRouter/Gemini."* Das Zielverzeichnis existiert nicht ⇒ **das Skript ist nie gelaufen.** Dasselbe Prinzip hielt die kreuzberg-`vlm`-Option aus dem Bake-off heraus. **Generelle Regel: PII verlässt die Maschine Richtung Dritt-Dienste nur durch Jurgens eigene Hand und Entscheidung** — für jeden künftigen externen Dienst neu zu entscheiden (die RunPod-Nutzung ist durch die EU-Platzierungs-Entscheidung abgedeckt).

**Fehlgeschlagene Zwischenanläufe (Beweissicherung, kein Qualitätsbefund):**
- Zwei Failure-Logs zeigen die zwei Infrastruktur-Symptome, die man wiedererkennen muss: 15/15 Dokumente mit HTTP 402 `Insufficient Balance` (leeres Konto) und 15/15 mit HTTP 401 `invalid api key` gegen eine URL, die in Wahrheit auf einen **gelöschten** Endpoint zeigte — „401" hieß dort „Ziel existiert nicht mehr".
- Zwei Läufe gegen einen fremdgehosteten VLM-Endpoint (`vlm-http-client` gegen Friendli) erzeugten **keinen einzigen brauchbaren Output, aus drei verschiedenen Ursachen** (Logs am 01.08.2026 gelesen): (a) je Lauf genau ein Task mit leerer Modellantwort; (b) die Masse mit HTTP 503 „Endpoint is unavailable" (dahinter sichtbar: connection refused zum Serving-Prozess); (c) **der interessante Fall** — vier Batches liefen komplett durch, der Endpoint antwortete mit echtem OCR-Text (inklusive bemerkenswert guter Handschrift-Lesungen), aber **jede Seite warf einen Layout-Format-Fehler**: Auf MinerUs Layout-Detection-Prompt lieferte das dort servierte Modell Tabellen-Zellen statt Layout-Boxen, MinerUs Client verwarf alles, und genau diese Dokumente liegen als 0-Byte-Markdown auf Platte. **Das ist ein echter Kompatibilitätsbefund** (naheliegende, aber unbewiesene Deutung: das servierte Modell/die Quantisierung beherrschte den Layout-Schritt des Zwei-Stufen-Protokolls nicht) — **generelle Lehre: wer je wieder ein fremdgehostetes VLM erwägt, muss die Layout-Detection-Kompatibilität testen, nicht nur die Erreichbarkeit pingen.** Über MinerU auf dem eigenen Worker sagt all das nichts; der Pfad wurde nicht weiterverfolgt.
- Ein Einzeltest gegen ein anderes Image ist byte-identisch mit dem RunPod-Output (`diff`, diese Session) — Beleg, dass das Ergebnis vom Modell kommt, nicht von der Umgebung.

---

## 7. Was offen ist

1. **Der formale Per-Dokument-Diff MinerU-Output gegen die `OCR-Befunde.md`-Paare** — nie durchgeführt. Methode: je Dokument (a) jeden Müll-Schnipsel im Output nachschlagen (korrekt / fehlend / anders falsch), (b) exakte Kennungen gesondert prüfen — **auch in `content_list`/Bild-Crops, nicht nur im Markdown** (Befund 5.1-2), (c) Rechenketten nachrechnen (Befund 5.1-3), (d) je Klasse verdichten. Die Befunde in 5.1 sind die ersten Zeilen dieses Diffs; er würde zugleich MinerU in die Engine-Matrix eintragen (dort heute „nicht gescort").
2. **Der Halluzinations-Verifikationspass auf dem Steuer-Korpus** — der Nachrechen-Test ist auf born-digital-Material exerziert, auf den Bescheiden nie; dort wiegt er schwerer (Akzeptanzkriterium des 30k-Laufs).
3. **Ground Truth** — existiert nicht; Erzeugung ist eine PII-Entscheidung (Abschnitt 6).
4. **Erkennungs-/Ziellisten-Kalibrierung** — Methodik und Fixture liegen bereit (hier Abschnitt 3), der Lauf steht aus.
5. **`jurgen-0cu`** (Bead „OCR-Backend-Vergleich", seit 25.06. in Arbeit) ist durch dieses Dokument inhaltlich abgeschlossen und kann geschlossen werden — Jurgens Entscheidung.

**Durch den Roh-Dump-Diff GEKLÄRT (02.08.2026) — beide vorher auf der Nachprüf-Liste:**
- **Der Firmennamen-Widerspruch (Erwerber gegen Vertragspartner) ist aufgelöst — durch fünf unabhängige Belege.** Vier Engines (tesseract, paddle-ocr, easyocr, MinerU) lesen übereinstimmend **denselben Aufbau**: der Adressat/Erwerber ist die Immobilien-Gesellschaft des Bestands, und **separat darunter, im Feld „Vertragspartner"**, steht eine andere Gesellschaft — die Verkäuferseite des beurkundeten Kaufvertrags. **Der fünfte und entscheidende Beleg stand in der Methodik des Trägerprojekts** (Absender-Pooling; sie liegt nicht in diesem Repository): Dort war die Vertragspartei als **bekannter Bauträger** des Projekts gelistet, in einer Reihe mit weiteren Bauträgern. Der Name ist damit nachweislich ein realer Firmenname aus dem Umfeld dieses Bestands und keine Verlesung des Erwerbernamens. Die Korrektur-Glosse in `OCR-Befunde.md` **verwechselt Erwerber- und Vertragspartner-Feld**; der Eintrag im Schadenskatalog ist ein Fehleintrag, kein OCR-Schaden — und sollte dort korrigiert werden, damit er nicht als Kalibrier-Sollwert weiterwirkt.
- **Die Adresse des Handwerkerbetriebs ist bestätigt.** Zwei unabhängige Engines lesen dieselbe Struktur (easyocr „Am Mulbech $ 12345 Beispielhausn", MinerU „Am Mühlbach 9, 12345 Beispielhausen") — es handelt sich um eine Straße am Mühlbach in Beispielhausen. Keine Nachprüfung mehr nötig.

**Weiter offen — Original ansehen, bevor daraus zitiert wird:**
- Eine Betragszeile („anzulegender Betrag 1.000.000 EUR") in einem Depot-Eröffnungsantrag — plausibel wirkend, aber unverifiziert gegen das Original.
- Ein von den übrigen abweichendes Quittungsdatum im Fremdsprachen-Dokument. **Neue Evidenz:** Die vier Quittungen tragen bei allen Engines übereinstimmend 19/05/2025 — außer der einen, bei der die Textebene gar kein Datum liefert, tesseract nur Fragmente und MinerU „19/05/2017" liest. Da alle übrigen Belege desselben Tages sind, spricht mehr für einen Lesefehler als für ein abweichendes Vorlagedatum; **entschieden ist es nicht.** (Der im Katalog dokumentierte Jahresfehler `7076` ist dagegen bestätigt: Textebene und tesseract lesen ihn beide, MinerU liest an derselben Stelle korrekt 2025.)
- **NEU: Die Urkundennummer des Handelsregister-Dokuments ist zwischen den Engines uneins** — Textebene „99:9 /2099X", tesseract „997 12099X", MinerU „999 /2099 X". Drei Lesungen, zwei Kandidaten (999 oder 997). Falls die Nummer je als Kennung benutzt werden soll, muss das Original entscheiden.
- **NEU: Zwei Steuernummern derselben Stammnummer für die KG.** Der Grunderwerbsteuer-Bescheid trägt bei tesseract, paddle-ocr und easyocr übereinstimmend `123/456/78901`; der geerntete Kennungsbestand des Pass-1-Suchrahmens führt für dieselbe Gesellschaft `123/456/23456`. Stamm identisch, Endnummer verschieden — entweder zwei echte Nummern (verschiedene Steuerarten oder Zeiträume) oder ein Erntefehler. Da drei Engines übereinstimmen, ist die Bescheid-Lesung belastbar; welche Nummer wofür gilt, ist offen.
- Eine Kfz-Steuernummer aus dem Kennungs-Erntebestand ist OCR-unsicher (ein Buchstabe ist ein Artefakt) — Sichtprüfung nötig, falls sie je tragend wird.
- **NEU (Werkzeugfrage, nicht Dokumentfrage): Kennt die MinerU-Bibliothek eine Option, Kopf-/Fußzeilen im Markdown zu behalten?** Der Job-Contract des Workers hat keine; ob die Bibliothek darunter eine besitzt (und ob der Worker sie durchreichen würde), ist ungeprüft. Falls ja, wäre das der einfachere Weg als das Anhängen der `header`-Einträge aus `content_list`. Beide Wege führen zum selben Ziel — die Frage ist nur, welcher weniger Code kostet.

---

## 8. Messlauf MinerU 4 (26.10.2026, Mineroo#19)

**Gegenstand:** MinerU 4.0.12, Stufe `advanced`, OCR erzwungen, Bildanalyse an, Image `2bccaebd0`, RTX 4090 (24 GB), EWR. Abgeschickt über `mineru-ocr ocr` und den Daemon (`--concurrency 4`) in eine eigene Datenbank `mineroo_messlauf`. Die Texte der Dokumente stehen nur lokal in der Lesemappe des Eigentümers (`Mineroo-Arbeitsstand-2026-10-24/messlauf-19/lesemappe/`), nicht hier.

**Material, 20 Dokumente mit 102 Seiten:** die 15 Fixture-Dokumente aus Abschnitt 3 (97 Seiten) und fünf Formel-Folien des Kursmaterials, je eine Seite (B819 „Binomial model“ S. 7, 8, 12, 21; „Black–Scholes model“ S. 8). Welche fünf Seiten die Hybrid-gegen-VLM-Messung aus 5.3 benutzte, lässt sich nicht mehr feststellen: Die gbrain-Seite gibt es nicht mehr. Gewählt sind deshalb die fünf formeldichtesten Folienseiten nach der 3.4-Ausgabe. Die Vergleichsfassung 3.4 ist für die Fixtures die aus 5.1 (Juni 2026), für die Folien die des Kursmaterial-Laufs (Juli 2026). Alle 20 Dokumente endeten `done`, ohne Vermerk.

### 8.1 Bewertungsweise (vom Eigentümer, nicht automatisch)

Je Fixture-Dokument, gegen `rendered_markdown` (MinerU 4) und das 3.4-Markdown:

1. jeden Müll-Schnipsel aus `OCR-Befunde.md` nachschlagen: korrekt, fehlt oder anders falsch;
2. Kennungen gesondert prüfen, jedes Vorkommen. In MinerU 4 kommen sie auch aus Kopf- und Fußzeile. Dem 3.4-Markdown fehlen vier Randklassen (5.1-2), dort ist ihr Fehlen kein Lesefehler;
3. Rechenketten der Bescheide nachrechnen (Abschnitt 4);
4. auf Erfindungen achten (CJK-Passagen, wiederholte Legenden, verfälschte Eigennamen);
5. je Klasse urteilen: besser, gleich oder schlechter. Keine Gesamtzahl bilden.

Je Folie: Formeln gegen die Folie zählen, Subskripte prüfen, und an Folie S. 12 eine Rechenkette nachrechnen (uS, dS, C_u, C_d und die Gleichung des Hedge-Portfolios).

**Hinweise aus den Kennzahlen für das Lesen.** Die Kennzahlen stammen aus Abschnitt 4 und werden über beide Fassungen gezählt.

- MinerU 4 liefert auf den Bescheiden 4–13 % mehr Zeichen; die Randklassen stehen jetzt im Text.
- Mojibake und Junk-Zeilen sind in beiden Fassungen gleich, bis auf ±2.
- In den sechs Grundsteuer- und Einkommensteuer-Bescheiden stehen 3–8 englische Sätze des Modells über Bildbereiche im Text, Muster „The image provided is a QR code …“. Die 3.4-Fassung hat 0. Das ist die Falle aus 5.1-2, jetzt im Abnehmer-Text, weil die Randklassen mitkommen.
- Auf den Folien setzt MinerU 4 weniger `$…$`: 11/12/13/11/28 gegen 21/16/15/17/41 bei 3.4. Ob dabei Mathematik verloren geht oder nur Einzelbuchstaben nicht mehr gesetzt werden, entscheidet der Blick auf die Folie. `$R_{f}$` und `$N(d_{1})$` sind erhalten.
- Beim Handschrift-Dokument meldete MinerU im Worker-Log eine am Längenlimit abgeschnittene Ausgabe und unbekannte Blocktypen (`list_block`).

| Nr | Seiten | Zeichen 4 / 3.4 | Alpha-Token | Mojibake | Junk-Zeilen | Inline-Formeln | CJK | Modell-Prosa (en) | Bild-Deutungen |
|---|---|---|---|---|---|---|---|---|---|
| 1 | 8 | 34106 / 32873 | 4071 / 4037 | 3 / 2 | 1 / 1 | 9 / 7 | 0 / 0 | 0 / 0 | 0 / 0 |
| 2 | 8 | 38695 / 37129 | 4409 / 4353 | 9 / 9 | 1 / 1 | 0 / 0 | 0 / 0 | 0 / 0 | 1 / 1 |
| 3 | 10 | 23745 / 20932 | 2586 / 2443 | 25 / 25 | 1 / 1 | 0 / 0 | 0 / 0 | 8 / 0 | 2 / 2 |
| 4 | 8 | 15117 / 13356 | 1831 / 1639 | 10 / 10 | 0 / 0 | 3 / 3 | 0 / 0 | 8 / 0 | 2 / 2 |
| 5 | 16 | 45092 / 40957 | 5150 / 4695 | 20 / 20 | 0 / 0 | 0 / 0 | 0 / 0 | 7 / 0 | 8 / 8 |
| 6 | 6 | 12829 / 11550 | 1455 / 1368 | 9 / 11 | 0 / 0 | 7 / 7 | 0 / 0 | 4 / 0 | 2 / 2 |
| 7 | 6 | 12427 / 11226 | 1419 / 1308 | 13 / 13 | 0 / 0 | 0 / 0 | 0 / 0 | 4 / 0 | 2 / 2 |
| 8 | 6 | 12375 / 11118 | 1403 / 1305 | 13 / 13 | 0 / 0 | 0 / 0 | 0 / 0 | 3 / 0 | 2 / 2 |
| 9 | 1 | 1156 / 654 | 123 / 65 | 0 / 0 | 0 / 0 | 0 / 0 | 1 / 0 | 0 / 0 | 1 / 1 |
| 10 | 1 | 1866 / 1818 | 174 / 199 | 1 / 1 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| 11 | 14 | 31148 / 30993 | 3610 / 3908 | 0 / 0 | 3 / 4 | 0 / 0 | 32 / 32 | 0 / 0 | 0 / 0 |
| 12 | 4 | 3133 / 3025 | 376 / 357 | 1 / 1 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 2 / 2 |
| 13 | 3 | 3096 / 2613 | 370 / 299 | 0 / 0 | 1 / 1 | 0 / 0 | 0 / 0 | 0 / 0 | 2 / 2 |
| 14 | 5 | 1382 / 1369 | 161 / 159 | 3 / 3 | 0 / 0 | 0 / 0 | 2 / 2 | 0 / 0 | 0 / 0 |
| 15 | 1 | 2538 / 2547 | 219 / 217 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| 16 | 1 | 2538 / 2527 | 380 / 374 | 1 / 0 | 0 / 0 | 11 / 21 | 0 / 0 | 0 / 0 | 0 / 0 |
| 17 | 1 | 2278 / 2233 | 383 / 395 | 1 / 0 | 0 / 0 | 12 / 16 | 0 / 0 | 0 / 0 | 0 / 0 |
| 18 | 1 | 2479 / 2421 | 346 / 340 | 29 / 28 | 0 / 0 | 13 / 15 | 0 / 0 | 0 / 0 | 2 / 2 |
| 19 | 1 | 2462 / 2335 | 330 / 315 | 1 / 0 | 0 / 0 | 11 / 17 | 0 / 0 | 0 / 0 | 0 / 0 |
| 20 | 1 | 3014 / 2925 | 439 / 426 | 1 / 0 | 0 / 0 | 28 / 41 | 0 / 0 | 0 / 0 | 0 / 0 |

Die Nummern 1–15 folgen der Dateiordnung des Fixture-Ordners. Welche Nummer welches Dokument ist, steht nur in der lokalen Lesemappe (`index.json`). Mojibake zählt `®£±¥©…`. Junk-Zeilen sind Zeilen mit drei oder mehr Doppelpunkten. „Modell-Prosa (en)“ ist eine Mustersuche nach Sätzen wie „the image …“ oder „appears to …“, also eine untere Schranke. **Das Urteil des Eigentümers steht noch aus** und gehört hierher, als Zählung und Urteil je Klasse.

### 8.2 Messwerte

Alle Zeiten stammen aus dem Rohergebnis (`raw.envelopes[*]`: `delayTime`, `executionTime`, `output_meta.debug.phase_ms`), ohne neuen Code. Die Kosten rechnen mit 1,10 USD/h für die 4090 serverless (0,000306 USD/s).

| Größe | Wert |
|---|---|
| Teilstücke / Seiten | 25 / 102 |
| Ausführung je Seite | Mittel 1,84 s; je Teilstück Median 1,92 s, Spanne 1,11–5,03 s; davon MinerU-Parse 85 % |
| Kosten je Seite, nur Ausführung | 0,00056 USD |
| Kosten des Blocks laut Guthaben | 0,322 USD (2,486 → 2,164), mit vier Kaltstarts und Leerlauf; also 0,0032 USD je Seite bei einem Lauf dieser Größe |
| Warten bei RunPod, warmer Worker | Median 94 ms, höchstens 1,7 s |
| Kaltstart nach längerem Leerlauf (Dauer nicht festgehalten) | 104–128 s (4 Worker) |
| Kaltstart nach 4,5 min Leerlauf | 1,3 s (FlashBoot holt den Worker samt geladenem Modell zurück) |
| Kaltstart nach 15 min Leerlauf | 124,8 s (neuer Worker) |
| Aufschlüsselung eines Kaltstarts (Worker-Log) | 114 s vom Start bis zur Bereitschaft: vLLM-Engine 89 s (davon torch.compile 18 s), Aufwärm-Parse einer Testseite 15 s |
| größte Antwort von `/status` | 6,05 MB (10 Seiten, 605 KB/Seite) |
| größte Antwort je Seite | 729 KB/Seite (Teilstück mit 7 Seiten, 5,10 MB) |
| Antwort bei 20 Seiten dieser Dichte, hochgerechnet | ≈ 14,6 MB |

**Antwortgröße gegen `/run`:** Keine Antwort erreichte die angebliche Grenze von 10 MB; bestätigt oder widerlegt ist sie damit nicht. Hochgerechnet läge ein volles Teilstück mit 20 Seiten der dichtesten Scans bei rund 14,6 MB: über 10 MB, unter den 20 MB, die der Worker erzwingt. Gilt die 10-MB-Grenze, tragen höchstens 13 solche Seiten (13 × 729 KB ≈ 9,5 MB). Die Entscheidung (Teilstückgröße oder Speicherweg) liegt beim Eigentümer.

**Nachtrag 27.10.2026 (Mineroo#20): ein Live-Job zur Antwortgrenze.** Abgeschickt über `RunPodClient` ein Teilstück aus 24 erfundenen dichten Scan-Seiten (ganzseitige Rasterbilder mit Text, zwei Fotos und einer Tabelle je Seite; 6,02 MB roh, 8,02 MB Base64, also auch eine Anfrage über 8 MB, die `/run` annahm). Job `3346c0d0-f384-4c78-ae15-a705d0940ced-e1`, Warteschlange samt Kaltstart 139 s, Ausführung 34,1 s (1,4 s je Seite). Ergebnis `FAILED` mit `ResponseTooLargeError` des Workers: Die Ausgabe hatte 26,7 MB (1,1 MB je Seite), über den 19,4 MB, die der Worker beim Transport `tarball_b64` zurückgibt. Der Worker begründet seine Abweisung damit, dass RunPod eine Antwort auf `/runsync` bei 20 MB und auf `/run` bei der Hälfte kappt und das Gateway eine zu große Antwort verwirft, den Job aber trotzdem `COMPLETED` ohne Ausgabe meldet. Eine Antwort über 10 MB ist also auch hier nicht angekommen. Das Paket rechnet deshalb mit 10 MB: Mit 25 % Abstand tragen die dichtesten Scans des Messlaufs 10 Seiten je Teilstück (`max_pages_per_chunk`). Kosten gerechnet rund 0,07 USD (Kaltstart, Ausführung und 60 s Leerlauf zu 0,000306 USD/s); das Guthaben vor dem Job ist nicht festgehalten, danach stand es bei 1,645 USD.

**GPU-Speicheranteil.** MinerU 4.0.12 setzt für vLLM `gpu_memory_utilization = max(6 / VRAM_GB, 0,1)` bei mehr als 8 GB (`mineru/model/vlm/engine_utils.py`). Auf der 4090 sind das 0,25. Laut Worker-Log stehen damit 2,17 GiB KV-Cache bereit, 189.440 Tokens, genug für 23 gleichzeitige Anfragen zu je 8.192 Tokens. `MINERU_VIRTUAL_VRAM_SIZE` ersetzt nur die erkannte Größe: Ein **kleinerer** Wert ergibt einen **höheren** Anteil. Mit `12` waren es 0,5, also 8,05 GiB, 703.200 Tokens und 85,8 gleichzeitige Anfragen. Gemessen an denselben 3 Dokumenten (7 Teilstücke, 40 Seiten) betrug die Ausführung zusammen 59,4 s bei 0,25 und 64,3 s bei 0,5. Ein Zwischenlauf, in dem 6 der 7 Teilstücke noch auf einem Worker mit 0,25 liefen, brauchte 67,6 s. Ein höherer Anteil bringt keinen Durchsatz: Bei `MINERU_MAX_CONCURRENCY=1` und höchstens 20 Seiten je Teilstück reicht schon der Cache der Voreinstellung. Bei gleicher Einstellung streut dasselbe Teilstück zwischen zwei Läufen um bis zu 46 %. **Der Endpoint bleibt bei MinerUs Voreinstellung; die Variable ist nicht gesetzt.**

**`executionTimeout`** steht seit dem 26.10.2026 auf **300 s** (vorher 600 s). Das längste Teilstück lief 17,8 s, die langsamste Seite 5,0 s. 20 Seiten × 5 s ergeben 100 s, dazu ein dreifacher Puffer. Der Sollwert des Preflights (`execution_timeout_ms`, Voreinstellung 600000) muss folgen.

**Leerlaufzeit:** bleibt Entscheidung des Eigentümers. Die Grundlage sind die drei Kaltstart-Zeilen oben. Bis 4,5 min Leerlauf kostet ein neuer Job 1,3 s, nach 15 min wieder rund 2 min. Wo die Grenze dazwischen liegt, ist nicht gemessen.
