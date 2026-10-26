"""Die Vollstaendigkeitspruefung je Teilstueck — ohne Datenbank, ohne Netz.

Geprueft wird an echten Antworten des MinerU-4-Endpoints und an Antworten, die
aus ihnen abgeleitet sind (`ersatz_antworten.abgeleitete_antwort`), fuer die
Lagen, die der Endpoint nicht gezielt liefert: eine verworfene Seite, eine
fehlende Datei, `degraded`.
"""

from __future__ import annotations

from typing import Any

import pytest

import ersatz_antworten as ersatz
from mineru_ocr import assembly
from mineru_ocr.chunking import Chunk
from mineru_ocr.errors import MissingArtifactError, MissingPagesError
from mineru_ocr.vollstaendigkeit import BENOETIGTE_DATEIEN, teilstueck_pruefen


def _teil(antwort: dict[str, Any], chunk: Chunk = Chunk(0, 0)) -> assembly.ChunkArtifacts:
    return assembly.unpack_response(antwort, chunk)


def _ohne_seite(page_idx: int) -> Any:
    """Entfernt die Seite `page_idx` aus dem Middle-JSON, wie wenn MinerU sie verworfen haette."""

    def aendern(middle: dict[str, Any]) -> dict[str, Any]:
        middle["pages"] = [s for s in middle["pages"] if s["page_idx"] != page_idx]
        return middle

    return aendern


def _degraded(*eintraege: dict[str, Any], count: int | None = None) -> dict[str, Any]:
    """Ein `degraded` in der Form des Job-Vertrags („Incomplete responses")."""
    return {"count": len(eintraege) if count is None else count, "items": list(eintraege)}


def _eintrag(artefakt: str | None, datei: str) -> dict[str, Any]:
    return {
        "artifact": artefakt,
        "file": datei,
        "reason": "unreadable",
        "error_type": "JSONDecodeError",
    }


# --- Die Liste der benoetigten Dateien -----------------------------------------


def test_das_paket_braucht_markdown_und_middle_json() -> None:
    """Die Liste steht an EINER Stelle und nennt, was der Code liest, der ein
    gespeichertes Ergebnis darstellt: `markdown.md` (der Text) und
    `middle_json.json` (Randbloecke, Leere, Seitenzaehlung). Die Vollmodus-Fassung
    kommt mit Mineroo#13 dazu, wenn `rendered_markdown` sie liest."""
    assert BENOETIGTE_DATEIEN == {
        assembly.DATEI_MARKDOWN: "markdown",
        assembly.DATEI_MIDDLE_JSON: "middle_json",
    }


# --- Seiten --------------------------------------------------------------------


@pytest.mark.parametrize("name", ["normale_seite", "leere_seite_pdf", "leere_seite_bild"])
def test_echte_einseitige_antworten_sind_vollstaendig(name: str) -> None:
    """Eine Seite geschickt, eine verarbeitet: nichts zu beanstanden, nichts zu
    vermerken. Die leere Seite zaehlt mit, obwohl sie keine Bloecke traegt."""
    assert teilstueck_pruefen(_teil(ersatz.echte_antwort(name)), seiten_geschickt=1) is None


def test_echte_teilstuecke_des_mehrteilers_sind_vollstaendig() -> None:
    """Die drei echten Teilstuecke (Seiten 0-4, 5-9, 10-11) tragen je so viele
    Seiten, wie geschickt wurden; die Seiten zaehlen im Teilstueck ab 0."""
    for name, (erste, letzte) in ersatz.echte_teilstuecke():
        chunk = Chunk(erste, letzte)
        teil = _teil(ersatz.echte_antwort(name), chunk)
        assert teilstueck_pruefen(teil, seiten_geschickt=chunk.page_count) is None, name


