"""Zuschnitt — die Regeln, an denen Geld haengt.

Alle Tests hier laufen OHNE eine einzige echte PDF-Datei: Die Schneidefunktion
wird hereingereicht, und eine erfundene, die Bytes passender Groesse liefert,
genuegt. Genau dafuer ist sie ein Parameter und kein Import.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from mineru_ocr.chunking import (
    Chunk,
    check_fits_unsplit,
    iter_chunks,
    needs_chunking,
    plan_chunks,
    split_in_half,
    verify_page_coverage,
)
from mineru_ocr.config import Settings
from mineru_ocr.errors import CorruptDocumentError, TooLargeError
from mineru_ocr.mime import DocumentType


class Schneider:
    """Eine erfundene Schneidefunktion, die ihre Aufrufe mitzaehlt.

    Die gelieferte Groesse ist die Summe der Einzelseiten-Groessen im Bereich.
    Damit laesst sich jeder Verlauf herstellen, den ein echtes PDF haben koennte
    — eine einzelne fette Seite zwischen lauter duennen ebenso wie ein
    gleichmaessiges Dokument —, und zwar ohne eine PDF-Bibliothek und ohne dass
    irgendetwas an der Groesse geraten waere.

    Warum eine Klasse mit `__call__` und keine geschachtelte Funktion: So ist
    das Ding zugleich die Schneidefunktion, die `iter_chunks` verlangt, UND
    traegt die Aufrufliste als eigenen Namen. Eine Funktion mit angehaengtem
    Zaehler-Attribut waere fuer mypy keine `Callable[[int, int], bytes]` mehr,
    und der Test muesste die Typpruefung an genau der Stelle abschalten, an der
    er die Schnittstelle nachbildet — also dort, wo sie am meisten wert ist.
    """

    def __init__(
        self, bytes_je_seite: dict[int, int] | None = None, grundwert: int = 100
    ) -> None:
        self.bytes_je_seite = bytes_je_seite if bytes_je_seite is not None else {}
        self.grundwert = grundwert
        self.aufrufe: list[tuple[int, int]] = []

    def __call__(self, erste: int, letzte: int) -> bytes:
        self.aufrufe.append((erste, letzte))
        groesse = sum(
            self.bytes_je_seite.get(seite, self.grundwert)
            for seite in range(erste, letzte + 1)
        )
        return b"x" * groesse


# --- Braucht dieses Dokument ueberhaupt einen Schnitt? --------------------


def test_kleines_dokument_braucht_keinen_schnitt(settings: Settings) -> None:
    """Unter beiden Schwellen ist `needs_chunking` falsch."""
    assert not needs_chunking(
        size_bytes=1_000_000,
        page_count=5,
        doc_type=DocumentType.PDF,
        settings=settings,
    )


def test_grosse_datei_braucht_schnitt(settings: Settings) -> None:
    """Ueber der Byte-Schwelle ist `needs_chunking` wahr, auch bei wenigen
    Seiten."""
    assert needs_chunking(
        size_bytes=settings.max_payload_bytes + 1,
        page_count=2,
        doc_type=DocumentType.PDF,
        settings=settings,
    )


def test_viele_seiten_brauchen_schnitt(settings: Settings) -> None:
    """Ueber der Seiten-Schwelle ist `needs_chunking` wahr, auch bei kleiner
    Datei.

    Der Fall, den man sonst uebersieht: 2 MB mit 200 Seiten geht muehelos hoch
    und bricht trotzdem das ANTWORT-Cap. Zwei unabhaengige Schwellen, zwei
    verschiedene Grenzen.
    """
    assert needs_chunking(
        size_bytes=2_000_000,
        page_count=200,
        doc_type=DocumentType.PDF,
        settings=settings,
    )


def test_genau_auf_der_schwelle_braucht_keinen_schnitt(settings: Settings) -> None:
    """Beide Schwellen sind EINSCHLIESSEND: genau `max_payload_bytes` und genau
    `max_pages_per_chunk` passen noch.

    Ohne diesen Test entscheidet der Zufall, ob irgendwo `<` oder `<=` steht,
    und der Unterschied faellt genau bei der Datei auf, die exakt auf der Grenze
    liegt — also selten und dann unerklaerlich.
    """
    assert not needs_chunking(
        size_bytes=settings.max_payload_bytes,
        page_count=settings.max_pages_per_chunk,
        doc_type=DocumentType.PDF,
        settings=settings,
    )


def test_nicht_pdf_braucht_nie_schnitt(settings: Settings) -> None:
    """Fuer Bilder und Office-Formate ist die Antwort immer falsch — nicht weil
    sie klein waeren, sondern weil sie sich nicht zerlegen lassen."""
    for typ in DocumentType:
        if typ.is_pdf:
            continue
        assert not needs_chunking(
            size_bytes=settings.max_payload_bytes * 10,
            page_count=None,
            doc_type=typ,
            settings=settings,
        ), typ


def test_pdf_ohne_seitenzahl_ist_programmierfehler(settings: Settings) -> None:
    """`page_count=None` bei PDF ergibt ValueError statt einer Antwort nach
    Bytegroesse allein.

    Die stille Alternative waere gefaehrlich: Ein 2-MB-PDF mit 200 Seiten
    bekaeme dann „braucht keinen Schnitt" und brechte das Antwort-Cap.
    """
    with pytest.raises(ValueError, match="page_count"):
        needs_chunking(
            size_bytes=1_000,
            page_count=None,
            doc_type=DocumentType.PDF,
            settings=settings,
        )


def test_zu_grosses_nicht_pdf_wirft_mit_hinweis(settings: Settings) -> None:
    """`check_fits_unsplit` wirft TooLargeError, und die Meldung nennt Groesse,
    Grenze UND den Aufruest-Hinweis (Festlegung 2.4)."""
    zu_gross = settings.max_payload_bytes + 1
    with pytest.raises(TooLargeError) as fehler:
        check_fits_unsplit(
            size_bytes=zu_gross, doc_type=DocumentType.DOCX, settings=settings
        )
    text = str(fehler.value)
    assert str(zu_gross) in text
    assert str(settings.max_payload_bytes) in text
    assert "AUFRUESTBAR" in text


def test_passendes_nicht_pdf_kehrt_still_zurueck(settings: Settings) -> None:
    """Der gute Fall ist die stille Rueckkehr, und die Grenze selbst passt noch."""
    check_fits_unsplit(
        size_bytes=settings.max_payload_bytes,
        doc_type=DocumentType.DOCX,
        settings=settings,
    )


# --- Der geplante Zuschnitt ----------------------------------------------


def test_plan_deckt_luecken_und_ueberlappungsfrei_ab(settings: Settings) -> None:
    """Fuer Seitenzahlen von 1 bis 500 gilt: `verify_page_coverage` wirft nie
    ueber dem Ergebnis von `plan_chunks`."""
    for seiten in range(1, 501):
        verify_page_coverage(plan_chunks(seiten, settings), seiten)


def test_letzter_block_ist_kuerzer(settings: Settings) -> None:
    """41 Seiten bei Schwelle 20 ergeben 20 + 20 + 1."""
    eng = dataclasses.replace(settings, max_pages_per_chunk=20)
    chunks = plan_chunks(41, eng)
    assert [(s.first_page, s.last_page) for s in chunks] == [(0, 19), (20, 39), (40, 40)]
    assert [s.page_count for s in chunks] == [20, 20, 1]


def test_plan_folgt_der_voreinstellung(settings: Settings) -> None:
    """45 Seiten bei der voreingestellten Schwelle von 20 ergeben 20 + 20 + 5."""
    assert [s.page_count for s in plan_chunks(45, settings)] == [20, 20, 5]


def test_plan_folgt_dem_eingestellten_wert(tmp_path: Path) -> None:
    """Die Seitenzahl je Teilstueck ist ein eingestellter Wert: aus der Datei,
    und die Umgebung schlaegt die Datei."""
    datei = tmp_path / "config.toml"
    datei.write_text("max_pages_per_chunk = 7\n")
    aus_datei = Settings.load(db_dsn="ungenutzt", environ={}, config_path=datei)
    assert [(s.first_page, s.last_page) for s in plan_chunks(15, aus_datei)] == [
        (0, 6), (7, 13), (14, 14)
    ]
    aus_umgebung = Settings.load(
        db_dsn="ungenutzt",
        environ={"MINERU_OCR_MAX_PAGES_PER_CHUNK": "15"},
        config_path=datei,
    )
    assert len(plan_chunks(15, aus_umgebung)) == 1


def test_plan_braucht_mindestens_eine_seite(settings: Settings) -> None:
    """Null oder weniger Seiten ist ein Programmierfehler, kein Eingabefehler."""
    for seiten in [0, -1]:
        with pytest.raises(ValueError):
            plan_chunks(seiten, settings)


# --- Halbieren ------------------------------------------------------------


def test_halbieren_erhaelt_die_abdeckung() -> None:
    """Die beiden Haelften decken zusammen genau denselben Bereich ab.

    Das ist die Invariante, die den ganzen Halbierungsvorgang sicher macht:
    Halbieren kann die Abdeckung nicht verletzen, egal wie oft es geschieht.
    """
    for erste, letzte in [(0, 1), (0, 4), (3, 8), (10, 11), (0, 99), (7, 100)]:
        ganz = Chunk(first_page=erste, last_page=letzte)
        links, rechts = split_in_half(ganz)
        assert links.first_page == ganz.first_page
        assert rechts.last_page == ganz.last_page
        assert rechts.first_page == links.last_page + 1
        assert links.page_count + rechts.page_count == ganz.page_count
        # Bei ungerader Seitenzahl bekommt die erste Haelfte die zusaetzliche.
        assert links.page_count >= rechts.page_count


def test_einzelne_seite_laesst_sich_nicht_halbieren() -> None:
    """`split_in_half` wirft ValueError bei einem einseitigen Chunk."""
    with pytest.raises(ValueError):
        split_in_half(Chunk(first_page=3, last_page=3))


# --- Die Gegenrechnung ----------------------------------------------------


def test_luecke_wird_erkannt() -> None:
    """Fehlt eine Seite zwischen zwei Chunks, wirft `verify_page_coverage`."""
    with pytest.raises(CorruptDocumentError, match="Luecke"):
        verify_page_coverage(
            [Chunk(first_page=0, last_page=3), Chunk(first_page=5, last_page=9)], 10
        )


def test_ueberlappung_wird_erkannt() -> None:
    """Ueberlappen zwei Chunks, wirft `verify_page_coverage`."""
    with pytest.raises(CorruptDocumentError, match="Ueberlappung"):
        verify_page_coverage(
            [Chunk(first_page=0, last_page=5), Chunk(first_page=4, last_page=9)], 10
        )


def test_luecke_und_ueberlappung_heben_sich_nicht_auf() -> None:
    """Eine Luecke UND eine gleich grosse Ueberlappung ergeben die richtige
    Gesamtsumme und muessen trotzdem erkannt werden.

    Der Grund, warum nicht nur die Summe geprueft wird: Das Ergebnis waere ein
    Dokument mit einer fehlenden und einer doppelten Seite — bei richtiger
    Gesamtzahl.
    """
    # 0..3 (4) + 5..8 (4) + 8..9 (2) = 10 Seiten. Seite 4 fehlt, Seite 8 doppelt.
    chunks = [
        Chunk(first_page=0, last_page=3),
        Chunk(first_page=5, last_page=8),
        Chunk(first_page=8, last_page=9),
    ]
    assert sum(s.page_count for s in chunks) == 10
    with pytest.raises(CorruptDocumentError, match="Luecke") as fehler:
        verify_page_coverage(chunks, 10)
    # Und die Meldung benennt die fehlende Seite, statt „die Summe stimmt nicht"
    # zu sagen — was hier ohnehin falsch waere, denn sie stimmt ja.
    assert "Seiten 4 bis 4 fehlen" in str(fehler.value)


def test_verschobene_abdeckung_wird_erkannt() -> None:
    """Ein lueckenloser, ueberschneidungsfreier Zuschnitt an der FALSCHEN Stelle
    wird erkannt.

    Der Fall, den die drei Pruefungen des Vertrags allein nicht faengt: (5, 9)
    und (10, 14) sind lueckenlos, ueberschneidungsfrei und summieren sich auf
    genau zehn Seiten — und decken trotzdem die falschen Seiten eines
    zehnseitigen Dokuments ab. Deshalb gibt es die vierte Pruefung.
    """
    chunks = [Chunk(first_page=5, last_page=9), Chunk(first_page=10, last_page=14)]
    assert sum(s.page_count for s in chunks) == 10
    with pytest.raises(CorruptDocumentError, match="Lage"):
        verify_page_coverage(chunks, 10)


def test_leerer_zuschnitt_wird_erkannt() -> None:
    """Keine Chunks fuer ein nicht leeres Dokument ist ein Befund, kein
    Sonderfall, der still durchgeht."""
    with pytest.raises(CorruptDocumentError):
        verify_page_coverage([], 10)


def test_meldung_nennt_die_betroffenen_seiten() -> None:
    """Die Meldung nennt die fehlenden Seitenzahlen — „Seitenzahlen stimmen
    nicht" allein hilft beim Suchen niemandem."""
    with pytest.raises(CorruptDocumentError) as fehler:
        verify_page_coverage(
            [Chunk(first_page=0, last_page=3), Chunk(first_page=7, last_page=9)], 10
        )
    text = str(fehler.value)
    assert "Seiten 4 bis 6 fehlen" in text
    # Und den ganzen Zuschnitt dazu, damit man den Fehler nicht aus einer
    # einzelnen Seitenzahl rekonstruieren muss.
    assert "(0, 3)" in text and "(7, 9)" in text


