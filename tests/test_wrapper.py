"""Die Hüllen — der Teil, dessen Zusicherungen wörtlich vom Auftraggeber stammen.

Deshalb steht diese Datei da, wo sie steht: Die Module, deren Zusicherungen der
Verfasser sich selbst hergeleitet hat, hatten von Anfang an Tests. Die beiden,
deren Verhalten Jurgen im Braindump wörtlich vorgegeben hat, hatten keine — also
war gemessen, was ausgedacht war, und ungemessen, was bestellt war.
"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path
from typing import Any

import psycopg
import pypdfium2
import pytest

from mineru_ocr import db
from mineru_ocr.config import Settings
from mineru_ocr.db import DocumentStatus, list_documents, write_terminal_async
from mineru_ocr.errors import IncompleteResultError, TooLargeError, UnsupportedTypeError
from mineru_ocr.wrapper import OCRMegaWrapper, OCRWrapper

pytestmark = pytest.mark.db

PNG_KOPF = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"


def _pdf(seiten: int, titel: str = "") -> bytes:
    """Ein PDF mit der gewuenschten Seitenzahl und wahlweise einem Titel.

    Der Titel dient dem Metadaten-Test: Zwei PDFs mit gleichem Seiteninhalt,
    aber verschiedenem Titel sind VERSCHIEDENE Bytes und damit verschiedene
    Dokumente — genau das prueft die Regel „kein metadaten-normalisierter Hash".
    """
    doc = pypdfium2.PdfDocument.new()
    try:
        for _ in range(seiten):
            doc.new_page(200.0, 300.0)
        puffer = io.BytesIO()
        doc.save(puffer)
        roh = puffer.getvalue()
    finally:
        doc.close()
    if titel:
        # Ein PDF-Kommentar HINTER der Kopfzeile: aendert die Bytes, nicht den
        # sichtbaren Inhalt und nicht die Magic Bytes.
        #
        # Hinter und nicht davor — der erste Anlauf setzte ihn davor, und
        # `mime.sniff_path` wies die Datei zu Recht ab: `%PDF-` muss an Position
        # 0 stehen, sonst ist es kein PDF. Das war ein Fehler im Fixture und
        # keiner in der Erkennung.
        kopf, rest = roh.split(b"\n", 1)
        roh = kopf + b"\n%" + titel.encode() + b"\n" + rest
    return roh


def _datei(tmp_path: Path, name: str, inhalt: bytes) -> Path:
    pfad = tmp_path / name
    pfad.parent.mkdir(parents=True, exist_ok=True)
    pfad.write_bytes(inhalt)
    return pfad


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


# --- Identitaet und Nichts-Tun -------------------------------------------


def test_gleicher_inhalt_gleiche_zeile(
    db_conn: psycopg.Connection[Any], settings: Settings, tmp_path: Path
) -> None:
    """Derselbe Inhalt ergibt EINE Zeile — der rohe Byte-Hash ist die Identitaet,
    gleich ob aus zwei Dateien oder aus Bytes.

    Wo die Bytes herkamen, merkt sich nichts: Die Datenbank kennt keine Pfade,
    also gibt es auch keinen Vermerk ueber einen zweiten.
    """
    inhalt = PNG_KOPF + b"gleicher inhalt"
    erste = OCRWrapper.from_path(_datei(tmp_path, "a.png", inhalt), db_conn, settings)
    zweite = OCRWrapper.from_path(
        _datei(tmp_path, "unterordner/b.png", inhalt), db_conn, settings
    )
    dritte = OCRWrapper.from_bytes(inhalt, db_conn, settings)

    assert erste.sha256 == zweite.sha256 == dritte.sha256
    assert len(list_documents(db_conn)) == 1
    assert dritte.row.notes == []


async def test_zweiter_aufruf_faellt_nicht_zurueck(
    db_conn: psycopg.Connection[Any],
    async_conn: psycopg.AsyncConnection[Any],
    settings: Settings,
    tmp_path: Path,
) -> None:
    """Eine fertige Zeile wird uebernommen, nicht ueberschrieben: „wenn ocrwrapper
    bereits OCR'd ist dann ist es eine noop"."""
    pfad = _datei(tmp_path, "fertig.png", PNG_KOPF + b"x")
    erste = OCRWrapper.from_path(pfad, db_conn, settings)
    await write_terminal_async(
        async_conn,
        sha256=erste.sha256,
        status=DocumentStatus.DONE,
        raw={"markdown": "schon erkannt", "content_list": [], "images": {}},
    )

    # Dieselbe Datei ein zweites Mal — und die Huelle ist SOFORT fertig.
    zweite = OCRWrapper.from_path(pfad, db_conn, settings)
    assert zweite.status is DocumentStatus.DONE
    assert zweite.success
    assert zweite["markdown"] == "schon erkannt"


