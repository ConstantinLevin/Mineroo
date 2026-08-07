"""Der Job-Contract — alles ohne Netz, ueber einen vorgegebenen Transport.

Jeder dieser Tests haelt eine Regel fest, die aus einem bezahlten Fehler
entstanden ist. Sie sind billig und sie sind die einzige Stelle, an der die
Regeln nicht bloss im Kommentar stehen.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.skip(reason="Vertrag steht, Koerper folgt in Phase B")


def test_nutzlast_hat_kein_formats_feld() -> None:
    """Die gesendete Nutzlast enthaelt unter keinen Umstaenden `formats`.

    Beide alten Ad-hoc-Skripte hatten `formats: ["markdown"]` gesetzt. Genau
    daraus wurde die Abruf-Regel: alles holen, alles behalten, nie filtern —
    denn dreissig Minuten spaeter gibt es die Antwort nirgends mehr.
    """


def test_nutzlast_hat_keine_null_werte() -> None:
    """Nicht gesetzte Felder werden WEGGELASSEN, nicht als null gesendet.

    Der Worker unterscheidet zwischen „nicht gesetzt" und „ausdruecklich null",
    und null fuehrt zu Verhalten, das wie ein Modellfehler aussieht.
    """


def test_seitenbereich_wird_nur_gesendet_wenn_gesetzt() -> None:
    """Ohne Angabe stehen `start_page` und `end_page` nicht in der Nutzlast —
    insbesondere wird keine `-1` gesendet."""


def test_schluessel_steht_nur_im_header() -> None:
    """Der API-Schluessel taucht in keiner URL auf, in keinem Nutzlast-Feld und
    in keiner Ausnahme-Meldung."""


def test_es_gibt_kein_runsync() -> None:
    """Der Zugang bietet keine Methode, die `/runsync` aufruft.

    Nach einer Ruhephase braucht der Endpoint rund achteinhalb Minuten zum
    Kaltstart, `/runsync` wartet sechzig Sekunden — man bekaeme eine
    Zeitueberschreitung auf einen Job, der laeuft und gleich bezahlt ist. Eine
    Methode, die es nicht gibt, kann niemand versehentlich benutzen.
    """


def test_404_beim_abfragen_ist_endgueltig() -> None:
    """`status` wirft ResultLostError bei HTTP 404 — kein Weiterfragen, kein
    Retry."""


def test_402_ergibt_kontostands_ausnahme() -> None:
    """HTTP 402 beim Absenden ergibt InsufficientBalanceError, nicht einen
    gewoehnlichen Fehler."""


def test_401_und_403_sind_konfigurationsfehler() -> None:
    """Ein untauglicher Schluessel ergibt ConfigurationError — Wiederholen
    macht ihn nicht gueltig."""


def test_5xx_ist_wiederholbar() -> None:
    """HTTP 5xx ergibt TransientRunPodError."""


def test_abwarten_liefert_auch_gescheiterte_antworten() -> None:
    """`await_result` gibt eine FAILED-Antwort ZURUECK, statt zu werfen.

    Wuerde hier geworfen, waere die bezahlte Fehlerantwort verloren — dabei ist
    gerade sie das, was man spaeter ansehen will.
    """


def test_abwarten_endet_bei_terminalem_zustand() -> None:
    """Jeder Zustand aus TERMINAL_STATUSES beendet die Schleife."""


def test_running_gilt_als_laufend() -> None:
    """`RUNNING` beendet die Schleife NICHT.

    Der Zustand wurde im Betrieb beobachtet, obwohl die Dokumentation nur
    `IN_PROGRESS` nennt. Ein unbekannter Zustand wuerde als Fehler behandelt und
    ein laufender Job abgebrochen.
    """


def test_zeitueberschreitung_ist_wiederholbar() -> None:
    """Ueberschreitet die Abfrageschleife `job_timeout_seconds`, kommt
    TransientRunPodError."""


def test_abbruch_wird_durchgereicht() -> None:
    """CancelledError in der Warteschleife wird nicht gefangen und nicht
    verwandelt — sonst liefe der Task beim Herunterfahren weiter."""


def test_worker_liste_ist_einzeln() -> None:
    """`workers` liefert Eintraege je Worker, nicht die zusammengefassten
    Zaehler des Endpoints (die luegen)."""


def test_leere_worker_liste_ist_kein_fehler() -> None:
    """Kein laufender Worker ist der Normalzustand bei einem Endpoint, der auf
    null herunterskaliert."""


def test_kontostand_fehlschlag_ist_nicht_leeres_konto() -> None:
    """Eine gescheiterte Kontostands-Abfrage wirft und liefert nicht die Null.

    Die Verwechslung waere in beide Richtungen teuer: die Arbeit wegen eines
    Netzwackelns anhalten, oder bei leerem Konto weiterlaufen.
    """


def test_kein_kommando_veraendert_bezahlte_ressourcen() -> None:
    """Die Klasse hat keine Methode, die `workersMax` setzt, ein Template
    aendert oder einen Endpoint anlegt — nicht abgeschaltet, sondern nicht
    vorhanden."""