def test_unmoeglicher_bereich_laesst_sich_nicht_anlegen() -> None:
    """`Chunk(5, 3)` und `Chunk(-1, 3)` werfen schon beim Anlegen.

    Ein Chunk mit `last_page < first_page` haette eine NEGATIVE `page_count`,
    und negative Seitenzahlen rechnen sich in jeder Summe still weg — genau die
    Art Fehler, gegen die die Gegenrechnung ueberhaupt steht.
    """
    with pytest.raises(ValueError):
        Chunk(first_page=5, last_page=3)
    with pytest.raises(ValueError):
        Chunk(first_page=-1, last_page=3)
    # Der einseitige Chunk ist dagegen der Normalfall und muss durchgehen.
    assert Chunk(first_page=3, last_page=3).page_count == 1


# --- Der Durchlauf mit Byte-Korrektur -------------------------------------


def test_passende_chunks_gehen_unveraendert_durch(settings: Settings) -> None:
    """Bleibt jeder geschnittene Block unter der Grenze, wird der Plan
    unveraendert geliefert."""
    eng = dataclasses.replace(settings, max_payload_bytes=1_000, max_pages_per_chunk=4)
    plan = plan_chunks(10, eng)
    schneider = Schneider(grundwert=100)
    geliefert = list(iter_chunks(plan, schneider, eng))
    assert [(s.first_page, s.last_page) for s, _ in geliefert] == [
        (0, 3),
        (4, 7),
        (8, 9),
    ]


