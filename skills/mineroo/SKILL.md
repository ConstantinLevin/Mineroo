---
name: mineroo
description: OCR auf Abruf mit Mineroo (MinerU auf RunPod), über die CLI `mineru-ocr` oder die Python-Bibliothek `mineru_ocr`. Nutzen, wenn Text, Struktur oder die vollständige MinerU-Antwort aus PDFs, Scans, Bildern oder Office-Dateien gebraucht wird, für eine Datei bis zu vielen; und wenn auf früher abgeschickte Dokumente gewartet oder ihr Ergebnis geholt werden soll.
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
(`help(mineru_ocr.OCRClient)`). Welche Dateitypen gelesen werden, steht in
`mineru_ocr.DocumentType`; alles andere weist das Abschicken ab.

## Ablauf über die CLI

Für Dateien und Ordner auf der Platte:

1. `mineru-ocr ocr PFAD… --json` reiht ein und kehrt sofort zurück. Je Datei
   steht dort ihr Hash; bei einem Treffer ist der Zustand schon gesetzt.
2. `mineru-ocr wait ZIEL… --timeout S --json` wartet auf alle und meldet jedes,
   sobald es endgültig ist. Ziele sind Dateien oder Hashes, keine Ordner: Für
   einen Ordner gibst du die Hashes aus Schritt 1 weiter.
3. Ergebnis holen: `mineru-ocr markdown ZIEL` für den Text des ganzen Dokuments,
   `mineru-ocr show ZIEL --raw` für die vollständige Antwort als JSON.

Setz bei `wait` immer eine Frist. Ohne Daemon wird nichts fertig, und ohne Frist
wartet `wait` dann unbegrenzt.

## Ablauf über die Bibliothek

`OCRClient` ist synchron, `AsyncOCRClient` asynchron, mit denselben Namen; beim
Async-Client steht vor jedem Aufruf `await`. Beide öffnest du mit `with` bzw.
`async with`. Importierbar ist das Paket im Interpreter der Miniconda-Base
(`/opt/homebrew/Caskroom/miniconda/base/bin/python3`), in dem es installiert ist.
Ist ein RunPod-Schlüssel eingestellt, prüft `with OCRClient()` beim Öffnen den
Endpoint wie `mineru-ocr preflight` und wirft bei einer Abweichung
`ConfigDriftError`; die Meldung gibst du dem Nutzer weiter.

- `ocr_start(bytes)` oder `ocr_start([bytes, …])` gibt je Dokument den Hash
  zurück, ein `Submitted`: eine Zeichenkette, dazu `.queued` (eingereiht) und
  `.status` (Endzustand bei einem Treffer, `None`, solange es offen ist).
- Warten: `wait(h)` auf eines, `wait_all(hs)` auf alle in Eingabereihenfolge,
  `as_completed(hs)` auf jedes, sobald es fertig ist. Gib `timeout=` mit.
- `get(h)` holt ein vorhandenes Ergebnis, ohne zu warten; `ocr(bytes)` schickt
  ab und wartet in einem Zug.
- Zurück kommt eine **Hülle** (`OCRWrapper`): `rendered_markdown` ist das ganze
  Dokument als Markdown, samt Kopf- und Fußzeilen und Randtexten; `raw` ist die
  vollständige Antwort; `w["content_list"]` und die übrigen Schlüssel reichen
  `raw` durch; `images` gibt die Bilder als Bytes.

Jeder Endzustand ist ein Ergebnis und keine Ausnahme: `failed` und
`input_error` kommen als Hülle zurück, prüf `.success` oder `.status`. `empty`
heißt, das Dokument war leer, und ist gültig. Ein unbekannter Hash wirft sofort
`DocumentNotFound`, eine abgelaufene Frist `WaitTimeoutError` mit den noch
offenen Hashes. Frist und Abbruch des Wartens fassen keinen Job an; das Dokument
bleibt unterwegs, und du kannst später wieder darauf warten.

Die Hüllen des Async-Clients kommen mit geladenem `raw`. Bei vielen großen
Dokumenten nimmst du `as_completed` und verarbeitest jede Hülle, bevor die
nächste kommt.

## Der Daemon

Fertig wird ein Dokument nur, wenn ein `mineru-ocr daemon` läuft: Er ist der
einzige Prozess, der mit RunPod spricht. Abschicken gelingt auch ohne ihn und
warnt nur (CLI auf stderr, Bibliothek im Logger `mineru_ocr.client`); die
Dokumente bleiben dann offen, bis einer startet. Ob einer läuft, zeigt
`mineru-ocr status`. Den Daemon startet der Betreiber: Läuft keiner, sagst du es
dem Nutzer, bevor du wartest.

Hängt ein Dokument, sagt `mineru-ocr show ZIEL`, ob sein Job wartet, läuft oder
ohne Ergebnis endete. Im letzten Fall, und bei `failed`, bringt
`mineru-ocr retry` es wieder in Gang.

## Was Lesen kostet

Bezahlt wird die Rechenzeit der Worker bei RunPod: für jedes Dokument, das neu
gelesen wird, dazu der Kaltstart eines Workers und seine Leerlaufzeit nach dem
letzten Job. Was eine Seite kostet, ist noch nicht gemessen (Mineroo#19); nenn
deshalb keine Zahl. Nichts kosten ein Treffer, das Warten und das Abholen
(`wait`, `get`, `show`, `markdown`, `status`). `retry` liest neu und bezahlt
neu.

Schick ab, was der Nutzer genannt hat. Willst du darüber hinaus weitere Dokumente
lesen lassen, fragst du ihn vorher.

## Personenbezogene Daten

Der Endpoint rechnet nur in Rechenzentren des EWR (EU-Staaten, Island und
Norwegen; dort gilt die DSGVO). Mineroo weiß nicht, ob ein Dokument
personenbezogen ist, und behandelt deshalb jedes so; personenbezogene Dokumente
darfst du damit lesen lassen. Ob die Rechenzentren des Endpoints in der
zulässigen Liste liegen, prüft der Preflight. Meldet er dort eine Abweichung,
schickst du nichts ab und sagst es dem Nutzer.

Ergebnisse personenbezogener Dokumente sind selbst personenbezogen. Sie liegen
in der Datenbank von Mineroo; in Commits, Issues und Logs gehören nur Hashes und
Zustände, nie Text oder `raw`.
