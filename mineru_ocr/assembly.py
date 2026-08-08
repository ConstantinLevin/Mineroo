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
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final

from .chunking import Chunk
from .errors import TerminalRunPodError

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
Daraus folgt der Satz, an dem die Voreinstellung von `rendered_text` haengt:
„Fuer den Bescheid-Korpus ist ein Write-back aus dem nackten Markdown damit
nachweislich schlechter als das, was heute schon auf den Records liegt."

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
    """Die Span-Visualisierung, ebenso."""

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
    die beiden Visualisierungs-PDFs entstehen zwar im Ausgabeverzeichnis, aber
    nur Tarball und S3 packen dieses Verzeichnis ein (Betriebshandbuch 14a).
    „Tarball traegt also strikt mehr als Inline."
    """
    benannt = {"markdown", "content_list", "images", "middle"}
    return ChunkArtifacts(
        chunk=chunk,
        markdown=ergebnis.get("markdown", ""),
        content_list=ergebnis.get("content_list", []),
        images=_bilder_dekodieren(ergebnis.get("images", {})),
        middle=ergebnis.get("middle", {}),
        layout_pdf=None,
        span_pdf=None,
        envelope=umschlag,
        # Alles Uebrige aufheben — die Abruf-Regel. An den sechs echten
        # Antworten sind das `basename`, `source` und `pages_requested`; bei
        # einer kuenftigen Fassung der Engine womoeglich mehr, und genau darum
        # wird hier nicht aufgezaehlt, sondern der Rest genommen.
        extras={
            schluessel: wert
            for schluessel, wert in ergebnis.items()
            if schluessel not in benannt
        },
    )


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

                if kurz.endswith(".md"):
                    markdown = inhalt.decode("utf-8")
                elif "_content_list" in kurz and kurz.endswith(".json"):
                    content_list = _json_laden(inhalt, name, erwartet=list)
                elif "_middle" in kurz and kurz.endswith(".json"):
                    middle = _json_laden(inhalt, name, erwartet=dict)
                elif "layout" in kurz and kurz.endswith(".pdf"):
                    layout_pdf = inhalt
                elif "span" in kurz and kurz.endswith(".pdf"):
                    span_pdf = inhalt
                elif "/images/" in name or name.startswith("images/"):
                    images[kurz] = inhalt
                else:
                    # KEIN Muster hat gepasst — und genau deshalb wird die Datei
                    # aufgehoben statt fallengelassen. Von den Dateinamen im
                    # Archiv ist nur EINER belegt; dass etwas Unbekanntes dabei
                    # ist, ist damit nicht der Sonderfall, sondern die
                    # wahrscheinlichste Lage. Base64, weil `raw` eine
                    # jsonb-Spalte ist und Bytes dort keine Entsprechung haben.
                    uebrig[name] = base64.b64encode(inhalt).decode("ascii")
    except tarfile.TarError as fehler:
        raise TerminalRunPodError(
            f"`tarball_b64` liess sich nicht als Archiv lesen: {fehler}"
        ) from fehler

    if not markdown and not content_list:
        raise TerminalRunPodError(
            f"Im Archiv war weder ein Markdown noch eine content_list zu finden. "
            f"Enthaltene Dateien: {gesehen}. Die Zuordnung geschieht ueber "
            f"Namensmuster; passt keines, hat die Gegenseite ihre Dateinamen "
            f"geaendert und die Muster oben gehoeren nachgezogen."
        )

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
    for teil in parts:
        # Vereinigung ohne Kollisionspruefung: Die Namen sind Inhalts-Hashes,
        # gleiche Namen sind also gleiche Inhalte (am 05.08.2026 empirisch
        # geprueft). Waeren sie es nicht, wuerde hier still ueberschrieben — die
        # Annahme steht deshalb hier, wo sie benutzt wird, und nicht nur im
        # Vertrag.
        bilder.update(teil.images)

    return {
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


def _b64_oder_none(daten: bytes | None) -> str | None:
    """Base64 fuer die Ablage in JSON; None bleibt None (Inline-Transport)."""
    if daten is None:
        return None
    return base64.b64encode(daten).decode("ascii")


def _blocktext(eintrag: dict[str, Any]) -> str:
    """Holt den lesbaren Text aus einem `content_list`-Eintrag.

    Die Felder sind je nach Blocktyp verschieden, und zwar am echten Material
    abgelesen: `text` bei Text-, Ueberschriften- und Randbloecken; `list_items`
    (eine Liste von Zeichenketten) bei Listen; `content` bei Bild- und
    Tabellenbloecken, wo es die Bildunterschrift bzw. den Tabelleninhalt traegt.
    Ein Eintrag ohne eines dieser Felder liefert die leere Zeichenkette.
    """
    text = eintrag.get("text")
    if isinstance(text, str) and text:
        return text
    posten = eintrag.get("list_items")
    if isinstance(posten, list) and posten:
        return "\n".join(str(einzeln) for einzeln in posten)
    inhalt = eintrag.get("content")
    if isinstance(inhalt, str) and inhalt:
        return inhalt
    return ""


def is_empty(result: dict[str, Any]) -> bool:
    """Sagt, ob ein vollstaendig verarbeitetes Dokument gar keinen Inhalt hatte.

    Warum das eine eigene Frage ist: Ein leeres Ergebnis ist GUELTIG. Eine
    wirklich leere Seite liefert wahrheitsgemaess nichts. Wer das als Erfolg
    verbucht, verliert die Seite unsichtbar; wer es als Fehler verbucht, legt sie
    endlos wieder vor und bezahlt jedes Mal. Beide Fehler sind in den
    Ad-hoc-Skripten je einmal wirklich passiert — deshalb ist `empty` ein eigener
    Endzustand.

    OFFENER PUNKT, ausdruecklich: Das genaue Kriterium wird am echten Verhalten
    der Schnittstelle festgestellt, nicht vorab erfunden — „das weiß ich ja nicht
    das muss ja rausgefudnen werden dann vorm bauen. es wird ja nicht ohne
    interaktion gebaut es ist ja consigment gegen die api" (Plan 2.4). Die sechs
    konservierten Antworten helfen nur halb: Sie zeigen, wie eine NICHT leere
    Antwort aussieht (Markdown zwischen 201 und 2069 Zeichen), aber keine von
    ihnen ist leer.

    Bis zur Feststellung gilt als Arbeitsfassung, bewusst streng: leer ist ein
    Dokument genau dann, wenn sein `markdown` nach dem Entfernen von Leerraum leer
    ist UND seine `content_list` keinen einzigen inhaltstragenden Eintrag hat. Im
    Zweifel gilt ein Dokument als NICHT leer — ein faelschlich als fertig
    verbuchtes leeres Dokument faellt beim Ansehen auf, ein faelschlich als leer
    verbuchtes volles nie.

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
    return True


