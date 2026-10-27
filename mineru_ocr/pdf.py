"""Der einzige Ort im Paket, der pypdfium2 benutzt.

Warum gekapselt: Zwei Dinge an dieser Bibliothek sind Fallen, und beide sollen
genau einmal richtig gemacht werden statt an jeder Aufrufstelle neu.

Die erste Falle ist die Zaehlweise. `import_pages` nimmt seine Seitenangabe in
zwei Formen entgegen, und die beiden zaehlen VERSCHIEDEN: als Liste von Ganzzahlen
sind es NULL-basierte Indizes, als Zeichenkette sind es EINS-basierte
Seitennummern und Bereiche. Wer die Formen verwechselt, verschiebt jedes Dokument
um genau eine Seite — ein Fehler, der beim Betrachten eines einzelnen Ergebnisses
voellig unauffaellig ist. Dieses Modul benutzt ausschliesslich die Listenform und
rechnet an seiner Aussengrenze in null-basierten, beidseitig einschliessenden
Seitenzahlen. Das passt zugleich zur Zaehlweise des Job-Contracts der Engine
(`start_page`/`end_page` sind dort ebenfalls null-basiert und einschliessend).

Die zweite Falle ist geerbt: MinerU bringt eine eigene Schneidefunktion mit
(`mineru.cli.common.convert_pdf_bytes_to_bytes`), die intern dieselbe Bibliothek
benutzt — deren Fallback ueberspringt kaputte Seiten aber STILL („Skipped broken
PDF pages"). Wir benutzen sie deshalb ausdruecklich NICHT und schneiden selbst;
die Gegenrechnung der Seitenzahlen (`chunking.verify_page_coverage`) faengt, was
dabei danebengeht (Plan 6).

Warum ueberhaupt lokal geschnitten wird, obwohl die Engine mit `start_page`/
`end_page` auch serverseitig schneiden koennte: Ein Seitenbereich verkleinert die
HOCHGELADENE Datei nicht. Gegen die 10-MB-Grenze auf `/run` hilft er also gar
nicht — dort ist lokales Schneiden alternativlos. Gegen das Antwort-Cap hilft er,
aber um den Preis, dieselbe Datei fuer jeden Bereich erneut hochzuladen. Der
lokale Schnitt laedt insgesamt etwa eine Dateigroesse hoch, der serverseitige das
N-fache. Beide Wege stehen offen (`runpod.submit` fuehrt die Parameter), der
Normalweg ist der lokale.
"""

from __future__ import annotations

import io
from types import TracebackType

import pypdfium2

from .errors import CorruptDocumentError


