"""Auspacken und Zusammensetzen der Antworten des MinerU-4-Workers.

Gespielt werden die ECHTEN Antworten des MinerU-4-Endpoints
(`artifacts/2026-10-26-mineru-4.0.12-endpoint/`, `advanced` mit OCR) und, fuer
Lagen, die sich nicht gezielt hervorrufen lassen, Ersatz-Antworten aus
`ersatz_antworten`, gebaut nach den Quellen, die die Antwortform festlegen
(Job-Vertrag des Forks, Code des Workers und des Harness, MinerU 4.0.12,
DocVortex 0.5.16; die Verweise stehen dort an jeder Bauform).
"""

from __future__ import annotations

import base64
import copy
import dataclasses
import io
import json
import random
import re
import tarfile
import time
from typing import Any

import pytest

import ersatz_antworten as ersatz
from mineru_ocr.assembly import (
    ChunkArtifacts,
    assemble,
    is_empty,
    rendered_markdown,
    unpack_response,
)
from mineru_ocr.chunking import Chunk
from mineru_ocr.errors import IncompleteResultError, TerminalRunPodError


def _ein_dokument() -> tuple[dict[str, Any], dict[str, bytes]]:
    """Eine Seite mit Kopfzeile, Text und Bild, und die Dateien des Workers dazu."""
    middle = ersatz.middle_json(
        [
            ersatz.seite(
                0,
                [
                    ersatz.text_block(0, "Geschaeftsbericht 2025", typ="header"),
                    ersatz.text_block(1, "Der Umsatz stieg."),
                    ersatz.bild_block(2, 0, beschreibung="Ein Balkendiagramm."),
                ],
            )
        ]
    )
    bilder = {ersatz.bild_pfad(0, 2): b"\xff\xd8\xffbild"}
    return middle, ersatz.dokument_dateien(middle, bilder=bilder)


# --- Auspacken ------------------------------------------------------------


def test_tarball_wird_in_mineru4_dateien_ausgepackt() -> None:
    """Jede Datei der Antwort landet in ihrem eigenen Feld, die JSON-Dateien als
    Struktur und nicht als Base64 (Mineroo#11, „Ablage in raw")."""
    middle, dateien = _ein_dokument()
    teil = unpack_response(ersatz.tarball_antwort(dateien), Chunk(0, 0))

    assert teil.markdown == dateien["markdown.md"].decode()
    assert teil.markdown_full == dateien["markdown_full.md"].decode()
    assert teil.middle_json == middle
    assert teil.structured_content == json.loads(dateien["structured_content.json"])
    assert teil.model_output == json.loads(dateien["model_output.json"])
    assert teil.images == {"page_0_image_2.jpg": b"\xff\xd8\xffbild"}


def test_inline_antwort_wird_in_dieselben_felder_ausgepackt() -> None:
    """Beim Inline-Transport stehen dieselben Dateien als Schluessel des Eintrags
    (Job-Vertrag, Tabelle unter „When `transport: "inline"`"); die Bilder als
    `{dateiname: base64}` (Harness, `kind="b64map"`)."""
    middle, dateien = _ein_dokument()
    eintrag = {
        "basename": "doc",
        "source": "b64",
        "pages_requested": -1,
        "markdown": dateien["markdown.md"].decode(),
        "markdown_full": dateien["markdown_full.md"].decode(),
        "middle_json": middle,
        "structured_content": json.loads(dateien["structured_content.json"]),
        "model_output": {},
        "images": {"page_0_image_2.jpg": base64.b64encode(b"\xff\xd8\xffbild").decode()},
    }
    teil = unpack_response(ersatz.antwort(eintrag), Chunk(0, 0))

    assert teil.markdown == dateien["markdown.md"].decode()
    assert teil.markdown_full == dateien["markdown_full.md"].decode()
    assert teil.middle_json == middle
    assert teil.model_output == {}
    assert teil.images == {"page_0_image_2.jpg": b"\xff\xd8\xffbild"}
    assert teil.extras == {"basename": "doc", "source": "b64", "pages_requested": -1}


def test_fehlende_dateien_sind_none_und_nicht_leer() -> None:
    """Der Worker laesst `model_output.json` weg, wenn MinerU keine Modellausgabe
    hat, und `markdown_full.md`, wenn er es nicht rendern kann (Job-Vertrag).
    „Nicht da" ist dann None; ein leeres Feld behauptete eine leere Datei."""
    middle, dateien = _ein_dokument()
    del dateien["model_output.json"], dateien["markdown_full.md"]
    teil = unpack_response(ersatz.tarball_antwort(dateien), Chunk(0, 0))

    assert teil.model_output is None
    assert teil.markdown_full is None
    assert teil.middle_json == middle


def test_unbekanntes_und_unlesbares_geht_nach_extras() -> None:
    """Die Abruf-Regel: Ein Archivmitglied mit unbekanntem Namen und eine
    bekannte Datei in unerwarteter Form gehen nicht verloren, sondern stehen als
    Base64 unter ihrem Pfad in `extras`; die Felder des Eintrags neben dem
    Archiv ebenso, `degraded` eingeschlossen (Job-Vertrag, „Incomplete
    responses")."""
    _, dateien = _ein_dokument()
    dateien["layout.pdf"] = b"%PDF-neu"
    dateien["structured_content.json"] = b'{"abgeschnitten": '
    eintrag = ersatz.tarball_eintrag(dateien)
    eintrag["degraded"] = {"count": 1, "items": [{"artifact": None, "file": "x.png"}]}
    teil = unpack_response(ersatz.antwort(eintrag), Chunk(0, 0))

    assert base64.b64decode(teil.extras["layout.pdf"]) == b"%PDF-neu"
    assert base64.b64decode(teil.extras["structured_content.json"]) == b'{"abgeschnitten": '
    assert teil.structured_content is None
    assert teil.extras["degraded"] == eintrag["degraded"]
    assert teil.extras["basename"] == "doc"
    assert "tarball_b64" not in teil.extras