def rendered_text(
    result: dict[str, Any],
    *,
    furniture: bool = True,
    foreign_filter: Callable[[str], bool] | None = None,
) -> str:
    """Bereitet den fertigen Text auf — Leseseite, aendert nichts am Gespeicherten.

    Der Unterschied zum `markdown` im Ergebnis: Dort steht der Text, wie die
    Engine ihn geliefert hat — und die Engine hat dabei fuenf Blockklassen
    HERAUSGEFILTERT (`FURNITURE_TYPES`). Hier bekommt der Aufrufer sie zurueck,
    und er kann eine eigene Entscheidung darueber treffen, was er fuer
    fremdsprachige Modell-Prosa haelt.

    KORREKTUR EINER FALSCHEN ZUSICHERUNG AUS PHASE A. Der Vertrag stand hier
    urspruenglich anders: `furniture=True` sollte Randbloecke „im Text lassen",
    und die Zusicherung lautete, mit den Voreinstellungen sei das Ergebnis
    zeichengleich mit `result["markdown"]`. Beides zusammen war nur widerspruchs-
    frei unter der Annahme, MinerUs Markdown TRAGE die Randbloecke. Diese Annahme
    ist falsch, und sie ist an zwei unabhaengigen Stellen widerlegt: an allen
    sechs konservierten Antworten vom 05.08.2026 nachgezaehlt und in der
    Kalibrierung ueber 15 Dokumente gemessen (Zahlen bei `FURNITURE_TYPES`).
    Haette die alte Zusicherung Bestand, waere die VOREINSTELLUNG dieses Moduls
    genau die Variante, von der die Kalibrierung sagt: „Fuer den Bescheid-Korpus
    ist ein Write-back aus dem nackten Markdown damit nachweislich schlechter als
    das, was heute schon auf den Records liegt." Die Zusicherung ist deshalb
    ersetzt; der Parametername bleibt, weil er unter der richtigen Annahme
    stimmt.

    Warum der zweite Filter keine offene Stelle mehr ist, sondern die Sicherung
    dieser Abhilfe: Die Kalibrierung hat ueber die 15 Dokumente 30 Fundstellen
    englischer Modell-Prosa gezaehlt — Erklaerungen des Modells darueber, warum
    ein Bildbereich nichts Lesbares enthalte —, „im selben Feld und mit demselben
    Typ wie echter Inhalt, ueberwiegend in `aside_text`, `footer` und `header`".
    Also genau in den Bloecken, die diese Funktion anhaengt. Woertlich: „Wer die
    Bloecke ungefiltert anhaengt, schreibt Werkzeug-Kommentare in deutsche
    Steuerakten und verunreinigt genau den Index, den er reparieren will."

    Die ENTSCHEIDUNG, was Fremdtext ist, gehoert trotzdem nicht hierher, sondern
    zum Rueckschreibeweg nach DEVONthink — deshalb ein Filter des Aufrufers und
    keine eingebaute Heuristik. Eine Voreinstellung, die stillschweigend filtert,
    waere genau die Anmassung, die dieser Aufbau vermeidet.

    Args:
        result: Das zusammengesetzte Ergebnis.
        furniture: True (Voreinstellung) haengt die Randbloecke aus
            `content_list` an das Markdown an — sie gehoeren zum Dokument, und in
            ihnen liegt das Aktenzeichen. False liefert `result["markdown"]`
            wortgleich, also den von der Engine selbst gefilterten Text.
        foreign_filter: Bekommt einen Blocktext, gibt True zurueck, wenn er
            WEGGELASSEN werden soll. None heisst: nichts weglassen. Wirkt nur auf
            die angehaengten Randbloecke — im Markdown der Engine wird nichts
            geloescht, denn dort stuende sonst am Ende ein Text, den weder die
            Engine noch der Aufrufer je gesehen hat.

    Zusicherung:
        Mit `furniture=False` ist das Ergebnis zeichengleich mit
        `result["markdown"]`, unabhaengig von `foreign_filter`.

        Mit `furniture=True` beginnt das Ergebnis mit genau diesem Markdown; alles
        Weitere ist angehaengt. Es wird also NIE etwas entfernt, nur ergaenzt —
        die Funktion kann den Text der Engine nicht verschlechtern.

        Die Randbloecke kommen in Dokumentreihenfolge, ohne eingefuegte Marker.
        Kein „--- Randbloecke ---", keine Seitenangabe davor: Das waere eine
        erfundene Struktur, die jeder nachgelagerte Leser erst lernen muesste,
        und fuer den Zweck — die Ziffernfolge des Aktenzeichens auffindbar machen
        — bringt sie nichts. Wer die Herkunft je Block braucht, liest
        `content_list`; dort steht sie samt `page_idx` und `type`.

    Kosten: keine.
    """
    markdown = result.get("markdown", "")
    if not isinstance(markdown, str):
        markdown = ""
    if not furniture:
        return markdown

    eintraege = result.get("content_list", [])
    if not isinstance(eintraege, list):
        return markdown

    angehaengt: list[str] = []
    for eintrag in eintraege:
        if not isinstance(eintrag, dict):
            continue
        if eintrag.get("type") not in FURNITURE_TYPES:
            continue
        text = _blocktext(eintrag).strip()
        if not text:
            continue
        if foreign_filter is not None and foreign_filter(text):
            continue
        angehaengt.append(text)

    if not angehaengt:
        return markdown
    if not markdown:
        return "\n\n".join(angehaengt)
    return markdown + "\n\n" + "\n\n".join(angehaengt)
