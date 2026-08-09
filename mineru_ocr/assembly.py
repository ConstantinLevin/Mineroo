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

Zusammengesetzt heisst NICHT reduziert. Weggelassen wird nichts: `middle`, die
Layout- und Span-PDFs, die `debug`-Bloecke, die Ausfuehrungszeiten — alles wandert
in das Ergebnis. Zusammengesetzt heisst nur, dass Text, Strukturliste und Bilder
wieder in der Reihenfolge und mit der Zaehlung stehen, die sie vor dem Schnitt
hatten.

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
import io
import json
import tarfile
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final, TypeVar

from .chunking import Chunk
from .errors import IncompleteResultError, TerminalRunPodError

W = TypeVar("W")
"""Der erwartete Typ eines benannten Antwortfeldes in `_geprueft` — `str` fuer
`markdown`, `list` fuer `content_list`, `dict` fuer `images` und `middle`."""

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
Auffindbarkeit der Steuerakten entschieden wird."""


@dataclass(frozen=True)
class ChunkArtifacts:
    """Was die Antwort EINES Teilstuecks an verwertbarem Inhalt enthielt.

    Reines Laufzeit-Zwischenergebnis im Executor: entsteht beim Auspacken einer
    Antwort und ist wieder verschwunden, sobald `assemble` daraus ein Dokument
    gemacht hat. Nichts davon wird gespeichert, nichts davon verlaesst den
    Executor.
    """

    chunk: Chunk
    """Welchen Seitenbereich des Originals dieses Teilstueck trug. Hier — und nur
    hier, zur Laufzeit — existiert der Versatz, mit dem `page_idx` zurueckgerechnet
    wird."""

    markdown: str
    """Der Text dieses Teilstuecks. Kann leer sein; das ist ein gueltiger Zustand
    (leere Seite), kein Fehler."""

    content_list: list[dict[str, Any]]
    """Die Strukturliste. Ihre `page_idx`-Werte sind teilstueck-lokal und beginnen
    bei 0, weil MinerU vor dem Parsen schneidet (am Quelltext geprueft,
    05.08.2026). Der Versatz wird in `assemble` aufgeschlagen."""

    images: dict[str, bytes]
    """Bilder unter ihrem Dateinamen. Die Namen sind Inhalts-Hashes und damit
    ueber Teilstuecke hinweg kollisionsfrei (empirisch geprueft am 05.08.2026) —
    darauf beruht, dass `assemble` einfach vereinigen darf.

    ZWEI EIGENSCHAFTEN, die man kennen muss, bevor man diese Bilder mit der
    `content_list` zusammenbringt. Beide sind am 08.08.2026 ueber alle sechs
    konservierten Antworten gezaehlt:

    ERSTENS ist der Schluessel hier der NACKTE Dateiname
    (`22316c5d…378712.jpg`), waehrend die `content_list` denselben Bildblock mit
    `"img_path": "images/22316c5d…378712.jpg"` fuehrt — also MIT Verzeichnis-
    Praefix. Wer den einen Wert direkt im anderen nachschlaegt, findet nichts und
    haelt das Bild fuer fehlend. Das Praefix gehoert vor dem Nachschlagen
    abgeschnitten.

    ZWEITENS enthaelt `images` MEHR Bilder, als die `content_list` ueber
    `img_path` erwaehnt: gezaehlt 3 gegen 1 Erwaehnung, 7 gegen 4, 6 gegen 1,
    6 gegen 3. Wer die Bilder an den Erwaehnungen ausrichtet und den Rest
    verwirft, wirft bezahlte Artefakte weg — genau der Fehler, gegen den die
    Abruf-Regel steht (alles holen, alles persistieren, nie filtern). Deshalb
    wandert hier ALLES weiter, unabhaengig davon, ob es erwaehnt wird. Woher die
    ueberzaehligen Bilder stammen (Markdown-Verweise, `middle`, oder gar nichts),
    ist NICHT untersucht — die Zahlen sind gezaehlt, ihre Ursache ist es nicht."""

    middle: dict[str, Any]
    """MinerUs internes Zwischenformat, unveraendert."""

    layout_pdf: bytes | None
    """Die Layout-Visualisierung, sofern die Antwort sie trug. Nur der Tarball
    liefert sie; beim Inline-Transport ist sie None."""

    span_pdf: bytes | None
    """Die Span-Visualisierung — mit unserem Backend IMMER None.

    AM QUELLTEXT GEKLAERT (08.08.2026, `mineru/cli/common.py` auf master, dazu
    die offizielle Doku der Ausgabedateien). `do_parse` und `aio_do_parse`
    tragen zwar `f_draw_span_bbox=True` als Vorgabe, aber JEDER VLM-Pfad
    ueberschreibt sie bedingungslos, gleich in der ersten Zeile:

        async def _async_process_vlm(...):
            parse_method = "vlm"
            f_draw_span_bbox = False

    Dasselbe in `_process_vlm`, beiden Hybrid-Pfaden und `_process_office_doc`.
    Nur `_process_pipeline` reicht den Parameter durch. Die Doku sagt es
    ebenso: „Text Spans File (span.pdf) — Note: Only applicable to pipeline
    backend."

    Wir fahren `backend="vlm-auto-engine"`. Dieses Feld kann also nicht
    befuellt werden, solange das so bleibt, und die `span`-Klausel in
    `_aus_tarball` kann nicht greifen. Beide bleiben trotzdem stehen: Sie
    kosten nichts, und ein Wechsel auf `pipeline` — der als Rueckfallweg
    ausdruecklich offen ist — braucht sie sofort wieder.

    WARUM DAS HIER SO AUSFUEHRLICH STEHT: Vorher stand an dieser Stelle „Die
    Span-Visualisierung, ebenso" — also: kommt ebenso wie das Layout-PDF mit
    dem Tarball. Diese Aussage stammt aus einer Quelltext-Pruefung vom
    05.08.2026, die die SIGNATUR von `aio_do_parse` gelesen und daraus
    geschlossen hat, der Worker ueberschreibe die Vorgabe nicht. Der zweite
    Halbsatz stimmt; die Folgerung nicht, weil MinerU selbst ueberschreibt,
    eine Funktionsebene tiefer. Ein Default in einer Signatur ist keine
    Zusicherung ueber das Verhalten."""

    envelope: dict[str, Any]
    """Was die Antwort ausser dem Inhalt trug: `status`, `id`, `executionTime`,
    `delayTime`, `workerId`, der `debug`-Block, `mineru_version`. Wird nicht
    ausgewertet, aber auch nicht weggeworfen — es ist die Herkunft eines bezahlten
    Ergebnisses."""

    extras: dict[str, Any]
    """ALLES, was die Antwort sonst noch trug und wir nicht benannt haben.

    DIES IST DIE ABRUF-REGEL ALS BAUTEIL, und es gibt dieses Feld, weil sie ohne
    es verletzt war. Die Regel lautet: alles holen, alles persistieren, nie
    filtern. Ohne dieses Feld tat das Auspacken genau das Gegenteil — es griff
    die vier Felder heraus, die es kannte, und liess den Rest fallen. Und der
    Rest war nicht leer:

        Beim INLINE-Transport gingen an jeder der sechs konservierten echten
        Antworten drei Felder verloren — `basename`, `source` und
        `pages_requested`. Gemessen am 08.08.2026, nicht vermutet.

        Beim TARBALL-Transport ging jede Datei verloren, deren Name auf keines
        der erwarteten Muster passte. Die Zuordnung geschieht ueber Muster, weil
        von den Dateinamen nur EINER belegt ist — genau deshalb ist die Chance,
        dass etwas Unbekanntes dabei ist, nicht klein, sondern die
        wahrscheinlichste aller Lagen.

    Was hier landet, wird nie ausgewertet und nie gelesen. Es wird aufgehoben.
    Das ist der ganze Zweck: Eine Antwort ist dreissig Minuten nach ihrer
    Entstehung nirgends mehr abrufbar, und was wir in diesem Moment nicht
    aufheben, ist fuer immer weg — bezahlt und weg.

    Beim Tarball sind die Werte Base64-Zeichenketten unter dem vollen Pfad im
    Archiv; beim Inline-Transport sind es die Werte, wie sie in der Antwort
    standen. Beides ist JSON-faehig und geht damit unveraendert in `raw`."""


def unpack_response(response: dict[str, Any], chunk: Chunk) -> ChunkArtifacts:
    """Packt eine volle `/status`-Antwort in ihre Bestandteile aus.

    Beherrscht beide Transportarten, und zwar aus einem Bestandsgrund: Wir
    versenden `tarball_b64`, aber die sechs konservierten echten Antworten vom
    05.08.2026 sind `inline`. Sie sind das einzige echte Testmaterial, das ohne
    einen bezahlten Lauf existiert; ein Auspacker, der nur den Tarball kann, waere
    gegen NICHTS pruefbar. Umgekehrt gilt: Der Tarball-Pfad hat bis zum ersten
    Live-Lauf kein Offline-Testmaterial, und diese Luecke ist benannt, nicht
    ueberdeckt.

    Beim Tarball wird das Base64-Feld dekodiert und das Archiv im Speicher
    ausgelesen (`base64`, `tarfile`, `io.BytesIO` — Standardbibliothek); beim
    Inline-Transport werden die Felder entnommen. In beiden Faellen wird nur
    gelesen; die uebergebene Antwort bleibt unberuehrt.

    Args:
        response: Die vollstaendige Antwort mit `status`, `id`, `output` und allem
            uebrigen — nicht nur der `output`-Teil.
        chunk: Welchen Seitenbereich diese Antwort trug.

    Vorbedingung:
        `response["status"] == "COMPLETED"`. Nicht erfolgreiche Endzustaende
        werden VOR dem Auspacken vom Executor in eine Ausnahme uebersetzt (Schritt
        3b dort) — hier kommen sie nicht an.

    Zusicherung:
        Verlustfrei: Was die Antwort trug, steht danach in einem Feld von
        `ChunkArtifacts`, auch das, was niemand auswertet (`envelope`). Was die
        Antwort nicht trug, wird nicht erfunden — fehlt ein Feld, ist das Ergebnis
        leer bzw. None, ohne stillen Ersatzwert.

    Raises:
        TerminalRunPodError: Die Antwort hat nicht die erwartete Form — kein
            `output`, kein `results`, unlesbarer Tarball. Endgueltig: Ein zweiter
            Versuch kauft dieselbe unlesbare Antwort.

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

    # Der Umschlag: alles, was die Antwort ausser dem Inhalt trug. Wird nicht
    # ausgewertet, aber auch nicht weggeworfen — es ist die Herkunft eines
    # bezahlten Ergebnisses.
    umschlag: dict[str, Any] = {
        schluessel: wert for schluessel, wert in response.items() if schluessel != "output"
    }
    umschlag["output_meta"] = {
        schluessel: wert for schluessel, wert in ausgabe.items() if schluessel != "results"
    }

    if "tarball_b64" in ergebnis:
        return _aus_tarball(ergebnis, chunk, umschlag)
    return _aus_inline(ergebnis, chunk, umschlag)


