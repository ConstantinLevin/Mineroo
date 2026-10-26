"""Ersatz-Antworten des MinerU-4-Workers, gebaut nach den Quellen, die ihre Form festlegen.

WARUM ES DIESE DATEI GIBT: Die echten Antworten des MinerU-4-Endpoints
(`ECHT_ENDPOINT`, unten) decken die gewoehnlichen Lagen ab. Was sich am Endpoint
nicht gezielt hervorrufen laesst — eine fehlende Datei, `degraded`, ein unbekanntes
Archivmitglied, ein Inline-Feld mit falschem Typ —, spielen die Tests mit
Antworten, die hier nach den Quellen gebaut sind, die die Antwortform bestimmen,
nicht nach einer geratenen Form. Jede Bauform nennt ihre Quelle; wo sie von einer
echten Antwort abweicht, gilt die echte.

Die Quellen, auf die sich die Verweise beziehen:

    VERTRAG: der veroeffentlichte Job-Vertrag des Forks
    `ConstantinLevin/mineru-runpod`, `docs/content/docs/reference/api.mdx`
    (Branch `feat/mineru-4`, Stand 516786c).

    WORKER: derselbe Fork, `handler.py` (`_handle_parse` baut die Erfolgsantwort,
    `handler` die Fehlerantwort), `worker/harness.py` (`MANIFEST`: welche Dateien
    in die Antwort gehen) und `worker/parse.py` (`run_mineru`: `ParseResult.save`,
    danach `markdown_full.md`; die Meldung fuer einen nicht lesbaren Typ).

    HARNESS: `runpod_doc_worker` 0.9.1, `transport/archive_build.py`
    (`_build_tarball_bytes`: ein gzip-Tar, jedes Mitglied unter seinem Pfad
    relativ zum Ausgabeverzeichnis, nur regulaere Dateien) und
    `contract/artifacts.py` (Inline-Bilder unter ihrem nackten Dateinamen).

    MINERU: MinerU 4.0.12 (Tag `mineru-4.0.12-released`), `mineru/parser/base.py`
    (`ParseResult.save` schreibt `markdown.md`, `middle_json.json`,
    `structured_content.json`, `images/` und, wenn es Modellausgabe gibt,
    `model_output.json`) und `docs/.../output_files` (Schema des Middle-JSON:
    `schema` `docvortex.middle`, `schema_version` `2.0`, Seiten mit `page_idx`
    und `blocks`, `extensions.mineru` mit `tier` und `parse_mode`).

    DOCVORTEX: DocVortex 0.5.16, die Mindestfassung, die MinerU 4.0.12 verlangt:
    `schema.py` (Blocktypen, Inhalt als Liste von Spans oder Unterbloecken),
    `export/_images.py` (`_register_image`: Bildnamen
    `images/page_{page_idx}_{typ}_{index}.{endung}`, `page_idx` die Seite im
    gelesenen Dokument, ab 0; Verweise in `image_path` und in `<img src>`) und
    `render/_internal/markdown/assets.py` (Markdown-Bild `![](images/…)`,
    `image_source` im strukturierten Inhalt).

GEGEN ECHTE ANTWORTEN GEPRUEFT: Unter `ECHT_ENDPOINT` liegen echte Antworten des
MinerU-4-Endpoints in `advanced` mit OCR. Sie bestaetigen die Dateinamen, das
Schema, die Bildnamen und die Verweisformen; die eine Abweichung (eine Seite ohne
Bloecke traegt keinen `blocks`-Schluessel) ist in `seite` nachgezogen. Wo es eine
echte Antwort gibt, spielen die Tests sie; diese Bauformen bleiben fuer Lagen, die
sich nicht gezielt hervorrufen lassen (ein fehlendes `markdown_full.md`,
`degraded`, ein unbekanntes Archivmitglied).
"""

from __future__ import annotations

import base64
import io
import json
import tarfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

MINERU_VERSION = "4.0.12"

