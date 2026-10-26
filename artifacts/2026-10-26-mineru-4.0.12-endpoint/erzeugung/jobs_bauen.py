"""Baut die Job-Eingaben der Aufnahme mit `runpod.job_eingabe`, der Nutzlast von `submit`.

Aufruf (mit diesem Repository auf dem PYTHONPATH): jobs_bauen.py MATERIAL LEERE_PDF ZIEL
Je Job eine Datei ZIEL/<name>.job.json mit {"id": null, "input": {...}} und eine
Zeile in ZIEL/plan.json mit Name und Seitenbereich.
"""
import base64
import dataclasses
import json
import pathlib
import sys

from mineru_ocr.chunking import plan_chunks
from mineru_ocr.config import Settings
from mineru_ocr.pdf import PdfReader
from mineru_ocr.runpod import job_eingabe

material, leere_pdf, ziel = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3])
ziel.mkdir(parents=True, exist_ok=True)
einstellung = Settings(db_dsn="ungenutzt", endpoint_id=None, api_key=None)
plan = []


def job(name: str, daten: bytes, **abweichend: str) -> None:
    felder = {"tier": einstellung.tier, "ocr_mode": einstellung.ocr_mode,
              "transport": einstellung.transport, **abweichend}
    eingabe = job_eingabe(file_b64=base64.b64encode(daten).decode("ascii"),
                          image_analysis=einstellung.image_analysis, **felder)
    (ziel / f"{name}.job.json").write_text(json.dumps({"id": None, "input": eingabe}))
    plan.append({"name": name, "felder": {k: v for k, v in eingabe.items() if k != "file_b64"}})


job("normale_seite", (material / "normale_seite.pdf").read_bytes())
job("leere_seite_bild", (material / "leere_seite.png").read_bytes())
job("leere_seite_pdf", leere_pdf.read_bytes())
daten = (material / "mehrteilig.pdf").read_bytes()
with PdfReader(daten) as leser:
    teile = plan_chunks(leser.page_count, dataclasses.replace(einstellung, max_pages_per_chunk=5))
    for nummer, chunk in enumerate(teile):
        name = f"mehrteilig_{nummer}_seiten_{chunk.first_page}-{chunk.last_page}"
        job(name, leser.extract(chunk.first_page, chunk.last_page))
        plan[-1]["seiten"] = [chunk.first_page, chunk.last_page]
job("dokument_docx", (material / "dokument.docx").read_bytes())
job("seite_html", (material / "seite.html").read_bytes())
job("fehlerantwort", (material / "normale_seite.pdf").read_bytes(), tier="gibt-es-nicht")
(ziel / "plan.json").write_text(json.dumps(plan, indent=1))
print(json.dumps(plan, indent=1))
