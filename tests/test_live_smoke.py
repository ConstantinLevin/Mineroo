"""Der Live-Smoke — KOSTET GELD und läuft nur mit ausdrücklicher Zustimmung.

Alle Tests hier tragen die Kennzeichnung `live` und werden von einem gewöhnlichen
`pytest`-Lauf nicht ausgeführt. Sie stehen trotzdem hier und nicht nur im Plan,
weil die Regel, um die es geht, im Projekt schon einmal Geld gekostet hat: erst
an einem einzigen Stück testen, den Output mit eigenen Augen lesen, eine
Rechenkette nachrechnen — und erst dann den Stapel fahren.

Der Marker allein wäre eine erfüllt aussehende Vorbereitung. Erst die Namen
machen daraus eine Liste, die man abhaken kann.

---

STAND DER LISTE: AM 08./09.08.2026 GEFAHREN, gegen den echten Endpoint
`l79lebhinxaqjz` (Image-Tag `17d4955b2`, Modell MinerU2.5-Pro-2605-1.2B,
`middle._version_name` 3.4.4). Gesamtkosten der Läufe: rund 0,25 USD.

Was dabei herauskam, steht bei den einzelnen Punkten. Die WICHTIGSTE Erkenntnis
steht aber hier, weil sie über die Liste hinausgeht: **Die Läufe haben mehr
Annahmen umgeworfen als bestätigt.** Kein Span-PDF, zwei verschiedene
`content_list`-Formate statt eines Versions-Fallbacks, ein Doppeltreffer auf dem
Zuordnungsmuster als Normalfall statt als Sonderfall, `/stream` mit umgekehrtem
Verhalten, und zwei Fehler in einer Reparatur desselben Tages, die erst eine
echte leere Antwort sichtbar machen konnte.

Das ist die Begründung dafür, warum diese Liste existiert — und zugleich der
Beleg, dass sie zu spät gefahren wurde. Die Vorgabe lautete: „es wird ja nicht
ohne interaktion gebaut es ist ja consigment gegen die api" (Plan 2.4).

WAS OFFLINE ABGESICHERT IST, seit die Antworten dieser Läufe als Fixtures unter
`artifacts/2026-08-08-erster-tarball-lauf/` liegen: das `empty`-Kriterium samt
dem Tarball-Auspacker an einer leeren Antwort und die Form einer echten
Fehlerantwort. Diese zwei brauchen kein Geld mehr.

Der Tarball-Auspacker an einer nicht leeren Antwort und der Seitenversatz ueber
drei echte Teilstuecke waren ebenso abgesichert, bis Mineroo#2 die Aufnahmen dazu
entfernte, weil sie personenbezogene Daten trugen. Ihre Tests laufen ausgesetzt
(`pytest -rs`), bis Ersatzaufnahmen ohne Personenbezug unter denselben Namen
liegen; bis dahin laufen diese Wege offline nur gegen erfundene Antworten.
"""

from __future__ import annotations

import pytest

pytestmark = [
    pytest.mark.live,
    pytest.mark.skip(reason="Kostet Geld; laeuft nur mit ausdruecklicher Zustimmung (Phase D)"),
]


def test_kleines_pdf_ganz() -> None:
    """Ein kleines PDF ohne Schnitt. Endet `done`, Text vorhanden.

    GEFAHREN am 08.08.2026 durch das Tool (`mineru-ocr ocr`, `wait`). Ergebnis
    `done`, Text korrekt einschliesslich deutscher Umlaute, Tabellenstruktur,
    Absender und Datum. Wartezeit 133 s (Kaltstart), Verarbeitung 2,76 s.
    """