ECHT_ENDPOINT = (
    Path(__file__).resolve().parent.parent / "artifacts" / "2026-10-26-mineru-4.0.12-endpoint"
)
"""ECHTE Antworten des MinerU-4-Endpoints (Herkunft: `README.md` dort).

Abgeschickt mit dem Client des Pakets (`RunPodClient.submit`, `await_result`) an
den Endpoint aus dem Fork `ConstantinLevin/mineru-runpod`, Image `516786ccf`
(`kaputte_pixel`: `46dd9e71c`), MinerU 4.0.12, in `advanced` mit OCR und
Bildanalyse — dem Produktionsweg. Unter
`antworten/` die volle `/status`-Antwort je Job, `plan.json` nennt die Seiten der
Teilstuecke."""


ECHT_BILDANALYSE = ECHT_ENDPOINT.parent / "2026-10-26-mineru-4.0.12-bildanalyse"
"""ECHTE Antwort desselben Endpoints auf eine Seite mit zwei Bildern, die keine
Diagramme sind (Herkunft: `README.md` dort): wo die Bild-Deutungen stehen
(Mineroo#13). Auf demselben Weg wie `ECHT_ENDPOINT` aufgenommen."""


ECHT_GANZDOKUMENTE = ECHT_ENDPOINT.parent / "2026-10-27-ganzdokumente"
"""ECHTE Antworten desselben Endpoints zu Ganzdokument-Formaten (Mineroo#17,
Herkunft: `README.md` dort), abgeschickt ueber `mineru-ocr` mit Daemon, wie der
Betrieb es tut."""


def echte_antwort(name: str, ordner: Path = ECHT_ENDPOINT) -> dict[str, Any]:
    """Die volle `/status`-Antwort eines aufgenommenen Jobs aus `ordner`."""
    geladen: dict[str, Any] = json.loads(
        (ordner / "antworten" / f"{name}.response.json").read_text()
    )
    return geladen


def abgeleitete_antwort(
    name: str,
    *,
    ohne: tuple[str, ...] = (),
    degraded: Any = None,
    middle_json_aendern: Any = None,
) -> dict[str, Any]:
    """Eine ECHTE Antwort, abgeleitet fuer eine Lage, die der Endpoint nicht gezielt liefert.

    Ausgangspunkt ist die Aufnahme `name` unter `ECHT_ENDPOINT`. Geaendert wird
    nur, was die Argumente nennen, alles andere bleibt Byte fuer Byte die echte
    Antwort:

        `ohne`: diese Mitglieder fehlen im Archiv. So liefert der Worker eine
        Datei, die er nicht lesen konnte, beim Transport `tarball_b64` (VERTRAG,
        „Incomplete responses": „the archive ships without that member").

        `degraded`: wird als Feld `degraded` in den Ergebnis-Eintrag gesetzt,
        neben `basename`, `source`, `pages_requested` und `tarball_b64`, in der
        Form des VERTRAGS (`count`, `items` mit `artifact`, `file`, `reason`,
        `error_type`). Die echten Antworten tragen keins (`README.md` dort).

        `middle_json_aendern`: eine Funktion, die das Middle-JSON des Archivs
        bekommt und veraendert zurueckgibt (etwa eine Seite entfernt, wie wenn
        MinerU sie verworfen haette).
    """
    roh = echte_antwort(name)
    eintrag = roh["output"]["results"][0]
    with tarfile.open(
        fileobj=io.BytesIO(base64.b64decode(eintrag["tarball_b64"])), mode="r:*"
    ) as archiv:
        dateien: dict[str, bytes] = {}
        for mitglied in archiv:
            quelle = archiv.extractfile(mitglied) if mitglied.isfile() else None
            if quelle is not None:
                dateien[mitglied.name] = quelle.read()
    for weg in ohne:
        del dateien[weg]
    if middle_json_aendern is not None:
        middle = json.loads(dateien["middle_json.json"])
        dateien["middle_json.json"] = json.dumps(middle_json_aendern(middle)).encode()
    eintrag["tarball_b64"] = tarball_b64(dateien)
    if degraded is not None:
        eintrag["degraded"] = degraded
    return roh


def echte_teilstuecke() -> list[tuple[str, tuple[int, int]]]:
    """Name und Seitenbereich der Teilstueck-Aufnahmen, in Dokumentreihenfolge."""
    plan = json.loads((ECHT_ENDPOINT / "plan.json").read_text())
    return [(job["name"], (job["seiten"][0], job["seiten"][1])) for job in plan if "seiten" in job]


