"""Every class the stylesheet defines is used by something that renders.

A rule nothing renders is weight every page downloads and every edit reads.
The stylesheet is the host's one shared bundle, and its users are the host's
templates, scripts and Python, plus the extensions that style through it. The
host cannot see an extension's templates, so a class an extension may write is
admitted from the published contract (``hq_sdk.template_contract.CLASSES``)
instead of by naming the extension.
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
USER_ROOTS = ("templates", "hq", "hq_sdk", "static/js")
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
    styles a state of a primitive an extension draws, so the state is theirs."""

    admitted = set(public)
    for selector in selectors:
        for chain in re.findall(r"(?:\.[\w-]+)+", selector):
            names = selector_classes(chain)
            if any(name in public for name in names):
                admitted.update(names)
    return admitted


def unused_style_classes(css: str, sources: Iterable[tuple[str, str]], public: Collection[str]) -> list[str]:
    """Classes ``css`` defines that no ``(suffix, text)`` source uses.

    A class is used when it appears as a word in any source, or begins with a
    prefix some source builds at render time. ``public`` classes are admitted."""

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


@tag("source", "tree")
class StylesheetUsageTests(SimpleTestCase):
    # Django renders a form's errors as ``<ul class="errorlist">`` itself.
    FRAMEWORK = {ErrorList().error_class}

    def test_every_class_the_stylesheet_defines_has_a_user(self) -> None:
        sources = [(path.suffix, path.read_text(encoding="utf-8")) for path in user_files()]
        unused = unused_style_classes(stylesheet(), sources, set(CLASSES) | self.FRAMEWORK)

        self.assertEqual(
            unused,
            [],
            "The stylesheet defines classes nothing renders: delete their rules, or if an extension "
            "needs one, add it to hq_sdk.template_contract.CLASSES.",
        )

    def test_the_search_reaches_the_files_it_reads(self) -> None:
        suffixes = {path.suffix for path in user_files()}

        self.assertLessEqual({".html", ".py", ".js"}, suffixes)


class UnusedStyleClassTests(SimpleTestCase):
    CSS = ".used { color: red } .orphan { color: red } .tone-good, .tone-bad { color: red } .card.state { color: red }"

    def test_a_class_nothing_uses_is_reported(self) -> None:
        sources = [(".html", '<p class="used tone-good">')]

        self.assertEqual(unused_style_classes(self.CSS, sources, {"card"}), ["orphan", "tone-bad"])

    def test_a_family_built_at_render_time_is_used_through_its_prefix(self) -> None:
        sources = [(".html", '<p class="used tone-{{ tone }}">')]

        self.assertEqual(unused_style_classes(self.CSS, sources, {"card"}), ["orphan"])

    def test_a_prefix_outside_a_class_attribute_admits_nothing(self) -> None:
        sources = [(".html", '<p class="used" id="tone-{{ tone }}">')]

        self.assertIn("tone-good", unused_style_classes(self.CSS, sources, {"card"}))

    def test_a_published_class_and_its_modifiers_are_admitted(self) -> None:
        self.assertEqual(unused_style_classes(".card.state { color: red }", [], {"card"}), [])
        self.assertEqual(unused_style_classes(".card.state { color: red }", [], set()), ["card", "state"])
