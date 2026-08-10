# OCR-Strecke für die DEVONthink-Klassifikation — Einstieg, übertragbare Lehren, Pflegeregeln

**Stand:** 05.08.2026.
**Wozu diese Datei:** Sie ist der Einstieg für jemanden mit null Vorkontext. Sie sagt, was hier liegt, in welcher Reihenfolge man liest, was das Vorhaben überhaupt ist, welche Lehren aus ihm über es hinaus tragen, wie dieses Dokumentenset gepflegt wird und welche Artefakte außerhalb dieses Verzeichnisses liegen. Sie ersetzt keines der vier Fachdokumente, sie führt in sie hinein.

---

## 1. Der Gegenstand in fünf Sätzen

In einer DEVONthink-Datenbank liegt das Dokumentenarchiv eines Family Office; es soll klassifiziert werden, und die Klassifikation arbeitet mit Text-Detektoren. Ein großer Teil des Bestands hat keine oder — der schwierigere Fall — eine **vorhandene, aber zerstörte** Textebene, weshalb diese Detektoren blind sind. Die gewählte Abhilfe ist eine OCR-Pipeline auf Basis von MinerU, die auf einem queue-basierten Serverless-Endpoint bei RunPod läuft und pro Seite ein Ergebnis liefert; sie ist entschieden und in zwei echten Läufen bewiesen. Was davor und danach liegt — welche Dokumente überhaupt behandelt werden müssen, wie sie aus der Datenbank heraus- und der Text wieder hineinkommt — ist entworfen, aber überwiegend **offen**. Der große Produktionslauf über den Gesamtbestand hat noch nicht stattgefunden.

---

## 2. Was hier liegt und in welcher Reihenfolge man es liest

Die vier Fachdokumente sind so geschnitten, dass jedes für sich lesbar ist; sie überschneiden sich bewusst dort, wo eine Aussage an beiden Orten gebraucht wird.

| Reihenfolge | Dokument | Wofür |
|---|---|---|
| 1 | **[MinerU-RunPod-Geschichte-und-Entscheidungen.md](MinerU-RunPod-Geschichte-und-Entscheidungen.md)** | Woher das Projekt kommt, welche Entscheidungen mit welcher Begründung gefallen sind, was zurückgenommen wurde, was offen ist. Enthält das Glossar der Werkzeugbegriffe — **wer die anderen Dokumente ohne dieses Glossar liest, stolpert ab der ersten Quellenangabe.** |
| 2 | **[MinerU-RunPod-Betriebshandbuch.md](MinerU-RunPod-Betriebshandbuch.md)** | Wie die Engine betrieben wird: Architektur, feste Infrastrukturfakten, Deploy, Job-Contract, Lifecycle von Vorflug bis Teardown, Abrechnungs-Wahrheiten, die vorhandenen Skripte und die Anforderungen an den künftigen Driver, die harten Regeln. |
| 3 | **[MinerU-DEVONthink-Integrationsstrecke.md](MinerU-DEVONthink-Integrationsstrecke.md)** | Die Stufen vor und nach der Engine: Erkennung (welche Dokumente brauchen Behandlung), Export, Write-back, Statusführung. Hier steckt der Großteil der noch offenen Entscheidungen. |
| 4 | **[MinerU-OCR-Kalibrierung-und-Backend-Vergleich.md](MinerU-OCR-Kalibrierung-und-Backend-Vergleich.md)** | Die Beweisschicht: welche Schadensklassen der Bestand hat, was die Engines auf ihnen leisten, und wie man OCR-Qualität misst, ohne sich selbst zu betrügen. |

**Wer nur etwas nachschlagen will**, findet in Betriebshandbuch 17 und Integrationsstrecke 9 je eine Tabelle „Nachprüf-Orte": Behauptung → wo man den echten Zustand prüft.

