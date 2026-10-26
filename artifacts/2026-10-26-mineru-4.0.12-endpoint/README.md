# Echte MinerU-4-Antworten vom Endpoint (26.10.2026)

Diese Ordner sind **echte `/status`-Antworten des neuen RunPod-Endpoints**, im
Betriebsweg erzeugt: Stufe `advanced`, OCR-Modus `ocr`, Bildanalyse an, über
den Client des Pakets (`runpod.RunPodClient.submit` und `await_result`). Sie
sind die Aufnahmen aus Mineroo#10, die die Tests abspielen
(`tests/ersatz_antworten.py`, `ECHT_ENDPOINT`).

## Womit

- Endpoint `csfbvej3d4cna5` (`mineru-runpod-v4`), queue-basiert, gebaut aus dem
  Fork `ConstantinLevin/mineru-runpod`, `main` auf Commit `516786c`; Image
  `registry.runpod.net/constantinlevin-mineru-runpod-main-dockerfile:516786ccf`.
- Rechenzentren laut Endpoint: `EU-RO-1`, `EU-CZ-1`, `EUR-IS-1`, `EUR-IS-2`,
  `EUR-NO-1` (EWR). GPU-Pools `ADA_24`, `AMPERE_24`, `AMPERE_48`, CUDA ab 13.0,
  `MINERU_MAX_CONCURRENCY=1`.
- Alle neun Jobs liefen auf demselben Worker (`9cl8cq4gycxlil`, NVIDIA GeForce
  RTX 4090, 24 GB). Jede Antwort nennt `mineru_version` `4.0.12` und das Modell
  `opendatalab/MinerU2.5-Pro-2605-1.2B`.
- Harness `runpod-doc-worker` 0.9.1, Transport `tarball_b64`.
- Kosten laut RunPod-Abrechnung des Endpoints: rund 0,05 USD.

## Was

Die Eingaben stehen unter `material/` und sind byte-gleich mit dem, was
abgeschickt wurde. Erfunden, ohne personenbezogene Daten
(`erzeugung/material.py`, reportlab, Pillow, python-docx); `leere_seite.pdf` ist
eine leere A4-Seite aus pypdfium2.

| Antwort | Eingabe | Job | Warteschlange + Kaltstart | Ausführung |
|---|---|---|---|---|
| `normale_seite` | `normale_seite.pdf` | `c7c9eae9-082e-4119-b5fc-1e0a947d8b7d-e2` | 176 s | 2,1 s |
| `leere_seite_bild` | `leere_seite.png` | `c6403b56-34c2-4f4b-aaa6-c6bf1662ff75-e2` | 163 s | 1,4 s |
| `leere_seite_pdf` | `leere_seite.pdf` | `347d289e-f2ed-4e90-bf83-0f9d62ac1796-e2` | 175 s | 1,0 s |
| `mehrteilig_0_seiten_0-4` | `mehrteilig.pdf`, Seiten 0–4 | `e88acf04-3f6e-4439-8791-6a8ac69adca8-e1` | 167 s | 3,4 s |
| `mehrteilig_1_seiten_5-9` | `mehrteilig.pdf`, Seiten 5–9 | `db95c3e1-d851-4275-8357-7ccc920c8aa2-e1` | 171 s | 2,9 s |
| `mehrteilig_2_seiten_10-11` | `mehrteilig.pdf`, Seiten 10–11 | `b735a141-ce39-4eac-a320-98261282e404-e2` | 182 s | 2,1 s |
| `dokument_docx` | `dokument.docx` | `543551b5-bdc7-49bd-ab3c-a0711c2f21aa-e1` | 179 s | 0,9 s |
| `seite_html` | `seite.html` | `0a0a0016-d650-4d65-bba5-a0820e200cb9-e1` | 181 s | 0,6 s |
| `fehlerantwort` | `normale_seite.pdf`, `tier` `gibt-es-nicht` | `07d59f33-bb11-4f9b-b1d0-c0684b216b49-e1` | 166 s | 0,3 s |

Die drei Teilstücke schneidet das Paket selbst (`chunking.plan_chunks`,
`pdf.PdfReader.extract`) bei fünf Seiten je Teilstück; `plan.json` nennt Felder
und Seiten je Job. Die Zeiten der Warteschlange enthalten den Kaltstart des
ersten Workers, auf den alle neun gemeinsam warteten.

## Befunde

- **Bildnamen kollidieren.** Jedes Teilstück zählt ab Seite 0:
  `mehrteilig_0` und `mehrteilig_1` tragen beide `page_0_chart_5.jpg` bis
  `page_4_chart_5.jpg`, `mehrteilig_2` `page_0_chart_5.jpg` und
  `page_1_chart_5.jpg`, mit verschiedenem Inhalt. Ohne den Namensraum je
  Teilstück (`assembly.BILD_NAMENSRAUM`) überschrieben sie einander.
- **Eine leere Seite** ist bei `advanced` mit OCR, als Bild wie als PDF: leeres
  `markdown.md` und `markdown_full.md`, keine Bilder, und im Middle-JSON die
  Seite `{"page_idx": 0}` **ohne** `blocks`-Schlüssel.
- **Randklassen.** `normale_seite` trägt alle fünf als Blöcke: `header`,
  `footer`, `page_number`, `aside_text`, `page_footnote`. `markdown.md` lässt
  Kopfzeile, Fußzeile, Seitenzahl und Randtext weg und behält die
  Seitenfußnote; `markdown_full.md` trägt alle fünf.
- **DOCX und HTML** liest der Worker unabhängig von der Bestellung als `flash`
  mit `txt` und ohne Bildanalyse (`output.tier`, `output.ocr_mode`,
  `output.image_analysis`); die Bestellung steht unter `output.debug.tier`.
- **Jede erfolgreiche Antwort** trägt `markdown.md`, `markdown_full.md`,
  `middle_json.json`, `structured_content.json` und `model_output.json`, dazu
  die Bilder unter `images/`. Kein `degraded` in diesen Antworten.
- **Die Fehlerantwort**: `status` `FAILED`, `error` auf oberster Ebene
  (`ValueError: input validation failed: tier must be one of [...]`), unter
  `output` `ok` `false` und der `traceback`.

## Wie neu erzeugen

Kostet Geld (ein Kaltstart und neun kurze Jobs). Mit diesem Repository auf dem
`PYTHONPATH`, dem RunPod-Schlüssel in `~/.runpod/config.toml` und einem
Arbeitsordner `ARBEIT` außerhalb des Repositorys:

```bash
python3 erzeugung/material.py material
python3 erzeugung/jobs_bauen.py material material/leere_seite.pdf ARBEIT/jobs
python3 erzeugung/abschicken.py ARBEIT/jobs antworten csfbvej3d4cna5
```

`material.py` erzeugt die leere PDF nicht; sie liegt fertig unter `material/`.
`abschicken.py` überspringt jeden Job, dessen Antwort schon da ist.
