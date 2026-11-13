"""A fold that is a part of the page sits on a surface.

A `<details>` drawn straight on the page, outside every card, is a section
with no frame: its summary starts at the page's edge, where every other
section's heading starts inside its card's padding, and what it opens spills
onto the background. So a page-level fold is a `{% card element="details" %}`
or sits inside one. Each page is read with every template it includes in
place, an extension's too, so a partial is judged where a page draws it.
"""

import re
from html.parser import HTMLParser
from typing import override

from django.test import SimpleTestCase, tag

from hq.platform.application.tests.source import template_files

# What draws a surface: the card tag, and the frames a few parts draw for
# themselves (a fact's metric, a command's form). A child of a section flow or a column pair is framed by its parent.
FRAMES = frozenset(("card", "form"))
FRAMING_PARENTS = frozenset(("section-flow", "two-col"))
CARD_TAG = "hq-card"

_COMMENT = re.compile(r"\{%-?\s*comment\b.*?%\}.*?\{%-?\s*endcomment\s*-?%\}|\{#.*?#\}", re.DOTALL)
_EXTENDS = re.compile(r"""\{%-?\s*extends\s+(["'])([^"']+)\1\s*-?%\}""")
_BLOCK = re.compile(r"\{%-?\s*(block|endblock)\b\s*(\w*).*?-?%\}", re.DOTALL)
_PARTIALDEF = re.compile(r"\{%-?\s*partialdef\s+(\w+)(\s+inline)?\s*-?%\}(.*?)\{%-?\s*endpartialdef\b.*?%\}", re.DOTALL)
_PARTIAL = re.compile(r"\{%-?\s*partial\s+(\w+)\s*-?%\}")
_INCLUDE = re.compile(r"""\{%-?\s*include\s+(["'])([^"'#]+)(?:#(\w+))?\1.*?-?%\}""", re.DOTALL)
_TAG = re.compile(r"\{%-?\s*(\w+).*?-?%\}", re.DOTALL)
_VALUE = re.compile(r"\{\{.*?\}\}", re.DOTALL)
_VOID = frozenset(
    ("area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr")
)


def _blocks(source: str) -> list[tuple[str, int, int, int, int]]:
    """Each outermost block: its name, where its tags start and end, and
    where its content starts and ends."""

    found, opened = [], []
    for match in _BLOCK.finditer(source):
        if match.group(1) == "block":
            opened.append((match.group(2), match.start(), match.end()))
        elif opened:
            name, start, inner = opened.pop()
            if not opened:
                found.append((name, start, inner, match.start(), match.end()))
    return found


def _named_blocks(source: str) -> dict[str, str]:
    """Every block a template declares, nested ones too, by name."""

    named = {}
    for name, _, inner, close, _ in _blocks(source):
        named[name] = source[inner:close]
        named |= _named_blocks(source[inner:close])
    return named


def _filled(source: str, given: dict[str, str]) -> str:
    """The source with each block drawn as the most derived template that
    declares it says, `block.super` as its own content."""

    drawn, at = [], 0
    for name, start, inner, close, end in _blocks(source):
        own = source[inner:close]
        chosen = given.get(name, own).replace("{{ block.super }}", own)
        drawn += [source[at:start], _filled(chosen, given)]
        at = end
    return "".join([*drawn, source[at:]])


def _drawn(name: str, sources: dict[str, str], seen: frozenset[str] = frozenset(), part: str | None = None) -> str:
    """A template's markup as a page draws it: each literal include drawn in
    its place, its parent drawn around its blocks, and each card as one
    element. Every other tag and value drops out, so both branches of a
    condition are read."""

    source = _COMMENT.sub("", sources[name])
    given: dict[str, str] = {}
    while (parent := _EXTENDS.search(source)) and parent.group(2) in sources:
        given = _named_blocks(source) | given
        source = _COMMENT.sub("", sources[parent.group(2)])
    source = _filled(source, given)
    defined = {match.group(1): match.group(3) for match in _PARTIALDEF.finditer(source)}
    if part is not None:
        source = defined.get(part, "")
    source = _PARTIALDEF.sub(lambda match: match.group(3) if match.group(2) else "", source)
    source = _PARTIAL.sub(lambda match: defined.get(match.group(1), ""), source)

    def include(match: re.Match) -> str:
        included = match.group(2)
        if included not in sources or included in seen:
            return ""
        return _drawn(included, sources, seen | {name}, match.group(3))

    source = _INCLUDE.sub(include, source)
    source = _TAG.sub(
        lambda match: {
            "card": f"<{CARD_TAG}>",
            "endcard": f"</{CARD_TAG}>",
            "section": f"<{CARD_TAG}>",
            "endsection": f"</{CARD_TAG}>",
        }.get(match.group(1), ""),
        source,
    )
    return _VALUE.sub("x", source)


