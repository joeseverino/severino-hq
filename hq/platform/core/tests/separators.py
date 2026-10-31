"""Where a template draws a mark between or beside words, read from its
source: each mark typed as text a reader would hear, and each held mark
(`{% dot %}`, `{% arrow %}`) a line could start with, followed down every
way through the template its conditions allow."""

import functools
import operator
import re
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from html.parser import HTMLParser
from typing import Any

from django.template import engines
from django.template.base import Lexer, Node, Origin, Template, TextNode, TokenType, Variable, VariableNode
from django.template.defaulttags import (
    AutoEscapeControlNode,
    CommentNode,
    FilterNode,
    ForNode,
    IfNode,
    LoadNode,
    PartialDefNode,
    SpacelessNode,
    WithNode,
)
from django.template.library import SimpleNode
from django.template.loader_tags import BlockNode, ExtendsNode, IncludeNode

from hq.platform.core.templatetags import value_tags

DOTS = "·•"
ARROWS = "→←↗↑↓›‹»«"

# What the text after the last part can end in: a part, a space (a line may
# break there), an edge (a block's start or a line break, where a line
# starts) or the outside (what comes before a template or block is drawn).
PART, SPACE, EDGE, OUTSIDE = "part", "space", "edge", "outside"
# Elements that start a line of their own, as their start and their end.
_BLOCKS = frozenset(
    [
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "dd",
        "details",
        "dialog",
        "div",
        "dl",
        "dt",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "summary",
        "table",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "tr",
        "ul",
    ]
)
_TAG_NAME = re.compile(r"</?([A-Za-z][\w-]*|!--)")


@dataclass(frozen=True)
class _At:
    """Where a template's output stands at one point."""

    tails: frozenset[str]
    tag: str = ""  # the element being written, while inside its brackets
    quote: str = ""

    def text(self, text: str) -> _At:
        return _scanned(self, text)

    def printed(self) -> _At:
        return self if self.tag else replace(self, tails=frozenset({PART}))

    def __or__(self, other: _At) -> _At:
        return replace(self, tails=self.tails | other.tails)


@functools.cache
def _scanned(at: _At, text: str) -> _At:
    """Where ``text`` leaves the output that stood at ``at``."""

    tails, tag, quote = at.tails, at.tag, at.quote
    position = 0
    while position < len(text):
        character = text[position]
        if tag:
            if quote:
                quote = "" if character == quote else quote
            elif character in "\"'":
                quote = character
            elif character == ">":
                tails = frozenset({EDGE}) if tag.lstrip("/").lower() in _BLOCKS else tails
                tag = ""
        elif character == "<" and (name := _TAG_NAME.match(text, position)):
            tag = name.group(0)[1:]
            position = name.end()
            continue
        else:
            tails = frozenset({SPACE if character.isspace() and character != "\xa0" else PART})
        position += 1
    return _At(tails, tag, quote)


_START = _At(frozenset({OUTSIDE}))
_ASSIGNS = ("asvar", "target_var", "var_name")


# The tags that draw a mark held to the part before them, and where each
# leaves the line: a dot may break after it, an arrow ends its part.
HELD = {value_tags.dot: SPACE, value_tags.arrow: PART}


def _held(node: Node) -> str | None:
    return HELD.get(node.func) if isinstance(node, SimpleNode) else None


def _atom(condition) -> str:
    """A condition's words, the key a fact about it is kept under."""

    if (text := getattr(condition, "text", None)) is not None:
        return text
    second = getattr(condition, "second", None)
    if second is None:
        return f"{condition.id} {_atom(condition.first)}"
    return f"({_atom(condition.first)} {condition.id} {_atom(second)})"


def _parts(condition) -> Iterator[Any]:
    """A condition and every condition it is made of."""

    yield condition
    if getattr(condition, "id", None) in {"not", "and", "or"}:
        yield from _parts(condition.first)
        if (second := getattr(condition, "second", None)) is not None:
            yield from _parts(second)