**Ebenfalls in diesem Verzeichnis, aber anderer Gegenstand:** `Klassifizierungs-Handbuch.md` und `Entscheidungsleitfaden-Offene-Punkte.md` beschreiben die Klassifikation selbst — die Suchrahmen, die Tag-Struktur, die Pass-Struktur. OCR ist dort Voraussetzung, nicht Thema. **Achtung, dort steht ein bekannter Fehler:** Das Handbuch führt unter „Was bereits gebaut ist" auf, jedes Dokument habe eine durchsuchbare Textebene. Das ist widerlegt und der Anlass für dieses ganze Vorhaben (Geschichte, Abschnitt 6, Punkt 11).

**Die übrigen Dateien im Repo-Wurzelverzeichnis** (`StatusQuoDiscussion.md`, `ChatGPTOpinion.md`, `ChatGPTOpinion2.md`, `RENDERED_TAGGER_PROMPT.md`, die drei `Prompt1_Answer_*.md`) sind Diskussions- und Recherchestände aus der Frühphase des Trägerprojekts. Sie sind **nicht** Teil dieser Dokumentation und für die OCR-Strecke ungeprüft.

---

## 3. Die übertragbaren Lehren

Der Teil dieses Projekts, der über es hinaus gilt. Jede Lehre ist teuer bezahlt; wo der volle Fall steht, ist angegeben. Sie sind bewusst ohne Werkzeugnamen formuliert — ein Werkzeug ist der Anlass einer Lehre, nie ihr Geltungsbereich.

**1. Beim Abruf wird alles geholt — und fehlt hinterher etwas, haben WIR verkackt, nie das Werkzeug.**
Kein Vorfiltern, keine Abwägung, keine Begründungspflicht. Was verwendet wird, ist eine getrennte und jederzeit änderbare Entscheidung; der Abruf ist es nicht. Die Schuldzuweisung gehört zur Regel: Wer einen Verlust als Werkzeug-Eigenart erzählt, lernt eine Eigenart und fängt beim nächsten Werkzeug von vorn an; wer ihn sich zuschreibt, lernt ein Verfahren. Dazu die Nachweispflicht — **dass alles da ist, wird gegen eine Referenz von außerhalb der Kette belegt**, nie durch den Vergleich zweier Ausgaben desselben Werkzeugs, der nur interne Konsistenz misst. Warum die Regel absolut sein muss: Der Verlust ist asymmetrisch (zu viel geholt ist reversibel, zu wenig kostet den ganzen Lauf), er wirkt zum falschen Zeitpunkt (beim Abruf weiß niemand, was später gebraucht wird), und er ist **unsichtbar** — fehlende Daten erzeugen kein Fehlersignal. → Betriebshandbuch 14a, Geschichte 4.2a.

**2. Ein Mechanismus, der stattgefunden hat, hat deshalb noch kein Geld gekostet — und bevor etwas „Messung" heißt, muss bewiesen sein, dass das Gemessene überhaupt passiert ist.**
Null Kosten für null Arbeit sind kein Befund, sondern eine Tautologie. Eine Behauptung härtet außerdem durch Zitation statt durch Beleg: Was einmal in einem Wissensspeicher steht, kommt mit Quellen-Autorität zurück. → Geschichte 4.1.

**3. Nur gegen das echte Ding verifizieren.**
Aggregat-Zähler und Statusfelder lügen; die Einzelaufstellung hält die Wahrheit. Bereitschaft ist der Dienst, der wirklich antwortet, nie ein Feld, das „läuft" sagt. Nach jeder Änderung wird der Ist-Zustand zurückgelesen, nicht die Konfiguration geglaubt — Konfiguration ist nicht Zustand und räumt nichts auf. Und nie eine Vermutung als Entscheidung erzählen: Unverifiziertes wird als unverifiziert ausgesprochen. → Betriebshandbuch 9.4, 15.

**4. Auf Fehler wird subtraktiv reagiert, nie additiv.**
Anhalten und die Primärquelle lesen — nicht denselben Aufruf wiederholen, nicht die Parallelität erhöhen, nicht noch eine Instanz starten, nicht nochmal neu starten. Nach ein, zwei gleichförmigen Fehlern gilt: Logs lesen, nicht wiederholen. Ohne begründeten nächsten Zug: stoppen und fragen. → Betriebshandbuch 15.

