---
name: mineroo
description: OCR auf Abruf mit Mineroo (MinerU auf RunPod; CLI `mineru-ocr`, Bibliothek `mineru_ocr`). Nutzen, wenn Text, Struktur oder die vollständige MinerU-Antwort aus PDFs, Scans, Bildern oder Office-Dateien gebraucht wird, und wenn auf früher abgeschickte Dokumente gewartet oder ihr Ergebnis geholt werden soll.
---

# Mineroo: Dokumente lesen lassen

Mineroo liest Dokumente mit MinerU auf RunPod und speichert die vollständige
Antwort in Postgres. Die CLI `mineru-ocr` nimmt Dateipfade und liefert Markdown;
die Python-Bibliothek `mineru_ocr` nimmt Bytes und bietet auch Struktur, Bilder
und Job-Verwaltung. Identische Bytes teilen sich dasselbe gespeicherte Ergebnis.

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