def test_ohne_markdown_ist_die_antwort_unverwertbar() -> None:
    """Ohne `markdown.md` laesst der Worker den Job scheitern; eine erfolgreiche
    Antwort ohne es hat eine andere Form als vereinbart und wird nicht still als
    leeres Dokument gelesen."""
    _, dateien = _ein_dokument()
    del dateien["markdown.md"]
    with pytest.raises(TerminalRunPodError, match="markdown.md"):
        unpack_response(ersatz.tarball_antwort(dateien), Chunk(0, 0))


def test_umschlag_traegt_version_und_gemeldete_einstellungen() -> None:
    """Was die Antwort ausser dem Inhalt trug, steht im Umschlag: die gemeldete
    MinerU-Version und die Lese-Einstellungen, wie MinerU sie vermerkt hat."""
    _, dateien = _ein_dokument()
    teil = unpack_response(
        ersatz.tarball_antwort(dateien, tier="basic", image_analysis=False), Chunk(0, 0)
    )
    assert teil.envelope["id"] == "erfundener-job"
    meta = teil.envelope["output_meta"]
    assert meta["mineru_version"] == "4.0.12"
    assert (meta["tier"], meta["ocr_mode"], meta["image_analysis"]) == ("basic", "ocr", False)
    assert "results" not in meta


@pytest.mark.parametrize(
    "antwort",
    [
        {"status": "COMPLETED"},
        {"status": "COMPLETED", "output": {"ok": True, "results": []}},
        {"status": "COMPLETED", "output": {"ok": True, "results": [{}, {}]}},
        {"status": "COMPLETED", "output": {"ok": True, "results": [{"tarball_b64": "kein base64!"}]}},
    ],
)
def test_antwort_in_fremder_form_wirft_endgueltig(antwort: dict[str, Any]) -> None:
    """Kein `output`, keine oder mehrere Ergebnisse, ein unlesbares Archiv: Ein
    zweiter Versuch kaufte dieselbe Antwort."""
    with pytest.raises(TerminalRunPodError):
        unpack_response(antwort, Chunk(0, 0))


def test_rohantwort_bleibt_unveraendert() -> None:
    """Auspacken liest nur."""
    _, dateien = _ein_dokument()
    antwort = ersatz.tarball_antwort(dateien)
    vorher = copy.deepcopy(antwort)
    unpack_response(antwort, Chunk(0, 0))
    assert antwort == vorher


def test_ein_doppeltes_archivmitglied_geht_nicht_verloren() -> None:
    """Steht ein Name zweimal im Archiv, gilt das erste Mitglied; das zweite steht
    mit einer Nummer am Pfad in `extras`, statt das erste zu ueberschreiben oder
    selbst zu verschwinden."""
    middle, dateien = _ein_dokument()
    archiv = io.BytesIO()
    with tarfile.open(fileobj=archiv, mode="w:gz") as tar:
        doppelte = [
            ("middle_json.json", b'{"zweites": true}'),
            ("notiz.txt", b"eins"),
            ("notiz.txt", b"zwei"),
        ]
        for pfad, daten in [*dateien.items(), *doppelte]:
            eintrag = tarfile.TarInfo(name=pfad)
            eintrag.size = len(daten)
            tar.addfile(eintrag, io.BytesIO(daten))
    eintrag_ergebnis = {"basename": "doc", "tarball_b64": base64.b64encode(archiv.getvalue()).decode()}
    teil = unpack_response(ersatz.antwort(eintrag_ergebnis), Chunk(0, 0))

    assert teil.middle_json == middle
    assert base64.b64decode(teil.extras["middle_json.json"]) == b'{"zweites": true}'
    assert base64.b64decode(teil.extras["notiz.txt"]) == b"eins"
    assert base64.b64decode(teil.extras["notiz.txt#2"]) == b"zwei"


def test_ein_inline_feld_mit_falschem_typ_geht_nach_extras() -> None:
    """JSON-`null` an Stelle einer Datei ist keine Datei: Das Feld ist None, und
    der Wert steht unter seinem Namen in `extras`."""
    _, dateien = _ein_dokument()
    eintrag: dict[str, Any] = {
        "markdown": dateien["markdown.md"].decode(),
        "middle_json": None,
        "images": {},
    }
    teil = unpack_response(ersatz.antwort(eintrag), Chunk(0, 0))
    assert teil.middle_json is None
    assert teil.extras == {"middle_json": None}


def test_ein_inline_bild_mit_ungueltigem_base64_wirft_endgueltig() -> None:
    """Ein Bild, dessen Wert nicht reines Base64 ist (hier mit einem fremden
    Zeichen), wird nicht still zu anderen Bytes dekodiert; ohne die strenge
    Pruefung wuerde `*` einfach uebergangen. Die Antwort ist unverwertbar, ein
    zweiter Versuch kaufte dieselbe."""
    _, dateien = _ein_dokument()
    eintrag: dict[str, Any] = {
        "markdown": dateien["markdown.md"].decode(),
        "images": {"page_0_image_2.jpg": "QUJD*"},
    }
    with pytest.raises(TerminalRunPodError, match="page_0_image_2.jpg"):
        unpack_response(ersatz.antwort(eintrag), Chunk(0, 0))


def test_ein_unlesbarer_text_im_archiv_geht_nach_extras() -> None:
    """Eine Textdatei, die kein UTF-8 ist, wird nicht mit Ersatzzeichen
    gelesen: Sie fehlt in ihrem Feld (None) und steht Byte fuer Byte in
    `extras`."""
    _, dateien = _ein_dokument()
    kaputt = b"Fu\xdfzeile \xff\xfe"
    teil = unpack_response(
        ersatz.tarball_antwort({**dateien, "markdown_full.md": kaputt}), Chunk(0, 0)
    )
    assert teil.markdown_full is None
    assert base64.b64decode(teil.extras["markdown_full.md"]) == kaputt


