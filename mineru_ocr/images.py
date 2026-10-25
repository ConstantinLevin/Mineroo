"""Der einzige Ort im Paket, der Pillow benutzt: Wie viele Bilder traegt eine Bilddatei?

Warum das ueberhaupt gefragt wird: Die Engine liest von einer Bilddatei nur das
erste Bild. Ein mehrseitiges Fax als TIFF endete sonst als einseitiges `done`, und
alle Seiten nach der ersten gingen still verloren — ohne ein Fehlersignal, das
einer bemerkte. Der Job zaehlt deshalb vor jedem RunPod-Kontakt.

Pillow oeffnet lazy: `Image.open` liest nur den Kopf, die Pixel werden nicht
geladen. Gezaehlt wird mit `n_frames`; fehlt das Attribut, kennt das Format keine
Folge von Bildern (JPEG, BMP), und es ist eines.
"""

from __future__ import annotations

import io

from PIL import Image


def count_images(data: bytes) -> int | None:
    """Zaehlt die Bilder in einer Bilddatei.

    Args:
        data: Die Bytes einer Bilddatei, deren Typ bereits erkannt wurde
            (`mime.sniff_bytes`).

    Returns:
        Die Zahl der Bilder, mindestens 1. `None`, wenn Pillow die Datei nicht
        oeffnen oder durchzaehlen kann (abgeschnitten, beschaedigt, zu viele
        Pixel): Dann steht nicht fest, dass es mehrere sind, und ob die Datei
        lesbar ist, entscheidet die Engine, nicht diese Zaehlung. Eine Pruefung,
        die nur zaehlen soll, macht die Datei nicht auch noch zum `input_error`.

    Kosten: keine. Reine lokale Rechenarbeit.
    """
    try:
        with Image.open(io.BytesIO(data)) as bild:
            return int(getattr(bild, "n_frames", 1))
    except Exception:  # noqa: BLE001 — Pillow wirft je Format Unterschiedliches
        return None
