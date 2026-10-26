"""Aus den Antworten der Engine wird EIN Dokument — im Executor, vor dem Schreiben.

Die tragende Festlegung, an der sich alles in dieser Datei ausrichtet: Eingabe
ist das ganze Dokument, Ausgabe ist das ganze Dokument in erkannter Form. Dass
die Engine unterwegs mehrfach angesprochen wurde, weil das Dokument fuer eine
einzelne Anfrage zu gross war, ist eine Eigenschaft des Transports. „DIE CHUNKS
SIND ABSTRAHIERT ES LANDET IN POSTGRES NUR DAS FERTIG ZUSAMMENGESETZTE UND
DADURCH GEHT KEINE INFORMATION VERLOREN WEIL INPUT IST DAS GANZE DOKUMENT UND
OUTPUT IST DAS GANZE DOKUMENT IN OCR, das es dazwischen chunks gab in der arbeit
is irrelevant" (Jurgen, 07.08.2026).

Daraus folgt der Ort: Zusammengesetzt wird HIER, im Ablauf des Executors, bevor
irgendetwas in die Datenbank geht — nicht spaeter beim Lesen. Eine fruehere
Fassung dieses Moduls hatte es umgekehrt, und daran ist sie gescheitert: Nur der
Executor weiss, welcher Teil welche Seiten trug. Verschiebt man das
Zusammensetzen auf die Leseseite, muss dieses Wissen mitreisen — also doch
Chunk-Angaben in der Datenbank, gegen die Festlegung. Setzt man dagegen vorher
zusammen, geht nichts verloren, weil nichts getrennt bleibt.

Zusammengesetzt heisst NICHT reduziert. Weggelassen wird nichts: jede Datei, die
der MinerU-4-Worker liefert (`markdown.md`, `markdown_full.md`, `middle_json.json`,
`structured_content.json`, `model_output.json`, die Bilder), die `debug`-Bloecke,
die Ausfuehrungszeiten, alles Unerkannte — alles wandert in das Ergebnis.
Zusammengesetzt heisst nur, dass Text und Seiten wieder in der Reihenfolge und mit
der Zaehlung stehen, die sie vor dem Schnitt hatten, und dass die Bilder der
Teilstuecke einander nicht ueberschreiben.

Was danach beim LESEN passiert, ist etwas anderes und bleibt hier ebenfalls:
Aufbereitungen des fertigen Ergebnisses (Randbloecke weglassen, filtern). Die
sind billig, jederzeit wiederholbar und aendern nichts an dem, was gespeichert
ist.

Dieses Modul importiert kein pypdfium2 und nichts Schweres. Wer ein fertiges
Ergebnis anschauen will, soll keine PDF-Bibliothek laden.
"""

from __future__ import annotations

import base64
import binascii
import copy
import io
import json
import re
import tarfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, TypeVar

from .chunking import Chunk
from .errors import IncompleteResultError, TerminalRunPodError

W = TypeVar("W")
"""Der erwartete Typ eines Feldes des gespeicherten Ergebnisses in `_pflichtfeld` —
`str` fuer `markdown`, `list` fuer `middle_json`."""

FURNITURE_TYPES: Final = frozenset(
    {"header", "footer", "aside_text", "page_number", "page_footnote"}
)
"""Die Blockklassen, die MinerU aus seinem Markdown HERAUSFILTERT.

Gemessen, nicht vermutet: Die Kalibrierung hat am 03.08.2026 ueber alle 15
content_list-Dateien gegen ihr jeweiliges Markdown gezaehlt — `header` 219 von
271 Bloecken nicht im Markdown, `footer` 186 von 205, `aside_text` 94 von 102,
`page_number` 29 von 57, `page_footnote` 5 von 5. Die Klasse `text` ist
vollstaendig wiederzufinden. An den sechs konservierten Antworten vom 05.08.2026
nachgeprueft: dort enthaelt kein Markdown einen einzigen dieser Bloecke (die zwei
scheinbaren Treffer waren Zeichenketten, die als gewoehnlicher Inhaltstext ein
zweites Mal vorkamen).

WARUM DAS HIER STEHT UND NICHT IRGENDWO AM RAND: In genau diesen Bloecken liegt
das AKTENZEICHEN. Die Kalibrierung hat den Weg an den Layout-Daten desselben
Dokuments nachgezeichnet — `model.json` fuehrt es als
`{"type": "header", "content": "Aktenzeichen 123/456/7890/012/345/6"}`,
`middle.json` legt denselben Text unter `discarded_blocks[6]` ab, `content_list`
fuehrt ihn als `{"type": "header", "text": "Aktenzeichen …"}` — und das Markdown
hat ihn nicht mehr. „Der Filter trifft nicht die Kennung, sondern die Zone."
Daraus folgt der Satz, aus dem `rendered_markdown` diese Bloecke OHNE Schalter
anhaengt — woertlich aus `docs/MinerU-OCR-Kalibrierung-und-Backend-Vergleich.md`,
Abschnitt 5.4, Befund (1): „Fuer den Bescheid-Korpus ist ein Write-back aus dem
nackten Markdown damit nachweislich schlechter als das, was heute schon auf den
Records liegt."

Die Liste ist damit keine Stilfrage, sondern die Stelle, an der ueber die
Auffindbarkeit der Kennungen in Kopf- und Fusszeilen entschieden wird.

Gemessen ist das an MinerU 3.4. Unter MinerU 4 sind es Blocktypen des Middle-JSON
mit denselben Namen (DocVortex 0.5.16, `BlockType`: `header`, `footer`,
`page_number`, `aside_text`, `page_footnote`). Gemessen am Endpoint (Stufe
`advanced`, OCR, `artifacts/2026-10-26-mineru-4.0.12-endpoint/`): `markdown.md`
laesst Kopfzeile, Fusszeile, Seitenzahl und Randtext weg und behaelt die
Seitenfussnote; `markdown_full.md` traegt alle fuenf."""


DATEI_MARKDOWN: Final = "markdown.md"
DATEI_MARKDOWN_FULL: Final = "markdown_full.md"
DATEI_MIDDLE_JSON: Final = "middle_json.json"
DATEI_STRUCTURED_CONTENT: Final = "structured_content.json"
DATEI_MODEL_OUTPUT: Final = "model_output.json"
BILDER_VERZEICHNIS: Final = "images/"
"""Die Dateien, die der MinerU-4-Worker ins Archiv legt, unter ihren festen Namen.

AUS DEN QUELLEN, nicht aus Mustern geraten: MinerU 4 benennt seine Dateien selbst
(`ParseResult.save` in MinerU 4.0.12 schreibt `markdown.md`, `middle_json.json`,
`structured_content.json`, `images/` und, wenn es Modellausgabe gibt,
`model_output.json`), der Worker legt `markdown_full.md` dazu, und
`worker/harness.py` des Forks (`MANIFEST`) nennt genau diese Namen; der Job-Vertrag
(`api.mdx`, „When `transport: "inline"`") fuehrt sie als „File in the archive". Die
Zuordnung nach Namensmustern, die unter MinerU 3.4 noetig war (dort trugen die
Namen das `basename`), entfaellt damit: Was auf keinen dieser Namen passt, ist
Unerkanntes und wandert nach `extras`."""