def test_verworfene_seite_im_teilstueck_ist_terminal() -> None:
    """MinerU verarbeitet von fuenf geschickten Seiten nur vier: Das Teilstueck
    ist unvollstaendig, und die Meldung nennt die fehlende Seite in der Zaehlung
    des Originals (Seite 7 = drittes Blatt des Teilstuecks 5-9)."""
    antwort = ersatz.abgeleitete_antwort(
        "mehrteilig_1_seiten_5-9", middle_json_aendern=_ohne_seite(2)
    )
    with pytest.raises(MissingPagesError) as fehler:
        teilstueck_pruefen(_teil(antwort, Chunk(5, 9)), seiten_geschickt=5)
    meldung = str(fehler.value)
    assert "5 Seiten geschickt" in meldung
    assert "4 verarbeitet" in meldung
    assert "[7]" in meldung


def test_zu_viele_seiten_sind_ebenfalls_eine_abweichung() -> None:
    """Mehr Seiten als geschickt heisst ebenso: Was MinerU verarbeitet hat, ist
    nicht, was das Paket geschickt hat. Die Seitenzahlen des Ergebnisses
    stimmten danach nicht mehr."""
    antwort = ersatz.echte_antwort("mehrteilig_2_seiten_10-11")
    with pytest.raises(MissingPagesError):
        teilstueck_pruefen(_teil(antwort, Chunk(10, 10)), seiten_geschickt=1)


def test_doppelte_seite_bei_richtiger_anzahl_ist_eine_abweichung() -> None:
    """Fuenf Seiten, aber Seite 2 zweimal und Seite 3 gar nicht: Die Anzahl
    stimmt, die Seiten nicht."""

    def aendern(middle: dict[str, Any]) -> dict[str, Any]:
        middle["pages"][3]["page_idx"] = 2
        return middle

    antwort = ersatz.abgeleitete_antwort(
        "mehrteilig_0_seiten_0-4", middle_json_aendern=aendern
    )
    with pytest.raises(MissingPagesError) as fehler:
        teilstueck_pruefen(_teil(antwort, Chunk(0, 4)), seiten_geschickt=5)
    assert "[3]" in str(fehler.value)


@pytest.mark.parametrize(
    "seiten",
    [
        "keine Liste",
        [{"blocks": []}],
        [{"page_idx": "0"}],
        [{"page_idx": True}],
        ["keine Seite"],
    ],
)
def test_unzaehlbare_seiten_sind_terminal(seiten: Any) -> None:
    """Laesst sich nicht feststellen, welche Seiten MinerU verarbeitet hat, gilt
    das Teilstueck nicht als vollstaendig: Im Zweifel `failed`, denn ein
    faelschlich `done` verbuchtes Dokument mit fehlender Seite faellt nie auf."""

    def aendern(middle: dict[str, Any]) -> dict[str, Any]:
        middle["pages"] = seiten
        return middle

    antwort = ersatz.abgeleitete_antwort("normale_seite", middle_json_aendern=aendern)
    with pytest.raises(MissingPagesError):
        teilstueck_pruefen(_teil(antwort), seiten_geschickt=1)


def test_ohne_seitenzahl_wird_nicht_gezaehlt() -> None:
    """Dateien, die MinerU als ganzes Dokument liest (DOCX, HTML), haben keine
    Seitenzahl, die das Paket kennt; `seiten_geschickt=None` laesst den Vergleich
    aus. Die echte DOCX-Antwort traegt eine logische Seite."""
    antwort = ersatz.echte_antwort("dokument_docx")
    assert teilstueck_pruefen(_teil(antwort), seiten_geschickt=None) is None


# --- Benoetigte Dateien und degraded --------------------------------------------


def test_fehlendes_middle_json_ist_terminal_auch_ohne_degraded() -> None:
    """Der Fall aus dem Code-Review zu Mineroo#11 (A1): Fehlt `middle_json.json`,
    endete das Dokument `done`, und jede Darstellung scheiterte danach dauerhaft.
    Terminal, auch wenn der Worker es nicht in `degraded` meldet, und auch bei
    einer Datei ohne Seitenzahl."""
    antwort = ersatz.abgeleitete_antwort("dokument_docx", ohne=("middle_json.json",))
    with pytest.raises(MissingArtifactError) as fehler:
        teilstueck_pruefen(_teil(antwort), seiten_geschickt=None)
    assert "middle_json.json" in str(fehler.value)


