"""HTML-Inseln der MinerU-Vollfassung nach Markdown umsetzen (#61)."""

from __future__ import annotations

import re
from html.parser import HTMLParser

from html_to_markdown import ConversionOptions, convert
from markdown_it import MarkdownIt
from markdown_it.rules_inline import StateInline, backtick, image


_HTML_TAGS = frozenset("""
a abbr address article aside b bdi bdo blockquote body br caption center cite code
col colgroup dd del details dfn div dl dt em figcaption figure font footer h1 h2 h3
h4 h5 h6 head header hgroup hr html i img ins kbd li main mark nav ol p picture pre
q rp rt ruby s samp section small source span strike strong sub summary sup table
tbody td tfoot th thead time tr tt u ul var wbr
""".split())
_BLOCK_TAGS = frozenset("""
address article aside blockquote body caption center dd details div dl dt figure
figcaption footer h1 h2 h3 h4 h5 h6 header hr html li main nav ol p pre section table ul
""".split())
_LEERE_TAGS = frozenset({"br", "col", "hr", "img", "source", "wbr"})


class _HTMLInseln(HTMLParser):
    """Findet Quellbereiche, ohne Text oder Zeichenreferenzen neu zu schreiben."""

    def __init__(self, text: str) -> None:
        super().__init__(convert_charrefs=False)
        self._zeilen = [0]
        for treffer in re.finditer("\n", text):
            self._zeilen.append(treffer.end())
        self._text = text
        self._stapel: list[str] = []
        self._anfang = 0
        self._tag = ""
        self.bereiche: list[tuple[int, int, str]] = []

    def _position(self) -> int:
        zeile, spalte = self.getpos()
        return self._zeilen[zeile - 1] + spalte

    def parse_html_declaration(self, i: int) -> int:
        try:
            return super().parse_html_declaration(i)
        except AssertionError:
            # HTMLParser lehnt unbekannte <![...]>-Deklarationen ab. Sie
            # bleiben woertlicher Text; die naechste HTML-Insel wird gelesen.
            ende = i + 2
            while ende < len(self.rawdata) and self.rawdata[ende] not in '<>\r\n':
                ende += 1
            if self.rawdata[ende:ende + 1] == '>':
                ende += 1
            self.handle_data(self.rawdata[i:ende])
            return ende

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in _HTML_TAGS:
            return
        position = self._position()
        if not self._stapel:
            self._anfang = position
            self._tag = tag
        if tag in _LEERE_TAGS:
            if not self._stapel:
                self.bereiche.append((position, position + len(self.get_starttag_text() or ""), tag))
        else:
            self._stapel.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag not in self._stapel:
            return
        # Auch fehlende innere End-Tags gehoeren zum umschliessenden HTML.
        del self._stapel[len(self._stapel) - 1 - self._stapel[::-1].index(tag):]
        if not self._stapel:
            ende = self._text.find(">", self._position()) + 1
            self.bereiche.append((self._anfang, ende, self._tag))


def _linkziel_ende(text: str, anfang: int) -> int | None:
    """Findet das Ende hinter ](; beachtet Klammern, Titel und Escapezeichen."""
    tiefe = 1
    zitat = ""
    winkel = False
    escaped = False
    for i in range(anfang, len(text)):
        zeichen = text[i]
        if escaped:
            escaped = False
        elif zeichen == "\\":
            escaped = True
        elif zitat:
            if zeichen == zitat:
                zitat = ""
        elif winkel:
            if zeichen == ">":
                winkel = False
        elif zeichen in {"'", '"'} and text[i - 1].isspace():
            zitat = zeichen
        elif zeichen == "<":
            winkel = True
        elif zeichen == "(":
            tiefe += 1
        elif zeichen == ")":
            tiefe -= 1
            if tiefe == 0:
                return i + 1
    return None


def _markdown_schuetzen(text: str) -> str:
    """Maskiert Code und Formeln laengentreu nur fuer die HTML-Erkennung."""
    maskiert = list(text)

    def schuetzen(anfang: int, ende: int) -> None:
        for i in range(anfang, ende):
            if text[i] != "\n":
                maskiert[i] = " "

    # Nur Quellbereiche lesen, nie Markdown neu serialisieren. Die Zeilenkarte
    # kennt auch offene Codezaeune, die mit ihrem Zitat oder Listeneintrag enden.
    zeilen = [0, *(t.end() for t in re.finditer(r"\r\n?|\n", text)), len(text)]
    markdown = MarkdownIt("commonmark", {"inline_definitions": True}).disable('inline')
    versatz = 0

    def codespanne(state: StateInline, silent: bool) -> bool:
        anfang = state.pos
        vorher = len(state.tokens)
        erkannt = backtick(state, silent)
        if not silent and len(state.tokens) > vorher and state.tokens[-1].type == 'code_inline':
            schuetzen(versatz + anfang, versatz + state.pos)
        return erkannt

    def bild(state: StateInline, silent: bool) -> bool:
        nonlocal versatz
        vorher = versatz
        # Bild-Labels parst markdown-it rekursiv als eigenen Quellausschnitt
        # hinter ![. Dessen Codespannen brauchen den passenden Quellversatz.
        versatz += state.pos + 2
        try:
            return image(state, silent)
        finally:
            versatz = vorher

    markdown.inline.ruler.at('backticks', codespanne)
    markdown.inline.ruler.at('image', bild)
    for token in markdown.parse(text):
        if token.type in {"fence", "code_block", "definition"} and token.map is not None:
            schuetzen(zeilen[token.map[0]], zeilen[token.map[1]])
        elif token.type == 'inline' and token.map is not None:
            # Innerhalb der vom Blockparser bestimmten Absatzgrenzen parsen.
            # Die Originalzeilen behalten Praefixe und CRLF, damit die Positionen
            # direkt auf die Quelle zeigen; kein normalisiertes token.content.
            versatz = zeilen[token.map[0]]
            markdown.inline.parse(text[versatz:zeilen[token.map[1]]], markdown, {}, [])
    for muster in (
        r"(?s)(?<!\\)\$\$.*?(?<!\\)\$\$|(?<![\\$])\$(?!\$)[^\n]*?(?<!\\)\$",
        r"(?s)\\\(.*?\\\)|\\\[.*?\\\]",
    ):
        for treffer in re.finditer(muster, "".join(maskiert)):
            schuetzen(treffer.start(), treffer.end())
    # Ziele und Titel sind Markdown-Syntax, kein Inline-HTML. Das Label
    # bleibt frei: [<b>Text</b>](ziel) enthaelt dagegen echtes Inline-HTML.
    for treffer in re.finditer(r"\]\(", "".join(maskiert)):
        ende = _linkziel_ende(text, treffer.end())
        if ende is not None:
            schuetzen(treffer.start(), ende)
    # Ein maskiertes Startzeichen verhindert, dass ein escaptes Tag beginnt.
    for treffer in re.finditer(r"(\\+)<", text):
        if len(treffer[1]) % 2:
            maskiert[treffer.end() - 1] = " "
    return "".join(maskiert)


