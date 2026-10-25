"""Der einzige Ort im Paket, der Pillow benutzt: Traegt eine Bilddatei mehrere Bilder?

Warum das ueberhaupt gefragt wird: Die Engine liest von einer Bilddatei nur das
erste Bild. Ein mehrseitiges Fax als TIFF endete sonst als einseitiges `done`, und
alle Seiten nach der ersten gingen still verloren — ohne ein Fehlersignal, das
einer bemerkte. Der Job fragt deshalb vor jedem RunPod-Kontakt.

Gefragt wird nur „mehr als eines?", nicht „wie viele?". Bei TIFF springt
`Image.seek(1)` zum zweiten Bild und kostet einen einzigen Verzeichniseintrag,
waehrend `n_frames` jede Seite durchlaeuft — bei Zehntausenden Seiten Sekunden.
Bei GIF, PNG und WebP ist es umgekehrt: Dort dekodiert `seek(1)` das erste Bild
(ein GIF mit 8192 x 8192 Pixeln: rund 120 ms und hunderte MB), und `is_animated`
dekodiert nichts. PNG und WebP antworten aus den Kopfdaten; GIF geht die Bloecke
bis zum zweiten Bild durch, und das kostet bei einem boesartig gebauten GIF
(Kommentarbloecke bis zur Groessengrenze von 7 MB) Sekunden bis Minuten. Deshalb
laeuft die Frage im Job in einem Thread (`asyncio.to_thread`) und erst nach der
Groessenpruefung: Pillow ist anders als PDFium dafuer threadsicher genug, solange
jedes Bild nur in einem Thread geoeffnet wird. Ein solcher Thread laesst sich
nicht abbrechen; er verzoegert beim Herunterfahren hoechstens bis zum Ende seiner
Arbeit.

Pillow oeffnet lazy: `Image.open` liest nur den Kopf, die Pixel werden nicht
geladen.
"""

from __future__ import annotations

import io

from PIL import Image


def has_several_images(data: bytes) -> bool:
    """Ob eine Bilddatei ein zweites Bild traegt.

    Args:
        data: Die Bytes einer Bilddatei, deren Typ bereits erkannt wurde
            (`mime.sniff_bytes`) und die mehrere Bilder tragen kann
            (`DocumentType.can_hold_several_images`).

    Returns:
        `True`, wenn Pillow ein zweites Bild findet. `False` bei genau einem Bild
        und auch dann, wenn Pillow die Datei nicht oeffnen oder bis zum zweiten
        Bild lesen kann (abgeschnitten, beschaedigt, zu viele Pixel): Dann steht
        nicht fest, dass es mehrere sind, und ob die Datei lesbar ist, entscheidet
        die Engine, nicht diese Pruefung. Eine Pruefung, die nur fragen soll,
        macht die Datei nicht auch noch zum `input_error`.

    Kosten: keine. Reine lokale Rechenarbeit.
    """
    try:
        with Image.open(io.BytesIO(data)) as bild:
            if bild.format == "TIFF":
                bild.seek(1)  # EOFError: es gibt kein zweites Bild
                return True
            return bool(getattr(bild, "is_animated", False))
    except Exception:  # noqa: BLE001 — EOFError heisst „nur ein Bild", der Rest „nicht zu lesen"
        return False
