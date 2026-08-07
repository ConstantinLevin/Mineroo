"""Typerkennung an den Magic Bytes."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.skip(reason="Vertrag steht, Koerper folgt in Phase B")


def test_jeder_unterstuetzte_typ_wird_erkannt() -> None:
    """Je ein Kopf-Fixture fuer PDF, PNG, JPEG, GIF, BMP, TIFF, WebP, DOCX,
    PPTX, XLSX ergibt den richtigen Typ."""


def test_endung_wird_ignoriert() -> None:
    """Eine JPEG-Datei mit der Endung .pdf wird als JPEG erkannt.

    Der Fall, gegen den die ganze Magic-Byte-Erkennung steht: In einem
    gewachsenen Bestand luegen Endungen, und wer ihnen glaubt, bezahlt fuer den
    Fehlschlag.
    """


def test_zip_ohne_office_struktur_wird_abgewiesen() -> None:
    """Ein gewoehnliches ZIP hat dieselben vier Magic Bytes wie DOCX und muss
    trotzdem UnsupportedTypeError ergeben — geprueft wird die Struktur im
    Archiv (word/, xl/, ppt/)."""


def test_leere_datei_wird_abgewiesen() -> None:
    """Null Bytes ergeben UnsupportedTypeError, keinen Absturz."""


def test_zu_kurze_datei_wird_abgewiesen() -> None:
    """Weniger Bytes als die Magic Bytes verlangen ergibt UnsupportedTypeError."""


def test_sniff_bytes_braucht_die_ganzen_bytes_beim_zip() -> None:
    """Ein abgeschnittener DOCX-Puffer ergibt UnsupportedTypeError statt eines
    falschen Treffers — das Archivverzeichnis steht am ENDE der Datei."""


def test_nur_pdf_ist_zerlegbar() -> None:
    """`is_pdf` ist genau fuer PDF wahr. Alles andere geht ungeteilt hinaus
    oder gar nicht."""


def test_leserechte_werden_nicht_als_typfehler_ausgegeben() -> None:
    """Eine nicht lesbare Datei ergibt OSError, nicht UnsupportedTypeError.

    Die Verwechslung wuerde die Fehlersuche in die falsche Richtung schicken.
    """
