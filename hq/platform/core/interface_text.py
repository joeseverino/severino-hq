"""What HQ and its extensions show people: their words and markup, read as source.

An em dash in interface text is the most recognisable mark of generated prose,
and it spreads by copying. A value that is missing is shown with the
`or_empty` filter or ``MISSING``, never a literal dash.

Counts get the same treatment. A plural built by hand, Django's ``pluralize``
or ``"s" if n != 1 else ""``, agrees the noun and forgets the verb ("1 need
output"). ``counted`` takes the whole phrase for one and for many instead, and
a literal phrase that cannot agree raises the moment a page renders it with
data, so every ``|counted:"..."`` literal and every literal ``counted(...)``
call is read here first.

HTML has no nested forms: the parser drops the inner opening tag and takes the
inner closing tag as the end of the outer form, so every control after it
belongs to no form.

These are properties of the source, so a gate reads them and a running HQ
never does: ``read`` walks each tree once and parses each file once, and
``InterfaceTextTests`` (the architecture tests) fails on anything it finds.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from django.apps import apps
from django.conf import settings

EM_DASH = chr(0x2014)  # built, so this file holds no literal one
_TEMPLATE_COMMENT = re.compile(
    r"\{#.*?#\}|\{%\s*comment\s*%\}.*?\{%\s*endcomment\s*%\}", re.DOTALL
)
_SKIPPED = frozenset(
    {"tests", "migrations", "staticfiles", "node_modules", "var", "data", "docs", "deploy"}
)
_PLURALIZE = re.compile(r"\|\s*pluralize\b")
# "account(s)": a plural hedged in brackets. Only flagged where a value is
# interpolated (an f-string, a template), since a plain string such as a CSV
# column named "Duration(s)" is data, not a count.
_BRACKETED = re.compile(r"(?:^|[a-z]|\}\})\((?:s|es|ies)\)")
_SUFFIXES = frozenset({"s", "es"})
_COUNTED_FILTER = re.compile(r"""\|\s*counted\s*:\s*(["'])(.*?)\1""")
_FORM_TAG = re.compile(r"<(/?)form\b", re.IGNORECASE)


@dataclass
class Reading:
    """Every finding of one pass over the source, as ``(path, line[, why])``."""

    files: int = 0
    em_dashes: list[tuple[Path, int]] = field(default_factory=list)
    hand_plurals: list[tuple[Path, int]] = field(default_factory=list)
    nested_forms: list[tuple[Path, int]] = field(default_factory=list)
    unagreeable_counts: list[tuple[Path, int, str]] = field(default_factory=list)


def _roots() -> list[Path]:
    """HQ's checkout, then each extension that is not installed as a package."""
    base = Path(settings.BASE_DIR).resolve()
    roots = [base]
    for config in apps.get_app_configs():
        path = Path(config.path).resolve()
        if "site-packages" in path.parts or path.is_relative_to(base):
            continue
        roots.append(path)
    return roots


def _plugin_roots() -> list[Path]:
    """Each installed extension's top-level package, wherever it is installed."""
    from hq.platform.application.plugins import installed_plugin_apps

    roots = []
    for name in installed_plugin_apps():
        spec = importlib.util.find_spec(name.split(".")[0])
        locations = (spec.submodule_search_locations or ()) if spec else ()
        roots.extend(Path(location).resolve() for location in locations)
    return roots


def _outermost(roots: list[Path]) -> list[Path]:
    """Each root once. A root inside another is dropped: the outer walk reads it."""
    kept: list[Path] = []
    for root in sorted(set(roots), key=lambda path: len(path.parts)):
        if not any(root.is_relative_to(outer) for outer in kept):
            kept.append(root)
    return kept


def _files(root: Path) -> Iterator[Path]:
    """The templates and Python a root shows people, skipped trees never entered."""
    for directory, names, files in os.walk(root):
        names[:] = sorted(
            name for name in names if name not in _SKIPPED and not name.startswith(".")
        )
        for name in sorted(files):
            if name.endswith((".html", ".py")) and not name.startswith(("test", ".")):
                yield Path(directory, name)


def _text(path: Path) -> str | None:
    """A file's text, or None when it cannot be read: one bad file hides no other."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _refusal(one: str, many: str | None) -> str | None:
    from hq.platform.application.ui import counted_forms

    try:
        counted_forms(one, many)
    except ValueError as error:
        return str(error)
    return None


def _template_counts(path: Path, number: int, line: str, reading: Reading) -> None:
    from hq.platform.core.templatetags.value_tags import counted_phrases

    for match in _COUNTED_FILTER.finditer(line):
        reason = _refusal(*counted_phrases(match.group(2)))
        if reason:
            reading.unagreeable_counts.append((path, number, reason))


def _template_forms(path: Path, number: int, line: str, depth: int, reading: Reading) -> int:
    """The form depth after ``line``; a form opened inside an open one is found."""
    for match in _FORM_TAG.finditer(line):
        if match.group(1):
            depth = max(depth - 1, 0)
        else:
            depth += 1
            if depth > 1:
                reading.nested_forms.append((path, number))
    return depth


def _read_template(path: Path, source: str, worded: bool, reading: Reading) -> None:
    text = _TEMPLATE_COMMENT.sub(lambda match: "\n" * match.group().count("\n"), source)
    depth = 0
    for number, line in enumerate(text.splitlines(), 1):
        _template_counts(path, number, line, reading)
        if not worded:
            continue
        if EM_DASH in line:
            reading.em_dashes.append((path, number))
        if _PLURALIZE.search(line) or _BRACKETED.search(line):
            reading.hand_plurals.append((path, number))
        depth = _template_forms(path, number, line, depth, reading)


def _docstrings(tree: ast.AST) -> set[int]:
    owners = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    return {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, owners)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }


def _is_suffix_guess(node: ast.AST) -> bool:
    """``"s" if n != 1 else ""`` and its mirror: a plural guessed from a letter."""
    if not isinstance(node, ast.IfExp):
        return False
    branches = {node.body, node.orelse}
    values = {b.value for b in branches if isinstance(b, ast.Constant)}
    return len(values) == 2 and "" in values and bool(values & _SUFFIXES)


def _is_bracketed_plural(node: ast.AST) -> bool:
    return isinstance(node, ast.JoinedStr) and any(
        isinstance(part, ast.Constant) and _BRACKETED.search(str(part.value))
        for part in node.values
    )


def _literal(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _counted_call_phrases(node: ast.AST) -> tuple[str, str | None] | None:
    """``(one, many)`` of a ``counted(n, "one"[, "many"])`` call with literal words."""
    if not isinstance(node, ast.Call):
        return None
    name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
    if name != "counted" or len(node.args) < 2:
        return None
    one = _literal(node.args[1])
    if one is None:
        return None
    keywords = {keyword.arg: keyword.value for keyword in node.keywords}
    many_node = node.args[2] if len(node.args) > 2 else keywords.get("many")
    if many_node is not None and _literal(many_node) is None:
        return None
    return one, _literal(many_node)


def _has_em_dash(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str) and EM_DASH in node.value


def _read_python(path: Path, source: str, worded: bool, reading: Reading) -> None:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return
    docstrings = _docstrings(tree) if worded else set()
    for node in ast.walk(tree):
        phrases = _counted_call_phrases(node)
        reason = _refusal(*phrases) if phrases else None
        if reason:
            reading.unagreeable_counts.append((path, node.lineno, reason))
        if not worded:
            continue
        if _has_em_dash(node) and id(node) not in docstrings:
            reading.em_dashes.append((path, node.lineno))
        if _is_suffix_guess(node) or _is_bracketed_plural(node):
            reading.hand_plurals.append((path, node.lineno))


def read() -> Reading:
    """One pass over HQ and its extensions: each tree walked and each file parsed once.

    Wording and markup are read in HQ and in each extension checked out beside
    it. ``counted`` phrases are read in those and in every installed extension
    as well, because a phrase that cannot agree fails a page wherever it lives.
    """
    worded_roots = _roots()
    reading = Reading()
    for root in _outermost(worded_roots + _plugin_roots()):
        for path in _files(root):
            source = _text(path)
            if source is None:
                continue
            reading.files += 1
            worded = any(path.is_relative_to(worded) for worded in worded_roots)
            if path.suffix == ".html":
                _read_template(path, source, worded, reading)
            else:
                _read_python(path, source, worded, reading)
    return reading
