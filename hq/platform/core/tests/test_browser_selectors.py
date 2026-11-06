"""Browser checks cannot outlive the markup they measure.

A selector that matches nothing makes a layout check wait for an element that
never arrives, or measure an empty list and pass. This reads every selector the
browser gate and the layout audit use and fails when no template renders it,
without starting a browser.
"""

import re
from functools import cache
from pathlib import Path
from typing import Any

from django.conf import settings
from django.test import SimpleTestCase, tag

from hq.platform.application.tests.facts import ROOT as FACTS_ROOT, STRING, TOP, grouped, scan, strings
from hq.platform.core.stylesheets import stylesheet
from hq.platform.core.tests.browser.selectors import SELECTORS
from hq.platform.core.tests.browser_modules import browser_modules

ROOT = Path(settings.BASE_DIR)
AUDIT = ROOT / "hq/platform/core" / "browser_audit.js"
# Host templates and the public example extension: the markup this repository
# can see. Installed extensions ship their own templates.
TEMPLATE_ROOTS = (ROOT / "templates", ROOT / "tests/fixtures/example_hq_plugin" / "templates")
# The tag libraries write markup into those templates.
TAG_LIBRARIES = ROOT / "hq/platform/core/templatetags"

_CLASS = re.compile(r"\.(-?[A-Za-z_][\w-]*)")
_ID = re.compile(r"#(-?[A-Za-z_][\w-]*)")
_ATTRIBUTE = re.compile(r"\[\s*([A-Za-z_][\w-]*)")
PLAYWRIGHT_QUERIES = {"locator", "query_selector", "query_selector_all", "wait_for_selector"}
# A literal handed to a DOM query, and a class handed to classList.
_QUERY = re.compile(r"\b(?:querySelector|querySelectorAll|closest|matches)\(\s*(['\"`])(.*?)\1", re.DOTALL)
_CLASS_LIST = re.compile(r"classList\.(?:contains|add|remove|toggle)\(\s*(['\"`])([\w-]+)\1")
# The gate's one registry of selectors, a statement of selectors.py itself.
REGISTRY: dict[str, Any] = {"kind": "assignment", "has": {"field": "left", "regex": "^SELECTORS$"}, **TOP}
# Each literal of the gate outside the registry.
GATE_STRING = {"all": [STRING, {"not": {"inside": {**REGISTRY, "stopBy": "end"}}}]}
# A literal handed to a Playwright query as its first argument.
QUERIED = {
    **STRING,
    "nthChild": 1,
    "inside": {
        "kind": "argument_list",
        "inside": {
            "kind": "call",
            "has": {
                "field": "function",
                "kind": "attribute",
                "has": {"field": "attribute", "regex": f"^({'|'.join(sorted(PLAYWRIGHT_QUERIES))})$"},
            },
        },
    },
}


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
    """(classes, ids, all markup text) across TEMPLATE_ROOTS and the tag libraries."""

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
            # A card's class, id and data- attributes are its tag's options.
            for options in re.findall(r"\{%\s*card\s([^%]*)%\}", source):
                classes.update(("card", *" ".join(re.findall(r'\bcss="([^"]*)"', options)).split()))
                ids.update(re.findall(r'\bid="([^"{}]+)"', options))
                drawn = (name.replace("_", "-") for name in re.findall(r"\b(data_\w+)=", options))
                text.append(f"<div {' '.join(drawn)}>")
    for library in TAG_LIBRARIES.glob("*.py"):
        source = library.read_text(encoding="utf-8")
        # A tag library writes markup too: its attributes are rendered.
        text.append(source)
        for value in re.findall(r'\bclass="([\w\s{}-]*)"', source):
            classes.update(value.replace("{}", " ").split())
    return classes, ids, "\n".join(text)


@cache
def stylesheet_classes() -> set[str]:
    css = stylesheet()
    css = re.sub(r"/\*.*?\*/", " ", css, flags=re.DOTALL)
    css = re.sub(r"\{[^{}]*\}", " ", css)
    return set(_CLASS.findall(css))


def rendered(kind: str, name: str) -> bool:
    classes, ids, text = markup()
    if kind == "class":
        return name in classes
    if kind == "id":
        return name in ids
    return re.search(rf"<[^>]*\s{re.escape(name)}(?:[\s=>/{{])", text) is not None


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


@tag("source", "tree")
class BrowserGateSelectorTests(SimpleTestCase):
    def test_every_gate_selector_names_rendered_markup(self):
        missing = sorted(
            f"{key}: {kind} {name}"
            for key, selector in SELECTORS.items()
            for kind, name in tokens(selector)
            if not rendered(kind, name)
        )
        self.assertEqual(missing, [])

    def test_the_gate_queries_markup_only_through_selectors(self):
        """A literal selector anywhere else in the gate would escape this check.

        In-page probes query by tag and attribute only; anything naming a class
        or an id belongs in SELECTORS.
        """

        # The example estate's gate is outside this rule: it presses the
        # estate's controls through literal selectors of its own.
        paths = [
            path.relative_to(FACTS_ROOT)
            for module, path in browser_modules().items()
            if module != "hq.platform.core.tests.browser.estate"
        ]
        found = grouped(scan({"registry": REGISTRY, "string": GATE_STRING, "queried": QUERIED}, *paths))
        self.assertEqual(len(found.get("registry", [])), 1, "no one SELECTORS registry to hold literals against")
        queried = {(match.path, match.start) for match in found.get("queried", [])}
        offences = []
        for match in found.get("string", []):
            if (match.path, match.start) in queried and isinstance(match.value(), str):
                offences.append(match.value())
            for text in strings(match):
                offences.extend(
                    selector
                    for selector in audit_selectors(text)
                    if any(kind != "attribute" for kind, _ in tokens(selector))
                )
        self.assertEqual(
            offences,
            [],
            "each of these names a class or an id outside SELECTORS: add it to SELECTORS "
            "(hq/platform/core/tests/browser/selectors.py) and pass it to the probe as an argument",
        )


@tag("source", "tree")
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
            if not rendered(kind, name) and not (kind == "class" and name in stylesheet_classes())
        )
        self.assertEqual(missing, [])