def test_kein_metadaten_normalisierter_hash(
    db_conn: psycopg.Connection[Any], settings: Settings, tmp_path: Path
) -> None:
    """Zwei PDFs mit gleichem Seiteninhalt, aber verschiedenen Bytes sind ZWEI
    Zeilen — es wird nichts wegnormalisiert.

    Ein Normalisierungsfehler waere der unsichtbarste Fehler ueberhaupt: „gilt als
    fertig, wurde nie erkannt". Deshalb ist der Hash der ROHE Byte-Hash, und
    deshalb prueft dieser Test, dass er es geblieben ist.
    """
    ohne = OCRWrapper.from_path(
        _datei(tmp_path, "ohne.pdf", _pdf(2)), db_conn, settings
    )
    mit = OCRWrapper.from_path(
        _datei(tmp_path, "mit.pdf", _pdf(2, titel="Zusatz")), db_conn, settings
    )
    assert ohne.sha256 != mit.sha256
    assert len(list_documents(db_conn)) == 2
    # Beide sind als PDF erkannt. Die Seitenzahl kennt erst der Job.
    assert ohne.mime == mit.mime == "application/pdf"


def test_nicht_unterstuetzter_typ_wirft_sofort(
    db_conn: psycopg.Connection[Any], settings: Settings, tmp_path: Path
) -> None:
    """`from_path` wirft beim BAUEN, nicht erst beim Verarbeiten — und legt keine
    Zeile an."""
    pfad = _datei(tmp_path, "notizen.txt", b"Sehr geehrte Damen und Herren,\n")
    with pytest.raises(UnsupportedTypeError):
        OCRWrapper.from_path(pfad, db_conn, settings)
    assert list_documents(db_conn) == []


def test_einreihen_oeffnet_keine_pdf(
    db_conn: psycopg.Connection[Any], settings: Settings, tmp_path: Path
) -> None:
    """Eine kaputte PDF bricht das Einreihen nicht ab: Sie wird abgelegt, ohne
    Seitenzahl, und endet im Job als `input_error`.

    Der Beweis, dass nichts geoeffnet wurde: Bytes, die nach PDF aussehen und
    sich nicht oeffnen lassen, kommen durch.
    """
    kaputt = b"%PDF-1.7\nund dann nur noch Unsinn\n"
    aus_datei = OCRWrapper.from_path(_datei(tmp_path, "k.pdf", kaputt), db_conn, settings)
    aus_bytes = OCRWrapper.from_bytes(kaputt + b"2", db_conn, settings)
    gut = OCRWrapper.from_bytes(_pdf(4), db_conn, settings)

    for huelle in (aus_datei, aus_bytes, gut):
        assert huelle.status is DocumentStatus.NEW
        assert huelle.page_count is None


def _eingabe(conn: psycopg.Connection[Any], sha256: str) -> bytes | None:
    """Die Bytes in `inputs` zu diesem Hash, oder None, wenn es keine gibt."""
    zeile = conn.execute(
        "SELECT content FROM inputs WHERE sha256 = %s", (sha256,)
    ).fetchone()
    return None if zeile is None else bytes(zeile[0])