# --- Zusammensetzen -------------------------------------------------------


def _teil_aus(
    chunk: Chunk, seiten: list[dict[str, Any]], bilder: dict[str, bytes] | None = None
) -> ChunkArtifacts:
    """Ein Teilstueck, wie das Auspacken es aus der Antwort des Workers macht."""
    dateien = ersatz.dokument_dateien(ersatz.middle_json(seiten), bilder=bilder)
    return unpack_response(ersatz.tarball_antwort(dateien), chunk)


def _zwei_teilstuecke() -> list[ChunkArtifacts]:
    """Ein dreiseitiges Dokument in zwei Teilstuecken, jedes mit einem Bild auf
    seiner ersten Seite. Jedes Teilstueck ist eine eigene Datei und zaehlt ab
    Seite 0; beide Bilder heissen deshalb `images/page_0_image_1.jpg`
    (DocVortex `_register_image`, Job-Vertrag zu `images`: „Separate files cut
    from one document by the caller each start at page `0` and do reuse
    them")."""
    return [
        _teil_aus(
            Chunk(0, 1),
            [
                ersatz.seite(0, [ersatz.text_block(0, "Seite eins"), ersatz.bild_block(1, 0)]),
                ersatz.seite(1, [ersatz.text_block(0, "Seite zwei")]),
            ],
            {ersatz.bild_pfad(0, 1): b"bild aus teilstueck 0"},
        ),
        _teil_aus(
            Chunk(2, 2),
            [ersatz.seite(0, [ersatz.text_block(0, "Seite drei"), ersatz.bild_block(1, 0)])],
            {ersatz.bild_pfad(0, 1): b"bild aus teilstueck 1"},
        ),
    ]


def test_seitenversatz_kommt_aus_den_seitenindizes() -> None:
    """Jedes `page_idx` des Teilstuecks wird um dessen erste Seite verschoben, im
    Middle-JSON (Seiten und `docvortex_layout`) und im strukturierten Inhalt.
    Danach zaehlen sie die Seiten des Originals, wie der Job-Vertrag es fuer
    `page_idx` sagt („refer to the original document")."""
    ergebnis = assemble(_zwei_teilstuecke())

    def seiten(datei: dict[str, Any]) -> list[int]:
        return [s["page_idx"] for s in datei["pages"]]

    assert [seiten(m) for m in ergebnis["middle_json"]] == [[0, 1], [2]]
    assert [seiten(m["extensions"]["docvortex_layout"]) for m in ergebnis["middle_json"]] == [
        [0, 1],
        [2],
    ]
    assert [seiten(s) for s in ergebnis["structured_content"]] == [[0, 1], [2]]


@pytest.mark.parametrize("page_idx", [0.5, True])
def test_nicht_ganzzahliges_page_idx_wirft(page_idx: Any) -> None:
    """Ein Versatz auf einer Seitenangabe, die keine ganze Zahl ist, waere eine
    falsche Seitenzahl, die niemand mehr als falsch erkennt. `True` ist in Python
    eine ganze Zahl, aber keine Seitenangabe: Der Versatz ergaebe Seite 1 statt
    eines Fehlers."""
    teil = _teil_aus(Chunk(3, 3), [ersatz.seite(0, [])])
    assert teil.middle_json is not None
    teil.middle_json["pages"][0]["page_idx"] = page_idx
    with pytest.raises(TerminalRunPodError, match="page_idx"):
        assemble([teil])


def _bild_unter(ergebnis: dict[str, Any], verweis: str) -> bytes:
    return base64.b64decode(ergebnis["images"][verweis])


def test_bilder_bekommen_je_teilstueck_einen_eigenen_namensraum() -> None:
    """Gleich benannte Bilder zweier Teilstuecke ueberschreiben sich nicht, und
    jeder Verweis zeigt danach auf das Bild seines eigenen Teilstuecks: im
    Markdown, im `image_path` des Middle-JSON und im `image_source` des
    strukturierten Inhalts (Mineroo#11; Story 24)."""
    ergebnis = assemble(_zwei_teilstuecke())

    assert len(ergebnis["images"]) == 2
    for nummer, middle in enumerate(ergebnis["middle_json"]):
        body = middle["pages"][0]["blocks"][1]["content"][0]
        assert _bild_unter(ergebnis, body["image_path"]) == f"bild aus teilstueck {nummer}".encode()
        quelle = ergebnis["structured_content"][nummer]["pages"][0]["blocks"][1]["image_source"]
        assert quelle == body["image_path"]

    verweise = re.findall(r"!\[\]\(([^)]+)\)", ergebnis["markdown"])
    assert [_bild_unter(ergebnis, v) for v in verweise] == [
        b"bild aus teilstueck 0",
        b"bild aus teilstueck 1",
    ]
    assert re.findall(r"!\[\]\(([^)]+)\)", ergebnis["markdown_full"]) == verweise


@pytest.mark.parametrize("src", ['src="{}"', "src='{}'", "src={}", "SRC={}", "src = {}"])
def test_verweise_im_html_einer_tabelle_werden_mit_umbenannt(src: str) -> None:
    """Bilder in visuellem HTML (eine Tabelle mit eingebettetem Bild) stehen als
    `<img src="images/…">` im `content` des `table_body` (DocVortex
    `_materialize_markup`, Namen `page_{p}_table_image_{index}_{ordinal}`).
    DocVortex behaelt dabei die Schreibweise des HTML, das das Modell geschrieben
    hat (Anfuehrungszeichen, Gross- und Kleinschreibung, Leerraum um `=`); jede
    dieser Formen ist ein Verweis."""
    attribut = src.format("images/page_0_table_image_0_1.png")
    tabelle = {
        "type": "table",
        "index": 0,
        "content": [
            {
                "type": "table_body",
                "index": 0,
                "content": f"<table><tr><td><img {attribut}></td></tr></table>",
            }
        ],
    }
    teile = [
        _teil_aus(Chunk(0, 0), [ersatz.seite(0, [tabelle])], {"images/page_0_table_image_0_1.png": b"a"}),
        _teil_aus(Chunk(1, 1), [ersatz.seite(0, [tabelle])], {"images/page_0_table_image_0_1.png": b"b"}),
    ]
    ergebnis = assemble(teile)

    for nummer, erwartet in enumerate([b"a", b"b"]):
        html = ergebnis["middle_json"][nummer]["pages"][0]["blocks"][0]["content"][0]["content"]
        (verweis,) = re.findall(r"""(?i)src\s*=\s*["']?([^"'\s>]+)""", html)
        assert verweis in ergebnis["images"]
        assert _bild_unter(ergebnis, verweis) == erwartet


