"""What HQ and its extensions show people: their markup and counts, read as source.

Their wording is Vale's (``tools/lint/vale``, ``mise run lint:prose``). These
are properties of the source, so a gate reads them and a running HQ never does:
``read`` walks each tree once, and ``InterfaceTextTests`` (the architecture
tests) fails on anything it finds. The walk imports only the standard library
and answers JSON, so a test runs it in a process of its own (``apart``), which
gives back what its parses took when it exits.
"""

import ast
import importlib.util
import os
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

lazy from django.apps import apps
lazy from django.conf import settings

lazy from hq.platform.application import plugins
lazy from hq.platform.application.ui import counted_forms
lazy from hq.platform.core.templatetags.value_tags import counted_phrases

TEMPLATE_COMMENT = re.compile(r"\{#.*?#\}|\{%\s*comment\s*%\}.*?\{%\s*endcomment\s*%\}", re.DOTALL)
_SKIPPED = frozenset({"tests", "migrations", "staticfiles", "node_modules", "var", "data", "docs", "deploy"})
# A plural built by hand, Django's pluralize or "s" if n != 1 else "", agrees the
# noun and forgets the verb ("1 need output"); counted takes the whole phrase.
_PLURALIZE = re.compile(r"\|\s*pluralize\b")
_SUFFIXES = frozenset({"s", "es"})
_COUNTED_FILTER = re.compile(r"""\|\s*counted\s*:\s*(["'])(.*?)\1""")
_FORM_TAG = re.compile(r"<(/?)form\b", re.IGNORECASE)


@dataclass
class TextFindings:
    """Every finding of one pass over the source, as ``(path, line[, why])``."""

    files: int = 0
    hand_plurals: list[tuple[Path, int]] = field(default_factory=list)
    nested_forms: list[tuple[Path, int]] = field(default_factory=list)
    open_comments: list[tuple[Path, int]] = field(default_factory=list)
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

    roots = []
    for name in plugins.installed_plugin_apps():
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
        names[:] = sorted(name for name in names if name not in _SKIPPED and not name.startswith("."))
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
    try:
        counted_forms(one, many)
    except ValueError as error:
        return str(error)
    return None


def _template_forms(path: str, number: int, line: str, depth: int, found: dict[str, list]) -> int:
    """The form depth after ``line``; a form opened inside an open one is found.

    HTML has no nested forms: the parser drops the inner opening tag and takes
    the inner closing tag as the outer form's end, orphaning every control after.
    """
    for match in _FORM_TAG.finditer(line):
        if match.group(1):
            depth = max(depth - 1, 0)
        else:
            depth += 1
            if depth > 1:
                found["nested_forms"].append((path, number))
    return depth


def open_comment_lines(source: str) -> list[int]:
    """Each line of a template that opens ``{#`` and does not close it there.

    Django's ``{# #}`` ends at the end of its line, so one that runs onto a second
    line is printed on the page; a longer note is ``{% comment %}``.
    """
    found = []
    for number, line in enumerate(source.splitlines(), 1):
        opened = line.rfind("{#")
        if opened != -1 and "#}" not in line[opened:]:
            found.append(number)
    return found


def unclosed_template_comments(template_root: str | Path) -> list[str]:
    """Every comment left open under ``template_root``, as "page.html:line".

    For an extension's own suite, which asserts it finds none in its templates.
    """
    root = Path(template_root)
    found = []
    for path in sorted(root.rglob("*.html")):
        source = _text(path)
        if source is None:
            found.append(f"{path.relative_to(root)}: unreadable")
            continue
        found.extend(f"{path.relative_to(root)}:{number}" for number in open_comment_lines(source))
    return found


def _read_template(path: str, source: str, *, worded: bool, found: dict[str, list]) -> None:
    found["open_comments"].extend((path, number) for number in open_comment_lines(source))
    text = TEMPLATE_COMMENT.sub(lambda match: "\n" * match.group().count("\n"), source)
    depth = 0
    for number, line in enumerate(text.splitlines(), 1):
        found["counted"].extend((path, number, match.group(2)) for match in _COUNTED_FILTER.finditer(line))
        if not worded:
            continue
        if _PLURALIZE.search(line):
            found["hand_plurals"].append((path, number))
        depth = _template_forms(path, number, line, depth, found)


def _is_suffix_guess(node: ast.AST) -> bool:
    """``"s" if n != 1 else ""`` and its mirror: a plural guessed from a letter."""
    if not isinstance(node, ast.IfExp):
        return False
    branches = {node.body, node.orelse}
    values = {b.value for b in branches if isinstance(b, ast.Constant)}
    return len(values) == 2 and "" in values and bool(values & _SUFFIXES)


def _literal(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _counted_call_phrases(node: ast.AST) -> tuple[str, str | None] | None:
    """``(one, many)`` of a ``counted(n, "one"[, "many"])`` call with literal words."""
    if not isinstance(node, ast.Call):
        return None
    name = (
        node.func.id
        if isinstance(node.func, ast.Name)
        else node.func.attr
        if isinstance(node.func, ast.Attribute)
        else None
    )
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


def _read_python(path: str, source: str, *, worded: bool, found: dict[str, list]) -> None:
    try:
        tree = ast.parse(source)
    except SyntaxError, ValueError:
        return
    for node in ast.walk(tree):
        if phrases := _counted_call_phrases(node):
            found["called"].append((path, node.lineno, *phrases))
        if worded and _is_suffix_guess(node):
            found["hand_plurals"].append((path, node.lineno))


def walk(roots: list[str], worded_roots: list[str]) -> dict[str, Any]:
    """What one pass over ``roots`` finds, each file parsed once, as JSON.

    Plurals and markup are read only under ``worded_roots``. A ``counted``
    phrase is found as its words, which ``read`` judges.
    """
    found: dict[str, Any] = {
        name: [] for name in ("hand_plurals", "nested_forms", "open_comments", "counted", "called")
    }
    files = 0
    for root in roots:
        for path in _files(Path(root)):
            source = _text(path)
            if source is None:
                continue
            files += 1
            worded = any(path.is_relative_to(worded) for worded in worded_roots)
            read_file = _read_template if path.suffix == ".html" else _read_python
            read_file(str(path), source, worded=worded, found=found)
    return found | {"files": files}


def _here(run: Callable[..., Any], *args: Any) -> Any:
    return run(*args)


def read(roots: list[Path] | None = None, *, run: Callable[..., Any] = _here) -> TextFindings:
    """One pass over HQ and its extensions, ``walk`` run by ``run`` (in this process unless given).

    Plurals and markup are read in HQ and the extensions checked out beside it,
    or in ``roots`` when given; ``counted`` phrases and open comments in every
    installed extension too, since either fails a page wherever it lives.
    """
    worded_roots = _roots() if roots is None else roots
    outermost = _outermost(worded_roots + _plugin_roots())
    found = run(walk, [str(root) for root in outermost], [str(root) for root in worded_roots])
    phrases = [(path, line, counted_phrases(words)) for path, line, words in found["counted"]]
    phrases += [(path, line, (one, many)) for path, line, one, many in found["called"]]
    return TextFindings(
        files=found["files"],
        hand_plurals=[(Path(path), line) for path, line in found["hand_plurals"]],
        nested_forms=[(Path(path), line) for path, line in found["nested_forms"]],
        open_comments=[(Path(path), line) for path, line in found["open_comments"]],
        unagreeable_counts=[
            (Path(path), line, reason) for path, line, words in phrases if (reason := _refusal(*words))
        ],
    )
