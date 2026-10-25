"""Die Hüllen — der Teil, dessen Zusicherungen wörtlich vom Auftraggeber stammen.

Deshalb steht diese Datei da, wo sie steht: Die Module, deren Zusicherungen der
Verfasser sich selbst hergeleitet hat, hatten von Anfang an Tests. Die beiden,
deren Verhalten Jurgen im Braindump wörtlich vorgegeben hat, hatten keine — also
war gemessen, was ausgedacht war, und ungemessen, was bestellt war.
"""

from __future__ import annotations

import hashlib
from typing import Any

import psycopg
import pytest

from mineru_ocr import db
from mineru_ocr.config import Settings
from mineru_ocr.db import DocumentStatus, list_documents, write_terminal_async
from mineru_ocr.errors import IncompleteResultError
from mineru_ocr.wrapper import OCRWrapper

pytestmark = pytest.mark.db

PNG_KOPF = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"


def _huelle(
    conn: psycopg.Connection[Any], settings: Settings, inhalt: bytes
) -> OCRWrapper:
    """Eine Huelle zu einer frisch angelegten Zeile `new`, ohne das Abschicken.

    Legt Zeile und Eingabe mit den Funktionen von `db` an, so wie das Abschicken
    es tut, und liest die Zeile OHNE `raw`: genau die Huelle, die `get()` gibt.
    """
    sha256 = hashlib.sha256(inhalt).hexdigest()
    with conn.transaction():
        db.insert_document(
            conn,
            sha256=sha256,
            size_bytes=len(inhalt),
            mime="image/png",
            endpoint_id=settings.endpoint_id,
        )
        db.insert_input(conn, sha256=sha256, content=inhalt)
    zeile = db.get_document(conn, sha256, with_raw=False)
    assert zeile is not None
    return OCRWrapper.from_row(zeile, conn)


# --- Ergebnis -------------------------------------------------------------


async def test_success_gilt_auch_fuer_leer(
    db_conn: psycopg.Connection[Any],
    async_conn: psycopg.AsyncConnection[Any],
    settings: Settings,
) -> None:
    """`empty` zaehlt als Erfolg: Eine leere Seite wurde erfolgreich als leer
    erkannt. „\\"success\\" ist eher gemeint also ob das ocr geklappt hat"."""
    huelle = _huelle(db_conn, settings, PNG_KOPF + b"leer")
    await write_terminal_async(
        async_conn,
        sha256=huelle.sha256,
        status=DocumentStatus.EMPTY,
        raw={"markdown": "", "content_list": [], "images": {}},
    )
    huelle.refresh()
    assert huelle.status is DocumentStatus.EMPTY
    assert huelle.success is True


async def test_getter_liefern_das_fertige(
    db_conn: psycopg.Connection[Any],
    async_conn: psycopg.AsyncConnection[Any],
    settings: Settings,
) -> None:
    """`markdown`, `content_list` und `images` kommen aus dem gespeicherten
    Gesamtergebnis — hier wird nichts mehr zusammengesetzt."""
    import base64

    huelle = _huelle(db_conn, settings, PNG_KOPF + b"voll")
    await write_terminal_async(
        async_conn,
        sha256=huelle.sha256,
        status=DocumentStatus.DONE,
        raw={
            "markdown": "Seite A\n\nSeite B",
            "content_list": [
                {"type": "text", "text": "A", "page_idx": 0},
                {"type": "text", "text": "B", "page_idx": 1},
            ],
            "images": {"bild.jpg": base64.b64encode(b"\xff\xd8\xff").decode("ascii")},
        },
    )
    huelle.refresh()

    assert huelle["markdown"] == "Seite A\n\nSeite B"
    assert [e["page_idx"] for e in huelle["content_list"]] == [0, 1]
    # Die Bilder kommen als BYTES heraus, nicht als Base64 — in der Datenbank
    # liegen sie als Zeichenketten, weil jsonb keine Bytes kennt.
    assert huelle.images == {"bild.jpg": b"\xff\xd8\xff"}


