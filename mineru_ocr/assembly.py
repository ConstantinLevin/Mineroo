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

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from .chunking import Chunk


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
    darauf beruht, dass `assemble` einfach vereinigen darf."""

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
    raise NotImplementedError


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

    Kosten: keine.
    """
    raise NotImplementedError


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

    Args:
        result: Das zusammengesetzte Ergebnis aus `assemble`.

    Kosten: keine.
    """
    raise NotImplementedError


def rendered_text(
    result: dict[str, Any],
    *,
    furniture: bool = True,
    foreign_filter: Callable[[str], bool] | None = None,
) -> str:
    """Bereitet den fertigen Text auf — Leseseite, aendert nichts am Gespeicherten.

    Der Unterschied zum `markdown` im Ergebnis: Dort steht der Text, wie die
    Engine ihn geliefert hat. Hier kann der Aufrufer Randbloecke weglassen —
    Kopf- und Fusszeilen, Seitenzahlen, verworfene Bloecke — und eine eigene
    Entscheidung darueber treffen, was er fuer fremdsprachige Modell-Prosa haelt.

    Warum der zweite Filter vorgesehen ist: MinerU legt gelegentlich englische
    Modell-Prosa in Randbloecke (Kalibrierung 5.1). Ob das stehen bleiben soll,
    ist eine Entscheidung des Rueckschreibewegs nach DEVONthink und ausdruecklich
    nicht die dieses Moduls. Hier ist nur die Stelle offengehalten; eine
    Voreinstellung, die stillschweigend filtert, waere genau die Anmassung, die
    dieser Aufbau vermeidet.

    Args:
        result: Das zusammengesetzte Ergebnis.
        furniture: True laesst Randbloecke im Text (Voreinstellung — sie gehoeren
            zum Dokument). False laesst sie weg.
        foreign_filter: Bekommt einen Blocktext, gibt True zurueck, wenn er
            WEGGELASSEN werden soll. None heisst: nichts weglassen.

    Zusicherung:
        Mit den Voreinstellungen (`furniture=True`, `foreign_filter=None`) ist das
        Ergebnis zeichengleich mit `result["markdown"]`. Ohne diese Zusage gaebe es
        zwei Antworten auf dieselbe Frage, und niemand wuesste, welche gilt.

    Kosten: keine.
    """
    raise NotImplementedError
