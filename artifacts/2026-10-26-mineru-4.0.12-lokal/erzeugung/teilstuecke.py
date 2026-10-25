"""Schneidet eine PDF mit dem Schnitt des Pakets und liest jedes Teilstueck mit echtem MinerU 4.

Aufruf: teilstuecke.py QUELLE ZIELORDNER SEITEN_JE_TEILSTUECK
Je Teilstueck entsteht ZIELORDNER/teil_N/ (die Ausgabe von ParseResult.save samt
markdown_full.md) und ZIELORDNER/teil_N.pdf (die geschnittenen Bytes).
"""
import asyncio
import dataclasses
import json
import pathlib
import sys


def main() -> None:
    from mineru.parser import parse_async
    from mineru.parser.base import ParseResult
    from mineru.parser.writer import FileBasedDataWriter
    from mineru.render.contracts import RenderMode

    from mineru_ocr.chunking import plan_chunks
    from mineru_ocr.config import Settings
    from mineru_ocr.pdf import PdfReader

    quelle, ziel, je = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), int(sys.argv[3])
    ziel.mkdir(parents=True, exist_ok=True)
    daten = quelle.read_bytes()
    einstellung = dataclasses.replace(
        Settings(db_dsn="ungenutzt", endpoint_id=None, api_key=None), max_pages_per_chunk=je
    )
    with PdfReader(daten) as leser:
        plan = plan_chunks(leser.page_count, einstellung)
        stuecke = [(c, leser.extract(c.first_page, c.last_page)) for c in plan]

    for nummer, (chunk, bytes_) in enumerate(stuecke):
        pdf = ziel / f"teil_{nummer}.pdf"
        pdf.write_bytes(bytes_)
        aus = ziel / f"teil_{nummer}"

        async def lesen(pdf: pathlib.Path = pdf):
            return await parse_async(pdf, tier="flash", ocr_mode="txt", image_analysis=False, page_range="")

        ergebnis = asyncio.run(lesen())
        ergebnis.save(FileBasedDataWriter(str(aus)))
        gespeichert = ParseResult.from_json((aus / "middle_json.json").read_text(encoding="utf-8"))
        (aus / "markdown_full.md").write_text(gespeichert.markdown(mode=RenderMode.FULL), encoding="utf-8")
        print(json.dumps({"teil": nummer, "first_page": chunk.first_page, "last_page": chunk.last_page,
                          "bilder": sorted(p.name for p in (aus / "images").iterdir()) if (aus / "images").exists() else []}))


if __name__ == "__main__":
    main()
