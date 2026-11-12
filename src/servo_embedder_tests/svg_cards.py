"""The cards of the SVG test pages in `pages/svg`, each made into a page scenario that shows only its SVG.

A card's page keeps the style sheet of the page it comes from, so that the SVG has the same size and inherits the same
properties, and puts the SVG in the top left corner without the dashed border.
"""

from __future__ import annotations

import html.parser
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from .page_scenarios import SUITE_CONFIG

PAGES = Path(str(resources.files("servo_embedder_tests") / "pages" / "svg"))
# The suite configuration of the cards; its first line marks the folders `write` wrote.
SUITE = """# Written by `servo-embedder-tests svg-cards`: a page scenario per card of the SVG test pages.
segments = 1

[page]
freeze = true
errors = true
"""


@dataclass(frozen=True)
class Card:
    page: str
    number: int
    title: str
    page_title: str
    style: str
    svg: str

    @property
    def name(self) -> str:
        """The page and the card's number, zero-padded to sort in page order: `01_basic_shapes-03`."""
        number, rest = self.page.split("_", 1)
        return f"{int(number):02d}_{rest}-{self.number:02d}"

    def html(self) -> str:
        title = html.escape(f"{self.page_title}: {self.title}")
        return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<style>{self.style}</style>
<style>
html, body {{ margin: 0; padding: 0; background: #ffffff; }}
body > svg {{ display: block; border: none; }}
</style>
</head>
<body>
{self.svg}
</body>
</html>
"""


class _CardParser(html.parser.HTMLParser):
    """Finds the title, the stylesheet and each `.test-card`'s heading and outermost `<svg>` of a page."""

    def __init__(self, text: str) -> None:
        super().__init__(convert_charrefs=True)
        self.text = text
        self.line_starts = [0]
        for line in text.splitlines(keepends=True):
            self.line_starts.append(self.line_starts[-1] + len(line))
        self.title = ""
        self.style = ""
        self.cards: list[tuple[str, str]] = []
        self._in: str | None = None
        self._heading = ""
        self._card_depth: int | None = None
        self._div_depth = 0
        self._svg_depth = 0
        self._svg_start = 0

    def _offset(self) -> int:
        line, column = self.getpos()
        return self.line_starts[line - 1] + column

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "div":
            self._div_depth += 1
            if "test-card" in (dict(attrs).get("class") or "").split():
                self._card_depth = self._div_depth
                self._heading = ""
        elif tag in ("title", "style") or (tag == "h3" and self._card_depth is not None and not self._svg_depth):
            self._in = tag
        elif tag == "svg" and self._card_depth is not None:
            if not self._svg_depth:
                self._svg_start = self._offset()
            self._svg_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == self._in:
            self._in = None
        if tag == "div":
            if self._div_depth == self._card_depth:
                self._card_depth = None
            self._div_depth -= 1
        elif tag == "svg" and self._svg_depth:
            self._svg_depth -= 1
            if not self._svg_depth:
                end = self.text.index(">", self._offset()) + 1
                self.cards.append((self._heading.strip(), self.text[self._svg_start : end]))

    def handle_data(self, data: str) -> None:
        if self._in == "title":
            self.title += data
        elif self._in == "style":
            self.style += data
        elif self._in == "h3":
            self._heading += data


def load(directory: Path = PAGES) -> list[Card]:
    """The cards of the numbered pages in `directory`, in page order."""
    cards = []
    for path in sorted(directory.glob("*.html"), key=lambda p: int(p.name.split("_", 1)[0])):
        parser = _CardParser(path.read_text(encoding="utf-8"))
        parser.feed(parser.text)
        parser.close()
        for number, (title, svg) in enumerate(parser.cards, 1):
            cards.append(Card(path.stem, number, title, parser.title.strip(), parser.style, svg))
    return cards


def write(dest: Path, cards: list[Card]) -> None:
    """Writes a page scenario of each card into `dest`: a new or empty folder, or one this wrote before."""
    suite = dest / SUITE_CONFIG
    written = suite.is_file() and suite.read_text(encoding="utf-8").startswith(SUITE.split("\n", 1)[0])
    if dest.is_dir() and any(dest.iterdir()) and not written:
        raise ValueError(f"{dest} has other files; pass a new folder or one that svg-cards wrote")
    for card in cards:
        directory = dest / card.name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "index.html").write_text(card.html(), encoding="utf-8")
    suite.write_text(SUITE, encoding="utf-8")
