"""Zuschnitt — die Regeln, an denen Geld haengt.

Alle Tests hier laufen OHNE eine einzige echte PDF-Datei: Die Schneidefunktion
wird hereingereicht, und eine erfundene, die Bytes passender Groesse liefert,
genuegt. Genau dafuer ist sie ein Parameter und kein Import.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.skip(reason="Vertrag steht, Koerper folgt in Phase B")


def test_kleines_dokument_braucht_keinen_schnitt() -> None:
    """Unter beiden Schwellen ist `needs_chunking` falsch."""


def test_grosse_datei_braucht_schnitt() -> None:
    """Ueber der Byte-Schwelle ist `needs_chunking` wahr, auch bei wenigen
    Seiten."""


def test_viele_seiten_brauchen_schnitt() -> None:
    """Ueber der Seiten-Schwelle ist `needs_chunking` wahr, auch bei kleiner
    Datei.

    Der Fall, den man sonst uebersieht: 2 MB mit 200 Seiten geht muehelos hoch
    und bricht trotzdem das ANTWORT-Cap. Zwei unabhaengige Schwellen, zwei
    verschiedene Grenzen.
    """


def test_nicht_pdf_braucht_nie_schnitt() -> None:
    """Fuer Bilder und Office-Formate ist die Antwort immer falsch — nicht weil
    sie klein waeren, sondern weil sie sich nicht zerlegen lassen."""


def test_zu_grosses_nicht_pdf_wirft_mit_hinweis() -> None:
    """`check_fits_unsplit` wirft TooLargeError, und die Meldung nennt Groesse,
    Grenze UND den Aufruest-Hinweis (Festlegung 2.4)."""


def test_plan_deckt_luecken_und_ueberlappungsfrei_ab() -> None:
    """Fuer Seitenzahlen von 1 bis 500 gilt: `verify_page_coverage` wirft nie
    ueber dem Ergebnis von `plan_chunks`."""


def test_letzter_block_ist_kuerzer() -> None:
    """41 Seiten bei Schwelle 20 ergeben 20 + 20 + 1."""


def test_halbieren_erhaelt_die_abdeckung() -> None:
    """Die beiden Haelften decken zusammen genau denselben Bereich ab.

    Das ist die Invariante, die den ganzen Halbierungsvorgang sicher macht:
    Halbieren kann die Abdeckung nicht verletzen, egal wie oft es geschieht.
    """


def test_einzelne_seite_laesst_sich_nicht_halbieren() -> None:
    """`split_in_half` wirft ValueError bei einem einseitigen Chunk."""


def test_luecke_wird_erkannt() -> None:
    """Fehlt eine Seite zwischen zwei Chunks, wirft `verify_page_coverage`."""


def test_ueberlappung_wird_erkannt() -> None:
    """Ueberlappen zwei Chunks, wirft `verify_page_coverage`."""


def test_luecke_und_ueberlappung_heben_sich_nicht_auf() -> None:
    """Eine Luecke UND eine gleich grosse Ueberlappung ergeben die richtige
    Gesamtsumme und muessen trotzdem erkannt werden.

    Der Grund, warum nicht nur die Summe geprueft wird: Das Ergebnis waere ein
    Dokument mit einer fehlenden und einer doppelten Seite — bei richtiger
    Gesamtzahl.
    """


def test_zu_grosser_chunk_wird_halbiert() -> None:
    """Liefert die Schneidefunktion zu viele Bytes, wird der Chunk geteilt und
    beide Haelften geliefert."""


def test_jeder_gelieferte_chunk_haelt_die_bytegrenze() -> None:
    """Ueber verschiedene erfundene Groessenverlaeufe: kein geliefertes Stueck
    ist groesser als die Grenze."""


def test_einzelseite_ueber_der_grenze_wirft() -> None:
    """Ist eine einzelne Seite auch allein zu gross, wirft `iter_chunks`
    TooLargeError — moeglicherweise NACHDEM schon Chunks geliefert wurden.

    Das ist gewollt: Der Aufrufer schreibt dann `input_error` samt der bereits
    erhaltenen Antworten, es geht nichts Bezahltes verloren.
    """


def test_reihenfolge_bleibt_aufsteigend() -> None:
    """Auch nach mehrfachem Halbieren kommen die Stuecke in aufsteigender
    Seitenreihenfolge."""


def test_es_lebt_immer_nur_ein_chunk() -> None:
    """Der Generator schneidet erst beim naechsten Abruf, nicht vorab.

    Gepruef durch Zaehlen der Aufrufe der Schneidefunktion. Ohne diese
    Eigenschaft haelt ein 200-Seiten-Dokument 70 MB im Speicher — mal
    vierundzwanzig gleichzeitig betreute Dokumente.
    """
