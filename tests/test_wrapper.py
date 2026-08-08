"""Die Hüllen — der Teil, dessen Zusicherungen wörtlich vom Auftraggeber stammen.

Deshalb steht diese Datei da, wo sie steht: Die Module, deren Zusicherungen der
Verfasser sich selbst hergeleitet hat, hatten von Anfang an Tests. Die beiden,
deren Verhalten Jurgen im Braindump wörtlich vorgegeben hat, hatten keine — also
war gemessen, was ausgedacht war, und ungemessen, was bestellt war.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import psycopg
import pypdfium2
import pytest

from mineru_ocr.config import Settings
from mineru_ocr.db import DocumentStatus, list_documents, write_terminal_async
from mineru_ocr.errors import IncompleteResultError, UnsupportedTypeError
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


# --- Identitaet und Nichts-Tun -------------------------------------------


def test_gleicher_inhalt_gleiche_zeile(
    db_conn: psycopg.Connection[Any], settings: Settings, tmp_path: Path
) -> None:
    """Dieselbe Datei unter zwei Pfaden ergibt EINE Zeile — der rohe Byte-Hash
    ist die Identitaet."""
    inhalt = PNG_KOPF + b"gleicher inhalt"
    erste = OCRWrapper.from_path(_datei(tmp_path, "a.png", inhalt), db_conn, settings)
    zweite = OCRWrapper.from_path(
        _datei(tmp_path, "unterordner/b.png", inhalt), db_conn, settings
    )

    assert erste.sha256 == zweite.sha256
    assert len(list_documents(db_conn)) == 1
    # Der ZUERST gesehene Pfad gewinnt — die zweite Anlage beruehrt die Zeile
    # nicht (`ON CONFLICT DO NOTHING`).
    assert zweite.source_path == erste.source_path


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
    assert zweite.markdown == "schon erkannt"


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
    # Und beide sind als PDF erkannt, mit derselben Seitenzahl.
    assert ohne.page_count == mit.page_count == 2


def test_nicht_unterstuetzter_typ_wirft_sofort(
    db_conn: psycopg.Connection[Any], settings: Settings, tmp_path: Path
) -> None:
    """`from_path` wirft beim BAUEN, nicht erst beim Verarbeiten — und legt keine
    Zeile an."""
    pfad = _datei(tmp_path, "notizen.txt", b"Sehr geehrte Damen und Herren,\n")
    with pytest.raises(UnsupportedTypeError):
        OCRWrapper.from_path(pfad, db_conn, settings)
    assert list_documents(db_conn) == []


def test_bytes_eingabe_landet_im_spool(
    db_conn: psycopg.Connection[Any], settings: Settings
) -> None:
    """`from_bytes` schreibt eine Spool-Datei, deren NAME der Hash ist.

    Derselbe Inhalt ergibt denselben Pfad — der zweite Nutzen der
    Hash-Identitaet. Und die Datei wird beim zweiten Mal NICHT neu geschrieben.
    """
    daten = PNG_KOPF + b"aus dem speicher"
    erste = OCRWrapper.from_bytes(daten, "speicher.png", db_conn, settings)

    spool = settings.spool_dir / f"{erste.sha256}.png"
    assert spool.exists()
    assert erste.source_path == spool
    zeitstempel = spool.stat().st_mtime_ns

    zweite = OCRWrapper.from_bytes(daten, "anderer name.png", db_conn, settings)
    assert zweite.sha256 == erste.sha256
    assert spool.stat().st_mtime_ns == zeitstempel


def test_unbrauchbare_bytes_hinterlassen_keine_spool_datei(
    db_conn: psycopg.Connection[Any], settings: Settings
) -> None:
    """Ein nicht unterstuetzter Typ darf keine Datei hinterlassen.

    Das Spool-Verzeichnis wird ausdruecklich NIE automatisch geleert — was dort
    landet, bleibt. Eine Datei zu einem Dokument, das es gar nicht gibt, laege
    dort also fuer immer.
    """
    with pytest.raises(UnsupportedTypeError):
        OCRWrapper.from_bytes(b"nur text", "x.txt", db_conn, settings)
    if settings.spool_dir.exists():
        assert list(settings.spool_dir.iterdir()) == []


# --- Der Ordner-Fall ------------------------------------------------------


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
    assert neue[0].source_name == "c.png"
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

    assert huelle.markdown == "Seite A\n\nSeite B"
    assert [e["page_idx"] for e in huelle.content_list] == [0, 1]
    # Die Bilder kommen als BYTES heraus, nicht als Base64 — in der Datenbank
    # liegen sie als Zeichenketten, weil jsonb keine Bytes kennt.
    assert huelle.images == {"bild.jpg": b"\xff\xd8\xff"}


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
        huelle.markdown
    with pytest.raises(IncompleteResultError):
        huelle.content_list


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
    assert aus_liste.markdown == "steht in raw"


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
        huelle.markdown
    # … `raw` nicht.
    assert huelle.raw["markdown"] == "das kam noch an"
    assert huelle.raw["failure"]["type"] == "TerminalRunPodError"


def test_bytes_werden_traege_gelesen(
    db_conn: psycopg.Connection[Any], settings: Settings, tmp_path: Path
) -> None:
    """`bytes` liest erst beim Zugriff vom Pfad; die Huelle haelt sie nicht.

    „pfad (damit die inputbytes nicht alle im ram liegen werden sie wenn
    gebraucht vom pfad kurz gelesen)" — geprueft daran, dass eine AENDERUNG der
    Datei beim naechsten Zugriff sichtbar ist.
    """
    pfad = _datei(tmp_path, "traege.png", PNG_KOPF + b"vorher")
    huelle = OCRWrapper.from_path(pfad, db_conn, settings)
    assert huelle.bytes == PNG_KOPF + b"vorher"

    pfad.write_bytes(PNG_KOPF + b"nachher")
    assert huelle.bytes == PNG_KOPF + b"nachher"


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
    assert huelle.markdown == "zweiter Stand"


async def test_rendered_text_haengt_randbloecke_an(
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

    assert huelle.rendered_text(furniture=False) == "Fliesstext."
    assert "Aktenzeichen 123/456" in huelle.rendered_text()


def test_to_dict_traegt_kein_raw(
    db_conn: psycopg.Connection[Any], settings: Settings, tmp_path: Path
) -> None:
    """Bis zu einem Megabyte je Seite haben in einer Statusausgabe nichts zu
    suchen."""
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