def text_block(index: int, text: str, typ: str = "text") -> dict[str, Any]:
    """Ein Textblock des Middle-JSON: `type`, `index`, `content` als Liste von Spans.

    DOCVORTEX `schema.py`: `InlineContentBlock.content: list[InlineSpan]`, ein
    `TextSpan` ist `{"type": "text", "content": str}`. Dieselbe Form tragen
    `header`, `footer`, `page_number`, `aside_text` (`PageAuxTextBlock`) und
    `page_footnote` (`PageFootnoteBlock`)."""
    return {"type": typ, "index": index, "content": [{"type": "text", "content": text}]}


def bild_block(
    index: int, page_idx: int, *, beschreibung: str = "", endung: str = "jpg"
) -> dict[str, Any]:
    """Ein Bildblock nach `ParseResult.save`: ein `image`-Elternblock, darin der
    `image_body` mit dem Verweis auf seine Datei.

    DOCVORTEX `schema.py`: `ImageBlock.content: list[ImageChildBlock]`, genau ein
    `image_body` mit demselben `index`. `export/_images.py`: Der Body bekommt
    `image_path = images/page_{page_idx}_image_{index}.{endung}`. Die Deutung der
    Bildanalyse steht im `content` des Bodys (VERTRAG, `image_analysis`: „the
    descriptions land in the Markdown behind the image")."""
    return {
        "type": "image",
        "index": index,
        "content": [
            {
                "type": "image_body",
                "index": index,
                "content": beschreibung,
                "image_path": bild_pfad(page_idx, index, endung=endung),
            }
        ],
    }


def bild_pfad(page_idx: int, index: int, *, endung: str = "jpg") -> str:
    """Der Pfad eines Bildes, wie DocVortex ihn vergibt (`_register_image`)."""
    return f"images/page_{page_idx}_image_{index}.{endung}"


def seite(page_idx: int, bloecke: list[dict[str, Any]]) -> dict[str, Any]:
    """Eine Seite des Middle-JSON (DOCVORTEX `PageInfo`: `page_idx`, `blocks`).

    Ohne Bloecke OHNE `blocks`-Schluessel: MinerU schreibt die Voreinstellung
    `[]` nicht mit, gemessen an der echten leeren Seite unter `ECHT_ENDPOINT`."""
    if not bloecke:
        return {"page_idx": page_idx}
    return {"page_idx": page_idx, "blocks": bloecke}


def middle_json(
    seiten: list[dict[str, Any]], *, tier: str = "advanced", parse_mode: str = "ocr"
) -> dict[str, Any]:
    """Das Middle-JSON eines PDF, wie `ParseResult.save` es als `middle_json.json` schreibt.

    MINERU, Abschnitt „Intermediate JSON": `metadata` mit `file_suffix` und
    `producer`, `extensions.mineru` mit `tier` und `parse_mode`, `pages`,
    `is_full_document`, `schema` und `schema_version`. Jedes PDF traegt dazu
    `extensions.docvortex_layout` mit einer Seite je `page_idx`."""
    return {
        "metadata": {
            "file_suffix": "pdf",
            "producer": {"name": "mineru", "version": MINERU_VERSION},
        },
        "extensions": {
            "mineru": {"tier": tier, "parse_mode": parse_mode},
            "docvortex_layout": {
                "version": 1,
                "pages": [
                    {"page_idx": s["page_idx"], "width_pt": 595.0, "height_pt": 842.0}
                    for s in seiten
                ],
            },
        },
        "pages": seiten,
        "is_full_document": True,
        "schema": "docvortex.middle",
        "schema_version": "2.0",
    }


def structured_content(middle: Mapping[str, Any]) -> dict[str, Any]:
    """Der strukturierte Inhalt zum Middle-JSON (`structured_content.json`).

    MINERU, Abschnitt „Structured Content": dieselben Seiten, Bloecke mit ihrem
    Text als Zeichenkette, `metadata` und `extensions`, kein `schema`. Bildbloecke
    tragen ihren Verweis als `image_source` (DOCVORTEX
    `render/_internal/structured_content/renderer.py`)."""
    seiten = []
    for s in middle["pages"]:
        bloecke = []
        for block in s.get("blocks", []):
            if block["type"] == "image":
                body = block["content"][0]
                bloecke.append(
                    {"type": "image", "content": body["content"], "image_source": body["image_path"]}
                )
            else:
                text = "".join(span["content"] for span in block["content"])
                bloecke.append({"type": block["type"], "content": text})
        seiten.append({"page_idx": s["page_idx"], "blocks": bloecke})
    return {
        "pages": seiten,
        "metadata": middle["metadata"],
        "extensions": middle["extensions"],
        "is_full_document": True,
    }


