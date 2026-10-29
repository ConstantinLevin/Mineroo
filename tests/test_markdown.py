"""Lesbares Markdown ohne Eingriff in bereits vorhandenes Markdown (#61)."""

from datetime import datetime, timezone

import pytest
from markdown_it import MarkdownIt

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


def test_markdown_referenztitel_in_containern_und_mit_escaptem_label_bleiben() -> None:
    for text in (
        '> [ref]: https://example.org "<b>literal title</b>"\n',
        '- [ref]: https://example.org "<b>literal title</b>"\n',
        '[ref\\]]: https://example.org "<b>literal title</b>"\n',
        '> [ref]: https://example.org\n>   "<b>literal title</b>"\n',
        '> - [ref]: https://example.org "<b>literal title</b>"\n',
        '1. [ref]: https://example.org "<b>literal title</b>"\n',
    ):
        assert huelle(text).readable_markdown == text


def test_markdown_code_in_zitaten_und_listen_bleibt_auch_neben_html_erhalten() -> None:
    for text in (
        '> ~~~html\n> <b>literal</b>\n> ~~~\n',
        '- ~~~html\n  <b>literal</b>\n  ~~~\n',
        '> - ~~~html\n>   <b>literal</b>\n>   ~~~\n',
        '>     <b>literal</b>\n',
        '-     <b>literal</b>\n',
        '> -     <b>literal</b>\n',
        '1.     <b>literal</b>\n',
    ):
        assert huelle(text + '\n<b>echt</b>').readable_markdown == text + '\n**echt**'


def test_offener_codezaun_endet_mit_seinem_markdown_container() -> None:
    tabelle = '<table><tr><th>A</th></tr></table>'
    for code in ('> ~~~html\n> <b>literal</b>\n', '- ~~~html\n  <b>literal</b>\n'):
        assert huelle(code + '\n' + tabelle).readable_markdown == code + '\n| A |\n| --- |'
    assert huelle('~~~html\n' + tabelle).readable_markdown == '~~~html\n' + tabelle


@pytest.mark.parametrize('davor,danach', [
    ('` offen\n\n', '\n\n` spaeter'),
    (r'\`', '`'),
    ('``', '`'),
    ('`', '``'),
])
def test_nur_commonmark_codespannen_schuetzen_html(davor: str, danach: str) -> None:
    html = '<b>echt</b>'
    assert huelle(davor + html + danach).readable_markdown == davor + '**echt**' + danach
    tabelle = '<table><tr><th>A</th></tr></table>'
    assert '<table>' not in huelle(davor + tabelle + danach).readable_markdown


@pytest.mark.parametrize('text', [
    '`<b>Code</b>`',
    '``<b>`Code`</b>``',
    '`<b>Code\nweiter</b>`',
    '> `<b>Code\n> weiter</b>`',
    '- `<b>Code\n  weiter</b>`',
    '# `<b>Code</b>` #',
    '`<b>Code\r\nweiter</b>`',
    '`<b>Code\rweiter</b>`',
])
def test_echte_codespannen_bleiben_zeichengetreu(text: str) -> None:
    assert huelle(text + '\n\n<b>echt</b>').readable_markdown == text + '\n\n**echt**'


@pytest.mark.parametrize('bild', [
    '![`<b>Code</b>`](x)',
    '![alt `<b>Code</b>`](x)',
    '![![`<b>Code</b>`](x)](y)',
    '![`<b>Code</b>`][ref]\n\n[ref]: image.png',
])
def test_codespannen_in_bildlabels_schuetzen_nur_ihren_quellausschnitt(bild: str) -> None:
    text = '<b>echt</b> ' + bild + '\n\n<b>danach</b>'
    assert huelle(text).readable_markdown == '**echt** ' + bild + '\n\n**danach**'


@pytest.mark.parametrize('deklaration', ['<![foo]>', '<![!]>', '<![]>', '<![foo', '<![123]>'])
def test_kaputte_deklarationen_bleiben_literal_und_html_wird_weiter_umgesetzt(
    deklaration: str,
) -> None:
    text = deklaration + '\n\n<b>echt</b>'
    assert huelle(text).readable_markdown == deklaration + '\n\n**echt**'