def test_degraded_middle_json_ist_terminal() -> None:
    """Der Worker meldet `middle_json.json` als unlesbar und laesst es weg: terminal,
    und die Meldung nennt Grund und Fehlertyp aus `degraded`."""
    antwort = ersatz.abgeleitete_antwort(
        "normale_seite",
        ohne=("middle_json.json",),
        degraded=_degraded(_eintrag("middle_json", "middle_json.json")),
    )
    with pytest.raises(MissingArtifactError) as fehler:
        teilstueck_pruefen(_teil(antwort), seiten_geschickt=1)
    meldung = str(fehler.value)
    assert "middle_json.json" in meldung
    assert "unreadable" in meldung
    assert "JSONDecodeError" in meldung


def test_degraded_benoetigte_datei_ist_terminal_auch_wenn_sie_da_ist() -> None:
    """Beim Inline-Transport kommt eine verlorene Datei als leerer Ersatzwert
    zurueck, sie ist also DA und trotzdem verloren (Job-Vertrag). Nennt
    `degraded` eine benoetigte Datei, entscheidet das, nicht ihr Vorhandensein.
    Erkannt am Dateinamen wie am Schluessel des Artefakts."""
    for eintrag in (
        _eintrag("middle_json", "middle_json.json"),
        _eintrag(None, "middle_json.json"),
        _eintrag("markdown", "irgendwas"),
    ):
        antwort = ersatz.abgeleitete_antwort("normale_seite", degraded=_degraded(eintrag))
        with pytest.raises(MissingArtifactError):
            teilstueck_pruefen(_teil(antwort), seiten_geschickt=1)


def test_degraded_andere_datei_wird_vermerkt_nicht_terminal() -> None:
    """Eine Datei, die das Paket nicht braucht (`structured_content.json`), in
    `degraded`: kein Fehler, aber das ganze `degraded` kommt zum Vermerken
    zurueck, damit es nicht nur in `raw` steht, wo niemand nachsieht."""
    degraded = _degraded(_eintrag("structured_content", "structured_content.json"))
    antwort = ersatz.abgeleitete_antwort(
        "normale_seite", ohne=("structured_content.json",), degraded=degraded
    )
    assert teilstueck_pruefen(_teil(antwort), seiten_geschickt=1) == degraded


def test_markdown_full_ist_vorerst_keine_benoetigte_datei() -> None:
    """`markdown_full.md` liest bis Mineroo#13 kein Darstellungscode; ein
    `degraded`-Eintrag dazu (der eine, den der Worker selbst schreibt, wenn er
    die Vollfassung nicht rendern kann) wird vermerkt."""
    degraded = _degraded(_eintrag("markdown_full", "markdown_full.md"))
    antwort = ersatz.abgeleitete_antwort(
        "normale_seite", ohne=("markdown_full.md",), degraded=degraded
    )
    assert teilstueck_pruefen(_teil(antwort), seiten_geschickt=1) == degraded


def test_gekuerztes_degraded_wird_mit_seiner_zahl_vermerkt() -> None:
    """`count` ueber der Laenge von `items` heisst: Die Liste ist gekuerzt (bei
    50). Die fehlenden Eintraege kennt niemand; eine benoetigte Datei faellt aber
    an ihrem Fehlen im Archiv auf. Vermerkt wird das ganze Objekt samt `count`."""
    degraded = _degraded(_eintrag(None, "page_0_chart_5.jpg"), count=73)
    antwort = ersatz.abgeleitete_antwort("normale_seite", degraded=degraded)
    assert teilstueck_pruefen(_teil(antwort), seiten_geschickt=1) == degraded


@pytest.mark.parametrize("unlesbar", ["kaputt", {"items": "keine Liste"}, {"items": ["kein Objekt"]}])
def test_unlesbares_degraded_wird_vermerkt(unlesbar: Any) -> None:
    """Ein `degraded`, das nicht die Form des Vertrags hat, sagt nur: Etwas fehlt.
    Ob es eine benoetigte Datei ist, entscheidet dann ihr Fehlen im Archiv; das
    `degraded` selbst wird unveraendert vermerkt."""
    antwort = ersatz.abgeleitete_antwort("normale_seite", degraded=unlesbar)
    assert teilstueck_pruefen(_teil(antwort), seiten_geschickt=1) == {"unlesbar": unlesbar}