class _Folds(HTMLParser):
    """Each `<details>` drawn on no surface, by its class."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.open: list[tuple[str, frozenset[str]]] = []
        self.bare: list[str] = []

    def framed(self, classes: frozenset[str]) -> bool:
        if classes & FRAMES:
            return True
        for index, (element, held) in enumerate(self.open):
            if element == CARD_TAG or held & FRAMES:
                return True
            if index and self.open[index - 1][1] & FRAMING_PARENTS:
                return True
        return False

    @override
    def handle_starttag(self, tag, attrs):
        if tag in _VOID:
            return
        classes = frozenset((dict(attrs).get("class") or "").split())
        if tag == "details" and not self.framed(classes):
            self.bare.append(".".join(("details", *sorted(classes))))
        self.open.append((tag, classes))

    @override
    def handle_endtag(self, tag):
        for index in range(len(self.open) - 1, -1, -1):
            if self.open[index][0] == tag:
                del self.open[index:]
                return


def bare_folds(name: str, sources: dict[str, str]) -> list[str]:
    """Each fold the page `name` draws outside every surface."""

    folds = _Folds()
    folds.feed(_drawn(name, sources))
    return folds.bare


def _pages(sources: dict[str, str]) -> list[str]:
    return [name for name, source in sources.items() if re.search(r"\{%-?\s*extends\b", source)]


class FoldRuleTests(SimpleTestCase):
    """What the check reads as a fold on no surface."""

    def test_a_fold_on_the_page_is_found(self):
        sources = {
            "page.html": '{% extends "base.html" %}{% block content %}<details class="raw"><summary>Raw</summary></details>{% endblock %}'
        }
        self.assertEqual(bare_folds("page.html", sources), ["details.raw"])

    def test_a_fold_in_a_bare_section_is_found(self):
        sources = {
            "page.html": '<section class="page-section"><h2>Names</h2><details><summary>3</summary></details></section>'
        }
        self.assertEqual(bare_folds("page.html", sources), ["details"])

    def test_a_fold_a_page_includes_is_read_where_the_page_draws_it(self):
        sources = {
            "page.html": '{% include "partials/fold.html" %}{% card %}{% include "partials/fold.html" with x=1 %}{% endcard %}',
            "partials/fold.html": '<details class="more"><summary>More</summary></details>',
        }
        self.assertEqual(bare_folds("page.html", sources), ["details.more"])

    def test_a_fold_on_a_surface_passes(self):
        sources = {
            "page.html": (
                '{% card element="details" css="section-fold" %}<summary><h2>Setup</h2></summary>{% endcard %}'
                "{% card %}<h2>Hosts</h2><details><summary>2 hidden</summary></details>{% endcard %}"
                '<div class="section-flow"><section class="page-section"><details><summary>Readout</summary></details></section></div>'
                "{% comment %}<details></details>{% endcomment %}"
            ),
        }
        self.assertEqual(bare_folds("page.html", sources), [])


@tag("source", "tree")
class TemplateFoldTests(SimpleTestCase):
    def test_every_page_level_fold_sits_on_a_surface(self):
        sources = {name: path.read_text(encoding="utf-8") for name, path in template_files()}
        found = [f"{name}: {fold}" for name in _pages(sources) for fold in bare_folds(name, sources)]
        self.assertEqual(found, [], 'a fold on the page is a {% card element="details" %}, or sits inside a card')
