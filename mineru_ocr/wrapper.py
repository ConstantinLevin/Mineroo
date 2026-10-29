"""Die Huelle aus Jurgens Braindump: ein Dokument, seine Angaben und sein Ergebnis.

Die Klasse ist ein REINER Postgres-Leser. Sie spricht nie mit RunPod, sie
verarbeitet nichts, sie wartet auf nichts, und sie legt nichts an: Dokumente
entstehen durch `OCRClient.ocr_start`. Eine Huelle ist die bequeme Sicht auf eine
Zeile — mehr will sie nicht sein, und weniger auch nicht. Sie kennt keinen Pfad.

Der Braindump, aus dem das stammt (Plan 2.1), ist an drei Stellen sehr genau, und
alle drei sind eingebaut. Erstens die Identitaet ueber den Inhalts-Hash:
„vor einem run wird auch immer die inputdatei von OCRWRAPPER initialisierung
gehashed und geschaut ob es […] bereits dafür einen eintrag gibt" (das tut das
Abschicken). Zweitens das Nichts-Tun bei Fertigem: „wenn ocrwrapper bereits OCR'd
ist dann ist es eine noop" (auch das Abschicken). Drittens der Maximalismus: „ja alles an metadaten etc was speicherbar ist
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
from typing import Any, TypeVar

import psycopg

from . import assembly, db
from .config import ReadSettings
from .markdown import html_to_markdown
from .db import DocumentRow, DocumentStatus
from .errors import ConfigurationError, DocumentNotFound, IncompleteResultError

F = TypeVar("F")
"""Der erwartete Typ eines Feldes im gespeicherten Ergebnis (`OCRWrapper._feld`)
— `dict` fuer `images`."""


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
    def from_row(
        cls, row: DocumentRow, conn: psycopg.Connection[Any] | None
    ) -> OCRWrapper:
        """Baut eine Huelle um eine bereits gelesene Zeile.

        Der Weg von einem Hash zu einem Ergebnis: `OCRClient.get` und `wait` bauen
        ihre Huellen damit, und die Kommandozeile hat ebenfalls nur den Hash.

        Args:
            row: Eine gelesene Zeile. Fehlt ihr `raw` (`raw_loaded` ist False, wie
                bei `db.get_document(..., with_raw=False)` und `list_documents`),
                laedt die Huelle es nach, sobald jemand das Ergebnis will, ueber
                `conn`. Abschicken und Nachsehen lesen deshalb kein `raw`.
            conn: Fuer das Nachladen und fuer `refresh()`. `None` fuer die
                Huellen des `AsyncOCRClient`: Sie kommen mit geladenem `raw`,
                weil eine Eigenschaft nicht `await`en kann, und halten keine
                Verbindung; neu lesen heisst dort `await client.get(sha256)`.
                Ohne `raw` kommt eine solche Huelle nur, wenn waehrend des Wartens
                ein `retry` dazwischenlief; dann meldet `raw` `DocumentNotFound`.

        Kosten: keine.
        """
        huelle = cls.__new__(cls)
        huelle.row = row
        huelle._conn = conn
        huelle._zwischenspeicher = {}
        return huelle

    # -- Angaben aus der Zeile --------------------------------------------

    row: DocumentRow
    """Die Zeile beim letzten Lesen. Der Maximalismus aus dem Braindump steckt
    hier: NICHTS wird beim Herausgeben weggelassen, alles was in der Zeile steht,
    steht auch hier."""

    @property
    def sha256(self) -> str:
        return self.row.sha256

    @property
    def size_bytes(self) -> int | None:
        return self.row.size_bytes

    @property
    def mime(self) -> str | None:
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
    def mineru_version(self) -> str | None:
        """Die MinerU-Version, mit der das Ergebnis entstand, wie der Worker sie
        meldete (bei mehreren Teilstuecken die niedrigste, Mineroo#14)."""
        return self.row.mineru_version

    @property
    def read_settings(self) -> ReadSettings | None:
        """Die Lese-Einstellungen, die fuer dieses Ergebnis bestellt waren; None,
        wenn es keine Eingabe gab. Was MinerU tatsaechlich nahm, steht in `raw`
        (je Teilstueck unter `envelopes`)."""
        return self.row.read_settings

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
    # Eine Huelle gibt es nur zu einem Ergebnis, und die Zeile entsteht erst mit
    # ihm.

    def __getitem__(self, name: str) -> Any:
        """Ein Element des gespeicherten Ergebnisses, unveraendert.

        `huelle["markdown"]` ist `huelle.raw["markdown"]`, `huelle["middle_json"]`
        ist `huelle.raw["middle_json"]`, und so fort fuer jedes Element, das
        `assembly.assemble` erzeugt: `markdown_full`, `structured_content`,
        `model_output`, `images`, `envelopes`, `extras` — und alles, was spaeter
        dazukommt, ohne dass hier etwas nachgetragen werden muesste.

        WARUM EIN ZUGANG STATT EINES GETTERS JE FELD: Hier standen vorher
        `markdown` und die Strukturliste als eigene Eigenschaften. Beide taten
        nichts anderes, als ein Feld durchzureichen — sie waren eine
        handverlesene Auswahl aus dem, was ohnehin vollstaendig da ist, und jede
        solche Auswahl ist eine Aussage darueber, was wichtig sei. Diese Aussage
        hat hier niemand zu treffen: In `raw` steht alles gleichberechtigt
        nebeneinander, und der Zugang bildet das ab.

        Ausgenommen bleibt, was mehr tut als durchreichen: `images` wandelt aus
        Base64 zurueck nach Bytes, und das kann ein generischer Zugang nicht —
        `huelle["images"]` liefert die gespeicherten Zeichenketten.

        Raises:
            IncompleteResultError: Das Dokument ist gescheitert (`failed`,
                `input_error`). Es gibt dann nichts auszugeben — was da ist,
                steht in `raw`.
            KeyError: Das Ergebnis traegt dieses Element nicht. Ausdruecklich ein
                Fehler und kein `None`: Ein stiller Ersatzwert liesse einen
                Tippfehler wie ein leeres Feld aussehen.
        """
        return self._ergebnis()[name]

    def __contains__(self, name: str) -> bool:
        """Ob das gespeicherte Ergebnis dieses Element traegt.

        Damit `if "markdown_full" in huelle` moeglich ist, ohne den `KeyError` aus
        `__getitem__` als Steuerfluss zu benutzen.
        """
        return name in self._ergebnis()

    @property
    def images(self) -> dict[str, bytes]:
        """Die Bilder des Dokuments.

        Jedes Bild steht unter dem Verweis, der im Text auf es zeigt, etwa
        `images/teilstueck_0/page_0_image_2.jpg` (`assembly.assemble` gibt
        jedem Teilstueck seinen eigenen Namensraum).

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
        — `structured_content`, `model_output`, die Umschlaege mit Laufzeiten,
        gemeldeter MinerU-Version und `debug`-Bloecken, `extras` — oder wer
        pruefen will, ob ein Getter richtig liegt,
        sieht hier nach.

        Wirft NICHT bei gescheiterten Dokumenten: Gerade dann will man sehen, was
        ueberhaupt ankam, und dort steht auch der Fehler-Umschlag.
        """
        self._sicherstellen_dass_raw_geladen_ist()
        return self.row.raw if self.row.raw is not None else {}

    @property
    def rendered_markdown(self) -> str:
        """Das GANZE Dokument in Markdown-Form. Siehe `assembly.rendered_markdown`.

        Alles, was auf dem Dokument steht, in einer Darstellung: die
        Vollmodus-Fassung `self["markdown_full"]`, unveraendert. Nicht
        `self["markdown"]`: Die Engine filtert beim Erzeugen dieses Markdowns
        Kopf- und Fusszeilen, Seitenzahlen und Randtexte heraus, und in genau
        ihnen liegt das Aktenzeichen.

        OHNE SCHALTER, und das ist Absicht. Diese Methode hiess `rendered_text`
        und hatte zwei Parameter, `furniture` und `foreign_filter`. Beide
        stellten dieselbe Frage — „was gehoert zum Text?" —, und beide
        beantworteten sie mit einer Voreinstellung. Das ist eine redaktionelle
        Entscheidung, und sie gehoert dem, der das Ergebnis benutzt, nicht dem,
        der es aufbereitet. Wer eine Auswahl braucht, baut sie aus
        `self["markdown"]`, `self["markdown_full"]` und den Bloecken in
        `self["middle_json"]` — an der Stelle, an der er weiss, wofuer.

        Raises:
            IncompleteResultError: Die Zeile hat noch kein Ergebnis — oder es
                gibt keine ganze Vollfassung (`markdown_full` fehlt oder ist
                kein Text, etwa weil sie einem Teilstueck fehlte). Eine halbe
                Ausgabe gibt es nicht: Sie saehe vollstaendig aus. Was da ist,
                steht unveraendert in `raw`.
        """
        if self.row.endpoint_id == "elevenlabs":
            from .elevenlabs import transcript
            return transcript(self._ergebnis())
        return assembly.rendered_markdown(self._ergebnis())

    @property
    def readable_markdown(self) -> str:
        """Die Vollfassung mit HTML als Markdown; vorhandenes Markdown bleibt.

        Wie `rendered_markdown`, aber HTML-Inseln (etwa Tabellen und
        Bildbeschreibungen) werden umgesetzt. Rohantwort und Vollfassung
        bleiben unveraendert; dieselben Vollstaendigkeitspruefungen gelten.
        """
        if self.row.endpoint_id == "elevenlabs":
            return self.rendered_markdown
        return html_to_markdown(self.rendered_markdown)

    # -- Umgang -----------------------------------------------------------

    def refresh(self) -> OCRWrapper:
        """Liest die Zeile neu und verwirft alle zwischengespeicherten Ergebnisse.

        Returns:
            Dieselbe Huelle, damit man `w.refresh().rendered_markdown` schreiben kann.

        Raises:
            DocumentNotFound: Die Zeile gibt es nicht mehr.
            ConfigurationError: Die Huelle stammt aus dem `AsyncOCRClient` und
                haelt keine Verbindung; die Meldung nennt den Weg. War ihre Zeile
                beim Laden schon weg (ein `retry` lief dazwischen), stattdessen
                `DocumentNotFound`.
            DatabaseError: Die Verbindung des Clients taugt nicht mehr (etwa
                nach einem Neustart von Postgres); ebenso beim Nachladen von
                `raw`.
        """
        zeile = db.get_document(self._verbindung(), self.row.sha256)
        if zeile is None:
            raise DocumentNotFound(
                f"Die Zeile zu {self.row.sha256} gibt es nicht mehr."
            )
        self.row = zeile
        self._zwischenspeicher.clear()
        return self

    def to_dict(self, raw: bool = False) -> dict[str, Any]:
        """Alles ueber dieses Dokument als einfache Abbildung.

        Fuer die Ausgabe mit `--json`. `raw` fehlt in der Voreinstellung bewusst:
        Es waere je Dokument gross (rund 0,1 MB je Seite), und keine
        Statusausgabe braucht es.

        Args:
            raw: True nimmt die vollstaendige Antwort unter `"raw"` mit und laedt
                sie dafuer nach, wenn die Huelle sie noch nicht hat. Bei einem
                Dokument ohne Ergebnis ist sie leer (`{}`), wie bei `.raw`.
        """
        # `raw` ZUERST: Es laedt die Zeile womoeglich neu, und die Angaben sollen
        # aus derselben Zeile stammen wie die Antwort, die neben ihnen steht.
        antwort = self.raw if raw else None
        abbildung = self._angaben()
        if antwort is not None:
            abbildung["raw"] = antwort
        return abbildung

    def _angaben(self) -> dict[str, Any]:
        """Die Angaben der Zeile ohne `raw`, dazu `success`."""
        return {**self.row.angaben(), "success": self.success}

    def __repr__(self) -> str:
        return (
            f"OCRWrapper(sha256={self.row.sha256!r}, "
            f"status={self.row.status.value!r})"
        )

    # -- Das Innere -------------------------------------------------------

    _conn: psycopg.Connection[Any] | None
    _zwischenspeicher: dict[str, Any]

    def _verbindung(self) -> psycopg.Connection[Any]:
        """Die Verbindung zum Neu- und Nachlesen — oder die Ansage, dass diese
        Huelle keine hat."""
        if self._conn is None and not self.row.raw_loaded:
            # Eine Huelle des AsyncOCRClient ohne `raw`: Das Warten sah den
            # Endzustand, aber ein `retry` lief vor dem Laden dazwischen.
            raise DocumentNotFound(
                f"Die Zeile zu {self.row.sha256}, die das Warten sah, gibt es "
                f"nicht mehr: Ein `retry` nahm sie weg, oder ein neuer Lauf schrieb "
                f"schon ein neues Ergebnis. Den jetzigen Stand holt "
                f"`await client.get({self.row.sha256!r})`."
            )
        if self._conn is None:
            raise ConfigurationError(
                f"Die Huelle zu {self.row.sha256} stammt aus dem `AsyncOCRClient` "
                f"und haelt keine Verbindung; sie traegt `raw` schon. Neu lesen "
                f"heisst dort `await client.get({self.row.sha256!r})`."
            )
        return self._conn

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
        zeile = db.get_document(self._verbindung(), self.row.sha256)
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
