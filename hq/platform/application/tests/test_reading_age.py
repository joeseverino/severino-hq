"""A queue item drawn from a stored reading says when that reading was taken."""

import re
from pathlib import Path

from django.test import SimpleTestCase, tag

from hq.platform.application.tests.reads import reads

from .facts import TOP, Import, imports, scan
from .source import template_files

APPLICATION = Path(__file__).resolve().parents[1]
PACKAGE = "hq.platform.application"
# What a module reads a stored provider reading through.
READS_A_READING = {"snapshots_of", "ProviderInventory"}

# A definition, as ``(module, name)``: a top-level function or class of a
# module under ``APPLICATION``, its methods folded into it.
Unit = tuple[str, str]

_NAMED = {"field": "name", "pattern": "$NAME"}
INSIGHT = {"kind": "call", "has": {"field": "function", "kind": "identifier", "regex": "^Insight$"}}
RULES = {
    # A top-level function or class, its decorators with it.
    "unit": {
        "any": [
            {"kind": "decorated_definition", "has": {"field": "definition", "has": _NAMED}},
            {
                "any": [{"kind": "function_definition"}, {"kind": "class_definition"}],
                "has": _NAMED,
                "not": {"inside": {"kind": "decorated_definition"}},
            },
        ],
        **TOP,
    },
    "insight": INSIGHT,
    "aged": {
        "all": [
            INSIGHT,
            {
                "kind": "call",
                "has": {
                    "field": "arguments",
                    "has": {"kind": "keyword_argument", "has": {"field": "name", "regex": "^observed_at$"}},
                },
            },
        ]
    },
}


def _name(names: set[str]) -> dict:
    """An identifier spelling one of ``names`` that stands for a value, as an
    ``ast.Name`` does: not an attribute's, a keyword's, a definition's, a
    parameter's or an import's own name."""

    return {
        "kind": "identifier",
        "regex": f"^({'|'.join(sorted(names))})$",
        "not": {
            "any": [
                {"inside": {"kind": kind, "field": field}}
                for kind, field in (
                    ("attribute", "attribute"),
                    ("keyword_argument", "name"),
                    ("function_definition", "name"),
                    ("class_definition", "name"),
                    ("default_parameter", "name"),
                    ("typed_default_parameter", "name"),
                )
            ]
            + [
                {"inside": {"kind": kind}}
                for kind in (
                    "parameters",
                    "lambda_parameters",
                    "typed_parameter",
                    "list_splat_pattern",
                    "dictionary_splat_pattern",
                    "dotted_name",
                    "aliased_import",
                )
            ]
        },
    }


def _module(path: Path) -> str:
    return ".".join(path.relative_to(APPLICATION).with_suffix("").parts)


def _imported(each: Import, module: str) -> str:
    """The module under ``APPLICATION`` a ``from`` import names, or ""."""

    assert each.source is not None
    name = each.source.lstrip(".")
    if level := len(each.source) - len(name):
        base = module.split(".")[:-level]
        return ".".join([*base, name] if name else base)
    return name.removeprefix(f"{PACKAGE}.") if name.startswith(f"{PACKAGE}.") else ""


class _Module:
    """What one module defines, imports, names and builds, each by line."""

    def __init__(self):
        self.units: dict[str, range] = {}
        self.imports: list[Import] = []
        self.names: list[tuple[int, str]] = []
        self.insights: dict[int, bool] = {}

    def within(self, unit: str | None, line: int) -> bool:
        return unit is None or line in self.units[unit]

    def named(self, unit: str | None = None) -> set[str]:
        """The names the module, or one of its units, reads or imports."""

        return {name for line, name in self.names if self.within(unit, line)} | {
            bound for each in self.imports if self.within(unit, each.line) for bound in each.bound()
        }

    def bindings(self, module: str, unit: str) -> dict[str, Unit]:
        """What each name in ``unit`` stands for: an import, at the top of the
        module or inside ``unit``, or a definition of the module's own."""

        bound: dict[str, Unit] = {name: (module, name) for name in self.units}
        for each in self.imports:
            if (each.top or self.within(unit, each.line)) and (source := _imported(each, module)):
                bound.update({alias or name: (source, name) for name, alias in each.names})
        return bound


def _modules() -> dict[str, _Module]:
    """Every module under ``APPLICATION`` but its tests, read by three scans."""

    modules: dict[str, _Module] = {}

    def of(path: Path) -> _Module | None:
        if "tests" in path.relative_to(APPLICATION).parts:
            return None
        return modules.setdefault(_module(path), _Module())

    paths = [path for path in APPLICATION.iterdir() if path.name != "tests" and (path.is_dir() or path.suffix == ".py")]
    for each in scan(RULES, *paths):
        if (module := of(each.path)) is None:
            continue
        if each.rule == "unit":
            module.units[each.captured["NAME"]] = range(each.line, each.line + each.text.count("\n") + 1)
        else:
            module.insights[each.line] = module.insights.get(each.line, False) or each.rule == "aged"
    for each in imports(*paths):
        if each.source is not None and (module := of(each.path)) is not None:
            module.imports.append(each)
    # Only a name that can stand for a definition here is worth reading back.
    names = READS_A_READING | {
        name
        for module, facts in modules.items()
        for name in [
            *facts.units,
            *(bound for each in facts.imports if _imported(each, module) for bound in each.bound()),
        ]
    }
    for each in scan({"name": _name(names)}, *paths):
        if (module := of(each.path)) is not None:
            module.names.append((each.line, each.text))
    return modules


def _readers(modules: dict[str, _Module]) -> set[Unit]:
    """Every definition that reaches a stored reading: one that names
    ``READS_A_READING``, or one that names a definition that does."""

    calls: dict[Unit, set[Unit]] = {}
    readers: set[Unit] = set()
    for module, facts in modules.items():
        for name in facts.units:
            unit = (module, name)
            named = facts.named(name)
            if named & READS_A_READING:
                readers.add(unit)
            bound = facts.bindings(module, name)
            calls[unit] = {bound[each] for each in named if each in bound} - {unit}
    grown = True
    while grown:
        grown = False
        for unit, reached in calls.items():
            if unit not in readers and reached & readers:
                readers.add(unit)
                grown = True
    return readers


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
        readers = _readers(modules)
        held: dict[tuple[str, int], bool] = {}
        for module, facts in modules.items():
            reads = bool(facts.named() & READS_A_READING)
            held.update(
                {
                    (module, line): aged
                    for line, aged in facts.insights.items()
                    if reads or any(facts.within(name, line) for name in facts.units if (module, name) in readers)
                }
            )

        missing = sorted(f"{module}:{line}" for (module, line), aged in held.items() if not aged)

        self.assertTrue(held, "no item is built from a stored reading: the rule checks nothing")
        self.assertIn(("container_findings", "add"), readers, "the walk no longer follows an indirect reader")
        self.assertEqual(missing, [])


TEMPLATES = APPLICATION.parents[2] / "templates"
PARTIAL = TEMPLATES / "partials/_reading_age.html"
# "read" just before an age: a page wording a reading's age itself.
OWN_WORDING = re.compile(r"\bread\b[^{<]{0,4}\{\{[^}]*\|(?:ago|ago_short|when:\"ago\")", re.IGNORECASE)


@tag("source", "tree")
@reads("**/templates/**")
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

        self.assertRegex('Last read {{ at|when:"ago" }}', OWN_WORDING, "the rule no longer finds an age worded by hand")
        self.assertEqual(found, [])
