"""Der Live-Smoke — KOSTET GELD und läuft nur mit ausdrücklicher Zustimmung.

Alle Tests hier tragen die Kennzeichnung `live` und werden von einem gewöhnlichen
`pytest`-Lauf nicht ausgeführt. Sie stehen trotzdem hier und nicht nur im Plan,
weil die Regel, um die es geht, im Projekt schon einmal Geld gekostet hat: erst
an einem einzigen Stück testen, den Output mit eigenen Augen lesen, eine
Rechenkette nachrechnen — und erst dann den Stapel fahren.

Der Marker allein wäre eine erfüllt aussehende Vorbereitung. Erst die Namen
machen daraus eine Liste, die man abhaken kann.
"""

from __future__ import annotations

import pytest

pytestmark = [
    pytest.mark.live,
    pytest.mark.skip(reason="Kostet Geld; laeuft nur mit ausdruecklicher Zustimmung (Phase D)"),
]


def test_kleines_pdf_ganz() -> None:
    """Ein kleines PDF ohne Schnitt. Endet `done`, Text vorhanden."""


def test_pdf_mit_erzwungenem_schnitt() -> None:
    """Schwelle voruebergehend klein setzen, damit wirklich geschnitten wird.

    Der eigentliche Zweck des ganzen Live-Smokes: Erst hier laeuft der
    Zusammensetz-Weg mit mehr als einem Teilstueck gegen echtes Material. Zu
    pruefen sind ausdruecklich die Seitenzahlen im Ergebnis — sie muessen
    fortlaufend die des ORIGINALS sein, nicht je Teilstueck wieder bei null
    beginnen.
    """


def test_bild() -> None:
    """Ein Bild als Eingabe: ein Teilstueck, kein Sonderfall im Ablauf."""


def test_ordner_waechst_live() -> None:
    """Ordner durchlaufen, eine Datei dazulegen, erneut durchlaufen — es laeuft
    nur das Fehlende."""


def test_tarball_wird_ausgepackt() -> None:
    """Die erste echte `tarball_b64`-Antwort ueberhaupt.

    Bis hierhin gibt es dafuer KEIN Offline-Testmaterial: Die sechs konservierten
    Antworten sind `inline`. Dieser Lauf schliesst die Luecke — und seine Antwort
    gehoert danach als Fixture konserviert, damit `test_assembly` sie offline
    benutzen kann.
    """


def test_leeres_ergebnis_am_echten_verhalten() -> None:
    """Eine leere Seite durchschicken und feststellen, WIE die Schnittstelle
    darauf antwortet.

    Das Kriterium fuer `empty` ist ausdruecklich offen und wird hier festgestellt,
    nicht vorab erfunden: „das weiß ich ja nicht das muss ja rausgefudnen werden
    dann vorm bauen. es wird ja nicht ohne interaktion gebaut es ist ja consigment
    gegen die api". Das Ergebnis gehoert danach in `assembly.is_empty`.
    """


def test_stream_verhaelt_sich_wie_erwartet() -> None:
    """OFFEN, und nur hier pruefbar: Wie verhaelt sich `/stream` an unserem
    Worker — kommen die Phasen-Ereignisse, und antwortet der Server sofort?

    Am Hersteller-SDK ist belegt, dass dort im Sekundentakt abgefragt wird, der
    Server also nicht offen haelt. Ob das an unserem Worker ebenso ist, ist eine
    Messung. Faellt sie anders aus, ist der Abfragetakt neu zu bewerten.
    """


def test_ergebnis_mit_eigenen_augen() -> None:
    """Kein automatischer Test: Der Text wird gelesen und eine Rechenkette des
    Dokuments nachgerechnet.

    Steht als Name in der Liste, damit der Schritt nicht ausfaellt. Ein Modell,
    das Zahlen erfindet, faellt keinem Gleichheitsvergleich auf.
    """


def test_image_tag_ist_das_erwartete() -> None:
    """Zugleich die Verifikation des neuen Worker-Images: Was in
    `image_tag_at_submit` landet, ist die Fassung, die laufen soll."""
