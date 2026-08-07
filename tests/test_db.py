"""Die Tabelle: Schreibregeln, Wecksignal, abgeleiteter Zustand.

Die Tests zum abgeleiteten Zustand sind REIN und brauchen keine Datenbank — sie
bekommen Zeile und Job-Liste hereingereicht. Das ist der Grund, warum
`derive_status` eine reine Funktion ist: Die Regel ist der heikle Teil, die
Abfrage ist Handwerk.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.skip(reason="Vertrag steht, Koerper folgt in Phase B")


# --- rein, ohne Datenbank -------------------------------------------------


def test_terminale_zeile_schlaegt_jeden_job() -> None:
    """Ist die Zeile endgueltig, ist das der Zustand — auch wenn danach noch ein
    Job auftaucht. Ein fertiges Dokument bleibt fertig."""


def test_ohne_job_ist_das_dokument_nur_registriert() -> None:
    """Zeile `new`, keine Jobs: Phase REGISTERED."""


def test_juengster_job_zaehlt_nicht_der_aelteste() -> None:
    """Bei mehreren Jobs entscheidet der mit der hoechsten Kennung.

    Job-Zeilen werden nie geloescht, also gibt es ueber die Zeit mehrere je
    Dokument — der aelteste sagt nichts ueber jetzt.
    """


def test_wartender_job_ergibt_waiting() -> None:
    """Zeile `new`, juengster Job `todo`: Phase WAITING."""


def test_laufender_job_ergibt_running() -> None:
    """Zeile `new`, juengster Job `doing`: Phase RUNNING."""


def test_beendeter_job_ohne_ergebnis_wird_erkannt() -> None:
    """Zeile `new`, juengster Job endgueltig: Phase ENDED_WITHOUT_RESULT.

    Der Fall nach hartem Abschuss oder Herunterfahren. Kein Datenfehler,
    sondern genau der Zustand, den das Ein-Buch-Modell erwartet — er heilt beim
    naechsten Einreihen von selbst.
    """


def test_versuchszaehler_wird_durchgereicht() -> None:
    """`attempts` des juengsten Jobs steht im abgeleiteten Zustand."""


# --- mit Datenbank --------------------------------------------------------


@pytest.mark.db
def test_setup_ist_mehrfach_aufrufbar() -> None:
    """Zweimal `setup` auf derselben Datenbank aendert nichts und wirft nicht."""


@pytest.mark.db
def test_setup_meldet_abweichendes_schema() -> None:
    """Passt die vorhandene Tabelle nicht zur Zeilenklasse, wirft `setup` mit
    Nennung der abweichenden Spalten und der Abhilfe — und veraendert NICHTS."""


@pytest.mark.db
def test_zweites_einfuegen_ist_ein_nichts_tun() -> None:
    """Derselbe Hash ein zweites Mal ergibt False und laesst die bestehende
    Zeile voellig unberuehrt — auch `updated_at`.

    Darauf beruht das Ordner-Szenario: „lässt nochmal durchlaufen weil eh nur
    läuft was noch fehlt".
    """


@pytest.mark.db
def test_terminal_schreiben_setzt_alles_in_einer_transaktion() -> None:
    """Nach dem Aufruf sind Zustand, raw, error, notes und updated_at gesetzt —
    es gibt keinen Zwischenzustand mit raw und Status `new`."""


@pytest.mark.db
def test_wecksignal_kommt_bei_jedem_endzustand() -> None:
    """Auch `failed` und `input_error` loesen das Signal aus.

    Weckte es nur bei Erfolg, liefe jeder Warter auf ein gescheitertes Dokument
    bis zum Zeitablauf und wuesste am Ende nicht, ob es scheiterte oder dauert.
    """


@pytest.mark.db
def test_fertiges_dokument_wird_nie_ueberschrieben() -> None:
    """Auf `done`, `empty` oder `input_error` wirft ein abweichender
    Schreibvorgang TerminalOverwriteError."""


@pytest.mark.db
def test_gescheitertes_dokument_darf_ueberschrieben_werden() -> None:
    """Auf `failed` schreibt derselbe Aufruf durch — das ist der Wiederanlauf
    (Plan 4.2) und der Grund, warum der Terminal-Guard `failed` nicht deckt."""


@pytest.mark.db
def test_gleicher_aufruf_zweimal_ist_unschaedlich() -> None:
    """Zweimal derselbe Endzustand wirft nicht."""


@pytest.mark.db
def test_notiz_haengt_an_statt_zu_ersetzen() -> None:
    """`append_note_async` verlaengert die Liste und beruehrt sonst nichts."""


@pytest.mark.db
def test_warten_findet_bereits_fertiges_dokument_sofort() -> None:
    """Ist das Dokument schon endgueltig, kehrt `wait_for_terminal` sofort
    zurueck, statt auf ein Signal zu warten, das schon vorbei ist."""


@pytest.mark.db
def test_warten_auf_unbekannten_hash_wirft_sofort() -> None:
    """DocumentNotFound kommt sofort, nicht nach Ablauf der Wartezeit."""


@pytest.mark.db
def test_warten_faellt_auf_abfragen_zurueck() -> None:
    """Auch ohne Signal — etwa bei einem Schreibvorgang von aussen — endet das
    Warten durch die Rueckfallabfrage."""


@pytest.mark.db
def test_liste_laedt_kein_raw() -> None:
    """`list_documents` liefert Zeilen mit `raw is None`, obwohl in der
    Datenbank etwas steht. Bei dreissigtausend Dokumenten waeren es sonst
    zweistellige Gigabyte."""


@pytest.mark.db
def test_zaehlung_kennt_jeden_zustand() -> None:
    """Auch Zustaende ohne Zeilen kommen mit Null vor — ein fehlender
    Schluessel wuerde die Statusausgabe genau dann sprengen, wenn alles gut
    lief."""