def _html_nach_zusammensetzen(inhalt: str) -> str:
    """Das HTML eines Tabellenblocks nach dem Zusammensetzen. Die Seite traegt
    dazu ein eigenes Bild (`images/page_0_image_1.jpg`), damit die Verweise
    ueberhaupt umbenannt werden."""
    tabelle = {
        "type": "table",
        "index": 0,
        "content": [{"type": "table_body", "index": 0, "content": inhalt}],
    }
    teil = _teil_aus(
        Chunk(0, 0),
        [ersatz.seite(0, [tabelle, ersatz.bild_block(1, 0)])],
        {ersatz.bild_pfad(0, 1): b"eigenes"},
    )
    html: str = assemble([teil])["middle_json"][0]["pages"][0]["blocks"][0]["content"][0]["content"]
    return html


@pytest.mark.parametrize(
    "inhalt",
    [
        "<img" + " " * 40_000 + ">",
        "Text <img alt=x weiter\n" * 4_000,
    ],
    ids=["viel_leerraum", "ohne_schluss"],
)
def test_ein_entgleistes_img_haelt_das_zusammensetzen_nicht_auf(inhalt: str) -> None:
    """Ein `<img` mit sehr viel Leerraum oder viele `<img` ohne schliessendes `>`
    (entgleiste Modellausgabe, OCR-Text) setzt sich in linearer Zeit zusammen.
    Das Zusammensetzen laeuft im Daemon synchron; eine Rueckwaertssuche in
    quadratischer Zeit hielte alle Jobs und den Herzschlag sekundenlang an."""
    beginn = time.perf_counter()
    html = _html_nach_zusammensetzen(inhalt)
    assert time.perf_counter() - beginn < 1.0
    assert html == inhalt


def test_eine_html_quelle_wird_nur_als_ganzes_umbenannt() -> None:
    """Eine HTML-Quelle, die mit dem Pfad eines eigenen Bildes beginnt, aber mehr
    traegt (`images/page_0_image_1.jpg?x=1`), ist nicht der Name, unter dem
    DocVortex das Bild ablegt, und bleibt stehen."""
    inhalt = f'<img src="{ersatz.bild_pfad(0, 1)}?x=1">'
    assert _html_nach_zusammensetzen(inhalt) == inhalt


@pytest.mark.parametrize("alt", ["a<b", "a<imgx"])
def test_ein_kleiner_als_vor_src_beendet_das_tag_nicht(alt: str) -> None:
    """Ein `<` ausserhalb von Anfuehrungszeichen, das kein neues `<img` beginnt
    (`alt=a<b`, auch `alt=a<imgx`), gehoert noch zum Tag. DocVortex legt das
    Bild dieses Tags ab, also ist seine Quelle ein Verweis und wird umbenannt."""
    inhalt = f'<img alt={alt} src="{ersatz.bild_pfad(0, 1)}">'
    assert (
        _html_nach_zusammensetzen(inhalt)
        == f'<img alt={alt} src="images/teilstueck_0/page_0_image_1.jpg">'
    )


_DOCVORTEX_HTML_BILDQUELLE = re.compile(
    r"(?P<prefix><img\b(?:[^>\"']|\"[^\"]*\"|'[^']*')*?\s+src\s*=\s*)"
    r"(?:(?P<quote>[\"'])(?P<quoted>.*?)(?P=quote)|(?P<unquoted>[^\s>]+))",
    re.IGNORECASE | re.DOTALL,
)
"""`_HTML_IMAGE_SOURCE_RE` aus DocVortex 0.5.16 (`export/_images.py`), woertlich.
Wechselt der Worker auf eine andere DocVortex-Version, wird die Kopie mitgezogen."""


def test_html_quellen_werden_umbenannt_wie_docvortex_sie_findet() -> None:
    """Auf zufaelligen Bruchstuecken aus `<img`, `src=`, `<`, `>` und Leerraum
    (ohne Anfuehrungszeichen, ohne `(` und nie mit dem Pfad am Anfang, damit nur
    die HTML-Regel greift) wird genau die Quelle umbenannt, die DocVortex als
    Bild ablegt. Die Saat ist fest; die Grenze mit Anfuehrungszeichen nennt der
    Docstring von `_HTML_BILDQUELLE`."""
    alt = ersatz.bild_pfad(0, 1)
    neu = "images/teilstueck_0/page_0_image_1.jpg"
    bausteine = [
        "<img", "<IMG", "<imgx", "<im", " ", "  ", "\n", "src", "SRC", "src=", "src =",
        "=", ">", "<", "<b", "a", alt, "alt=", "<img src=", "<IMG SRC=",
    ]

    def wie_docvortex(treffer: re.Match[str]) -> str:
        quelle = treffer.group("unquoted")
        return f"{treffer.group('prefix')}{neu if quelle == alt else quelle}"

    zufall = random.Random(11)
    for _ in range(2_000):
        inhalt = "x" + "".join(zufall.choice(bausteine) for _ in range(zufall.randint(1, 30)))
        erwartet = _DOCVORTEX_HTML_BILDQUELLE.sub(wie_docvortex, inhalt)
        assert _html_nach_zusammensetzen(inhalt) == erwartet, inhalt