def test_pdf_mit_erzwungenem_schnitt() -> None:
    """Schwelle voruebergehend klein setzen, damit wirklich geschnitten wird.

    Der eigentliche Zweck des ganzen Live-Smokes: Erst hier laeuft der
    Zusammensetz-Weg mit mehr als einem Teilstueck gegen echtes Material. Zu
    pruefen sind ausdruecklich die Seitenzahlen im Ergebnis — sie muessen
    fortlaufend die des ORIGINALS sein, nicht je Teilstueck wieder bei null
    beginnen.

    GEFAHREN am 08.08.2026: 12 Seiten, `max_pages_per_chunk=5`, drei Teilstuecke
    (0-4, 5-9, 10-11). Ergebnis 85 Bloecke, `page_idx` LUECKENLOS 0 bis 11.

    Und die Rohdaten dazu am 09.08. als Fixture gesichert. Sie belegten die
    Annahme, auf der die ganze Rechnung ruht und die vorher nur am Quelltext
    behauptet war: Jedes Teilstueck traegt `page_idx` LOKAL ab 0 — gemessen
    0-4, 0-4, 0-1. Der Weg war damit OFFLINE abgesichert
    (`test_assembly.py::test_seitenversatz_gegen_ECHTE_mehrteilige_antworten`).
    Seit Mineroo#2 die Aufnahmen entfernt hat, weil sie personenbezogene Daten
    trugen, laeuft dieser Test ausgesetzt, bis Ersatzaufnahmen ohne
    Personenbezug vorliegen.
    """


def test_bild() -> None:
    """Ein Bild als Eingabe: ein Teilstueck, kein Sonderfall im Ablauf.

    GEFAHREN am 08.08.2026 mit einer aus einem echten Dokument gerasterten PNG
    (1190x1684). `input_format: image` laut `debug`, 21 Bloecke, deutscher Text
    mit Umlauten und Anfuehrungszeichen — und darin das Aktenzeichen
    `AZ 00123-45 XYZ / ABC`, also genau die Kennungsklasse, um die es dem
    Vorhaben geht.
    """


def test_ordner_waechst_live() -> None:
    """Ordner durchlaufen, eine Datei dazulegen, erneut durchlaufen — es laeuft
    nur das Fehlende.

    GEFAHREN am 08.08.2026, und mit einer Verschaerfung, die mehr prueft als der
    Name sagt: Der Ordner enthielt KOPIEN bereits verarbeiteter Dateien unter
    NEUEN Pfaden. Ergebnis „2 Dokumente, davon 2 bereits fertig; 0 eingereiht" —
    die Adopt-Regel greift ueber den Inhalt, nicht ueber den Pfad, und es wurde
    nichts neu bezahlt. Nach dem Dazulegen: „3 Dokumente, davon 2 bereits
    fertig; 1 eingereiht".

    Der zweite Pfad hinterlaesst heute bewusst keine Spur (Mineroo#23): Die
    Datenbank kennt keine Pfade, und derselbe Inhalt ist dasselbe Dokument.
    """


def test_tarball_wird_ausgepackt() -> None:
    """Die erste echte `tarball_b64`-Antwort ueberhaupt.

    Bis hierhin gibt es dafuer KEIN Offline-Testmaterial: Die sechs konservierten
    Antworten sind `inline`. Dieser Lauf schliesst die Luecke — und seine Antwort
    gehoert danach als Fixture konserviert, damit `test_assembly` sie offline
    benutzen kann.

    GEFAHREN am 08.08.2026, Antwort konserviert als
    `artifacts/2026-08-08-erster-tarball-lauf/gute_nacht.tarball.response.json`
    (vor dem ersten Push entfernt, Mineroo#2; siehe `TARBALL_FIXTURE`).

    DREI ANNAHMEN SIND DARAN GESCHEITERT:

        Das Archiv traegt `doc_content_list.json` UND
        `doc_content_list_v2.json` — beide treffen dasselbe Zuordnungsmuster.
        Der Doppeltreffer galt als hypothetisch und kam beim allerersten Lauf.

        Die beiden sind NICHT zwei Generationen desselben Formats, sondern zwei
        verschiedene Formate (flach mit `page_idx` gegen nach Seiten
        geschachtelt mit `title`/`paragraph`/`table`).

        Es gibt KEIN span-PDF und KEIN model.json. Vollstaendiger
        Archiv-Inhalt: `doc.md`, `doc_content_list.json`,
        `doc_content_list_v2.json`, `doc_layout.pdf`, `doc_middle.json`,
        `images/<inhaltshash>.jpg`.
    """


