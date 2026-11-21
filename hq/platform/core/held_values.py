"""Every value a template prints as text holds the names in it, because the
loader writes the hold into the template as it loads.

A browser may break a line after any hyphen, so "example-docker-host-01"
printed bare can split into "example-docker-" and "host-01". ``breakable``
holds a name's parts and leaves a plain word, a number and markup as they
are, so it is safe on any value. The engine reads a template as HTML as it
compiles it from a file (template_engine's loaders), or from a string that
draws a part as a page does (``template``), and ends each value printed where text belongs in
``|breakable``, with the mark the template writes straight after it
(``{{ host }}.``) taken into the hold, so the mark never starts a line alone.
A value printed in a tag (an attribute), a comment, or an element whose text
stays plain (``PLAIN_TEXT``) is left as Django prints it. Django has no hook
in a value's output (``VariableNode.render`` calls
``render_value_in_context`` itself), so the hold is written into the source.
"""

import re
from collections.abc import Callable

from django.template import Engine, Template
from django.template.base import DebugLexer, TokenType

# Elements whose text is never a token: a span inside one is not markup, is
# not drawn (an SVG drawing), or is not shown on the page; and a `<time>`,
# whose text is a moment as `moments.py` writes it.
PLAIN_TEXT = {"title", "option", "textarea", "script", "style", "svg", "time"}

_TAG = re.compile(r"<(/?)([A-Za-z][\w-]*)")
# The marks a sentence puts straight after a value.
_MARK = re.compile(r"[.,;:!?)\]]+")
_EXTENDS = re.compile(r"""extends\s+["']([^"']+)["']""")


class _Markup:
    """Where a template's text stands as HTML: inside a tag, a comment, or an
    element whose text stays plain."""

    def __init__(self):
        self.in_tag = False
        self.quote = ""
        self.in_comment = False
        self.open: list[str] = []
        self._tag = ""

    def read(self, text: str) -> None:
        at = 0
        while at < len(text):
            at = self._step(text, at)

    def _step(self, text: str, at: int) -> int:
        if self.in_comment:
            end = text.find("-->", at)
            self.in_comment = end < 0
            return len(text) if end < 0 else end + 3
        if self.in_tag:
            return self._in_tag(text, at)
        if text.startswith("<!--", at):
            self.in_comment = True
            return at + 4
        if found := _TAG.match(text, at):
            self.in_tag = True
            self._tag = found.group(1) + found.group(2).lower()
            return found.end()
        next_tag = text.find("<", at + 1)
        return len(text) if next_tag < 0 else next_tag

    def _in_tag(self, text: str, at: int) -> int:
        char = text[at]
        if self.quote:
            self.quote = "" if char == self.quote else self.quote
        elif char in "\"'":
            self.quote = char
        elif char == ">":
            self.in_tag = False
            self._closed()
        return at + 1

    def _closed(self) -> None:
        name = self._tag.lstrip("/")
        if name not in PLAIN_TEXT:
            return
        if self._tag.startswith("/"):
            if name in self.open:
                self.open.remove(name)
        else:
            self.open.append(name)

    @property
    def in_text(self) -> bool:
        return not (self.in_tag or self.in_comment or self.open)


def template(source: str) -> Template:
    """A template written in Python that draws a part as a page does (a
    test's, a catalog entry's), held as a loaded one is. One Django builds
    from a string is not ours, so it is held no more than the print gate
    refuses its values (print_gate)."""

    engine = Engine.get_default()
    return engine.from_string(held(source, engine))


def held(source: str, engine: Engine, name: str = "") -> str:
    """``source``, the template ``name`` of ``engine``, with each value it
    prints as text ending in ``|breakable``."""

    return _read(source, lambda: _plain_blocks(engine, _parent(source), name))[0]


def _parent(source: str) -> str | None:
    """The template ``source`` extends, when it names it."""

    for token in DebugLexer(source).tokenize():
        if token.token_type is TokenType.BLOCK:
            found = _EXTENDS.match(token.contents.strip())
            return found[1] if found else None
        if token.token_type is TokenType.TEXT and token.contents.strip():
            return None
    return None


def _plain_blocks(engine: Engine, parent: str | None, name: str) -> frozenset[str]:
    """The blocks ``parent`` and the templates it extends open where text is
    plain, which the template ``name`` fills as text that stays plain."""

    if not parent or parent == name:
        return frozenset()
    source = engine.get_template(parent).source
    return _read(source, lambda: _plain_blocks(engine, _parent(source), parent))[1]


def _read(source: str, inherited: Callable[[], frozenset[str]]) -> tuple[str, frozenset[str]]:
    """``source`` held, and the blocks it opens where text is plain, with
    ``inherited`` naming its parent's."""

    markup = _Markup()
    tokens = DebugLexer(source).tokenize()
    plain: set[str] = set()
    blocks: list[str] = []
    out: list[str] = []
    commented = False
    for index, token in enumerate(tokens):
        start, end = token.position
        text = source[start:end]
        if token.token_type is TokenType.BLOCK:
            word, _, rest = token.contents.strip().partition(" ")
            commented = word == "comment" or (commented and word != "endcomment")
            if word == "block":
                blocks.append(rest.strip())
                if not markup.in_text or rest.strip() in inherited():
                    plain.add(rest.strip())
            elif word == "endblock" and blocks:
                blocks.pop()
        elif token.token_type is TokenType.TEXT and not commented:
            markup.read(text)
        elif token.token_type is TokenType.VAR and not commented and markup.in_text and not plain.intersection(blocks):
            text = _hold(token.contents, tokens, index, source)
        out.append(text)
    return "".join(out), frozenset(plain)


def _hold(contents: str, tokens, index: int, source: str) -> str:
    following = tokens[index + 1] if index + 1 < len(tokens) else None
    mark = following and following.token_type is TokenType.TEXT and _MARK.match(following.contents)
    if not mark:
        return f"{{{{ {contents}|breakable }}}}"
    # The mark moves into the hold: the next token's text starts after it.
    start, end = following.position
    following.position = (start + mark.end(), end)
    following.contents = following.contents[mark.end() :]
    return f'{{{{ {contents}|breakable:"{mark.group()}" }}}}'
