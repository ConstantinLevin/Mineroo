"""HTML-Inseln der MinerU-Vollfassung nach Markdown umsetzen (#61)."""

from __future__ import annotations

import re
from html.parser import HTMLParser

from html_to_markdown import ConversionOptions, convert
from markdown_it import MarkdownIt


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
    for token in MarkdownIt("commonmark", {"inline_definitions": True}).parse(text):
        if token.type in {"fence", "code_block", "definition"} and token.map is not None:
            schuetzen(zeilen[token.map[0]], zeilen[token.map[1]])
    for muster in (
        r"(?s)(`+)(?!`)(.+?)(?<!`)\1(?!`)",
        r"(?s)(?<!\\)\$\$.*?(?<!\\)\$\$|(?<![\\$])\$(?!\$)[^\n]*?(?<!\\)\$",
        r"(?s)\\\(.*?\\\)|\\\[.*?\\\]",
    ):
        for treffer in re.finditer(muster, "".join(maskiert)):
            schuetzen(treffer.start(), treffer.end())
    # Ziele und Titel sind Markdown-Syntax, kein Inline-HTML. Das Label
    # bleibt frei: [<b>Text</b>](ziel) enthaelt dagegen echtes Inline-HTML.
    for treffer in re.finditer(r"\]\(", "".join(maskiert)):
        tiefe = 1
        zitat = ""
        winkel = False
        escaped = False
        for i in range(treffer.end(), len(text)):
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
                    schuetzen(treffer.start(), i + 1)
                    break
    # Ein maskiertes Startzeichen verhindert, dass ein escaptes Tag beginnt.
    for treffer in re.finditer(r"(\\+)<", text):
        if len(treffer[1]) % 2:
            maskiert[treffer.end() - 1] = " "
    return "".join(maskiert)


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
        markdown = convert(text[anfang:ende], ConversionOptions(br_in_tables=True)).strip("\n")
        if tag == "br":
            markdown = "  \n"
        elif tag in _BLOCK_TAGS:
            # Abstand gehoert zur ersetzten HTML-Insel. Der Quelltext daneben
            # wird auch dann nicht angefasst, wenn er bereits Abstand mitbringt.
            davor = text[:anfang]
            danach = text[ende:]
            if davor:
                markdown = "\n" * max(0, 2 - (len(davor) - len(davor.rstrip("\n")))) + markdown
            if danach:
                markdown += "\n" * max(0, 2 - (len(danach) - len(danach.lstrip("\n"))))
        teile.append(markdown)
        position = ende
    teile.append(text[position:])
    return "".join(teile)
