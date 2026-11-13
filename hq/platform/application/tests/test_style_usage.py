"""Every class the stylesheet defines is used by something that renders.

A rule nothing renders is weight every page downloads and every edit reads.
The stylesheet is the host's one shared bundle, and its users are the host's
templates, scripts and Python, plus the extensions that style through it. The
host cannot see an extension's templates, so a published class
(``hq_sdk.template_contract.CLASSES``) is used when the host or the example
extensions draw it, and a state that modifies one is the extensions' to draw.
Publishing a class is no use of it: one nothing draws keeps its rules alive.
"""

import re
from collections.abc import Collection, Iterable
from pathlib import Path

from django.forms.utils import ErrorList
from django.test import SimpleTestCase, tag

from hq.platform.core.stylesheets import stylesheet
from hq_sdk.template_contract import CLASSES

from ..plugin_testing import selector_classes, stylesheet_selectors

ROOT = Path(__file__).resolve().parents[4]
USER_ROOTS = ("templates", "hq", "hq_sdk", "static/js", "examples/extension/src", "tests/fixtures")
# The contract lists the classes an extension may write; it is not a user.
NOT_USERS = frozenset({"template_contract.py", "contract.json"})

WORD = re.compile(r"[A-Za-z0-9_-]+")
# A class built at render time ends in a prefix ahead of an interpolation:
# ``pill-{{ tone }}`` in a class attribute, ``pill-{tone}`` or ``pill-${tone}``
# after a space in Python and script (a bare ``f"connection-{slug}"`` is an id).
CLASS_ATTRIBUTE = re.compile(r'class="([^"]*)"')
TEMPLATE_PREFIX = re.compile(r"(?<![\w-])([a-z][a-z0-9]*(?:-[a-z0-9]+)*-)(?=\{\{)")
SOURCE_PREFIX = re.compile(r"(?:(?<=\s)|(?<=class=\"))([a-z][a-z0-9]*(?:-[a-z0-9]+)*-)(?=\$?\{)")
SOURCE_SUFFIXES = {".py", ".js"}


def _prefixes(suffix: str, text: str) -> set[str]:
    if suffix == ".html":
        return {prefix for attribute in CLASS_ATTRIBUTE.findall(text) for prefix in TEMPLATE_PREFIX.findall(attribute)}
    if suffix in SOURCE_SUFFIXES:
        return set(SOURCE_PREFIX.findall(text))
    return set()


def user_files(root: Path = ROOT) -> Iterable[Path]:
    for name in USER_ROOTS:
        for path in sorted((root / name).rglob("*")):
            is_test = "tests" in path.parts or path.name.startswith("test_")
            if path.is_file() and not is_test and path.name not in NOT_USERS and "__pycache__" not in path.parts:
                yield path


def _admitted_by_contract(selectors: Iterable[str], public: Collection[str]) -> set[str]:
    """Classes that only modify a published one: ``.route-line.route-band-2``
    styles a state of a primitive an extension draws, so the state is theirs.
    The published class itself is not admitted: something must draw it."""

    admitted: set[str] = set()
    for selector in selectors:
        for chain in re.findall(r"(?:\.[\w-]+)+", selector):
            names = selector_classes(chain)
            if any(name in public for name in names):
                admitted.update(name for name in names if name not in public)
    return admitted


def unused_style_classes(css: str, sources: Iterable[tuple[str, str]], public: Collection[str]) -> list[str]:
    """Classes ``css`` defines that no ``(suffix, text)`` source uses.

    A class is used when it appears as a word in any source, or begins with a
    prefix some source builds at render time. A class that modifies a
    ``public`` one is admitted."""

    selectors = stylesheet_selectors(css)
    defined = {name for selector in selectors for name in selector_classes(selector)}
    words: set[str] = set()
    prefixes: set[str] = set()
    for suffix, text in sources:
        words.update(WORD.findall(text))
        prefixes.update(_prefixes(suffix, text))
    admitted = _admitted_by_contract(selectors, public)
    return sorted(
        name for name in defined - words - admitted if not any(name.startswith(prefix) for prefix in prefixes)
    )


# Published classes nothing draws, each to be deleted with its rules or drawn
# by the example extension; this only falls.
UNDRAWN = frozenset(
    {
        "chart-card",
        "danger-link",
        "domain-charts",
        "domain-head",
        "domain-head-copy",
        "domain-stack",
        "field-row",
        "figure-hero",
        "figure-hero-note",
        "figure-stats",
        "finding-evidence",
        "pie-chart",
        "pie-row",
        "print-footer",
        "print-only",
        "prose-list",
        "route-band-1",
        "route-band-2",
        "route-band-3",
        "route-band-4",
        "route-band-5",
        "route-frame",
        "route-line",
        "route-map",
        "route-marker",
        "route-scale",
        "route-swatch",
        "section-period",
    }
)


@tag("source", "tree")
class StylesheetUsageTests(SimpleTestCase):
    # Django renders a form's errors as ``<ul class="errorlist">`` itself.
    FRAMEWORK = {ErrorList().error_class}

    def test_every_class_the_stylesheet_defines_has_a_user(self) -> None:
        sources = [(path.suffix, path.read_text(encoding="utf-8")) for path in user_files()]
        unused = set(unused_style_classes(stylesheet(), sources, set(CLASSES))) - self.FRAMEWORK

        self.assertEqual(
            sorted(unused - UNDRAWN),
            [],
            "The stylesheet defines classes nothing renders: delete their rules, and a published one's "
            "hq_sdk.template_contract.CLASSES entry, or draw it in the example extension.",
        )
        self.assertEqual(sorted(UNDRAWN - unused), [], "a class is drawn or gone: delete its UNDRAWN entry")

    def test_the_search_reaches_the_files_it_reads(self) -> None:
        suffixes = {path.suffix for path in user_files()}

        self.assertLessEqual({".html", ".py", ".js"}, suffixes)


class UnusedStyleClassTests(SimpleTestCase):
    CSS = ".used { color: red } .orphan { color: red } .tone-good, .tone-bad { color: red } .card.state { color: red }"

    def test_a_class_nothing_uses_is_reported(self) -> None:
        sources = [(".html", '<p class="card used tone-good">')]

        self.assertEqual(unused_style_classes(self.CSS, sources, {"card"}), ["orphan", "tone-bad"])

    def test_a_family_built_at_render_time_is_used_through_its_prefix(self) -> None:
        sources = [(".html", '<p class="card used tone-{{ tone }}">')]

        self.assertEqual(unused_style_classes(self.CSS, sources, {"card"}), ["orphan"])

    def test_a_prefix_outside_a_class_attribute_admits_nothing(self) -> None:
        sources = [(".html", '<p class="used" id="tone-{{ tone }}">')]

        self.assertIn("tone-good", unused_style_classes(self.CSS, sources, {"card"}))

    def test_a_published_class_s_modifiers_are_admitted_and_it_needs_a_drawer(self) -> None:
        self.assertEqual(unused_style_classes(".card.state { color: red }", [], {"card"}), ["card"])
        self.assertEqual(
            unused_style_classes(".card.state { color: red }", [(".html", '<p class="card">')], {"card"}), []
        )
        self.assertEqual(unused_style_classes(".card.state { color: red }", [], set()), ["card", "state"])