def test_abschicken_legt_die_bytes_in_inputs_ab(
    db_conn: psycopg.Connection[Any], settings: Settings, tmp_path: Path
) -> None:
    """Aus einer Datei wie aus Bytes liegen die Bytes danach in `inputs`, unter
    dem Hash, aus dem die Zeile entstand."""
    aus_datei = PNG_KOPF + b"aus einer datei"
    aus_bytes = PNG_KOPF + b"aus dem speicher"

    a = OCRWrapper.from_path(_datei(tmp_path, "a.png", aus_datei), db_conn, settings)
    b = OCRWrapper.from_bytes(aus_bytes, db_conn, settings)

    assert _eingabe(db_conn, a.sha256) == aus_datei
    assert _eingabe(db_conn, b.sha256) == aus_bytes


def test_zweites_abschicken_aendert_die_eingabe_nicht(
    db_conn: psycopg.Connection[Any], settings: Settings, tmp_path: Path
) -> None:
    """Derselbe Inhalt ein zweites Mal legt keine zweite Eingabe an."""
    inhalt = PNG_KOPF + b"einmal"
    erste = OCRWrapper.from_path(_datei(tmp_path, "a.png", inhalt), db_conn, settings)
    OCRWrapper.from_path(_datei(tmp_path, "b.png", inhalt), db_conn, settings)
    OCRWrapper.from_bytes(inhalt, db_conn, settings)

    assert _eingabe(db_conn, erste.sha256) == inhalt
    zaehlung = db_conn.execute("SELECT count(*) FROM inputs").fetchone()
    assert zaehlung is not None and zaehlung[0] == 1


async def test_fertiges_dokument_bekommt_keine_neue_eingabe(
    db_conn: psycopg.Connection[Any],
    async_conn: psycopg.AsyncConnection[Any],
    settings: Settings,
    tmp_path: Path,
) -> None:
    """Ist das Dokument fertig, ist seine Eingabe weg — und erneutes Abschicken
    bringt sie nicht zurueck. Sonst bliebe ein Treffer als Leiche in `inputs`
    liegen, und \"offen\" liesse sich an der Eingabe nicht mehr ablesen."""
    pfad = _datei(tmp_path, "fertig.png", PNG_KOPF + b"fertig")
    erste = OCRWrapper.from_path(pfad, db_conn, settings)
    await write_terminal_async(
        async_conn, sha256=erste.sha256, status=DocumentStatus.DONE, raw={}
    )
    assert _eingabe(db_conn, erste.sha256) is None

    OCRWrapper.from_path(pfad, db_conn, settings)
    assert _eingabe(db_conn, erste.sha256) is None


def test_nicht_unterstuetzter_typ_hinterlaesst_keine_eingabe(
    db_conn: psycopg.Connection[Any], settings: Settings, tmp_path: Path
) -> None:
    """Was abgewiesen wird, wird nicht abgelegt."""
    with pytest.raises(UnsupportedTypeError):
        OCRWrapper.from_path(_datei(tmp_path, "n.txt", b"text"), db_conn, settings)
    with pytest.raises(UnsupportedTypeError):
        OCRWrapper.from_bytes(b"nur text", db_conn, settings)
    zaehlung = db_conn.execute("SELECT count(*) FROM inputs").fetchone()
    assert zaehlung is not None and zaehlung[0] == 0


