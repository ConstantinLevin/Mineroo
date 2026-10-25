"""Erzeugt ECHTE MinerU-4.0.12-Ausgabe lokal: Stufe flash, OCR-Modus txt (DocVortex nativ, ohne Modelle).

Aufruf: erzeugen.py QUELLE ZIEL [SEITENBEREICH]. Schreibt mit `ParseResult.save`,
wie der Worker es tut, und rendert danach `markdown_full.md` im Vollmodus
(`RenderMode.FULL`), wie `worker/parse.py` es tut."""
import asyncio, os, pathlib, sys


def main() -> None:
    from mineru.parser import parse_async
    from mineru.parser.base import ParseResult
    from mineru.parser.writer import FileBasedDataWriter
    from mineru.render.contracts import RenderMode

    quelle, ziel = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
    bereich = sys.argv[3] if len(sys.argv) > 3 else ""

    async def lesen():
        return await parse_async(quelle, tier="flash", ocr_mode="txt", image_analysis=False, page_range=bereich)

    ergebnis = asyncio.run(lesen())
    ziel.mkdir(parents=True, exist_ok=True)
    ergebnis.save(FileBasedDataWriter(str(ziel)))
    gespeichert = ParseResult.from_json((ziel / "middle_json.json").read_text(encoding="utf-8"))
    (ziel / "markdown_full.md").write_text(gespeichert.markdown(mode=RenderMode.FULL), encoding="utf-8")
    print("fertig", sorted(p.relative_to(ziel).as_posix() for p in ziel.rglob("*") if p.is_file()))


if __name__ == "__main__":
    main()
