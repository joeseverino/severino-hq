"""No template prints a figure inside a chip.

A chip (`.chip`, `.scope-chip`, `.pill`, `.tag`) is a word: a choice, a
status, a name. A figure (an amount, a count, a percent) is read as a number,
so it stands as one: in a `partials/_kpi_grid.html` cell, a table's numeric
column or a sentence (docs/Design.md, "Values read as what they are").
"""

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

from django.conf import settings
from django.template.base import Lexer, TokenType
from django.test import SimpleTestCase, tag

from hq.platform.core.tests.test_template_values import filters, template_paths

# The classes a chip is drawn by: one of these, or any class ending "-chip".
CHIPS = {"chip", "pill", "tag"}

# Filters whose output is a figure.
FIGURES = {
    "money": "an amount",
    "counted": "a count and the words for it",
    "length": "a count",
    "intcomma": "a number",
    "floatformat": "a number",
    "bytes": "a size",
}

# A printed name that is a count or a total by what it is called.
_FIGURE_NAME = re.compile(r"(?:^|[._])(?:count|total)$")

# Elements that never close, so are never a chip's parent.
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}

_MARK = "\x00"
_PRINTED = re.compile(rf"{_MARK}(\d+){_MARK}(%?)")


@dataclass(frozen=True)
class Site:
    path: str
    line: int
    value: str

    def __str__(self):
        return f"{self.path}:{self.line}: {{{{ {self.value} }}}}"


def is_figure(contents: str, percent: bool = False) -> bool:
    """Whether a printed variable is a number: through a figure's filter, by a
    count's or a total's name, or followed by a percent sign."""

    printed = contents.split("|")[0].strip()
    return percent or bool(FIGURES.keys() & set(filters(contents))) or bool(_FIGURE_NAME.search(printed))


def is_chip(classes: str) -> bool:
    return any(name in CHIPS or name.endswith("-chip") for name in classes.split())


class _Chips(HTMLParser):
    """Reads a template's markup with each printed variable marked in place,
    and collects the marks that stand in a chip's text."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.open: list[tuple[str, bool]] = []
        self.inside: list[tuple[int, bool]] = []

    def handle_starttag(self, tag, attrs):
        if tag not in VOID:
            self.open.append((tag, is_chip(dict(attrs).get("class") or "")))

    def handle_endtag(self, tag):
        for at in range(len(self.open) - 1, -1, -1):
            if self.open[at][0] == tag:
                del self.open[at:]
                return

    def handle_data(self, data):
        if any(chip for _, chip in self.open):
            self.inside.extend((int(found[1]), bool(found[2])) for found in _PRINTED.finditer(data))


def figures_in_chips(source: str, path: str = "") -> list[Site]:
    """Each figure ``source`` prints inside a chip. Both sides of a condition
    are read, as either may be drawn."""

    tokens = []
    marked = []
    commented = False
    for token in Lexer(source).tokenize():
        if token.token_type == TokenType.BLOCK:
            word = token.contents.split()[0] if token.contents.split() else ""
            commented = word == "comment" or (commented and word != "endcomment")
        elif commented:
            continue
        elif token.token_type == TokenType.TEXT:
            marked.append(token.contents)
        elif token.token_type == TokenType.VAR:
            marked.append(f"{_MARK}{len(tokens)}{_MARK}")
            tokens.append(token)
    reader = _Chips()
    reader.feed("".join(marked))
    reader.close()
    return [
        Site(path, tokens[at].lineno, tokens[at].contents)
        for at, percent in reader.inside
        if is_figure(tokens[at].contents, percent)
    ]


class FiguresInChipsTests(SimpleTestCase):
    """What the check reads as a figure in a chip."""

    def values(self, source):
        return [site.value for site in figures_in_chips(source)]

    def test_an_amount_a_count_and_a_percent_in_a_chip_are_found(self):
        source = (
            '<a class="chip" href="{{ url }}">{{ label|breakable }} <strong>{{ total|money }}</strong></a>'
            '<span class="pill">{{ hops|counted:"step" }}</span>'
            '<span class="scope-chip">{{ share }}%</span>'
            '<span class="tag">{{ group.count|breakable }}</span>'
        )

        self.assertEqual(self.values(source), ["total|money", 'hops|counted:"step"', "share", "group.count|breakable"])

    def test_words_in_a_chip_pass(self):
        self.assertEqual(self.values('<span class="pill pill-{{ tone }}">{{ state|breakable }}</span>'), [])

    def test_a_figure_outside_a_chip_passes(self):
        source = '<span class="pill">Open</span> <span class="count">{{ n|counted:"host" }}</span> {{ total|money }}'

        self.assertEqual(self.values(source), [])

    def test_a_chip_class_set_by_a_condition_is_read(self):
        source = '<span class="{% if serious %}pill pill-serious{% endif %}">{{ alerts|length }}</span>'

        self.assertEqual(self.values(source), ["alerts|length"])

    def test_a_class_that_only_names_a_chip_passes(self):
        self.assertEqual(self.values('<span class="pill-row chips">{{ n|length }}</span>'), [])

    def test_a_comment_passes(self):
        self.assertEqual(self.values('{% comment %}<span class="pill">{{ n|length }}</span>{% endcomment %}'), [])


@tag("source")
class TemplateChipTests(SimpleTestCase):
    def test_no_template_prints_a_figure_in_a_chip(self):
        root = Path(settings.BASE_DIR)
        found = [
            site
            for path in template_paths()
            for site in figures_in_chips(path.read_text(encoding="utf-8"), str(path.relative_to(root)))
        ]

        self.assertEqual(
            [str(site) for site in found],
            [],
            "Numbers are never chips: a figure stands in a `partials/_kpi_grid.html` cell, a numeric column or a"
            " sentence, and a chip keeps its words",
        )
