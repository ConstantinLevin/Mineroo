---
name: mineroo
description: OCR auf Abruf mit Mineroo (MinerU auf RunPod; CLI `mineru-ocr`, Bibliothek `mineru_ocr`). Nutzen, wenn Text, Struktur oder die vollständige MinerU-Antwort aus PDFs, Scans, Bildern oder Office-Dateien gebraucht wird, und wenn auf früher abgeschickte Dokumente gewartet oder ihr Ergebnis geholt werden soll.
---

# Mineroo: Dokumente lesen lassen

Mineroo schickt Dokumente an MinerU auf einem RunPod-Endpoint und legt die
vollständige Antwort in Postgres ab. Die Datenbank ist der Cache: Dieselben Bytes
noch einmal abzuschicken ist ein **Treffer**, kostet nichts und liefert das
gespeicherte Ergebnis, aus jedem Prozess und jedem Agenten, auch Tage später.
Mehrere Agenten dürfen gleichzeitig abschicken und warten, auch während ein
großer Lauf läuft; kein Dokument läuft dabei doppelt.

Optionen, Exit-Codes und Ausgabeformen stehen in `mineru-ocr --help` und
`mineru-ocr <kommando> --help`, die Bibliothek in ihren Docstrings
(`help(mineru_ocr.OCRClient)`, `help(mineru_ocr.AsyncOCRClient)`). Welche
Dateitypen gelesen werden, steht in `mineru_ocr.DocumentType`: PDFs, Bilder und
Dateien, die MinerU als ganzes Dokument liest (Office auch im alten Format,
OpenDocument, RTF, EPUB, OFD, HTML, MHTML). Erkannt wird am Inhalt, nicht am
Namen; alles andere, auch CSV, weist das Abschicken ab.

## Vor dem ersten Abschicken: Rechenzentren prüfen

Der Endpoint soll nur in Rechenzentren des EWR rechnen (EU-Staaten, Island und
Norwegen; dort gilt die DSGVO). Mineroo weiß nicht, ob ein Dokument
personenbezogen ist, und behandelt deshalb jedes so. Ob der Endpoint die
zulässige Liste einhält, sagt allein der Preflight. Weder der CLI-Dateiaufruf
noch der `AsyncOCRClient` lassen ihn laufen. Deshalb, einmal je Sitzung vor dem
ersten Abschicken, auf jedem Weg:

Über die Bibliothek (das frühere CLI-Kommando `preflight` entfällt):

```python
from mineru_ocr import OCRClient

with OCRClient(preflight=False, strict=False, schema_check=False) as client:
    befund = client.preflight()
```

In `befund["checks"]` zählt der Eintrag mit `"punkt": "datacenters"`:

- `in_ordnung`: Du darfst abschicken, auch personenbezogene Dokumente.
- `abweichung`: Du schickst nichts ab und zeigst dem Nutzer `soll` und `ist`.
- Jedes andere Ergebnis: `nicht_pruefbar` (der Endpoint nennt seine
  Rechenzentren nicht, oder ihre Abfrage scheiterte), kein `datacenters`-Eintrag (der Endpoint war nicht
  erreichbar) oder ein Preflight, der mit einem Fehler endet (etwa ohne
  RunPod-Schlüssel oder Endpoint). Dann sagst du dem Nutzer, dass sich die
  Rechenzentren nicht bestätigen lassen, samt der Meldung, und schickst erst ab,
  wenn er zustimmt.

Der Preflight verändert nichts, fragt aber RunPod und braucht deshalb den
RunPod-Schlüssel; dasselbe gilt für `client.status()`.

Geheimnisse der Worker-Umgebung zeigt das Paket nur als `***` (Namen mit
KEY, SECRET, TOKEN oder PASSWORD, unabhängig von Großschreibung). Ihr Wert
ist im Preflight `nicht_pruefbar`; fehlende oder leere erwartete Geheimnisse
sind eine Abweichung. `workers()` gibt dieselbe maskierte Umgebung zurück.

## Ablauf über die CLI

Für Dateien und Ordner auf der Platte genügt `mineru-ocr PFAD…`: abschicken,
synchron warten und den vollständigen Markdown-Text auf stdout ausgeben. Ist das
Ergebnis schon gespeichert, kommt es sofort aus Postgres, auch ohne Daemon.
`-t` schreibt den Text in eine temporäre Datei und gibt deren Pfad aus;
`-o PFAD` schreibt unter diesem Pfad. Bei mehreren Dokumenten steht vor jedem
Text seine Quelldatei; `-t` und `-o` schreiben den ganzen Stapel in eine Datei.
Ordner werden rekursiv gelesen, fremde und zu große Dateien mit Grund
übersprungen. Meldungen gehen auf stderr.