**5. Vor dem Stapellauf an wenigen Stücken testen und das Ergebnis mit eigenen Augen lesen.**
Nicht die Rückmeldung des Werkzeugs lesen — das Ergebnis. Der Fehler, der einen Stapellauf ruiniert, ist fast nie der, der eine Fehlermeldung erzeugt. → Betriebshandbuch 9.1, 15.

**6. Ein Auftrag wird am Material geprüft, nicht am Ordner.**
Von 2733 Seiten in einem Zielverzeichnis waren zwei Drittel Material, für das die Verarbeitung nichts hinzufügt. Eine Anweisung wörtlich auszuführen, ohne zu prüfen, ob sie das Gemeinte trifft, ist keine Gehorsamkeit, sondern Faulheit. → Kalibrierung 5.3.

**7. Stille Rückfälle entlarven, bevor man Zahlen glaubt.**
Ein Werkzeug kann eine Anfrage annehmen, sie unbemerkt an eine andere Instanz weiterreichen und trotzdem „erfolgreich" melden. Der billige Test: **Byte-Vergleich zwischen angeblich verschiedenen Erzeugern.** Wo Dateien identisch sind, hat man Kopien gemessen und nicht Alternativen. Ebenso: Eine Verarbeitungskette kann intern wissen, dass sie versagt hat, und trotzdem etwas liefern — das steht im Protokoll, nicht im Ergebnis. Protokoll und Ergebnisliste gehören gegeneinander gelesen. → Kalibrierung 4.

**8. Ein Wert aus drei Fällen ist kein Wert.**
Abdeckung wird immer mitgeführt. Die höchste Zahl einer Auswertung war zugleich die mit der kleinsten Basis und ein Artefakt von Lehre 7. → Kalibrierung 4.

**9. Auf beschädigtem Material sind exakte Kennungen der robusteste Fang — und man sucht nach der Ziffernfolge, nie nach dem Wort.**
Ziffern überleben, wo das Wort daneben in vier verschiedene Verstümmelungen zerfällt. Und **jedes Vorkommen auswerten, nicht das erste**: Dieselbe Kennung steht an einer Stelle falsch und an anderer richtig; Mehrfachvorkommen ist der eigentliche Rettungsmechanismus. → Kalibrierung 1, 5.4.

**10. Gegen erfundene Zahlen hilft Nachrechnen.**
Wo eine Ausgabe eine Rechenkette enthält, wird sie nachgerechnet — eine halluzinierte Zahl erfüllt ihre eigene Gleichung nicht. Das ist der einzige Test, den ein erfindendes Modell nicht bestehen kann, und er ist billig. Nötig ist er nachweislich: Auf schwerem Material erfindet ein generatives Modell plausiblen Text, und zwar bevorzugt in den Randbereichen eines sonst korrekten Dokuments — was ihn gefährlicher macht, nicht harmloser, weil in einem offensichtlich guten Ergebnis niemand sucht. → Kalibrierung 4, 5.1.

**11. Ein generatives Werkzeug legt eigene Kommentare in die Datenfelder.**
Erklärungen des Modells darüber, warum ein Bereich nichts Lesbares enthalte, stehen im selben Feld und mit demselben Typ wie echter Inhalt. Jede Übernahme von Rohblöcken in einen Zieltext braucht deshalb einen Fremdtext-Filter. → Kalibrierung 5.1.

**12. Ein leeres Ergebnis beweist keine Leere.**
Es kann auch heißen, dass eine Zwischenstufe an diesem einen Objekt scheitert — bei OCR etwa der Renderer, unabhängig von der Erkennungsqualität. Ein zweiter Renderer ist deshalb ein wirksamer Rettungspfad und kein Luxus. Und umgekehrt: Ein leeres Ergebnis ist ein **gültiges** Ergebnis, wenn der Gegenstand leer ist; es gehört als eigener Zustand geführt, nicht als Erfolg (sonst unsichtbarer Verlust) und nicht als Fehler (sonst endlose Wiedervorlage und Doppelkosten). → Kalibrierung 5.4, Betriebshandbuch 11.3.

