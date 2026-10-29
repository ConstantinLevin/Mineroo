# Python-Bibliothek verwenden

Für neues OCR gelten zuerst [Prüfung und Auftragsgrenze](ein-dokument.md).
Benutze einen Python-Interpreter, in dem `mineru_ocr` installiert ist.
Die Bibliothek nimmt vollständige Dokumentbytes; Dateipfade liest der Aufrufer.

## Ein Dokument, synchron

```python
from pathlib import Path
from mineru_ocr import OCRClient

with OCRClient() as client:
    huelle = client.ocr(Path("dokument.pdf").read_bytes(), timeout=600)
    if huelle.success:
        print(huelle.rendered_markdown)
    else:
        print(huelle.sha256, huelle.status.value)
```

`OCRClient()` prüft beim Öffnen Schema und, wenn Schlüssel und Endpoint
konfiguriert sind, automatisch den Preflight. Ein `ConfigDriftError` meldet
abweichende Sollwerte; gib die Meldung dem Nutzer weiter.

## Abschicken, warten und später holen

| Bedarf | Synchroner Aufruf |
| --- | --- |
| Einreihen ohne Warten; Hash als `Submitted` zurück | `h = client.ocr_start(dokumentbytes)` |
| Mehrere einreihen; Hashes in Eingabereihenfolge | `hs = client.ocr_start([bytes_a, bytes_b])` |
| Auf einen Hash warten | `huelle = client.wait(h, timeout=600)` |
| Auf alle warten, in Eingabereihenfolge sammeln | `huellen = client.wait_all(hs, timeout=600)` |
| Fertige einzeln verarbeiten | `for huelle in client.as_completed(hs, timeout=600): ...` |
| Gespeichertes Ergebnis ohne Warten holen | `huelle = client.get(h)` |
| Zustand eines benannten Dokuments ohne `raw` | `stand = client.document(h)` |

Setze bei jedem wartenden Aufruf (`ocr`, `wait`, `wait_all`, `as_completed`)
`timeout=`. Die Frist gilt je Aufruf; bei `WaitTimeoutError` bleibt der Job
unterwegs. Bei mehreren Hashes nennt `.pending` die offenen.
`get` wirft `DocumentNotFound`, wenn es noch kein Ergebnis gibt.
Hashes funktionieren auch in einer späteren Sitzung oder einem anderen Prozess.
Zum reinen Abholen genügt `OCRClient(preflight=False)` ohne RunPod-Zugang.

Die Bibliothek reiht auch ohne Daemon ein und warnt über `mineru_ocr.client`.
Gib die Warnung vor dem Warten weiter: Offen bleibt das Dokument, bis ein
Daemon arbeitet. Bei der CLI führt derselbe Fall zum Abbruch mit Exit 5.
Stapel und Speicherbedarf: [Viele Dokumente](viele-dokumente.md).

## Asynchron

```python
from mineru_ocr import AsyncOCRClient

async def lesen(dokumente: list[bytes]) -> None:
    async with AsyncOCRClient() as client:
        hashes = await client.ocr_start(dokumente)
        async for huelle in client.as_completed(hashes, timeout=600):
            if huelle.success:
                print(huelle.rendered_markdown)
            else:
                print(huelle.sha256, huelle.status.value)
```

`ocr_start`, `ocr`, `wait`, `wait_all` und `get` werden mit `await` aufgerufen.
`as_completed` ist ein async-Generator und nimmt `async for`.
`preflight`, `status`, `document`, `retry`, `retry_all` und die RunPod-Auskünfte
gibt es nur synchron. Der Async-Client fragt RunPod nie selbst ab.
Beim asynchronen Warten kommt die Ladezeit fertiger Ergebnisse zur Wartefrist hinzu.
Ein Client gehört jeweils einem Aufrufer bzw. einer asynchronen Aufgabe;
gleichzeitige Aufrufer benutzen eigene Clients.

## Die Ergebnishülle `OCRWrapper`

Jeder Endzustand kommt als Hülle zurück, auch `failed` und `input_error`.
Prüfe `.success`: `done` und `empty` sind Erfolg; `.status` unterscheidet sie.

| Feld | Inhalt |
| --- | --- |
| `.rendered_markdown` | Vollfassung aus `markdown_full`, mit Randtexten und Bilddeutungen |
| `.readable_markdown` | Vollfassung mit HTML-Inseln als GFM-Markdown |
| `.raw` | Vollständiges gespeichertes Ergebnis, auch bei Fehlschlag |
| `["middle_json"]` | Struktur der gelesenen Seiten |
| `.images` | Bildnamen auf dekodierte Bildbytes |
| `.read_settings`, `.mineru_version` | Bestellte Leseeinstellungen und MinerU-Version |
| `.to_dict(raw=True)` | Dokumentangaben einschließlich `raw` |

Die Vollfassung trägt Kopf-/Fußzeilen, Seitenzahlen, Randtexte und
Seitenfußnoten in Lesereihenfolge sowie Modell-Deutungen hinter Bildern als
`<details>`; eine Deutung kann falsch sein. Fehlt die Vollfassung, wirft ihre
Darstellung `IncompleteResultError`; verwende dann `.raw` zur Diagnose.

Synchrone Hüllen laden `raw` bei Bedarf über die Client-Verbindung: Verarbeite
oder exportiere sie innerhalb von `with`. Asynchrone Hüllen haben `raw` bereits
geladen; hole sie bei Bedarf mit `await client.get(hash)` erneut. Bei großen
Antworten bevorzuge `as_completed` und verarbeite die Hüllen einzeln.

Leseparameter an `ocr_start`/`ocr`: [Leseeinstellungen](technik.md#leseeinstellungen).
`status()`, `preflight()`, `document()` und Wiederholen bei Problemen:
[Technik und Betrieb](technik.md).
Die genauen Methodenverträge stehen in `help(OCRClient)`, `help(AsyncOCRClient)`
und `help(OCRWrapper)` nach dem jeweiligen Import.
