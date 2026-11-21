"""A template's source as the hand-drawn shapes read it (``hand_drawn``):
its tags and their attributes, its text outside comments and tags, each
offset's line, and the finders a shape is built from."""

import re
from collections.abc import Callable, Iterator
from functools import cached_property
from html.parser import HTMLParser
from typing import override

from django.template.base import tag_re

# A template comment, which no page receives, and a template tag or value.
_COMMENT_TAG = re.compile(r"\{%-?\s*(end)?comment\b.*?%\}", re.DOTALL)
_DJANGO = re.compile(r"\{%.*?%\}|\{\{.*?\}\}", re.DOTALL)
# A `{#` its line never closes, which Django prints as text.
OPEN_COMMENT = re.compile(r"\{#(?![^\n]*#\})")
# An element's start tag, read where template tags are masked; an HTML
# comment or an end tag, which bound a run of text too.
_START = re.compile(r"<([a-zA-Z][\w-]*)\b[^<>]*>")
_BOUND = re.compile(r"<!--.*?-->|</?[a-zA-Z][^<>]*>", re.DOTALL)
_RAW = re.compile(r"<(script|style)\b[^<>]*>.*?</\1\s*>", re.DOTALL | re.IGNORECASE)
_ENTITY = re.compile(r"&(?:#\d+|#x[0-9a-fA-F]+|\w+);")


def blank(text: str) -> str:
    return re.sub(r"[^\n]", " ", text)


class Source:
    """A template's source as its shapes read it: its comments blanked, so a
    position is still its line, and its start tags and runs of text found
    with every template tag and value masked."""

    def __init__(self, source: str) -> None:
        pieces, commented = [], False
        for index, piece in enumerate(tag_re.split(source)):
            if index % 2:
                if piece.startswith("{#"):
                    piece = blank(piece)
                elif match := _COMMENT_TAG.fullmatch(piece):
                    commented, piece = not match.group(1), blank(piece)
            pieces.append(blank(piece) if commented else piece)
        self.text = "".join(pieces)
        self.masked = _DJANGO.sub(lambda match: re.sub(r"[^\n]", "\0", match.group()), self.text)

    def line(self, at: int) -> int:
        return self.text.count("\n", 0, at) + 1

    @cached_property
    def tags(self) -> list[tuple[str, int, int]]:
        """Each start tag: its element's name, lowercased, and its span."""

        return [(match.group(1).lower(), *match.span()) for match in _START.finditer(self.masked)]

    @cached_property
    def runs(self) -> list[list[tuple[int, str]]]:
        """Each run of text between two tags, outside a script or a style,
        as its pieces split at each entity: a piece's position and its text,
        stripped; an entity is a piece of its own."""

        masked = _RAW.sub(lambda match: blank(match.group()), self.masked)
        bounds = [match.span() for match in _BOUND.finditer(masked)]
        found = []
        for start, end in zip(
            [0, *(end for _, end in bounds)], [*(start for start, _ in bounds), len(masked)], strict=True
        ):
            pieces, at = [], start
            for part in re.split(f"({_ENTITY.pattern})", self.text[start:end]):
                if stripped := part.strip():
                    pieces.append((at + part.index(stripped[0]), stripped))
                at += len(part)
            if pieces:
                found.append(pieces)
        return found

    def at(self, line: int) -> int:
        """Where the line ``line`` starts."""

        return sum(len(each) + 1 for each in self.text.split("\n")[: line - 1])

    @cached_property
    def marked(self) -> tuple[str, list[str]]:
        """The source with each template tag dropped, its lines kept, and each
        value marked by its place among the values, with the values."""

        values: list[str] = []

        def mark(match: re.Match[str]) -> str:
            if match.group().startswith("{%"):
                return "\n" * match.group().count("\n")
            values.append(match.group()[2:-2].strip())
            return f"\0{len(values) - 1}\0"

        return _DJANGO.sub(mark, self.text), values

    def walked(self, visit: Callable[[list[tuple[str, dict]], str], Iterator[int]]) -> Iterator[int]:
        """Each place ``visit`` finds in a run of text, given the elements
        open around it, both sides of each condition read."""

        walk = _Walk(visit)
        walk.feed(self.marked[0])
        walk.close()
        return (self.at(line) for line in walk.seen)


_VOID = frozenset(
    ("area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr")
)


class _Walk(HTMLParser):
    def __init__(self, visit: Callable[[list[tuple[str, dict]], str], Iterator[int]]) -> None:
        super().__init__(convert_charrefs=True)
        self.open: list[tuple[str, dict]] = []
        self.visit = visit
        self.seen: list[int] = []

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in _VOID:
            self.open.append((tag, dict(attrs)))

    @override
    def handle_endtag(self, tag: str) -> None:
        for at in range(len(self.open) - 1, -1, -1):
            if self.open[at][0] == tag:
                del self.open[at:]
                return

    @override
    def handle_data(self, data: str) -> None:
        line = self.getpos()[0]
        self.seen += [line + data.count("\n", 0, offset) for offset in self.visit(self.open, data)]


type Find = Callable[[Source], Iterator[int]]


def matched(pattern: str, flags: int = 0) -> Find:
    """Each match of ``pattern`` in the source."""

    compiled = re.compile(pattern, flags)
    return lambda source: (match.start() for match in compiled.finditer(source.text))


def tagged(name: str, attributes: str = "", *, without: str = "") -> Find:
    """Each start tag of an element whose name is ``name``, holding
    ``attributes`` and not ``without`` where given, each a pattern over the
    tag as written."""

    named, holding, lacking = re.compile(f"(?:{name})"), re.compile(attributes), re.compile(without)

    def find(source: Source) -> Iterator[int]:
        for element, start, end in source.tags:
            written = source.text[start:end]
            if named.fullmatch(element) and holding.search(written) and not (without and lacking.search(written)):
                yield start

    return find


def within(name: str, pattern: str) -> Find:
    """Each element ``name`` whose content up to its end tag holds ``pattern``."""

    held = re.compile(pattern)

    def find(source: Source) -> Iterator[int]:
        for element, start, end in source.tags:
            if element == name:
                closing = source.masked.find(f"</{name}", end)
                if held.search(source.text, end, len(source.text) if closing < 0 else closing):
                    yield start

    return find