**13. Formalisierte Dokumente liefern ihre Vollständigkeitsangabe oft selbst mit.**
Bevor man eine erwartete Stückzahl von außen konstruiert, lohnt die Frage, ob der Gegenstand sie mitbringt. Wo er es tut, ist sie die bessere Sollgröße, weil sie pro Objekt gilt statt im Mittel. → Kalibrierung 5.1, Betriebshandbuch 11.3.

**14. Was die Schnittstelle hergibt und was es bedeutet, sind zwei verschiedene Dinge.**
„Das Feld ist befüllt" ist eine belegte Tatsache; „also ist der Inhalt brauchbar" ist eine Deutung, die falsch sein kann. Wer Qualität prüft, baut eine Leiter von billig-schwachen zu teuer-starken Belegen und betritt jede Sprosse erst, wenn die darunter bestanden ist — bis hinauf zum Lesen des Inhalts gegen erwartete Anker. Ein Feld, das man nicht angefragt hat, ist außerdem **kein leeres Feld**. → Integrationsstrecke 2.4, 2.5.

**15. Defektklassen sind Kalibrier-Kompartimente, keine Suchthemen.**
Ein Sammel-Label wie „braucht Behandlung" ist zu breit zum Kalibrieren, weil verschiedene Defekte verschiedene Signale und verschiedene Heilmittel haben. Pro Klasse: vorher definieren, mit einer billigen Abfrage Kandidaten erzeugen, die Zugehörigkeit mit dem richtigen tieferliegenden Lesepfad **beweisen**, Beinahe-Treffer ausdrücklich ausschließen, und Schwellen an einem bekannten Schadens-Set kalibrieren statt sie zu raten. → Integrationsstrecke 2.9.

**16. Widersprüche werden benannt, nicht still aufgelöst — und ein „ungeklärt" altert.**
Wo zwei Quellen einander widersprechen, ist die plausiblere Fassung stillschweigend zu wählen eine unausgewiesene Entscheidung, von der der Leser nie erfährt. Und ein Vermerk „das ist offen" ist selbst eine Behauptung mit Datum: In diesem Projekt standen zwei Punkte als offen in einer Liste, die längst erledigt waren — einer davon neun Tage vor seiner Eintragung. Vor dem Abarbeiten nachsehen, ob der Eintrag noch stimmt. → Geschichte 4.3, 6.7; Betriebshandbuch 11.1.

**17. Ein Zitat ist kein Beleg, sondern ein Zeiger auf einen Beleg.**
Wer eine bestehende, durch eigene Messung gedeckte Regel umwerfen will, öffnet vorher die zitierte Quelle **selbst**. Sonst tauscht er Evidenz gegen Hörensagen — und weil eine fremde Quelle mit Autorität auftritt, merkt niemand den Tausch. Real passiert: Ein Blog-Artikel gab vor, die Hersteller-Doku zu zitieren; das behauptete Zitat stand dort nicht, die Doku sagte das Gegenteil. **Dasselbe gilt für automatische Prüfungen und Review-Meldungen — sie sind Zeiger, keine Befunde.** → Geschichte 4.3 Punkt 4.

**18. Ein veraltetes Runbook ist keine tote Datei, sondern eine Landmine.**
Der teuerste Zwischenfall dieses Projekts entstand, weil eine Anleitung befolgt wurde, deren Architektur seit Wochen aufgegeben war. Daraus folgen zwei stehende Regeln: **Gibt es Dokumentation, wird sie erforscht und gelesen — als Erstes**, und zwar auch die Seite, die den eigenen Plan kippen könnte; und **Dokumentation ist nicht der Fix, sie vor dem Handeln zu konsultieren ist der Fix.** Was daraus für dieses Dokumentenset folgt, steht in Abschnitt 4. → Geschichte 2.6, 2.5.