def _aus_inline(
    ergebnis: dict[str, Any], chunk: Chunk, umschlag: dict[str, Any]
) -> ChunkArtifacts:
    """Entnimmt die Felder des Inline-Transports.

    Layout- und Span-PDF sind hier IMMER None, und das ist kein Versaeumnis
    dieser Funktion: Der Inline-Transport liefert nur die vier benannten Formate,
    das Layout-PDF entsteht zwar im Ausgabeverzeichnis, aber nur Tarball und S3
    packen dieses Verzeichnis ein (Betriebshandbuch 14a). „Tarball traegt also
    strikt mehr als Inline" — das gilt weiter, aber die Begruendung ist um das
    Span-PDF zu kuerzen: Das entsteht mit unserem Backend gar nicht erst, in
    keinem Transport. Warum, steht am Feld `ChunkArtifacts.span_pdf`.
    """
    benannt = {"markdown", "content_list", "images", "middle"}

    # Alles Uebrige aufheben — die Abruf-Regel. An den sechs echten Antworten
    # sind das `basename`, `source` und `pages_requested`; bei einer kuenftigen
    # Fassung der Engine womoeglich mehr, und genau darum wird hier nicht
    # aufgezaehlt, sondern der Rest genommen.
    uebrig: dict[str, Any] = {
        schluessel: wert
        for schluessel, wert in ergebnis.items()
        if schluessel not in benannt
    }

    # Die vier benannten Felder werden auf ihren Typ GEPRUEFT und nicht bloss mit
    # einem Vorgabewert geholt. Der Unterschied ist der Fall, den JSON kennt und
    # ein Python-Vorgabewert nicht: ein VORHANDENER Schluessel mit dem Wert
    # `null`. `ergebnis.get("markdown", "")` liefert dann `None`, weil der
    # Schluessel ja da ist — und `None` ist keine Zeichenkette, obwohl
    # `ChunkArtifacts.markdown: str` es zusichert.
    #
    # Teuer war daran, dass der Schaden nicht dort auftrat, wo er entstand:
    # `assemble` warf spaeter beim `"\n\n".join(...)` einen `TypeError`, das
    # Dokument endete `failed` statt `empty`, und die bezahlte Antwort war
    # unbrauchbar — beim Wiederlauf genauso. Ein Feld mit unerwartetem Typ geht
    # dabei NICHT verloren: Es wandert unter seinem eigenen Namen in `extras`.
    # Die Abruf-Regel gilt auch fuer Formfehler.
    markdown = _geprueft(ergebnis, "markdown", str, "", uebrig)
    content_list = _geprueft(ergebnis, "content_list", list, [], uebrig)
    rohbilder = _geprueft(ergebnis, "images", dict, {}, uebrig)
    middle = _geprueft(ergebnis, "middle", dict, {}, uebrig)

    return ChunkArtifacts(
        chunk=chunk,
        markdown=markdown,
        content_list=content_list,
        images=_bilder_dekodieren(rohbilder),
        middle=middle,
        layout_pdf=None,
        span_pdf=None,
        envelope=umschlag,
        extras=uebrig,
    )


def _geprueft(
    quelle: dict[str, Any],
    schluessel: str,
    typ: type[W],
    ersatz: W,
    uebrig: dict[str, Any],
) -> W:
    """Holt ein benanntes Feld, aber nur mit dem erwarteten Typ.

    Hat das Feld einen anderen Typ — der haeufigste Fall ist JSON-`null` —, wird
    der Ersatzwert zurueckgegeben UND der tatsaechliche Wert in `uebrig`
    abgelegt, unter seinem eigenen Namen. Damit ist er in `extras` und geht nicht
    verloren; still zu einem leeren Wert zu werden waere genau der Verlust ohne
    Fehlersignal, gegen den die Abruf-Regel steht.

    `quelle` wird NICHT veraendert — `unpack_response` sichert zu, dass die
    uebergebene Antwort unberuehrt bleibt. Geschrieben wird ausschliesslich in
    `uebrig`, das dieser Funktion gehoert und ohnehin nach `extras` wandert.
    """
    wert = quelle.get(schluessel, ersatz)
    if isinstance(wert, typ):
        return wert
    uebrig[schluessel] = wert
    return ersatz