def model_output(seitenzahl: int) -> dict[str, Any]:
    """Die rohe Modellausgabe (`model_output.json`), nur in der Hauptform.

    DOCVORTEX `schema.py`, `ModelJson`: `schema` `docvortex.model`, `pages` als
    Liste von Blocklisten, `page_index_map`."""
    return {
        "schema": "docvortex.model",
        "schema_version": "2.0",
        "pages": [[] for _ in range(seitenzahl)],
        "page_index_map": [],
    }


def markdown_aus(middle: Mapping[str, Any], *, rand: bool = False) -> str:
    """Ein Markdown zum Middle-JSON: Text der Bloecke, Bilder als `![](images/…)`.

    DOCVORTEX `render/_internal/markdown/assets.py`: `build_markdown_image`
    schreibt `![alt](pfad)`; die Deutung steht dahinter. Die Standardfassung
    laesst Randklassen weg, die Vollmodus-Fassung (`rand=True`) nimmt sie mit
    (WORKER `parse.py`, Kommentar zu `markdown_full.md`)."""
    rand_typen = {"header", "footer", "page_number", "aside_text", "page_footnote"}
    teile: list[str] = []
    for s in middle["pages"]:
        for block in s.get("blocks", []):
            if block["type"] == "image":
                body = block["content"][0]
                teile.append(f"![]({body['image_path']})")
                if body["content"]:
                    teile.append(body["content"])
            elif block["type"] in rand_typen and not rand:
                continue
            else:
                teile.append("".join(span["content"] for span in block["content"]))
    return "\n\n".join(teile)


def dokument_dateien(
    middle: dict[str, Any],
    *,
    bilder: Mapping[str, bytes] | None = None,
    mit_model_output: bool = True,
) -> dict[str, bytes]:
    """Die Dateien eines Ausgabeverzeichnisses, wie der Worker es packt.

    MINERU `ParseResult.save`: `markdown.md`, `middle_json.json`,
    `structured_content.json`, `images/…` und `model_output.json`, wenn es
    Modellausgabe gibt. WORKER `parse.py`: dazu `markdown_full.md`. Die Namen
    sind die aus `worker/harness.py` (`MANIFEST`)."""
    dateien = {
        "markdown.md": markdown_aus(middle).encode(),
        "markdown_full.md": markdown_aus(middle, rand=True).encode(),
        "middle_json.json": json.dumps(middle, ensure_ascii=False, indent=2).encode(),
        "structured_content.json": json.dumps(
            structured_content(middle), ensure_ascii=False, indent=2
        ).encode(),
    }
    if mit_model_output:
        dateien["model_output.json"] = json.dumps(model_output(len(middle["pages"]))).encode()
    for pfad, daten in (bilder or {}).items():
        dateien[pfad] = daten
    return dateien


def tarball_b64(dateien: Mapping[str, bytes]) -> str:
    """Das Archiv wie der Harness es baut: gzip-Tar, Mitglieder unter ihrem Pfad
    relativ zum Ausgabeverzeichnis, sortiert, nur regulaere Dateien (HARNESS
    `_build_tarball_bytes`, `_archive_members`)."""
    puffer = io.BytesIO()
    with tarfile.open(fileobj=puffer, mode="w:gz") as archiv:
        for pfad in sorted(dateien):
            daten = dateien[pfad]
            eintrag = tarfile.TarInfo(name=pfad)
            eintrag.size = len(daten)
            archiv.addfile(eintrag, io.BytesIO(daten))
    return base64.b64encode(puffer.getvalue()).decode("ascii")