def test_zu_grosser_chunk_wird_halbiert(settings: Settings) -> None:
    """Liefert die Schneidefunktion zu viele Bytes, wird der Chunk geteilt und
    beide Haelften geliefert."""
    eng = dataclasses.replace(settings, max_payload_bytes=1_000, max_pages_per_chunk=4)
    plan = [Chunk(first_page=0, last_page=3)]
    # 4 Seiten mal 300 = 1200 > 1000; je zwei Seiten mal 300 = 600 passt.
    schneider = Schneider(grundwert=300)
    geliefert = list(iter_chunks(plan, schneider, eng))
    assert [(s.first_page, s.last_page) for s, _ in geliefert] == [(0, 1), (2, 3)]


def test_jeder_gelieferte_chunk_haelt_die_bytegrenze(settings: Settings) -> None:
    """Ueber verschiedene erfundene Groessenverlaeufe: kein geliefertes Stueck
    ist groesser als die Grenze."""
    eng = dataclasses.replace(settings, max_payload_bytes=1_000, max_pages_per_chunk=8)
    verlaeufe: list[dict[int, int]] = [
        {},  # gleichmaessig
        {0: 900},  # eine fette Seite ganz vorn
        {7: 900},  # eine fette Seite ganz hinten
        {3: 700, 4: 700},  # zwei fette Seiten in der Mitte
        {seite: 400 for seite in range(8)},  # durchweg dick
    ]
    for verlauf in verlaeufe:
        plan = plan_chunks(8, eng)
        schneider = Schneider(verlauf, 100)
        for chunk, daten in iter_chunks(plan, schneider, eng):
            assert len(daten) <= eng.max_payload_bytes, (verlauf, chunk)


