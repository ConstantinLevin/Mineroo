---
name: mineroo
description: OCR und Audiotranskription mit Mineroo (CLI `mineru-ocr`, Bibliothek `mineru_ocr`). Nutzen für Text und Struktur aus Dokumenten, Transkripte mit Sprecherwechseln aus Audiodateien sowie Warten und Abholen früherer Ergebnisse.
---

# Mineroo: Dokumente lesen lassen

Mineroo liest Dokumente mit MinerU auf RunPod und speichert die vollständige
Antwort in Postgres. Die CLI `mineru-ocr` nimmt Dateipfade und liefert Markdown;
die Python-Bibliothek `mineru_ocr` nimmt Bytes und bietet auch Struktur, Bilder
und Job-Verwaltung. Identische Bytes teilen sich dasselbe gespeicherte Ergebnis.
Audio liest ElevenLabs Scribe v2 in einer eigenen Daemon-Lane, mit Verarbeitung
in den USA. Vor Audio-Aufträgen [Audio: Betrieb und Kosten](references/technik.md#audio)
lesen; die EWR-Prüfung für OCR gilt weiterhin für RunPod.

## Welche Referenz brauchst du?

Lies nur die Referenzen, deren Bedingung auf deinen Auftrag zutrifft:

- **Ein Dokument lesen:** [Ein Dokument](references/ein-dokument.md).
  Enthält die Prüfung vor dem Abschicken, einen Dateiaufruf und den Umgang mit
  dessen Ergebnis. Für den normalen PDF-Auftrag reicht diese Referenz.
- **Mehrere Dokumente oder einen Ordner lesen:**
  [Viele Dokumente](references/viele-dokumente.md).
- **CLI-Ausgabe wählen, Optionen oder Exit-Codes nachsehen:**
  [CLI](references/cli.md).
- **Python verwenden, mit Hashes warten oder Struktur und Bilder holen:**
  [Python-Bibliothek](references/python.md).
- **Einrichtung, Daemon-Betrieb, Fehlerdiagnose, Leseeinstellungen oder
  Kostenschätzung:** [Technik und Betrieb](references/technik.md).