def test_ein_fremder_verweis_mit_demselben_pfadende_bleibt_stehen() -> None:
    """Externe Bilder in visuellem HTML behalten ihre URL (DocVortex
    `_materialize_markup`); endet eine solche URL zufaellig auf den Pfad eines
    eigenen Bildes, wird sie nicht umgebogen."""
    fremd = f"https://example.org/{ersatz.bild_pfad(0, 1)}"
    assert _html_nach_zusammensetzen(f'<img src="{fremd}">') == f'<img src="{fremd}">'


@pytest.mark.parametrize(
    "vorlage",
    [
        '<img src="https://example.org/render?path={bild}">',
        '<img src="https://example.org/render?src={bild}">',
        "<img src=\"https://example.org/render?img='{bild}'\">",
        "![](https://example.org/render?src={bild})",
    ],
)
def test_ein_bildpfad_als_parameter_einer_fremden_url_bleibt_stehen(vorlage: str) -> None:
    """Umbenannt wird nur ein ganzes lokales Bildziel. Eine externe URL, die den
    Pfad eines eigenen Bildes als Parameter traegt, ist kein Verweis auf dieses
    Bild und geht unveraendert weiter; sonst fragte ihr Server nach einem
    anderen Pfad. Das gilt auch, wenn der Parameter selbst `src` heisst oder den
    Pfad in Anfuehrungszeichen setzt."""
    inhalt = vorlage.format(bild=ersatz.bild_pfad(0, 1))
    assert _html_nach_zusammensetzen(inhalt) == inhalt


def test_ein_name_ist_kein_praefix_eines_anderen() -> None:
    """`images/page_0_image_1.jpg` und `images/page_0_image_10.jpg` sind zwei
    Bilder; das Umbenennen des einen trifft den Verweis auf das andere nicht."""
    seiten = [
        ersatz.seite(0, [ersatz.bild_block(1, 0), ersatz.bild_block(10, 0)]),
    ]
    teil = _teil_aus(
        Chunk(0, 0),
        seiten,
        {ersatz.bild_pfad(0, 1): b"eins", ersatz.bild_pfad(0, 10): b"zehn"},
    )
    ergebnis = assemble([teil])

    verweise = re.findall(r"!\[\]\(([^)]+)\)", ergebnis["markdown"])
    assert [_bild_unter(ergebnis, v) for v in verweise] == [b"eins", b"zehn"]


def test_markdown_in_dokumentreihenfolge_und_vollfassung_nur_ganz() -> None:
    """Die Texte stehen in der Reihenfolge der Teilstuecke. Fehlt die
    Vollmodus-Fassung in einem Teilstueck, steht keine da: Ein Text, dem ein
    Teilstueck fehlt, saehe vollstaendig aus."""
    teile = _zwei_teilstuecke()
    ergebnis = assemble(teile)
    assert ergebnis["markdown"].index("Seite eins") < ergebnis["markdown"].index("Seite drei")
    assert ergebnis["markdown_full"] is not None
    assert "Seite drei" in ergebnis["markdown_full"]

    ohne = dataclasses.replace(teile[1], markdown_full=None)
    assert assemble([teile[0], ohne])["markdown_full"] is None


def test_fehlt_die_vollfassung_in_einem_teilstueck_bleiben_die_anderen_erhalten() -> None:
    """Die Abruf-Regel: Ohne zusammengesetzte Vollfassung steht die bezahlte
    Vollfassung jedes Teilstuecks, das sie trug, trotzdem in `raw`, an ihrer
    Stelle in der Reihenfolge der Teilstuecke."""
    teile = _zwei_teilstuecke()
    ohne = dataclasses.replace(teile[1], markdown_full=None)
    ergebnis = assemble([teile[0], ohne])

    erste = ergebnis["markdown_full_je_teilstueck"][0]
    assert erste is not None and "Seite eins" in erste
    assert ergebnis["markdown_full_je_teilstueck"][1] is None
    # Die Verweise darin zeigen wie ueberall auf das eigene Bild.
    (verweis,) = re.findall(r"!\[\]\(([^)]+)\)", erste)
    assert _bild_unter(ergebnis, verweis) == b"bild aus teilstueck 0"
    # Ist die Vollfassung ganz, steht sie nur einmal da; traegt sie kein
    # Teilstueck, gibt es nichts aufzuheben.
    assert "markdown_full_je_teilstueck" not in assemble(teile)
    keins = [dataclasses.replace(teil, markdown_full=None) for teil in teile]
    assert "markdown_full_je_teilstueck" not in assemble(keins)


def test_jede_datei_steht_je_teilstueck_und_als_json() -> None:
    """`middle_json`, `structured_content`, `model_output`, `envelopes` und
    `extras` tragen je Teilstueck einen Eintrag, und das Ganze ist JSON-faehig
    (es geht unveraendert in eine `jsonb`-Spalte)."""
    teile = _zwei_teilstuecke()
    ergebnis = assemble(teile)
    for schluessel in ("middle_json", "structured_content", "model_output", "envelopes", "extras"):
        assert len(ergebnis[schluessel]) == 2, schluessel
    assert ergebnis["model_output"] == [teil.model_output for teil in teile]
    assert ergebnis["extras"][1] == teile[1].extras
    assert json.loads(json.dumps(ergebnis)) == ergebnis


def test_zusammensetzen_veraendert_die_teilstuecke_nicht() -> None:
    teile = _zwei_teilstuecke()
    vorher = copy.deepcopy(teile)
    assemble(teile)
    assert teile == vorher


# --- Leer oder nicht ------------------------------------------------------


def _ergebnis(seiten: list[dict[str, Any]], bilder: dict[str, bytes] | None = None) -> dict[str, Any]:
    return assemble([_teil_aus(Chunk(0, len(seiten) - 1), seiten, bilder)])