def test_leeres_ergebnis_am_echten_verhalten() -> None:
    """Eine leere Seite durchschicken und feststellen, WIE die Schnittstelle
    darauf antwortet.

    Das Kriterium fuer `empty` ist ausdruecklich offen und wird hier festgestellt,
    nicht vorab erfunden: „das weiß ich ja nicht das muss ja rausgefudnen werden
    dann vorm bauen. es wird ja nicht ohne interaktion gebaut es ist ja consigment
    gegen die api". Das Ergebnis gehoert danach in `assembly.is_empty`.

    GEFAHREN am 08.08.2026 mit einer leeren A4-Seite. Die Schnittstelle antwortet
    mit `COMPLETED` und `ok: true` — eine leere Seite ist fuer sie ein ERFOLG.
    Das Archiv traegt `doc.md` mit 0 Bytes, `doc_content_list.json` mit `[]`,
    leere Blocklisten in `middle` — und trotzdem ein `doc_layout.pdf` von 462
    Bytes. Ein Layout-PDF ist also KEIN Hinweis auf Inhalt.

    Die vorher gewaehlte Arbeitsfassung trifft damit zu und ist jetzt belegt.

    DIESE EINE ANTWORT HAT ZWEI FEHLER ANS LICHT GEBRACHT, die einander
    verdeckten und die kein Review haette finden koennen: eine Belegt-Pruefung am
    Wahrheitswert (eine gueltige leere Liste galt als „Platz frei", die
    v2-Fassung ueberschrieb die flache) und eine Vollstaendigkeitspruefung, die
    eine echt leere Seite fuer ein kaputtes Archiv gehalten haette — also
    `failed` statt `empty`, mit Wiedervorlage und erneutem Bezahlen.

    Antwort konserviert als `leere_seite.tarball.response.json`; der Weg ist
    damit OFFLINE abgesichert.
    """


def test_stream_verhaelt_sich_wie_erwartet() -> None:
    """OFFEN, und nur hier pruefbar: Wie verhaelt sich `/stream` an unserem
    Worker — kommen die Phasen-Ereignisse, und antwortet der Server sofort?

    Am Hersteller-SDK ist belegt, dass dort im Sekundentakt abgefragt wird, der
    Server also nicht offen haelt. Ob das an unserem Worker ebenso ist, ist eine
    Messung. Faellt sie anders aus, ist der Abfragetakt neu zu bewerten.

    GEMESSEN am 08.08.2026 — SIE FAELLT ANDERS AUS, in beide Richtungen:

        Der Server HAELT die Verbindung offen, rund 10 Sekunden. Der erste
        Aufruf brauchte 10,04 s und kam mit `IN_PROGRESS` zurueck, der zweite
        0,03 s mit `COMPLETED`.

        Der Strom ist LEER. Unser Worker sendet keine Phasen-Ereignisse — weder
        waehrend `IN_PROGRESS` noch bei `COMPLETED`. `/stream` traegt gegenueber
        `/status` also keine zusaetzliche Information.

    FOLGE FUER DEN ABFRAGETAKT: `/stream` koennte die Abfrageschleife als
    Long-Poll ersetzen — gleiche Latenz, ein Request statt einer je Intervall.
    Der Gewinn ist gering, solange `poll_interval_seconds` bei 10 s liegt; er
    waechst mit der Zahl gleichzeitiger Dokumente. NICHT umgesetzt, weil es eine
    Optimierung ohne belegten Bedarf waere. Nicht gemessen: ob der Server bei
    einem KALTEN Start ebenfalls nur 10 s haelt.
    """