def _value(condition, facts: dict[str, bool]) -> bool | None:
    """Whether ``condition`` holds, by what the path has already passed; None
    when that does not say."""

    if (known := facts.get(_atom(condition))) is not None:
        return known
    kind = getattr(condition, "id", None)
    if kind == "not":
        value = _value(condition.first, facts)
        return None if value is None else not value
    if kind in {"and", "or"}:
        values = {_value(condition.first, facts), _value(condition.second, facts)}
        decides = kind == "or"
        if decides in values:
            return decides
        return None if None in values else not decides
    return None


def _implied(condition, *, holds: bool) -> Iterator[tuple[str, bool]]:
    """What ``condition`` holding, or not, says about itself and the
    conditions it is made of."""

    yield _atom(condition), holds
    kind = getattr(condition, "id", None)
    if kind == "not":
        yield from _implied(condition.first, holds=not holds)
    elif kind in {"and", "or"} and holds == (kind == "and"):
        yield from _implied(condition.first, holds=holds)
        yield from _implied(condition.second, holds=holds)


def _conditions(nodelist) -> Iterator[Any]:
    for node in nodelist:
        if isinstance(node, IfNode):
            yield from (condition for condition, _ in node.conditions_nodelists if condition is not None)
        for name in node.child_nodelists:
            yield from _conditions(getattr(node, name, None) or ())


def _atoms(nodelist) -> Counter[str]:
    return Counter(_atom(part) for condition in _conditions(nodelist) for part in _parts(condition))


_LOOP = frozenset({"forloop.first", "forloop.last"})


def _repeated(nodelist) -> frozenset[str]:
    """The conditions a template asks more than once, the only ones a later
    branch can depend on."""

    return frozenset(atom for atom, count in _atoms(nodelist).items() if count > 1) | _LOOP


_NAMED_VALUES = {("True",): True, ("False",): False, ("None",): False}


def _written(value) -> bool | None:
    """Whether a value written into a tag is true, or None for a variable."""

    if not isinstance(value, Variable):
        return bool(value)
    if value.literal is not None:
        return bool(value.literal)
    return _NAMED_VALUES.get(value.lookups)


def _given(node: IncludeNode, nodelist) -> frozenset[tuple[str, bool]]:
    """What an include says of the names its template asks about: a value
    written into the tag, and, with ``only``, every name it leaves out."""

    given = {
        name: written
        for name, value in node.extra_context.items()
        if not value.filters and (written := _written(value.var)) is not None
    }
    if node.isolated_context:
        given |= {
            atom: False
            for atom in _atoms(nodelist)
            if re.fullmatch(r"[\w.]+", atom) and atom.split(".")[0] not in node.extra_context
        }
    return frozenset(given.items())


# How many ways through are followed apart before those that stand at the
# same place are made one.
_APART = 32


def _merged(paths: frozenset[_Path]) -> frozenset[_Path]:
    """Past ``_APART`` ways, those that stand at the same place made one,
    knowing what all of them know, so a template of many conditions is
    followed in time; a later condition can then tell fewer of them apart."""

    if len(paths) <= _APART:
        return paths
    known: dict[_At, frozenset[tuple[str, bool]]] = {}
    for at, facts in paths:
        known[at] = known[at] & facts if at in known else facts
    return frozenset(known.items())


# One way through a template: where its output stands, and what it has
# learned about the conditions it passed.
type _Path = tuple[_At, frozenset[tuple[str, bool]]]