def test_abdeckung_bleibt_nach_dem_halbieren_vollstaendig(settings: Settings) -> None:
    """Die gelieferten Chunks decken das Dokument weiterhin genau ab — das ist
    die Zusicherung, die aus der Invariante von `split_in_half` folgt."""
    eng = dataclasses.replace(settings, max_payload_bytes=1_000, max_pages_per_chunk=8)
    plan = plan_chunks(16, eng)
    schneider = Schneider({3: 700, 4: 700, 11: 950}, 100)
    geliefert = [chunk for chunk, _ in iter_chunks(plan, schneider, eng)]
    verify_page_coverage(geliefert, 16)


def test_einzelseite_ueber_der_grenze_wirft(settings: Settings) -> None:
    """Ist eine einzelne Seite auch allein zu gross, wirft `iter_chunks`
    TooLargeError — moeglicherweise NACHDEM schon Chunks geliefert wurden.

    Das ist gewollt: Der Aufrufer schreibt dann `input_error` samt der bereits
    erhaltenen Antworten, es geht nichts Bezahltes verloren.
    """
    eng = dataclasses.replace(settings, max_payload_bytes=1_000, max_pages_per_chunk=4)
    plan = plan_chunks(8, eng)
    # Seite 6 ist allein zu gross. Die Seiten 0 bis 3 gehen vorher sauber durch.
    schneider = Schneider({6: 5_000}, 100)
    geliefert: list[Chunk] = []
    with pytest.raises(TooLargeError) as fehler:
        for chunk, _ in iter_chunks(plan, schneider, eng):
            geliefert.append(chunk)
    # Vor dem Wurf wurde bereits geliefert — genau der bezahlte Teil, der in der
    # `input_error`-Zeile landet.
    assert geliefert
    # Und die Meldung nennt die Seite, sonst sucht man sie in einem
    # 200-Seiten-Dokument von Hand.
    assert "Seite 6" in str(fehler.value)
    assert "AUFRUESTBAR" in str(fehler.value)


