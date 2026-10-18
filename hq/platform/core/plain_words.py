"""The words HQ never shows its owner, read from source.

The owner runs these machines and knows them. He does not know how HQ is built
and should never need to, so the names HQ uses for its own parts stay in the
code and out of the pages. ``NEVER_SHOWN`` is that vocabulary and what to say
instead; ``RETIRED_NAMES`` is every label that was replaced by one name for
one thing. ``read`` finds either in the text a person sees:

- in a template: the text between tags, the attributes a browser shows or
  reads out, and the sentences a tag is handed as a literal;
- in Python: a string literal with words in it that is not a docstring.

Never a comment, an identifier, a route name, a path or a class name: those
may keep the internal vocabulary. Like ``interface_text``, a gate reads this
and a running HQ never does.
"""

import ast
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import override

from .interface_text import TEMPLATE_COMMENT, docstrings

# Each word with the forms it takes, and what a page says instead.
NEVER_SHOWN: tuple[tuple[str, str], ...] = (
    (r"kinds?", "the thing's own name, or 'type'"),
    (r"sweeps?|swept|sweeping", "read"),
    (r"reconcil\w*|converg\w*", "apply, restore"),
    (r"declarations?|declared?|declares|declaring|desired", "what HQ expects, HQ's copy"),
    (r"specs?", "settings"),
    (r"observed|observations?", "live, what was read"),
    (r"generations?|revisions?", "nothing: never shown"),
    (r"drift(?:ed|s|ing)?", "changed outside HQ"),
    (r"providers?", "the service's own name, or 'service'"),
    (r"(?:managed )?resources?", "the thing's own noun, or 'record'"),
    (r"(?:an?|the|this|that|each|every|one|no) finding|findings", "problem, or just say the thing"),
    (r"claim(?:s|ed)?|raise[sd]?", "nothing: never shown"),
    (r"standing|facets?|projections?|lineage|capabilit(?:y|ies)|abilit(?:y|ies)", "what it means in that place"),
    (r"consumers?", "what uses it"),
    (r"render(?:s|ed|ing|er|ers)?", "written, refreshed"),
    (r"mint(?:s|ed|ing)?", "issue"),
)
# Names that were replaced by one name for one thing, matched as written.
RETIRED_NAMES: tuple[tuple[str, str], ...] = (
    ("Action items", "Needs you"),
    ("Command Center", "Search"),
    ("Command center", "Search"),
    ("Refresh now", "Read now"),
    ("Request fresh sweep", "Check again"),
    ("Refresh controller readings", "Read now"),
    ("Read now and check again", "Check again"),
)
# How HQ is configured, which a page never asks its owner to type: a setting's
# name, or the value to put in a credential's field. Matched as written, and
# in code too, where an identifier is otherwise allowed.
NEVER_ASKED: tuple[tuple[str, str], ...] = (
    (r"SEVERINO_[A-Z0-9_]+", "what the setting does: 'off in HQ's settings'"),
    (r"manages to 1", "what it lets HQ do, and where the connection's guide says how"),
)
# "Findings" is the name of a page, and stays one.
_PAGE_NAMES = re.compile(r"\bFindings\b")
_NEVER = tuple(
    (re.compile(rf"(?<![\w-])(?:{forms})(?![\w-])", re.IGNORECASE), instead) for forms, instead in NEVER_SHOWN
)
_ASKED = tuple((re.compile(rf"(?<![\w-])(?:{forms})(?![\w-])"), instead) for forms, instead in NEVER_ASKED)
_TEMPLATE_TAG = re.compile(r"\{%.*?%\}|\{\{.*?\}\}", re.DOTALL)
_TAG_LITERAL = re.compile(r"""(["'])((?:(?!\1).)*)\1""", re.DOTALL)
# What a browser shows or reads out from an element, besides its text.
_SHOWN_ATTRIBUTES = frozenset(
    {
        "title",
        "aria-label",
        "placeholder",
        "alt",
        "data-fragment-failure",
        "data-fragment-busy",
        "data-submit-label",
        "data-empty",
        "data-tip",
    }
)
# An identifier, a route, a path or a class list: lower case, no sentence in it.
_IDENTIFIER = re.compile(r"[a-z0-9_.:/#?&=%\- ]*\Z")
# A type written as a string, "Name | None": not a sentence.
_ANNOTATION = re.compile(r"[A-Z]\w*(?: \| [A-Za-z]\w*)+")
_UNSEEN_ELEMENTS = frozenset({"script", "style"})
# Shown, but as an identifier, which may keep HQ's own names.
_IDENTIFIER_ELEMENTS = frozenset({"code", "pre", "kbd"})


@dataclass(frozen=True, slots=True)
class Found:
    path: Path
    line: int
    word: str
    instead: str
    text: str

    def __str__(self) -> str:
        return f'{self.path}:{self.line}: "{self.word}" in "{self.text}" (say: {self.instead})'


def words_in(text: str) -> Iterator[tuple[str, str]]:
    """Each never-shown word or retired name in ``text``, with what to say."""

    for name, instead in RETIRED_NAMES:
        if name in text:
            yield name, instead
    plain = _PAGE_NAMES.sub(" ", text)
    for pattern, instead in _NEVER:
        match = pattern.search(plain)
        if match:
            yield match.group(), instead
    yield from asked_in(text)