@dataclass
class _Flow:
    """Follows what is drawn before each held mark, down every way through a
    template its conditions allow and into every template it includes."""

    engine: Any
    found: set[tuple[str, int]] = field(default_factory=set)
    including: tuple[str, ...] = ()
    # A loop's body is followed from several starts and a partial from every
    # page that includes it, so each walk is taken once.
    walked: dict[tuple[int, _Path, bool], frozenset[_Path]] = field(default_factory=dict)
    # Each nodelist walked, kept so the id that keys its walks is never reused.
    kept: list[Any] = field(default_factory=list)
    templates: dict[str, Template] = field(default_factory=dict)
    repeated: dict[str, frozenset[str]] = field(default_factory=dict)
    given: dict[IncludeNode, frozenset[tuple[str, bool]]] = field(default_factory=dict)
    asked: frozenset[str] = _LOOP

    def template(self, template: Template) -> None:
        self.asked = _repeated(template.nodelist)
        self.walk(template.nodelist, frozenset({(_START, frozenset())}), record=True)

    def walk(self, nodelist, paths: frozenset[_Path], *, record: bool) -> frozenset[_Path]:
        self.kept.append(nodelist)
        ends: set[_Path] = set()
        for path in paths:
            key = (id(nodelist), path, record)
            if key not in self.walked:
                self.walked[key] = self.walked.get((id(nodelist), path, True)) or self._through(
                    nodelist, path, record=record
                )
            ends |= self.walked[key]
        return frozenset(ends)

    def _through(self, nodelist, path: _Path, *, record: bool) -> frozenset[_Path]:
        paths = frozenset({path})
        for node in nodelist:
            paths = _merged(self.node(node, paths, record=record))
        return paths

    def node(self, node, paths: frozenset[_Path], *, record: bool) -> frozenset[_Path]:
        if isinstance(node, IfNode):
            return frozenset(end for path in paths for end in self.branches(node, path, record=record))
        if isinstance(node, ForNode):
            return frozenset(end for path in paths for end in self.loop(node, path, record=record))
        if isinstance(node, IncludeNode):
            return frozenset(end for path in paths for end in self.include(node, path, record=record))
        if isinstance(node, (WithNode, AutoEscapeControlNode, FilterNode, SpacelessNode)):
            return self.walk(node.nodelist, paths, record=record)
        return frozenset((self.step(node, at, record=record), facts) for at, facts in paths)

    def step(self, node, at: _At, *, record: bool) -> _At:
        """Where one node that chooses nothing leaves the output."""

        if after := _held(node):
            if record and (at.tag or at.tails & {SPACE, EDGE}):
                self.found.add((node.origin.template_name, node.token.lineno))
            return replace(at, tails=frozenset({after}))
        if isinstance(node, TextNode):
            return at.text(node.s)
        if isinstance(node, VariableNode):
            return at.printed()
        if isinstance(node, (CommentNode, LoadNode)) or any(getattr(node, name, None) for name in _ASSIGNS):
            return at
        outside = frozenset({(_START, frozenset())})
        if isinstance(node, (BlockNode, ExtendsNode)):
            self.walk(node.nodelist, outside, record=record)
            return _START
        if isinstance(node, PartialDefNode):
            drawn = self.walk(node.nodelist, outside, record=record)
            return functools.reduce(operator.or_, (end for end, _ in drawn)) if node.inline else at
        for name in node.child_nodelists:
            # A tag of its own wraps what it encloses in markup of its own.
            if nodelist := getattr(node, name, None):
                self.walk(nodelist, outside, record=record)
        return at.printed()

    def branches(self, node, path: _Path, *, record: bool) -> Iterator[_Path]:
        at, facts = path
        for condition, nodelist in node.conditions_nodelists:
            holds = True if condition is None else _value(condition, dict(facts))
            if holds is False:
                continue
            taken = facts if condition is None else self.learned(facts, condition, holds=True)
            yield from self.walk(nodelist, frozenset({(at, taken)}), record=record)
            if holds:
                return
            facts = self.learned(facts, condition, holds=False)
        yield at, facts

    def learned(self, facts, condition, *, holds: bool) -> frozenset[tuple[str, bool]]:
        new = {(atom, value) for atom, value in _implied(condition, holds=holds) if atom in self.asked}
        return facts | frozenset(new)

    def loop(self, node, path: _Path, *, record: bool) -> Iterator[_Path]:
        """A loop is entered from before it on its first pass and from its own
        end on the next; what follows it follows its last pass, or its
        ``{% empty %}`` branch when it has one. What the outer template
        learned holds again after it."""

        at, facts = path
        names = {*node.loopvars, "forloop"}
        outer = frozenset(fact for fact in facts if fact[0].split(".")[0].split("|")[0] not in names)

        def passes(starts, **loop):
            known = outer | frozenset((f"forloop.{name}", value) for name, value in loop.items())
            return self.walk(node.nodelist_loop, frozenset((start, known) for start in starts), record=False)

        def ends(paths):
            return {end for end, _ in paths}

        again = {at} | ends(passes({at}))
        later = ends(passes(again, last=False))
        self.walk(node.nodelist_loop, frozenset({(at, outer | {("forloop.first", True)})}), record=record)
        self.walk(
            node.nodelist_loop, frozenset((start, outer | {("forloop.first", False)}) for start in later), record=record
        )
        after = ends(passes({at} | later, last=True))
        if node.nodelist_empty:
            after |= ends(self.walk(node.nodelist_empty, frozenset({(at, outer)}), record=record))
        yield from ((end, facts) for end in after)

    def include(self, node, path: _Path, *, record: bool) -> Iterator[_Path]:
        at, facts = path
        name = node.template.var
        if not isinstance(name, str) or name in self.including:
            yield at.printed(), facts
            return
        if name not in self.templates:
            self.templates[name] = self.engine.get_template(name)
            self.repeated[name] = _repeated(self.templates[name].nodelist)
        included = self.templates[name]
        if node not in self.given:
            self.given[node] = _given(node, included.nodelist)
        outer, self.asked, self.including = self.asked, self.repeated[name], (*self.including, name)
        try:
            ends = self.walk(included.nodelist, frozenset({(at, self.given[node])}), record=record)
        finally:
            self.asked, self.including = outer, self.including[:-1]
        yield from ((end, facts) for end, _ in ends)