def antwort(
    eintrag: dict[str, Any],
    *,
    tier: str = "advanced",
    ocr_mode: str = "ocr",
    image_analysis: bool = True,
    mineru_version: str = MINERU_VERSION,
) -> dict[str, Any]:
    """Eine volle `/status`-Antwort mit `COMPLETED` um einen Ergebnis-Eintrag.

    WORKER `_handle_parse`: `ok`, `elapsed_seconds`, `mineru_version`, die drei
    gemeldeten Lese-Einstellungen, `results` mit genau einem Eintrag, `debug`.
    Aussen der Umschlag, den RunPod um die Ausgabe legt (`id`, `status`,
    `delayTime`, `executionTime`, `workerId`), wie in den echten Antworten unter
    `ECHT_ENDPOINT`."""
    return {
        "delayTime": 812,
        "executionTime": 4211,
        "id": "erfundener-job",
        "status": "COMPLETED",
        "workerId": "erfundenerwrk",
        "output": {
            "ok": True,
            "elapsed_seconds": 4.2,
            "mineru_version": mineru_version,
            "tier": tier,
            "ocr_mode": ocr_mode,
            "image_analysis": image_analysis,
            "results": [eintrag],
            "debug": {
                "tier": tier,
                "model_dir": None,
                "gpu": {"available": True, "name": "NVIDIA RTX 4090"},
                "phase_ms": {"fetch_input": 12, "mineru_parse": 18420, "package": 95},
            },
        },
    }


def tarball_eintrag(dateien: Mapping[str, bytes]) -> dict[str, Any]:
    """Der Ergebnis-Eintrag beim Transport `tarball_b64` (VERTRAG, „Per-entry keys")."""
    return {
        "basename": "doc",
        "source": "b64",
        "pages_requested": -1,
        "tarball_b64": tarball_b64(dateien),
    }


def tarball_antwort(dateien: Mapping[str, bytes], **einstellungen: Any) -> dict[str, Any]:
    """Eine volle Antwort mit Tarball um die gegebenen Dateien."""
    return antwort(tarball_eintrag(dateien), **einstellungen)


def fertige_antwort(
    text: str = "Text", *, page_idx: Any = 0, seiten: int = 1, **gemeldet: Any
) -> dict[str, Any]:
    """Die gewoehnliche Erfolgsantwort fuer Tests, die nur einen Text brauchen:
    `seiten` Seiten, auf der ersten ein Textblock mit `text` (ohne Text: Seiten
    ohne Block). `page_idx` ist fuer Tests, die eine kaputte Seitenangabe
    brauchen; es gilt fuer die erste Seite, die weiteren zaehlen ab 1.
    `gemeldet` setzt, was der Worker meldet (`tier`, `ocr_mode`,
    `image_analysis`, `mineru_version`, siehe `antwort`).

    `seiten` muss zu dem passen, was der Test schickt: Nach jedem Teilstueck
    prueft der Executor, ob MinerU so viele Seiten verarbeitet hat, wie das
    Paket geschickt hat (Mineroo#12)."""
    erste = seite(page_idx, [text_block(0, text)] if text else [])
    return tarball_antwort(
        dokument_dateien(middle_json([erste] + [seite(nr, []) for nr in range(1, seiten)])),
        **gemeldet,
    )


def fehlerantwort(fehler: str) -> dict[str, Any]:
    """Eine `FAILED`-Antwort des Workers.

    WORKER `handler`: Die Fehlerantwort traegt `error` (Typname und Meldung),
    `ok: false`, `elapsed_seconds`, `mineru_version`, `traceback` und `debug`.
    RunPod hebt `error` auf die oberste Ebene der `/status`-Antwort und legt den
    Rest unter `output`, wie es die echte Fehlerantwort unter `ECHT_ENDPOINT`
    zeigt."""
    return {
        "delayTime": 176874,
        "error": fehler,
        "executionTime": 515,
        "id": "erfundener-job",
        "output": {
            "debug": {"gpu": {"available": True}, "model_dir": None, "phase_ms": {}},
            "elapsed_seconds": 0.1,
            "mineru_version": MINERU_VERSION,
            "ok": False,
            "traceback": f"Traceback (most recent call last):\n  ...\n{fehler}\n",
        },
        "status": "FAILED",
        "workerId": "erfundenerwrk",
    }
