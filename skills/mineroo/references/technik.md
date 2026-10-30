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

Nur der Daemon schickt Dokumente an RunPod und Audio an ElevenLabs. Dateiaufruf und Bibliothek starten
ihn nicht. Ein laufender Daemon kann eingereihte Dokumente kostenpflichtig
abarbeiten; starte ihn im Rahmen des Betriebsauftrags.

```bash
mineru-ocr daemon list
mineru-ocr daemon start X
mineru-ocr daemon stop X
```

`X` ist eine ganze Zahl ab 1. `start X` startet X Hintergrundprozesse mit je
eigenem Protokoll; `--concurrency N` bestimmt die OCR-Plätze je Prozess.
`stop X` beendet bis zu X ordentlich nach der Summe laufender OCR- und Audio-Jobs,
die am wenigsten belegten zuerst, bei Gleichstand nach aufsteigender PID.
Wartungs-Jobs und wartende Jobs zählen nicht mit.
Vor dem ersten Kind prüft `start` den gesetzten `ELEVENLABS_API_KEY` mit
kostenfreiem `GET /v1/user` (Leseberechtigung nötig). Bei fehlendem/ungültigem
Schlüssel, verweigertem Zugriff oder Unerreichbarkeit startet kein Kind;
ein Timeout wird einmal wiederholt. Kontodaten werden nicht ausgegeben.
`list` liefert JSON zu lokalen Daemons und Protokollpfaden. Unter
`lanes.ocr`/`lanes.audio` stehen je `slots.occupied`/`slots.total` (lokale
Belegung/Kapazität) und `queue.waiting`/`queue.running` (datenbankweit).
`daemons[].lanes` zeigt `occupied`/`total` je Lane und Prozess. Die bisherigen
Felder `slots`/`queue` und `daemons[].occupied`/`available` bleiben OCR.
Ohne Daemons sind Kapazitäten 0, offene Jobs bleiben sichtbar. Bei älteren
Daemons ohne Lane-Auskunft ist Audio-Kapazität JSON-`null`. Die Kommandos
verwalten lokale Daemons der gewählten Datenbank. Nach einem Rechnerneustart
startet nichts von selbst.

Ein frischer Worker-Herzschlag ist jünger als 30 s, gemessen an der Datenbankuhr.
Die CLI reiht ohne ihn nichts ein (Exit 5), liefert gespeicherte Ergebnisse
aber weiter. Ist die Lebendigkeit nicht lesbar, endet sie mit Exit 1.
Die Bibliothek (`ocr_start`, `retry`) reiht in beiden Fällen trotzdem ein und
warnt; ohne Daemon bleiben die Dokumente offen.

## Audio

Audio (MP3, M4A/AAC, WAV, FLAC, OGG/Opus, AIFF) wird am Inhalt erkannt und
ungeteilt an ElevenLabs Scribe v2 gesendet. M4A mit allgemeinen MP4-Marken
(`mp42`, `isom`) ist bei reinen Audiospuren erlaubt; eine Videospur bleibt
unabhängig von Marke und Endung ausgeschlossen. Der Daemon hat dafür einen dritten
Worker in der Queue `audio`, mit `audio_concurrency` Plätzen (Vorgabe 2).
OCR-Leseeinstellungen und RunPods Uploadgrenze gelten für Audio nicht;
`db.MAX_INPUT_BYTES` (1 GB) gilt weiterhin.

Der Schlüssel kommt nur aus `ELEVENLABS_API_KEY` in der Startumgebung des
Daemons, wird maskiert und über ein anonymes Rohr an Kinder gereicht.
Er gehört weder in `config.toml` noch ins Repository oder Ausgaben. `.env` wird
ignoriert und nicht automatisch geladen. Clients brauchen den Schlüssel nicht.
`audio_region = "us"` in der Konfiguration bezeichnet die USA-Verarbeitung,
die der Eigentümer in #69 ausdrücklich akzeptiert hat: „ja das passt mit er
verarbeitung.“ RunPods EWR-Regel gilt weiter für OCR. EU-Verarbeitung und
Abschalten der Speicherung sind bei ElevenLabs Enterprise-Funktionen.

Die Messung in #69 (30.10.2026) ergab rund 0,40 USD je Audiostunde, über das
Tarifkontingent; Free hatte 10.000 Credits pro Monat (etwa 2,5 Stunden), darüber
Pay as you go. Treffer kosten nichts. Ein Verbindungsabbruch kann ein bezahltes
Transkript verlieren, weil seine ID erst mit der Antwort ankommt; ein neuer Lauf
kostet erneut. Es gelten dieselbe Auftragsgrenze und derselbe Schutz von
Ergebnissen wie bei OCR. Tests verwenden nur erfundene oder synthetische Daten.

Die vollständige Antwort einschließlich Wortzeiten liegt in `raw.response`,
die gesendeten Parameter in `raw.parameters`. Sprecherwechsel und Geräusche
liefert `rendered_markdown`; `readable_markdown` ist bei Audio identisch.
Ohne erkanntes Wort lautet der Zustand `empty`. `retry` und `retry_all` wählen
bei Audio wieder `audio`. Fehlender/ungültiger Schlüssel ergibt `failed`
(Konfigurationsfehler), HTTP 422 `input_error` mit dem maskierten Antwortgrund.
429/5xx, Verbindungsabbruch und Zeitüberschreitung werden über die zentrale
Einordnung bis zum Versuchsbudget wiederholt, danach steht `failed`.
`quota_exceeded` (auch HTTP 400/401) bzw. HTTP 402 pausiert im laufenden Versuch:
erneute Anfrage nach `balance_recheck_seconds` (intern 900 s), ohne Retry-Verbrauch.
Beim Herunterfahren wird bis zur Budgetgrenze neu eingereiht, im letzten
Versuch `failed` mit Abbruchvermerk geschrieben.

Die Anfragefrist wächst mit der Dauer: 300 s plus Dauer/10; eine Stunde erhält
660 s (gemessen in #69: 59 Minuten in 43 Sekunden). `ffprobe` liest die Dauer
lokal mit maximal zehn Sekunden; fehlt es oder ist die Dauer nicht lesbar,
gilt eine Schätzung mit 8 kbit/s. Die Datei-Abweisung übernimmt weiterhin ElevenLabs.

`daemon list` zeigt beide Lanes; die kostenfreie Startprüfung
und Audio-Fortschritt mit `-d` sind umgesetzt (#72).

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
PDF, Bilder, Audio und die genannten Ganzdokument-Formate werden unterstützt; CSV/TSV
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