def test_reihenfolge_bleibt_aufsteigend(settings: Settings) -> None:
    """Auch nach mehrfachem Halbieren kommen die Stuecke in aufsteigender
    Seitenreihenfolge."""
    eng = dataclasses.replace(settings, max_payload_bytes=1_000, max_pages_per_chunk=16)
    plan = plan_chunks(32, eng)
    # Grundwert 200 zwingt 16er-Bloecke (3200) durch mehrere Halbierungen bis
    # auf Vierer-Bloecke (800) hinunter.
    schneider = Schneider(grundwert=200)
    erste_seiten = [chunk.first_page for chunk, _ in iter_chunks(plan, schneider, eng)]
    assert erste_seiten == sorted(erste_seiten)
    assert erste_seiten == [0, 4, 8, 12, 16, 20, 24, 28]


def test_es_lebt_immer_nur_ein_chunk(settings: Settings) -> None:
    """Der Generator schneidet erst beim naechsten Abruf, nicht vorab.

    Geprueft durch Zaehlen der Aufrufe der Schneidefunktion. Ohne diese
    Eigenschaft haelt ein 200-Seiten-Dokument 70 MB im Speicher — mal
    vierundzwanzig gleichzeitig betreute Dokumente.
    """
    eng = dataclasses.replace(settings, max_payload_bytes=1_000, max_pages_per_chunk=4)
    plan = plan_chunks(20, eng)
    schneider = Schneider(grundwert=100)

    lauf = iter_chunks(plan, schneider, eng)
    # Das blosse Erzeugen des Generators schneidet noch gar nichts.
    assert schneider.aufrufe == []

    next(lauf)
    assert schneider.aufrufe == [(0, 3)]

    next(lauf)
    assert schneider.aufrufe == [(0, 3), (4, 7)]

    # Und nach dem vollstaendigen Durchlauf ist jeder Block genau einmal
    # geschnitten worden — kein Block wird zweimal bezahlt.
    list(lauf)
    assert schneider.aufrufe == [
        (0, 3),
        (4, 7),
        (8, 11),
        (12, 15),
        (16, 19),
    ]