Setz beim Dateiaufruf `--timeout S`, um das Warten je Dokument zu begrenzen.
Exit-Code 4 lässt den Job bestehen; später rufst du denselben Pfad erneut auf.
Exit-Code 5 sagt: kein laufender Daemon, für diese Datei wurde nichts eingereiht.
`empty` ist gültig (Exit 0), mit Grund ohne Text; `failed` und `input_error`
werden je Datei mit Grund und Exit 3 gemeldet. Ein gespeicherter Fehlschlag wird
nicht automatisch neu bezahlt. Betrieb-/Dateifehler sind Exit 1, Aufruffehler 2.
Bei gemischten Fehlern gilt 1 vor 5 vor 4 vor 3 vor 0; erfolgreiche Texte stehen
trotzdem in der Ausgabe.

`setup` und `daemon` bleiben Betriebskommandos. Die bisherigen Kommandos `ocr`,
`status`, `show`, `wait`, `markdown`, `retry` und `preflight` sind entfernt.
Struktur, Bilder, Rohantworten, Zustände und Wiederholungen nutzt du über Python.

## Ablauf über die Bibliothek

`OCRClient` ist synchron, `AsyncOCRClient` asynchron; du öffnest sie mit `with`
bzw. `async with`. Beide können abschicken (`ocr_start`; `ocr` schickt ab und
wartet in einem Zug), warten (`wait`, `wait_all`, `as_completed`) und holen
(`get`); `status`, `preflight`, `document` und `retry` hat nur der synchrone.
Beim Async-Client rufst du jede
dieser Methoden mit `await`, außer `as_completed`: Das ist ein async-Generator,
`async for w in client.as_completed(hs, timeout=…)`. Importierbar ist das Paket
im Interpreter der Miniconda-Base
(`/opt/homebrew/Caskroom/miniconda/base/bin/python3`), in dem es installiert ist.
Wirft `with OCRClient()` beim Öffnen `ConfigDriftError`, weicht der Endpoint von
seinen Sollwerten ab; die Meldung gibst du dem Nutzer weiter.

- Mit dem Hash, den `ocr_start` je Dokument zurückgibt, wartest und holst du.
- Auf viele wartest du mit `wait_all` oder `as_completed`. Jedem Aufruf, der
  wartet (`wait`, `wait_all`, `as_completed`, `ocr`), gibst du `timeout=` mit:
  Ohne Daemon kehrt er sonst nie zurück.
- Zurück kommt eine **Hülle**: `rendered_markdown` ist das ganze Dokument als
  Markdown, `raw` die vollständige Antwort. Der Text trägt auch Kopf- und
  Fußzeilen, Seitenzahlen, Randtexte und Seitenfußnoten an ihrer Stelle, und
  hinter jedem Bild die Deutung des Modells als eingeklappten Block
  (`<details>`); eine Deutung kann falsch sein.

Jeder Endzustand kommt als Hülle zurück, auch `failed` und `input_error`. Prüf
`.success`, bevor du `rendered_markdown` liest; `empty` heißt, das Dokument war
leer, und ist gültig. Eine abgelaufene Frist fasst keinen Job an: Das Dokument
bleibt unterwegs, und du wartest später erneut darauf.

Die Hüllen des Async-Clients kommen mit geladenem `raw`. Bei vielen großen
Dokumenten nimmst du `as_completed` und verarbeitest jede Hülle, bevor die
nächste kommt.

## Lese-Einstellungen

Stufe, OCR-Modus und Bildanalyse wählst du je Lauf: beim CLI-Dateiaufruf mit
`--tier`, `--ocr-mode` und `--no-image-analysis`, in der Bibliothek mit `tier=`,
`ocr_mode=` und `image_analysis=` an `ocr_start` und `ocr`. Ohne Angabe liest
MinerU in `advanced` mit erzwungenem OCR und Bildanalyse; davon weichst du nur ab,
wenn der Nutzer es verlangt (etwa eine saubere digitale PDF im Textmodus).

Bei Dateien, die MinerU als ganzes Dokument liest (Office, OpenDocument, RTF,
EPUB, OFD, HTML, MHTML), wirken sie nicht: Der Worker liest sie immer in `flash`,
und am Ergebnis stehen keine Einstellungen.

Die Einstellungen wirken nur auf Dokumente, die der Aufruf neu einreiht. Ein
Treffer liefert das gespeicherte Ergebnis, gleich welche Einstellungen du
verlangst, und ein Neu-Lesen mit anderen gibt es nicht. Womit ein Ergebnis
gelesen wurde, zeigen an der Hülle `.read_settings` und `.mineru_version`; wie
viele Dokumente mit welchen gelesen sind, zeigt `client.status()`.

## Der Daemon