def test_ergebnis_mit_eigenen_augen() -> None:
    """Kein automatischer Test: Der Text wird gelesen und eine Rechenkette des
    Dokuments nachgerechnet.

    Steht als Name in der Liste, damit der Schritt nicht ausfaellt. Ein Modell,
    das Zahlen erfindet, faellt keinem Gleichheitsvergleich auf.

    GEFAHREN am 08.08.2026 an einem 12-seitigen Dokument mit deutscher
    Rechtsprosa und zwei Organigramm-Bildern.

    UND GENAU DIESER SCHRITT HAT GEFUNDEN, was kein Vergleich gefunden haette:
    Fuer die zwei Organigramm-Bilder hat das Modell Mermaid-Flowcharts ERZEUGT
    und in dasselbe Feld gelegt wie transkribierten Text. Eines fuehrte einen
    Knoten zweimal mit demselben Anteil — eine Struktur, die ihre eigene Logik
    nicht erfuellt. Bead jurgen-092; die Entscheidung, ob erzeugte Deutungen in
    einen Index duerfen, ist offen.
    """


def test_image_tag_ist_das_erwartete() -> None:
    """Zugleich die Verifikation des neuen Worker-Images: Was in
    `image_tag_at_submit` landet, ist die Fassung, die laufen soll.

    GEFAHREN am 08.08.2026. Auf allen Zeilen steht
    `registry.runpod.net/constantinhvber-mineru-runpod-main-dockerfile:17d4955b2`
    — also v1.8.0. Der Preflight meldet das als `warnung`, und der v1.8.4-Rebuild
    ist damit weiterhin offen; die Provenienz selbst funktioniert.
    """


def test_echte_fehlerantwort() -> None:
    """Was der Endpoint bei einem echten Fehlschlag zurueckgibt.

    Stand nicht auf der urspruenglichen Liste und gehoert darauf: Der Fehlerpfad
    des Executors bewertet eine Antwort, die niemand je gesehen hatte.

    GEFAHREN am 09.08.2026, erzwungen mit einem gueltigen PDF und einem
    ungueltigen Backend-Namen — die lokalen Pruefungen haetten daran nichts zu
    beanstanden, der Fehler entsteht rein auf der Gegenseite.

    Sie traegt MEHR als angenommen: `error` auf oberster Ebene, ein volles
    `output` mit `ok: false`, den `debug`-Block (GPU, Modellverzeichnis,
    Phasenzeiten) und einen kompletten `traceback` aus dem Worker. Eine
    Fehlerantwort ist also NICHT leer.

    Dieselbe Antwort hat die Liste der gueltigen Backend-Namen geliefert — der
    Worker nennt sie selbst, wenn man ihn falsch fragt. Sie steht jetzt in
    `config.GUELTIGE_BACKENDS` und wird lokal geprueft; vorher war ein Tippfehler
    nur zu erfahren, indem man dafuer bezahlte (177 s Kaltstart je Dokument).

    Antwort konserviert als `fehlerantwort.response.json`; der Weg ist damit
    OFFLINE abgesichert.
    """


def test_402_verhalten_bei_leerem_konto() -> None:
    """NICHT GEFAHREN und auf absehbare Zeit nicht fahrbar.

    Die Balance-Wache (Regel 8) ist gebaut und offline geprueft — Pause statt
    Wurf beim Absenden, Pause ohne erneutes Absenden beim Abholen, und ein
    benannter Widerspruch, wenn der Kontostand „gedeckt" sagt und der Endpoint
    trotzdem ablehnt.

    Was NICHT geprueft ist: wie der Endpoint sich WIRKLICH verhaelt, wenn das
    Konto leerlaeuft. Ob er HTTP 402 schickt, ob er es beim Absenden oder erst
    beim Abfragen tut, welche Meldung dabei steht.

    Das laesst sich nicht pruefen, ohne das Konto tatsaechlich leerlaufen zu
    lassen — der Test waere teurer als der Fehler, den er verhindert. Er steht
    hier, damit die Luecke benannt ist statt uebersehen: Der 402-Pfad ist gegen
    eine Vorstellung gebaut, und das ist genau die Lage, aus der die uebrigen
    Befunde dieser Liste entstanden sind.
    """
