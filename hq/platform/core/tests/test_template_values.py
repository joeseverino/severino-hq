"""Every value a Python builder prints as text holds the names in it."""

import ast
import re
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase, tag

from hq.platform.application.tests.facts import scan
from hq.platform.core.hand_drawn import HOLDING
from hq.platform.core.held_values import _Markup


@dataclass(frozen=True)
class Site:
    path: str
    line: int
    value: str

    def __str__(self):
        return f"{self.path}:{self.line}: {{{{ {self.value} }}}}"


# What a `format_html` builder may print as text: a call that holds its names
# or composes markup whose own values are read where it was built (the entity
# tag holds its name with breakable, and offer_markup its label), an amount,
# and a tag's attributes (page_parts.html_attributes), which are never text.
HOLDING_CALLS = HOLDING | {
    "entity",
    "format_html",
    "format_html_join",
    "html_attributes",
    "mark_safe",
    "money",
    "offer_markup",
}
_PLACEHOLDER = re.compile(r"\{(\w*)\}")
# A format string that is attributes to add to a tag (' title="{}"') starts
# inside that tag.
_ATTRIBUTES = re.compile(r"\s+[\w:-]+=")


def _called(node: ast.AST) -> str:
    func = node.func if isinstance(node, ast.Call) else None
    if isinstance(func, ast.Name):
        return func.id
    return func.attr if isinstance(func, ast.Attribute) else ""


def _text_placeholders(fmt: str) -> list[str]:
    """The placeholders of a format string printed as text, by position
    ("0", "1", ...) or by name."""

    markup = _Markup()
    markup.in_tag = bool(_ATTRIBUTES.match(fmt))
    found = []
    at = 0
    position = 0
    for match in _PLACEHOLDER.finditer(fmt):
        markup.read(fmt[at : match.start()])
        at = match.end()
        name = match[1] or str(position)
        position += not match[1]
        if markup.in_text:
            found.append(name)
    return found


def _locals(scope: ast.AST) -> dict[str, ast.AST]:
    return {
        target.id: node.value
        for node in ast.walk(scope)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }


class _Builders:
    """The `format_html` calls of one module, read against its own helpers."""

    def __init__(self, source: str, path: str):
        self.path = path
        self.tree = ast.parse(source)
        self.functions = [
            node for node in ast.walk(self.tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        self.helpers = {function.name: function for function in self.functions}
        self._asked: set[str] = set()

    def held(self, node: ast.AST, assigned: dict[str, ast.AST]) -> bool:
        """Whether a builder's argument holds its names: a call that does, a
        helper of this module whose every result does, a choice's label, a
        literal, a constant HQ wrote, or a local set to one of these."""

        if isinstance(node, ast.IfExp):
            return self.held(node.body, assigned) and self.held(node.orelse, assigned)
        if isinstance(node, ast.Constant):
            return True
        if isinstance(node, ast.Name):
            return node.id.isupper() or (node.id in assigned and self.held(assigned[node.id], assigned))
        called = _called(node)
        if called in HOLDING_CALLS or (called.startswith("get_") and called.endswith("_display")):
            return True
        return called in self.helpers and self._helper_holds(called)

    def _helper_holds(self, name: str) -> bool:
        if name in self._asked:
            return False
        self._asked.add(name)
        helper = self.helpers[name]
        assigned = _locals(helper)
        returned = [node.value for node in ast.walk(helper) if isinstance(node, ast.Return) and node.value]
        return bool(returned) and all(self.held(value, assigned) for value in returned)

    def _unheld_in(self, call: ast.Call, assigned: dict[str, ast.AST]) -> list[Site]:
        if not (isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str)):
            return []
        arguments = {str(index): arg for index, arg in enumerate(call.args[1:])}
        arguments |= {keyword.arg: keyword.value for keyword in call.keywords if keyword.arg}
        found = []
        for name in _text_placeholders(call.args[0].value):
            argument = arguments.get(name)
            if argument is None or isinstance(argument, ast.Starred) or not self.held(argument, assigned):
                shown = ast.unparse(argument) if argument is not None else f"{{{name}}}"
                found.append(Site(self.path, call.lineno, shown))
        return found

    def unheld(self) -> list[Site]:
        found = []
        seen = set()
        # The innermost scope first, so a call reads the locals of its own function.
        for scope in [*reversed(self.functions), self.tree]:
            assigned = _locals(scope)
            for call in ast.walk(scope):
                if id(call) in seen or _called(call) != "format_html" or not call.args:
                    continue
                seen.add(id(call))
                self._asked.clear()
                found += self._unheld_in(call, assigned)
        return sorted(found, key=lambda site: site.line)


def unheld_builders(source: str, path: str = "") -> list[Site]:
    """Each value a `format_html` call in ``source`` prints as text without holding its names."""

    return _Builders(source, path).unheld()


# A call of format_html, by its name or as an attribute.
BUILDER = {"kind": "call", "has": {"field": "function", "regex": r"(^|\.)format_html$"}}


def builder_paths() -> list[Path]:
    """Each module of hq and hq_sdk, less their tests, that calls format_html: the only ones ``_Builders`` reads."""

    root = Path(settings.BASE_DIR)
    return sorted({match.path for match in scan({"builder": BUILDER}, root / "hq", root / "hq_sdk", tests=False)})


class UnheldBuilderTests(SimpleTestCase):
    """What the check reads as a value a Python builder prints as text."""

    def values(self, source):
        return [site.value for site in unheld_builders(source)]

    def test_a_bare_value_in_text_is_found(self):
        self.assertEqual(self.values("format_html('<b>{}</b> · {}', host, n)"), ["host", "n"])

    def test_a_held_value_passes(self):
        self.assertEqual(self.values("format_html('<b>{}</b>', breakable(host))"), [])

    def test_an_attribute_value_passes(self):
        source = 'format_html(\'<a href="{}" title="{t}">{}</a>\', url, breakable(x), t=y)'

        self.assertEqual(self.values(source), [])

    def test_a_local_set_to_a_held_value_passes(self):
        source = (
            "def f():\n"
            "    label = breakable(x) if a else format_html('{}', breakable(y))\n"
            "    return format_html('<b>{}</b>', label)\n"
        )

        self.assertEqual(self.values(source), [])

    def test_a_value_spread_from_a_tuple_is_found(self):
        self.assertEqual(self.values("format_html('<a href=\"{}\">{}</a>', *mention)"), ["{1}"])

    def test_attributes_added_to_a_tag_pass(self):
        self.assertEqual(self.values("format_html(' title=\"{}\"', link.title)"), [])

    def test_a_helper_that_returns_held_markup_passes(self):
        source = (
            "def mention(link):\n"
            "    return format_html('<a href=\"{}\">{}</a>', link.url, breakable(link.label))\n"
            "def bare(link):\n"
            "    return link.label\n"
            "format_html('from {} and {}', mention(a), bare(b))\n"
        )

        self.assertEqual(self.values(source), ["bare(b)"])

    def test_words_hq_writes_pass(self):
        self.assertEqual(self.values("format_html('{} {} {}', MISSING, 'x', row.get_status_display())"), [])


@tag("source", "tree")
class BuilderValueTests(SimpleTestCase):
    def test_every_value_a_builder_prints_as_text_holds_its_names(self):
        root = Path(settings.BASE_DIR)
        found = []
        for path in builder_paths():
            found += unheld_builders(path.read_text(encoding="utf-8"), str(path.relative_to(root)))

        self.assertEqual([str(site) for site in found], [])