def _bilder_dekodieren(roh: Any) -> dict[str, bytes]:
    """Bringt die Bilder auf Bytes, egal wie sie ankamen.

    AM ECHTEN MATERIAL FESTGESTELLT (08.08.2026, alle sechs konservierten
    Antworten): Der Inline-Transport uebergibt `images` als Objekt, dessen Werte
    Base64-ZEICHENKETTEN sind; dekodiert ergeben sie JPEG. Gezaehlt wurden 22
    Bilder ueber vier der sechs Antworten (0, 0, 3, 7, 6, 6), jedes einzelne
    dekodiert sauber und traegt eine gueltige JPEG-Signatur. Die Namen sind
    Inhalts-Hashes mit Endung, etwa
    `22316c5d10157247086005ef9387b08b3099b520cc7d5ba0d9ce193134378712.jpg`.

    Diese Feststellung ersetzt eine frueher hier stehende Behauptung, `images`
    sei in allen sechs Antworten leer. Die war falsch und entstand daraus, dass
    nur EINE Antwort angesehen und das Ergebnis auf alle sechs verallgemeinert
    wurde; zufaellig ist die erste Antwort eine der beiden bildlosen.

    Rohe Bytes werden trotzdem angenommen: Der Tarball-Pfad liefert die Bilder
    als Dateien aus dem Archiv, also als Bytes. Beide Wege muenden hier, und
    beide sollen dasselbe ergeben. Bei allem anderen wird geworfen, statt still
    etwas Falsches durchzureichen.
    """
    if not isinstance(roh, dict):
        raise TerminalRunPodError(
            f"`images` ist kein Objekt, sondern {type(roh).__name__}."
        )
    bilder: dict[str, bytes] = {}
    for name, wert in roh.items():
        if isinstance(wert, bytes):
            bilder[name] = wert
        elif isinstance(wert, str):
            try:
                bilder[name] = base64.b64decode(wert, validate=True)
            except (binascii.Error, ValueError) as fehler:
                raise TerminalRunPodError(
                    f"Bild {name!r} ist eine Zeichenkette, aber kein gueltiges "
                    f"Base64: {fehler}"
                ) from fehler
        else:
            raise TerminalRunPodError(
                f"Bild {name!r} hat den unerwarteten Typ {type(wert).__name__}; "
                f"erwartet wurden Bytes oder eine Base64-Zeichenkette."
            )
    return bilder


