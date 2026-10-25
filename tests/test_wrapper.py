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
from mineru_ocr.client import AsyncOCRClient
from mineru_ocr.config import Settings
from mineru_ocr.db import DocumentStatus, list_documents, write_terminal_async
from mineru_ocr.errors import ConfigurationError, IncompleteResultError
from mineru_ocr.wrapper import OCRWrapper

pytestmark = pytest.mark.db

PNG_KOPF = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"


def _huelle(
    conn: psycopg.Connection[Any], settings: Settings, inhalt: bytes
) -> OCRWrapper:
    """Eine Huelle zu einem gescheiterten Ergebnis ohne `raw`, ohne Daemon.

    Eine Huelle gibt es nur zu einem Ergebnis, und die Zeile legt sonst erst der
    Job an. Hier steht sie von Hand als `failed` da, mit stehender Eingabe, wie
    der Job sie bei `failed` hinterlaesst: Die Tests schreiben ihr Ergebnis dann
    mit `write_terminal_async` darueber, denn `failed` darf ueberschrieben
    werden. Gelesen wird OHNE `raw`: genau die Huelle, die `get()` gibt.
    """
    sha256 = hashlib.sha256(inhalt).hexdigest()
    with conn.transaction():
        conn.execute(
            "INSERT INTO documents (sha256, size_bytes, mime, status, endpoint_id) "
            "VALUES (%s, %s, %s, 'failed', %s)",
            (sha256, len(inhalt), "image/png", settings.require_endpoint_id()),
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
        endpoint_id="testendpoint",
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
        endpoint_id="testendpoint",
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
        endpoint_id="testendpoint",
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

    Ohne diese Nachladung waere eine Huelle ohne `raw` (aus einer Abfrage, die es
    nicht mitliest) mit den Ergebnis-Gettern unvertraeglich, und der Fehler saehe
    aus wie ein leeres Ergebnis.
    """
    huelle = _huelle(db_conn, settings, PNG_KOPF + b"n")
    await write_terminal_async(
        async_conn,
        sha256=huelle.sha256,
        status=DocumentStatus.DONE,
        raw={"markdown": "steht in raw", "content_list": [], "images": {}},
        endpoint_id="testendpoint",
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
        endpoint_id="testendpoint",
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
        endpoint_id="testendpoint",
    )
    huelle.refresh()
    assert huelle.raw["markdown"] == "erster Stand"

    await write_terminal_async(
        async_conn,
        sha256=huelle.sha256,
        status=DocumentStatus.DONE,
        raw={"markdown": "zweiter Stand", "content_list": [], "images": {}},
        endpoint_id="testendpoint",
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
        endpoint_id="testendpoint",
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
    assert abbildung["status"] == "failed"
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
        async_conn, sha256=huelle.sha256, status=DocumentStatus.DONE, raw=raw, endpoint_id="testendpoint"
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


async def test_to_dict_mit_raw_beschreibt_dieselbe_zeile_wie_das_raw(
    db_conn: psycopg.Connection[Any],
    async_conn: psycopg.AsyncConnection[Any],
    settings: Settings,
) -> None:
    """Wird das Dokument zwischen `get` und `to_dict(raw=True)` neu fertig (ein
    `retry` ueber einem gescheiterten), kommt die Abbildung aus der Zeile, aus
    der auch `raw` stammt: nicht `status` `failed` und `success` False neben
    einer fertigen Antwort."""
    huelle = _huelle(db_conn, settings, PNG_KOPF + b"fertig dazwischen")
    raw = {"markdown": "Text.", "content_list": [], "images": {}}
    await write_terminal_async(
        async_conn, sha256=huelle.sha256, status=DocumentStatus.DONE, raw=raw, endpoint_id="testendpoint"
    )
    assert huelle.row.status is DocumentStatus.FAILED

    abbildung = huelle.to_dict(raw=True)

    assert abbildung["raw"] == raw
    assert abbildung["status"] == "done"
    assert abbildung["success"] is True


def test_to_dict_mit_raw_ohne_gespeichertes_raw_traegt_ein_leeres_raw(
    db_conn: psycopg.Connection[Any], settings: Settings
) -> None:
    """Eine Zeile ohne gespeichertes `raw` liefert `{}`; `raw` selbst wirft dort
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


# --- Huellen des asynchronen Clients --------------------------------------


async def test_huellen_des_async_clients_tragen_raw_geladen(
    db_conn: psycopg.Connection[Any],
    async_conn: psycopg.AsyncConnection[Any],
    settings: Settings,
) -> None:
    """Eine Eigenschaft kann nicht `await`en: Die Huellen des Async-Clients kommen
    deshalb mit geladenem `raw`, aus `get`, `wait`, `wait_all` und
    `as_completed`, und das Ergebnis ist auch nach dem Verlassen des Clients
    lesbar, ohne Verbindung."""
    huelle = _huelle(db_conn, settings, PNG_KOPF + b"async")
    await write_terminal_async(
        async_conn,
        sha256=huelle.sha256,
        status=DocumentStatus.DONE,
        raw={"markdown": "geladen", "content_list": [], "images": {}},
        endpoint_id="testendpoint",
    )

    async with AsyncOCRClient(db_dsn=settings.db_dsn) as klient:
        huellen = [
            await klient.get(huelle.sha256),
            await klient.wait(huelle.sha256, timeout=5.0),
            *await klient.wait_all([huelle.sha256], timeout=5.0),
            *[h async for h in klient.as_completed([huelle.sha256], timeout=5.0)],
        ]

    for geholt in huellen:
        assert geholt.row.raw_loaded is True
        assert geholt["markdown"] == "geladen"
        assert geholt.to_dict(raw=True)["raw"]["markdown"] == "geladen"


async def test_refresh_einer_huelle_des_async_clients_nennt_den_weg(
    db_conn: psycopg.Connection[Any],
    async_conn: psycopg.AsyncConnection[Any],
    settings: Settings,
) -> None:
    """Neu lesen kann eine Huelle des Async-Clients nicht selbst, denn sie haelt
    keine Verbindung. `refresh()` sagt das und nennt den Weg, statt an einer
    fehlenden Verbindung zu scheitern."""
    huelle = _huelle(db_conn, settings, PNG_KOPF + b"async refresh")
    await write_terminal_async(
        async_conn,
        sha256=huelle.sha256,
        status=DocumentStatus.FAILED,
        raw={},
        endpoint_id="testendpoint",
    )
    async with AsyncOCRClient(db_dsn=settings.db_dsn) as klient:
        geholt = await klient.get(huelle.sha256)

    with pytest.raises(ConfigurationError, match="await client.get"):
        geholt.refresh()
