"""Baut das Testmaterial der Aufnahme zur Bildanalyse, erfunden und ohne personenbezogene Daten.

Aufruf: material.py ZIELORDNER. Erzeugt:
  bild_seite.pdf   eine Seite mit Kopfzeile, Ueberschrift, Fliesstext, zwei
                   Bildern, die keine Diagramme sind (eine gezeichnete Szene mit
                   Haus, Baum und Sonne und ein Organigramm aus Kaesten und
                   Linien), je mit Bildunterschrift, und einer Seitenzahl
"""
import io
import pathlib
import sys

from PIL import Image, ImageDraw
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

ziel = pathlib.Path(sys.argv[1])
ziel.mkdir(parents=True, exist_ok=True)
breite, hoehe = A4


def als_bild(bild: Image.Image) -> ImageReader:
    puffer = io.BytesIO()
    bild.save(puffer, format="PNG")
    return ImageReader(io.BytesIO(puffer.getvalue()))


def szene() -> ImageReader:
    """Ein Haus mit rotem Dach, ein Baum und eine Sonne ueber einer Wiese."""
    bild = Image.new("RGB", (480, 300), "#cfe8ff")
    z = ImageDraw.Draw(bild)
    z.rectangle([0, 220, 480, 300], fill="#5cb85c")
    z.ellipse([370, 30, 440, 100], fill="#f7d33c")
    z.rectangle([90, 140, 230, 240], fill="#e8d3a9", outline="black", width=2)
    z.polygon([(75, 140), (160, 70), (245, 140)], fill="#c0392b", outline="black")
    z.rectangle([145, 190, 175, 240], fill="#7b4a2a", outline="black")
    z.rectangle([105, 160, 135, 185], fill="#9fd3ff", outline="black")
    z.rectangle([195, 160, 225, 185], fill="#9fd3ff", outline="black")
    z.rectangle([310, 160, 330, 240], fill="#7b4a2a")
    z.ellipse([270, 90, 370, 180], fill="#2e8b57")
    return als_bild(bild)


def organigramm() -> ImageReader:
    """Eine Geschaeftsfuehrung, darunter drei Abteilungen, durch Linien verbunden."""
    bild = Image.new("RGB", (480, 240), "white")
    z = ImageDraw.Draw(bild)
    z.rectangle([170, 20, 310, 70], outline="black", width=2)
    z.text((190, 38), "Geschaeftsfuehrung", fill="black")
    z.line([(240, 70), (240, 110)], fill="black", width=2)
    z.line([(80, 110), (400, 110)], fill="black", width=2)
    for x, name in [(80, "Einkauf"), (240, "Fertigung"), (400, "Vertrieb")]:
        z.line([(x, 110), (x, 150)], fill="black", width=2)
        z.rectangle([x - 60, 150, x + 60, 200], outline="black", width=2)
        z.text((x - 25, 168), name, fill="black")
    return als_bild(bild)


c = canvas.Canvas(str(ziel / "bild_seite.pdf"), pagesize=A4)
c.setTitle("Werkszeitung Beispielwerke")
c.setFont("Helvetica", 9)
c.drawString(50, hoehe - 36, "Werkszeitung der Beispielwerke  |  Ausgabe Herbst 2026")
c.setFont("Helvetica-Bold", 16)
c.drawString(50, hoehe - 85, "Neues Gaestehaus am Werk Nord")
c.setFont("Helvetica", 11)
y = hoehe - 112
for zeile in [
    "Am Werk Nord ist das neue Gaestehaus fertig geworden. Es bietet sechs Zimmer fuer",
    "Besucher aus den Partnerwerken und liegt am Rand der Werkswiese.",
]:
    c.drawString(50, y, zeile)
    y -= 16
c.drawImage(szene(), 50, y - 200, width=300, height=188)
c.setFont("Helvetica-Oblique", 10)
c.drawString(50, y - 215, "Bild 1: Das Gaestehaus am Werk Nord")
c.setFont("Helvetica", 11)
c.drawString(50, y - 245, "Die Abteilungen des Werks sind seit dem Sommer so gegliedert:")
c.drawImage(organigramm(), 50, y - 420, width=300, height=150)
c.setFont("Helvetica-Oblique", 10)
c.drawString(50, y - 435, "Bild 2: Gliederung des Werks Nord")
c.setFont("Helvetica", 9)
c.drawRightString(breite - 50, 36, "Seite 3")
c.showPage()
c.save()

print("geschrieben:", sorted(p.name for p in ziel.iterdir()))