def _aus_tarball(
    ergebnis: dict[str, Any], chunk: Chunk, umschlag: dict[str, Any]
) -> ChunkArtifacts:
    """Packt das Base64-Archiv im Speicher aus und ordnet die Dateien zu.

    Zugeordnet wird nach ENDUNG und Namensmuster, nicht nach fest erwarteten
    Dateinamen. Der Grund ist Ehrlichkeit ueber das, was wir wissen: Aus der
    Doku belegt ist genau ein Name, und der traegt schon zwei Generationen —
    `package.py` sucht `{basename}_content_list.json` und faellt auf
    `{basename}_content_list_v2.json` zurueck (Betriebshandbuch 14a). Die
    uebrigen Namen sind NICHT belegt; sie fest zu verdrahten hiesse, sie zu
    erfinden. Ein Muster-Abgleich ueberlebt eine dritte Generation, ein
    verdrahteter Name nicht.

    Und wenn nichts passt, wird geworfen und der Archiv-Inhalt genannt — der
    erste Live-Lauf sagt uns die Wahrheit dann laut, statt ein leeres Ergebnis
    zu liefern, das wie eine leere Seite aussieht.
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

    markdown = ""
    content_list: list[dict[str, Any]] = []
    middle: dict[str, Any] = {}
    images: dict[str, bytes] = {}
    layout_pdf: bytes | None = None
    span_pdf: bytes | None = None
    gesehen: list[str] = []
    uebrig: dict[str, Any] = {}

    # WELCHE PLAETZE SCHON BELEGT SIND — als eigene Menge und ausdruecklich NICHT
    # am Wahrheitswert der Felder abgelesen. Der Unterschied ist am 08.08.2026 an
    # einer echten Antwort aufgefallen und war ein Fehler: Die erste Fassung
    # dieser Pruefung fragte `not content_list`, also den Wahrheitswert. Eine
    # GUELTIGE LEERE Liste ist aber falsy, und genau die liefert die Engine fuer
    # eine leere Seite — `doc_content_list.json` enthielt `[]`. Der Platz galt
    # damit weiter als frei, und die naechste Datei auf demselben Muster
    # (`doc_content_list_v2.json`, Inhalt `[[]]`) ueberschrieb ihn. Heraus kam
    # `[[]]` statt `[]`: das falsche Format, still eingesetzt.
    #
    # Bei einer nicht leeren v2-Fassung waere der Schaden groesser gewesen —
    # `assemble` macht `dict(eintrag)` ueber die Eintraege, und eine Liste von
    # Bloecken statt eines Blocks ergibt dort keinen Fehler, sondern Unsinn.
    #
    # „Leer" und „nicht da" sind zwei verschiedene Dinge, und nur das zweite ist
    # ein freier Platz.
    belegt: set[str] = set()

    try:
        with tarfile.open(fileobj=io.BytesIO(archivbytes), mode="r:*") as archiv:
            for eintrag in archiv:
                if not eintrag.isfile():
                    continue
                gesehen.append(eintrag.name)
                quelle = archiv.extractfile(eintrag)
                if quelle is None:
                    continue
                inhalt = quelle.read()
                name = eintrag.name
                kurz = name.rsplit("/", 1)[-1]

                # JEDER Zweig prueft erst, ob sein Platz noch frei ist. Ohne
                # diese Pruefung gewann bei zwei Dateien auf demselben Muster
                # schlicht die zuletzt gelesene, und die frueher gelesene war
                # ERSATZLOS weg — sie landete weder in ihrem Feld noch in
                # `uebrig`. Das ist die unsichtbarste Verlustform, die dieses
                # Modul kennt, und sie steht ausgerechnet an der Stelle, an der
                # das Modul selbst einraeumt, von den Dateinamen im Archiv sei
                # nur EINER belegt: Unter erklaerter Unkenntnis ueber die Namen
                # ist ein Doppeltreffer nicht der Sonderfall.
                #
                # Wer gewinnt, ist ausdruecklich die willkuerliche Entscheidung
                # — der zuerst gelesene —, und sie ist es nur deshalb gefahrlos,
                # weil der Verlierer vollstaendig in `uebrig` steht. Der erste
                # Live-Lauf zeigt dann an einer echten Antwort, ob es
                # Doppeltreffer ueberhaupt gibt und welcher der richtige ist.
                if kurz.endswith(".md") and "markdown" not in belegt:
                    markdown = inhalt.decode("utf-8")
                    belegt.add("markdown")
                elif (
                    "_content_list" in kurz
                    and kurz.endswith(".json")
                    and "content_list" not in belegt
                ):
                    content_list = _json_laden(inhalt, name, erwartet=list)
                    belegt.add("content_list")
                elif (
                    "_middle" in kurz
                    and kurz.endswith(".json")
                    and "middle" not in belegt
                ):
                    middle = _json_laden(inhalt, name, erwartet=dict)
                    belegt.add("middle")
                elif (
                    "layout" in kurz
                    and kurz.endswith(".pdf")
                    and "layout_pdf" not in belegt
                ):
                    layout_pdf = inhalt
                    belegt.add("layout_pdf")
                elif (
                    "span" in kurz
                    and kurz.endswith(".pdf")
                    and "span_pdf" not in belegt
                ):
                    span_pdf = inhalt
                    belegt.add("span_pdf")
                elif ("/images/" in name or name.startswith("images/")) and (
                    kurz not in images
                ):
                    images[kurz] = inhalt
                else:
                    # Entweder hat KEIN Muster gepasst, oder eines hat gepasst,
                    # aber sein Platz war schon belegt. In beiden Faellen wird
                    # die Datei aufgehoben statt fallengelassen. Von den
                    # Dateinamen im Archiv ist nur EINER belegt; dass etwas
                    # Unbekanntes dabei ist, ist damit nicht der Sonderfall,
                    # sondern die wahrscheinlichste Lage. Base64, weil `raw` eine
                    # jsonb-Spalte ist und Bytes dort keine Entsprechung haben.
                    #
                    # Der Schluessel ist der VOLLE Archivpfad und nicht der kurze
                    # Name: Der volle Pfad ist im Archiv eindeutig, der kurze
                    # nicht — und ein Doppeltreffer, der hier landet, darf nicht
                    # ausgerechnet beim Aufheben ein zweites Mal ueberschrieben
                    # werden.
                    uebrig[name] = base64.b64encode(inhalt).decode("ascii")
    except tarfile.TarError as fehler:
        raise TerminalRunPodError(
            f"`tarball_b64` liess sich nicht als Archiv lesen: {fehler}"
        ) from fehler

    if "markdown" not in belegt and "content_list" not in belegt:
        raise TerminalRunPodError(
            f"Im Archiv war weder ein Markdown noch eine content_list zu finden. "
            f"Enthaltene Dateien: {gesehen}. Die Zuordnung geschieht ueber "
            f"Namensmuster; passt keines, hat die Gegenseite ihre Dateinamen "
            f"geaendert und die Muster oben gehoeren nachgezogen."
        )
    # ES WIRD AUF ABWESENHEIT GEPRUEFT, NICHT AUF LEERE. Die frueherer Fassung
    # fragte `if not markdown and not content_list` — also den Wahrheitswert —
    # und haette damit eine ECHT LEERE SEITE fuer ein kaputtes Archiv gehalten.
    #
    # Am 08.08.2026 gemessen: Eine leere A4-Seite liefert `COMPLETED`, `ok: true`
    # und ein Archiv mit `doc.md` (0 Bytes), `doc_content_list.json` (`[]`),
    # `doc_middle.json` mit leeren `preproc_blocks`, `discarded_blocks` und
    # `para_blocks` — und trotzdem einem `doc_layout.pdf` (462 Bytes). Der
    # Unterschied ist keine Spitzfindigkeit: Ein Wurf an dieser Stelle machte aus
    # dem gueltigen Zustand `empty` ein `failed`, und `failed` bedeutet
    # Wiedervorlage und erneutes Bezahlen — genau der Fehler, wegen dessen
    # `empty` ueberhaupt ein eigener Endzustand ist.

    # Auch die Felder NEBEN `tarball_b64` gehoeren aufgehoben — beim Inline-
    # Transport tragen dieselben Stellen `basename`, `source` und
    # `pages_requested`, und es gibt keinen Grund anzunehmen, dass der Tarball
    # sie nicht ebenso traegt.
    for schluessel, wert in ergebnis.items():
        if schluessel != "tarball_b64":
            uebrig[schluessel] = wert

    return ChunkArtifacts(
        chunk=chunk,
        markdown=markdown,
        content_list=content_list,
        images=images,
        middle=middle,
        layout_pdf=layout_pdf,
        span_pdf=span_pdf,
        envelope=umschlag,
        extras=uebrig,
    )


def _json_laden(inhalt: bytes, name: str, *, erwartet: type) -> Any:
    """Liest eine JSON-Datei aus dem Archiv und prueft ihre aeussere Form."""
    try:
        wert = json.loads(inhalt.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as fehler:
        raise TerminalRunPodError(
            f"{name} im Archiv ist kein lesbares JSON: {fehler}"
        ) from fehler
    if not isinstance(wert, erwartet):
        raise TerminalRunPodError(
            f"{name} im Archiv ist {type(wert).__name__}, erwartet war "
            f"{erwartet.__name__}."
        )
    return wert


def assemble(parts: Sequence[ChunkArtifacts]) -> dict[str, Any]:
    """Macht aus den Teilstuecken EIN Dokument. Das Ergebnis ist, was gespeichert wird.

    Der Kern dieses Moduls und der einzige Ort, an dem die Seitenzahlen des
    Originals wiederhergestellt werden. Danach gibt es keine Teilstuecke mehr —
    weder im Rueckgabewert noch irgendwo dahinter.

    Was zusammengesetzt wird:
        `markdown`: die Texte in Dokumentreihenfolge, durch eine Leerzeile
        getrennt (`"\\n\\n".join`, ohne Nachbearbeitung). Keine
        Ueberschriften-Reparatur, kein Zusammenziehen ueber die Schnittkante,
        keine Doppelten-Erkennung — jede solche Klugheit waere eine
        Auswertungs-Entscheidung an einer Stelle, an der niemand sie erwartet,
        und im Ergebnis nicht mehr von einem Fehler der Engine zu unterscheiden.

        `content_list`: die Eintraege in Dokumentreihenfolge, wobei auf jedes
        `page_idx` der `first_page` seines Teilstuecks addiert wird. Das ist die
        eine Addition, um die es hier geht, und die fehleranfaelligste Stelle des
        ganzen Vorgangs: MinerU schneidet vor dem Parsen, also faengt `page_idx`
        in JEDEM Teilstueck wieder bei 0 an. Ohne die Korrektur traegt ein
        120-Seiten-Dokument sechsmal eine „Seite 0" — und weil die Liste trotzdem
        vollstaendig und plausibel aussieht, faellt es niemandem auf.

        `images`: die Vereinigung. Gleiche Namen sind gleiche Inhalte, weil die
        Namen Inhalts-Hashes sind.

        `middle`: eine Liste, ein Eintrag je Teilstueck, in Dokumentreihenfolge —
        ausdruecklich NICHT verschmolzen. `middle` ist MinerU-intern versioniert
        (die Antworten tragen `_version_name` und `_backend`); ein selbst
        ausgedachtes Zusammenfuehren waere eine Behauptung ueber ein fremdes
        Format, die schon beim naechsten Modellwechsel falsch sein kann und im
        Ergebnis nicht mehr als Erfindung erkennbar waere. Der spaetere
        Rueckschreibeweg braucht ohnehin die einzelnen Bloecke.

        `layout_pdfs`, `span_pdfs`: ebenso als Listen in Reihenfolge.

        `envelopes`: die Umschlaege in Reihenfolge — Herkunft, nicht Inhalt.

    Args:
        parts: ALLE Teilstuecke des Dokuments, erfolgreich und vollstaendig, in
            aufsteigender Seitenreihenfolge. Bei einem ungeschnittenen Dokument
            ist das ein einziges Element mit `Chunk(0, 0)` — und der Ablauf ist
            derselbe. Es gibt keinen Sonderfall „war nicht geschnitten".

    Returns:
        Das Gesamtergebnis, genau die Form, die in `documents.raw` landet.

    Vorbedingung:
        Die Teilstuecke decken das Dokument lueckenlos ab. Geprueft wird das
        VORHER vom Executor ueber `chunking.verify_page_coverage`; diese Funktion
        prueft es nicht noch einmal und verlaesst sich darauf.

    Zusicherung:
        Kein `page_idx` im Ergebnis liegt ausserhalb von 0 bis Seitenzahl minus 1.
        Die Reihenfolge der Eintraege innerhalb eines Teilstuecks bleibt erhalten.
        Die uebergebenen Teilstuecke werden nicht veraendert.

        Und, weil es der ganze Zweck ist: Am Ergebnis ist nicht mehr ablesbar, ob
        und wo geschnitten wurde. Genau so soll es sein.

        Das Ergebnis ist JSON-faehig — es geht unveraendert in eine `jsonb`-Spalte.
        Deshalb werden Bilder und die beiden Visualisierungs-PDFs hier wieder nach
        Base64 gewandelt: Bytes haben in JSON keine Entsprechung. Wer sie zurueck
        will, dekodiert sie beim Lesen; die Kodierung ist verlustfrei und
        umkehrbar.

    Raises:
        TerminalRunPodError: Ein `content_list`-Eintrag traegt ein `page_idx`, das
            keine ganze Zahl ist. Dann ist der Versatz nicht berechenbar, und ihn
            unveraendert durchzureichen hiesse, eine falsche Seitenzahl zu
            speichern, die spaeter niemand mehr als falsch erkennt. Eintraege GANZ
            OHNE `page_idx` gehen dagegen unveraendert durch — sie behaupten keine
            Seite, also ist auch nichts zu korrigieren.

    Kosten: keine.
    """
    zusammen: list[dict[str, Any]] = []
    for teil in parts:
        versatz = teil.chunk.first_page
        for eintrag in teil.content_list:
            kopie = dict(eintrag)
            if "page_idx" in kopie:
                seite = kopie["page_idx"]
                if not isinstance(seite, int) or isinstance(seite, bool):
                    raise TerminalRunPodError(
                        f"content_list-Eintrag mit nicht ganzzahligem page_idx "
                        f"({seite!r}, Typ {type(seite).__name__}) im Teilstueck "
                        f"({teil.chunk.first_page}, {teil.chunk.last_page}). Der "
                        f"Seitenversatz ist damit nicht berechenbar."
                    )
                kopie["page_idx"] = seite + versatz
            zusammen.append(kopie)

    bilder: dict[str, bytes] = {}
    kollisionen: list[dict[str, Any]] = []
    for nummer, teil in enumerate(parts):
        # Die Vereinigung beruht auf der Annahme, die Namen seien Inhalts-Hashes
        # — gleiche Namen also gleiche Inhalte. Sie ist am 05.08.2026 empirisch
        # geprueft, aber NUR fuer den Inline-Transport: Dort liefert die Engine
        # die Namen selbst. Der Tarball-Pfad nimmt den nackten Dateinamen aus dem
        # Archiv, und dafuer gibt es bis zum ersten Live-Lauf kein Material —
        # ausgerechnet der Pfad also, den der Betrieb faehrt.
        #
        # Ein blosses `bilder.update(...)` haette bei verletzter Annahme still
        # ueberschrieben: bezahltes Bild weg, kein Fehler, keine Meldung. Deshalb
        # wird die Annahme hier GEPRUEFT statt geglaubt. Trifft sie zu, aendert
        # sich nichts; trifft sie nicht zu, bleibt das erste Bild stehen, das
        # zweite wird unter einem eigenen Namen aufgehoben, und die Kollision
        # steht als eigener Eintrag im Ergebnis — sichtbar fuer den ersten
        # Live-Lauf, der die Frage beantworten kann.
        for name, daten in teil.images.items():
            vorhanden = bilder.get(name)
            if vorhanden is None:
                bilder[name] = daten
            elif vorhanden != daten:
                ersatzname = f"{name}#kollision-teilstueck-{nummer}"
                bilder[ersatzname] = daten
                kollisionen.append(
                    {
                        "name": name,
                        "teilstueck": nummer,
                        "aufgehoben_als": ersatzname,
                        "hinweis": (
                            "Zwei Teilstuecke lieferten dasselbe Bild unter "
                            "demselben Namen mit VERSCHIEDENEM Inhalt. Die "
                            "Annahme, die Bildnamen seien Inhalts-Hashes, gilt "
                            "hier nicht. Beide Bilder sind erhalten."
                        ),
                    }
                )

    ergebnis: dict[str, Any] = {
        "markdown": "\n\n".join(teil.markdown for teil in parts),
        "content_list": zusammen,
        "images": {
            name: base64.b64encode(daten).decode("ascii")
            for name, daten in bilder.items()
        },
        "middle": [teil.middle for teil in parts],
        "layout_pdfs": [_b64_oder_none(teil.layout_pdf) for teil in parts],
        "span_pdfs": [_b64_oder_none(teil.span_pdf) for teil in parts],
        "envelopes": [teil.envelope for teil in parts],
        # Je Teilstueck eine eigene Abbildung und NICHT vereinigt: Die
        # Schluessel koennen sich zwischen Teilstuecken wiederholen (jedes
        # Teilstueck hat sein eigenes `basename`, seine eigene
        # `pages_requested`), und eine Vereinigung liesse alle bis auf die
        # letzte fallen. Genau der stille Verlust, gegen den `extras` steht.
        "extras": [teil.extras for teil in parts],
    }
    if kollisionen:
        # NUR wenn es sie gibt. Ein Schluessel, der immer da ist und fast immer
        # leer, wird ueberlesen; einer, der nur im Ausnahmefall auftaucht, faellt
        # beim Ansehen einer Zeile sofort auf.
        ergebnis["bild_namenskollisionen"] = kollisionen
    return ergebnis


def _b64_oder_none(daten: bytes | None) -> str | None:
    """Base64 fuer die Ablage in JSON; None bleibt None (Inline-Transport)."""
    if daten is None:
        return None
    return base64.b64encode(daten).decode("ascii")


TEXTFELDER: Final = (
    "text",
    "content",
    "table_body",
    "list_items",
    "table_caption",
    "table_footnote",
    "image_caption",
    "image_footnote",
    "chart_caption",
    "chart_footnote",
)
"""Die Felder eines `content_list`-Eintrags, die lesbaren Text tragen koennen.