async def test_fehlendes_images_wird_gemeldet_statt_leer_zu_liefern(
    db_conn: psycopg.Connection[Any],
    async_conn: psycopg.AsyncConnection[Any],
    settings: Settings,
) -> None:
    """Ein Ergebnis ohne `images` ist beschaedigt und wird gemeldet.

    WARUM DAS EIN EIGENER TEST IST UND NICHT EINE FEINHEIT: `{}` ist bei diesem
    Feld ein voellig gewoehnliches Ergebnis — zwei der sechs echten Antworten
    haben wirklich keine Bilder. Ein stiller Ersatzwert brachte den Schaden also
    in genau die Form, in der ihn niemand mehr erkennt: „dieses Dokument hat
    keine Bilder", ununterscheidbar von der Wahrheit.

    `_feld` hatte diesen Ersatzwert, begruendet mit „eine aeltere Fassung hat es
    nicht geschrieben". Nachgeprueft (10.08.2026): `assembly.assemble` schreibt
    `images` in jeder Fassung seit `0938725`, und alle acht Zeilen der
    Produktionsdatenbank tragen den Schluessel. Die Fassung, auf die sich die
    Duldung berief, hat es nie gegeben.
    """
    huelle = _huelle(db_conn, settings, PNG_KOPF + b"ohne")
    await write_terminal_async(
        async_conn,
        sha256=huelle.sha256,
        status=DocumentStatus.DONE,
        raw={"markdown": "Text.", "content_list": []},
    )
    huelle.refresh()

    with pytest.raises(IncompleteResultError) as fehler:
        _ = huelle.images
    assert "images" in str(fehler.value)


async def test_getter_werfen_ohne_ergebnis(
    db_conn: psycopg.Connection[Any], settings: Settings
) -> None:
    """Ein Dokument ohne Endergebnis liefert NICHTS und sagt es.

    Still einen leeren Text zu liefern waere der Fehler, den niemand bemerkt:
    Er saehe aus wie ein leeres Dokument.
    """
    huelle = _huelle(db_conn, settings, PNG_KOPF + b"neu")
    with pytest.raises(IncompleteResultError):
        huelle["markdown"]
    with pytest.raises(IncompleteResultError):
        huelle["content_list"]


async def test_gelistete_zeile_laedt_raw_nach(
    db_conn: psycopg.Connection[Any],
    async_conn: psycopg.AsyncConnection[Any],
    settings: Settings,
) -> None:
    """Eine Huelle aus `list_documents` (ohne `raw`) liefert fuer ein FERTIGES
    Dokument den Text — sie laedt die Zeile nach, statt leer zu antworten.

    Ohne diese Nachladung waere `mega.refresh()` (eine Abfrage fuer alle) mit den
    Ergebnis-Gettern unvertraeglich, und der Fehler saehe aus wie ein leeres
    Ergebnis.
    """
    huelle = _huelle(db_conn, settings, PNG_KOPF + b"n")
    await write_terminal_async(
        async_conn,
        sha256=huelle.sha256,
        status=DocumentStatus.DONE,
        raw={"markdown": "steht in raw", "content_list": [], "images": {}},
    )

    # Der Weg ueber die Liste — dort fehlt `raw` absichtlich.
    gelistet = list_documents(db_conn)[0]
    assert gelistet.raw is None
    assert gelistet.raw_loaded is False

    aus_liste = OCRWrapper.from_row(gelistet, db_conn)
    assert aus_liste["markdown"] == "steht in raw"


async def test_raw_wirft_auch_bei_gescheiterten_nicht(
    db_conn: psycopg.Connection[Any],
    async_conn: psycopg.AsyncConnection[Any],
    settings: Settings,
) -> None:
    """Gerade dann will man sehen, was ankam — dort steht auch der
    Fehler-Umschlag."""
    huelle = _huelle(db_conn, settings, PNG_KOPF + b"k")
    await write_terminal_async(
        async_conn,
        sha256=huelle.sha256,
        status=DocumentStatus.FAILED,
        raw={"markdown": "das kam noch an", "failure": {"type": "TerminalRunPodError"}},
        error="TerminalRunPodError: aus",
    )
    huelle.refresh()

    # Die Getter werfen …
    with pytest.raises(IncompleteResultError):
        huelle["markdown"]
    # … `raw` nicht.
    assert huelle.raw["markdown"] == "das kam noch an"
    assert huelle.raw["failure"]["type"] == "TerminalRunPodError"


async def test_refresh_verwirft_die_zwischenspeicherung(
    db_conn: psycopg.Connection[Any],
    async_conn: psycopg.AsyncConnection[Any],
    settings: Settings,
) -> None:
    """Nach `refresh()` kommen die Getter aus der neuen Zeile."""
    huelle = _huelle(db_conn, settings, PNG_KOPF + b"w")
    await write_terminal_async(
        async_conn,
        sha256=huelle.sha256,
        status=DocumentStatus.FAILED,
        raw={"markdown": "erster Stand"},
    )
    huelle.refresh()
    assert huelle.raw["markdown"] == "erster Stand"

    await write_terminal_async(
        async_conn,
        sha256=huelle.sha256,
        status=DocumentStatus.DONE,
        raw={"markdown": "zweiter Stand", "content_list": [], "images": {}},
    )
    # OHNE refresh: der alte Stand. Die Huelle fragt die Datenbank nicht
    # heimlich bei jedem Zugriff — das machte aus einer Schleife ueber tausend
    # Dokumente unbemerkt zehntausend Abfragen.
    assert huelle.raw["markdown"] == "erster Stand"

    huelle.refresh()
    assert huelle["markdown"] == "zweiter Stand"


