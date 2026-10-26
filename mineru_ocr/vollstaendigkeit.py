"""Ob die Antwort eines Teilstuecks vollstaendig ist — geprueft nach jedem Teilstueck.

Der Worker meldet `COMPLETED` auch dann, wenn etwas fehlt: Eine Datei, die er
nicht lesen konnte, laesst er weg und nennt sie in `degraded` (Job-Vertrag,
„Incomplete responses"); eine Seite, die MinerU verwirft, fehlt einfach im
Middle-JSON. Ohne diese Pruefung endete ein solches Dokument `done`, und nichts
meldete den Verlust (Mineroo#12; Mineroo#1, Stories 13 und 14).

Was hier fehlt, endet terminal ueber die eine Entscheidung in `errors`: Die
Ausnahmen sind `IncompleteResponseError`, also `TerminalRunPodError`, und der
Executor schreibt `failed` mit der bezahlten Antwort in `raw`. Was in `degraded`
steht und das Paket nicht braucht, wird nur vermerkt.

AM ENDPOINT GEPRUEFT (26.10.2026, MinerU 4.0.12, Image `2bccaebd0`): Eine PDF,
deren Seitenbaum vier Seiten behauptet, von denen PDFium die vierte nicht laden
kann, laesst MinerU nicht still auf drei Seiten schrumpfen; der ganze Job
scheitert (`RuntimeError: PDF render worker PdfiumError: Failed to load page.`)
und endet `failed`. Die Seitenpruefung ist das Netz fuer jeden anderen Weg, auf
dem eine Seite fehlt. Ihre Grenze steht in MinerUs Quellen
(`mineru/parser/mineru_parser.py`, `_insert_broken_pages`): Mit einem
Seitenbereich setzt MinerU fuer eine kaputte Seite einen leeren Platzhalter
`{"page_idx": n}` ein, der wie eine leere Seite aussieht. Das Paket schickt
keinen Seitenbereich; wer das aendert, muss diese Luecke schliessen.

Dieses Modul liest nur, was `assembly.unpack_response` ausgepackt hat. Es
kennt weder Datenbank noch RunPod.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from .assembly import DATEI_MARKDOWN, DATEI_MIDDLE_JSON, ChunkArtifacts
from .errors import MissingArtifactError, MissingPagesError

BENOETIGTE_DATEIEN: Final[Mapping[str, str]] = {
    DATEI_MARKDOWN: "markdown",
    DATEI_MIDDLE_JSON: "middle_json",
}
"""Die Dateien einer Antwort, ohne die das Paket ein Dokument nicht `done` werden laesst.

