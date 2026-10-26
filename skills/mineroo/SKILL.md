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
Dateitypen gelesen werden, steht in `mineru_ocr.DocumentType`; alles andere
weist das Abschicken ab.

## Vor dem ersten Abschicken: Rechenzentren prüfen

Der Endpoint soll nur in Rechenzentren des EWR rechnen (EU-Staaten, Island und
Norwegen; dort gilt die DSGVO). Mineroo weiß nicht, ob ein Dokument
personenbezogen ist, und behandelt deshalb jedes so. Ob der Endpoint die
zulässige Liste einhält, sagt allein der Preflight, und weder das Kommando `ocr`
noch der `AsyncOCRClient` lassen ihn laufen. Deshalb, einmal je Sitzung vor dem
ersten Abschicken, auf jedem Weg:

`mineru-ocr preflight --no-strict --json`, darin der Eintrag mit
`"punkt": "datacenters"`:

- `in_ordnung`: Du darfst abschicken, auch personenbezogene Dokumente.
- `abweichung`: Du schickst nichts ab und zeigst dem Nutzer `soll` und `ist`.
- Jedes andere Ergebnis: `nicht_pruefbar` (der Endpoint nennt seine
  Rechenzentren nicht), kein `datacenters`-Eintrag (der Endpoint war nicht
  erreichbar) oder ein Preflight, der mit einem Fehler endet (etwa ohne
  RunPod-Schlüssel oder Endpoint). Dann sagst du dem Nutzer, dass sich die
  Rechenzentren nicht bestätigen lassen, samt der Meldung, und schickst erst ab,
  wenn er zustimmt.

Der Preflight verändert nichts, fragt aber RunPod und braucht deshalb den
RunPod-Schlüssel; dasselbe gilt für `mineru-ocr status`.

## Ablauf über die CLI

Für Dateien und Ordner auf der Platte:

1. `mineru-ocr ocr PFAD… --json`; je Datei steht dort ihr Hash.
2. `mineru-ocr wait ZIEL… --timeout S --json`. Ziele sind Dateien oder Hashes;
   für einen Ordner gibst du die Hashes aus Schritt 1 weiter. Läuft die Frist
   ab, wartest du später erneut auf die noch offenen.
3. Ergebnis holen: `mineru-ocr markdown ZIEL` für den Text des ganzen Dokuments,
   `mineru-ocr show ZIEL --raw` für die vollständige Antwort als JSON.

Setz bei `wait` immer `--timeout`: Ohne Daemon wird nichts fertig.

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
  Markdown, `raw` die vollständige Antwort.

Jeder Endzustand kommt als Hülle zurück, auch `failed` und `input_error`. Prüf
`.success`, bevor du `rendered_markdown` liest; `empty` heißt, das Dokument war
leer, und ist gültig. Eine abgelaufene Frist fasst keinen Job an: Das Dokument
bleibt unterwegs, und du wartest später erneut darauf.

Die Hüllen des Async-Clients kommen mit geladenem `raw`. Bei vielen großen
Dokumenten nimmst du `as_completed` und verarbeitest jede Hülle, bevor die
nächste kommt.

## Lese-Einstellungen

Stufe, OCR-Modus und Bildanalyse wählst du je Lauf: beim Kommando `ocr` mit
`--tier`, `--ocr-mode` und `--no-image-analysis`, in der Bibliothek mit `tier=`,
`ocr_mode=` und `image_analysis=` an `ocr_start` und `ocr`. Ohne Angabe liest
MinerU in `advanced` mit erzwungenem OCR und Bildanalyse; davon weichst du nur ab,
wenn der Nutzer es verlangt (etwa eine saubere digitale PDF im Textmodus).

Die Einstellungen wirken nur auf Dokumente, die der Aufruf neu einreiht. Ein
Treffer liefert das gespeicherte Ergebnis, gleich welche Einstellungen du
verlangst, und ein Neu-Lesen mit anderen gibt es nicht. Womit ein Ergebnis
gelesen wurde, zeigen `mineru-ocr show ZIEL` und an der Hülle `.read_settings` und
`.mineru_version`; wie viele Dokumente mit welchen gelesen sind, zeigt
`mineru-ocr status`.

## Der Daemon

Fertig wird ein Dokument nur, wenn ein `mineru-ocr daemon` läuft: Er ist der
einzige Prozess, der Dokumente an RunPod schickt. Abschicken gelingt auch ohne
ihn und warnt nur (CLI auf stderr, Bibliothek im Logger `mineru_ocr.client`);
die Dokumente bleiben dann offen, bis einer startet. Den Daemon startet der
Betreiber. Kommt beim Abschicken die Warnung, gibst du sie dem Nutzer weiter,
bevor du wartest; sie sagt, ob kein Daemon läuft oder ob sich das nicht prüfen
ließ. Ob einer läuft, zeigen auch `mineru-ocr status` und der Preflight.

Hängt ein Dokument, sagt `mineru-ocr show ZIEL`, ob sein Job wartet, läuft oder
ohne Ergebnis endete. Im letzten Fall und bei `failed` hilft
`mineru-ocr retry ZIEL --yes`, nachdem der Nutzer zugestimmt hat (siehe Kosten).
Nennt `show` als Fehler `UnstorableResultError`, liess sich das Ergebnis nicht
speichern; ein `retry` bezahlt das Dokument neu und scheitert voraussichtlich
wieder. Das sagst du dem Nutzer, bevor er zustimmt.

## Was Lesen kostet

Bezahlt wird die Rechenzeit der Worker bei RunPod: für jedes Dokument, das neu
gelesen wird, dazu der Kaltstart eines Workers und seine Leerlaufzeit nach dem
letzten Job. Was eine Seite kostet, ist noch nicht gemessen (Mineroo#19); fragt
der Nutzer nach dem Preis, sagst du ihm genau das. Keine Rechenzeit kosten ein
Treffer, das Warten und das Abholen (`wait`, `get`, `show`, `markdown`). `retry`
lässt ein Dokument ohne gültiges Ergebnis neu lesen und bezahlt es neu.

Schick ab, was der Nutzer genannt hat. Willst du darüber hinaus weitere Dokumente
lesen lassen oder ein `retry` auslösen, fragst du ihn vorher.

## Ergebnisse personenbezogener Dokumente

Ergebnisse personenbezogener Dokumente sind selbst personenbezogen. Sie liegen
in der Datenbank von Mineroo; in Commits, Issues und Logs gehören nur Hashes und
Zustände.
