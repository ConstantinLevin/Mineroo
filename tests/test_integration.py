"""Die Pflicht-Szenarien aus Plan 10.2 — mit echter Datenbank, ohne echtes RunPod.

Jedes Szenario prueft ZWEI Endzustaende: den der Zeile und den des Jobs. Nur eins
von beiden zu pruefen liesse genau den Fehler durch, gegen den das ganze
Ein-Buch-Modell steht — dass die beiden Buecher verschiedene Geschichten
erzaehlen.

Die Buchstaben in Klammern verweisen auf Plan 10.2, damit man von dort hierher
und zurueckfindet.
"""

from __future__ import annotations

import pytest

pytestmark = [
    pytest.mark.db,
    pytest.mark.skip(reason="Vertrag steht, Koerper folgt in Phase C"),
]


def test_absturz_mitten_im_dokument_laeuft_von_vorn() -> None:
    """(a) Task mitten im Dokument abschiessen, Waechter legt neu vor, Dokument
    laeuft von vorn und endet `done`.

    Der Kernfall der Designannahme „Abbruch ist Normalfall".
    """


def test_nicht_wiederholbarer_fehler_im_ersten_versuch() -> None:
    """(b) Zeile sofort `failed`, juengster Job `failed` — kein Retry-Karussell."""


def test_erschoepfte_versuche_enden_failed() -> None:
    """(c) Nach drei voruebergehenden Fehlern: Zeile `failed`."""


def test_leeres_konto_pausiert_ohne_zu_zaehlen() -> None:
    """(d) Der Task wartet, der Versuchszaehler bleibt UNVERAENDERT, nach dem
    Aufladen laeuft er durch.

    Der Zaehler ist der Punkt: Zaehlte das Warten mit, waere er kein ehrliches
    Mass fuer Fehlversuche mehr.
    """


def test_abbruch_beim_herunterfahren_heilt_durch_neu_einreihen() -> None:
    """(e) Job `aborted` (endgueltig, KEIN automatischer Retry — und
    `retry_job_by_id` nimmt `aborted` nicht an, Feature-Verifikation Punkt 19),
    Zeile bleibt `new`, abgeleiteter Zustand zeigt den beendeten Job; erneutes
    Einreihen laesst das Dokument durchlaufen und es endet `done`."""


def test_doppeltes_einreihen_wird_abgefangen() -> None:
    """(f) Das zweite Einreihen ergibt „laeuft schon" statt eines zweiten Jobs."""


def test_retry_von_hand_verarbeitet_wirklich_neu() -> None:
    """(g) `retry_job_by_id` setzt den Job auf `todo`, die Ereignis-Historie
    bleibt — UND das Dokument wird wirklich neu verarbeitet: es endet `done`,
    `raw` ist ueberschrieben.

    Genau diese letzte Zusicherung faengt einen Terminal-Guard, der faelschlich
    `failed` deckt. Ohne sie saehe ein Nichts-Tun wie ein Erfolg aus.
    """


def test_wartungs_queue_verhungert_nicht() -> None:
    """(h) Alle OCR-Plaetze blockiert, der Waechter auf der Wartungs-Queue laeuft
    trotzdem.

    Die unbelegte Stelle des Entwurfs (Feature-Verifikation Punkt 1): Zwei
    Worker-Koroutinen in einem Prozess sind in der Doku weder erlaubt noch
    verboten. Schlaegt dieser Test fehl, wird auf den dokumentierten Rueckfall
    umgeplant — zwei Prozesse, einer je Queue.
    """


def test_duplikat_waehrend_der_verarbeitung_ist_ein_nichts_tun() -> None:
    """(i) Ein waehrend `doing` nachgeschobenes Duplikat wird per `lock`
    serialisiert, startet danach und endet sofort am Terminal-Guard — keine
    Doppelzahlung."""


def test_jede_wiedervorlage_erhoeht_den_versuchszaehler() -> None:
    """(j) Bestaetigt am laufenden System, was am SQL-Schema belegt ist
    (Feature-Verifikation Punkt 19): Jedes Zuruecksetzen auf `todo` — durch den
    Waechter wie durch einen Retry von Hand — erhoeht `attempts` und zehrt am
    Budget aus `max_attempts`.

    Das Ergebnis gehoert als Kommentar an die Wiederholungs-Strategie, damit es
    niemand fuer einen Fehler haelt.
    """


def test_daemon_faehrt_sauber_herunter() -> None:
    """Beide Worker enden, die Betriebsmittel werden NACH ihnen geschlossen.

    Andersherum zoege man einem noch laufenden Task den Boden weg — und dessen
    letzter Schreibvorgang ist der mit dem bezahlten Ergebnis.
    """


def test_ohne_daemon_passiert_nichts_und_das_wird_gesagt() -> None:
    """Ohne laufenden Daemon stapeln sich Jobs, und der Preflight sagt es.

    Die haeufigste Betriebs-Ueberraschung dieses Aufbaus: Alles sieht normal aus,
    nur arbeitet niemand.
    """