def test_leere_seite_ist_empty() -> None:
    """Eine Seite ohne einen einzigen Block, ein leeres Markdown, keine Bilder:
    `empty`, ein gueltiges Ergebnis (Story 25).

    Die echte leere Seite traegt keinen `blocks`-Schluessel (siehe
    `test_die_echte_leere_seite_ist_empty`); diese Ersatz-Seite nimmt dieselbe
    Form an."""
    assert is_empty(_ergebnis([ersatz.seite(0, [])]))


def test_ein_randblock_allein_ist_nicht_leer() -> None:
    """Ein Dokument, von dem nur die Kopfzeile erkannt wurde, traegt womoeglich
    die wertvollste Kennung des Ganzen (`rendered_markdown`); als `empty`
    verbucht wuerde es nie wieder angesehen."""
    ergebnis = _ergebnis([ersatz.seite(0, [ersatz.text_block(0, "Az. 12/34", typ="header")])])
    ergebnis["markdown"] = ""
    ergebnis["markdown_full"] = ""
    assert not is_empty(ergebnis)


def test_im_zweifel_nicht_leer() -> None:
    """Ein Block ohne Text, ein Bild, auf das nichts verweist, oder ein
    fehlendes Middle-JSON: nicht leer. Ein faelschlich als fertig verbuchtes
    leeres Dokument faellt beim Ansehen auf, ein faelschlich als leer
    verbuchtes volles nie."""
    leer = _ergebnis([ersatz.seite(0, [])])

    ohne_text = _ergebnis([ersatz.seite(0, [{"type": "equation", "index": 0, "content": ""}])])
    ohne_text["markdown"] = ""
    ohne_text["markdown_full"] = ""
    assert not is_empty(ohne_text)

    assert not is_empty({**leer, "images": {"images/teilstueck_0/x.jpg": "AAAA"}})
    assert not is_empty({**leer, "middle_json": [None]})
    assert not is_empty({**leer, "markdown_full": "Fusszeile"})


@pytest.mark.parametrize("aufnahme", ["leere_seite_bild", "leere_seite_pdf"])
def test_die_echte_leere_seite_ist_empty(aufnahme: str) -> None:
    """Die ECHTE Antwort des Endpoints fuer eine leere A4-Seite, als PNG und als
    PDF, in `advanced` mit OCR (`artifacts/2026-10-26-mineru-4.0.12-endpoint/`):
    leeres Markdown, keine Bilder, und im Middle-JSON die Seite
    `{"page_idx": 0}` OHNE `blocks`-Schluessel. Das ist `empty` (Story 25), und
    die Darstellung ist der leere Text, kein Fehler."""
    ergebnis = assemble([unpack_response(ersatz.echte_antwort(aufnahme), Chunk(0, 0))])

    assert ergebnis["middle_json"][0]["pages"] == [{"page_idx": 0}]
    assert ergebnis["middle_json"][0]["extensions"]["mineru"] == {"tier": "advanced", "parse_mode": "ocr"}
    assert is_empty(ergebnis)
    assert rendered_markdown(ergebnis) == ""


def test_ein_leeres_dokument_aus_mehreren_teilstuecken_ist_empty() -> None:
    """Zwei leere Seiten, in zwei Teilstuecke geschnitten, je mit der echten
    Antwort auf die leere PDF-Seite: Die Vollfassung traegt dann nur die
    Trennlinie zwischen den Seiten, die MinerU auch ungeschnitten setzte
    (DocVortex 0.5.16 verbindet im Vollmodus jede Seite, auch eine leere, mit
    ihr). Eine Trennlinie ist kein Inhalt; das Dokument ist `empty`."""
    teile = [
        unpack_response(ersatz.echte_antwort("leere_seite_pdf"), Chunk(seite, seite))
        for seite in (0, 1)
    ]
    assert is_empty(assemble(teile))


# --- Die Leseseite --------------------------------------------------------


def test_ohne_vollfassung_eines_teilstuecks_wirft_die_darstellung() -> None:
    """Fehlt einem Teilstueck `markdown_full.md` (der Worker meldet sie dann in
    `degraded`), gibt es den Text fuer Abnehmer nicht: Das Paket setzt ihn nicht
    selbst aus `markdown` und Middle-JSON zusammen, sondern wirft. Ein Text, dem
    die Randklassen eines Teilstuecks fehlen, saehe vollstaendig aus."""
    erster = _teil_aus(Chunk(0, 0), [ersatz.seite(0, [ersatz.text_block(0, "Az. 12/34", typ="header")])])
    zweiter = _teil_aus(Chunk(1, 1), [ersatz.seite(0, [ersatz.text_block(0, "Seite 2")])])
    ergebnis = assemble([erster, dataclasses.replace(zweiter, markdown_full=None)])
    assert ergebnis["markdown_full"] is None

    with pytest.raises(IncompleteResultError) as fehler:
        rendered_markdown(ergebnis)
    # Die Meldung nennt den Grund und wo das Bezahlte liegt; beschaedigt ist
    # an dieser Zeile nichts, sie ist so zusammengesetzt worden.
    meldung = str(fehler.value)
    assert "markdown_full_je_teilstueck" in meldung
    assert "Teilstueck" in meldung
    assert "beschaedigt" not in meldung


def test_ohne_vollfassung_eines_ungeschnittenen_dokuments_nennt_die_darstellung_den_grund() -> None:
    """Dasselbe fuer ein Dokument aus einem Teilstueck, dem `markdown_full.md`
    fehlte: Die Meldung nennt die Datei und verweist nicht auf Vollfassungen
    anderer Teilstuecke, die es nicht gibt."""
    teil = _teil_aus(Chunk(0, 0), [ersatz.seite(0, [ersatz.text_block(0, "Az. 12/34", typ="header")])])
    ergebnis = assemble([dataclasses.replace(teil, markdown_full=None)])

    with pytest.raises(IncompleteResultError) as fehler:
        rendered_markdown(ergebnis)
    meldung = str(fehler.value)
    assert "markdown_full.md" in meldung
    assert "markdown_full_je_teilstueck" not in meldung
    assert "beschaedigt" not in meldung


