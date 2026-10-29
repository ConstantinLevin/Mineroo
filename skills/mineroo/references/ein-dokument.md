# Ein Dokument lesen

## Vor dem Abschicken

Mineroo behandelt jedes Dokument als potenziell personenbezogen. Prüfe einmal
je Sitzung vor dem ersten Abschicken die Rechenzentren; die CLI und der
Async-Client erledigen das nicht. Zulässig sind die konfigurierten
EWR-Rechenzentren in EU-Staaten, Island und Norwegen.

```python
from mineru_ocr import OCRClient

with OCRClient(preflight=False, strict=False, schema_check=False) as client:
    befund = client.preflight()
rechenzentren = next(
    (p for p in befund["checks"] if p["punkt"] == "datacenters"), None
)
print(rechenzentren)
```

Entscheidend ist `rechenzentren["bewertung"]`:

- `in_ordnung`: Abschicken ist erlaubt, auch bei personenbezogenen Dokumenten.
- `abweichung`: Nichts abschicken; dem Nutzer `soll` und `ist` zeigen.
- `nicht_pruefbar`, fehlender Eintrag oder Fehler beim Preflight: Meldung
  weitergeben und erst nach Zustimmung abschicken.

Der Preflight verändert nichts und erzeugt keine OCR-Rechenkosten. Er fragt
RunPod ab und braucht dessen Schlüssel sowie einen konfigurierten Endpoint.
Beim reinen Abholen eines gespeicherten Ergebnisses wird nichts abgeschickt.

## Lesen

```bash
mineru-ocr "dokument.pdf" --timeout 600
```

Die CLI wartet und liefert den vollständigen Markdown-Text auf stdout; Meldungen
stehen auf stderr. Lies den ausgegebenen Text und bearbeite damit den Auftrag.
Ein gespeichertes Ergebnis kommt ohne erneute OCR, auch ohne Daemon.

- **Exit 0:** Erfolg; `empty` ist ein gültiges leeres Dokument, mit Grund auf stderr.
- **Exit 4:** Nur das Warten endete; derselbe Dateiaufruf setzt es später fort.
- **Exit 5:** Kein laufender Daemon; für diese Datei wurde nichts eingereiht.
  Der Betreiber muss einen Daemon bereitstellen, siehe [Technik](technik.md#daemon).
- **Exit 3:** `failed` oder `input_error`; Grund weitergeben. Derselbe Dateiaufruf
  bezahlt einen gespeicherten Fehlschlag nicht neu. Diagnose: [Technik](technik.md#fehlerdiagnose).

Andere Exit-Codes und Dateiausgabe stehen in [CLI](cli.md).

## Auftragsgrenze und Daten

Neu gelesene Dokumente kosten RunPod-Rechenzeit; Treffer, Warten und Abholen
kosten keine OCR-Rechenzeit. Schicke die vom Nutzer genannten Dokumente ab.
Für weitere Dokumente oder ein `retry` brauchst du vorher seine Zustimmung.
Personenbezogene Ergebnisse bleiben personenbezogen: In Commits, Issues und
Betriebslogs gehören nur Hashes und Zustände, keine Dokumentinhalte.