@dataclass(frozen=True)
class ChunkArtifacts:
    """Was die Antwort EINES Teilstuecks an verwertbarem Inhalt enthielt.

    Reines Laufzeit-Zwischenergebnis im Executor: entsteht beim Auspacken einer
    Antwort und ist wieder verschwunden, sobald `assemble` daraus ein Dokument
    gemacht hat. Nichts davon wird gespeichert, nichts davon verlaesst den
    Executor.

    Jede Datei der Antwort hat ihr eigenes Feld (Mineroo#11). „Leer" und „nicht
    da" sind dabei zwei verschiedene Dinge: Eine Datei, die die Antwort nicht
    trug, ist `None`; eine, die sie leer trug, ist leer. Der Worker laesst eine
    Datei weg, die er nicht lesen konnte, und sagt es in `degraded` (Job-Vertrag,
    „Incomplete responses"); `degraded` steht im Eintrag und damit in `extras`.
    """

    chunk: Chunk
    """Welchen Seitenbereich des Originals dieses Teilstueck trug. Hier — und nur
    hier, zur Laufzeit — existiert der Versatz, mit dem `page_idx` zurueckgerechnet
    wird."""

    markdown: str
    """`markdown.md`: der Text dieses Teilstuecks, wie MinerU ihn rendert. Die
    eine Datei, ohne die der Worker den Job scheitern laesst (Job-Vertrag:
    „The one required artifact"); fehlt sie, ist die Antwort unverwertbar. Leer
    ist ein gueltiger Zustand (leere Seite), kein Fehler."""

    markdown_full: str | None
    """`markdown_full.md`: derselbe Text im Vollmodus (`RenderMode.FULL`), der
    Kopf- und Fusszeilen, Seitenzahlen, Randtext und Seitenfussnoten behalten
    soll. Die Datei des Workers, nicht MinerUs; kann er sie nicht rendern, fehlt
    sie (None). Am Endpoint gemessen traegt sie alle fuenf Randklassen."""

    middle_json: dict[str, Any] | None
    """`middle_json.json`: MinerUs Zwischenformat, massgeblich fuer die
    Auswertung (Schema `docvortex.middle` 2.0: Seiten mit `page_idx` und
    `blocks`). Als Struktur, unveraendert; die `page_idx` sind teilstueck-lokal,
    weil jedes Teilstueck eine eigene Datei ist, die bei Seite 0 beginnt."""

    structured_content: dict[str, Any] | None
    """`structured_content.json`: die Seiten fuer Abnehmer, Bloecke mit ihrem
    Text als Zeichenkette."""

    model_output: dict[str, Any] | None
    """`model_output.json`: MinerUs rohe Modellausgabe. Fehlt, wenn MinerU keine
    hat (Job-Vertrag)."""

    images: dict[str, bytes]
    """Die Bilder unter ihrem Namen ohne `images/`, etwa `page_0_image_2.jpg`.

    MinerU 4 (ueber DocVortex) benennt sie nach Seite und Block,
    `page_{page_idx}_{typ}_{index}.{endung}`, mit der Seite im gelesenen
    Dokument. Jedes Teilstueck ist eine eigene Datei und zaehlt wieder ab Seite
    0; die Namen zweier Teilstuecke kollidieren also. `assemble` gibt deshalb
    jedem Teilstueck seinen eigenen Namensraum.

    Alle Bilder wandern weiter, ob ein Verweis sie nennt oder nicht (Abruf-Regel:
    alles holen, alles persistieren, nie filtern)."""

    envelope: dict[str, Any]
    """Was die Antwort ausser dem Inhalt trug: `status`, `id`, `executionTime`,
    `delayTime`, `workerId` und unter `output_meta` den Rest der Ausgabe —
    `mineru_version`, die gemeldeten Lese-Einstellungen, `debug`. Wird hier nicht
    ausgewertet, aber auch nicht weggeworfen — es ist die Herkunft eines
    bezahlten Ergebnisses."""

    extras: dict[str, Any]
    """ALLES, was die Antwort sonst noch trug und wir nicht benannt haben.

    DIES IST DIE ABRUF-REGEL ALS BAUTEIL: alles holen, alles persistieren, nie
    filtern. Eine Antwort ist dreissig Minuten nach ihrer Entstehung nirgends
    mehr abrufbar, und was wir in diesem Moment nicht aufheben, ist fuer immer
    weg — bezahlt und weg.

    Hier landen die Felder des Eintrags neben dem Inhalt (`basename`, `source`,
    `pages_requested`, `degraded`), jede Archivdatei, deren Name keiner der
    bekannten ist (als Base64 unter ihrem vollen Pfad im Archiv), und jede
    bekannte, die nicht die erwartete Form hatte (eine JSON-Datei, die kein
    lesbares JSON-Objekt ist; ein Inline-Feld mit falschem Typ). Beides ist
    JSON-faehig und geht damit unveraendert in `raw`."""


