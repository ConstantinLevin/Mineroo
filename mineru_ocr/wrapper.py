"""Die Huellen aus Jurgens Braindump: ein Dokument, viele Dokumente.

Beide Klassen sind REINE Postgres-Leser. Sie sprechen nie mit RunPod, sie
verarbeiten nichts, sie warten auf nichts. Eine Huelle ist die bequeme Sicht auf
eine Zeile — mehr will sie nicht sein, und weniger auch nicht.

Der Braindump, aus dem das stammt (Plan 2.1), ist an drei Stellen sehr genau, und
alle drei sind hier eingebaut. Erstens die Identitaet ueber den Inhalts-Hash:
„vor einem run wird auch immer die inputdatei von OCRWRAPPER initialisierung
gehashed und geschaut ob es […] bereits dafür einen eintrag gibt". Zweitens das
Nichts-Tun bei Fertigem: „wenn ocrwrapper bereits OCR'd ist dann ist es eine
noop". Drittens der Maximalismus: „ja alles an metadaten etc was speicherbar ist
in den objekten speichern. maximalismus."

Und eine Stelle ist wichtiger als alle drei, weil sie bestimmt, was gespeichert
wird und was nicht: „wir speicehrn lediglich […] den ocrwrapper samt raw output
darin und die getter formatieren nur on the fly (von mir aus auch gerne gecached)
den output in die gebracuhten funktionen." Gespeichert wird ausschliesslich roh.
Alles Aufbereitete entsteht beim Zugriff und wird hoechstens im Objekt behalten,
nie in der Datenbank.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, TypeVar

import psycopg

from . import assembly, db, mime
from .config import Settings
from .db import DocumentRow, DocumentStatus
from .errors import (
    DocumentNotFound,
    IncompleteResultError,
    TooLargeError,
    UnsupportedTypeError,
)

F = TypeVar("F")
"""Der erwartete Typ eines Feldes im gespeicherten Ergebnis (`OCRWrapper._feld`)
— `str` fuer `markdown`, `list` fuer `content_list`, `dict` fuer `images`."""


def _groesse_pruefen(groesse: int, was: str) -> None:
    """Weist eine Eingabe ab, die nicht in `inputs` passt — mit einer Meldung.

    Ohne diese Pruefung scheitert das Ablegen ab etwa 1 GiB mit einem
    Verbindungsabbruch des Servers, und in einem Ordnerlauf bricht das das ganze
    Einreihen ab.

    Raises:
        TooLargeError: `groesse` liegt ueber `db.MAX_INPUT_BYTES`.
    """
    if groesse > db.MAX_INPUT_BYTES:
        raise TooLargeError(
            f"{was}: {groesse} Byte sind mehr, als sich ablegen laesst "
            f"({db.MAX_INPUT_BYTES} Byte). Die Eingabe liegt als `bytea` in der "
            f"Datenbank, und Postgres begrenzt ein Feld auf knapp 1 GiB."
        )


def _anlegen(
    conn: psycopg.Connection[Any],
    *,
    data: bytes,
    sha256: str,
    mime_type: str,
    endpoint_id: str,
) -> bool:
    """Legt Zeile und Eingabe an — in EINER Transaktion, und nur neu.

    Die Eingabe kommt nur zu einer Zeile, die dabei neu entsteht. Gibt es die
    Zeile schon, ist das Dokument entweder fertig (dann ist seine Eingabe
    absichtlich weg und darf nicht zurueckkehren) oder unterwegs oder gescheitert
    (dann liegt seine Eingabe bereits da). Beides in einer Transaktion, damit es
    keine Zeile ohne Eingabe gibt, die ein Job nicht lesen koennte.

    Returns:
        True, wenn die Zeile neu angelegt wurde.
    """
    with conn.transaction():
        angelegt = db.insert_document(
            conn,
            sha256=sha256,
            size_bytes=len(data),
            mime=mime_type,
            endpoint_id=endpoint_id,
        )
        if angelegt:
            db.insert_input(conn, sha256=sha256, content=data)
    return angelegt


class OCRWrapper:
    """Ein Dokument: seine Angaben, sein Zustand, sein Ergebnis.

    Die Huelle haelt eine Abschrift der Zeile, wie sie beim letzten Lesen war.
    Sie ist also NICHT von selbst aktuell — `refresh()` holt den neuen Stand.
    Das ist Absicht und nicht Faulheit: Eine Huelle, die bei jedem Attributzugriff
    heimlich die Datenbank fragt, macht aus einer Schleife ueber tausend Dokumente
    unbemerkt zehntausend Abfragen. Wer den frischen Stand braucht, sagt es.

    Nebenlaeufigkeit: nicht nebenlaeufig benutzbar. Eine Huelle gehoert einem
    Aufrufer. Die Datenbank darunter vertraegt beliebig viele Leser.
    """

    @classmethod
    def from_path(
        cls, path: Path, conn: psycopg.Connection[Any], settings: Settings
    ) -> OCRWrapper:
        """Baut eine Huelle aus einer Datei: lesen, dann wie `from_bytes`.

        Vor dem Lesen werden Typ (am Anfang der Datei, bei einem ZIP am
        Verzeichnis) und Groesse geprueft: Eine grosse Datei, die ohnehin
        abgewiesen wird, kommt so nie ganz in den Speicher. Danach wird die Datei
        einmal ganz gelesen; Hash, Typ und die abgelegte Eingabe stammen aus
        denselben Bytes. Der Pfad geht nirgends hin — weder in die Datenbank noch
        in den Job. Er ist nach dem Lesen vergessen.

        Raises:
            UnsupportedTypeError: Der Typ wird nicht unterstuetzt. SOFORT beim
                Bauen, nicht erst beim Verarbeiten — „es wird immer ein objekt der
                klasse OCRWRAPPER zurückgegeben […] rejected falls nicht
                unterstüzter type" (Plan 2.1). Die Meldung nennt den Dateinamen.
            TooLargeError: Die Datei passt nicht in `inputs` (`db.MAX_INPUT_BYTES`).
            OSError: Nicht lesbar.

        Kosten: keine.
        """
        mime.sniff_path(path)
        _groesse_pruefen(path.stat().st_size, path.name)
        return cls.from_bytes(path.read_bytes(), conn, settings)

    @classmethod
    def from_row(cls, row: DocumentRow, conn: psycopg.Connection[Any]) -> OCRWrapper:
        """Baut eine Huelle um eine bereits gelesene Zeile. Kein Dateizugriff.

        Der Weg von einem Hash zu einem Ergebnis, und ohne ihn faellt die halbe
        Bedienung aus: `mineru-ocr markdown <hash>` hat nur den Hash — es gibt
        nichts zu hashen und nichts zu erkennen, die Zeile steht ja schon da. Ohne
        diesen Konstruktor muesste sich die Kommandozeile ihre eigene Fassung der
        Getter bauen, und damit gaebe es zwei.

        Args:
            row: Eine vollstaendig gelesene Zeile (`db.get_document`), nicht eine
                aus `list_documents`, der `raw` fehlt.
            conn: Fuer spaetere `refresh()`-Aufrufe.

        Kosten: keine.
        """
        huelle = cls.__new__(cls)
        huelle.row = row
        huelle._conn = conn
        huelle._zwischenspeicher = {}
        return huelle

    @classmethod
    def from_bytes(
        cls,
        data: bytes,
        conn: psycopg.Connection[Any],
        settings: Settings,
    ) -> OCRWrapper:
        """Baut eine Huelle aus Bytes und legt Zeile und Eingabe an, falls sie fehlen.

        Der Ablauf: Hash der Bytes bilden; Typ am Inhalt bestimmen und bei einem
        nicht unterstuetzten SOFORT werfen, ohne etwas anzulegen; Zeile und
        Eingabe in `inputs` in einer Transaktion anlegen. Es wird keine Datei
        geschrieben und kein PDF geoeffnet: Die Seitenzahl bestimmt der Job.

        „Uebernehmen" ist dabei der wichtigere Fall und die Umsetzung von Jurgens
        Nichts-Tun-Regel: Gibt es die Zeile schon und ist sie fertig, ist diese
        Huelle sofort fertig. Es wird nichts neu verarbeitet, nichts erneut
        bezahlt, und die Eingabe kommt nicht zurueck.

        `settings` wird gebraucht, weil `db.insert_document` den `endpoint_id`
        verlangt. Sie wird uebergeben und nicht aus einer globalen Stelle geholt —
        es gibt keine, und das ist eine ausdrueckliche Regel (`config.py`).

        Args:
            data: Die vollstaendigen Bytes.
            conn: Offene Verbindung, gehoert dem Aufrufer.

        Returns:
            Die Huelle, entweder zu einer neuen oder zu einer uebernommenen Zeile.

        Raises:
            UnsupportedTypeError: Der Typ wird nicht unterstuetzt.
            TooLargeError: Die Bytes passen nicht in `inputs` (`db.MAX_INPUT_BYTES`).

        Kosten: keine.
        """
        typ = mime.sniff_bytes(data)
        _groesse_pruefen(len(data), "die Bytes")
        sha256 = hashlib.sha256(data).hexdigest()

        _anlegen(
            conn,
            data=data,
            sha256=sha256,
            mime_type=typ.mime,
            endpoint_id=settings.endpoint_id,
        )
        zeile = db.get_document(conn, sha256)
        if zeile is None:  # pragma: no cover — direkt nach dem Anlegen unmoeglich
            raise DocumentNotFound(
                f"Die Zeile zu {sha256} war unmittelbar nach dem Anlegen nicht "
                f"lesbar. Das deutet auf eine abgebrochene Transaktion beim "
                f"Aufrufer hin."
            )
        return cls.from_row(zeile, conn)

    # -- Angaben aus der Zeile --------------------------------------------

    row: DocumentRow
    """Die Zeile beim letzten Lesen. Der Maximalismus aus dem Braindump steckt
    hier: NICHTS wird beim Herausgeben weggelassen, alles was in der Zeile steht,
    steht auch hier."""

    @property
    def sha256(self) -> str:
        return self.row.sha256

    @property
    def size_bytes(self) -> int:
        return self.row.size_bytes

    @property
    def mime(self) -> str:
        return self.row.mime

    @property
    def page_count(self) -> int | None:
        return self.row.page_count

    @property
    def status(self) -> DocumentStatus:
        return self.row.status

    @property
    def notes(self) -> list[dict[str, Any]]:
        return self.row.notes

    @property
    def success(self) -> bool:
        """Ob die Texterkennung geklappt hat — `done` oder `empty`.

        Genau Jurgens Bedeutung: „\\"success\\" ist eher gemeint also ob das ocr
        geklappt hat" (Plan 2.1). `empty` zaehlt ausdruecklich als Erfolg: Eine
        leere Seite wurde erfolgreich als leer erkannt. Wer zwischen „hat Text"
        und „hat geklappt" unterscheiden muss, sieht auf `status`.
        """
        return self.row.status in (DocumentStatus.DONE, DocumentStatus.EMPTY)

    # -- Ergebnis, jedes Mal frisch aus dem Rohbestand --------------------

    # Alles Folgende liest aus dem FERTIGEN Ergebnis in der Zeile. Zusammengesetzt
    # wurde es im Executor, vor dem Schreiben — hier wird nichts mehr gefuegt und
    # nichts mehr umgerechnet. Deshalb kann hier auch nichts unvollstaendig sein:
    # Eine Zeile ist entweder endgueltig, dann ist das Ergebnis ganz, oder sie ist
    # es nicht, dann gibt es keins.

    def __getitem__(self, name: str) -> Any:
        """Ein Element des gespeicherten Ergebnisses, unveraendert.

        `huelle["markdown"]` ist `huelle.raw["markdown"]`, `huelle["content_list"]`
        ist `huelle.raw["content_list"]`, und so fort fuer jedes Element, das
        `assembly.assemble` erzeugt: `images`, `middle`, `layout_pdfs`,
        `envelopes`, `extras` — und alles, was spaeter dazukommt, ohne dass hier
        etwas nachgetragen werden muesste.

        WARUM EIN ZUGANG STATT EINES GETTERS JE FELD: Hier standen vorher
        `markdown` und `content_list` als eigene Eigenschaften. Beide taten
        nichts anderes, als ein Feld durchzureichen — sie waren eine
        handverlesene Auswahl aus dem, was ohnehin vollstaendig da ist, und jede
        solche Auswahl ist eine Aussage darueber, was wichtig sei. Diese Aussage
        hat hier niemand zu treffen: In `raw` steht alles gleichberechtigt
        nebeneinander, und der Zugang bildet das ab.

        Ausgenommen bleibt, was mehr tut als durchreichen: `images` wandelt aus
        Base64 zurueck nach Bytes, und das kann ein generischer Zugang nicht —
        `huelle["images"]` liefert die gespeicherten Zeichenketten.

        Raises:
            IncompleteResultError: Die Zeile hat noch kein Ergebnis (`new`) oder
                ist gescheitert. Es gibt dann nichts auszugeben — was da ist,
                steht in `raw`.
            KeyError: Das Ergebnis traegt dieses Element nicht. Ausdruecklich ein
                Fehler und kein `None`: Ein stiller Ersatzwert liesse einen
                Tippfehler wie ein leeres Feld aussehen.
        """
        return self._ergebnis()[name]

    def __contains__(self, name: str) -> bool:
        """Ob das gespeicherte Ergebnis dieses Element traegt.

        Damit `if "layout_pdfs" in huelle` moeglich ist, ohne den `KeyError` aus
        `__getitem__` als Steuerfluss zu benutzen.
        """
        return name in self._ergebnis()

    @property
    def images(self) -> dict[str, bytes]:
        """Die Bilder des Dokuments.

        Aus Base64 zurueckgewandelt: In der Datenbank liegen sie als
        Zeichenketten, weil `jsonb` keine Bytes kennt. Die Wandlung ist
        verlustfrei und geschieht hier, damit der Aufrufer Bytes bekommt und
        nicht eine Kodierung, die er selbst aufloesen muesste.

        Zusicherung:
            JEDES gespeicherte Bild kommt heraus. Frueher stand hier ein Filter
            `if isinstance(wert, str)` im Dict-Comprehension — ein Bild, dessen
            Wert keine Zeichenkette war, verschwand damit ohne Fehler und ohne
            Meldung, und der Aufrufer bekam eine Abbildung, die vollstaendig
            aussah. Reproduziert wurde das mit drei gespeicherten Bildern, von
            denen der Getter eines lieferte. Das ist genau das Muster
            „herausgreifen statt nehmen", gegen das die Abruf-Regel steht — und
            es stand ausgerechnet in der Fassade, die dem Aufrufer das Ergebnis
            zeigt.

        Raises:
            IncompleteResultError: Wie bei `markdown`; zusaetzlich, wenn ein
                einzelnes Bild nicht dekodierbar ist. Die Meldung nennt seinen
                Namen — wer ihn hat, findet das Bild in `raw` wieder.
        """
        roh = self._feld("images", dict)
        bilder: dict[str, bytes] = {}
        for name, wert in roh.items():
            if isinstance(wert, bytes):
                bilder[name] = wert
                continue
            if not isinstance(wert, str):
                raise IncompleteResultError(
                    f"Bild {name!r} liegt als {type(wert).__name__} vor, "
                    f"erwartet wurde eine Base64-Zeichenkette. Das Bild ist "
                    f"nicht verloren — es steht unveraendert in `raw`."
                )
            try:
                bilder[name] = base64.b64decode(wert)
            except (binascii.Error, ValueError) as fehler:
                raise IncompleteResultError(
                    f"Bild {name!r} liess sich nicht aus Base64 zurueckwandeln: "
                    f"{fehler}. Es steht unveraendert in `raw`."
                ) from fehler
        return bilder

    def _feld(self, name: str, typ: type[F]) -> F:
        """Holt ein Feld des Ergebnisses. Fehlt es oder hat es den falschen Typ,
        wird geworfen.

        Der frueherer Bau war ueberall derselbe — `wert if isinstance(wert, typ)
        else ersatz` —, und er hatte ueberall dieselbe Schwaeche: Ein Formfehler
        in `raw` wurde zu einem leeren Wert, und ein leerer Wert sieht aus wie
        ein leeres Dokument. Damit war der einzige Hinweis auf eine beschaedigte
        Zeile ausgerechnet der, den man fuer ein gueltiges Ergebnis haelt.

        HIER STAND EIN ERSATZWERT FUER DEN FALL, DASS DAS FELD GAR NICHT DA IST,
        mit der Begruendung, das sei Abwesenheit („eine aeltere Fassung hat es
        nicht geschrieben") und keine Beschaedigung. Die Begruendung war nie
        geprueft. Nachgeprueft (10.08.2026) ist sie falsch: `assembly.assemble`
        schreibt `images` — das einzige Feld, das ueber diese Funktion geht — in
        JEDER Fassung seit der Erstimplementierung, nachgesehen in allen neun
        Commits an `assembly.py`, und alle acht Zeilen der Produktionsdatenbank
        tragen den Schluessel. Ein fehlendes `images` ist damit dasselbe wie ein
        `images` mit falschem Typ: eine beschaedigte oder fremd geschriebene
        Zeile.

        Und die Folge waere dieselbe wie ueberall in dieser Datei: Ein leeres
        `{}` heisst „dieses Dokument hat keine Bilder", und das ist ein
        vollkommen gewoehnliches Ergebnis — zwei der sechs echten Antworten
        haben wirklich keine. Der Ersatzwert haette den Schaden also in genau
        die Form gebracht, in der ihn niemand mehr erkennt.
        """
        ergebnis = self._ergebnis()
        if name not in ergebnis:
            raise IncompleteResultError(
                f"Dem gespeicherten Ergebnis fehlt das Feld {name!r}. "
                f"`assembly.assemble` schreibt es immer, in jeder Fassung — die "
                f"Zeile ist also beschaedigt oder von fremder Hand geschrieben. "
                f"Sie wird NICHT mit einem leeren Wert beantwortet: Ein leeres "
                f"Feld ist hier ein gueltiges Ergebnis und damit der einzige "
                f"Schaden, den man nicht sieht. Was da ist, steht unveraendert "
                f"in `raw`."
            )
        wert = ergebnis[name]
        if isinstance(wert, typ):
            return wert
        raise IncompleteResultError(
            f"Das Feld {name!r} des gespeicherten Ergebnisses ist "
            f"{type(wert).__name__} statt {typ.__name__}. Die Zeile ist "
            f"beschaedigt oder von fremder Hand geschrieben. Der Inhalt ist "
            f"nicht verloren — er steht unveraendert in `raw`."
        )

    @property
    def raw(self) -> dict[str, Any]:
        """Das gespeicherte Gesamtergebnis, unveraendert und ungefiltert.

        Der Weg an allem vorbei: Wer etwas braucht, was die Getter nicht liefern
        — `middle`, die Layout- und Span-PDFs, die Umschlaege mit Laufzeiten und
        `debug`-Bloecken — oder wer pruefen will, ob ein Getter richtig liegt,
        sieht hier nach.

        Wirft NICHT bei gescheiterten Dokumenten: Gerade dann will man sehen, was
        ueberhaupt ankam, und dort steht auch der Fehler-Umschlag.
        """
        self._sicherstellen_dass_raw_geladen_ist()
        return self.row.raw if self.row.raw is not None else {}

    @property
    def rendered_markdown(self) -> str:
        """Das GANZE Dokument in Markdown-Form. Siehe `assembly.rendered_markdown`.

        Alles, was auf dem Dokument steht, in einer Darstellung. `self["markdown"]`
        — das Markdown der Engine — ist davon der grosse Bestandteil, aber nicht
        das Ganze: Die Engine filtert beim Erzeugen ihres Markdowns Blockklassen
        heraus, und in genau ihnen liegt das Aktenzeichen.

        OHNE SCHALTER, und das ist Absicht. Diese Methode hiess `rendered_text`
        und hatte zwei Parameter, `furniture` und `foreign_filter`. Beide
        stellten dieselbe Frage — „was gehoert zum Text?" —, und beide
        beantworteten sie mit einer Voreinstellung. Das ist eine redaktionelle
        Entscheidung, und sie gehoert dem, der das Ergebnis benutzt, nicht dem,
        der es aufbereitet. Wer eine Auswahl braucht, baut sie aus
        `self["markdown"]` und `self["content_list"]` — an der Stelle, an der er
        weiss, wofuer.

        Raises:
            IncompleteResultError: Die Zeile hat noch kein Ergebnis — oder das
                gespeicherte Ergebnis ist beschaedigt und laesst sich nicht ganz
                darstellen (`markdown` mit falschem Typ, `content_list` keine
                Liste oder mit einem Eintrag, der kein Dict ist, oder beide
                Felder fehlen). Eine halbe Ausgabe gibt es nicht: Sie saehe
                vollstaendig aus. Was da ist, steht unveraendert in `raw`.
        """
        return assembly.rendered_markdown(self._ergebnis())

    # -- Umgang -----------------------------------------------------------

    def refresh(self) -> OCRWrapper:
        """Liest die Zeile neu und verwirft alle zwischengespeicherten Ergebnisse.

        Returns:
            Dieselbe Huelle, damit man `w.refresh().rendered_markdown` schreiben kann.

        Raises:
            DocumentNotFound: Die Zeile gibt es nicht mehr.
        """
        zeile = db.get_document(self._conn, self.row.sha256)
        if zeile is None:
            raise DocumentNotFound(
                f"Die Zeile zu {self.row.sha256} gibt es nicht mehr."
            )
        self.row = zeile
        self._zwischenspeicher.clear()
        return self

    def to_dict(self) -> dict[str, Any]:
        """Alles ueber dieses Dokument als einfache Abbildung — ohne `raw`.

        Fuer die Ausgabe mit `--json`. `raw` fehlt bewusst: Es waere je Dokument
        gross (rund 0,1 MB je Seite), und keine Statusausgabe braucht es. Wer
        es will, nimmt `raw`.
        """
        return {
            "sha256": self.row.sha256,
            "size_bytes": self.row.size_bytes,
            "mime": self.row.mime,
            "page_count": self.row.page_count,
            "status": self.row.status.value,
            "success": self.success,
            "error": self.row.error,
            "endpoint_id": self.row.endpoint_id,
            "image_tag_at_submit": self.row.image_tag_at_submit,
            "notes": self.row.notes,
            "created_at": self.row.created_at.isoformat(),
            "updated_at": self.row.updated_at.isoformat(),
        }

    def __repr__(self) -> str:
        return (
            f"OCRWrapper(sha256={self.row.sha256!r}, "
            f"status={self.row.status.value!r})"
        )

    # -- Das Innere -------------------------------------------------------

    _conn: psycopg.Connection[Any]
    _zwischenspeicher: dict[str, Any]

    def _sicherstellen_dass_raw_geladen_ist(self) -> None:
        """Laedt `raw` nach, wenn die Zeile aus einer Liste stammt.

        Der Fall, gegen den das steht, ist der unsichtbarste im ganzen Paket:
        `list_documents` laedt `raw` NICHT (zweistellige Gigabyte bei
        dreissigtausend Dokumenten) und setzt dafuer `raw_loaded = False`. Ohne
        dieses Nachladen lieferte eine Huelle aus einer gelisteten Zeile fuer ein
        FERTIGES Dokument einen leeren Text — und das saehe aus wie ein leeres
        Ergebnis, nicht wie ein Fehler.
        """
        if self.row.raw_loaded:
            return
        zeile = db.get_document(self._conn, self.row.sha256)
        if zeile is None:
            raise DocumentNotFound(
                f"Die Zeile zu {self.row.sha256} gibt es nicht mehr."
            )
        self.row = zeile

    def _ergebnis(self) -> dict[str, Any]:
        """Das zusammengesetzte Ergebnis — oder eine klare Ansage, warum keins.

        Hier faellt die Entscheidung, die alle Ergebnis-Getter teilen: Ein
        Dokument ohne Endergebnis liefert NICHTS und sagt es. Still einen leeren
        Text zu liefern waere der Fehler, den niemand bemerkt, weil er wie ein
        leeres Dokument aussieht.
        """
        if "ergebnis" in self._zwischenspeicher:
            wert: dict[str, Any] = self._zwischenspeicher["ergebnis"]
            return wert

        if not self.success:
            raise IncompleteResultError(
                f"{self.row.sha256} steht auf {self.row.status.value!r} und "
                f"hat kein vollstaendiges Ergebnis. Was bis dahin ankam — auch "
                f"bei einem gescheiterten Dokument — steht in `.raw`; dort wirft "
                f"nichts."
            )

        self._sicherstellen_dass_raw_geladen_ist()
        ergebnis = self.row.raw if self.row.raw is not None else {}
        self._zwischenspeicher["ergebnis"] = ergebnis
        return ergebnis


class OCRMegaWrapper:
    """Viele Dokumente als eine Einheit — Jurgens Ordner-Szenario.

    Der Fall, um den herum diese Klasse gebaut ist, steht woertlich im Braindump:
    „stell dir vor man hat einen OCRMEGAWRAPPER aus einem ordner initialisiert und
    dann lässt man laufen, dann später kommt zum ordner noch ein pfad hinzu den
    tut man einfach dazu und lässt nochmal durchlaufen weil eh nur läuft was noch
    fehlt" (Plan 2.1). Genau das muss ohne jede Sonderbehandlung funktionieren —
    und es tut es, weil die Identitaet am Hash haengt und Fertiges beim erneuten
    Durchlaufen ein Nichts-Tun ist.
    """

    @classmethod
    def from_paths(
        cls, paths: list[Path], conn: psycopg.Connection[Any], settings: Settings
    ) -> OCRMegaWrapper:
        """Baut eine Sammlung aus ausdruecklich genannten Pfaden.

        Raises:
            UnsupportedTypeError: Bei einem nicht unterstuetzten Pfad — hier wird
                geworfen und nicht uebersprungen. Wer eine Datei ausdruecklich
                nennt, meint sie; sie stillschweigend auszulassen waere die
                schlechtere Ueberraschung.
        """
        sammlung = cls.__new__(cls)
        sammlung._conn = conn
        sammlung._settings = settings
        sammlung._huellen = {}
        sammlung._uebersprungen = {}
        for pfad in paths:
            huelle = OCRWrapper.from_path(pfad, conn, settings)
            sammlung._huellen[huelle.sha256] = huelle
        return sammlung

    @classmethod
    def from_dir(
        cls,
        directory: Path,
        conn: psycopg.Connection[Any],
        settings: Settings,
        glob: str = "**/*",
        progress: Callable[[int, int], None] | None = None,
    ) -> OCRMegaWrapper:
        """Baut eine Sammlung aus einem Verzeichnis.

        Anders als `from_paths` UEBERSPRINGT diese Methode nicht unterstuetzte
        Dateien und ZAEHLT sie, statt an der ersten `.DS_Store` zu sterben. Der
        Unterschied ist kein Widerspruch, sondern folgt aus der Absicht: Wer ein
        Verzeichnis uebergibt, meint „was hier verwertbar ist", nicht „jede
        einzelne Datei hier".

        Args:
            directory: Vorhandenes Verzeichnis.
            conn: Offene Verbindung.
            settings: Fuer `endpoint_id`.
            glob: Welche Dateien betrachtet werden. Voreinstellung: rekursiv alle.
            progress: Wird je fertig verarbeiteter Datei mit dem laufenden Zaehler
                und der Gesamtzahl gerufen. None heisst: keine Meldung.

        Returns:
            Die Sammlung. Was uebersprungen wurde, steht in `stats()` — gezaehlt
            und nach Grund aufgeschluesselt, nicht bloss verschwunden.

        Verhalten bei GROSSEN Bestaenden — der eigentliche Grund fuer `progress`:
            Der teure Teil ist nicht die Datenbank, sondern das Hashen. Jede Datei
            wird dafuer einmal ganz gelesen. Bei zehntausend Dokumenten laeuft
            diese Methode also minutenlang, waehrend aeusserlich nichts geschieht
            — und wer das nicht weiss, haelt es fuer einen Haenger und bricht ab.
            Deshalb meldet sie Fortschritt.

            Sie ist ausserdem WIEDERAUFSETZBAR, ohne dass sie etwas dafuer tun
            muesste: Ein zweiter Lauf ueber dasselbe Verzeichnis hasht zwar erneut,
            legt aber nichts doppelt an (der Hash ist der Primaerschluessel) und
            fasst Fertiges nicht an. Abbrechen und neu anfangen ist also
            unschaedlich — das ist dieselbe Eigenschaft, auf der das
            Ordner-Szenario beruht.

        Kosten: keine.
        """
        sammlung = cls.__new__(cls)
        sammlung._conn = conn
        sammlung._settings = settings
        sammlung._huellen = {}
        sammlung._uebersprungen = {}

        kandidaten = [pfad for pfad in sorted(directory.glob(glob)) if pfad.is_file()]
        gesamt = len(kandidaten)
        for nummer, pfad in enumerate(kandidaten, start=1):
            try:
                huelle = OCRWrapper.from_path(pfad, conn, settings)
            except UnsupportedTypeError:
                # GEZAEHLT, nicht verschwunden. Wer ein Verzeichnis uebergibt,
                # meint „was hier verwertbar ist" — aber er will trotzdem
                # wissen, was uebergangen wurde, und wie oft.
                sammlung._uebersprungen["nicht_unterstuetzter_typ"] = (
                    sammlung._uebersprungen.get("nicht_unterstuetzter_typ", 0) + 1
                )
            except TooLargeError:
                sammlung._uebersprungen["zu_gross"] = (
                    sammlung._uebersprungen.get("zu_gross", 0) + 1
                )
            except OSError:
                sammlung._uebersprungen["nicht_lesbar"] = (
                    sammlung._uebersprungen.get("nicht_lesbar", 0) + 1
                )
            else:
                sammlung._huellen[huelle.sha256] = huelle
            if progress is not None:
                progress(nummer, gesamt)
        return sammlung

    # Aus dem Braindump woertlich uebernommener Zweitname.
    paths = from_paths

    def add(self, item: Path | OCRWrapper) -> OCRWrapper:
        """Nimmt ein Dokument auf, doppelte werden ueber den Hash erkannt.

        Returns:
            Die Huelle — die neue, oder die bereits vorhandene, wenn derselbe
            Inhalt schon in der Sammlung ist.
        """
        huelle = (
            item
            if isinstance(item, OCRWrapper)
            else OCRWrapper.from_path(item, self._conn, self._settings)
        )
        vorhanden = self._huellen.get(huelle.sha256)
        if vorhanden is not None:
            return vorhanden
        self._huellen[huelle.sha256] = huelle
        return huelle

    def extend(self, paths: list[Path]) -> list[OCRWrapper]:
        """Nimmt mehrere Dokumente auf. Returns: nur die tatsaechlich neuen."""
        neue: list[OCRWrapper] = []
        for pfad in paths:
            vorher = set(self._huellen)
            huelle = self.add(pfad)
            if huelle.sha256 not in vorher:
                neue.append(huelle)
        return neue

    def refresh_dir(self, directory: Path, glob: str = "**/*") -> list[OCRWrapper]:
        """Sieht im Verzeichnis nach, was seit dem letzten Mal dazugekommen ist.

        Das Herzstueck des Ordner-Szenarios. Returns: nur die neuen Huellen —
        Vorhandenes wird nicht angefasst, Fertiges nicht neu verarbeitet.
        """
        neue: list[OCRWrapper] = []
        for pfad in sorted(directory.glob(glob)):
            if not pfad.is_file():
                continue
            try:
                huelle = OCRWrapper.from_path(pfad, self._conn, self._settings)
            except (UnsupportedTypeError, TooLargeError, OSError):
                continue
            if huelle.sha256 not in self._huellen:
                self._huellen[huelle.sha256] = huelle
                neue.append(huelle)
        return neue

    def refresh(self) -> OCRMegaWrapper:
        """Liest alle Zeilen neu. EINE Abfrage, nicht eine je Dokument.

        Der Unterschied ist bei dreissigtausend Dokumenten der zwischen einer
        Abfrage und dreissigtausend. Geladen wird dabei OHNE `raw` — eine
        Uebersicht braucht es nie, und mit ihm waeren es zweistellige Gigabyte.
        Die Huellen merken sich das (`raw_loaded = False`) und laden einzeln
        nach, sobald wirklich jemand das Ergebnis will.
        """
        for zeile in db.list_documents(self._conn):
            huelle = self._huellen.get(zeile.sha256)
            if huelle is not None:
                huelle.row = zeile
                huelle._zwischenspeicher.clear()
        return self

    # -- Uebersicht -------------------------------------------------------

    def status_counts(self) -> dict[DocumentStatus, int]:
        """Wie viele Dokumente in welchem Zustand — maschinell auswertbar.

        Der programmatische Teil von Jurgens Forderung: „er braucht methoden die
        den status z.b. aller in ihm aggregiert (programmatisch und per text
        ausgebbar)" (Plan 2.1).
        """
        zaehlung = {zustand: 0 for zustand in DocumentStatus}
        for huelle in self._huellen.values():
            zaehlung[huelle.status] += 1
        return zaehlung

    def stats(self) -> dict[str, Any]:
        """Zahlen ueber die ganze Sammlung: Zustaende, Bytes, Seiten, Fehler,
        Leere und die beim Einlesen uebersprungenen Dateien nach Grund.

        `pages` zaehlt nur Dokumente, deren Seitenzahl bekannt ist: Das Einreihen
        oeffnet keine PDF, die Seitenzahl kommt erst mit dem Endergebnis des Jobs.
        Offene Dokumente tragen also nichts bei."""
        zaehlung = self.status_counts()
        return {
            "documents": len(self._huellen),
            "status": {zustand.value: anzahl for zustand, anzahl in zaehlung.items()},
            "bytes": sum(h.size_bytes for h in self._huellen.values()),
            "pages": sum(
                h.page_count for h in self._huellen.values() if h.page_count is not None
            ),
            "failed": zaehlung[DocumentStatus.FAILED]
            + zaehlung[DocumentStatus.INPUT_ERROR],
            "empty": zaehlung[DocumentStatus.EMPTY],
            "pending": zaehlung[DocumentStatus.NEW],
            "skipped": dict(self._uebersprungen),
            "upgrade_cases": len(self._aufruest_faelle()),
        }

    def report(self, verbose: bool = False) -> str:
        """Dieselben Zahlen als lesbarer Text — der zweite Teil derselben Forderung.

        Weist ausserdem offene AUFRUEST-FAELLE aus (Dokumente mit `input_error`
        und einem Aufruest-Hinweis in `notes`). Das ist der vorgesehene Weg, auf
        dem so ein Fall zu einem Aufgaben-Eintrag wird: Der Report zeigt ihn, der
        Mensch legt ihn an. Der Daemon legt NIE selbst einen an (Plan 4.1) — ein
        Hintergrundprozess, der Tickets erzeugt, ist ein Seiteneffekt, den
        niemand erwartet.

        Args:
            verbose: False gibt die Zusammenfassung. True zaehlt zusaetzlich jedes
                gescheiterte Dokument einzeln auf, mit Hash und Fehlertext.
        """
        zahlen = self.stats()
        zeilen = [
            f"{zahlen['documents']} Dokumente, {zahlen['bytes']} Byte, "
            f"{zahlen['pages']} Seiten (soweit bekannt)",
        ]
        for zustand in DocumentStatus:
            zeilen.append(f"  {zustand.value:<12} {zahlen['status'][zustand.value]}")

        if zahlen["skipped"]:
            zeilen.append("Beim Einlesen uebersprungen:")
            for grund, anzahl in sorted(zahlen["skipped"].items()):
                zeilen.append(f"  {grund:<28} {anzahl}")

        aufruestbar = self._aufruest_faelle()
        if aufruestbar:
            # Der vorgesehene Weg, auf dem ein solcher Fall zu einem
            # Aufgaben-Eintrag wird: Der Report zeigt ihn, der Mensch legt ihn
            # an. Der Daemon legt NIE selbst einen an — ein Hintergrundprozess,
            # der Tickets erzeugt, ist ein Seiteneffekt, den niemand erwartet.
            zeilen.append(
                f"OFFENE AUFRUEST-FAELLE: {len(aufruestbar)} — bitte je einen "
                f"Aufgaben-Eintrag anlegen:"
            )
            for huelle in aufruestbar:
                zeilen.append(f"  {huelle.sha256}")

        if verbose:
            gescheitert = self.failed
            if gescheitert:
                zeilen.append("Gescheitert im Einzelnen:")
                for huelle in gescheitert:
                    zeilen.append(
                        f"  {huelle.sha256}  [{huelle.status.value}]  "
                        f"{huelle.row.error}"
                    )
        return "\n".join(zeilen)

    def _aufruest_faelle(self) -> list[OCRWrapper]:
        """Dokumente mit `input_error` UND einem Aufruest-Hinweis in `notes`.

        Erkannt am Wort AUFRUESTBAR, das `chunking.check_fits_unsplit` in seine
        Meldung setzt und der Executor in den Vermerk uebernimmt. Ein eigenes
        Feld dafuer waere ein zweites Datenmodell fuer etwas, das noch keines
        hat — `notes` ist gerade der Ort dafuer.
        """
        faelle: list[OCRWrapper] = []
        for huelle in self._huellen.values():
            if huelle.status is not DocumentStatus.INPUT_ERROR:
                continue
            text = f"{huelle.row.error} {huelle.notes}"
            if "AUFRUESTBAR" in text:
                faelle.append(huelle)
        return faelle

    # -- Sichten ----------------------------------------------------------

    @property
    def pending(self) -> list[OCRWrapper]:
        """Alle noch nicht endgueltigen — genau die, die ein Durchlauf anfasst."""
        return [h for h in self._huellen.values() if not h.status.is_terminal]

    @property
    def done(self) -> list[OCRWrapper]:
        """Alle erfolgreichen, `done` und `empty`."""
        return [h for h in self._huellen.values() if h.success]

    @property
    def failed(self) -> list[OCRWrapper]:
        """Alle gescheiterten, `failed` und `input_error`."""
        return [
            h
            for h in self._huellen.values()
            if h.status in (DocumentStatus.FAILED, DocumentStatus.INPUT_ERROR)
        ]

    def by_state(self, state: DocumentStatus) -> list[OCRWrapper]:
        """Alle in genau einem Zustand."""
        return [h for h in self._huellen.values() if h.status is state]

    def __iter__(self) -> Iterator[OCRWrapper]:
        return iter(self._huellen.values())

    def __len__(self) -> int:
        return len(self._huellen)

    def __getitem__(self, key: int | str) -> OCRWrapper:
        """Zugriff ueber Position ODER ueber den Hash."""
        if isinstance(key, int):
            return list(self._huellen.values())[key]
        huelle = self._huellen.get(key)
        if huelle is None:
            raise KeyError(f"Kein Dokument mit dem Hash {key} in dieser Sammlung.")
        return huelle

    def __repr__(self) -> str:
        zaehlung = self.status_counts()
        teile = ", ".join(
            f"{zustand.value}={anzahl}"
            for zustand, anzahl in zaehlung.items()
            if anzahl
        )
        return f"OCRMegaWrapper({len(self._huellen)} Dokumente: {teile})"

    # -- Das Innere -------------------------------------------------------

    _conn: psycopg.Connection[Any]
    _settings: Settings
    _huellen: dict[str, OCRWrapper]
    _uebersprungen: dict[str, int]
