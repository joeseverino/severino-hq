"""A queue item drawn from a stored reading says when that reading was taken."""

import ast
import re
from collections.abc import Iterator
from pathlib import Path

from django.test import SimpleTestCase, tag

from .source import template_files

APPLICATION = Path(__file__).resolve().parents[1]
PACKAGE = "hq.platform.application"
# What a module reads a stored provider reading through.
READS_A_READING = {"snapshots_of", "ProviderInventory"}

# A definition, as ``(module, name)``: a top-level function or class of a
# module under ``APPLICATION``, its methods folded into it.
Unit = tuple[str, str]


def _modules() -> dict[str, ast.Module]:
    found = {}
    for path in sorted(APPLICATION.rglob("*.py")):
        relative = path.relative_to(APPLICATION)
        if "tests" in relative.parts:
            continue
        found[".".join(relative.with_suffix("").parts)] = ast.parse(path.read_text(), str(path))
    return found


def _imported(node: ast.ImportFrom, module: str) -> str:
    """The module under ``APPLICATION`` an import names, or ""."""

    name = node.module or ""
    if node.level:
        base = module.split(".")[: -node.level]
        return ".".join([*base, name] if name else base)
    return name.removeprefix(f"{PACKAGE}.") if name.startswith(f"{PACKAGE}.") else ""


def _bindings(module: str, tree: ast.Module, scope: ast.AST) -> dict[str, Unit]:
    """What each name in ``scope`` stands for: an import, at the top of the
    module or inside ``scope``, or a definition of the module's own."""

    bound: dict[str, Unit] = {
        node.name: (module, node.name)
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
    }
    for node in [*tree.body, *ast.walk(scope)]:
        if isinstance(node, ast.ImportFrom) and (source := _imported(node, module)):
            bound.update({alias.asname or alias.name: (source, alias.name) for alias in node.names})
    return bound


def _named(scope: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(scope):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.ImportFrom):
            found.update(alias.asname or alias.name for alias in node.names)
    return found


def _units(modules: dict[str, ast.Module]) -> dict[Unit, ast.AST]:
    return {
        (module, node.name): node
        for module, tree in modules.items()
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
    }


def _readers(modules: dict[str, ast.Module], units: dict[Unit, ast.AST]) -> set[Unit]:
    """Every definition that reaches a stored reading: one that names
    ``READS_A_READING``, or one that names a definition that does."""

    calls: dict[Unit, set[Unit]] = {}
    readers: set[Unit] = set()
    for unit, node in units.items():
        named = _named(node)
        if named & READS_A_READING:
            readers.add(unit)
        bound = _bindings(unit[0], modules[unit[0]], node)
        calls[unit] = {bound[name] for name in named if name in bound} - {unit}
    grown = True
    while grown:
        grown = False
        for unit, reached in calls.items():
            if unit not in readers and reached & readers:
                readers.add(unit)
                grown = True
    return readers


def _insights(scope: ast.AST) -> Iterator[ast.Call]:
    for node in ast.walk(scope):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Insight":
            yield node


@tag("source", "tree")
class ReadingItemsCarryTheirAgeTests(SimpleTestCase):
    def test_every_item_built_from_a_stored_reading_carries_observed_at(self):
        """Without it a card cannot say how old its claim is, and an old
        reading reads as a current fact.

        An item is held to it when its module reads a reading itself, or when
        the function or class that builds it calls, however indirectly, one
        that does: an item built from ``github_estate.repositories()`` or from
        ``containers()`` is drawn from a reading as much as one built from
        ``snapshots_of``. What the walk cannot follow is a reading handed in as
        an argument or reached through an attribute, which is why a module that
        reads one holds every item it builds. An item drawn from no single
        reading says so with ``observed_at=None``."""

        modules = _modules()
        units = _units(modules)
        readers = _readers(modules, units)
        held: dict[tuple[str, int], ast.Call] = {}
        for module, tree in modules.items():
            if _named(tree) & READS_A_READING:
                held.update({(module, call.lineno): call for call in _insights(tree)})
        for unit in readers:
            held.update({(unit[0], call.lineno): call for call in _insights(units[unit])})

        missing = sorted(
            f"{module}:{line}"
            for (module, line), call in held.items()
            if not any(keyword.arg == "observed_at" for keyword in call.keywords)
        )

        self.assertTrue(held, "no item is built from a stored reading: the rule checks nothing")
        self.assertIn(("container_attention", "attention"), readers, "the walk no longer follows an indirect reader")
        self.assertEqual(missing, [])


TEMPLATES = APPLICATION.parents[2] / "templates"
PARTIAL = TEMPLATES / "partials/_reading_age.html"
# "read" just before an age: a page wording a reading's age itself.
OWN_WORDING = re.compile(r"\bread\b[^{<]{0,4}\{\{[^}]*\|(?:ago|ago_short|when:\"ago\")", re.IGNORECASE)


@tag("source", "tree")
class OneWordingTests(SimpleTestCase):
    def test_a_page_says_a_readings_age_through_the_partial(self):
        """Each page worded it its own way ("read 5 minutes ago", "as of",
        "Seen"), so the same age read differently from page to page."""

        found = [
            f"{name}:{text.count(chr(10), 0, match.start()) + 1}"
            for name, path in template_files()
            if path != PARTIAL
            for text in (path.read_text(encoding="utf-8"),)
            for match in OWN_WORDING.finditer(text)
        ]

        self.assertRegex(
            PARTIAL.read_text(), OWN_WORDING, "the partial no longer words an age: the rule checks nothing"
        )
        self.assertEqual(found, [])
