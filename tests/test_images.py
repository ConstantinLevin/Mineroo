"""Hat eine Bilddatei mehr als ein Bild? Reine Rechnung auf Bytes, ohne Datenbank."""

from __future__ import annotations

import io
from typing import Any

import pytest
from PIL import (
    GifImagePlugin,
    Image,
    PngImagePlugin,
    TiffImagePlugin,
    WebPImagePlugin,
)

from mineru_ocr.images import has_several_images
from mineru_ocr.mime import DocumentType


def _bild(dateiformat: str, anzahl: int) -> bytes:
    """Eine Bilddatei mit `anzahl` Bildern (Frames), in dem Format."""
    bilder = [Image.new("RGB", (8, 8), farbe) for farbe in ("red", "blue", "green")]
    puffer = io.BytesIO()
    # JPEG und BMP kennen kein `save_all`; sie tragen auch nur ein Bild.
    folge: dict[str, Any] = (
        {"save_all": True, "append_images": bilder[1:anzahl]}
        if dateiformat not in ("JPEG", "BMP")
        else {}
    )
    bilder[0].save(
        puffer,
        format=dateiformat,
        **folge,
        **({"lossless": True} if dateiformat == "WEBP" else {}),
    )
    return puffer.getvalue()


@pytest.mark.parametrize("dateiformat", ["TIFF", "GIF", "WEBP", "PNG"])
def test_mehrere_bilder_werden_erkannt(dateiformat: str) -> None:
    assert has_several_images(_bild(dateiformat, 3)) is True


@pytest.mark.parametrize("dateiformat", ["TIFF", "GIF", "WEBP", "PNG", "JPEG", "BMP"])
def test_ein_bild_ist_nicht_mehrbildig(dateiformat: str) -> None:
    assert has_several_images(_bild(dateiformat, 1)) is False


def test_zwei_bilder_genuegen() -> None:
    assert has_several_images(_bild("TIFF", 2)) is True


@pytest.mark.parametrize(
    "inhalt",
    [
        b"",
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"abgeschnitten",
        b"II*\x00\x08\x00\x00\x00" + b"\x00" * 4,
        b"kein Bild",
    ],
)
def test_was_pillow_nicht_lesen_kann_ist_nicht_bewiesen_mehrbildig(
    inhalt: bytes,
) -> None:
    """Gezaehlt wird, was sich zaehlen laesst. Ob die Datei lesbar ist, entscheidet
    die Engine; die Pruefung macht sie nicht auch noch zum Fehler."""
    assert has_several_images(inhalt) is False


def test_gefragt_wird_per_sprung_zum_zweiten_bild_nicht_per_durchzaehlen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Die Frage ist „mehr als eines?", nicht „wie viele?": `n_frames` laeuft bei
    TIFF durch jede Seite, und bei Zehntausenden Seiten hielte das die
    Ereignisschleife des Daemons sekundenlang an, mit allen anderen Dokumenten."""

    def _nicht_durchzaehlen(self: object) -> int:
        pytest.fail("n_frames zaehlt jede Seite durch")

    monkeypatch.setattr(
        TiffImagePlugin.TiffImageFile, "n_frames", property(_nicht_durchzaehlen)
    )

    assert has_several_images(_bild("TIFF", 3)) is True


@pytest.mark.parametrize(
    ("dateiformat", "klasse"),
    [
        ("GIF", GifImagePlugin.GifImageFile),
        ("PNG", PngImagePlugin.PngImageFile),
        ("WEBP", WebPImagePlugin.WebPImageFile),
    ],
)
def test_bei_gif_png_und_webp_wird_kein_bild_dekodiert(
    monkeypatch: pytest.MonkeyPatch, dateiformat: str, klasse: type
) -> None:
    """Bei diesen Formaten dekodiert der Sprung zum zweiten Bild das erste: Ein
    kleines GIF mit 8192 x 8192 Pixeln kostete so rund 120 ms und hunderte MB. Die
    Frage beantwortet dort `is_animated` aus den Kopfdaten."""

    echtes_seek = klasse.seek  # type: ignore[attr-defined]

    def _nicht_springen(self: Any, frame: int) -> None:
        # Der Rueckweg zu Bild 0 ist harmlos; der Sprung nach vorn dekodiert.
        if frame != 0:
            pytest.fail("seek dekodiert das erste Bild")
        echtes_seek(self, frame)

    monkeypatch.setattr(klasse, "seek", _nicht_springen)

    assert has_several_images(_bild(dateiformat, 3)) is True
    assert has_several_images(_bild(dateiformat, 1)) is False


def test_jpeg_und_bmp_sind_keine_bildfolgen() -> None:
    """Ein Multi-Picture-JPEG (zweites Bild als Vorschau oder Tiefenkarte) ist ein
    Foto. Deshalb zaehlt die Typpruefung diese beiden gar nicht erst."""
    assert not DocumentType.JPEG.can_hold_several_images
    assert not DocumentType.BMP.can_hold_several_images


@pytest.mark.parametrize(
    "typ",
    [DocumentType.TIFF, DocumentType.GIF, DocumentType.WEBP, DocumentType.PNG],
)
def test_diese_bildtypen_koennen_mehrere_bilder_tragen(typ: DocumentType) -> None:
    assert typ.can_hold_several_images


@pytest.mark.parametrize(
    "typ", [DocumentType.PDF, DocumentType.DOCX, DocumentType.PPTX, DocumentType.XLSX]
)
def test_dokumente_sind_keine_bildfolgen(typ: DocumentType) -> None:
    assert not typ.can_hold_several_images
