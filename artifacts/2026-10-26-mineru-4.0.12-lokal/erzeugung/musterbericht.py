"""Baut einen erfundenen Musterbericht ohne personenbezogene Daten (reportlab, Textebene).

Neun Seiten; jede mit Kopfzeile und Fusszeile mit Seitenzahl, Fliesstext, auf jeder
Seite ein Bild an derselben Stelle (damit MinerU je Teilstueck dieselben Namen
vergibt), auf jeder dritten eine Tabelle.
"""
import io
import sys

from PIL import Image, ImageDraw
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

ziel = sys.argv[1]
breite, hoehe = A4
c = canvas.Canvas(ziel, pagesize=A4)
c.setTitle("Musterbericht 2025")
farben = ["#c0392b", "#2980b9", "#27ae60", "#8e44ad", "#d35400", "#16a085", "#2c3e50", "#f39c12", "#7f8c8d"]
for seite in range(9):
    c.setFont("Helvetica", 9)
    c.drawString(50, hoehe - 40, "Musterbericht 2025 der Beispiel AG")
    c.drawRightString(breite - 50, 30, f"Seite {seite + 1} von 9")
    c.setFont("Helvetica-Bold", 16)
    c.drawString(50, hoehe - 90, f"Kapitel {seite + 1}: Entwicklung im Bereich {chr(65 + seite)}")
    c.setFont("Helvetica", 11)
    zeilen = [
        f"Im Berichtsjahr wuchs der Bereich {chr(65 + seite)} um {seite + 3} Prozent gegenueber dem Vorjahr.",
        "Die Zahlen sind erfunden und dienen allein als Testmaterial fuer die Texterkennung.",
        f"Der Umsatz betrug {(seite + 1) * 125} Millionen Euro, das Ergebnis {(seite + 1) * 11} Millionen Euro.",
        "Die folgende Abbildung zeigt die Entwicklung der Kennzahl ueber die letzten Quartale.",
    ]
    y = hoehe - 120
    for zeile in zeilen:
        c.drawString(50, y, zeile)
        y -= 16
    bild = Image.new("RGB", (240, 140), "white")
    zeichner = ImageDraw.Draw(bild)
    for q in range(4):
        h = 20 + ((seite + q) * 23) % 100
        zeichner.rectangle([20 + q * 55, 130 - h, 60 + q * 55, 130], fill=farben[(seite + q) % len(farben)])
    puffer = io.BytesIO()
    bild.save(puffer, format="PNG")
    c.drawImage(ImageReader(io.BytesIO(puffer.getvalue())), 50, y - 170, width=240, height=140)
    c.setFont("Helvetica-Oblique", 10)
    c.drawString(50, y - 186, f"Abbildung {seite + 1}: Kennzahl {chr(65 + seite)} je Quartal")
    if seite % 3 == 0:
        c.setFont("Helvetica", 10)
        ty = y - 230
        for r, reihe in enumerate([("Quartal", "Umsatz", "Ergebnis"), ("Q1", "30", "2"), ("Q2", "31", "3"), ("Q3", "33", "3")]):
            for s, wert in enumerate(reihe):
                c.rect(50 + s * 90, ty - r * 18, 90, 18)
                c.drawString(55 + s * 90, ty - r * 18 + 5, wert)
    c.showPage()
c.save()
print("geschrieben", ziel)