def test_die_darstellung_braucht_nur_die_vollfassung() -> None:
    """Der Text fuer Abnehmer ist `markdown_full` und sonst nichts; ein Ergebnis,
    das ihn traegt, wird dargestellt, auch ohne `markdown` und `middle_json`."""
    assert rendered_markdown({"markdown_full": "Az. 12/34\n\nText"}) == "Az. 12/34\n\nText"


@pytest.mark.parametrize("vollfassung", [None, 5, "fehlt"])
def test_eine_beschaedigte_vollfassung_wirft_statt_weniger_zu_liefern(vollfassung: Any) -> None:
    """Entweder das ganze Dokument oder eine Ausnahme: Ein Ersatz aus `markdown`
    liesse die Randklassen weg und saehe nach einem vollstaendigen Dokument
    aus, ein leerer Ersatz nach einem leeren."""
    ergebnis = _ergebnis([ersatz.seite(0, [ersatz.text_block(0, "Az. 12/34", typ="header")])])
    if vollfassung == "fehlt":
        del ergebnis["markdown_full"]
    else:
        ergebnis["markdown_full"] = vollfassung
    with pytest.raises(IncompleteResultError, match="markdown_full"):
        rendered_markdown(ergebnis)
    with pytest.raises(IncompleteResultError):
        rendered_markdown({})


# --- Echte Antworten des Endpoints --------------------------------------


def _echte_teilstuecke() -> list[ChunkArtifacts]:
    """Die drei echten Teilstueck-Antworten des zwoelfseitigen Berichts, so wie der
    Endpoint sie geliefert hat (`artifacts/2026-10-26-mineru-4.0.12-endpoint/`,
    `plan.json`: Seiten 0-4, 5-9, 10-11)."""
    return [
        unpack_response(ersatz.echte_antwort(name), Chunk(erste, letzte))
        for name, (erste, letzte) in ersatz.echte_teilstuecke()
    ]


def _bildpfade(middle: dict[str, Any]) -> list[str]:
    pfade: list[str] = []
    stapel = [block for seite in middle["pages"] for block in seite.get("blocks", [])]
    while stapel:
        block = stapel.pop()
        if "image_path" in block:
            pfade.append(block["image_path"])
        if isinstance(block.get("content"), list):
            stapel.extend(teil for teil in block["content"] if "index" in teil)
    return pfade


def test_echte_teilstuecke_kollidieren_und_jeder_verweis_zeigt_auf_sein_bild() -> None:
    """An den echten Antworten: Die Teilstuecke 0 und 1 tragen gleich benannte
    Bilder (`page_0_chart_5.jpg` bis `page_4_chart_5.jpg`) mit verschiedenem
    Inhalt. Nach dem Zusammensetzen steht jedes Bild einzeln da, und jeder
    Verweis — `image_path`, Markdown, `image_source` — zeigt auf das Bild aus
    seinem eigenen Teilstueck (Story 24)."""
    teile = _echte_teilstuecke()
    gleich = set(teile[0].images) & set(teile[1].images)
    assert gleich, "Die Aufnahme zeigt die Kollision nicht mehr"
    assert any(teile[0].images[n] != teile[1].images[n] for n in gleich)

    ergebnis = assemble(teile)

    assert len(ergebnis["images"]) == sum(len(teil.images) for teil in teile) == 12
    for nummer, (teil, middle) in enumerate(zip(teile, ergebnis["middle_json"])):
        pfade = _bildpfade(middle)
        assert pfade
        for pfad in pfade:
            name = pfad.removeprefix(f"images/teilstueck_{nummer}/")
            assert _bild_unter(ergebnis, pfad) == teil.images[name]
    verweise = re.findall(r"!\[[^\]]*\]\(([^)]+)\)", ergebnis["markdown"])
    assert len(verweise) == 12
    for verweis in verweise:
        assert verweis in ergebnis["images"]
    for struktur in ergebnis["structured_content"]:
        for seite in struktur["pages"]:
            for block in seite["blocks"]:
                if "image_source" in block:
                    assert block["image_source"] in ergebnis["images"]


def test_echte_teilstuecke_zaehlen_danach_die_seiten_des_originals() -> None:
    """Jedes echte Teilstueck zaehlt ab Seite 0; zusammengesetzt zaehlen Seiten,
    `docvortex_layout` und strukturierter Inhalt 0 bis 11."""
    teile = _echte_teilstuecke()
    assert [[s["page_idx"] for s in teil.middle_json["pages"]] for teil in teile if teil.middle_json] == [
        [0, 1, 2, 3, 4],
        [0, 1, 2, 3, 4],
        [0, 1],
    ]
    ergebnis = assemble(teile)
    erwartet = list(range(12))
    assert [s["page_idx"] for m in ergebnis["middle_json"] for s in m["pages"]] == erwartet
    assert [
        s["page_idx"] for m in ergebnis["middle_json"] for s in m["extensions"]["docvortex_layout"]["pages"]
    ] == erwartet
    assert [s["page_idx"] for sc in ergebnis["structured_content"] for s in sc["pages"]] == erwartet