def _container_praefix(zeile: str) -> tuple[str, str]:
    """Liest Zitat-/Listenmarker und ihre Einrueckung fuer Folgezeilen."""
    treffer = re.match(r'(?:[ \t]*(?:>[ \t]?|(?:[-+*]|[0-9]{1,9}[.)])[ \t]+))*[ \t]*', zeile)
    assert treffer is not None
    praefix = treffer[0]
    fortsetzung = re.sub(r'[-+*]|[0-9]{1,9}[.)]', lambda t: ' ' * len(t[0]), praefix)
    return praefix, fortsetzung


def _html_ohne_praefixe(html: str, fortsetzung: str) -> str:
    """Entfernt Container auch mit wechselnden Zitatabstaenden und Tabs."""
    normalisiert = fortsetzung.expandtabs(4)
    zitate = normalisiert.count('>')
    einrueckung = len(normalisiert.rsplit('>', 1)[-1]) - bool(zitate and normalisiert.endswith(' '))
    muster = re.compile(r' *> ?' * zitate + ' ' * einrueckung)
    zeilen = re.split(r'(\r\n?|\n)', html)
    for n in range(2, len(zeilen), 2):
        zeile = zeilen[n]
        treffer = muster.match(zeile.expandtabs(4))
        if treffer is None:
            continue
        # Die visuelle Spalte auf die Originalzeichen zurueckfuehren. Endet der
        # Praefix mitten in einem Tab, gehoeren dessen restliche Spalten zum HTML.
        spalte = 0
        i = 0
        while spalte < treffer.end():
            spalte += 4 - spalte % 4 if zeile[i] == '\t' else 1
            i += 1
        zeilen[n] = ' ' * (spalte - treffer.end()) + zeile[i:]
    return ''.join(zeilen)


def html_to_markdown(text: str) -> str:
    """Konvertiert nur HTML; Markdown ausserhalb bleibt zeichengetreu erhalten.

    Tabellen werden GFM-Tabellen. Unvollstaendige HTML-Inseln bleiben erhalten.
    Die Umwandlung liest und veraendert weder Datenbank noch Rohantwort.
    """
    parser = _HTMLInseln(text)
    parser.feed(_markdown_schuetzen(text))
    parser.close()
    teile: list[str] = []
    position = 0
    for anfang, ende, tag in parser.bereiche:
        teile.append(text[position:anfang])
        zeilenanfang = max(text.rfind('\n', 0, anfang), text.rfind('\r', 0, anfang)) + 1
        davor_in_zeile = text[zeilenanfang:anfang]
        praefix, fortsetzung = _container_praefix(davor_in_zeile)
        html = text[anfang:ende]
        if fortsetzung:
            # Praefixe der Quellzeilen gehoeren zu Markdown, nicht in die
            # HTML-Zellen. Die erste Zeile beginnt bereits hinter dem Praefix.
            html = _html_ohne_praefixe(html, fortsetzung)
        markdown = convert(html, ConversionOptions(br_in_tables=True)).strip("\n")
        if tag == "br":
            markdown = "  \n"
        elif tag in _BLOCK_TAGS:
            # Abstand gehoert zur ersetzten HTML-Insel. Der Quelltext daneben
            # wird auch dann nicht angefasst, wenn er bereits Abstand mitbringt.
            davor = text[:anfang]
            danach = text[ende:]
            if davor and not (fortsetzung and davor_in_zeile == praefix):
                markdown = "\n" * max(0, 2 - (len(davor) - len(davor.rstrip("\n")))) + markdown
            if danach:
                markdown += "\n" * max(0, 2 - (len(danach) - len(danach.lstrip("\n"))))
        if fortsetzung:
            markdown = markdown.replace('\n', '\n' + fortsetzung)
        teile.append(markdown)
        position = ende
    teile.append(text[position:])
    return "".join(teile)