def unpack_response(response: dict[str, Any], chunk: Chunk) -> ChunkArtifacts:
    """Packt eine volle `/status`-Antwort in ihre Bestandteile aus.

    Der Betrieb versendet `tarball_b64`; das Archiv wird im Speicher ausgelesen.
    Der Inline-Transport wird ebenso verstanden (gleiche Dateien, als Schluessel
    des Eintrags), damit eine Antwort in dieser Form nicht als unverwertbar
    endet. In beiden Faellen wird nur gelesen; die uebergebene Antwort bleibt
    unberuehrt.

    Args:
        response: Die vollstaendige Antwort mit `status`, `id`, `output` und allem
            uebrigen — nicht nur der `output`-Teil.
        chunk: Welchen Seitenbereich diese Antwort trug.

    Vorbedingung:
        `response["status"] == "COMPLETED"`. Nicht erfolgreiche Endzustaende
        werden VOR dem Auspacken vom Executor in eine Ausnahme uebersetzt — hier
        kommen sie nicht an.

    Zusicherung:
        Verlustfrei: Was die Antwort trug, steht danach in einem Feld von
        `ChunkArtifacts`, auch das, was niemand auswertet (`envelope`, `extras`).
        Was die Antwort nicht trug, wird nicht erfunden: Eine fehlende Datei ist
        None, ohne stillen Ersatzwert.

    Raises:
        TerminalRunPodError: Die Antwort hat nicht die erwartete Form — kein
            `output`, kein `results`, unlesbarer Tarball, kein Markdown.
            Endgueltig: Ein zweiter Versuch kauft dieselbe unlesbare Antwort. Die
            Antwort selbst hebt der Executor dann vollstaendig auf.

    Kosten: keine.
    """
    ausgabe = response.get("output")
    if not isinstance(ausgabe, dict):
        raise TerminalRunPodError(
            f"Antwort ohne verwertbares `output`-Feld (vorhandene Schluessel: "
            f"{sorted(response)}). Ein zweiter Versuch kauft dieselbe Antwort."
        )
    ergebnisse = ausgabe.get("results")
    if not isinstance(ergebnisse, list) or not ergebnisse:
        raise TerminalRunPodError(
            f"Antwort ohne `output.results` oder mit leerer Liste (vorhandene "
            f"Schluessel in output: {sorted(ausgabe)})."
        )
    if len(ergebnisse) != 1:
        # Ein Job traegt genau eine Datei — wir schicken je Teilstueck eine.
        # Mehrere Ergebnisse hiesse, dass die Gegenseite etwas anderes getan hat
        # als bestellt; still das erste zu nehmen waere der unsichtbarste
        # denkbare Verlust.
        raise TerminalRunPodError(
            f"Antwort traegt {len(ergebnisse)} Ergebnisse, erwartet war genau "
            f"eines je Teilstueck. Es wurde eine einzelne Datei geschickt."
        )
    ergebnis = ergebnisse[0]
    if not isinstance(ergebnis, dict):
        raise TerminalRunPodError(
            f"Der Ergebnis-Eintrag ist {type(ergebnis).__name__} statt eines Objekts."
        )

    umschlag: dict[str, Any] = {
        schluessel: wert for schluessel, wert in response.items() if schluessel != "output"
    }
    umschlag["output_meta"] = {
        schluessel: wert for schluessel, wert in ausgabe.items() if schluessel != "results"
    }

    if "tarball_b64" in ergebnis:
        dateien, uebrig = _dateien_aus_tarball(ergebnis)
    else:
        dateien, uebrig = _dateien_aus_inline(ergebnis)

    markdown = dateien.get(DATEI_MARKDOWN)
    if not isinstance(markdown, str):
        raise TerminalRunPodError(
            f"Die Antwort traegt kein `{DATEI_MARKDOWN}`. Der Worker laesst einen "
            f"Job ohne Markdown scheitern (Job-Vertrag, „The one required "
            f"artifact“); fehlt es trotzdem, hat die Gegenseite ihre Dateien "
            f"geaendert. Vorhanden: {sorted(dateien)}, sonst {sorted(uebrig)}."
        )
    return ChunkArtifacts(
        chunk=chunk,
        markdown=markdown,
        markdown_full=dateien.get(DATEI_MARKDOWN_FULL),
        middle_json=dateien.get(DATEI_MIDDLE_JSON),
        structured_content=dateien.get(DATEI_STRUCTURED_CONTENT),
        model_output=dateien.get(DATEI_MODEL_OUTPUT),
        images=dateien.get(BILDER_VERZEICHNIS, {}),
        envelope=umschlag,
        extras=uebrig,
    )


_TEXTDATEIEN: Final = (DATEI_MARKDOWN, DATEI_MARKDOWN_FULL)
_JSONDATEIEN: Final = (DATEI_MIDDLE_JSON, DATEI_STRUCTURED_CONTENT, DATEI_MODEL_OUTPUT)
_INLINE_SCHLUESSEL: Final = {
    "markdown": DATEI_MARKDOWN,
    "markdown_full": DATEI_MARKDOWN_FULL,
    "middle_json": DATEI_MIDDLE_JSON,
    "structured_content": DATEI_STRUCTURED_CONTENT,
    "model_output": DATEI_MODEL_OUTPUT,
}
"""Inline-Schluessel des Eintrags → die Datei, die er traegt (Job-Vertrag, Tabelle
unter „When `transport: "inline"`")."""