def test_echte_normale_seite_traegt_alle_fuenf_randklassen_einmal_an_ihrer_stelle() -> None:
    """Die echte normale Seite (`advanced`, OCR) liefert alle fuenf Randklassen als
    eigene Bloecke. `markdown.md` laesst Kopfzeile, Fusszeile, Seitenzahl und
    Randtext weg und behaelt die Seitenfussnote; `markdown_full.md` traegt alle
    fuenf, je einmal, in der Lesereihenfolge des Middle-JSON (`index`): die
    Kopfzeile vor der Ueberschrift, die Fussnote hinter dem Text, Fusszeile,
    Seitenzahl und Randtext am Ende der Seite. Damit ist sie der Text fuer
    Abnehmer, und das Paket setzt nichts selbst zusammen (Mineroo#13)."""
    ergebnis = assemble([unpack_response(ersatz.echte_antwort("normale_seite"), Chunk(0, 0))])
    typen = {block["type"] for block in ergebnis["middle_json"][0]["pages"][0]["blocks"]}
    assert {"header", "footer", "page_number", "aside_text", "page_footnote"} <= typen
    for text in ["Jahresbericht 2025 | Lagebericht", "Musterstrasse 1", "Seite 14", "Revisionsgesellschaft"]:
        assert text not in ergebnis["markdown"]
    assert "Rahmenvertraegen" in ergebnis["markdown"]

    dargestellt = rendered_markdown(ergebnis)

    assert dargestellt == ergebnis["markdown_full"]
    reihenfolge = [
        "Jahresbericht 2025 | Lagebericht",  # Kopfzeile
        "3. Geschaeftsverlauf im Berichtsjahr",
        "Rahmenvertraegen",  # Seitenfussnote
        "Musterstrasse 1",  # Fusszeile
        "Seite 14",  # Seitenzahl
        "Revisionsgesellschaft",  # Randtext
    ]
    for text in reihenfolge:
        assert dargestellt.count(text) == 1, text
    stellen = [dargestellt.index(text) for text in reihenfolge]
    assert stellen == sorted(stellen)
    assert not is_empty(ergebnis)


def test_echte_teilstuecke_ergeben_den_ganzen_text_mit_jeder_seitenzahl_einmal() -> None:
    """Der Text fuer Abnehmer ist das ganze Dokument, auch ueber Teilstuecke: Die
    zwoelf Seiten des echten Berichts tragen je ihre Kopfzeile und ihre
    Seitenzahl, die `markdown.md` weglaesst, jede Seitenzahl genau einmal und in
    der Reihenfolge der Seiten."""
    ergebnis = assemble(_echte_teilstuecke())
    assert "Seite 1 von 12" not in ergebnis["markdown"]

    dargestellt = rendered_markdown(ergebnis)

    assert dargestellt.count("Technischer Bericht Kuehlanlagen – Messkampagne 2025") == 12
    seitenzahlen = [f"Seite {nummer} von 12" for nummer in range(1, 13)]
    for seitenzahl in seitenzahlen:
        assert dargestellt.count(seitenzahl) == 1, seitenzahl
    stellen = [dargestellt.index(seitenzahl) for seitenzahl in seitenzahlen]
    assert stellen == sorted(stellen)


def test_echte_teilstuecke_trennen_jede_seite_wie_ein_ungeschnittenes_dokument() -> None:
    """Die Vollfassung setzt zwischen je zwei benachbarte Seiten eine Trennlinie
    (MinerU 4.0.12, `docs/next/middle-json/rendering.md`, `RenderMode.FULL`), und
    das zeigen die echten Teilstuecke: fuenf Seiten, vier Trennlinien. Der Text
    fuer Abnehmer verraet nicht, wo das Paket geschnitten hat: Zwoelf Seiten
    tragen elf Trennlinien, auch hinter der fuenften und der zehnten Seite."""
    teile = _echte_teilstuecke()
    trennlinie = "\n\n---\n\n"
    assert [teil.markdown_full.count(trennlinie) for teil in teile if teil.markdown_full] == [4, 4, 1]

    dargestellt = rendered_markdown(assemble(teile))

    assert dargestellt.count(trennlinie) == 11
    for seitenzahl in ("Seite 5 von 12", "Seite 10 von 12"):
        assert dargestellt.split(seitenzahl, 1)[1].startswith(trennlinie), seitenzahl


def test_echte_bild_deutungen_stehen_hinter_ihrem_bild_im_text() -> None:
    """Mit eingeschalteter Bildanalyse schreibt MinerU die Deutung jedes Bildes
    hinter das Bild, als eingeklappten Block (`<details>`), auch fuer Bilder,
    die keine Diagramme sind: eine gezeichnete Szene als Beschreibung, ein
    Organigramm als Mermaid-Graph. Gemessen an der echten Seite
    (`ECHT_BILDANALYSE`, `advanced` mit OCR und Bildanalyse an); der Text fuer
    Abnehmer traegt beide je einmal, zwischen Bild und Bildunterschrift."""
    antwort = ersatz.echte_antwort("bild_seite", ersatz.ECHT_BILDANALYSE)
    assert antwort["output"]["image_analysis"] is True
    dargestellt = rendered_markdown(assemble([unpack_response(antwort, Chunk(0, 0))]))

    for bild, deutung, unterschrift in [
        (
            "![](images/teilstueck_0/page_0_image_3.jpg)",
            "Simple illustration of a house with red roof, green tree, and yellow sun",
            "Bild 1: Das Gaestehaus am Werk Nord",
        ),
        (
            "![](images/teilstueck_0/page_0_image_6.jpg)",
            'A["Geschaeftsfuehrung"] --> B["Einkauf"]',
            "Bild 2: Gliederung des Werks Nord",
        ),
    ]:
        assert dargestellt.count(deutung) == 1, deutung
        assert dargestellt.index(bild) < dargestellt.index(deutung) < dargestellt.index(unterschrift)
    assert "<summary>natural_image</summary>" in dargestellt
    assert "<summary>flowchart</summary>" in dargestellt


@pytest.mark.parametrize(
    ("aufnahme", "text"),
    [("dokument_docx", "Druckluftanlage"), ("seite_html", "Heizzeiten")],
)
def test_echte_ganzdokumente_werden_ausgepackt(aufnahme: str, text: str) -> None:
    """DOCX und HTML liest der Worker ganz, ohne Modelle, und meldet die Stufe so,
    wie MinerU sie vermerkt hat: `flash`, `txt`, Bildanalyse aus."""
    antwort = ersatz.echte_antwort(aufnahme)
    assert (antwort["output"]["tier"], antwort["output"]["ocr_mode"]) == ("flash", "txt")
    ergebnis = assemble([unpack_response(antwort, Chunk(0, 0))])
    assert text in ergebnis["markdown"]
    assert not is_empty(ergebnis)