class PdfReader:
    """Ein einmal geoeffnetes PDF, aus dem mehrfach Seitenbereiche geschnitten werden.

    Warum eine Klasse und nicht eine Funktion pro Schnitt: Ein Dokument mit
    120 Seiten wird in sechs Chunks geschnitten. Sechsmal dieselben Bytes zu
    parsen ist verschwendete Arbeit. Einmal oeffnen heisst ausserdem, dass alle
    Chunks aus DEMSELBEN geparsten Dokument stammen.

    Besitz und Lebensdauer: Der Aufrufer besitzt den Reader und schliesst ihn.
    Als Kontext-Manager benutzen; die zugrundeliegenden Ressourcen werden beim
    Verlassen freigegeben. Nach dem Schliessen wirft jede Methode.

    Nebenlaeufigkeit — und hier liegt die eigentliche Gefahr NICHT dort, wo man
    sie vermutet:

        PDFium ist nicht thread-sicher, und zwar AUCH ueber verschiedene
        Dokumente hinweg: „PDFium is not thread-safe, meaning simultaneous calls
        across different threads, even with different documents, are not allowed
        and can lead to crashes or corruption" (pypdfium2-Doku, „Incompatibility
        with Threading"). Koroutinen in EINEM Event-Loop sind aber keine Threads
        — vierundzwanzig gleichzeitig betreute Dokumente mit je eigenem Reader
        sind aus PDFiums Sicht also unbedenklich, weil zu jedem Zeitpunkt nur
        einer von ihnen laeuft.

        Die tatsaechliche Folge ist die umgekehrte: `extract` ist BLOCKIERENDE
        Rechenarbeit innerhalb einer asynchronen Aufgabe. Sie haelt den
        Event-Loop an, und zwar fuer alle gleichzeitig betreuten Dokumente. Das
        ist hinnehmbar, solange die Schnitte klein sind (Millisekunden bis
        Sekundenbruchteile bei den gemessenen Groessen) — aber es ist eine
        Eigenschaft, die man kennen muss.

        Und der naheliegende Ausweg ist VERBOTEN: `asyncio.to_thread` waere genau
        der Thread, den PDFium nicht vertraegt, und der Schaden waere kein
        Fehler, sondern ein Absturz oder eine stille Verfaelschung. Sollte sich
        das Blockieren im Bestandslauf als Bremse erweisen, ist der dokumentierte
        Weg ein eigener PROZESS („its operations can be parallelized using
        processes instead of threads"), nicht ein Thread.
    """

    def __init__(self, data: bytes) -> None:
        """Oeffnet das PDF aus seinen Bytes.

        Args:
            data: Die Bytes eines PDF, das bereits als PDF erkannt wurde
                (`mime.sniff_bytes`). Dieses Modul prueft den Typ NICHT erneut —
                die Typerkennung hat genau einen Eigentuemer, und der heisst
                `mime`. Es muss `bytes` sein: pypdfium2 nimmt ein `memoryview`
                nicht an.

        Raises:
            CorruptDocumentError: Die Bytes lassen sich nicht als PDF oeffnen —
                sei es beschaedigt, verschluesselt oder passwortgeschuetzt.
                Alle drei Faelle sind fuer uns dasselbe: nicht verarbeitbar,
                endgueltig, ohne RunPod-Kontakt.
        """
        try:
            self._doc = pypdfium2.PdfDocument(data)
        except pypdfium2.PdfiumError as fehler:
            # Beschaedigt, verschluesselt und passwortgeschuetzt sind fuer uns
            # dasselbe: nicht verarbeitbar. Sie liessen sich in dieser Fassung
            # auch gar nicht auseinanderhalten — `PdfiumError` traegt keinen
            # Fehlercode, der Grund steckt nur in der Meldung. Deshalb wandert
            # die Meldung mit, statt dass wir sie deuten.
            raise CorruptDocumentError(
                f"Die Eingabe laesst sich nicht als PDF oeffnen: {fehler}"
            ) from fehler

    def __enter__(self) -> PdfReader:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._doc.close()

    @property
    def page_count(self) -> int:
        """Anzahl der Seiten. Mindestens 1 — ein PDF mit null Seiten laesst sich
        nicht oeffnen und ist bereits im Konstruktor gescheitert."""
        return len(self._doc)

    def check_pages(self) -> None:
        """Laedt jede Seite einmal, ohne sie zu rendern, und schliesst sie wieder.

        Fuer eine PDF, die ungeteilt hinausgeht: Sie wird nicht geschnitten, also
        faellt eine Seite, die sich nicht laden laesst, sonst erst im Worker auf
        (`PdfiumError: Failed to load page.`), nach der Bezahlung, und endet
        `failed` statt `input_error`. Geschnitten faellt dieselbe Seite schon in
        `extract` auf.

        Raises:
            CorruptDocumentError: Eine Seite laesst sich nicht laden; die Meldung
                nennt sie null-basiert.

        Kosten: keine. Reine lokale Rechenarbeit.
        """
        for nummer in range(self.page_count):
            try:
                seite = self._doc[nummer]
            except pypdfium2.PdfiumError as fehler:
                raise CorruptDocumentError(
                    f"Seite {nummer} (null-basiert) laesst sich nicht laden: {fehler}"
                ) from fehler
            seite.close()

    def render_page(self, page: int) -> bytes:
        """Rendert eine Einzelseite als JPEG: 200 dpi, laengste Kante hoechstens 3500 Pixel.

        Nur als Ersatz fuer eine zu grosse Einzelseite (Mineroo#49). Wie der
        Schnitt synchron im Event-Loop: PDFium darf nicht in einen Thread.
        `CorruptDocumentError` nennt eine Seite, die sich nicht rendern laesst.
        """
        if not 0 <= page < self.page_count:
            raise ValueError(f"Ungueltige Seite {page} (null-basiert).")
        try:
            seite = self._doc[page]
            try:
                breite, hoehe = seite.get_size()
                bitmap = seite.render(scale=min(200 / 72, 3500 / max(breite, hoehe)))
                try:
                    bild = bitmap.to_pil()
                    try:
                        # PDFium rundet Pixel auf; Gleitkommafehler koennen 3501 ergeben.
                        bild.thumbnail((3500, 3500))
                        puffer = io.BytesIO()
                        bild.save(puffer, format="JPEG", quality=90)
                        return puffer.getvalue()
                    finally:
                        bild.close()
                finally:
                    bitmap.close()
            finally:
                seite.close()
        except pypdfium2.PdfiumError as fehler:
            raise CorruptDocumentError(
                f"Seite {page} (null-basiert) laesst sich nicht rendern: {fehler}"
            ) from fehler

    def extract(self, first_page: int, last_page: int) -> bytes:
        """Schneidet einen Seitenbereich als eigenstaendiges PDF heraus.

        Args:
            first_page: Erste Seite, NULL-basiert, einschliessend.
            last_page: Letzte Seite, NULL-basiert, EINSCHLIESSEND. `extract(0, 0)`
                liefert genau die erste Seite.

        Returns:
            Die Bytes eines vollstaendigen, fuer sich lesbaren PDF-Dokuments mit
            genau `last_page - first_page + 1` Seiten.

        Vorbedingung:
            `0 <= first_page <= last_page < page_count`. Verletzung ist ein
            Programmierfehler und wirft `ValueError`, keinen `InputError` — es
            liegt dann nicht an der Datei, sondern am Aufrufer.

        Umsetzung, weil zwei Einzelheiten daran haengen:
            `PdfDocument.new()`, dann `import_pages(quelle, pages=[…])` mit einer
            LISTE null-basierter Indizes (nie der Zeichenketten-Form, siehe
            Modul-Vorspann), dann `save(io.BytesIO())`.

            Beim Speichern bleibt es bei der VOREINSTELLUNG der Flaggen, und das
            ist wichtiger, als es aussieht: `save` benutzt ohne Zutun
            `FPDF_NO_INCREMENTAL` — es schreibt die Datei neu. Wer dort spaeter
            `flags=0` setzt, bekommt stillschweigend das inkrementelle Anhaengen
            und damit genau die Aufblaehung zurueck, wegen der pypdf im ganzen
            Projekt verboten ist. Die gemessenen 1,00 haengen an dieser Flagge.

        Zusicherung:
            Das Ergebnis hat GENAU `last_page - first_page + 1` Seiten. Diese
            Zusicherung wird nicht behauptet, sondern geprueft: Das neu gebaute
            Dokument wird gezaehlt, bevor es gespeichert wird, und bei Abweichung
            fliegt `CorruptDocumentError`. Sie steht hier und nicht beim Aufrufer,
            weil hier das gezaehlte Dokument ohnehin schon in der Hand liegt — die
            Pruefung kostet an dieser Stelle nichts und waere anderswo ein
            erneutes Oeffnen. Sie ist die Antwort auf die geerbte Falle aus dem
            Modul-Vorspann: Ein Schnitt, der Seiten verliert, wird hier laut,
            nicht spaeter still.

            Der Ausschnitt blaeht nicht auf: gemessen am 17-MB-Bescheid mit
            16 Seiten ergab die Summe aller Einzelseiten 1,00-fache Groesse des
            Originals (das Verbot von pypdf im Betriebshandbuch ist
            pypdf-spezifisch — dort wurden aus 360 MB Quelle 4,1 GB Einzelseiten,
            weil pypdf das gemeinsame Ressourcen-Verzeichnis in jede Seite
            kopiert). Diese Zusicherung ist eine MESSUNG an einem Dokument, keine
            Eigenschaft des Formats: Ein Dokument mit sehr grossen, auf allen
            Seiten geteilten Ressourcen koennte sich anders verhalten. Deshalb
            prueft der Aufrufer die Groesse des Ergebnisses und halbiert bei
            Bedarf weiter (`chunking.build_chunks`), statt sich auf 1,00 zu
            verlassen.

            Die Seitenreihenfolge bleibt die des Originals. Lesezeichen,
            Formularfelder und Anmerkungen ueberleben den Schnitt nicht
            notwendigerweise — das ist hinnehmbar, weil das Ergebnis
            ausschliesslich zur Texterkennung dient und nie archiviert wird.

        Raises:
            ValueError: Vorbedingung verletzt.
            CorruptDocumentError: Der Schnitt schlaegt fehl (kaputte Seite).

        Kosten: keine. Reine lokale Rechenarbeit.
        """
        if not 0 <= first_page <= last_page < self.page_count:
            raise ValueError(
                f"Ungueltiger Seitenbereich ({first_page}, {last_page}) fuer ein "
                f"Dokument mit {self.page_count} Seiten. Erwartet wird "
                f"0 <= first_page <= last_page < {self.page_count}; die Zaehlung "
                f"ist null-basiert und beidseitig einschliessend."
            )

        erwartet = last_page - first_page + 1
        # AUSSCHLIESSLICH die Listenform: null-basierte Indizes. Die
        # Zeichenketten-Form derselben Funktion zaehlt EINS-basiert, und die
        # Verwechslung verschoebe jedes Dokument um genau eine Seite — am
        # Einzelergebnis unsichtbar.
        seiten = list(range(first_page, last_page + 1))

        neu = pypdfium2.PdfDocument.new()
        try:
            try:
                neu.import_pages(self._doc, pages=seiten)
            except pypdfium2.PdfiumError as fehler:
                raise CorruptDocumentError(
                    f"Seiten {first_page} bis {last_page} "
                    f"liessen sich nicht herausschneiden: {fehler}"
                ) from fehler

            # Die Nachzaehlung, BEVOR gespeichert wird. Sie steht hier, weil das
            # gezaehlte Dokument ohnehin schon in der Hand liegt, und sie ist die
            # Antwort auf die geerbte Falle: Ein Schnitt, der Seiten verliert,
            # wird laut statt spaeter still.
            if len(neu) != erwartet:
                raise CorruptDocumentError(
                    f"Der Ausschnitt der Seiten {first_page} "
                    f"bis {last_page} hat {len(neu)} statt {erwartet} Seiten. "
                    f"Wahrscheinlichste Ursache: eine beschaedigte Seite wurde "
                    f"beim Schneiden uebersprungen."
                )

            puffer = io.BytesIO()
            # Keine `flags`-Angabe: Die Voreinstellung ist FPDF_NO_INCREMENTAL,
            # also vollstaendiges Neuschreiben. Mit `flags=0` bekaeme man das
            # inkrementelle Anhaengen — und damit genau die Aufblaehung zurueck,
            # wegen der pypdf im ganzen Projekt verboten ist. Die gemessenen
            # 1,00 haengen an dieser Voreinstellung.
            neu.save(puffer)
            return puffer.getvalue()
        finally:
            neu.close()