async def test_rendered_markdown_ist_das_ganze_dokument(
    db_conn: psycopg.Connection[Any],
    async_conn: psycopg.AsyncConnection[Any],
    settings: Settings,
) -> None:
    """Die Voreinstellung haengt die Randbloecke an; `furniture=False` liefert
    das nackte Markdown der Engine.

    KORREKTUR EINER ZUSICHERUNG AUS PHASE A: Hier stand urspruenglich, das
    Ergebnis sei mit den Voreinstellungen zeichengleich mit `markdown`. Das ist
    messbar falsch — MinerUs Markdown filtert fuenf Blockklassen heraus, und in
    genau ihnen liegt das Aktenzeichen.
    """
    huelle = _huelle(db_conn, settings, PNG_KOPF + b"r")
    await write_terminal_async(
        async_conn,
        sha256=huelle.sha256,
        status=DocumentStatus.DONE,
        raw={
            "markdown": "Fliesstext.",
            "content_list": [
                {"type": "text", "text": "Fliesstext.", "page_idx": 0},
                {"type": "header", "text": "Aktenzeichen 123/456", "page_idx": 0},
            ],
            "images": {},
        },
    )
    huelle.refresh()

    assert huelle["markdown"] == "Fliesstext."
    assert "Aktenzeichen 123/456" in huelle.rendered_markdown


def test_to_dict_traegt_kein_raw(
    db_conn: psycopg.Connection[Any], settings: Settings
) -> None:
    """Rund 0,1 MB je Seite haben in einer Statusausgabe nichts zu suchen."""
    huelle = _huelle(db_conn, settings, PNG_KOPF + b"d")
    abbildung = huelle.to_dict()
    assert "raw" not in abbildung
    assert abbildung["sha256"] == huelle.sha256
    assert abbildung["status"] == "new"
    # Und alles ist JSON-faehig — die Ausgabe mit `--json` ist der Zweck.
    import json

    assert json.loads(json.dumps(abbildung)) == abbildung


async def test_to_dict_mit_raw_traegt_das_vollstaendige_ergebnis(
    db_conn: psycopg.Connection[Any],
    async_conn: psycopg.AsyncConnection[Any],
    settings: Settings,
) -> None:
    """`to_dict(raw=True)` nimmt die vollstaendige Antwort mit — und laedt sie
    dafuer nach, wenn die Huelle sie noch nicht hat."""
    huelle = _huelle(db_conn, settings, PNG_KOPF + b"mit raw")
    raw = {"markdown": "Text.", "content_list": [], "images": {}, "extras": {"a": 1}}
    await write_terminal_async(
        async_conn, sha256=huelle.sha256, status=DocumentStatus.DONE, raw=raw
    )

    zeile = db.get_document(db_conn, huelle.sha256, with_raw=False)
    assert zeile is not None
    frisch = OCRWrapper.from_row(zeile, db_conn)
    assert frisch.row.raw_loaded is False

    abbildung = frisch.to_dict(raw=True)
    assert abbildung["raw"] == raw
    assert abbildung["sha256"] == huelle.sha256
    # Ohne den Schalter bleibt es dabei: kein `raw`.
    assert "raw" not in frisch.to_dict()
    assert "raw" not in frisch.to_dict(raw=False)


def test_to_dict_mit_raw_ohne_ergebnis_traegt_ein_leeres_raw(
    db_conn: psycopg.Connection[Any], settings: Settings
) -> None:
    """Ein Dokument ohne Ergebnis hat noch kein `raw`; `raw` selbst wirft dort
    nicht, und `to_dict(raw=True)` ebenso wenig."""
    huelle = _huelle(db_conn, settings, PNG_KOPF + b"ohne ergebnis")
    assert huelle.to_dict(raw=True)["raw"] == {}


def test_get_document_ohne_raw_laedt_raw_nicht(
    db_conn: psycopg.Connection[Any], settings: Settings
) -> None:
    """Der schmale Lesezugriff auf EIN Dokument: `raw` bleibt ungelesen und die
    Zeile sagt es (`raw_loaded`). Das Abschicken eines schon fertigen Dokuments
    darf nicht dessen ganze Antwort lesen."""
    huelle = _huelle(db_conn, settings, PNG_KOPF + b"schmal")
    zeile = db.get_document(db_conn, huelle.sha256, with_raw=False)
    assert zeile is not None
    assert zeile.raw is None
    assert zeile.raw_loaded is False
    voll = db.get_document(db_conn, huelle.sha256)
    assert voll is not None and voll.raw_loaded is True
