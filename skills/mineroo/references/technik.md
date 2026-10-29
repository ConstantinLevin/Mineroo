# Technik und Betrieb

## Einrichtung

Mineroo braucht Postgres mit dem Schema des Pakets. `mineru-ocr setup` legt
Datenbank und Schema an; wiederholte Aufrufe verändern keine bestehenden Daten.
Bei einem veralteten Schema nennt die Fehlermeldung die Abhilfe; `setup` führt
keine Migrationen aus. Clients und Daemon prüfen das Schema beim Öffnen lesend.

Die Datenbankverbindung kommt aus `MINERU_OCR_DB_DSN` oder
`~/.mineru_ocr/config.toml`; `MINERU_OCR_CONFIG` wählt eine andere Datei.
Explizite Angaben haben Vorrang vor Umgebung und Datei. `setup` und die
Daemon-Unterkommandos bieten dafür `--dsn`; Clients nehmen `db_dsn=`.
Die Kommandohilfe nennt die Optionen, `help(mineru_ocr.Settings.load)` die
Konfigurationsquellen. Der Endpoint kommt aus `MINERU_OCR_ENDPOINT_ID` oder
`endpoint_id` in der Datei; der RunPod-Schlüssel aus `~/.runpod/config.toml`,
Feld `apikey`. Zugangsdaten bleiben in der lokalen Konfiguration.

## Daemon

Nur der Daemon schickt Dokumente an RunPod. Dateiaufruf und Bibliothek starten
ihn nicht. Ein laufender Daemon kann eingereihte Dokumente kostenpflichtig
abarbeiten; starte ihn im Rahmen des Betriebsauftrags.

```bash
mineru-ocr daemon list
mineru-ocr daemon start X
mineru-ocr daemon stop X
```

`X` ist eine ganze Zahl ab 1. `start X` startet X Hintergrundprozesse mit je
eigenem Protokoll; `--concurrency N` bestimmt die OCR-Plätze je Prozess.
`stop X` beendet bis zu X ordentlich, die am wenigsten belegten zuerst.
`list` liefert JSON zu lokalen Daemons, belegten/verfügbaren OCR-Plätzen,
Protokollpfaden und zur datenbankweiten OCR-Warteschlange. Die Kommandos
verwalten lokale Daemons der gewählten Datenbank. Nach einem Rechnerneustart
startet nichts von selbst.

Ein frischer Worker-Herzschlag ist jünger als 30 s, gemessen an der Datenbankuhr.
Die CLI reiht ohne ihn nichts ein (Exit 5), liefert gespeicherte Ergebnisse
aber weiter. Ist die Lebendigkeit nicht lesbar, endet sie mit Exit 1.
Die Bibliothek (`ocr_start`, `retry`) reiht in beiden Fällen trotzdem ein und
warnt; ohne Daemon bleiben die Dokumente offen.

## Preflight und Datenschutz

