"""Einstellungen — und die einzige Zusage des Pakets, deren Bruch nicht rückholbar ist."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.skip(reason="Vertrag steht, Koerper folgt in Phase B")


def test_repr_zeigt_den_schluessel_nicht() -> None:
    """`repr(settings)` enthaelt den Schluessel nicht und enthaelt `***`.

    Von allen Zusagen des Pakets ist das die eine, deren Bruch nicht rueckholbar
    ist: Ein Schluessel, der einmal in einem Log steht, steht dort.
    """


def test_str_zeigt_den_schluessel_nicht() -> None:
    """Auch ueber den Rueckfall `str()` kommt er nicht heraus."""


def test_asdict_zeigt_ihn_sehr_wohl() -> None:
    """`dataclasses.asdict` UMGEHT die Maskierung — der Test haelt die Grenze der
    Zusage fest, damit sich niemand zu viel darauf verlaesst.

    Er prueft damit ausdruecklich kein Wunschverhalten, sondern das tatsaechliche:
    Wer serialisiert, muss das Feld selbst entfernen.
    """


def test_dataclass_ueberschreibt_repr_nicht() -> None:
    """Die Maskierung ueberlebt den Dataclass-Dekorator."""


def test_ohne_schluessel_wirft_load() -> None:
    """Kein Schluessel uebergeben und keiner in `~/.runpod/config.toml`:
    `ConfigurationError`, nicht eine Einstellung mit leerem Feld."""


def test_load_legt_nichts_an() -> None:
    """`load` erzeugt das Spool-Verzeichnis nicht und beruehrt die Platte nur
    lesend — eine Einstellung zu bilden muss auch offline gehen."""


def test_load_fragt_weder_datenbank_noch_endpoint() -> None:
    """Keine Verbindung, kein HTTP. Pruefen ist Sache des Preflights."""


def test_uebergabe_schlaegt_voreinstellung() -> None:
    """Was uebergeben wurde, gilt; sonst die Voreinstellung. Zwei Quellen, keine
    dritte — insbesondere keine Umgebungsvariablen."""


def test_backend_und_grenzen_kommen_an() -> None:
    """`backend`, `min_balance_start_usd` und `ocr_concurrency` landen wirklich in
    der Einstellung.

    Der Test gegen den stillen Nichts-Tuer: Ein oeffentlicher Parameter ohne Weg
    in die Einstellung meldet keinen Fehler und wirkt trotzdem nicht.
    """


def test_einstellung_ist_unveraenderlich() -> None:
    """Ein Schreibversuch an einem Feld wirft — sie darf waehrend eines Laufs
    nicht unter der Hand kippen."""