Fertig wird ein neues Dokument nur, wenn ein Daemon läuft: Er ist
der einzige Prozess, der Dokumente an RunPod schickt. Die CLI verweigert ohne
frischen Herzschlag das Einreihen und endet mit Exit 5; Treffer gibt sie weiter
aus. Mit `mineru-ocr daemon start X` startet der Betreiber X Hintergrundprozesse,
mit `mineru-ocr daemon stop X` beendet er bis zu X ordentlich (die am wenigsten
belegten zuerst), mit `mineru-ocr daemon list` sieht er eingerücktes JSON zu
Anzahl, belegten/verfügbaren OCR-Plätzen, Warteschlange und Protokollpfaden.
Der Vordergrund-Aufruf entfällt. Der Dateiaufruf startet nie selbst einen.

Die Bibliothek (`ocr_start`, `retry`) kann weiterhin ohne Daemon einreihen und
warnt im Logger `mineru_ocr.client`; die Dokumente bleiben offen, bis einer
startet. Gib die Warnung dem Nutzer weiter, bevor du wartest. `client.status()`
und der Preflight melden die Lebendigkeit. Ein frischer Herzschlag ist jünger
als 30 s, gemessen an der Datenbankuhr. Eine nicht lesbare Lebendigkeit endet in
der CLI mit Exit 1 ohne Einreihen, in der Bibliothek nur mit Warnung.

Hängt ein Dokument, sagt `client.document(hash)`, ob sein Job wartet, läuft oder
ohne Ergebnis endete. Im letzten Fall und bei `failed` hilft
`client.retry(hash)`, nachdem der Nutzer zugestimmt hat (siehe Kosten).
`UnstorableResultError` bedeutet, das Ergebnis ließ sich nicht speichern; ein
`retry` bezahlt das Dokument neu und scheitert voraussichtlich wieder. Ebenso
bei `MissingPagesError` (nicht alle Seiten verarbeitet) und
`MissingArtifactError` (Pflichtdatei fehlt): Die bezahlte Antwort steht in
`client.get(hash).raw`, und ein `retry` bezahlt das Dokument neu. Gib den Grund
dem Nutzer weiter, bevor er zustimmt. Bei `ResponseTooLargeError` teilt der Daemon das betroffene PDF-Teilstück
automatisch weiter und liest es in Teilen erneut (erneut bezahlt). Erst wenn
die Antwort einer einzelnen Seite zu groß ist, bleibt das Dokument `failed`;
ein kleineres `max_pages_per_chunk` hilft dann nicht.
Eine PDF-Einzelseite über der Uploadgrenze wird als Bild mit 200 dpi und längster
Kante höchstens 3.500 Pixel versucht. `notes` nennt diese Originalseiten
null-basiert unter `seiten_gerendert`. Passt auch das Bild nicht, endet das
Dokument `input_error`.

## Was Lesen kostet

Bezahlt wird die Rechenzeit der Worker bei RunPod: für jedes Dokument, das neu
gelesen wird, dazu der Kaltstart eines Workers und seine Leerlaufzeit nach dem
letzten Job. Gemessen ist das einmal (Mineroo#19; MinerU 4.0.12, Stufe
`advanced` mit OCR und Bildanalyse, eine RTX 4090): Die Rechenzeit kostet rund
0,0006 USD je Seite. Ein Lauf von 20 Dokumenten mit 102 Seiten kostete mit
Kaltstarts und Leerlauf rund 0,003 USD je Seite; je kleiner der Lauf, desto mehr
fallen Kaltstart und Leerlauf je Seite ins Gewicht. Fragt der Nutzer nach dem
Preis, nennst du ihm diese Messung als Größenordnung, nicht als Zusage
(Einzelheiten in `docs/MinerU-OCR-Kalibrierung-und-Backend-Vergleich.md`,
Abschnitt 8.2). Keine Rechenzeit kosten ein
Treffer, das Warten und das Abholen (`wait`, `get`, ein CLI-Treffer). `retry`
lässt ein Dokument ohne gültiges Ergebnis neu lesen und bezahlt es neu.

Der Eigentümer hat die Leerlaufzeit auf **5 s** festgelegt; am Endpoint
`csfbvej3d4cna5` ist `idleTimeout` so gesetzt, und der Preflight erwartet diesen
Wert als Voreinstellung (`idle_timeout`). FlashBoot holte einen
herunterskalierten Worker nach 4,5 min Leerlauf in 1,3 s zurück; längeres
Warmhalten kostet nur Geld. Nach 15 min Pause gab es ohnehin einen Kaltstart
von rund 2 min (Mineroo#19).

Schick ab, was der Nutzer genannt hat. Willst du darüber hinaus weitere Dokumente
lesen lassen oder ein `retry` auslösen, fragst du ihn vorher.

## Ergebnisse personenbezogener Dokumente

Ergebnisse personenbezogener Dokumente sind selbst personenbezogen. Sie liegen
in der Datenbank von Mineroo; in Commits, Issues und Logs gehören nur Hashes und
Zustände.
