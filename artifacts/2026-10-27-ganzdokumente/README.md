# Ganzdokument-Formate am Endpoint (27.10.2026)

Zwei Jobs zu Mineroo#17, im Betriebsweg abgeschickt: `mineru-ocr ocr` reiht ein,
`mineru-ocr daemon` arbeitet ab, `mineru-ocr wait` wartet, alles mit dem Paket aus
dem Arbeitsbaum dieses Tickets und einer Wegwerf-Datenbank. Gesendet wurden die
Voreinstellungen der Eingabe (`advanced`, `ocr`, Bildanalyse an).

## Womit

- Endpoint `csfbvej3d4cna5`, Image
  `registry.runpod.net/constantinlevin-mineru-runpod-main-dockerfile:2bccaebd0`
  (`image_tag_at_submit` beider Zeilen), MinerU `4.0.12`, Worker
  `i6qq4m9h9gksbk` (NVIDIA GeForce RTX 4090, 24 GB), Transport `tarball_b64`.
- Kosten: Das Guthaben fiel in dem Zeitraum um 0,106 USD (2,1437 auf 2,0374).
  Beide Jobs warteten gemeinsam auf einen Kaltstart (rund 100 s) und liefen je
  rund eine Sekunde.

## Was

`material/` ist byte-gleich mit dem, was abgeschickt wurde, erfunden und ohne
personenbezogene Daten. `hausmitteilung.md` ist die Quelle; daraus
`pandoc hausmitteilung.md -s -o hausmitteilung.html`, dann
`textutil -convert doc hausmitteilung.html -output hausmitteilung.doc` (macOS).
Die Metadaten des DOC tragen keinen Autor. `ohne_word_daten.cfb` ist dasselbe
DOC, in dessen OLE2-Verzeichnis der Name des Stroms `WordDocument` durch
`WortDokument` ersetzt ist (gleiche Laenge, sonst byte-gleich).

| Eingabe | sha256 | Job | Ergebnis |
|---|---|---|---|
| `hausmitteilung.doc` | `540291f6…be4b` | `c4b7a6e8-41bd-4e56-9c8c-9210ae049643-e1` | `done` |
| `ohne_word_daten.cfb` | `013d77b1…ec2d` | `4f8517ab-343d-4760-b99f-4daf888e20e4-e2` | `failed` (vor der Korrektur) |

`antworten/ole2_ohne_word_daten.response.json` ist die volle `/status`-Antwort
des zweiten Jobs, so wie sie im `raw` der Zeile unter `unverwertbare_antworten`
stand. Die Antwort des ersten Jobs liegt nicht hier: Das Paket speichert das
zusammengesetzte Ergebnis, nicht die `/status`-Antwort.

## Befunde

- **Ein altes Word-Dokument** (OLE2) lässt die Vorerkennung als
  `application/x-ole-storage` durch; MinerU liest es ganz und meldet `flash`,
  `txt`, Bildanalyse aus (bestellt unter `debug.tier`: `advanced`). Text und
  Tabelle kommen vollständig an, eine logische Seite (`page_idx` 0), keine
  Seitenzahl. Die Zeile trägt keine Lese-Einstellungen und keinen Vermerk.
- **Ein OLE2-Container ohne `WordDocument`**: `status` `FAILED`, `error`
  `LegacyOfficeMissingPartError: missing required OLE stream: WordDocument`,
  entstanden in DocVortex (`docvortex.analyzers.native.office.errors`, laut
  `traceback`). MinerU erkennt die Datei als DOC und kann sie nicht lesen. Das
  Paket kannte die Meldung nicht und schrieb `failed`; seither führt es
  DocVortex' Fehlertypen für Ganzdokumente als Abweisung der Datei
  (`errors.DATEIABWEISUNG_KENNUNGEN`), und das Dokument endet `input_error`.
- Der Preflight meldete vor dem Lauf eine Abweichung, die mit den
  Ganzdokument-Formaten nichts zu tun hat: `MINERU_MODEL_SOURCE` ist auf keinem
  der zuletzt gelaufenen Worker gesetzt (Soll `local`).
