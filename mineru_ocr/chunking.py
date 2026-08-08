"""Wie ein zu grosses Dokument in versendbare Stuecke zerfaellt — Schreibseite.

Dieses Modul entscheidet, OB und WIE geschnitten wird. Es schneidet nicht selbst:
Das Schneiden wird als Funktion hereingereicht (siehe `iter_chunks`). Der Grund
ist Pruefbarkeit — die Regeln hier sind die, an denen Geld haengt, und sie sollen
gegen erfundene Groessen durchgespielt werden koennen, ohne dass eine einzige
PDF-Datei im Spiel ist.

Die Gegenseite ist `assembly.py`: dort wird wieder zusammengesetzt. Beide Seiten
sind absichtlich getrennte Module, weil sie verschiedene Aufrufer haben (hier der
Daemon, dort die Fassade), verschiedenes Testmaterial brauchen — und weil die
Leseseite ohne pypdfium2 auskommen muss: Wer nur ein fertiges Ergebnis anschauen
will, soll keine PDF-Bibliothek laden.

Und das Wichtigste am ganzen Modul, weil es die Vorgabe des Auftraggebers ist:
NICHTS hiervon erreicht die Datenbank. Der Zuschnitt ist Laufzeit-Inneres des
Executors, kein Datenmodell — „ES KOMEMN KEINE CHUNKS IM POSTGRES AN […] Der
EXEKUTOR MANAGED DOCH ALLES UND SPEICHERT NUR FERTIG WEN JUCKEN CHUNKS SPÄTER"
(Plan 2.4). Ein `Chunk` lebt vom Aufruf bis zum Terminal-Schreiben und dann nie
wieder.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass

from .config import Settings
from .errors import CorruptDocumentError, TooLargeError
from .mime import DocumentType


def _passt(size_bytes: int, settings: Settings) -> bool:
    """Ob eine Bytefolge unter der Upload-Grenze bleibt.

    Die EINE Stelle im Modul, an der gegen `max_payload_bytes` verglichen wird.
    `check_fits_unsplit` und `iter_chunks` fragen beide hier, damit nicht zwei
    Vergleiche entstehen, die spaeter auseinanderlaufen koennen — der eine mit
    `<`, der andere mit `<=`, und der Unterschied faellt genau bei der Datei auf,
    die exakt auf der Grenze liegt.
    """
    return size_bytes <= settings.max_payload_bytes


@dataclass(frozen=True)
class Chunk:
    """Ein Seitenbereich eines Dokuments, null-basiert und beidseitig einschliessend.

    Es gibt bewusst KEINE laufende Nummer an dieser Klasse. Die Reihenfolge ergibt
    sich aus `first_page`, und eine zweite Ordnungsangabe koennte mit der ersten
    in Widerspruch geraten — genau der Fall, den man beim Zusammensetzen nie
    bemerkt, weil beide Ordnungen meistens uebereinstimmen.

    Ein einseitiges Dokument (Bild, DOCX) hat genau einen Chunk `(0, 0)`, und
    dieser eine Chunk traegt dann die ganze Datei. Das ist Absicht: Der
    Verarbeitungsweg im Executor kennt so keinen Sonderfall „ungeschnitten" —
    es gibt immer eine Liste von Chunks, manchmal mit einem Element.
    """

    first_page: int
    """Erste Seite, null-basiert, einschliessend."""

    last_page: int
    """Letzte Seite, null-basiert, EINSCHLIESSEND."""

    def __post_init__(self) -> None:
        """Weist einen unmoeglichen Bereich schon beim Anlegen ab.

        Warum hier und nicht in den Pruefungen weiter unten: Ein Chunk mit
        `last_page < first_page` hat eine NEGATIVE `page_count`, und negative
        Seitenzahlen rechnen sich in jeder Summe still weg — genau die Art
        Fehler, gegen die `verify_page_coverage` ueberhaupt steht. Die
        Bedingung an dieser Stelle zu erzwingen macht sie fuer jede Stelle
        wahr, die einen Chunk in die Hand bekommt, statt nur fuer die, die
        nachrechnet.

        Es ist ein Programmierfehler und kein Eingabefehler: Kein Dokument der
        Welt bringt jemanden dazu, `Chunk(5, 3)` zu bauen — das tut nur eine
        falsche Rechnung im Zuschnitt.
        """
        if self.first_page < 0:
            raise ValueError(
                f"first_page ist null-basiert und kann nicht negativ sein: "
                f"{self.first_page}."
            )
        if self.last_page < self.first_page:
            raise ValueError(
                f"last_page ({self.last_page}) liegt vor first_page "
                f"({self.first_page}). Die Angabe ist null-basiert und "
                f"beidseitig einschliessend; ein leerer Bereich ist kein Chunk."
            )

    @property
    def page_count(self) -> int:
        """`last_page - first_page + 1`. Immer mindestens 1."""
        return self.last_page - self.first_page + 1


def needs_chunking(
    *,
    size_bytes: int,
    page_count: int | None,
    doc_type: DocumentType,
    settings: Settings,
) -> bool:
    """Sagt, ob dieses Dokument ueberhaupt zerlegt werden muss.

    Zwei Gruende, und sie sind unabhaengig voneinander (Plan 6): Die Datei ist
    roh groesser als `settings.max_payload_bytes` — dann bricht sie die
    Upload-Grenze. ODER sie hat mehr Seiten als `settings.max_pages_per_chunk` —
    dann droht sie die ANTWORT-Grenze zu brechen, was ein voellig anderer Cap ist
    und deshalb eine eigene Schwelle hat. Eine 2-MB-Datei mit 200 Seiten muss
    zerlegt werden, obwohl sie muehelos hochgeht.

    Args:
        size_bytes: Rohe Dateigroesse, unkodiert.
        page_count: Seitenzahl bei PDF, sonst None.
        doc_type: Der erkannte Typ.
        settings: Traegt die beiden Schwellen.

    Returns:
        True, wenn zerlegt werden muss.

    Zusicherung:
        Fuer Nicht-PDF ist die Antwort IMMER False — nicht weil solche Dateien
        klein waeren, sondern weil sie sich nicht zerlegen lassen. Ob sie
        trotzdem zu gross sind, beantwortet `check_fits_unsplit`, und dieses
        Nebeneinander ist Absicht: „muss zerlegt werden" und „passt nicht" sind
        zwei verschiedene Fragen, und sie zusammenzuwerfen war der Weg, auf dem
        uebergrosse DOCX frueher stillschweigend durchgereicht wurden.

    Raises:
        ValueError: `doc_type` ist PDF, aber `page_count` ist None. Die Seitenzahl
            eines PDF ist an dieser Stelle bekannt — der Aufrufer hat die Datei
            gerade geoeffnet. Fehlt sie, waere die Seiten-Schwelle nicht pruefbar,
            und die Antwort fiele allein nach der Bytegroesse aus: Ein 2-MB-PDF
            mit 200 Seiten ginge ungeteilt hinaus und brechte das Antwort-Cap.
            Lieber laut als still falsch — deshalb ein Programmierfehler und kein
            stillschweigendes Weiterrechnen.

    Kosten: keine.
    """
    if not doc_type.is_pdf:
        return False
    if page_count is None:
        raise ValueError(
            f"needs_chunking wurde fuer {doc_type.value} ohne page_count gerufen. "
            f"Bei PDF ist die Seitenzahl Pflicht, sonst bleibt die Seiten-Schwelle "
            f"({settings.max_pages_per_chunk}) ungeprueft."
        )
    return (
        size_bytes > settings.max_payload_bytes
        or page_count > settings.max_pages_per_chunk
    )


def check_fits_unsplit(
    *,
    size_bytes: int,
    doc_type: DocumentType,
    settings: Settings,
) -> None:
    """Prueft ein nicht zerlegbares Dokument gegen die Upload-Grenze.

    Gilt fuer alles ausser PDF — Bilder, DOCX, PPTX, XLSX. AUSDRUECKLICH NICHT
    fuer die einzelne zu grosse PDF-Seite: Die faellt erst beim Zerlegen auf, und
    `iter_chunks` ruft dafuer DIESE Funktion (statt selbst zu werfen), damit
    Schwelle und Meldung samt Aufruest-Hinweis an einer Stelle stehen und nicht
    an zweien auseinanderlaufen.

    Args:
        size_bytes: Rohe Groesse.
        doc_type: Der erkannte Typ.
        settings: Traegt die Schwelle.

    Returns:
        Nichts. Der gute Fall ist die stille Rueckkehr.

    Raises:
        TooLargeError: Passt nicht. Die Meldung nennt die tatsaechliche Groesse,
            die Grenze UND den Aufruest-Hinweis, denn genau so war die Festlegung:
            „error fuers erste aber so dass man einen hinweis bekommt es
            nachtraeglich aufzuruesten und es auch als TODO reintun" (Plan 2.4).
            Der Aufruest-Weg, den der Hinweis nennt, waere das Rastern der Seite
            in ein Bild — bewusst nicht gebaut, weil der Fall im gesamten
            geprueften Bestand nicht vorkam (groesste Einzelseite: 1,27 MB).

    Kosten: keine.
    """
    if _passt(size_bytes, settings):
        return
    raise TooLargeError(
        f"{size_bytes} Byte ueberschreiten die Upload-Grenze von "
        f"{settings.max_payload_bytes} Byte, und {doc_type.value} laesst sich "
        f"nicht seitenweise zerlegen. "
        f"AUFRUESTBAR: Der Weg dafuer waere, die Seite bzw. das Bild zu rastern "
        f"und verkleinert zu versenden. Bewusst nicht gebaut, weil der Fall im "
        f"gesamten geprueften Bestand nicht vorkam (groesste Einzelseite: "
        f"1,27 MB). Wer ihn braucht, legt dafuer eine Aufgabe an — dieser "
        f"Hinweis ist der Ausloeser."
    )


def plan_chunks(page_count: int, settings: Settings) -> list[Chunk]:
    """Legt den Zuschnitt nach Seitenzahl fest — vor jeder Ruecksicht auf Bytes.

    Gierige Bloecke in Dokumentreihenfolge: Seiten 0 bis 19, 20 bis 39, und so
    weiter; der letzte Block ist kuerzer. Kein Ueberlappen, keine Luecke, keine
    Umsortierung.

    Warum nach Seiten und nicht nach Bytes: Die Seitenzahl steht vorab fest, die
    Bytegroesse eines Ausschnitts erst nach dem Schnitt. Also wird zuerst nach dem
    Bekannten geplant und danach mit dem Gemessenen korrigiert (`iter_chunks`).

    Args:
        page_count: Seitenzahl des Dokuments, mindestens 1.
        settings: Traegt `max_pages_per_chunk`.

    Returns:
        Aufsteigend nach `first_page` sortierte, luecken- und ueberlappungsfreie
        Chunks, die zusammen genau 0 bis `page_count - 1` abdecken. Bei einem
        Dokument unter der Schwelle ist das eine Liste mit einem Element.

    Zusicherung:
        `verify_page_coverage(plan_chunks(n, s), n)` wirft nie. Rein, ohne I/O.

    Raises:
        ValueError: `page_count` kleiner als 1 — ein Programmierfehler, kein
            Eingabefehler.

    Kosten: keine.
    """
    if page_count < 1:
        raise ValueError(
            f"plan_chunks braucht mindestens eine Seite, bekam {page_count}. "
            f"Ein Dokument ohne Seiten ist kein Zuschnitt-Problem, sondern ein "
            f"Lesefehler weiter oben."
        )
    schritt = settings.max_pages_per_chunk
    return [
        Chunk(first_page=start, last_page=min(start + schritt, page_count) - 1)
        for start in range(0, page_count, schritt)
    ]


def split_in_half(chunk: Chunk) -> tuple[Chunk, Chunk]:
    """Teilt einen Chunk in zwei benachbarte Haelften.

    Bei ungerader Seitenzahl bekommt die erste Haelfte die zusaetzliche Seite.

    Args:
        chunk: Ein Chunk mit mindestens zwei Seiten.

    Returns:
        Zwei Chunks, die zusammen genau denselben Bereich abdecken wie der
        uebergebene — das ist die Invariante, die den ganzen Halbierungs-Vorgang
        sicher macht: Halbieren kann die Abdeckung nicht verletzen, egal wie oft
        es geschieht.

    Raises:
        ValueError: Der Chunk hat nur eine Seite. Eine einzelne Seite laesst sich
            nicht weiter teilen; wer hier landet, hat den Abbruchfall des
            Aufrufers nicht behandelt.

    Kosten: keine.
    """
    if chunk.page_count < 2:
        raise ValueError(
            f"Chunk ({chunk.first_page}, {chunk.last_page}) hat nur eine Seite "
            f"und laesst sich nicht halbieren. Der Aufrufer muss den Abbruchfall "
            f"vorher behandeln — bei einer zu grossen Einzelseite ist das "
            f"check_fits_unsplit."
        )
    # Bei ungerader Seitenzahl bekommt die erste Haelfte die zusaetzliche Seite:
    # aufrunden statt abrunden. Die Wahl ist beliebig, aber sie muss FEST sein,
    # sonst haengt der Zuschnitt von der Rundungsrichtung ab.
    erste_haelfte = (chunk.page_count + 1) // 2
    mitte = chunk.first_page + erste_haelfte - 1
    return (
        Chunk(first_page=chunk.first_page, last_page=mitte),
        Chunk(first_page=mitte + 1, last_page=chunk.last_page),
    )


def verify_page_coverage(chunks: Sequence[Chunk], page_count: int) -> None:
    """Rechnet nach, dass die Chunks das Dokument vollstaendig und ueberschneidungsfrei abdecken.

    Das ist die laute Gegenrechnung gegen die geerbte Falle aus `pdf.py`: Wo
    MinerUs eigener Schnitt kaputte Seiten still ueberspringt, rechnen wir nach.
    Geprueft wird die STRUKTUR der nach `first_page` sortierten Chunks, in drei
    Schritten: Die Abdeckung beginnt bei Seite 0; je zwei benachbarte Chunks
    schliessen luecken- und ueberlappungsfrei aneinander an; die Abdeckung endet
    bei Seite `page_count - 1`.

    Warum nicht die Summe der Seitenzahlen: Eine Luecke und eine Ueberlappung
    gleicher Groesse heben sich in der Summe auf. Das Ergebnis waere ein
    Dokument, dem eine Seite fehlt und das eine andere doppelt traegt — bei
    richtiger Gesamtzahl. Die Summe ist umgekehrt eine FOLGE der drei Pruefungen:
    Wer bei 0 beginnt, lueckenlos anschliesst und bei `page_count - 1` endet, hat
    zwangslaeufig genau `page_count` Seiten. Sie eigens nachzurechnen brachte
    deshalb keine zusaetzliche Sicherheit — waere aber, an erster Stelle geprueft,
    aktiv schaedlich: Eine schlichte fehlende Seite kaeme dann als „falsche
    Summe" heraus statt als „die Seiten 4 bis 6 fehlen", und damit ohne die
    Angabe, die man zum Suchen braucht.

    Die Verankerung an Anfang und Ende steht hier, weil Luecken- und
    Ueberlappungsfreiheit allein LUECKENHAFT waeren, und zwar so, dass es
    niemandem auffiele: Fuer ein zehnseitiges Dokument sind die Chunks (5, 9)
    und (10, 14) lueckenlos, ueberschneidungsfrei und summieren sich auf genau
    zehn Seiten — und decken trotzdem die falschen Seiten ab.

    Der Vertrag aus Phase A nannte an dieser Stelle Summe, Luecke und
    Ueberlappung. Beim Ausformulieren des Koerpers stellte sich heraus, dass
    diese drei zusammen unvollstaendig sind (siehe das Beispiel eben) und dass
    die Summe unter ihnen die einzige entbehrliche ist. Die Zusicherung ist
    dadurch staerker geworden, nicht schwaecher: Jeder Zuschnitt, den die alte
    Fassung beanstandet haette, wird auch hier beanstandet — nur mit einer
    genaueren Meldung.

    Args:
        chunks: Die zu pruefenden Chunks, in beliebiger Reihenfolge.
        page_count: Die Seitenzahl, die abgedeckt sein muss.

    Returns:
        Nichts.

    Raises:
        CorruptDocumentError: Irgendetwas stimmt nicht. Die Meldung nennt, WAS —
            Luecke, Ueberlappung oder falsche Lage — samt der betroffenen
            Seitenzahlen UND des vollstaendigen Zuschnitts, weil „Seitenzahlen
            stimmen nicht" allein niemandem beim Suchen hilft.
        ValueError: `page_count` kleiner als 1 — wie bei `plan_chunks` ein
            Programmierfehler und kein Befund ueber das Dokument.

    Kosten: keine.
    """
    if page_count < 1:
        raise ValueError(
            f"verify_page_coverage braucht mindestens eine Seite, bekam "
            f"{page_count}."
        )
    if not chunks:
        raise CorruptDocumentError(
            f"Der Zuschnitt ist leer, das Dokument hat aber {page_count} Seiten."
        )

    sortiert = sorted(chunks, key=lambda stueck: stueck.first_page)
    alle = [(stueck.first_page, stueck.last_page) for stueck in sortiert]

    if sortiert[0].first_page != 0:
        raise CorruptDocumentError(
            f"Falsche Lage: Der Zuschnitt beginnt bei Seite "
            f"{sortiert[0].first_page} statt bei Seite 0. Chunks: {alle}"
        )

    for links, rechts in itertools.pairwise(sortiert):
        if rechts.first_page > links.last_page + 1:
            raise CorruptDocumentError(
                f"Luecke: Nach Chunk ({links.first_page}, {links.last_page}) "
                f"folgt ({rechts.first_page}, {rechts.last_page}) — die Seiten "
                f"{links.last_page + 1} bis {rechts.first_page - 1} fehlen. "
                f"Chunks: {alle}"
            )
        if rechts.first_page <= links.last_page:
            raise CorruptDocumentError(
                f"Ueberlappung: Chunk ({links.first_page}, {links.last_page}) und "
                f"({rechts.first_page}, {rechts.last_page}) teilen sich die Seiten "
                f"{rechts.first_page} bis {min(links.last_page, rechts.last_page)}. "
                f"Chunks: {alle}"
            )

    if sortiert[-1].last_page != page_count - 1:
        raise CorruptDocumentError(
            f"Falsche Lage: Der Zuschnitt endet bei Seite "
            f"{sortiert[-1].last_page}, das Dokument hat {page_count} Seiten "
            f"(letzte Seite {page_count - 1}). Chunks: {alle}"
        )


def iter_chunks(
    plan: Sequence[Chunk],
    cut: Callable[[int, int], bytes],
    settings: Settings,
) -> Iterator[tuple[Chunk, bytes]]:
    """Geht den Zuschnitt durch, korrigiert ihn an der Bytegrenze und liefert Stueck fuer Stueck.

    Fuer jeden geplanten Chunk wird geschnitten und die Groesse gemessen. Passt
    das Ergebnis unter `settings.max_payload_bytes`, wird es geliefert. Passt es
    nicht, wird der Chunk halbiert und mit beiden Haelften genauso verfahren —
    so lange, bis es passt oder bis eine einzelne Seite uebrig ist, die immer noch
    zu gross ist.

    Warum ein Generator und keine Liste: Ein 200-Seiten-Dokument zerfaellt in zehn
    Chunks zu je bis zu 7 MB. Alle gleichzeitig im Speicher zu halten waeren
    70 MB — und der Daemon betreut vierundzwanzig Dokumente gleichzeitig. Als
    Generator lebt immer nur EIN Chunk, weil der Aufrufer ihn abschickt und
    freigibt, bevor der naechste geschnitten wird.

    Args:
        plan: Das Ergebnis von `plan_chunks`, unveraendert.
        cut: Die Schneidefunktion, aufgerufen als `cut(first_page, last_page)` mit
            null-basierten, einschliessenden Seitenzahlen — im Betrieb ist das
            `PdfReader.extract`, im Test eine erfundene Funktion, die Bytes
            passender Groesse zurueckgibt. Diese Umkehrung ist der Grund, warum
            die Halbierungsregel ohne eine einzige echte PDF-Datei durchgespielt
            werden kann.
        settings: Traegt die Bytegrenze.

    Yields:
        Paare aus Chunk und den Bytes des zugehoerigen Ausschnitts, in
        aufsteigender Seitenreihenfolge. Die gelieferten Chunks koennen FEINER
        sein als der uebergebene Plan (dort, wo halbiert wurde) — nie groeber, nie
        umsortiert.

    Zusicherung:
        Die Folge der gelieferten Chunks deckt denselben Bereich ab wie `plan`
        (Folge aus der Invariante von `split_in_half`). Jede gelieferte Bytefolge
        ist hoechstens `settings.max_payload_bytes` gross — mit der einen
        Ausnahme, die es nicht gibt, weil sie wirft: siehe unten.

        NICHT zugesichert ist, dass alle Chunks vor dem ersten `yield` geschnitten
        wurden. Wer die Gesamtzahl der Chunks vorab braucht, bekommt sie nicht;
        das ist Absicht, denn sie steht erst am Ende fest.

    Raises:
        TooLargeError: Eine einzelne Seite ist auch allein noch zu gross —
            geworfen ueber `check_fits_unsplit`, damit Schwelle und Meldung nur
            an einer Stelle stehen. Faellt beim Durchlaufen an, also
            moeglicherweise NACHDEM schon Teilstuecke geliefert und vom Aufrufer
            bezahlt wurden. Das ist hinnehmbar und gewollt: Der Aufrufer schreibt
            dann `input_error` mitsamt dem, was bis dahin ankam — nichts
            Bezahltes geht verloren, aber die Zeile traegt dann bezahlte
            Ergebnisse trotz `input_error` (siehe `TooLargeError`).
        CorruptDocumentError: `cut` ist gescheitert (durchgereicht).

    Kosten: keine — die HTTP-Aufrufe macht der Aufrufer, nicht dieses Modul.
    """
    for geplant in plan:
        # Die offene Arbeit EINES geplanten Chunks. Neu entstandene Haelften
        # kommen VORNE hinein, damit die Reihenfolge aufsteigend bleibt: Wer die
        # erste Haelfte hinten anhaengt, liefert sie nach allen spaeteren Seiten
        # aus, und beim Zusammensetzen faellt das nicht auf, weil dort nach
        # `first_page` sortiert wird. Der Fehler waere also unsichtbar — deshalb
        # steht die Reihenfolge hier fest und wird eigens geprueft.
        offen = [geplant]
        while offen:
            aktuell = offen.pop(0)
            daten = cut(aktuell.first_page, aktuell.last_page)

            if _passt(len(daten), settings):
                yield aktuell, daten
                continue

            if aktuell.page_count == 1:
                # Eine einzelne Seite laesst sich nicht weiter teilen. Der Wurf
                # geht ueber `check_fits_unsplit`, damit Schwelle, Meldung und
                # Aufruest-Hinweis nur an EINER Stelle formuliert sind; die
                # Seitenzahl kommt hier dazu, weil nur diese Stelle sie kennt.
                try:
                    check_fits_unsplit(
                        size_bytes=len(daten),
                        doc_type=DocumentType.PDF,
                        settings=settings,
                    )
                except TooLargeError as fehler:
                    raise TooLargeError(
                        f"Seite {aktuell.first_page} (null-basiert) allein: {fehler}"
                    ) from fehler
                raise AssertionError(
                    "unerreichbar: check_fits_unsplit haette werfen muessen, denn "
                    "_passt ist gerade falsch. Wer das hier sieht, hat die "
                    "Kopplung der beiden zerschnitten."
                )

            erste, zweite = split_in_half(aktuell)
            offen.insert(0, zweite)
            offen.insert(0, erste)
