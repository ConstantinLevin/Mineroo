"""Lesbares Markdown ohne Eingriff in bereits vorhandenes Markdown (#61)."""

from datetime import datetime, timezone

from mineru_ocr import DocumentRow, DocumentStatus, OCRWrapper


def huelle(text: str) -> OCRWrapper:
    zeit = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return OCRWrapper.from_row(DocumentRow(
        sha256="a" * 64, size_bytes=12, mime="image/png", page_count=1,
        status=DocumentStatus.DONE, raw={"markdown_full": text}, error=None,
        endpoint_id="testendpoint", image_tag_at_submit=None, notes=[],
        created_at=zeit, updated_at=zeit,
    ), None)


def test_lesbare_vollfassung_wandelt_nur_html_um() -> None:
    davor = "# Kopf\n\n*kursiv* **fett** foo_bar \\*literal\\*\n\n$$x_i = a*b$$\n\n"
    danach = "\n\n[Link](https://example.org/a_b)  \n_Fuss_ &amp; Ende\n"
    html = "<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>"
    dokument = huelle(davor + html + danach)
    assert dokument.readable_markdown == davor + "| A | B |\n| --- | --- |\n| 1 | 2 |" + danach
    assert dokument.rendered_markdown == davor + html + danach
    assert dokument.raw == {"markdown_full": davor + html + danach}


def test_markdown_code_formeln_und_autolinks_bleiben_unveraendert() -> None:
    text = (
        '`<b>Code</b>` und ``<table><tr><td>Code</td></tr></table>``\n\n'
        '```html\n<table><tr><td>Beispiel</td></tr></table>\n```\n\n'
        '~~~html\n<b>Beispiel</b>\n~~~\n\n'
        '    <b>Eingerueckter Code</b>\n\n'
        '$<b>x_i</b>$ und $$<b>a*b</b>$$\n'
        r'\(<b>x</b>\) und \[<b>y</b>\]' '\n'
        '<https://example.org/a_b> <synthetisch@example.org>\n'
        r'\<b>woertliches Tag</b>' '\n'
    )
    assert huelle(text).readable_markdown == text


def test_html_bloecke_inline_tags_und_zeilenumbrueche_werden_lesbar() -> None:
    text = ('Vor <strong>fett</strong> und <em>kursiv</em><br>weiter\n\n'
            '<details><summary>Bild</summary><p>Beschreibung.</p></details>\n\n'
            'Tabelle:<table data-x=">">\n<tr><td>A<br>B</td><td>Übung</td></tr></table>Ende')
    lesbar = huelle(text).readable_markdown
    assert lesbar.startswith('Vor **fett** und *kursiv*  \nweiter\n\n**Bild**\n\nBeschreibung.\n\n')
    assert 'Tabelle:\n\n| A  <br>B | Übung |\n| --- | --- |\n\nEnde' in lesbar


def test_markdown_linkziele_und_titel_bleiben_auch_neben_html_erhalten() -> None:
    texte = [
        '[link](https://example.org "<b>literal title</b>")',
        '![alt](image.png "<b>literal title</b>")',
        '[ref]: https://example.org "<b>literal title</b>"\n',
        "[link](https://example.org 'Titel ) <b>literal</b>')",
        '[link](https://example.org/a_(b) "Titel <b>literal</b>")',
        '[ref]: https://example.org\n  "Titel <b>literal</b>"\n',
    ]
    for text in texte:
        assert huelle(text + '\n\n<b>echt</b>').readable_markdown == text + '\n\n**echt**'
    assert huelle('[<b>Label</b>](https://example.org "<b>Titel</b>")').readable_markdown == (
        '[**Label**](https://example.org "<b>Titel</b>")'
    )