def _engine():
    return engines["django"].engine  # type: ignore[attr-defined]  # the Django backend's own engine


def loose_dots(sources: dict[str, str]) -> list[str]:
    """``name:line`` of each held mark in ``sources`` (each template's source
    by its name), or a template one includes, that a line may start with:
    one drawn after a space, at a line's start or inside an element's
    brackets."""

    engine = _engine()
    flow = _Flow(engine)
    for name, source in sources.items():
        flow.template(Template(source, origin=Origin(name=name, template_name=name), engine=engine))
    return [f"{template}:{line}" for template, line in sorted(flow.found)]


class _Marks(HTMLParser):
    """Reads a template's markup, both sides of each condition, and collects
    each mark a reader would hear or a line could strand."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.open: list[tuple[str, bool]] = []
        self.found: list[tuple[int, str]] = []

    def handle_starttag(self, tag, attrs):
        if tag in {"area", "br", "col", "hr", "img", "input", "link", "meta", "source", "wbr"}:
            return
        named = dict(attrs)
        # A name given to a control replaces its text; a landmark's does not.
        renamed = tag in {"a", "button"} and bool(named.get("aria-label"))
        self.open.append((tag, named.get("aria-hidden") == "true" or renamed))

    def handle_endtag(self, tag):
        for at in range(len(self.open) - 1, -1, -1):
            if self.open[at][0] == tag:
                del self.open[at:]
                return

    def handle_data(self, data):
        tags = {tag for tag, _ in self.open}
        if tags & {"title", "script", "style"}:
            return
        line = self.getpos()[0]
        unheard = any(silent for _, silent in self.open) or "kbd" in tags
        for offset, character in enumerate(data):
            if character in DOTS or (character in ARROWS and not unheard):
                self.found.append((line + data.count("\n", 0, offset), character))


def typed_marks(source: str) -> list[tuple[int, str]]:
    """The line and the mark of each dot or arrow ``source`` lays out as
    text for a reader to hear, rather than drawing it through ``{% dot %}``,
    ``.dot-list`` or ``{% arrow %}``."""

    marked = []
    commented = False
    for token in Lexer(source).tokenize():
        if token.token_type == TokenType.BLOCK:
            word = token.contents.split()[0] if token.contents.split() else ""
            commented = word == "comment" or (commented and word != "endcomment")
            # The tag's place, its lines kept, so the reader counts the source's.
            marked.append("\n" * token.contents.count("\n"))
        elif token.token_type == TokenType.TEXT:
            marked.append(re.sub(r"[^\n]", " ", token.contents) if commented else token.contents)
        elif token.token_type == TokenType.VAR:
            marked.append("x")
    reader = _Marks()
    reader.feed("".join(marked))
    reader.close()
    return reader.found