**19. Bezahlte Ressourcen und Parallelität brauchen ausdrückliche Zustimmung — mit Anzahl, Stufe und Grund.**
Der Zähler läuft die gesamte Existenzzeit einer Ressource, auch durch jedes Debuggen. Und eine Regel, die aus einer einzelnen Beobachtung eine harte Grenze macht, ist gefährlicher als keine Regel: In diesem Projekt hätte eine so verallgemeinerte Grenze beinahe den einzigen Weg verhindert, der funktionierte. → Betriebshandbuch 9.1, 9.9, 15.

**20. Was überleben soll, muss an einen dauerhaften Ort.**
Arbeitsverzeichnisse einzelner Sitzungen können jederzeit weggeräumt werden. Die beiden Skripte, die die bisherigen Produktionsläufe gefahren haben, existierten wochenlang ausschließlich dort. → Geschichte 2.7, Glossar.

---

## 4. Wie dieses Dokumentenset gepflegt wird

Aus Lehre 17 folgt eine unangenehme Konsequenz: **Diese fünf Dateien sind ab sofort das größte Runbook des Projekts und damit sein größtes Landminen-Risiko.** Sie enthalten Dutzende datierte Momentaufnahmen, als „ungeprüft" markierte Aussagen und Angaben über Zustände, die anderswo konfiguriert sind. Deshalb gelten für sie selbst Regeln:

1. **Jede Aussage über einen anderswo konfigurierten Zustand trägt ihren Nachprüf-Ort.** Endpoint-Kennungen, Preise, Worker-Zahlen, Kontostände, Werkzeug-Fähigkeiten sind Momentaufnahmen; die Tabellen „Nachprüf-Orte" (Betriebshandbuch 17, Integrationsstrecke 9) sind der Index dafür. Wer eine solche Angabe benutzt, prüft sie; wer sie ändert, ändert sie dort und nicht nur an der Fundstelle.
2. **Widerlegtes wird als widerlegt gekennzeichnet, nicht überschrieben.** Das Projekt hat mehrere Zurücknahmen, und ihre Anatomie ist wertvoller als die zurückgenommene Aussage (Geschichte 4). Wer eine Aussage kippt, schreibt hin, was vorher galt, warum es fiel und wodurch — sonst wiederholt jemand die Herleitung.
3. **Wer eine dieser Dateien anfasst, denkt das Ganze neu und schreibt nur den Unterschied.** Die meisten Stellen sind über mehrere Runden entstanden und geprüft; sie bleiben stehen, wenn die Prüfung sie bestätigt. Neu geschrieben wird, was falsch war oder wo die neue Fassung nachweislich besser ist — nicht, wo sie nur anders wäre.
4. **Konkrete Einzelwerte gehören nicht hierher.** Aktenzeichen, Steuernummern, Beträge, UUIDs einzelner Dokumente leben in den Artefakten und im Aufgaben-Tracker. Diese Dokumente halten Klassen, Verfahren und Entscheidungen. Ein Dokumenten-Dossier wäre nicht nur überflüssig, es würde beim ersten Bestandswechsel falsch.
5. **Bei jeder größeren Änderung an der Strecke wird geprüft, ob eine Aussage hier dadurch tot ist** — insbesondere nach einem Endpoint-, Modell-, Werkzeug- oder Schnittstellenwechsel. Genau dieser Schritt wurde beim Architekturwechsel im Juli übersprungen, mit bekanntem Ausgang.

---

## 5. Wo die Artefakte liegen — und was hier noch fehlt

**In diesem Repository:** die fünf Dokumente unter `docs/`; `DEVONthink.sdef` (die maßgebliche Quelle dafür, was DEVONthinks Skript-Oberfläche hergibt); `scripts/ocr_run.py` und `scripts/ocr_pdfs.py` (die beiden bisherigen Driver als Dauerkopien — Vorlagen, nicht der künftige Driver); `scripts/ocr_test.py` (der Mini-Driver des Doku-Verifikationstests vom 05.08.2026 — nach den Anforderungen des Betriebshandbuchs gebaut, für Einzelseiten-Tests, ebenfalls nicht der künftige Driver) samt seinen Belegen unter `artifacts/2026-08-05-doku-verifikationstest/` (Chronik: Geschichte, Abschnitt 2.9).

