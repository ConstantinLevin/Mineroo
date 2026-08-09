"""Zusammensetzen — gegen die sechs echten Antworten, wo es geht.

Die Fixtures sind Inline-Antworten (siehe conftest). Alles, was sie tragen, wird
hier gegen sie geprueft; der Tarball-Pfad hat bis zum ersten Live-Lauf kein
Material und ist unten ausdruecklich als offen markiert, statt mit einer
erfundenen Antwort scheinbar abgedeckt zu werden.
"""

from __future__ import annotations

import base64
import copy
import io
import json
import tarfile
from typing import Any

import pytest

from mineru_ocr.assembly import (
    FURNITURE_TYPES,
    ChunkArtifacts,
    assemble,
    is_empty,
    rendered_text,
    unpack_response,
)
from mineru_ocr.chunking import Chunk
from mineru_ocr.errors import TerminalRunPodError


def _antwort(
    *,
    markdown: str = "",
    content_list: list[dict[str, Any]] | None = None,
    images: dict[str, str] | None = None,
    middle: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Baut eine Inline-Antwort in der FORM der echten Antworten.

    Die Form ist an den konservierten Antworten abgelesen, nicht ausgedacht:
    aussen `delayTime`/`executionTime`/`id`/`status`/`workerId`, darin `output`
    mit `debug`/`elapsed_seconds`/`mineru_version`/`ok`/`results`, und `results`
    ist eine Liste mit GENAU EINEM Eintrag.
    """
    return {
        "delayTime": 812,
        "executionTime": 4211,
        "id": "sync-erfunden-fuer-den-test",
        "status": "COMPLETED",
        "workerId": "erfundenerwrk",
        "output": {
            "ok": True,
            "elapsed_seconds": 4.2,
            "mineru_version": "2.5.4",
            "debug": {"backend": "vlm-auto-engine"},
            "results": [
                {
                    "basename": "doc",
                    "source": "b64",
                    "pages_requested": -1,
                    "markdown": markdown,
                    "content_list": content_list if content_list is not None else [],
                    "images": images if images is not None else {},
                    "middle": middle if middle is not None else {},
                }
            ],
        },
    }


def _teil(
    chunk: Chunk,
    *,
    markdown: str = "",
    content_list: list[dict[str, Any]] | None = None,
    images: dict[str, bytes] | None = None,
    middle: dict[str, Any] | None = None,
) -> ChunkArtifacts:
    return ChunkArtifacts(
        chunk=chunk,
        markdown=markdown,
        content_list=content_list if content_list is not None else [],
        images=images if images is not None else {},
        middle=middle if middle is not None else {},
        layout_pdf=None,
        span_pdf=None,
        envelope={"id": f"job-{chunk.first_page}"},
        extras={},
    )


# --- Auspacken ------------------------------------------------------------


def test_inline_antwort_wird_ausgepackt(real_responses: list[dict[str, Any]]) -> None:
    """Gegen die echten Fixtures: markdown, content_list, middle und die Bilder
    kommen heraus, die PDFs sind None."""
    assert len(real_responses) == 6, "Die sechs konservierten Antworten fehlen"
    for antwort in real_responses:
        teil = unpack_response(antwort, Chunk(first_page=0, last_page=0))
        erwartet = antwort["output"]["results"][0]
        assert teil.markdown == erwartet["markdown"]
        assert teil.content_list == erwartet["content_list"]
        assert teil.middle == erwartet["middle"]
        assert set(teil.images) == set(erwartet["images"])
        # Inline liefert die beiden Visualisierungs-PDFs nicht — nur Tarball und
        # S3 packen das ganze Ausgabeverzeichnis ein (Betriebshandbuch 14a).
        assert teil.layout_pdf is None
        assert teil.span_pdf is None


def test_inline_bilder_sind_base64_jpeg(real_responses: list[dict[str, Any]]) -> None:
    """Das Inline-Bildformat, am echten Material festgehalten.

    Diese Frage stand im Vertrag als OFFEN — mit der falschen Begruendung, in
    allen sechs Antworten sei `images` leer. Das war eine Verallgemeinerung aus
    EINER angesehenen Antwort, und zufaellig ist die erste eine der beiden
    bildlosen. Gezaehlt sind es 22 Bilder ueber vier der sechs Antworten.

    Der Test haelt fest, was gemessen wurde: Werte sind Base64-ZEICHENKETTEN,
    dekodiert ergeben sie JPEG. Aendert die Gegenseite das, faellt es hier auf
    und nicht erst an einem kaputten Bild in der Datenbank.
    """
    gesamt = 0
    for antwort in real_responses:
        roh = antwort["output"]["results"][0]["images"]
        teil = unpack_response(antwort, Chunk(first_page=0, last_page=0))
        for name, wert in roh.items():
            assert isinstance(wert, str), (name, type(wert))
            assert teil.images[name] == base64.b64decode(wert, validate=True)
            # JPEG-Signatur: SOI-Marker.
            assert teil.images[name].startswith(b"\xff\xd8\xff"), name
            gesamt += 1
    assert gesamt == 22, f"Erwartet waren 22 gezaehlte Bilder, gefunden {gesamt}"


def test_bildnamen_tragen_kein_verzeichnis_praefix(
    real_responses: list[dict[str, Any]],
) -> None:
    """Die Schluessel in `images` sind nackte Dateinamen, die `img_path` in der
    content_list tragen `images/` davor.

    Wer den einen Wert direkt im anderen nachschlaegt, findet nichts und haelt
    das Bild fuer fehlend. Der Test haelt den Unterschied fest, damit niemand ihn
    fuer einen Fehler in den Daten haelt.

    Und die zweite Haelfte des Befunds: Es gibt MEHR Bilder als Erwaehnungen.
    Wer die Bilder an den Erwaehnungen ausrichtet, wirft bezahlte Artefakte weg.
    """
    mit_bildern = 0
    for antwort in real_responses:
        ergebnis = antwort["output"]["results"][0]
        namen = set(ergebnis["images"])
        if not namen:
            continue
        mit_bildern += 1
        assert not any(name.startswith("images/") for name in namen)

        pfade = [
            eintrag["img_path"]
            for eintrag in ergebnis["content_list"]
            if eintrag.get("img_path")
        ]
        assert all(pfad.startswith("images/") for pfad in pfade), pfade
        # Jede Erwaehnung findet ihr Bild — NACH dem Abschneiden des Praefix.
        for pfad in pfade:
            assert pfad.removeprefix("images/") in namen, pfad
        # Aber nicht jedes Bild wird erwaehnt.
        assert len(namen) >= len(pfade)
    assert mit_bildern == 4, f"Erwartet waren 4 bildhaltige Antworten, {mit_bildern}"


def test_umschlag_traegt_die_herkunft(real_responses: list[dict[str, Any]]) -> None:
    """Was die Antwort ausser dem Inhalt trug, geht nicht verloren.

    `id`, `workerId`, `executionTime`, `delayTime` und der `debug`-Block sind die
    Herkunft eines BEZAHLTEN Ergebnisses. Sie wegzuwerfen hiesse, spaeter nicht
    mehr beantworten zu koennen, welcher Worker mit welcher Fassung es erzeugt
    hat.
    """
    teil = unpack_response(real_responses[0], Chunk(first_page=0, last_page=0))
    for schluessel in ["id", "workerId", "executionTime", "delayTime", "status"]:
        assert schluessel in teil.envelope, schluessel
    assert "debug" in teil.envelope["output_meta"]
    assert "mineru_version" in teil.envelope["output_meta"]
    # Der Inhalt selbst steckt NICHT im Umschlag — sonst laege er doppelt vor.
    assert "results" not in teil.envelope["output_meta"]


def test_tarball_antwort_wird_ausgepackt() -> None:
    """Der Tarball-Pfad gegen ein SELBST GEBAUTES Archiv.

    EINSCHRAENKUNG, die man kennen muss: Die konservierten Antworten sind inline;
    fuer den Tarball gibt es kein echtes Material. Aus der Doku belegt ist genau
    EIN Dateiname — `package.py` sucht `{basename}_content_list.json` und faellt
    auf `{basename}_content_list_v2.json` zurueck. Die uebrigen Namen hier sind
    eine ANNAHME, keine Messung.

    Deshalb ordnet `unpack_response` nach Namensmustern statt nach festen Namen,
    und deshalb prueft dieser Test das MUSTER — nicht, dass die Gegenseite genau
    diese Namen benutzt. Trifft der erste Live-Lauf andere Namen, wirft das
    Auspacken laut und nennt den Archiv-Inhalt; still leer wird es nie.
    """
    puffer = io.BytesIO()
    dateien = {
        "doc/doc.md": b"# Ueberschrift\n\nText.",
        "doc/doc_content_list.json": json.dumps(
            [{"type": "text", "text": "Text.", "page_idx": 0}]
        ).encode(),
        "doc/doc_middle.json": json.dumps({"_backend": "vlm", "pdf_info": []}).encode(),
        "doc/doc_layout.pdf": b"%PDF-1.7 layout",
        "doc/doc_span.pdf": b"%PDF-1.7 span",
        "doc/images/abc123.jpg": b"\xff\xd8\xff\xe0 bildbytes",
    }
    with tarfile.open(fileobj=puffer, mode="w:gz") as archiv:
        for name, inhalt in dateien.items():
            info = tarfile.TarInfo(name)
            info.size = len(inhalt)
            archiv.addfile(info, io.BytesIO(inhalt))

    antwort = _antwort()
    antwort["output"]["results"][0] = {
        "basename": "doc",
        "tarball_b64": base64.b64encode(puffer.getvalue()).decode("ascii"),
    }

    teil = unpack_response(antwort, Chunk(first_page=4, last_page=4))
    assert teil.markdown == "# Ueberschrift\n\nText."
    assert teil.content_list == [{"type": "text", "text": "Text.", "page_idx": 0}]
    assert teil.middle == {"_backend": "vlm", "pdf_info": []}
    assert teil.layout_pdf == b"%PDF-1.7 layout"
    assert teil.span_pdf == b"%PDF-1.7 span"
    assert teil.images == {"abc123.jpg": b"\xff\xd8\xff\xe0 bildbytes"}


def test_tarball_ohne_erkennbaren_inhalt_wirft_und_nennt_die_dateien() -> None:
    """Passt kein Namensmuster, wird laut geworfen und der Archiv-Inhalt genannt.

    Das ist der Ausweg aus der Annahme oben: Aendert die Gegenseite ihre
    Dateinamen, entsteht kein leeres Ergebnis, das wie eine leere Seite aussieht,
    sondern ein Fehler, der die tatsaechlichen Namen mitliefert.
    """
    puffer = io.BytesIO()
    with tarfile.open(fileobj=puffer, mode="w:gz") as archiv:
        inhalt = b"nichts Verwertbares"
        info = tarfile.TarInfo("doc/voellig_anders.dat")
        info.size = len(inhalt)
        archiv.addfile(info, io.BytesIO(inhalt))

    antwort = _antwort()
    antwort["output"]["results"][0] = {
        "tarball_b64": base64.b64encode(puffer.getvalue()).decode("ascii")
    }
    with pytest.raises(TerminalRunPodError) as fehler:
        unpack_response(antwort, Chunk(first_page=0, last_page=0))
    assert "doc/voellig_anders.dat" in str(fehler.value)


def test_fehlerantwort_wird_nicht_ausgepackt() -> None:
    """Eine Antwort ohne `output` ergibt TerminalRunPodError, nicht einen
    leeren Satz Bestandteile."""
    chunk = Chunk(first_page=0, last_page=0)
    kaputte: list[dict[str, Any]] = [
        {"status": "FAILED", "id": "x"},
        {"status": "COMPLETED", "output": None},
        {"status": "COMPLETED", "output": {}},
        {"status": "COMPLETED", "output": {"results": []}},
        {"status": "COMPLETED", "output": {"results": "keine Liste"}},
    ]
    for kaputt in kaputte:
        with pytest.raises(TerminalRunPodError):
            unpack_response(kaputt, chunk)


def test_mehrere_ergebnisse_werden_nicht_stillschweigend_gekuerzt() -> None:
    """Zwei Ergebnisse in einer Antwort werfen, statt das erste zu nehmen.

    Wir schicken je Teilstueck genau eine Datei. Kommen zwei zurueck, hat die
    Gegenseite etwas anderes getan als bestellt — das erste stillschweigend zu
    nehmen waere der unsichtbarste denkbare Verlust.
    """
    antwort = _antwort(markdown="eins")
    antwort["output"]["results"].append({"markdown": "zwei", "content_list": []})
    with pytest.raises(TerminalRunPodError, match="2 Ergebnisse"):
        unpack_response(antwort, Chunk(first_page=0, last_page=0))


def test_bilder_kommen_als_bytes_heraus() -> None:
    """Base64-Zeichenketten werden dekodiert, Unlesbares wirft.

    OFFEN: Wie der Inline-Transport Bilder uebergibt, ist an echtem Material
    nicht feststellbar — in allen sechs konservierten Antworten ist `images`
    leer. Geprueft wird deshalb die Zusicherung dieser Funktion (beides annehmen,
    bei allem anderen werfen), nicht das tatsaechliche Verhalten der Engine.
    """
    antwort = _antwort(images={"a.jpg": base64.b64encode(b"bild").decode("ascii")})
    teil = unpack_response(antwort, Chunk(first_page=0, last_page=0))
    assert teil.images == {"a.jpg": b"bild"}

    kaputt = _antwort(images={"a.jpg": "kein gueltiges base64 !!!"})
    with pytest.raises(TerminalRunPodError):
        unpack_response(kaputt, Chunk(first_page=0, last_page=0))


# --- Zusammensetzen -------------------------------------------------------


def test_seitenversatz_wird_aufgeschlagen() -> None:
    """Zwei Teilstuecke mit teilstueck-lokalen page_idx ab 0 ergeben zusammen
    fortlaufende Seitenzahlen des Originals.

    Der fehleranfaelligste Punkt des ganzen Zusammensetzens: MinerU schneidet vor
    dem Parsen, also faengt page_idx in JEDEM Teilstueck wieder bei 0 an. Ohne
    Korrektur traegt ein 120-Seiten-Dokument sechsmal eine „Seite 0" — und sieht
    dabei vollstaendig und plausibel aus.

    ACHTUNG zum Material: Die sechs konservierten Antworten sind einseitige
    Bilder, ihre page_idx sind alle 0. Fuer die Mehr-Teilstueck-Lage gibt es
    ebenfalls kein echtes Material — dieser Test laeuft gegen erfundene
    Antworten, bis der Live-Smoke eine echte mehrteilige Antwort erzeugt hat.
    """
    ergebnis = assemble(
        [
            _teil(
                Chunk(first_page=0, last_page=1),
                content_list=[
                    {"type": "text", "text": "s0", "page_idx": 0},
                    {"type": "text", "text": "s1", "page_idx": 1},
                ],
            ),
            _teil(
                Chunk(first_page=2, last_page=3),
                content_list=[
                    {"type": "text", "text": "s2", "page_idx": 0},
                    {"type": "text", "text": "s3", "page_idx": 1},
                ],
            ),
        ]
    )
    assert [e["page_idx"] for e in ergebnis["content_list"]] == [0, 1, 2, 3]
    assert [e["text"] for e in ergebnis["content_list"]] == ["s0", "s1", "s2", "s3"]


def test_nicht_ganzzahliges_page_idx_wirft() -> None:
    """Ein page_idx, das keine ganze Zahl ist, wirft — statt unveraendert
    durchzugehen und eine falsche Seitenzahl zu speichern, die spaeter niemand
    mehr als falsch erkennt."""
    with pytest.raises(TerminalRunPodError, match="page_idx"):
        assemble(
            [
                _teil(
                    Chunk(first_page=5, last_page=6),
                    content_list=[{"type": "text", "page_idx": "0"}],
                )
            ]
        )


def test_eintrag_ohne_page_idx_geht_unveraendert_durch() -> None:
    """Ein Eintrag ohne `page_idx` behauptet keine Seite — also ist auch nichts
    zu korrigieren, und es wird keine erfunden."""
    ergebnis = assemble(
        [_teil(Chunk(first_page=7, last_page=7), content_list=[{"type": "text"}])]
    )
    assert ergebnis["content_list"] == [{"type": "text"}]


def test_am_ergebnis_ist_der_schnitt_nicht_ablesbar() -> None:
    """Das zusammengesetzte Ergebnis traegt keine Spur davon, ob und wo
    geschnitten wurde.

    Das ist der ganze Zweck: Rein geht das ganze Dokument, raus kommt das ganze
    Dokument. Waere die Teilung am Ergebnis noch sichtbar, muesste die Leseseite
    sie kennen — und damit stuende sie in der Datenbank.

    Geprueft wird es an der einzigen Stelle, an der es sich ueberhaupt pruefen
    laesst: Dasselbe Dokument, einmal in einem Stueck und einmal in dreien
    verarbeitet, ergibt in Markdown und content_list dasselbe.
    """
    seiten = [{"type": "text", "text": f"Seite {n}", "page_idx": n} for n in range(6)]

    ganz = assemble(
        [
            _teil(
                Chunk(first_page=0, last_page=5),
                markdown="Seite 0\nSeite 1\nSeite 2\nSeite 3\nSeite 4\nSeite 5",
                content_list=seiten,
            )
        ]
    )
    geteilt = assemble(
        [
            _teil(
                Chunk(first_page=0, last_page=1),
                markdown="Seite 0\nSeite 1",
                content_list=[
                    {"type": "text", "text": "Seite 0", "page_idx": 0},
                    {"type": "text", "text": "Seite 1", "page_idx": 1},
                ],
            ),
            _teil(
                Chunk(first_page=2, last_page=3),
                markdown="Seite 2\nSeite 3",
                content_list=[
                    {"type": "text", "text": "Seite 2", "page_idx": 0},
                    {"type": "text", "text": "Seite 3", "page_idx": 1},
                ],
            ),
            _teil(
                Chunk(first_page=4, last_page=5),
                markdown="Seite 4\nSeite 5",
                content_list=[
                    {"type": "text", "text": "Seite 4", "page_idx": 0},
                    {"type": "text", "text": "Seite 5", "page_idx": 1},
                ],
            ),
        ]
    )
    assert geteilt["content_list"] == ganz["content_list"]
    # Im Markdown bleibt der Unterschied der Trenner zwischen den Stuecken —
    # das ist die EINE Stelle, an der der Schnitt nachwirkt, und sie ist
    # bekannt und gewollt (keine Naht-Reparatur, siehe assemble).
    assert geteilt["markdown"].split() == ganz["markdown"].split()


def test_seitenzahlen_bleiben_im_bereich() -> None:
    """Kein page_idx liegt ausserhalb von 0 bis Seitenzahl minus 1."""
    teile = [
        _teil(
            Chunk(first_page=start, last_page=start + 3),
            content_list=[
                {"type": "text", "page_idx": lokal} for lokal in range(4)
            ],
        )
        for start in range(0, 20, 4)
    ]
    ergebnis = assemble(teile)
    seiten = [e["page_idx"] for e in ergebnis["content_list"]]
    assert min(seiten) == 0
    assert max(seiten) == 19
    assert seiten == sorted(seiten)


def test_rohantwort_bleibt_unveraendert(real_responses: list[dict[str, Any]]) -> None:
    """Das Zusammensetzen aendert die uebergebenen Antworten nicht.

    Sonst waere ein zweites, anderes Zusammensetzen desselben Rohbestands nicht
    mehr moeglich — und genau das ist der Grund, warum roh gespeichert wird.
    """
    vorher = copy.deepcopy(real_responses)
    teile = [
        unpack_response(antwort, Chunk(first_page=nr, last_page=nr))
        for nr, antwort in enumerate(real_responses)
    ]
    assemble(teile)
    assert real_responses == vorher


def test_markdown_wird_in_dokumentreihenfolge_gefuegt() -> None:
    """Aneinanderhaengen in aufsteigender Seitenreihenfolge, mehr nicht."""
    ergebnis = assemble(
        [
            _teil(Chunk(first_page=0, last_page=0), markdown="erstes"),
            _teil(Chunk(first_page=1, last_page=1), markdown="zweites"),
            _teil(Chunk(first_page=2, last_page=2), markdown="drittes"),
        ]
    )
    assert ergebnis["markdown"] == "erstes\n\nzweites\n\ndrittes"


def test_bilder_werden_vereinigt() -> None:
    """Gleiche Namen sind gleiche Inhalte (Namen sind Inhalts-Hashes), also
    darf einfach vereinigt werden."""
    ergebnis = assemble(
        [
            _teil(Chunk(first_page=0, last_page=0), images={"a.jpg": b"A"}),
            _teil(
                Chunk(first_page=1, last_page=1),
                images={"a.jpg": b"A", "b.jpg": b"B"},
            ),
        ]
    )
    assert set(ergebnis["images"]) == {"a.jpg", "b.jpg"}
    assert base64.b64decode(ergebnis["images"]["a.jpg"]) == b"A"
    assert base64.b64decode(ergebnis["images"]["b.jpg"]) == b"B"


def test_ergebnis_ist_json_faehig() -> None:
    """Das Ergebnis geht unveraendert in eine jsonb-Spalte.

    Bytes haben in JSON keine Entsprechung; blieben sie roh, faellt das erst beim
    Schreiben in die Datenbank auf — also nach dem bezahlten Lauf.
    """
    ergebnis = assemble(
        [_teil(Chunk(first_page=0, last_page=0), images={"a.jpg": b"\xff\xd8\xff"})]
    )
    wieder = json.loads(json.dumps(ergebnis))
    assert wieder == ergebnis


def test_middle_wird_nicht_verschmolzen() -> None:
    """`assemble` legt `middle` als Liste je Teilstueck ab — ein erfundenes
    Zusammenfuehren waere eine Behauptung ueber ein fremdes, versioniertes
    Format."""
    ergebnis = assemble(
        [
            _teil(Chunk(first_page=0, last_page=0), middle={"_version_name": "2.5.4"}),
            _teil(Chunk(first_page=1, last_page=1), middle={"_version_name": "2.5.4"}),
        ]
    )
    assert ergebnis["middle"] == [{"_version_name": "2.5.4"}, {"_version_name": "2.5.4"}]
    assert ergebnis["layout_pdfs"] == [None, None]
    assert ergebnis["span_pdfs"] == [None, None]
    assert len(ergebnis["envelopes"]) == 2


# --- Leer oder nicht ------------------------------------------------------


def test_leer_erkennt_leeres_dokument() -> None:
    """Alle Teilstuecke ohne Text und ohne inhaltstragende Eintraege ergeben
    `True`.

    Das genaue Kriterium ist am echten Verhalten der Schnittstelle
    festzustellen (Plan 2.4) — dieser Test haelt die Arbeitsfassung fest und
    wird nachgezogen, sobald eine echte leere Antwort vorliegt.
    """
    assert is_empty(assemble([_teil(Chunk(first_page=0, last_page=0))]))
    assert is_empty(
        assemble(
            [
                _teil(
                    Chunk(first_page=0, last_page=0),
                    markdown="   \n\n  ",
                    content_list=[{"type": "text", "text": "  "}],
                )
            ]
        )
    )


def test_leer_ist_streng() -> None:
    """Ein einziger inhaltstragender Eintrag genuegt, damit `is_empty` falsch
    ist.

    Im Zweifel gilt ein Dokument als NICHT leer: Ein faelschlich als fertig
    verbuchtes leeres Dokument faellt beim Ansehen auf, ein faelschlich als leer
    verbuchtes volles nie.
    """
    inhaltstragend: list[dict[str, Any]] = [
        {"type": "text", "text": "x"},
        {"type": "list", "list_items": ["x"]},
        {"type": "image", "img_path": "images/a.jpg", "content": ""},
        {"type": "table", "content": "<table/>"},
    ]
    for eintrag in inhaltstragend:
        ergebnis = assemble(
            [_teil(Chunk(first_page=0, last_page=0), content_list=[eintrag])]
        )
        assert not is_empty(ergebnis), eintrag


def test_nur_ein_randblock_ist_nicht_leer() -> None:
    """Ein Dokument, von dem NUR der Briefkopf erkannt wurde, ist nicht leer.

    Der Fall ist nicht konstruiert: In genau diesen Bloecken liegt das
    Aktenzeichen (Kalibrierung 5.1). Zaehlte man sie nicht mit, wuerde ein
    Bescheid, dessen Kennung sauber erkannt wurde, als `empty` verbucht und nie
    wieder angesehen — und die Kennung ist der wertvollste Fang des ganzen
    Dokuments.
    """
    ergebnis = assemble(
        [
            _teil(
                Chunk(first_page=0, last_page=0),
                content_list=[
                    {
                        "type": "header",
                        "text": "Aktenzeichen 123/456/7890/012/345/6",
                        "page_idx": 0,
                    }
                ],
            )
        ]
    )
    assert not is_empty(ergebnis)


def test_echte_antworten_sind_nicht_leer(real_responses: list[dict[str, Any]]) -> None:
    """Keine der sechs konservierten Antworten gilt als leer.

    Sie zeigen nur, wie eine NICHT leere Antwort aussieht (Markdown zwischen 201
    und 2069 Zeichen) — eine echte leere Antwort gibt es im Bestand nicht. Der
    Test sichert damit die eine Richtung, die belegbar ist.
    """
    for antwort in real_responses:
        teil = unpack_response(antwort, Chunk(first_page=0, last_page=0))
        assert not is_empty(assemble([teil]))


# --- Die Leseseite --------------------------------------------------------


def test_randbloecke_lassen_sich_weglassen() -> None:
    """`rendered_text(furniture=False)` liefert das Markdown der Engine
    wortgleich — also ohne die Randbloecke, die sie selbst herausgefiltert hat.
    """
    ergebnis = assemble(
        [
            _teil(
                Chunk(first_page=0, last_page=0),
                markdown="# Introduction\n\nFliesstext.",
                content_list=[
                    {"type": "text", "text": "Fliesstext.", "page_idx": 0},
                    {"type": "header", "text": "Aktenzeichen 123/456", "page_idx": 0},
                    {"type": "footer", "text": "Copyright 2024", "page_idx": 0},
                ],
            )
        ]
    )
    assert rendered_text(ergebnis, furniture=False) == "# Introduction\n\nFliesstext."


def test_randbloecke_werden_voreingestellt_angehaengt() -> None:
    """Die VOREINSTELLUNG haengt die Randbloecke an — dort liegt das
    Aktenzeichen.

    Der Befund dahinter, an zwei unabhaengigen Stellen belegt: MinerUs Markdown
    filtert fuenf Blockklassen heraus, und das Aktenzeichen liegt als
    `{"type": "header"}` in der content_list. „Fuer den Bescheid-Korpus ist ein
    Write-back aus dem nackten Markdown damit nachweislich schlechter als das,
    was heute schon auf den Records liegt" (Kalibrierung 5.4).
    """
    ergebnis = assemble(
        [
            _teil(
                Chunk(first_page=0, last_page=0),
                markdown="# Introduction\n\nFliesstext.",
                content_list=[
                    {"type": "text", "text": "Fliesstext.", "page_idx": 0},
                    {"type": "header", "text": "Aktenzeichen 123/456", "page_idx": 0},
                ],
            )
        ]
    )
    text = rendered_text(ergebnis)
    assert "Aktenzeichen 123/456" in text
    # Angehaengt, nie ersetzt: Das Markdown der Engine steht unveraendert vorn.
    assert text.startswith("# Introduction\n\nFliesstext.")


def test_alle_fuenf_randklassen_werden_erfasst() -> None:
    """Es sind FUENF Klassen, nicht die drei offensichtlichen.

    Die Kalibrierung hat sie einzeln gezaehlt: `header`, `footer`, `aside_text`,
    `page_number`, `page_footnote`. Wer nur Kopf- und Fusszeilen erfasst, laesst
    `aside_text` liegen — 94 von 102 Bloecken dieser Klasse fehlen im Markdown.
    """
    assert FURNITURE_TYPES == {
        "header",
        "footer",
        "aside_text",
        "page_number",
        "page_footnote",
    }
    ergebnis = assemble(
        [
            _teil(
                Chunk(first_page=0, last_page=0),
                markdown="Inhalt",
                content_list=[
                    {"type": klasse, "text": f"rand-{klasse}", "page_idx": 0}
                    for klasse in sorted(FURNITURE_TYPES)
                ],
            )
        ]
    )
    text = rendered_text(ergebnis)
    for klasse in FURNITURE_TYPES:
        assert f"rand-{klasse}" in text, klasse


def test_fremdsprachenfilter_wird_angewandt() -> None:
    """Der uebergebene Filter entscheidet, welche Bloecke wegfallen — die
    Voreinstellung filtert NICHTS.

    Der Filter ist keine Zierde: Ueber die 15 Kalibrierungs-Dokumente stehen 30
    Fundstellen englischer Modell-Prosa in genau diesen Bloecken. „Wer die
    Bloecke ungefiltert anhaengt, schreibt Werkzeug-Kommentare in deutsche
    Steuerakten."
    """
    prosa = "The image does not contain any readable content."
    ergebnis = assemble(
        [
            _teil(
                Chunk(first_page=0, last_page=0),
                markdown="Inhalt",
                content_list=[
                    {"type": "header", "text": "Aktenzeichen 123/456", "page_idx": 0},
                    {"type": "aside_text", "text": prosa, "page_idx": 0},
                ],
            )
        ]
    )
    ohne_filter = rendered_text(ergebnis)
    assert prosa in ohne_filter

    mit_filter = rendered_text(
        ergebnis, foreign_filter=lambda block: block.startswith("The image")
    )
    assert prosa not in mit_filter
    assert "Aktenzeichen 123/456" in mit_filter


def test_filter_greift_nie_in_das_markdown_der_engine() -> None:
    """Der Filter wirkt nur auf die angehaengten Randbloecke.

    Sonst stuende am Ende ein Text da, den weder die Engine noch der Aufrufer je
    gesehen hat — und die Zusicherung „es wird nie etwas entfernt, nur ergaenzt"
    waere gebrochen.
    """
    ergebnis = assemble(
        [
            _teil(
                Chunk(first_page=0, last_page=0),
                markdown="The image shows a chart.",
                content_list=[{"type": "header", "text": "Kopf", "page_idx": 0}],
            )
        ]
    )
    text = rendered_text(ergebnis, foreign_filter=lambda block: True)
    assert text == "The image shows a chart."


def test_ohne_randbloecke_sind_beide_wege_gleich(
    real_responses: list[dict[str, Any]],
) -> None:
    """Traegt ein Dokument keine Randbloecke, liefern beide Einstellungen
    dasselbe — die Voreinstellung kann nichts kaputt machen, wo es nichts
    anzuhaengen gibt."""
    for antwort in real_responses:
        teil = unpack_response(antwort, Chunk(first_page=0, last_page=0))
        ohne_rand = ChunkArtifacts(
            chunk=teil.chunk,
            markdown=teil.markdown,
            content_list=[
                e for e in teil.content_list if e.get("type") not in FURNITURE_TYPES
            ],
            images=teil.images,
            middle=teil.middle,
            layout_pdf=None,
            span_pdf=None,
            envelope=teil.envelope,
            extras=teil.extras,
        )
        ergebnis = assemble([ohne_rand])
        assert rendered_text(ergebnis) == rendered_text(ergebnis, furniture=False)


# --- Die Abruf-Regel als Maschinenpruefung -------------------------------


def test_kein_feld_der_echten_antwort_geht_verloren(
    real_responses: list[dict[str, Any]],
) -> None:
    """JEDES Feld jeder echten Antwort ist im gespeicherten Ergebnis wiederzufinden.

    DAS IST DIE ABRUF-REGEL ALS MASCHINENPRUEFUNG, und sie steht hier, weil die
    Regel ohne sie verletzt war: `unpack_response` griff die vier Felder heraus,
    die es kannte, und liess `basename`, `source` und `pages_requested` fallen —
    an jeder der sechs Antworten, still. Aufgefallen ist das nicht beim Lesen des
    Codes, sondern erst bei der Frage „speichern wir wirklich alles?".

    Ein Test, der die drei Feldnamen aufzaehlt, haette den naechsten Verlust
    nicht gefangen: Er prueft dann genau die Felder, die man schon kennt. Dieser
    hier zaehlt nicht auf — er nimmt die Antwort, wie sie ist, und verlangt, dass
    jedes ihrer Felder ANKOMMT. Fuegt die Engine morgen ein Feld hinzu, faellt es
    hier auf, ohne dass jemand den Test anfassen muesste.
    """
    for antwort in real_responses:
        ergebnis_roh = antwort["output"]["results"][0]
        teil = unpack_response(antwort, Chunk(first_page=0, last_page=0))
        gespeichert = assemble([teil])

        gesucht = set(ergebnis_roh)
        gefunden = {"markdown", "content_list", "images", "middle"}
        gefunden |= set(gespeichert["extras"][0])

        fehlend = gesucht - gefunden
        assert not fehlend, (
            f"Diese Felder der Antwort landen NIRGENDS: {sorted(fehlend)}. "
            f"Das ist ein Verstoss gegen die Abruf-Regel — was hier nicht "
            f"aufgehoben wird, ist dreissig Minuten spaeter fuer immer weg."
        )


def test_werte_der_uebrigen_felder_kommen_unveraendert_an(
    real_responses: list[dict[str, Any]],
) -> None:
    """Nicht nur die NAMEN kommen an, sondern die Werte — unveraendert.

    Der Unterschied ist nicht theoretisch: Ein Feld unter seinem Namen mit einem
    leeren Wert zu speichern saehe im vorigen Test genauso aus wie eines mit
    seinem Inhalt.
    """
    for antwort in real_responses:
        ergebnis_roh = antwort["output"]["results"][0]
        teil = unpack_response(antwort, Chunk(first_page=0, last_page=0))
        for schluessel, wert in ergebnis_roh.items():
            if schluessel in {"markdown", "content_list", "images", "middle"}:
                continue
            assert teil.extras[schluessel] == wert, schluessel


def test_unbekannte_dateien_im_archiv_gehen_nicht_verloren() -> None:
    """Eine Datei im Tarball, die auf KEIN Muster passt, wird aufgehoben.

    Der zweite Teil derselben Regel, und der wahrscheinlichere Fall: Von den
    Dateinamen im Archiv ist genau EINER belegt (`{basename}_content_list.json`
    mit einem Versions-Rueckfall). Dass etwas Unbekanntes dabei ist, ist damit
    nicht der Sonderfall, sondern die wahrscheinlichste Lage.

    Vor der Behebung wurde eine solche Datei STILL verworfen — kein Fehler,
    keine Meldung, nur weg. Und zwar bezahlt weg.
    """
    puffer = io.BytesIO()
    dateien = {
        "doc/doc.md": b"# Text",
        "doc/doc_content_list.json": b'[{"type":"text","page_idx":0}]',
        "doc/doc_model.json": b'{"das":"kannten wir nicht"}',
        "doc/voellig_unbekannt.dat": b"\x00\x01 binaer",
    }
    with tarfile.open(fileobj=puffer, mode="w:gz") as archiv:
        for name, inhalt in dateien.items():
            info = tarfile.TarInfo(name)
            info.size = len(inhalt)
            archiv.addfile(info, io.BytesIO(inhalt))

    antwort = _antwort()
    antwort["output"]["results"][0] = {
        "basename": "doc",
        "pages_requested": -1,
        "tarball_b64": base64.b64encode(puffer.getvalue()).decode("ascii"),
    }

    teil = unpack_response(antwort, Chunk(first_page=0, last_page=0))

    # Die beiden unbekannten Dateien sind da — unter ihrem vollen Pfad im
    # Archiv und mit unveraendertem Inhalt.
    assert (
        base64.b64decode(teil.extras["doc/doc_model.json"])
        == b'{"das":"kannten wir nicht"}'
    )
    assert (
        base64.b64decode(teil.extras["doc/voellig_unbekannt.dat"])
        == b"\x00\x01 binaer"
    )
    # Und die Felder NEBEN dem Tarball ebenso.
    assert teil.extras["basename"] == "doc"
    assert teil.extras["pages_requested"] == -1


def test_echte_tarball_antwort_wird_vollstaendig_ausgepackt(
    echte_tarball_antwort: dict[str, Any],
) -> None:
    """Der Produktionspfad gegen eine ECHTE Antwort — nicht gegen eine gebaute.

    Bis zum 08.08.2026 gab es fuer `tarball_b64` kein Testmaterial, obwohl der
    Betrieb ausschliesslich diesen Transport faehrt. Der Auspacker war damit
    gegen eine VORSTELLUNG der Antwort geprueft und gegen nichts sonst. Alle
    Zusicherungen hier sind an dieser einen echten Antwort GEMESSEN, nicht
    hergeleitet.

    Was die Messung umgeworfen hat, steht ausfuehrlich an `TARBALL_FIXTURE` in
    `conftest.py`.
    """
    from mineru_ocr.assembly import unpack_response

    teil = unpack_response(echte_tarball_antwort, Chunk(first_page=0, last_page=0))

    # Die vier benannten Inhalte, mit den gemessenen Groessen.
    assert len(teil.markdown) == 666
    assert len(teil.content_list) == 13
    assert sorted(teil.middle) == ["_backend", "_version_name", "pdf_info"]
    assert len(teil.images) == 1
    bildname, bildbytes = next(iter(teil.images.items()))
    assert bildname.endswith(".jpg")
    # Wirklich ein JPEG, nicht bloss ein Feld mit Inhalt.
    assert bildbytes.startswith(b"\xff\xd8\xff")

    # GEMESSEN und danach AM QUELLTEXT ERKLAERT: Das Archiv traegt ein
    # layout-PDF, aber KEIN span-PDF — und das ist kein Zufall dieser Antwort.
    # `mineru/cli/common.py` setzt in JEDEM VLM-Pfad `f_draw_span_bbox = False`,
    # bedingungslos, ungeachtet der Vorgabe `True` in der Signatur von
    # `aio_do_parse`; die offizielle Doku sagt zu span.pdf „Only applicable to
    # pipeline backend". Wir fahren `vlm-auto-engine`.
    #
    # Der Test haelt damit fest, was fuer dieses Backend GILT, nicht bloss, was
    # einmal beobachtet wurde. Faellt er, ist entweder das Backend gewechselt
    # oder MinerU hat diese Ueberschreibung aufgegeben — beides gehoert bemerkt.
    assert teil.layout_pdf is not None
    assert teil.layout_pdf.startswith(b"%PDF")
    assert teil.span_pdf is None

    # Der Umschlag traegt alles ausser `output`, plus die Metadaten aus `output`.
    assert sorted(teil.envelope) == [
        "delayTime",
        "executionTime",
        "id",
        "output_meta",
        "status",
        "workerId",
    ]


def test_seitenversatz_gegen_ECHTE_mehrteilige_antworten(
    echte_mehrteilige_antworten: list[tuple[tuple[int, int], dict[str, Any]]],
) -> None:
    """Die fehleranfaelligste Rechnung des Pakets — endlich an echtem Material.

    Bis zum 09.08.2026 gab es fuer die Mehr-Teilstueck-Lage KEIN echtes
    Antwortmaterial: Die sechs konservierten Antworten sind alle einseitig mit
    `page_idx == 0`. Der Seitenversatz beim Zusammensetzen lief damit gegen
    erfundene Antworten — also gegen genau die Vorstellung, die er richtig
    umsetzen soll.

    Diese drei Antworten sind die echten Teilstuecke EINES Dokuments: ein
    12-seitiges Steuerdokument, in drei Stuecke zu 5, 5 und 2 Seiten geschnitten
    und einzeln durch den echten Endpoint geschickt.

    WAS SIE BEWEISEN: Jedes Teilstueck traegt `page_idx` LOKAL, beginnend bei 0.
    Das ist die Annahme, auf der die ganze Korrektur ruht — sie stand als „am
    Quelltext geprueft, 05.08.2026" im Vertrag, war aber nie an einer echten
    mehrteiligen Antwort nachgesehen. Ohne die Korrektur traege das Dokument
    dreimal eine „Seite 0", und weil die Liste dabei vollstaendig und plausibel
    aussieht, faellt es niemandem auf.
    """
    from mineru_ocr.assembly import unpack_response
    from mineru_ocr.chunking import verify_page_coverage

    teile = [
        unpack_response(antwort, Chunk(first_page=erste, last_page=letzte))
        for (erste, letzte), antwort in echte_mehrteilige_antworten
    ]

    # ERST der Befund ueber die Rohdaten: lokal, jeweils ab 0.
    lokal = [
        sorted({b["page_idx"] for b in t.content_list if "page_idx" in b})
        for t in teile
    ]
    assert lokal == [[0, 1, 2, 3, 4], [0, 1, 2, 3, 4], [0, 1]]

    # Die Abdeckung geht auf — dieselbe Pruefung, die der Executor fahrt.
    verify_page_coverage([t.chunk for t in teile], 12)

    # UND DANN das Ergebnis: lueckenlos die Seiten des ORIGINALS.
    ergebnis = assemble(teile)
    seiten = sorted({b["page_idx"] for b in ergebnis["content_list"]})
    assert seiten == list(range(12))
    # Die Gesamtzahl stimmt mit dem Lauf durch das Tool am 08.08.2026 ueberein.
    assert len(ergebnis["content_list"]) == 85
    assert is_empty(ergebnis) is False

    # Und der Doppeltreffer ist keine Eigenheit einzelner Antworten, sondern die
    # Regel: JEDES Teilstueck bringt sein eigenes v2 mit.
    assert all("doc_content_list_v2.json" in t.extras for t in teile)


def test_echte_leere_seite_ist_empty_und_nicht_kaputt(
    echte_leere_antwort: dict[str, Any],
) -> None:
    """Die ECHTE Antwort auf eine leere Seite — der Punkt, der „vorm bauen"
    haette geklaert werden sollen und am 08.08.2026 nachgeholt wurde.

    Gemessen: Die Engine meldet `COMPLETED` und `ok: true`. Das Archiv traegt
    `doc.md` mit 0 Bytes, `doc_content_list.json` mit `[]` und ein
    `doc_middle.json` mit leeren Blocklisten — und trotzdem ein `doc_layout.pdf`.
    Ein Layout-PDF ist also KEIN Hinweis auf Inhalt.

    Diese eine Antwort hat zwei Fehler ans Licht gebracht, die einander
    verdeckten und die dieser Test beide festhaelt:

    ERSTENS wurde die flache `content_list` (`[]`) von der v2-Fassung (`[[]]`)
    ueberschrieben, weil die Belegt-Pruefung den WAHRHEITSWERT statt der
    ANWESENHEIT fragte. Heraus kam `[[]]` — das falsche Format, still
    eingesetzt. Bei einer nicht leeren v2-Fassung waere daraus in `assemble`
    ueber `dict(eintrag)` Unsinn geworden.

    ZWEITENS haette die Vollstaendigkeitspruefung am Ende von `_aus_tarball`
    (`if not markdown and not content_list: raise`) eine echt leere Seite fuer
    ein kaputtes Archiv gehalten — und `failed` statt `empty` bedeutet
    Wiedervorlage und erneutes Bezahlen. Genau der Fehler, wegen dessen `empty`
    ueberhaupt ein eigener Endzustand ist.
    """
    from mineru_ocr.assembly import unpack_response

    teil = unpack_response(echte_leere_antwort, Chunk(first_page=0, last_page=0))

    # Kein Wurf — eine leere Seite ist ein gueltiges Ergebnis, kein Formfehler.
    assert teil.markdown == ""
    # Das FLACHE Format, leer — nicht `[[]]` aus der v2-Fassung.
    assert teil.content_list == []
    assert teil.images == {}
    # Die v2-Fassung ist trotzdem aufgehoben.
    assert "doc_content_list_v2.json" in teil.extras
    # Und das Layout-PDF ist da, obwohl die Seite leer ist.
    assert teil.layout_pdf is not None

    ergebnis = assemble([teil])
    assert is_empty(ergebnis) is True


def test_echte_tarball_antwort_traegt_zwei_content_list_generationen(
    echte_tarball_antwort: dict[str, Any],
) -> None:
    """DER DOPPELTREFFER IST ECHT — und er kam beim allerersten Lauf.

    Das Archiv enthaelt `doc_content_list.json` UND `doc_content_list_v2.json`,
    und beide treffen dasselbe Zuordnungsmuster (`_content_list` + `.json`). Vor
    der Behebung haette die zuletzt gelesene die zuerst gelesene ersetzt, und die
    Verliererin waere ERSATZLOS weg gewesen — nicht im Feld, nicht in `extras`,
    einfach fort, bezahlt und ohne Fehlersignal.

    Der Fall galt bis dahin als hypothetisch: Das Modul raeumte ein, von den
    Dateinamen sei nur einer belegt, und behandelte einen Doppeltreffer deshalb
    als denkbar. Er ist nicht denkbar, er ist der Normalfall.

    Und die beiden sind NICHT zwei Generationen desselben Formats, wie die
    Dokumentation annahm („`package.py` sucht `{basename}_content_list.json` und
    faellt auf `{basename}_content_list_v2.json` zurueck"). Sie sind zwei
    verschiedene Formate: Die genommene ist flach mit `page_idx` je Block, die
    andere nach Seiten geschachtelt mit `title`/`paragraph`/`table` und
    zusaetzlicher Strukturinformation.
    """
    from mineru_ocr.assembly import unpack_response

    teil = unpack_response(echte_tarball_antwort, Chunk(first_page=0, last_page=0))

    # Die Verliererin des Doppeltreffers ist da, unter ihrem Archivnamen.
    assert "doc_content_list_v2.json" in teil.extras
    v2 = json.loads(base64.b64decode(teil.extras["doc_content_list_v2.json"]))

    # Und sie ist wirklich etwas anderes: geschachtelt statt flach.
    assert isinstance(v2, list) and len(v2) == 1
    innen = v2[0]
    assert isinstance(innen, list) and len(innen) == 13
    typen = {b["type"] for b in innen}
    assert typen == {"title", "paragraph", "table"}
    # Die genommene Fassung kennt diese Typen NICHT — sie nennt alles `text`.
    assert {b["type"] for b in teil.content_list} == {"text", "table"}

    # Auch die Felder neben dem Tarball sind aufgehoben. Die Werte sind
    # ABGELESEN und nicht erschlossen: `source` traegt `"b64"` (die Transportart)
    # und nicht etwa denselben Wert wie `basename` — genau diese Verwechslung
    # hat der Test beim ersten Lauf gefangen.
    assert teil.extras["basename"] == "doc"
    assert teil.extras["source"] == "b64"
    assert teil.extras["pages_requested"] == -1


def test_zwei_dateien_auf_demselben_muster_gehen_nicht_verloren() -> None:
    """Zwei `.md` im Archiv: eine fuellt das Feld, die andere steht in `extras`.

    Vorher gewann schlicht die zuletzt gelesene, und die frueher gelesene war
    ERSATZLOS weg — weder im Feld noch in `extras`. Das ist die unsichtbarste
    Verlustform des Moduls, und sie stand ausgerechnet an der Stelle, an der das
    Modul selbst einraeumt, von den Dateinamen im Archiv sei nur EINER belegt:
    Unter erklaerter Unkenntnis ueber die Namen ist ein Doppeltreffer kein
    Sonderfall.

    Geprueft wird ausdruecklich NICHT, welche der beiden gewinnt — das ist eine
    willkuerliche Festlegung (die zuerst gelesene) und wird erst durch einen
    echten Tarball-Lauf beantwortbar. Geprueft wird, dass BEIDE Inhalte danach
    noch da sind.
    """
    puffer = io.BytesIO()
    dateien = {
        "doc/doc.md": b"# Erstes Markdown",
        "doc/anhang_notizen.md": b"# Zweites Markdown mit echtem Inhalt",
        "doc/doc_content_list.json": b'[{"type":"text","page_idx":0}]',
        "doc/doc_content_list_v2.json": b'[{"type":"text","text":"v2","page_idx":0}]',
    }
    with tarfile.open(fileobj=puffer, mode="w:gz") as archiv:
        for name, inhalt in dateien.items():
            info = tarfile.TarInfo(name)
            info.size = len(inhalt)
            archiv.addfile(info, io.BytesIO(inhalt))

    antwort = _antwort()
    antwort["output"]["results"][0] = {
        "basename": "doc",
        "tarball_b64": base64.b64encode(puffer.getvalue()).decode("ascii"),
    }

    teil = unpack_response(antwort, Chunk(first_page=0, last_page=0))

    # Beide Markdown-Inhalte sind auffindbar: einer im Feld, einer in `extras`.
    aufgehoben = {
        name: base64.b64decode(wert).decode("utf-8")
        for name, wert in teil.extras.items()
        if isinstance(wert, str) and name.endswith((".md", ".json"))
    }
    vorhanden = {teil.markdown, *aufgehoben.values()}
    assert "# Erstes Markdown" in vorhanden
    assert "# Zweites Markdown mit echtem Inhalt" in vorhanden

    # Dasselbe fuer die beiden content_list-Generationen. Welche der Doku-Fassung
    # entspricht, ist offen (`package.py` bevorzugt die ohne `_v2` und faellt
    # darauf zurueck) — verloren gehen darf keine.
    roh = json.dumps(teil.content_list) + " ".join(aufgehoben.values())
    assert '"v2"' in roh


def test_json_null_wird_nicht_als_none_durchgereicht() -> None:
    """Ein Feld mit dem JSON-Wert `null` verletzt die Typzusagen nicht.

    `ergebnis.get("markdown", "")` greift nur bei FEHLENDEM Schluessel. Steht der
    Schluessel mit `null` da — fuer eine leere Seite durchaus plausibel —, kam
    `None` heraus, gegen `ChunkArtifacts.markdown: str`. Der Schaden trat nicht
    hier auf, sondern erst beim Zusammensetzen: `"\\n\\n".join(...)` warf ueber
    dem `None` einen `TypeError`, das Dokument endete `failed` statt `empty`, und
    die Antwort war bezahlt und unbrauchbar — beim Wiederlauf genauso.

    Und die zweite Haelfte, ohne die es nur eine Verschleierung waere: Der
    `null`-Wert selbst geht nicht verloren, sondern steht in `extras`.
    """
    antwort = _antwort()
    antwort["output"]["results"][0] = {
        "basename": "doc",
        "markdown": None,
        "content_list": None,
        "images": None,
        "middle": None,
    }

    teil = unpack_response(antwort, Chunk(first_page=0, last_page=0))

    assert teil.markdown == ""
    assert teil.content_list == []
    assert teil.images == {}
    assert teil.middle == {}
    # Aufgehoben statt still ersetzt.
    assert teil.extras["markdown"] is None
    assert teil.extras["content_list"] is None

    # Und der eigentliche Punkt: Das Zusammensetzen laeuft jetzt durch, statt zu
    # werfen und das bezahlte Ergebnis mitzunehmen.
    ergebnis = assemble([teil])
    assert ergebnis["markdown"] == ""
    assert is_empty(ergebnis) is True


def test_tabelleninhalt_geht_nicht_verloren() -> None:
    """Der Inhalt eines `table`-Blocks steht in `table_body`, nicht in `content`.

    AM ECHTEN MATERIAL AUSGEZAEHLT (alle sechs konservierten Antworten): Ein
    `table`-Block traegt `bbox`, `img_path`, `page_idx`, `table_body`,
    `table_caption`, `table_footnote` — und KEIN `content`. Die frueherer Fassung
    von `_blocktext` las `text`, `list_items`, `content` und lieferte fuer einen
    Tabellenblock deshalb die LEERE Zeichenkette, obwohl in `table_body` die
    ganze Tabelle als HTML steht.

    WIE WEIT DER SCHADEN REICHT, und wie weit ausdruecklich nicht: Es liegt nahe,
    hier denselben Fall wie beim Aktenzeichen zu vermuten — bezahlter Text, der
    es nie in die Ausgabe schafft. Nachgemessen an den sechs Antworten stimmt das
    NICHT: MinerUs eigenes `markdown` traegt alle drei `table_body` bereits
    zeichengleich. `rendered_text` verliert also nichts, und dieser Test prueft
    es deshalb auch nicht.

    Der Schaden lag allein bei `is_empty`: Ein Dokument, dessen `markdown` leer
    ist und dessen einziger Inhalt eine Tabelle ist, galt als `empty` und waere
    laut Vertrag nie wieder angesehen worden.

    Der Block hier hat die Form des echten Materials — insbesondere OHNE
    `img_path`, denn genau der rettete den Fall bisher zufaellig.
    """
    tabelle = {
        "type": "table",
        "page_idx": 0,
        "table_body": (
            "<table><tr><td>Current share price (S)</td><td>473p</td></tr></table>"
        ),
        "table_caption": ["Tabelle 1: Eingangsgroessen"],
        "table_footnote": [],
    }
    ergebnis = assemble(
        [
            ChunkArtifacts(
                chunk=Chunk(first_page=0, last_page=0),
                markdown="",
                content_list=[tabelle],
                images={},
                middle={},
                layout_pdf=None,
                span_pdf=None,
                envelope={},
                extras={},
            )
        ]
    )

    # DAS ist der Befund: Ein Dokument, das nur aus dieser Tabelle besteht, ist
    # NICHT leer. Vorher war es `empty`.
    assert is_empty(ergebnis) is False


def test_blocktext_sammelt_alle_textfelder_statt_des_ersten_passenden() -> None:
    """`_blocktext` nimmt ALLES, was ein Block an Text traegt — nicht das erste.

    Am echten Material gemessen: Ein `chart`-Block traegt `content` mit einer
    Markdown-Tabelle UND `chart_caption` mit echtem Text („Figure 3.1 Cumulative
    standard normal distribution and density"). Die frueherer Fassung gab das
    ERSTE passende Feld zurueck (`content`) und liess die Unterschrift liegen.

    Geprueft wird `_blocktext` unmittelbar und nicht ueber `rendered_text`:
    `chart` ist kein Randblock, wird also von `rendered_text` gar nicht
    angehaengt — und muss es auch nicht, weil MinerUs Markdown beide Texte
    nachweislich schon traegt. Die Auswirkung des Sammelns liegt bei `is_empty`
    und bei jedem kuenftigen Leser dieser Funktion.
    """
    from mineru_ocr.assembly import _blocktext

    diagramm = {
        "type": "chart",
        "page_idx": 0,
        "content": "| Metric | Value |\n| --- | --- |\n| Area | 0.9374 |",
        "chart_caption": ["Figure 3.1 Cumulative standard normal distribution"],
        "chart_footnote": [],
    }
    text = _blocktext(diagramm)
    # BEIDES, nicht nur das erste passende Feld.
    assert "0.9374" in text
    assert "Figure 3.1 Cumulative standard normal distribution" in text


def test_extras_werden_je_teilstueck_gehalten() -> None:
    """Je Teilstueck eine eigene Abbildung, NICHT vereinigt.

    Die Schluessel wiederholen sich zwischen Teilstuecken — jedes hat sein
    eigenes `basename` und seine eigene `pages_requested`. Eine Vereinigung
    liesse alle bis auf die letzte fallen: derselbe stille Verlust eine Ebene
    hoeher.
    """
    ergebnis = assemble(
        [
            ChunkArtifacts(
                chunk=Chunk(first_page=0, last_page=0),
                markdown="a",
                content_list=[],
                images={},
                middle={},
                layout_pdf=None,
                span_pdf=None,
                envelope={},
                extras={"basename": "erstes", "pages_requested": -1},
            ),
            ChunkArtifacts(
                chunk=Chunk(first_page=1, last_page=1),
                markdown="b",
                content_list=[],
                images={},
                middle={},
                layout_pdf=None,
                span_pdf=None,
                envelope={},
                extras={"basename": "zweites", "pages_requested": -1},
            ),
        ]
    )
    assert ergebnis["extras"] == [
        {"basename": "erstes", "pages_requested": -1},
        {"basename": "zweites", "pages_requested": -1},
    ]