Die verbindliche EWR-Prüfung vor dem ersten Abschicken samt Auswertung steht
in [Ein Dokument](ein-dokument.md#vor-dem-abschicken); sie gilt auch für Stapel
und Python. Sie ist eine Betriebsregel: CLI und Async-Client führen keinen
Preflight aus. Beim synchronen Client läuft er standardmäßig nur, wenn
RunPod-Schlüssel und Endpoint konfiguriert sind.

`client.preflight()` verändert nichts und prüft Endpoint-Sollwerte, Images,
Worker-Umgebung, Rechenzentren, Kontostand und Daemon-Lebendigkeit. Er braucht
einen RunPod-Schlüssel und Endpoint. Mit `strict=True` führen echte
Abweichungen zu `ConfigDriftError`; mit `strict=False` bleiben sie im Befund
als `abweichung` sichtbar. Die Auswertung entscheidet über das Abschicken.
Eine leere Liste `dataCenterIds` bedeutet freie Regionswahl und ist eine
Abweichung; eine fehlende/unlesbare Liste ist `nicht_pruefbar`.

Geheimnisse der Worker-Umgebung werden als `***` maskiert (Namen mit KEY,
SECRET, TOKEN oder PASSWORD auch als Teilwort, unabhängig von Großschreibung).
Ihr Wert ist nicht prüfbar; fehlende oder leere erwartete Geheimnisse sind eine
Abweichung. `client.workers()` liefert dieselbe maskierte Umgebung.
`client.status()` fragt RunPod nach dem Kontostand und braucht dessen Schlüssel;
die Übersicht zählt Dokumente und Jobs und meldet die Daemon-Lebendigkeit.

## Leseeinstellungen

| CLI | Python an `ocr_start` und `ocr` | Werte / Vorgabe |
| --- | --- | --- |
| `--tier` | `tier=` | `flash`, `basic`, `standard`, `advanced`; Vorgabe `advanced` |
| `--ocr-mode` | `ocr_mode=` | `auto`, `txt`, `ocr`; Vorgabe `ocr` |
| `--image-analysis` / `--no-image-analysis` | `image_analysis=` | Vorgabe `True`; wirkt nur bei `advanced` |

Lass die Vorgaben stehen, außer der Nutzer verlangt etwas anderes. `ocr` erzwingt
OCR, `txt` nimmt die PDF-Textebene, `auto` lässt MinerU entscheiden.
Bei Ganzdokument-Formaten (Office, OpenDocument, RTF, EPUB, OFD, HTML, MHTML)
wirken diese Einstellungen nicht: Der Worker liest sie in `flash`; am Ergebnis
stehen keine bestellten Leseeinstellungen.

Die Einstellungen gelten nur für neu eingereihte Dokumente. Identische Bytes
liefern das gespeicherte Ergebnis oder schließen sich dem laufenden Job an,
unabhängig von neu verlangten Einstellungen; ein Neu-Lesen mit anderen gibt es
nicht. `.read_settings` nennt die bestellten Einstellungen, `.mineru_version`
die Version; die tatsächlichen Worker-Angaben stehen je Teilstück in `.raw`.

Dateitypen erkennt Mineroo am Inhalt, nicht am Namen (`mineru_ocr.DocumentType`).
PDF, Bilder und die genannten Ganzdokument-Formate werden unterstützt; CSV/TSV
werden abgewiesen. Mehrseitige Bilddateien enden `input_error` mit dem Hinweis,
sie als PDF einzureichen.

## Fehlerdiagnose

`client.document(hash)` unterscheidet wartend, laufend und ohne Ergebnis beendet.
Bei `failed` oder einem ohne Ergebnis beendeten Job kommt `client.retry(hash)`
in Betracht. Dafür gilt die [Zustimmungsregel](ein-dokument.md#auftragsgrenze-und-daten).
Ein erneuter Dateiaufruf wiederholt einen gespeicherten Fehlschlag nicht.
`retry` lässt `done`, `empty` und `input_error` unverändert. Für mehrere benannte
Hash-Ziele gibt es `retry_all(hashes)`; ohne Argument erfasst es alle stehenden
Eingaben, weshalb es nur zu einem entsprechend umfassenden Auftrag passt.

Vor einer Zustimmung den Grund erklären: `UnstorableResultError` heißt, dass
Speichern scheiterte; ein neuer bezahlter Lauf scheitert voraussichtlich wieder.
Bei `MissingPagesError` oder `MissingArtifactError` ist die Antwort unvollständig;
die bezahlte Antwort steht in `client.get(hash).raw`. Ein `retry` liest und
bezahlt erneut; sichere benötigte Rohdaten vorher, da es die Fehlerzeile ersetzt.

Bei `ResponseTooLargeError` teilt der Daemon das betroffene PDF-Teilstück weiter
und liest diese Teile erneut, erneut bezahlt. Ist schon die Antwort einer
einzelnen Seite zu groß, bleibt das Dokument `failed`; kleinere Teilstücke helfen
dann nicht. Eine PDF-Einzelseite über der Uploadgrenze wird als Bild mit 200 dpi
und höchstens 3.500 Pixel langer Kante versucht. `notes` nennt die Originalseiten
null-basiert unter `seiten_gerendert`. Passt auch das Bild nicht, endet es `input_error`.

## Kosten einschätzen

RunPod berechnet Worker-Rechenzeit, Kaltstart und Leerlauf nach dem letzten Job.
Die Messung aus Mineroo#19 (MinerU 4.0.12, `advanced`, OCR und Bildanalyse,
RTX 4090) ergab rund **0,0006 USD je Seite** reine Ausführung. Der Lauf mit
20 Dokumenten und 102 Seiten kostete samt Kaltstarts und Leerlauf rund
**0,003 USD je Seite**. Das ist eine historische Größenordnung, keine Preiszusage;
bei kleinen Läufen fallen Kaltstart und Leerlauf je Seite stärker ins Gewicht.

Die Vorgabe `idle_timeout = 5` Sekunden folgt der Festlegung des Eigentümers.
Gemessen wurden 1,3 s Wiederanlauf per FlashBoot nach 4,5 min Leerlauf und rund
2 min Kaltstart nach 15 min Pause. Einzelheiten stehen in
[Messwerte, Abschnitt 8.2](../../../docs/MinerU-OCR-Kalibrierung-und-Backend-Vergleich.md#82-messwerte).
Treffer, Warten und Abholen erzeugen keine neue OCR-Rechenzeit; ein manuelles
Wiederholen bezahlt die Verarbeitung erneut. Auftragsgrenze und Umgang mit
personenbezogenen Ergebnissen: [Ein Dokument](ein-dokument.md#auftragsgrenze-und-daten).