Je Datei ihr Schluessel als Artefakt, wie ihn `degraded` nennt (`items[].artifact`,
Job-Vertrag) und wie das Feld von `assembly.ChunkArtifacts` heisst. Die Liste
ergibt sich aus dem Code, der ein gespeichertes Ergebnis liest:

    `markdown.md`: der Text des Dokuments. `assembly.rendered_markdown` beginnt
    damit; ohne ihn laesst schon der Worker den Job scheitern, und
    `assembly.unpack_response` weist eine Antwort ohne ihn ab.

    `middle_json.json`: `assembly.rendered_markdown` liest daraus die
    Randbloecke (`_pflichtfeld`, `_randbloecke`), `assembly.is_empty` die
    Bloecke, und die Seitenzaehlung dieses Moduls die Seiten. Fehlte es, endete
    das Dokument `done`, und jede Darstellung scheiterte danach dauerhaft
    (Code-Review zu Mineroo#11, Befund A1).

Wer Code schreibt, der eine weitere Datei des gespeicherten Ergebnisses
braucht, traegt sie hier ein; mit Mineroo#13 kommt `markdown_full.md` dazu
(`rendered_markdown` wird dann die Vollmodus-Fassung). Eine Datei, die hier
nicht steht, darf fehlen: Ein `degraded`-Eintrag zu ihr wird vermerkt."""


def teilstueck_pruefen(
    teil: ChunkArtifacts, *, seiten_geschickt: int | None
) -> dict[str, Any] | None:
    """Prueft die ausgepackte Antwort eines Teilstuecks auf Vollstaendigkeit.

    In dieser Reihenfolge:

    1. `degraded` nennt eine benoetigte Datei (`BENOETIGTE_DATEIEN`, am
       Schluessel des Artefakts oder am Dateinamen): terminal, auch wenn die
       Datei da ist. Beim Inline-Transport kommt eine verlorene Datei als leerer
       Ersatzwert zurueck; sie waere da und doch verloren.

    2. Eine benoetigte Datei fehlt in der Antwort: terminal, auch ohne
       `degraded`. Das deckt eine gekuerzte oder unlesbare `degraded`-Liste mit
       ab, denn beim Transport `tarball_b64` fehlt eine verlorene Datei im
       Archiv.

    3. Bei bekannter Seitenzahl: Die Seiten des Middle-JSON (ihre `page_idx`,
       im Teilstueck ab 0 gezaehlt) muessen genau die geschickten sein, jede
       einmal. Weniger heisst, MinerU hat eine Seite verworfen; mehr oder
       doppelte heissen, die Seitenzahlen des Ergebnisses stimmten nicht. Laesst
       sich nicht feststellen, welche Seiten verarbeitet wurden (keine
       Seitenliste, eine Seite ohne ganzzahliges `page_idx`), gilt das
       Teilstueck nicht als vollstaendig: Ein faelschlich `done` verbuchtes
       Dokument mit fehlender Seite faellt nie auf.

    Args:
        teil: Das ausgepackte Teilstueck (`assembly.unpack_response`).
        seiten_geschickt: Wie viele Seiten das Paket in diesem Teilstueck
            geschickt hat: bei PDFs die Seiten des Ausschnitts, bei einem Bild
            1. `None` fuer Dateien, die MinerU als ganzes Dokument liest (Office,
            HTML); fuer sie kennt das Paket keine Seitenzahl, und der Vergleich
            entfaellt.

    Returns:
        Was zu vermerken ist: das `degraded` der Antwort, wenn sie eins traegt
        und keine benoetigte Datei betrifft (unveraendert, samt `count`; ein
        `degraded`, das nicht die Form des Vertrags hat, als
        `{"unlesbar": …}`). Sonst None.

    Raises:
        MissingArtifactError: Schritt 1 oder 2.
        MissingPagesError: Schritt 3.

    Kosten: keine.
    """
    degraded = teil.extras.get("degraded")
    eintraege = _degraded_eintraege(degraded)
    for eintrag in eintraege or []:
        datei = _benoetigte_datei(eintrag)
        if datei is not None:
            raise MissingArtifactError(
                f"Der Worker meldet die Datei {datei!r}, die das Paket braucht, in "
                f"`degraded` als verloren (Grund {eintrag.get('reason')!r}, Fehlertyp "
                f"{eintrag.get('error_type')!r}) {_bereich(teil)}. Das Dokument wird "
                f"nicht `done`; die bezahlte Antwort steht in `raw`."
            )
    for datei, artefakt in BENOETIGTE_DATEIEN.items():
        if getattr(teil, artefakt) is None:
            raise MissingArtifactError(
                f"Der Antwort fehlt die Datei {datei!r}, die das Paket braucht, "
                f"{_bereich(teil)}. Ohne sie waere das Ergebnis nicht darstellbar. "
                f"Das Dokument wird nicht `done`; die bezahlte Antwort steht in `raw`."
            )
    if seiten_geschickt is not None:
        _seiten_vergleichen(teil, seiten_geschickt)
    if degraded is None:
        return None
    if eintraege is None:
        return {"unlesbar": degraded}
    vermerk: dict[str, Any] = degraded
    return vermerk


def _degraded_eintraege(degraded: Any) -> list[Mapping[str, Any]] | None:
    """Die Eintraege eines `degraded` in der Form des Vertrags, oder None.

    None heisst: Es gibt keins, oder es hat nicht die Form `{"items": [ {…}, … ]}`
    (Job-Vertrag, „Incomplete responses").
    """
    if not isinstance(degraded, Mapping):
        return None
    eintraege = degraded.get("items")
    if not isinstance(eintraege, list) or not all(
        isinstance(eintrag, Mapping) for eintrag in eintraege
    ):
        return None
    return eintraege


def _benoetigte_datei(eintrag: Mapping[str, Any]) -> str | None:
    """Die benoetigte Datei, die ein `degraded`-Eintrag nennt, oder None.

    Erkannt am Dateinamen (`file`) oder am Schluessel des Artefakts
    (`artifact`, None fuer ein Archivmitglied, das kein Format ist).
    """
    for datei, artefakt in BENOETIGTE_DATEIEN.items():
        if eintrag.get("file") == datei or eintrag.get("artifact") == artefakt:
            return datei
    return None


def _bereich(teil: ChunkArtifacts) -> str:
    """Der Seitenbereich des Teilstuecks im Original, fuer die Meldungen."""
    return (
        f"(Teilstueck mit den Seiten {teil.chunk.first_page} bis "
        f"{teil.chunk.last_page} des Originals, ab 0 gezaehlt)"
    )


def _seiten_vergleichen(teil: ChunkArtifacts, seiten_geschickt: int) -> None:
    """Schritt 3 von `teilstueck_pruefen`. Wirft `MissingPagesError`."""
    middle = teil.middle_json
    seiten = middle.get("pages") if middle is not None else None
    nummern: list[int] = []
    unzaehlbar = not isinstance(seiten, list)
    for seite in seiten if isinstance(seiten, list) else []:
        nummer = seite.get("page_idx") if isinstance(seite, Mapping) else None
        if not isinstance(nummer, int) or isinstance(nummer, bool):
            unzaehlbar = True
            break
        nummern.append(nummer)
    if unzaehlbar:
        raise MissingPagesError(
            f"Welche Seiten MinerU verarbeitet hat, laesst sich am Middle-JSON "
            f"nicht feststellen (keine Seitenliste oder eine Seite ohne "
            f"ganzzahliges `page_idx`) {_bereich(teil)}. Geschickt waren "
            f"{seiten_geschickt} Seiten. Das Dokument wird nicht `done`; die "
            f"bezahlte Antwort steht in `raw`."
        )
    erwartet = list(range(seiten_geschickt))
    if sorted(nummern) == erwartet:
        return
    fehlend = [
        nummer + teil.chunk.first_page for nummer in erwartet if nummer not in nummern
    ]
    raise MissingPagesError(
        f"{seiten_geschickt} Seiten geschickt, {len(nummern)} verarbeitet "
        f"{_bereich(teil)}. Es fehlen die Seiten {fehlend} des Originals (ab 0 "
        f"gezaehlt); verarbeitet hat MinerU die Seiten {sorted(nummern)} des "
        f"Teilstuecks. Das Dokument wird nicht `done`; die bezahlte Antwort "
        f"steht in `raw`."
    )