def _dateien_aus_tarball(
    ergebnis: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Liest das Archiv im Speicher und ordnet seine Mitglieder ihren Namen zu.

    Gibt die erkannten Dateien (Text als `str`, JSON als Struktur, Bilder unter
    `BILDER_VERZEICHNIS` als `{name: bytes}`) und alles Uebrige zurueck: die
    Felder des Eintrags neben `tarball_b64` und jedes Mitglied, das auf keinen
    bekannten Namen passt oder nicht die erwartete Form hat, als Base64 unter
    seinem vollen Pfad.
    """
    roh = ergebnis.get("tarball_b64")
    if not isinstance(roh, str):
        raise TerminalRunPodError(
            f"`tarball_b64` ist {type(roh).__name__} statt einer Zeichenkette."
        )
    try:
        archivbytes = base64.b64decode(roh, validate=True)
    except (binascii.Error, ValueError) as fehler:
        raise TerminalRunPodError(
            f"`tarball_b64` ist kein gueltiges Base64: {fehler}"
        ) from fehler

    dateien: dict[str, Any] = {}
    bilder: dict[str, bytes] = {}
    uebrig: dict[str, Any] = {
        schluessel: wert for schluessel, wert in ergebnis.items() if schluessel != "tarball_b64"
    }
    try:
        with tarfile.open(fileobj=io.BytesIO(archivbytes), mode="r:*") as archiv:
            for eintrag in archiv:
                if not eintrag.isfile():
                    continue
                quelle = archiv.extractfile(eintrag)
                if quelle is None:
                    continue
                inhalt = quelle.read()
                name = eintrag.name
                if name in _TEXTDATEIEN and name not in dateien:
                    try:
                        dateien[name] = inhalt.decode("utf-8")
                        continue
                    except UnicodeDecodeError:
                        pass
                elif name in _JSONDATEIEN and name not in dateien:
                    wert = _json_objekt(inhalt)
                    if wert is not None:
                        dateien[name] = wert
                        continue
                elif (
                    name.startswith(BILDER_VERZEICHNIS)
                    and len(name) > len(BILDER_VERZEICHNIS)
                    and name[len(BILDER_VERZEICHNIS) :] not in bilder
                ):
                    bilder[name[len(BILDER_VERZEICHNIS) :]] = inhalt
                    continue
                # Kein bekannter Name, oder einer in unerwarteter Form, oder ein
                # zweites Mitglied unter demselben Namen: aufgehoben statt
                # fallengelassen. Unter dem VOLLEN Archivpfad und als Base64, weil
                # `raw` eine jsonb-Spalte ist und Bytes dort keine Entsprechung
                # haben. Ein Pfad, der hier schon steht, bekommt eine Nummer, damit
                # auch ein Doppel nicht beim Aufheben ein zweites Mal verloren geht.
                schluessel = name
                nummer = 1
                while schluessel in uebrig:
                    nummer += 1
                    schluessel = f"{name}#{nummer}"
                uebrig[schluessel] = base64.b64encode(inhalt).decode("ascii")
    except tarfile.TarError as fehler:
        raise TerminalRunPodError(
            f"`tarball_b64` liess sich nicht als Archiv lesen: {fehler}"
        ) from fehler
    dateien[BILDER_VERZEICHNIS] = bilder
    return dateien, uebrig


def _json_objekt(inhalt: bytes) -> dict[str, Any] | None:
    """Eine JSON-Datei als Objekt, oder None, wenn sie keines ist."""
    try:
        wert = json.loads(inhalt.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return wert if isinstance(wert, dict) else None


def _dateien_aus_inline(
    ergebnis: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Entnimmt die Dateien des Inline-Transports aus den Schluesseln des Eintrags.

    Jedes Feld wird auf seinen Typ GEPRUEFT und nicht bloss geholt: Ein
    VORHANDENER Schluessel mit dem Wert `null` (oder einem anderen Typ) ist keine
    Datei, und als solche durchgereicht braeche er erst spaeter beim
    Zusammensetzen, wo der Schaden nicht mehr seiner Ursache zuzuordnen ist. Ein
    Feld mit unerwartetem Typ geht dabei nicht verloren: Es bleibt unter seinem
    eigenen Namen in den uebrigen Feldern, also in `extras`.
    """
    dateien: dict[str, Any] = {}
    uebrig: dict[str, Any] = {}
    for schluessel, wert in ergebnis.items():
        datei = _INLINE_SCHLUESSEL.get(schluessel)
        if datei in _TEXTDATEIEN and isinstance(wert, str):
            dateien[datei] = wert
        elif datei in _JSONDATEIEN and isinstance(wert, dict):
            dateien[datei] = wert
        elif schluessel == "images" and isinstance(wert, dict):
            dateien[BILDER_VERZEICHNIS] = _bilder_dekodieren(wert)
        else:
            uebrig[schluessel] = wert
    return dateien, uebrig


def _bilder_dekodieren(roh: dict[str, Any]) -> dict[str, bytes]:
    """Bringt die Inline-Bilder auf Bytes.

    Der Inline-Transport liefert `images` als `{dateiname: base64}` mit dem
    nackten Dateinamen (Harness `contract/artifacts.py`, `kind="b64map"`). Ein
    Wert, der kein gueltiges Base64 ist, wird geworfen statt still etwas
    Falsches durchzureichen.
    """
    bilder: dict[str, bytes] = {}
    for name, wert in roh.items():
        if not isinstance(wert, str):
            raise TerminalRunPodError(
                f"Bild {name!r} hat den unerwarteten Typ {type(wert).__name__}; "
                f"erwartet war eine Base64-Zeichenkette."
            )
        try:
            bilder[name] = base64.b64decode(wert, validate=True)
        except (binascii.Error, ValueError) as fehler:
            raise TerminalRunPodError(
                f"Bild {name!r} ist kein gueltiges Base64: {fehler}"
            ) from fehler
    return bilder


BILD_NAMENSRAUM: Final = "teilstueck_{nummer}"
"""Der Namensraum der Bilder eines Teilstuecks, ein Verzeichnis unter `images/`.

MinerU 4 benennt Bilder nach der Seite im gelesenen Dokument
(`images/page_{page_idx}_{typ}_{index}.{endung}`, DocVortex `_register_image`).
Jedes Teilstueck ist eine eigene Datei, die bei Seite 0 beginnt, also heissen die
Bilder zweier Teilstuecke gleich (Job-Vertrag zu `images`: „Separate files cut
from one document by the caller each start at page `0` and do reuse them").
Ohne eigenen Namensraum ueberschriebe das Bild des einen Teilstuecks das des
anderen, und ein Verweis zeigte auf ein fremdes Bild (Mineroo#11, Story 24)."""


def assemble(parts: Sequence[ChunkArtifacts]) -> dict[str, Any]:
    """Macht aus den Teilstuecken EIN Dokument. Das Ergebnis ist, was gespeichert wird.

    Der einzige Ort, an dem die Seitenzahlen des Originals wiederhergestellt und
    die Bilder der Teilstuecke getrennt werden. Danach gibt es keine Teilstuecke
    mehr ausser in den Listen, die je Teilstueck einen Eintrag tragen.

    Was im Ergebnis steht (Mineroo#11, „Ablage in raw": jede Datei der Antwort
    hat ihren eigenen Schluessel):

        `markdown`: die Texte aus `markdown.md` in Dokumentreihenfolge, durch
        eine Leerzeile getrennt (`"\\n\\n".join`, ohne Nachbearbeitung). Keine
        Reparatur ueber die Schnittkante — jede solche Klugheit waere eine
        Auswertungs-Entscheidung, die im Ergebnis nicht mehr von einem Fehler
        der Engine zu unterscheiden ist.

        `markdown_full`: ebenso aus `markdown_full.md`. Fehlt die Datei in
        EINEM Teilstueck, ist der Wert None: Ein Text, dem ein Teilstueck fehlt,
        saehe vollstaendig aus und waere es nicht. Die Vollfassungen der
        anderen Teilstuecke sind dann trotzdem bezahlt und gehen nicht verloren
        (Abruf-Regel): Sie stehen unter `markdown_full_je_teilstueck`, je
        Teilstueck ein Eintrag, None fuer das fehlende. Den Schluessel gibt es
        NUR in diesem Fall; ist die Vollfassung ganz, steht sie einmal da.

        `middle_json`, `structured_content`, `model_output`: je Teilstueck ein
        Eintrag, in Dokumentreihenfolge, als JSON-Struktur — ausdruecklich NICHT
        verschmolzen: ein selbst ausgedachtes Zusammenfuehren waere eine
        Behauptung ueber ein fremdes Format. Ein fehlender Eintrag ist None. In
        `middle_json` und `structured_content` zaehlt jedes Seiten-`page_idx`
        die Seite des Originals (siehe `_seiten_verschoben`); `model_output`
        bleibt, wie es kam.

        `images`: jedes Bild unter dem Verweis, der im Text auf es zeigt —
        `images/teilstueck_{nummer}/{name}` (`BILD_NAMENSRAUM`) —, als Base64,
        weil Bytes in JSON keine Entsprechung haben.

        `envelopes`, `extras`: je Teilstueck ein Eintrag, nicht vereinigt; die
        Schluessel wiederholen sich ueber Teilstuecke, und eine Vereinigung
        behielte nur den letzten (Abruf-Regel).

    VERWEISE AUF BILDER werden mit umbenannt: Jedes Vorkommen von
    `images/{name}` fuer ein Bild des Teilstuecks, in jeder Zeichenkette jeder
    seiner Dateien — Markdown (`![](images/…)`), `image_path` und
    `<img src="images/…">` im Middle-JSON, `image_source` im strukturierten
    Inhalt —, zeigt danach auf `images/teilstueck_{nummer}/{name}`. So zeigt
    jeder Verweis auf sein eigenes Bild. Bilder, auf die nichts verweist,
    wandern trotzdem mit.

    Args:
        parts: ALLE Teilstuecke des Dokuments, erfolgreich und vollstaendig, in
            aufsteigender Seitenreihenfolge. Bei einem ungeschnittenen Dokument
            ist das ein einziges Element mit `Chunk(0, 0)` — und der Ablauf ist
            derselbe. Es gibt keinen Sonderfall „war nicht geschnitten".

    Returns:
        Das Gesamtergebnis, genau die Form, die in `documents.raw` landet.

    Vorbedingung:
        Die Teilstuecke decken das Dokument lueckenlos ab. Geprueft wird das
        VORHER vom Executor ueber `chunking.verify_page_coverage`.

    Zusicherung:
        Die uebergebenen Teilstuecke werden nicht veraendert. Das Ergebnis ist
        JSON-faehig.

    Raises:
        TerminalRunPodError: Ein Seiten-`page_idx` ist keine ganze Zahl (siehe
            `_seiten_verschoben`).

    Kosten: keine.
    """
    markdowns: list[str] = []
    volle: list[str | None] = []
    middles: list[dict[str, Any] | None] = []
    strukturen: list[dict[str, Any] | None] = []
    modelle: list[dict[str, Any] | None] = []
    bilder: dict[str, str] = {}
    for nummer, teil in enumerate(parts):
        umbenannt = {
            f"{BILDER_VERZEICHNIS}{name}": (
                f"{BILDER_VERZEICHNIS}{BILD_NAMENSRAUM.format(nummer=nummer)}/{name}"
            )
            for name in teil.images
        }
        for name, daten in teil.images.items():
            bilder[umbenannt[f"{BILDER_VERZEICHNIS}{name}"]] = base64.b64encode(
                daten
            ).decode("ascii")
        markdowns.append(_verweise_umbenannt(teil.markdown, umbenannt))
        volle.append(_verweise_umbenannt(teil.markdown_full, umbenannt))
        middles.append(
            _verweise_umbenannt(_seiten_verschoben(teil.middle_json, teil.chunk), umbenannt)
        )
        strukturen.append(
            _verweise_umbenannt(
                _seiten_verschoben(teil.structured_content, teil.chunk), umbenannt
            )
        )
        modelle.append(_verweise_umbenannt(teil.model_output, umbenannt))

    voll: str | None = None
    if all(text is not None for text in volle):
        voll = "\n\n".join(text for text in volle if text is not None)
    ergebnis: dict[str, Any] = {
        "markdown": "\n\n".join(markdowns),
        "markdown_full": voll,
        "middle_json": middles,
        "structured_content": strukturen,
        "model_output": modelle,
        "images": bilder,
        "envelopes": [teil.envelope for teil in parts],
        "extras": [teil.extras for teil in parts],
    }
    if voll is None and any(text is not None for text in volle):
        ergebnis["markdown_full_je_teilstueck"] = volle
    return ergebnis


T = TypeVar("T")


_HTML_BILDQUELLE: Final = re.compile(
    r"(?P<vor><img\b(?:[^<>\"'\s]++|\s++(?!src\s*=)|<(?!img\b)|\"[^\"]*\"|'[^']*')*"
    r"\s+src\s*=\s*)"
    r"(?:(?P<anfuehrung>[\"'])(?P<gequotet>.*?)(?P=anfuehrung)|(?P<ungequotet>[^\s>]+))",
    re.IGNORECASE | re.DOTALL,
)
"""Die Quelle eines HTML-Bildes, wie DocVortex 0.5.16 sie findet, wenn es die
Bilder visuellen HTMLs ablegt (`export/_images.py`, `_HTML_IMAGE_SOURCE_RE`):
`<img … src=…>` in jeder Schreibweise, mit oder ohne Anfuehrungszeichen.
DocVortex ersetzt nur den Wert und behaelt den Rest, wie das Modell ihn schrieb.

Anders als bei DocVortex liest der Ausdruck die Attribute vor `src` als Folgen
ohne Leerraum, Leerraum, auf den kein `src=` folgt, ein `<`, das kein neues
`<img` beginnt, und Werte in Anfuehrungszeichen, die ersten beiden possessiv
(sie werden nie anders aufgeteilt). Ein `<img` ausserhalb von
Anfuehrungszeichen beendet das Tag. So braucht er lineare Zeit. DocVortex'
Ausdruck brauchte fuer ein `<img` ohne `>` oder mit viel Leerraum quadratische
Zeit, und das Zusammensetzen laeuft im Daemon synchron: Ein entgleistes
Dokument hielte alle Jobs an.

Die echten Antworten tragen kein `<img`, sagen also nichts ueber die
Gleichwertigkeit. Gemessen ist sie an zufaelligen Bruchstuecken aus `<img`,
`src=`, `<`, `>`, Leerraum und Anfuehrungszeichen: Ohne Anfuehrungszeichen
findet der Ausdruck dieselben Quellen wie DocVortex. Abweichen kann er nur,
wenn ein `<img` ohne `>` einen Wert in Anfuehrungszeichen verschluckt, in dem
ein weiteres `<img … src=…>` steht, und danach ueber ein `<img` ausserhalb
der Anfuehrungszeichen weiterlaeuft: DocVortex legt dann das Bild hinter dem
verschluckten Wert ab, der Ausdruck benennt die Quelle darin um."""


def _verweise_umbenannt(wert: T, umbenannt: Mapping[str, str]) -> T:
    """Eine Kopie von `wert`, in deren Zeichenketten jeder Bildverweis umbenannt ist.

    Ein Verweis ist ein Schluessel von `umbenannt` (`images/{name}`) in den
    Formen, in denen MinerU 4 ein Bild nennt: am Anfang eines Feldes
    (`image_path` im Middle-JSON, `image_source` im strukturierten Inhalt), nach
    der Klammer eines Markdown-Bildes (`![…](images/…)`, DocVortex
    `build_markdown_image`) und als GANZE Quelle eines HTML-Bildes
    (`<img src="images/…">` in jeder Schreibweise, `_HTML_BILDQUELLE`). Die
    echten Antworten des Endpoints tragen nur die ersten beiden Formen.

    Alles andere bleibt stehen, auch wenn es den Pfad enthaelt: Eine externe
    URL, die auf denselben Pfad endet oder ihn als Parameter traegt
    (`https://example.org/render?src=images/…`; DocVortex laesst externe Bilder
    in visuellem HTML unveraendert), ist kein Verweis auf das eigene Bild. Da
    jeder Name auf seine Endung schliesst, ist keiner der Anfang eines anderen
    (`page_0_image_1.jpg`, `page_0_image_10.jpg`). MinerU schreibt die Verweise
    unmaskiert (DocVortex `normalize_image_source` laesst die Zeichen der Namen
    stehen), deshalb genuegt der woertliche Vergleich.
    """
    if not umbenannt:
        return wert
    muster = re.compile(
        r"(?:^|(?<=\())(" + "|".join(re.escape(alt) for alt in umbenannt) + r")"
    )

    def html_quelle(treffer: re.Match[str]) -> str:
        anfuehrung = treffer.group("anfuehrung") or ""
        quelle = treffer.group("gequotet") if anfuehrung else treffer.group("ungequotet")
        neu = umbenannt.get(quelle, quelle)
        return f"{treffer.group('vor')}{anfuehrung}{neu}{anfuehrung}"

    def ersetzen(teil: Any) -> Any:
        if isinstance(teil, str):
            teil = _HTML_BILDQUELLE.sub(html_quelle, teil)
            return muster.sub(lambda treffer: umbenannt[treffer.group(1)], teil)
        if isinstance(teil, dict):
            return {schluessel: ersetzen(inhalt) for schluessel, inhalt in teil.items()}
        if isinstance(teil, list):
            return [ersetzen(inhalt) for inhalt in teil]
        return teil

    ergebnis: T = ersetzen(wert)
    return ergebnis


def _seiten_verschoben(datei: dict[str, Any] | None, chunk: Chunk) -> dict[str, Any] | None:
    """Eine Kopie der Datei, in der jedes Seiten-`page_idx` die Seite des Originals zaehlt.

    Verschoben werden die `page_idx` unter `pages` und unter
    `extensions.docvortex_layout.pages` — die beiden Stellen, an denen MinerU 4
    eine Seite nennt (MinerU 4.0.12, „Intermediate JSON"; der strukturierte
    Inhalt traegt beide ebenso). Alles andere bleibt, wie es kam.

    Raises:
        TerminalRunPodError: Ein `page_idx` ist keine ganze Zahl. Dann ist der
            Versatz nicht berechenbar, und ihn unveraendert durchzureichen hiesse,
            eine falsche Seitenzahl zu speichern, die spaeter niemand mehr als
            falsch erkennt.
    """
    if datei is None:
        return None
    kopie = copy.deepcopy(datei)
    erweiterungen = kopie.get("extensions")
    layout = erweiterungen.get("docvortex_layout") if isinstance(erweiterungen, dict) else None
    for seiten in (kopie.get("pages"), layout.get("pages") if isinstance(layout, dict) else None):
        if not isinstance(seiten, list):
            continue
        for seite in seiten:
            if not isinstance(seite, dict) or "page_idx" not in seite:
                continue
            nummer = seite["page_idx"]
            if not isinstance(nummer, int) or isinstance(nummer, bool):
                raise TerminalRunPodError(
                    f"Seite mit nicht ganzzahligem page_idx ({nummer!r}, Typ "
                    f"{type(nummer).__name__}) im Teilstueck ({chunk.first_page}, "
                    f"{chunk.last_page}). Der Seitenversatz ist damit nicht "
                    f"berechenbar."
                )
            seite["page_idx"] = nummer + chunk.first_page
    return kopie


def _blocktext(block: Mapping[str, Any]) -> str:
    """Der Text eines Randblocks des Middle-JSON.

    Ein Randblock (`FURNITURE_TYPES`) traegt seinen Text als Liste von Spans
    (DocVortex 0.5.16, `schema.py`: `PageAuxTextBlock` und `PageFootnoteBlock`
    sind `InlineContentBlock`, ihr `content` ist eine Liste von `InlineSpan`), und
    jeder Span seinen Text als Zeichenkette im eigenen `content`. Die Spans werden
    aneinandergefuegt. Ein Block ohne Text liefert die leere Zeichenkette.

    Eine Inline-Formel (`equation_inline`) steht in `$` wie im Markdown der
    Engine (`$^{1}$`). Ohne sie fand `rendered_markdown` eine Seitenfussnote mit
    Fussnotenzeichen nicht im Markdown wieder und haengte sie ein zweites Mal
    an, gemessen an der echten normalen Seite des Endpoints.
    """
    inhalt = block.get("content")
    if block.get("type") == "equation_inline" and isinstance(inhalt, str):
        return f"${inhalt}$"
    if isinstance(inhalt, str):
        return inhalt
    if not isinstance(inhalt, list):
        return ""
    # Ein Eintrag, der wider Erwarten kein Objekt ist, bleibt sichtbar, statt
    # still zu verschwinden.
    return "".join(
        _blocktext(teil) if isinstance(teil, Mapping) else str(teil) for teil in inhalt
    )


def _bloecke(seite: Any) -> list[Any] | None:
    """Die Bloecke einer Seite des Middle-JSON, oder None bei fremder Form.

    EIN FEHLENDER `blocks`-SCHLUESSEL HEISST: KEINE BLOECKE. MinerU 4 schreibt
    Voreinstellungen nicht mit, und `PageInfo.blocks` hat die Voreinstellung
    `[]` (DocVortex 0.5.16, `schema.py`). Gemessen an den echten Antworten des
    MinerU-4-Endpoints fuer eine leere A4-Seite, als PNG und als PDF, in
    `advanced` mit OCR (`artifacts/2026-10-26-mineru-4.0.12-endpoint/`): Die
    Seite steht dort als `{"page_idx": 0}`. Die Ersatz-Antworten hatten `"blocks": []`
    angenommen, und so galt die echte leere Seite nicht als leer.
    """
    if not isinstance(seite, dict):
        return None
    bloecke = seite.get("blocks", [])
    return bloecke if isinstance(bloecke, list) else None


def _bloecke_aller_seiten(middles: list[Any]) -> list[Any]:
    """Jeder Block aller Seiten von `middle_json`, in Dokumentreihenfolge.

    Raises:
        IncompleteResultError: Ein Eintrag oder eine seiner Seiten hat nicht die
            Form des Schemas. Uebersprungen wird nichts: Ein uebersprungener
            Eintrag kann der Randblock mit dem Aktenzeichen sein.
    """
    bloecke: list[Any] = []
    for nummer, middle in enumerate(middles):
        seiten = middle.get("pages") if isinstance(middle, dict) else None
        if not isinstance(seiten, list):
            raise IncompleteResultError(
                f"Eintrag {nummer} von `middle_json` traegt keine Seitenliste "
                f"({type(middle).__name__}). Uebersprungen wird er NICHT: Er kann "
                f"den Randblock mit dem Aktenzeichen tragen, und die Ausgabe saehe "
                f"dann vollstaendig aus. Was da ist, steht unveraendert in `raw`."
            )
        for seite in seiten:
            auf_der_seite = _bloecke(seite)
            if auf_der_seite is None:
                raise IncompleteResultError(
                    f"Eine Seite in Eintrag {nummer} von `middle_json` traegt keine "
                    f"Blockliste. Was da ist, steht unveraendert in `raw`."
                )
            bloecke.extend(auf_der_seite)
    return bloecke


def is_empty(result: dict[str, Any]) -> bool:
    """Sagt, ob ein vollstaendig verarbeitetes Dokument gar keinen Inhalt hatte.

    Warum das eine eigene Frage ist: Ein leeres Ergebnis ist GUELTIG. Eine
    wirklich leere Seite liefert wahrheitsgemaess nichts. Wer das als Erfolg
    verbucht, verliert die Seite unsichtbar; wer es als Fehler verbucht, legt sie
    endlos wieder vor und bezahlt jedes Mal. Deshalb ist `empty` ein eigener
    Endzustand (Story 25).

    DIE REGEL: Leer ist ein Dokument genau dann, wenn sein `markdown` und sein
    `markdown_full` nach dem Entfernen von Leerraum leer sind, keine Seite seines
    Middle-JSON einen einzigen Block traegt und es kein Bild hat. Im Zweifel gilt
    es als NICHT leer — ein faelschlich als fertig verbuchtes leeres Dokument
    faellt beim Ansehen auf, ein faelschlich als leer verbuchtes volles nie:

        Jeder Block zaehlt, auch einer ohne Text und auch ein Randblock. Ein
        Bescheid, von dem nur der Briefkopf mit dem Aktenzeichen erkannt wurde,
        ist alles andere als leer (siehe `FURNITURE_TYPES`).

        Ein Bild zaehlt, auch eines, auf das nichts verweist: bezahlter Inhalt.

        Ein fehlendes oder unlesbares Middle-JSON zaehlt als Inhalt, denn ueber
        seine Bloecke laesst sich dann nichts sagen.

    AN DEN ECHTEN ANTWORTEN DES ENDPOINTS FESTGESTELLT: Der MinerU-4-Endpoint
    liefert fuer eine leere A4-Seite, als PNG und als PDF, in `advanced` mit OCR
    `COMPLETED`, leeres `markdown.md` und `markdown_full.md`, keine Bilder und im
    Middle-JSON die Seite `{"page_idx": 0}` ohne `blocks`
    (`artifacts/2026-10-26-mineru-4.0.12-endpoint/`; siehe `_bloecke`).

    Args:
        result: Das zusammengesetzte Ergebnis aus `assemble`.

    Kosten: keine.
    """
    for feld in ("markdown", "markdown_full"):
        text = result.get(feld)
        if isinstance(text, str) and text.strip():
            return False
    bilder = result.get("images")
    if not isinstance(bilder, dict) or bilder:
        return False
    middles = result.get("middle_json")
    if not isinstance(middles, list):
        return False
    try:
        return not _bloecke_aller_seiten(middles)
    except IncompleteResultError:
        return False


def _pflichtfeld(result: dict[str, Any], name: str, typ: type[W]) -> W:
    """Holt ein Feld des GESPEICHERTEN Ergebnisses. Fehlt es, ist das ein Fehler.

    Hier ist die LESESEITE: Das Ergebnis liegt laengst in der Datenbank, es gibt
    kein `extras` mehr, in das etwas gerettet werden koennte, und der Aufrufer
    hat nach einer Darstellung gefragt. Ein stiller Ersatzwert waere hier kein
    Aufheben, sondern ein Verlust mit freundlichem Gesicht.

    Ein fehlendes Feld ist dabei genauso eine Beschaedigung wie ein falscher Typ:
    `assemble` schreibt `markdown` und `middle_json` immer, und es gibt keine
    gespeicherten Ergebnisse aus der Zeit vor MinerU 4, die anders aussaehen
    (Mineroo#1, „Nur noch MinerU 4"). Fehlt `markdown`, kaemen nur die
    Randbloecke heraus und der Fliesstext waere lautlos weg; fehlt
    `middle_json`, fehlte das Aktenzeichen. Beides saehe nach einem kurzen
    Dokument aus und nicht nach einem Fehler.

    Raises:
        IncompleteResultError: Das Feld fehlt, oder es ist da und hat einen
            anderen Typ (haeufigster Fall: JSON-`null`).
    """
    if name not in result:
        raise IncompleteResultError(
            f"Dem gespeicherten Ergebnis fehlt das Feld {name!r}. "
            f"`assembly.assemble` schreibt es immer — die Zeile ist also "
            f"beschaedigt oder von fremder Hand geschrieben. Sie wird NICHT mit "
            f"einem leeren Wert dargestellt: Die Ausgabe saehe dann nach einem "
            f"kurzen Dokument aus statt nach einem Fehler. Was da ist, steht "
            f"unveraendert in `raw`."
        )
    wert = result[name]
    if isinstance(wert, typ):
        return wert
    raise IncompleteResultError(
        f"Das Feld {name!r} des gespeicherten Ergebnisses ist "
        f"{type(wert).__name__} statt {typ.__name__}. Es wird NICHT durch einen "
        f"leeren Wert ersetzt: Ein leeres Feld sieht aus wie ein leeres "
        f"Dokument, und die Ausgabe waere dann von einem gueltigen Ergebnis "
        f"nicht zu unterscheiden. Die Zeile ist beschaedigt; ihr Inhalt steht "
        f"unveraendert in `raw`."
    )


def _randbloecke(middles: list[Any]) -> list[str]:
    """Die Texte der Randbloecke aller Seiten, in Dokumentreihenfolge.

    Raises:
        IncompleteResultError: siehe `_bloecke_aller_seiten`; dazu ein Block, der
            kein Objekt ist. Uebersprungen wird er NICHT: Ob er ein Randblock
            war, laesst sich nicht sagen, und er kann das Aktenzeichen tragen.
    """
    texte: list[str] = []
    for block in _bloecke_aller_seiten(middles):
        if not isinstance(block, dict):
            raise IncompleteResultError(
                f"Ein Block in `middle_json` ist {type(block).__name__} statt "
                f"eines Objekts. Uebersprungen wird er NICHT: Er kann der "
                f"Randblock mit dem Aktenzeichen sein, und die Ausgabe saehe dann "
                f"vollstaendig aus. Was da ist, steht unveraendert in `raw`."
            )
        if block.get("type") in FURNITURE_TYPES:
            texte.append(_blocktext(block).strip())
    return texte


def middle_json_lesbar(middle: Any) -> None:
    """Prueft, ob die Darstellung das Middle-JSON eines Teilstuecks lesen kann.

    Dieselben Leser wie `rendered_markdown` (`_randbloecke`), damit die
    Vollstaendigkeitspruefung (`vollstaendigkeit.teilstueck_pruefen`) kein
    Teilstueck durchlaesst, dessen Darstellung danach dauerhaft scheitert.

    Raises:
        IncompleteResultError: siehe `_randbloecke`.

    Kosten: keine.
    """
    _randbloecke([middle])


_MASKIERT: Final = re.compile(r"\\([*_`~$])")
"""Ein Zeichen, das DocVortex 0.5.16 im Markdown mit einem Backslash maskiert
(`render/_internal/markdown/escaping.py`, `escape_conservative_markdown_text`:
`*`, `_`, `` ` ``, `~`, `$`)."""


_DOCVORTEX_ENTITY: Final = re.compile(r"&(amp|lt|gt);")
"""Die Entities, die DocVortex 0.5.16 beim Maskieren von Text schreibt
(`render/_internal/markdown/inline.py`: `html.escape(…, quote=False)` fuer Text,
der wie ein HTML-Tag aussieht, `_escape_entity_like_text` fuer Text, der wie
eine Entity aussieht). Andere Entities im Markdown hat DocVortex nicht
geschrieben, etwa im Inline-Code, den es roh ausgibt; sie bleiben, wie man sie
liest.

Die Grenze: Code wird dabei nicht ausgenommen. Steht `&lt;`, `&gt;` oder
`&amp;` woertlich in Code, wird es fuer den Vergleich trotzdem zum Zeichen, und
ein Randblock mit genau diesem Text gilt als vorhanden (`&lt;Az 12/34&gt;` im
Code verdeckt eine Kopfzeile „<Az 12/34>“). Ausgegeben wird das Markdown
unveraendert; die Darstellung fuer Abnehmer entscheidet Mineroo#13."""

_ENTITY_ZEICHEN: Final = {"amp": "&", "lt": "<", "gt": ">"}
"""Das Zeichen zu jeder Entity von `_DOCVORTEX_ENTITY`."""


def _ohne_backslash(text: str) -> str:
    """Der Text ohne einen Backslash vor `*`, `_`, `` ` ``, `~` und `$`.

    Fuer beide Seiten des Vergleichs eines Randblocks mit dem Markdown: Im
    Markdown setzt DocVortex den Backslash (`2025\\_17`), und im Rohtext steht
    er manchmal schon, weil MinerU ein `$` wie in LaTeX als `\\$` liefert (am
    Endpoint gemessen, QA zu Mineroo#11); DocVortex maskiert ein Zeichen hinter
    einem Backslash dann nicht noch einmal. Ohne Backslash lauten beide gleich.
    """
    return _MASKIERT.sub(r"\1", text)


def _ohne_maskierung(markdown: str) -> str:
    """Das Markdown, wie es ohne DocVortex' Maskierung lautete.

    Nur fuer den Vergleich mit dem Rohtext eines Randblocks (`_ohne_backslash`):
    Text, der wie ein HTML-Tag oder eine Entity aussieht, steht im Markdown
    zudem als Entity (`&lt;Az 12/34&gt;`, `_DOCVORTEX_ENTITY`). Ausgegeben wird
    das Markdown immer unveraendert.
    """
    return _DOCVORTEX_ENTITY.sub(
        lambda treffer: _ENTITY_ZEICHEN[treffer.group(1)], _ohne_backslash(markdown)
    )


def rendered_markdown(result: dict[str, Any]) -> str:
    """Das GANZE Dokument in Markdown-Form. Leseseite, aendert nichts am Gespeicherten.

    Alles, was auf dem Dokument steht, in einer Darstellung: das Markdown der
    Engine und dahinter die Randbloecke (`FURNITURE_TYPES`), die es nicht
    enthaelt. KEINE SCHALTER: Was davon unerwuenscht ist, weiss nur der
    Abnehmer; er hat die Bausteine (`markdown`, `markdown_full` und je Block
    `middle_json`).

    Die Randbloecke kommen aus dem Middle-JSON, der massgeblichen Datei unter
    MinerU 4 (Mineroo#11). Die Aufnahmen des Endpoints zeigen, dass der
    Vollmodus-Markdown des Workers (`markdown_full.md`) alle fuenf Randklassen
    traegt (`FURNITURE_TYPES`); ob er damit an die Stelle dieser Ergaenzung
    tritt, entscheidet das Ticket „Text fuer Abnehmer" (Mineroo#13). Bis dahin
    gilt die Regel aus der Zeit von MinerU 3.4, auf das neue Format uebertragen.

    WARUM DIE RANDBLOECKE ZAEHLEN: In ihnen liegt das Aktenzeichen. „Der Filter
    trifft nicht die Kennung, sondern die Zone." Die volle Rechnung steht bei
    `FURNITURE_TYPES`.

    Args:
        result: Das zusammengesetzte Ergebnis.

    Zusicherung:
        Das Ergebnis BEGINNT mit dem Markdown der Engine, zeichengleich; alles
        Weitere ist angehaengt. Es wird nie etwas entfernt, nur ergaenzt.

        Angehaengt wird ein Randblock nur, wenn sein Text im Markdown FEHLT.
        Verglichen wird ohne Maskierung, auf beiden Seiten
        (`_ohne_maskierung`, `_ohne_backslash`): Im Markdown steht `2025_17` als
        `2025\\_17`, im Rohtext ein `$` manchmal schon als `\\$`, und das ist
        jeweils derselbe Text. Verglichen wird gegen das Markdown und nicht gegen die bereits
        angehaengten: Elf Seiten mit derselben Fusszeile sind elfmal dieselbe
        Fusszeile, kein Duplikat. Die Randbloecke kommen in Dokumentreihenfolge,
        ohne eingefuegte Marker.

        UND: Es kommt entweder das ganze Dokument heraus oder eine Ausnahme.
        Niemals eine Zeichenkette, die weniger ist, als der Name verspricht.

    Raises:
        IncompleteResultError: Das gespeicherte Ergebnis ist beschaedigt — ein
            fehlendes Feld, ein Feld mit falschem Typ, ein Eintrag von
            `middle_json` ohne Seiten (auch einer, den der Worker nicht liefern
            konnte), ein Block, der kein Objekt ist, oder gar kein Ergebnis.
            Nichts davon ist verloren; es steht unveraendert in `raw`.

    Kosten: keine.
    """
    if not result:
        raise IncompleteResultError(
            "Die Zeile ist terminal, traegt aber ueberhaupt kein Ergebnis — "
            "`raw` ist leer oder NULL. Es gibt nichts darzustellen. Was der "
            "Lauf gemacht hat, steht in `notes` und `error`."
        )
    if "markdown" not in result and "middle_json" not in result:
        raise IncompleteResultError(
            "Dieses Ergebnis traegt weder `markdown` noch `middle_json` und ist "
            "damit gar kein zusammengesetztes Ergebnis. Die eine bekannte Form "
            "dieser Art ist die Notfassung aus dem Fehlerpfad "
            "(`_rohfassung_der_teile`, erkennbar an `chunks` und "
            "`assembly_failed`); sie haengt an `failed`/`input_error` und sollte "
            "die Darstellung im Betrieb nie erreichen. Was da ist, steht "
            "unveraendert in `raw`."
        )
    markdown = _pflichtfeld(result, "markdown", str)
    middles = _pflichtfeld(result, "middle_json", list)

    vergleich = _ohne_maskierung(markdown)
    angehaengt = [
        text
        for text in _randbloecke(middles)
        if text and _ohne_backslash(text) not in vergleich
    ]
    if not angehaengt:
        return markdown
    if not markdown:
        return "\n\n".join(angehaengt)
    return markdown + "\n\n" + "\n\n".join(angehaengt)
