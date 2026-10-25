# Echte MinerU-4.0.12-Ausgaben, lokal erzeugt (26.10.2026)

Diese Ordner sind **echte Ausgaben von MinerU 4.0.12**, geschrieben von MinerUs
eigenem `ParseResult.save`, wie der Worker des Forks `ConstantinLevin/mineru-runpod`
es tut (`worker/parse.py`). Dazu `markdown_full.md`, wie der Worker es rendert
(`ParseResult.from_json(...).markdown(mode=RenderMode.FULL)`). Sie sind **nicht**
über RunPod gelaufen: Den neuen Endpoint gibt es noch nicht (Mineroo#10).

## Womit

- MinerU 4.0.12, Quelltext des Tags `mineru-4.0.12-released` (Tag-Objekt
  `b4edf7e`, Commit `b7f3dd0`), `mineru/version.py` = `4.0.12`.
- DocVortex 0.5.16, die Mindestfassung, die MinerU 4.0.12 verlangt.
- Stufe `flash`, OCR-Modus `txt`, Bildanalyse aus: MinerUs nativer Weg über
  DocVortex, ohne Modelle, auf der CPU. Die Stufe `advanced` mit OCR, die der
  Betrieb fährt, braucht die GPU-Modelle und kommt mit den Aufnahmen aus Mineroo#10.
  Das Dateiformat (Dateinamen, Schema `docvortex.middle` 2.0, Bildnamen und
  Verweise) ist dasselbe; welche Blöcke das Modell erkennt, ist es nicht.

## Was

- `leere_seite.pdf` → `leere_seite/`: eine leere A4-Seite (pypdfium2). Befund:
  leeres `markdown.md` und `markdown_full.md`, keine Bilder, und im Middle-JSON
  die Seite `{"page_idx": 0}` **ohne** `blocks`-Schlüssel, weil MinerU
  Voreinstellungen nicht schreibt.
- `musterbericht.pdf`: ein erfundener Bericht ohne personenbezogene Daten
  (`erzeugung/musterbericht.py`, reportlab): neun Seiten mit Kopf- und Fußzeile,
  Seitenzahl, Text, einem Bild je Seite und einer Tabelle auf jeder dritten.
  - `musterbericht_ganz/`: das Dokument in einem Stück.
  - `musterbericht_teile/teil_N/`: dasselbe Dokument mit dem Schnitt des Pakets
    (`chunking.plan_chunks`, `pdf.PdfReader.extract`) bei vier Seiten je
    Teilstück, jedes Teilstück einzeln gelesen; `plan.json` nennt die Seiten
    `[[0, 3], [4, 7], [8, 8]]`. Befund: Jedes Teilstück zählt ab Seite 0, und
    `teil_0` und `teil_1` tragen vier gleich benannte Bilder
    (`page_0_image_4.jpg` bis `page_3_image_4.jpg`) mit verschiedenem Inhalt.
    Tabellen-Ausschnitte rendert MinerU aus dem geschnittenen PDF minimal anders
    als aus dem ganzen (gleiche Größe, höchstens 5/255 Abweichung je Pixel).
  - Befund zu Randklassen: MinerU stuft in `flash`/`txt` die Kopfzeile, die
    Kapitelüberschrift und den ersten Satz jeder Seite als `header` ein und die
    Seitenzahl als `footer`; `markdown.md` lässt sie weg, `markdown_full.md`
    trägt sie.

## Wie neu erzeugen

Mit dem Quelltext von MinerU 4.0.12 und DocVortex 0.5.16 auf dem `PYTHONPATH`
(dazu dieses Repository für den Schnitt), ohne etwas zu installieren:

```bash
python3 erzeugung/musterbericht.py musterbericht.pdf
python3 erzeugung/erzeugen.py musterbericht.pdf musterbericht_ganz
python3 erzeugung/teilstuecke.py musterbericht.pdf musterbericht_teile 4
python3 erzeugung/erzeugen.py leere_seite.pdf leere_seite
```

`erzeugung/teilstuecke.py` schreibt den Plan als eine JSON-Zeile je Teilstück auf
stdout; `plan.json` ist daraus gebildet.