AM ECHTEN MATERIAL AUSGEZAEHLT (08.08.2026, alle sechs konservierten Antworten,
insgesamt 100 Bloecke). Welcher Blocktyp welche Felder wirklich traegt:

    `text` (50), `header` (16), `footer` (5), `page_number` (5): `text`
    `equation` (11): `text`, dazu `text_format`
    `list` (4): `list_items` — eine Liste von Zeichenketten
    `image` (5): `content`, `image_caption`, `image_footnote`, `img_path`
    `chart` (1): `content`, `chart_caption`, `chart_footnote`, `img_path`
    `table` (3): `table_body`, `table_caption`, `table_footnote`, `img_path`

DIE REIHENFOLGE IST DIE LESEREIHENFOLGE, nicht eine Rangfolge: Es wird ALLES
genommen, was da ist, und nicht das erste Passende.

Warum diese Liste eine eigene Konstante mit voller Begruendung ist: Die fruehere
Fassung las genau drei Felder — `text`, `list_items`, `content` — und ihr
Docstring behauptete dabei, `content` trage „die Bildunterschrift bzw. den
Tabelleninhalt", und das sei am echten Material abgelesen. Beides stimmt nicht:

    Ein `table`-Block hat ueberhaupt kein `content`. Der GESAMTE Tabelleninhalt
    steht in `table_body`, als HTML. Gemessen etwa:
    `<table><tr><td>Current share price (S)</td><td>473p</td></tr>…</table>` —
    also genau die Zahlen, um derentwillen ein Steuerdokument gelesen wird. Die
    frueherer Fassung lieferte fuer diesen Block die LEERE Zeichenkette.

    Ein `image`-Block traegt zwar `content`, aber es war in allen fuenf
    Vorkommen leer; die Unterschrift steht in `image_caption`, einer Liste.

    Ein `chart`-Block traegt `content` (dort stand eine Markdown-Tabelle) UND
    `chart_caption` mit echtem Text („Figure 3.1 Cumulative standard normal
    distribution and density"), der ebenfalls nie gelesen wurde.

WIE WEIT DER SCHADEN REICHTE — und wie weit ausdruecklich NICHT. Es liegt nahe,
hier denselben Fall wie beim Aktenzeichen zu vermuten: bezahlter Text, der es nie
in die Ausgabe schafft. Nachgemessen (08.08.2026) stimmt das NICHT: Fuer alle
drei Faelle traegt MinerUs eigenes `markdown` den Inhalt bereits — die drei
`table_body`, die `chart_caption` und der `content` des Diagramms sind darin
zeichengleich enthalten. `rendered_markdown` verliert sie also nicht, und es
haengt sie auch nicht an, weil `table`, `chart` und `image` keine Randbloecke
sind.

Der Schaden lag ausschliesslich bei `is_empty`: Ein Dokument, dessen `markdown`
leer ist und dessen einziger Inhalt eine Tabelle ist, lieferte `_blocktext` fuer
diese Tabelle die leere Zeichenkette — es galt als `empty` und waere laut Vertrag
nie wieder angesehen worden. Dass der Fall bisher nicht eintrat, lag am
`img_path`-Zweig in `is_empty`, den alle drei echten Tabellen zufaellig
mitbringen; das ist eine Eigenschaft dieser Engine-Fassung, keine Zusicherung.

Die Liste bleibt unvollstaendig, und das ist gesagt statt verschwiegen: Sie deckt
die neun Blocktypen ab, die in den sechs konservierten Antworten vorkommen. Ein
Blocktyp, den dieses Material nicht enthaelt, kann Felder tragen, die hier
fehlen. Deshalb steht in `raw` immer die vollstaendige `content_list` — wer
etwas vermisst, findet es dort und traegt es hier nach."""


def _blocktext(eintrag: dict[str, Any]) -> str:
    """Sammelt ALLEN lesbaren Text eines `content_list`-Eintrags.

    Sammelt und waehlt nicht aus: Ein Tabellenblock traegt Inhalt UND
    Unterschrift, ein Diagrammblock Inhalt UND Bildunterschrift. Die frueherer
    Fassung gab das erste passende Feld zurueck und liess den Rest liegen — bei
    `table` gab sie mangels `content` sogar gar nichts zurueck. Welche Felder es
    gibt und was daran gemessen ist, steht an `TEXTFELDER`.

    Die Teile werden durch einen Zeilenumbruch verbunden, in der Reihenfolge von
    `TEXTFELDER`. Ein Eintrag ganz ohne diese Felder liefert die leere
    Zeichenkette.
    """
    teile: list[str] = []
    for feld in TEXTFELDER:
        wert = eintrag.get(feld)
        if isinstance(wert, str) and wert.strip():
            teile.append(wert)
        elif isinstance(wert, list):
            # `list_items` und die sechs `*_caption`/`*_footnote`-Felder sind
            # Listen von Zeichenketten. `str(einzeln)` und nicht ein Typfilter:
            # Ein Eintrag, der wider Erwarten keine Zeichenkette ist, soll
            # sichtbar bleiben statt still zu verschwinden.
            for einzeln in wert:
                text = str(einzeln)
                if text.strip():
                    teile.append(text)
    return "\n".join(teile)


def is_empty(result: dict[str, Any]) -> bool:
    """Sagt, ob ein vollstaendig verarbeitetes Dokument gar keinen Inhalt hatte.

    Warum das eine eigene Frage ist: Ein leeres Ergebnis ist GUELTIG. Eine
    wirklich leere Seite liefert wahrheitsgemaess nichts. Wer das als Erfolg
    verbucht, verliert die Seite unsichtbar; wer es als Fehler verbucht, legt sie
    endlos wieder vor und bezahlt jedes Mal. Beide Fehler sind in den
    Ad-hoc-Skripten je einmal wirklich passiert — deshalb ist `empty` ein eigener
    Endzustand.

    DAS KRITERIUM IST AM ECHTEN VERHALTEN FESTGESTELLT, wie es die Vorgabe
    verlangte — „das weiß ich ja nicht das muss ja rausgefudnen werden dann vorm
    bauen. es wird ja nicht ohne interaktion gebaut es ist ja consigment gegen
    die api" (Plan 2.4). Die Feststellung ist am 08.08.2026 nachgeholt worden,
    indem eine leere A4-Seite durch den echten Endpoint geschickt wurde
    (`artifacts/2026-08-08-erster-tarball-lauf/leere_seite.tarball.response.json`).

    WAS DIE SCHNITTSTELLE AUF EINE LEERE SEITE ANTWORTET, gemessen:

        `status: "COMPLETED"` und `output.ok: true` — eine leere Seite ist fuer
        die Engine ein ERFOLG, kein Fehler. Sie meldet nichts Besonderes.

        Das Archiv enthaelt `doc.md` mit 0 Bytes, `doc_content_list.json` mit
        `[]`, `doc_content_list_v2.json` mit `[[]]` und ein `doc_middle.json`,
        dessen `pdf_info[0]` leere `preproc_blocks`, `discarded_blocks` und
        `para_blocks` traegt. Keine Bilder.

        Und trotzdem ein `doc_layout.pdf` (462 Bytes). Die Anwesenheit eines
        Layout-PDF ist also KEIN Hinweis auf Inhalt.

    Die vorher als Arbeitsfassung gewaehlte Regel trifft damit zu und ist jetzt
    belegt: leer ist ein Dokument genau dann, wenn sein `markdown` nach dem
    Entfernen von Leerraum leer ist UND seine `content_list` keinen einzigen
    inhaltstragenden Eintrag hat. Im Zweifel gilt ein Dokument als NICHT leer —
    ein faelschlich als fertig verbuchtes leeres Dokument faellt beim Ansehen
    auf, ein faelschlich als leer verbuchtes volles nie.

    Was die Messung ZUSAETZLICH zutage foerderte, steht an `_aus_tarball`: Die
    leere Antwort brachte zwei Fehler ans Licht, die einander verdeckt hatten —
    eine Belegt-Pruefung am Wahrheitswert (eine gueltige leere Liste galt als
    „Platz frei") und eine Vollstaendigkeitspruefung, die eine echt leere Seite
    fuer ein kaputtes Archiv gehalten haette.

    Was dabei als „inhaltstragend" zaehlt, ist ausdruecklich WEIT gefasst:
    JEDER Eintrag mit lesbarem Text zaehlt, auch ein Randblock. Das folgt aus dem
    Aktenzeichen-Befund (siehe `FURNITURE_TYPES`) — ein Bescheid, von dem nur der
    Briefkopf mit dem Aktenzeichen erkannt wurde, ist alles andere als leer; er
    traegt die wertvollste Kennung des ganzen Dokuments. Die Randbloecke von der
    Zaehlung auszunehmen hiesse, genau solche Dokumente als `empty` zu verbuchen
    und nie wieder anzusehen.

    Args:
        result: Das zusammengesetzte Ergebnis aus `assemble`.

    Kosten: keine.
    """
    markdown = result.get("markdown", "")
    if isinstance(markdown, str) and markdown.strip():
        return False
    eintraege = result.get("content_list", [])
    if not isinstance(eintraege, list):
        # Kein Befund ueber Leere, sondern eine kaputte Form — im Zweifel NICHT
        # leer, damit das Dokument im Blickfeld bleibt.
        return False
    for eintrag in eintraege:
        if isinstance(eintrag, dict) and _blocktext(eintrag).strip():
            return False
        # Ein Bild ohne Bildunterschrift traegt keinen Text und trotzdem Inhalt.
        if isinstance(eintrag, dict) and eintrag.get("img_path"):
            return False

    # UND ZULETZT DIE BILDER SELBST. Sie standen hier nicht, und das war eine
    # Luecke mit Ansage: Am `images`-Feld von `ChunkArtifacts` steht ausgezaehlt,
    # dass die Antwort regelmaessig MEHR Bilder traegt, als die `content_list`
    # ueber `img_path` erwaehnt — gemessen 3 gegen 1 Erwaehnung, 7 gegen 4, 6
    # gegen 1, 6 gegen 3. Ein Dokument, dessen einziger Ertrag solche unerwaehnten
    # Bilder sind, lief damit durch beide Pruefungen darueber und galt als leer.
    #
    # `empty` heisst laut Vertrag: nie wieder ansehen. Bezahlte Bilder auf diesem
    # Weg zu verlieren waere genau der unsichtbare Verlust, gegen den der eigene
    # Grundsatz dieser Funktion steht — „im Zweifel NICHT leer".
    bilder = result.get("images")
    if isinstance(bilder, dict) and bilder:
        return False
    return True


def _pflichtfeld(result: dict[str, Any], name: str, typ: type[W], ersatz: W) -> W:
    """Holt ein Feld des GESPEICHERTEN Ergebnisses und besteht auf seinem Typ.

    Der Unterschied zu `_geprueft`, und warum es beide gibt: `_geprueft` steht auf
    der SCHREIBSEITE, beim Auspacken einer frisch bezahlten Antwort. Dort ist ein
    Feld mit falschem Typ kein Grund aufzuhoeren — es wandert nach `uebrig` und
    landet in `extras`, ist also aufgehoben, und der Lauf geht weiter. Hier ist
    die LESESEITE: Das Ergebnis liegt laengst in der Datenbank, es gibt kein
    `extras` mehr, in das etwas gerettet werden koennte, und der Aufrufer hat
    nach einer Darstellung gefragt. Ein stiller Ersatzwert waere hier kein
    Aufheben, sondern ein Verlust mit freundlichem Gesicht.

    `ersatz` wird ausschliesslich benutzt, wenn das Feld GAR NICHT da ist. Das
    ist eine Abwesenheit — eine aeltere Fassung hat es nicht geschrieben — und
    keine Beschaedigung. Dieselbe Trennung trifft `OCRWrapper._feld`; sie ist
    hier wiederholt und nicht importiert, weil `assembly` nichts von der Fassade
    weiss und auch nichts von ihr wissen soll.

    Raises:
        IncompleteResultError: Das Feld ist da, hat aber einen anderen Typ. Der
            haeufigste Fall ist JSON-`null`.
    """
    if name not in result:
        return ersatz
    wert = result[name]
    if isinstance(wert, typ):
        return wert
    raise IncompleteResultError(
        f"Das Feld {name!r} des gespeicherten Ergebnisses ist "
        f"{type(wert).__name__} statt {typ.__name__}. Es wird NICHT durch einen "
        f"leeren Wert ersetzt: Ein leeres Feld sieht aus wie ein leeres "
        f"Dokument, und die Ausgabe waere dann von einem gueltigen Ergebnis "
        f"nicht zu unterscheiden. Die Zeile ist beschaedigt oder stammt aus "
        f"einer anderen Fassung; ihr Inhalt steht unveraendert in `raw`."
    )


def rendered_markdown(result: dict[str, Any]) -> str:
    """Das GANZE Dokument in Markdown-Form. Leseseite, aendert nichts am Gespeicherten.

    Alles, was auf dem Dokument steht, in einer Darstellung. Das Markdown der
    Engine ist davon der grosse Bestandteil, aber nicht das Ganze: Die Engine
    filtert beim Erzeugen ihres Markdowns Blockklassen HERAUS (`FURNITURE_TYPES`),
    und die gehoeren zum Dokument. Sie kommen hier zurueck.

    KEINE SCHALTER, und das ist der Punkt. Frueher hiess diese Funktion
    `rendered_text` und hatte zwei Parameter — `furniture`, um die Randbloecke
    weglassen zu koennen, und `foreign_filter`, um vom Modell erzeugten Text
    auszusortieren. Beide sind weg, weil beide dieselbe Frage stellten: „Was
    gehoert zum Text?" Diese Frage hat hier keine Antwort zu suchen. Eine
    Voreinstellung dafuer waere eine redaktionelle Behauptung, und die gehoert
    dem, der das Ergebnis benutzt, nicht dem, der es aufbereitet.

    Wer weniger will als alles, hat den Weg dafuer: `wrapper["content_list"]`
    liefert jeden Block einzeln, samt `type` und `page_idx`, und
    `wrapper["markdown"]` das nackte Engine-Markdown. Aus beiden laesst sich
    jede Auswahl bauen — an der Stelle, an der jemand weiss, wofuer.

    WORAUS SICH „DAS GANZE" ZUSAMMENSETZT, gemessen und nicht angenommen:

        Das Markdown der Engine, unveraendert und vollstaendig. Es traegt den
        Fliesstext, die Ueberschriften, die Tabellen als HTML, die Bildverweise
        — und die vom Modell erzeugten Deutungen von Bildern, die MinerU in
        `<details>`-Bloecke einbettet (dokumentiertes Verhalten der
        multimodalen Ausgabe).

        Dazu die Randbloecke, die im Markdown FEHLEN. Am 08.08.2026 an einem
        echten zwoelfseitigen Dokument gemessen: Die `header`-Bloecke stehen im
        Markdown, die `footer`-Bloecke auf allen elf Seiten nicht. Die Engine
        filtert also nicht einheitlich, und welche Klasse sie wegfiltert, ist
        keine Konstante, auf die man sich verlassen kann.

    WARUM DIE RANDBLOECKE ZAEHLEN, und das ist der teuer bezahlte Teil: In ihnen
    liegt das Aktenzeichen. Die Kalibrierung hat den Weg an den Layout-Daten
    eines Bescheids nachgezeichnet — `content_list` fuehrt ihn als
    `{"type": "header", "text": "Aktenzeichen 123/456/7890/012/345/6"}`, und das
    Markdown hat ihn nicht mehr. „Der Filter trifft nicht die Kennung, sondern
    die Zone." Die volle Rechnung steht bei `FURNITURE_TYPES`.

    WAS DER AUFRUFER UEBER DAS ERGEBNIS WISSEN MUSS, weil „alles" auch das
    Unerwuenschte einschliesst — zwei Messungen, beide nicht vermutet:

        In den angehaengten Randbloecken steht MODELL-PROSA. Ueber die 15
        Kalibrierungs-Dokumente wurden 30 Fundstellen englischer Erklaerungen
        gezaehlt — Saetze des Modells darueber, warum ein Bildbereich nichts
        Lesbares enthalte —, „im selben Feld und mit demselben Typ wie echter
        Inhalt, ueberwiegend in `aside_text`, `footer` und `header`". Also genau
        in den Klassen, die hier zurueckkommen.

        Im Markdown der Engine stehen ERZEUGTE DEUTUNGEN VON BILDERN. Fuer Bild-
        und Diagrammbloecke legt MinerU eine eigene Lesart des Bildes in das Feld
        `content` und bettet sie als `<details>`-Block hinter den Bildverweis —
        dokumentiertes Verhalten der multimodalen Ausgabe. Am 08.08.2026
        gemessen: zwei Mermaid-Flowcharts zu zwei Organigrammen, und eines davon
        fuehrt einen Knoten zweimal mit demselben Anteil, erfuellt seine
        eigene Logik also nicht.

    Beides ist HIER nicht gefiltert, und das ist Absicht: Was davon unerwuenscht
    ist, weiss nur der Abnehmer. Wer auswaehlen will, hat die Bausteine — die
    Modell-Prosa steht als eigener Eintrag in `content_list` (mit `type` und
    `page_idx`), die erzeugten Deutungen als `content` von `image`- und
    `chart`-Eintraegen, und im Markdown erkennbar an ihrer `<details>`-Form.

    Args:
        result: Das zusammengesetzte Ergebnis.

    Zusicherung:
        Das Ergebnis BEGINNT mit dem Markdown der Engine, zeichengleich; alles
        Weitere ist angehaengt. Es wird nie etwas entfernt, nur ergaenzt — diese
        Funktion kann den Text der Engine nicht verschlechtern.

        Angehaengt wird ein Randblock nur, wenn sein Text im Markdown FEHLT.
        Dass elf Seiten elf gleichlautende Fusszeilen beitragen, ist dabei kein
        Duplikat, sondern elfmal dieselbe Fusszeile.

        Die Randbloecke kommen in Dokumentreihenfolge, ohne eingefuegte Marker.
        Kein „--- Randbloecke ---", keine Seitenangabe davor: Das waere eine
        erfundene Struktur, die jeder nachgelagerte Leser erst lernen muesste,
        und fuer den Zweck — die Ziffernfolge des Aktenzeichens auffindbar
        machen — bringt sie nichts. Wer die Herkunft je Block braucht, liest
        `content_list`; dort steht sie samt `page_idx` und `type`.

        UND: Es kommt entweder das ganze Dokument heraus oder eine Ausnahme.
        Niemals eine Zeichenkette, die weniger ist, als der Name verspricht.
        Hier stand frueher an drei Stellen ein stilles Ausweichen — ein
        `markdown`, das nicht `str` war, wurde zu `""`; eine `content_list`, die
        keine Liste war, fuehrte zu einem sofortigen `return markdown`; ein
        Eintrag, der kein Dict war, wurde uebersprungen. Alle drei erzeugten
        eine Ausgabe, die VOLLSTAENDIG AUSSAH.

        Gemessen am 10.08.2026 durch die echte Kommandozeile gegen eine echte
        `done`-Zeile mit `raw = {"markdown": null, "content_list": [header
        „Aktenzeichen 123/456/7890", text „Der ganze Fliesstext des
        Bescheids."]}`: `mineru-ocr markdown … --out datei.md` endete mit
        Exit-Code 0 und schrieb eine Datei mit dem einen Wort
        „Aktenzeichen 123/456/7890" — der ganze Fliesstext fehlte, und nichts
        sagte es. Das ist derselbe Verlust, den der Bild-Getter schon einmal
        hatte, als ein `isinstance`-Filter eines von drei Bildern verschwinden
        liess.

        Abwesenheit ist davon ausgenommen und bleibt geduldet: Fehlt eines der
        beiden Felder ganz, ist das eine aeltere Fassung und keine
        Beschaedigung. Fehlen BEIDE, ist es kein zusammengesetztes Ergebnis —
        `assemble` schreibt immer beide — und auch das wird gesagt, statt still
        eine leere Zeichenkette zu liefern.

    Raises:
        IncompleteResultError: Das gespeicherte Ergebnis ist beschaedigt — ein
            Feld mit falschem Typ, ein `content_list`-Eintrag, der kein Dict
            ist, oder ein Ergebnis ohne beide Felder. Nichts davon ist verloren;
            es steht unveraendert in `raw`.

    Kosten: keine.
    """
    if "markdown" not in result and "content_list" not in result:
        raise IncompleteResultError(
            "Dieses Ergebnis traegt weder `markdown` noch `content_list`. "
            "`assembly.assemble` schreibt beide Felder immer, also ist es kein "
            "zusammengesetztes Ergebnis — vermutlich die Notfassung aus dem "
            "Fehlerpfad (`_rohfassung_der_teile`, erkennbar an `chunks` und "
            "`assembly_failed`) oder eine Zeile aus einer anderen Fassung. Was "
            "da ist, steht unveraendert in `raw`."
        )
    markdown = _pflichtfeld(result, "markdown", str, "")
    eintraege = _pflichtfeld(result, "content_list", list, [])

    angehaengt: list[str] = []
    for nummer, eintrag in enumerate(eintraege):
        if not isinstance(eintrag, dict):
            raise IncompleteResultError(
                f"Eintrag {nummer} von `content_list` ist "
                f"{type(eintrag).__name__} statt dict. Uebersprungen wird er "
                f"NICHT: Ein uebersprungener Eintrag kann ein Randblock mit dem "
                f"Aktenzeichen sein, und die Ausgabe saehe dann vollstaendig "
                f"aus. Der Eintrag ist nicht verloren — er steht unveraendert "
                f"in `raw`."
            )
        if eintrag.get("type") not in FURNITURE_TYPES:
            continue
        text = _blocktext(eintrag).strip()
        if not text:
            continue
        # WAS SCHON IM MARKDOWN STEHT, WIRD NICHT ANGEHAENGT. Die Engine
        # filtert die Randklassen NICHT einheitlich — am 08.08.2026 an einem
        # echten 12-Seiter gemessen: Die `header`-Bloecke („Kanzlei") stehen im
        # Markdown, die `footer`-Bloecke („Kanzlei
        # Wirtschaftspruefungsgesellschaft") auf allen elf Seiten nicht.
        #
        # Ohne diese Pruefung wurde jeder Randblock angehaengt, auch der schon
        # vorhandene: Der Kopf jeder Seite erschien zweimal. Fuer den Zweck —
        # die Ziffernfolge eines Aktenzeichens auffindbar machen — ist die
        # Dopplung harmlos, aber sie ist eine Ungenauigkeit, die jeder spaetere
        # Leser fuer einen Fehler der Engine halten muss.
        #
        # Verglichen wird gegen das MARKDOWN und nicht gegen die bereits
        # angehaengten: Zwei Seiten duerfen denselben Fusszeilentext beitragen,
        # das ist kein Duplikat, sondern zweimal dieselbe Fusszeile.
        if text in markdown:
            continue
        angehaengt.append(text)

    if not angehaengt:
        return markdown
    if not markdown:
        return "\n\n".join(angehaengt)
    return markdown + "\n\n" + "\n\n".join(angehaengt)
