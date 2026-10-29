# Viele Dokumente lesen

Vor dem ersten Abschicken gelten [Prüfung und Auftragsgrenze](ein-dokument.md).
Wähle die Dateien oder Ordner aus dem Auftrag; danach genügt der passende Weg.

## Dateien oder Ordner über die CLI

```bash
mineru-ocr -d "scans/" -o texte.md --timeout 600
mineru-ocr -d "a.pdf" "b.pdf" -t --timeout 600
```

Ordner werden rekursiv nach Pfad sortiert gelesen. Fremde und zu große Dateien
werden darin mit Grund übersprungen; Fehler ausdrücklich genannter Dateien
halten die übrigen Dateien nicht auf. Prüfe stderr und den Exit-Code auf
ausgelassene oder gescheiterte Dokumente.

Mit `-d` werden offene Dokumente vor dem Warten eingereiht; ohne `-d` wird jede
Datei nacheinander eingereiht und abgewartet. Die Textausgabe bleibt in
Eingabereihenfolge, mit `# Datei: PFAD` vor jedem Text. `-t` und `-o` schreiben
den ganzen Stapel in eine Datei; stdout nennt deren Pfad. Lies diese Datei.
`--timeout` gilt je Dokument, nicht für den ganzen Stapel.
Weitere Ausgabeformen und der Vorrang gemischter Exit-Codes stehen in [CLI](cli.md).

## Über Python einreihen und nach Fertigstellung verarbeiten

```python
from pathlib import Path
from mineru_ocr import OCRClient

pfade = [Path("a.pdf"), Path("b.pdf")]
with OCRClient() as client:
    hashes = client.ocr_start([p.read_bytes() for p in pfade])
    for huelle in client.as_completed(hashes, timeout=600):
        if huelle.success:
            text = huelle.rendered_markdown
            # Hier den Text für den Auftrag verarbeiten.
        else:
            print(huelle.sha256, huelle.status.value)
```

`ocr_start` prüft alle übergebenen Dokumente vor dem Einreihen. Bei großen
Beständen reiche überschaubare Gruppen ein, um ihre Bytes nicht alle zugleich
im Speicher zu halten. Mehrere Agenten können gleichzeitig einreichen:
Dieselben Bytes werden nur einmal eingereiht, auch bei überlappenden Gruppen.

`wait_all(hashes, timeout=600)` sammelt alle Hüllen in Eingabereihenfolge;
`as_completed` liefert jeden eindeutigen Hash, sobald er fertig ist. Die Wartefrist
gilt jeweils für alle gefragten Hashes zusammen. Bei `WaitTimeoutError` nennt `.pending`
die offenen Hashes; warte später auf diese weiter. Bereits verarbeitete
Ergebnisse bleiben erhalten, und kein Job wird durch die Frist abgebrochen.

Beim Async-Client ist `as_completed` ein async-Generator: `async for`, ohne
`await` am Aufruf. Seine Hüllen tragen `raw` bereits geladen; bei großen
Antworten verarbeite jede Hülle, bevor du die nächste holst, statt alle mit
`wait_all` zu sammeln. Details zu Clients und Hüllen: [Python](python.md).