**Außerhalb dieses Repositories, aber tragend** (vollständig verzeichnet in Geschichte 7): das Fixture-Set aus 15 Schadens-Dokumenten samt seinem Befund-Katalog, die Ausgaben des Engine-Vergleichs und der Kalibrierung, die fünf Skills mit dem operativen Runbook und den allgemeinen RunPod-Anleitungen, die Wissensspeicher-Seiten des Projekts, die Aufgaben-Einträge im Tracker. **Diese Verteilung ist ein bekannter, ungelöster Zustand**: Solange die Beweisschicht und das operative Original in einem anderen Projektverzeichnis liegen und drei der fünf Skills nur Verweise auf ein drittes Verzeichnis sind, steht dieses Repository nicht für sich. Was davon hierher kopiert werden soll, ist eine offene Entscheidung.

**Was in dieser Dokumentation noch fehlt — benannt, damit es nicht als erledigt durchgeht:**

- ~~**Es gibt keinen Driver.** Für dieses Vorhaben existiert noch keine Zeile Code.~~ **ÜBERHOLT SEIT DEM 07.08.2026.** Der Driver ist gebaut: das Paket `mineru_ocr` mit zwei Zugängen (`import mineru_ocr` und das Kommando `mineru-ocr` mit neun Unterkommandos), der Exekutor als eigener Prozess. Stand 10.08.2026: 341 Tests grün, mypy strict sauber, elf bezahlte Läufe gegen den echten Endpoint (zusammen 0,4179 USD), sechs echte Antworten des Produktionspfads als Testmaterial unter `artifacts/2026-08-08-erster-tarball-lauf/`. Der Einstieg steht im [README des Repositories](../README.md), der Aufbau samt Begründungen in `plans/2026-08-05-ocr-service-plan.md`, die Arbeitsregeln in `CLAUDE.md`.

  **Der Rest dieses Punktes gilt unverändert weiter, und deshalb steht er hier statt gelöscht zu werden:** Die beiden Skripte unter `scripts/` sind Ad-hoc-Werkzeuge aus einer einzigen Sitzung, geschrieben für zwei andere Bestände, mit fest verdrahteten Pfaden — sie haben nie einen Datenbank-Record gesehen und tragen mehrere benannte Fehler (Betriebshandbuch 11.2a). Ihr Wert ist, dass sie den Job-Contract erwiesenermaßen richtig bedienen und dass ihre Schwächen zu einer Anforderungsliste geworden sind (Betriebshandbuch 11.3). **`mineru_ocr` ist an ihnen gemessen und nicht aus ihnen gepatcht worden** — genau so, wie es hier gefordert war.

  **Was der Driver NICHT erledigt hat:** Der große Lauf über den Gesamtbestand hat weiterhin nicht stattgefunden, und die Stufen davor und danach — Erkennung, Export, Write-back — sind unverändert offen (Integrationsstrecke). Gebaut ist Stufe 3, nicht die Strecke.
- **Die Kommando-Referenz deckt die Engine-Seite ab, nicht die Datenbank-Seite.** Für Erkennung, Export, Write-back und Statusführung stehen die Verfahren und die Werkzeugnamen fest, die konkreten Aufrufe nicht — was daran liegt, dass dort die Entscheidungen selbst noch offen sind (Integrationsstrecke).
- **Die lokale Alternative ist benannt, aber nicht bewertet.** Die OCR des Betriebssystems ist lokal, kostenlos und ohne Datenschutz-Frage; sie wurde in diesem Projekt genau einmal gemessen, für eine ganz andere Aufgabe, und lief nie gegen das Fixture-Set (Kalibrierung 5.2). Sie ist damit ein unbewerteter Kandidat, kein ausgeschiedener — die Frage ist offen, nicht beantwortet.
- **Der Berührungspunkt zum Trägerprojekt.** Die Suchrahmen der Klassifikation und der geerntete Kennungsbestand leben ausschließlich im Aufgaben-Tracker. Die OCR-Strecke verliert und rettet genau diese Kennungen — die beiden Stränge berühren sich also hart, und wo dieser Bestand außerhalb des Trackers gesichert ist, ist ungeklärt.