def test_kaputte_deklaration_in_html_bleibt_literal() -> None:
    assert huelle('<b><![foo]></b>').readable_markdown == '**<![foo]>**'


def test_zitatpraefix_ohne_leerzeichen_gehoert_nicht_zum_html_inhalt() -> None:
    assert huelle('> <div>one\n>two</div>').readable_markdown == '> one\n> two'
    assert huelle('> <table><tr><td>A\n>B</td></tr></table>').readable_markdown == (
        '> | A<br>B |\n> | --- |'
    )
    assert huelle('> > <div>one\n>>two</div>').readable_markdown == '> > one\n> > two'


@pytest.mark.parametrize('anfang,folge,erwartet', [
    ('>> ', '> > ', '>> '),
    ('>>', '> > ', '>>'),
    ('> ', '  >', '> '),
    ('>> ', '>  > ', '>> '),
    ('- > ', '\t> ', '  > '),
    ('> - ', '>\t', '>   '),
])
def test_container_praefixe_duerfen_abstaende_und_tabs_wechseln(
    anfang: str, folge: str, erwartet: str,
) -> None:
    text = anfang + '<div>one\n' + folge + 'two</div>'
    assert huelle(text).readable_markdown == anfang + 'one\n' + erwartet + 'two'
    text = anfang + '<table><tr><td>A\n' + folge + 'B</td></tr></table>'
    assert huelle(text).readable_markdown == anfang + '| A<br>B |\n' + erwartet + '| --- |'


@pytest.mark.parametrize('umbruch', ['\n', '\r\n', '\r'])
def test_container_erkennen_alle_commonmark_zeilenenden(umbruch: str) -> None:
    assert huelle('> <b>one' + umbruch + '> two</b>').readable_markdown == (
        '> **one\n> two**'
    )
    assert huelle('> <div>one' + umbruch + '> two</div>').readable_markdown == '> one\n> two'
    assert huelle('> <table><tr><td>A' + umbruch + '>B</td></tr></table>').readable_markdown == (
        '> | A<br>B |\n> | --- |'
    )
    assert huelle('Davor' + umbruch + '> <table><tr><th>A</th></tr></table>').readable_markdown == (
        'Davor' + umbruch + '> | A |\n> | --- |'
    )
    assert huelle('> - <div>one' + umbruch + '>\ttwo</div>').readable_markdown == '> - one\n>   two'


@pytest.mark.parametrize('anfang,fortsetzung', [
    ('> ', '> '), ('> > ', '> > '), ('- ', '  '), ('10. ', '    '),
    ('> - ', '>   '), ('- > ', '  > '), ('  - ', '    '),
])
@pytest.mark.parametrize('mehrzeilig', [False, True])
@pytest.mark.parametrize('vorspann', ['', 'Davor '])
def test_tabellen_bleiben_in_ihren_markdown_containern(
    anfang: str, fortsetzung: str, mehrzeilig: bool, vorspann: str,
) -> None:
    tabelle = '<table><tr><th>A</th></tr><tr><td>2</td></tr></table>'
    if mehrzeilig:
        tabelle = tabelle.replace('><tr>', '>\n' + fortsetzung + '<tr>')
    text = anfang + vorspann + tabelle + ' Danach\n' + fortsetzung + 'Weiter'
    lesbar = huelle(text).readable_markdown
    assert '<table>' not in lesbar
    assert '| 2 |' in lesbar
    assert 'Danach' in lesbar and 'Weiter' in lesbar
    # Nicht nur Praefixe pruefen: Ein GFM-Leser muss die Tabelle innerhalb
    # derselben Zitate und Listeneintraege sehen, auch bei leerem Vorspann.
    parser = MarkdownIt('commonmark').enable('table')
    original = parser.parse(text)
    erwartet = [t.type for t in original if t.type in {'blockquote_open', 'list_item_open'}]
    container: list[str] = []
    gesehen: list[str] | None = None
    for token in parser.parse(lesbar):
        if token.type in {'blockquote_open', 'list_item_open'}:
            container.append(token.type)
        elif token.type in {'blockquote_close', 'list_item_close'}:
            container.pop()
        elif token.type == 'table_open':
            gesehen = container.copy()
    assert gesehen == erwartet
