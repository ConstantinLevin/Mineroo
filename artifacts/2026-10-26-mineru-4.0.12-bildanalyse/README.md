# Echte MinerU-4-Antwort mit Bild-Deutungen (26.10.2026)

Eine **echte `/status`-Antwort des RunPod-Endpoints**, auf demselben Weg erzeugt
wie die Aufnahmen unter `../2026-10-26-mineru-4.0.12-endpoint/`: Stufe
`advanced`, OCR-Modus `ocr`, Bildanalyse an, Transport `tarball_b64`, über den
Client des Pakets (`runpod.RunPodClient.submit` und `await_result`). Sie gehört
zu Mineroo#13 und zeigt, wo die Bild-Deutungen stehen; die Tests spielen sie ab
(`tests/ersatz_antworten.py`, `ECHT_BILDANALYSE`).

Die übrigen Aufnahmen enthalten nur Diagramme (`chart`). Diese Seite trägt zwei
Bilder, die keine sind: eine gezeichnete Szene und ein Organigramm.

## Womit

- Endpoint `csfbvej3d4cna5` (`mineru-runpod-v4`), Worker NVIDIA GeForce RTX 4090
  (24 GB), MinerU `4.0.12`, Modell `opendatalab/MinerU2.5-Pro-2605-1.2B`.
- Job `7b9f1606-c9cb-4bde-886c-e0f5f7feb3d4-e1`, 135 s von Abschicken bis
  Ergebnis (mit Kaltstart), Ausführung 2,5 s.
- Kosten: Guthaben vorher 2,9085 USD, nachher 2,8795 USD, also rund 0,029 USD
  einschließlich Kaltstart.

## Was

`material/bild_seite.pdf` ist byte-gleich mit dem, was abgeschickt wurde.
Erfunden, ohne personenbezogene Daten (`erzeugung/material.py`, reportlab,
Pillow): eine Seite mit Kopfzeile, Überschrift, Fließtext, einer gezeichneten
Szene (Haus mit rotem Dach, Baum, Sonne), einem Organigramm (Geschäftsführung
über Einkauf, Fertigung, Vertrieb), je mit Bildunterschrift, und einer
Seitenzahl.

## Befunde

- **Die Bild-Deutungen** stehen in `markdown.md` und in `markdown_full.md`
  gleich, hinter dem Bild und vor der Bildunterschrift, als eingeklappter Block:
  `<details>`, darin `<summary>` mit der Art des Bildes aus dem Middle-JSON
  (`sub_type`) und die Deutung. Die Szene ist `natural_image` mit einer
  englischen Beschreibung, das Organigramm `flowchart` mit einem
  Mermaid-Graphen.
- Im Middle-JSON steht die Deutung im `content` des `image_body`, das Bild ist
  `type` `image` mit `sub_type`.
- `markdown.md` lässt die Kopfzeile und die Seitenzahl weg, `markdown_full.md`
  trägt beide an ihrer Stelle (Kopfzeile zuerst, Seitenzahl zuletzt), wie auf
  der normalen Seite der übrigen Aufnahmen.

## Wie neu erzeugen

Kostet Geld (ein Kaltstart und ein kurzer Job). Mit diesem Repository auf dem
`PYTHONPATH` und dem RunPod-Schlüssel in `~/.runpod/config.toml`:

```bash
python3 erzeugung/material.py material
python3 erzeugung/abschicken.py material antworten csfbvej3d4cna5
```

`abschicken.py` überspringt den Job, wenn die Antwort schon da ist.