def asked_in(text: str) -> Iterator[tuple[str, str]]:
    """Each piece of HQ's configuration ``text`` asks its owner to type."""

    for pattern, instead in _ASKED:
        match = pattern.search(text)
        if match:
            yield match.group(), instead


def _is_prose(text: str) -> bool:
    """Words a person reads: not an identifier, a path or a list of classes."""

    stripped = text.strip()
    if not stripped or stripped.endswith((".html", ".py", ".js", ".css")):
        return False
    if _ANNOTATION.fullmatch(stripped):
        return False
    return not _IDENTIFIER.match(stripped)


class _Shown(HTMLParser):
    """The text and the shown attributes of a template, with their lines, and
    apart from them the identifiers it shows."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.found: list[tuple[int, str]] = []
        self.identifiers: list[tuple[int, str]] = []
        self._unseen = 0
        self._identifier = 0

    @override
    def handle_starttag(self, tag, attrs):
        if tag in _UNSEEN_ELEMENTS:
            self._unseen += 1
        if tag in _IDENTIFIER_ELEMENTS:
            self._identifier += 1
        for name, value in attrs:
            if name in _SHOWN_ATTRIBUTES and value:
                self.found.append((self.getpos()[0], value))

    @override
    def handle_endtag(self, tag):
        if tag in _UNSEEN_ELEMENTS and self._unseen:
            self._unseen -= 1
        if tag in _IDENTIFIER_ELEMENTS and self._identifier:
            self._identifier -= 1

    @override
    def handle_data(self, data):
        if self._unseen or not data.strip():
            return
        (self.identifiers if self._identifier else self.found).append((self.getpos()[0], data))


def _blank(match: re.Match) -> str:
    """The same lines, with nothing on them: positions after it stay true."""

    return "\n" * match.group().count("\n")


def _shown(source: str) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
    uncommented = TEMPLATE_COMMENT.sub(_blank, source)
    shown: list[tuple[int, str]] = []
    for tag in _TEMPLATE_TAG.finditer(uncommented):
        line = uncommented.count("\n", 0, tag.start()) + 1
        shown.extend(
            (line, literal.group(2)) for literal in _TAG_LITERAL.finditer(tag.group()) if _is_prose(literal.group(2))
        )
    parser = _Shown()
    parser.feed(_TEMPLATE_TAG.sub(_blank, uncommented))
    parser.close()
    shown.extend(parser.found)
    return sorted(shown), parser.identifiers


def template_text(source: str) -> list[tuple[int, str]]:
    """``(line, text)`` for everything in a template a person sees as words."""

    return _shown(source)[0]


def template_identifiers(source: str) -> list[tuple[int, str]]:
    """``(line, text)`` for what a template shows as an identifier (code, pre, kbd)."""

    return _shown(source)[1]


# Calls whose words go to a log or to whoever is writing the code, never to a page.
_UNSEEN_CALLS = frozenset(
    {
        "debug",
        "info",
        "warning",
        "error",
        "exception",
        "critical",
        "log",
        "getLogger",
        "ImproperlyConfigured",
        "RuntimeError",
        "TypeError",
        "AssertionError",
        "NotImplementedError",
        "KeyError",
        "LookupError",
        "compile",
        "CheckMessage",
        "Error",
        "Warning",
        "add_argument",
        "CommandError",
        "SuspiciousOperation",
        "Http404",
        # A shared value's description is the controller contract's, read by its author.
        "SharedValue",
    }
)


def _unseen_strings(tree: ast.AST) -> set[int]:
    unseen = docstrings(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if name in _UNSEEN_CALLS:
            unseen.update(id(inner) for inner in ast.walk(node))
    return unseen


def python_text(source: str) -> list[tuple[int, str]]:
    """``(line, text)`` for every string in a module that holds words for a person."""

    try:
        tree = ast.parse(source)
    except SyntaxError, ValueError:
        return []
    unseen = _unseen_strings(tree)
    return sorted(
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in unseen
        and " " in node.value.strip()
        and _is_prose(node.value)
    )


# This module spells the words out, which is the one place they belong.
_SELF = Path(__file__).resolve()


def _files(paths: Iterable[Path]) -> Iterator[Path]:
    for path in paths:
        if path.is_dir():
            yield from sorted(
                found
                for found in path.rglob("*")
                if found.suffix in (".html", ".py")
                and "tests" not in found.parts
                and "migrations" not in found.parts
                and not found.name.startswith("test")
                and found.resolve() != _SELF
            )
        elif path.exists():
            yield path
        else:
            # A path that matches nothing is a moved file, and a rule that
            # reads nothing passes.
            raise FileNotFoundError(f"{path} is listed to be read and is not there.")


def read(paths: Iterable[Path], *, root: Path) -> tuple[list[Found], int]:
    """Every never-shown word in what ``paths`` show a person, and how many
    files were read. A directory is read whole."""

    found: list[Found] = []
    files = 0
    for path in _files(paths):
        source = path.read_text(encoding="utf-8", errors="replace")
        files += 1
        if path.suffix == ".html":
            words, identifiers = _shown(source)
        else:
            words, identifiers = python_text(source), []
        for reading, shown in ((words_in, words), (asked_in, identifiers)):
            for line, text in shown:
                for word, instead in reading(text):
                    found.append(Found(path.relative_to(root), line, word, instead, " ".join(text.split())[:90]))
    return found, files