def test_nicht_unterstuetzter_typ_wird_abgewiesen_ohne_die_datei_zu_lesen(
    db_conn: psycopg.Connection[Any],
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Eine grosse Datei fremden Typs im Ordner darf nicht erst ganz in den
    Speicher, um dann abgewiesen zu werden; die Meldung nennt ausserdem ihren
    Namen, sonst weiss man bei mehreren Dateien nicht, welche gemeint ist."""
    pfad = _datei(tmp_path, "film.mov", b"\x00\x00\x00\x14ftypqt  " + b"x" * 100)

    def nie(self: Path) -> bytes:
        raise AssertionError("die Datei wurde ganz gelesen")

    monkeypatch.setattr(Path, "read_bytes", nie)
    with pytest.raises(UnsupportedTypeError, match="film.mov"):
        OCRWrapper.from_path(pfad, db_conn, settings)


def test_zu_grosse_eingabe_wird_mit_meldung_abgewiesen_und_nicht_abgelegt(
    db_conn: psycopg.Connection[Any],
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ueber `db.MAX_INPUT_BYTES` gibt es `TooLargeError` statt eines
    Verbindungsabbruchs beim Ablegen; die Datei wird dabei nicht gelesen, und
    es bleibt nichts liegen."""
    monkeypatch.setattr(db, "MAX_INPUT_BYTES", 50)
    inhalt = PNG_KOPF + b"x" * 100
    pfad = _datei(tmp_path, "gross.png", inhalt)

    def nie(self: Path) -> bytes:
        raise AssertionError("die Datei wurde gelesen")

    with monkeypatch.context() as m:
        m.setattr(Path, "read_bytes", nie)
        with pytest.raises(TooLargeError, match="gross.png"):
            OCRWrapper.from_path(pfad, db_conn, settings)
    with pytest.raises(TooLargeError):
        OCRWrapper.from_bytes(inhalt, db_conn, settings)

    assert list_documents(db_conn) == []
    zaehlung = db_conn.execute("SELECT count(*) FROM inputs").fetchone()
    assert zaehlung is not None and zaehlung[0] == 0


def test_zeile_und_eingabe_entstehen_zusammen_oder_gar_nicht(
    db_conn: psycopg.Connection[Any],
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scheitert das Ablegen der Eingabe, bleibt auch die Zeile nicht stehen: eine
    Zeile `new` ohne Eingabe koennte kein Job lesen."""

    def scheitert(*_: Any, **__: Any) -> bool:
        raise psycopg.OperationalError("Verbindung weg")

    monkeypatch.setattr(db, "insert_input", scheitert)
    with pytest.raises(psycopg.OperationalError):
        OCRWrapper.from_bytes(PNG_KOPF + b"atomar", db_conn, settings)

    assert list_documents(db_conn) == []


# --- Der Ordner-Fall ------------------------------------------------------


def test_refresh_dir_ueberspringt_zu_grosse_dateien_wie_from_dir(
    db_conn: psycopg.Connection[Any],
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`refresh_dir` hat dieselbe Auslassregel wie `from_dir`: Eine zu grosse Datei
    bricht das Nachsehen nicht ab."""
    ordner = tmp_path / "bestand"
    _datei(ordner, "a.png", PNG_KOPF + b"1")
    mega = OCRMegaWrapper.from_dir(ordner, db_conn, settings)
    monkeypatch.setattr(db, "MAX_INPUT_BYTES", 50)
    _datei(ordner, "b_gross.png", PNG_KOPF + b"x" * 100)
    _datei(ordner, "c_klein.png", PNG_KOPF + b"2")

    neue = mega.refresh_dir(ordner)

    assert [h.sha256 for h in neue] == [
        hashlib.sha256(PNG_KOPF + b"2").hexdigest()
    ]


def test_from_dir_ueberspringt_zu_grosse_dateien_und_reiht_den_rest_ein(
    db_conn: psycopg.Connection[Any],
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Eine Datei ueber der Grenze bricht den Ordner nicht ab: Sie wird gezaehlt,
    die uebrigen laufen weiter."""
    monkeypatch.setattr(db, "MAX_INPUT_BYTES", 50)
    ordner = tmp_path / "bestand"
    _datei(ordner, "a_gross.png", PNG_KOPF + b"x" * 100)
    _datei(ordner, "b_klein.png", PNG_KOPF)

    mega = OCRMegaWrapper.from_dir(ordner, db_conn, settings)

    assert len(mega) == 1
    assert mega.stats()["skipped"] == {"zu_gross": 1}


def test_from_dir_ueberspringt_und_zaehlt(
    db_conn: psycopg.Connection[Any], settings: Settings, tmp_path: Path
) -> None:
    """Nicht unterstuetzte Dateien beenden den Lauf nicht, sondern erscheinen
    gezaehlt und nach Grund aufgeschluesselt in `stats()`."""
    ordner = tmp_path / "bestand"
    _datei(ordner, "a.png", PNG_KOPF + b"1")
    _datei(ordner, "b.png", PNG_KOPF + b"2")
    _datei(ordner, ".DS_Store", b"Bud1\x00\x00\x00\x00")
    _datei(ordner, "notiz.txt", b"kein Dokument")

    mega = OCRMegaWrapper.from_dir(ordner, db_conn, settings)
    assert len(mega) == 2
    zahlen = mega.stats()
    assert zahlen["documents"] == 2
    assert zahlen["skipped"]["nicht_unterstuetzter_typ"] == 2


def test_from_paths_wirft_dagegen(
    db_conn: psycopg.Connection[Any], settings: Settings, tmp_path: Path
) -> None:
    """Wer eine Datei beim Namen nennt, meint sie — sie stillschweigend
    auszulassen waere die schlechtere Ueberraschung."""
    gut = _datei(tmp_path, "gut.png", PNG_KOPF + b"1")
    schlecht = _datei(tmp_path, "schlecht.txt", b"kein Dokument")
    with pytest.raises(UnsupportedTypeError):
        OCRMegaWrapper.from_paths([gut, schlecht], db_conn, settings)


def test_ordner_waechst(
    db_conn: psycopg.Connection[Any], settings: Settings, tmp_path: Path
) -> None:
    """Ein zweiter Durchlauf ueber dasselbe Verzeichnis mit einer neuen Datei
    liefert NUR die neue; Fertiges wird nicht angefasst.

    „dann später kommt zum ordner noch ein pfad hinzu den tut man einfach dazu
    und lässt nochmal durchlaufen weil eh nur läuft was noch fehlt"
    """
    ordner = tmp_path / "waechst"
    _datei(ordner, "a.png", PNG_KOPF + b"1")
    _datei(ordner, "b.png", PNG_KOPF + b"2")

    mega = OCRMegaWrapper.from_dir(ordner, db_conn, settings)
    assert len(mega) == 2

    _datei(ordner, "c.png", PNG_KOPF + b"3")
    neue = mega.refresh_dir(ordner)

    assert len(neue) == 1
    assert neue[0].sha256 == hashlib.sha256(PNG_KOPF + b"3").hexdigest()
    assert len(mega) == 3
    # Und in der Datenbank steht jedes genau einmal.
    assert len(list_documents(db_conn)) == 3


def test_abbruch_beim_einlesen_ist_unschaedlich(
    db_conn: psycopg.Connection[Any], settings: Settings, tmp_path: Path
) -> None:
    """Mittendrin abgebrochen und neu gestartet: nichts doppelt, nichts fehlt.

    Die Eigenschaft, auf der das ganze Ordner-Szenario beruht — und sie kostet
    nichts, weil der Hash der Primaerschluessel ist.
    """
    ordner = tmp_path / "abbruch"
    for nr in range(5):
        _datei(ordner, f"{nr}.png", PNG_KOPF + str(nr).encode())

    # Erster Lauf: nach zwei Dateien abgebrochen.
    class Abbruch(Exception):
        pass

    def nach_zwei(nummer: int, gesamt: int) -> None:
        if nummer == 2:
            raise Abbruch()

    with pytest.raises(Abbruch):
        OCRMegaWrapper.from_dir(ordner, db_conn, settings, progress=nach_zwei)
    assert len(list_documents(db_conn)) == 2

    # Zweiter Lauf: vollstaendig, und nichts doppelt.
    mega = OCRMegaWrapper.from_dir(ordner, db_conn, settings)
    assert len(mega) == 5
    assert len(list_documents(db_conn)) == 5


def test_fortschritt_wird_gemeldet(
    db_conn: psycopg.Connection[Any], settings: Settings, tmp_path: Path
) -> None:
    """`progress` wird je Datei gerufen. Bei zehntausend Dokumenten ist das
    Hashen minutenlang, und ohne Meldung sieht es aus wie ein Haenger."""
    ordner = tmp_path / "melden"
    for nr in range(4):
        _datei(ordner, f"{nr}.png", PNG_KOPF + str(nr).encode())

    meldungen: list[tuple[int, int]] = []
    OCRMegaWrapper.from_dir(
        ordner, db_conn, settings, progress=lambda n, g: meldungen.append((n, g))
    )
    assert meldungen == [(1, 4), (2, 4), (3, 4), (4, 4)]


async def test_uebersicht_und_sichten(
    db_conn: psycopg.Connection[Any],
    async_conn: psycopg.AsyncConnection[Any],
    settings: Settings,
    tmp_path: Path,
) -> None:
    """Zaehlung, Sichten und Textbericht — beide Haelften von Jurgens Forderung:
    „programmatisch und per text ausgebbar"."""
    ordner = tmp_path / "uebersicht"
    for nr in range(3):
        _datei(ordner, f"{nr}.png", PNG_KOPF + str(nr).encode())
    mega = OCRMegaWrapper.from_dir(ordner, db_conn, settings)

    ziele = [DocumentStatus.DONE, DocumentStatus.EMPTY, DocumentStatus.FAILED]
    for huelle, ziel in zip(list(mega), ziele, strict=True):
        await write_terminal_async(
            async_conn,
            sha256=huelle.sha256,
            status=ziel,
            raw={"markdown": "x", "content_list": [], "images": {}},
            error="kaputt" if ziel is DocumentStatus.FAILED else None,
        )
    mega.refresh()

    zaehlung = mega.status_counts()
    assert zaehlung[DocumentStatus.DONE] == 1
    assert zaehlung[DocumentStatus.EMPTY] == 1
    assert zaehlung[DocumentStatus.FAILED] == 1
    # Jeder Zustand kommt vor, auch mit Null — sonst sprengt ein fehlender
    # Schluessel die Ausgabe genau dann, wenn alles gut lief.
    assert set(zaehlung) == set(DocumentStatus)

    assert len(mega.done) == 2  # `empty` zaehlt als Erfolg
    assert len(mega.failed) == 1
    assert len(mega.pending) == 0

    text = mega.report(verbose=True)
    assert "3 Dokumente" in text
    assert "kaputt" in text


async def test_aufruest_faelle_erscheinen_im_bericht(
    db_conn: psycopg.Connection[Any],
    async_conn: psycopg.AsyncConnection[Any],
    settings: Settings,
    tmp_path: Path,
) -> None:
    """Ein aufruestbarer Fall wird im Bericht ausgewiesen — der vorgesehene Weg
    zu einem Aufgaben-Eintrag.

    Der Daemon legt NIE selbst einen an (Plan 4.1): Ein Hintergrundprozess, der
    Tickets erzeugt, ist ein Seiteneffekt, den niemand erwartet. Der Bericht
    zeigt ihn, der Mensch legt ihn an — und wenn der Bericht ihn nicht zeigt,
    passiert nie etwas.
    """
    pfad = _datei(tmp_path, "riesig.png", PNG_KOPF + b"gross")
    huelle = OCRWrapper.from_path(pfad, db_conn, settings)
    await write_terminal_async(
        async_conn,
        sha256=huelle.sha256,
        status=DocumentStatus.INPUT_ERROR,
        raw={},
        error="TooLargeError: … AUFRUESTBAR: Der Weg dafuer waere …",
    )

    mega = OCRMegaWrapper.from_paths([pfad], db_conn, settings)
    mega.refresh()
    assert mega.stats()["upgrade_cases"] == 1
    assert "OFFENE AUFRUEST-FAELLE" in mega.report()


# --- Ergebnis -------------------------------------------------------------


async def test_success_gilt_auch_fuer_leer(
    db_conn: psycopg.Connection[Any],
    async_conn: psycopg.AsyncConnection[Any],
    settings: Settings,
    tmp_path: Path,
) -> None:
    """`empty` zaehlt als Erfolg: Eine leere Seite wurde erfolgreich als leer
    erkannt. „\\"success\\" ist eher gemeint also ob das ocr geklappt hat"."""
    pfad = _datei(tmp_path, "leer.png", PNG_KOPF + b"leer")
    huelle = OCRWrapper.from_path(pfad, db_conn, settings)
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
    tmp_path: Path,
) -> None:
    """`markdown`, `content_list` und `images` kommen aus dem gespeicherten
    Gesamtergebnis — hier wird nichts mehr zusammengesetzt."""
    import base64

    pfad = _datei(tmp_path, "voll.png", PNG_KOPF + b"voll")
    huelle = OCRWrapper.from_path(pfad, db_conn, settings)
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
    tmp_path: Path,
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
    pfad = _datei(tmp_path, "ohne_bilder.png", PNG_KOPF + b"ohne")
    huelle = OCRWrapper.from_path(pfad, db_conn, settings)
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
    db_conn: psycopg.Connection[Any], settings: Settings, tmp_path: Path
) -> None:
    """Ein Dokument ohne Endergebnis liefert NICHTS und sagt es.

    Still einen leeren Text zu liefern waere der Fehler, den niemand bemerkt:
    Er saehe aus wie ein leeres Dokument.
    """
    pfad = _datei(tmp_path, "neu.png", PNG_KOPF + b"neu")
    huelle = OCRWrapper.from_path(pfad, db_conn, settings)
    with pytest.raises(IncompleteResultError):
        huelle["markdown"]
    with pytest.raises(IncompleteResultError):
        huelle["content_list"]


async def test_gelistete_zeile_laedt_raw_nach(
    db_conn: psycopg.Connection[Any],
    async_conn: psycopg.AsyncConnection[Any],
    settings: Settings,
    tmp_path: Path,
) -> None:
    """Eine Huelle aus `list_documents` (ohne `raw`) liefert fuer ein FERTIGES
    Dokument den Text — sie laedt die Zeile nach, statt leer zu antworten.

    Ohne diese Nachladung waere `mega.refresh()` (eine Abfrage fuer alle) mit den
    Ergebnis-Gettern unvertraeglich, und der Fehler saehe aus wie ein leeres
    Ergebnis.
    """
    pfad = _datei(tmp_path, "nachladen.png", PNG_KOPF + b"n")
    huelle = OCRWrapper.from_path(pfad, db_conn, settings)
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
    tmp_path: Path,
) -> None:
    """Gerade dann will man sehen, was ankam — dort steht auch der
    Fehler-Umschlag."""
    pfad = _datei(tmp_path, "kaputt.png", PNG_KOPF + b"k")
    huelle = OCRWrapper.from_path(pfad, db_conn, settings)
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
    tmp_path: Path,
) -> None:
    """Nach `refresh()` kommen die Getter aus der neuen Zeile."""
    pfad = _datei(tmp_path, "wechselt.png", PNG_KOPF + b"w")
    huelle = OCRWrapper.from_path(pfad, db_conn, settings)
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
    tmp_path: Path,
) -> None:
    """Die Voreinstellung haengt die Randbloecke an; `furniture=False` liefert
    das nackte Markdown der Engine.

    KORREKTUR EINER ZUSICHERUNG AUS PHASE A: Hier stand urspruenglich, das
    Ergebnis sei mit den Voreinstellungen zeichengleich mit `markdown`. Das ist
    messbar falsch — MinerUs Markdown filtert fuenf Blockklassen heraus, und in
    genau ihnen liegt das Aktenzeichen.
    """
    pfad = _datei(tmp_path, "rand.png", PNG_KOPF + b"r")
    huelle = OCRWrapper.from_path(pfad, db_conn, settings)
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
    db_conn: psycopg.Connection[Any], settings: Settings, tmp_path: Path
) -> None:
    """Rund 0,1 MB je Seite haben in einer Statusausgabe nichts zu suchen."""
    pfad = _datei(tmp_path, "d.png", PNG_KOPF + b"d")
    huelle = OCRWrapper.from_path(pfad, db_conn, settings)
    abbildung = huelle.to_dict()
    assert "raw" not in abbildung
    assert abbildung["sha256"] == huelle.sha256
    assert abbildung["status"] == "new"
    # Und alles ist JSON-faehig — die Ausgabe mit `--json` ist der Zweck.
    import json

    assert json.loads(json.dumps(abbildung)) == abbildung


def test_zugriff_ueber_position_und_hash(
    db_conn: psycopg.Connection[Any], settings: Settings, tmp_path: Path
) -> None:
    """Die Sammlung ist ueber die Position UND ueber den Hash ansprechbar."""
    ordner = tmp_path / "zugriff"
    for nr in range(3):
        _datei(ordner, f"{nr}.png", PNG_KOPF + str(nr).encode())
    mega = OCRMegaWrapper.from_dir(ordner, db_conn, settings)

    erste = mega[0]
    assert mega[erste.sha256] is erste
    assert len(list(mega)) == 3
    with pytest.raises(KeyError):
        mega["gibtesnicht"]


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
