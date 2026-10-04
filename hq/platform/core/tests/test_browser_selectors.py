"""Browser checks cannot outlive the markup they measure.

A selector that matches nothing makes a layout check wait for an element that
never arrives, or measure an empty list and pass. This reads every selector the
browser gate and the layout audit use and fails when no template renders it,
without starting a browser.
"""

from __future__ import annotations

import ast
import re
from functools import cache
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

from hq.platform.core import browser_tests

ROOT = Path(settings.BASE_DIR)
GATE = ROOT / "hq/platform/core" / "browser_tests.py"
AUDIT = ROOT / "scripts" / "layout-audit.js"
# Host templates and the public example extension: the markup this repository
# can see. Installed extensions ship their own templates.
TEMPLATE_ROOTS = (ROOT / "templates", ROOT / "tests/fixtures/example_hq_plugin" / "templates")

_CLASS = re.compile(r"\.(-?[A-Za-z_][\w-]*)")
_ID = re.compile(r"#(-?[A-Za-z_][\w-]*)")
_ATTRIBUTE = re.compile(r"\[\s*([A-Za-z_][\w-]*)")
PLAYWRIGHT_QUERIES = {"locator", "query_selector", "query_selector_all", "wait_for_selector"}
# A literal handed to a DOM query, and a class handed to classList.
_QUERY = re.compile(
    r"\b(?:querySelector|querySelectorAll|closest|matches)\(\s*(['\"`])(.*?)\1", re.S
)
_CLASS_LIST = re.compile(
    r"classList\.(?:contains|add|remove|toggle)\(\s*(['\"`])([\w-]+)\1"
)


def tokens(selector: str) -> set[tuple[str, str]]:
    # Attribute values are not names to look for.
    bare = re.sub(r"\[([^\]=]*)=[^\]]*\]", r"[\1]", selector)
    return (
        {("class", name) for name in _CLASS.findall(bare)}
        | {("id", name) for name in _ID.findall(bare)}
        | {("attribute", name) for name in _ATTRIBUTE.findall(bare)}
    )


@cache
def markup() -> tuple[set[str], set[str], str]:
    """(classes, ids, all template text) across TEMPLATE_ROOTS."""

    classes: set[str] = set()
    ids: set[str] = set()
    text = []
    for folder in TEMPLATE_ROOTS:
        for template in folder.rglob("*.html"):
            source = template.read_text(encoding="utf-8")
            text.append(source)
            for value in re.findall(r'\bclass="([^"]*)"', source):
                classes.update(value.split())
            ids.update(re.findall(r'\bid="([^"{}]+)"', source))
    return classes, ids, "\n".join(text)


@cache
def stylesheet_classes() -> set[str]:
    css = (ROOT / "static" / "css" / "app.css").read_text(encoding="utf-8")
    css = re.sub(r"/\*.*?\*/", " ", css, flags=re.S)
    css = re.sub(r"\{[^{}]*\}", " ", css)
    return set(_CLASS.findall(css))


def rendered(kind: str, name: str) -> bool:
    classes, ids, text = markup()
    if kind == "class":
        return name in classes
    if kind == "id":
        return name in ids
    return re.search(rf"<[^>]*\s{re.escape(name)}(?:[\s=>/])", text) is not None


def audit_selectors(source: str) -> set[str]:
    found = {match.group(2) for match in _QUERY.finditer(source)}
    found |= {"." + match.group(2) for match in _CLASS_LIST.finditer(source)}
    return found


class SelectorExtractionTests(SimpleTestCase):
    def test_tokens_are_classes_ids_and_attribute_names(self):
        self.assertEqual(
            tokens('figure.chart-card > #main [data-tip="a.b"], td.num-col'),
            {
                ("class", "chart-card"),
                ("class", "num-col"),
                ("id", "main"),
                ("attribute", "data-tip"),
            },
        )

    def test_audit_queries_and_class_lists_are_read(self):
        source = (
            "document.querySelectorAll('.card, figure.chart-card');"
            "el.closest(\"[data-chart]\"); row.classList.contains('two-col');"
            "const rule = 'not.a.selector';"
        )
        self.assertEqual(
            audit_selectors(source),
            {".card, figure.chart-card", "[data-chart]", ".two-col"},
        )

    def test_a_removed_class_is_reported(self):
        self.assertFalse(rendered("class", "dashboard-board"))
        self.assertTrue(rendered("class", "dashboard-highlights"))
        self.assertTrue(rendered("attribute", "data-chart"))


class BrowserGateSelectorTests(SimpleTestCase):
    def test_every_gate_selector_names_rendered_markup(self):
        missing = sorted(
            f"{key}: {kind} {name}"
            for key, selector in browser_tests.SELECTORS.items()
            for kind, name in tokens(selector)
            if not rendered(kind, name)
        )
        self.assertEqual(missing, [])

    def test_the_gate_queries_markup_only_through_selectors(self):
        """A literal selector anywhere else in the gate would escape this check.

        In-page probes query by tag and attribute only; anything naming a class
        or an id belongs in SELECTORS.
        """

        tree = ast.parse(GATE.read_text(encoding="utf-8"))
        registry = next(
            node.value
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(getattr(t, "id", "") == "SELECTORS" for t in node.targets)
        )
        allowed = {id(node) for node in ast.walk(registry)}
        queried = {
            id(call.args[0])
            for call in ast.walk(tree)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr in PLAYWRIGHT_QUERIES
            and call.args
        }
        offences = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if id(node) in allowed:
                continue
            if id(node) in queried:
                offences.append(node.value)
            offences.extend(
                selector
                for selector in audit_selectors(node.value)
                if any(kind != "attribute" for kind, _ in tokens(selector))
            )
        self.assertEqual(offences, [])


class LayoutAuditSelectorTests(SimpleTestCase):
    def test_the_audit_reads_selectors(self):
        self.assertGreater(len(audit_selectors(AUDIT.read_text(encoding="utf-8"))), 5)

    def test_every_audit_selector_names_rendered_markup_or_a_shared_primitive(self):
        """The audit also runs against extension pages this repository cannot see.

        A class an extension renders is still one the host stylesheet declares
        as a shared primitive, so that is the second place a name may live.
        """

        missing = sorted(
            f"{kind} {name} in {selector!r}"
            for selector in audit_selectors(AUDIT.read_text(encoding="utf-8"))
            for kind, name in tokens(selector)
            if not rendered(kind, name)
            and not (kind == "class" and name in stylesheet_classes())
        )
        self.assertEqual(missing, [])
