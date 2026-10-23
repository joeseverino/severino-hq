"""No page says a value the way a machine writes it.

A stamp reads "Oct 22, 9:36 AM", a flag "yes", a list its words or its count,
a field its label. Each estate the layout gates render (the sparse, dense and
derived stress estates, and the example estate with every part its pages
fetch) is rendered here as a reader is served it, and its words are read for
what only a machine would write: an ISO date-time, a Python dump of a list or
a record, a bare ``True`` or ``False``, a field's snake_case name where its
label belongs.
"""

import re

from django.test import TestCase

from hq.platform.application.tests.markup import all_elements, text
from hq.platform.core.tests.test_browser_dense_fixtures import build_dense_estate, render_dense_pages
from hq.platform.core.tests.test_browser_derived_fixtures import build_derived_estate, render_derived_pages
from hq.platform.core.tests.test_browser_fixtures import build_estate, render_pages
lazy from hq.platform.core.estate_browser_tests import render_estate

# Text a reader is shown as a machine wrote it on purpose: a script's data, a
# command or document shown as code, a value being edited.
_VERBATIM = re.compile(r"<(script|style|template|textarea|pre|code)\b[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)

MACHINE_VALUES = {
    "a date-time stamp": re.compile(r"\b\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}"),
    "a dumped record": re.compile(r"\{'[^']*': "),
    "a dumped list": re.compile(r"\[(?:'[^']*'|\{)"),
    "a bare flag": re.compile(r"(?<![\w-])(?:True|False)(?![\w-])"),
    # A widget printed as text: a filter that escaped it splits its tag.
    "markup": re.compile(r"<[a-z][\w-]*[\s/>]|(?<![\w-])[a-z][\w-]*=\"[^\"]*\""),
}
# A field's name as code writes it, where a label is printed.
_KEY = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$")
_LABELS = frozenset({"th", "dt", "label", "legend"})


def machine_values(html: str) -> list[str]:
    """Each machine-written value in a page's words, named by what it is."""

    said = text(_VERBATIM.sub(" ", html))
    found = [
        f"{name}: {match.group(0)!r} in {_around(said, match)!r}"
        for name, pattern in MACHINE_VALUES.items()
        for match in pattern.finditer(said)
    ]
    found += [
        f"a field's name for a label: {element.words!r}"
        for element in all_elements(_VERBATIM.sub(" ", html))
        if element.tag in _LABELS and _KEY.match(element.words)
    ]
    return found


def _around(said: str, match: re.Match) -> str:
    return said[max(0, match.start() - 40) : match.end() + 40]


class _PageWords(TestCase):
    def assert_said_in_words(self, pages):
        for name, html in pages.items():
            with self.subTest(page=name):
                self.assertEqual(machine_values(html), [])


class StressEstateWordsTests(_PageWords):
    def test_the_sparse_estate_says_every_value_in_words(self):
        self.assert_said_in_words(render_pages(build_estate()))

    def test_the_dense_estate_says_every_value_in_words(self):
        self.assert_said_in_words(render_dense_pages(build_dense_estate()))


class DerivedEstateWordsTests(_PageWords):
    def test_every_list_says_its_longest_values_in_words(self):
        self.assert_said_in_words(render_derived_pages(build_derived_estate()))


class ExampleEstateWordsTests(_PageWords):
    def test_every_page_and_part_of_the_example_estate_says_its_values_in_words(self):
        _paths, answers = render_estate()
        self.assert_said_in_words({f"{url} {part}".strip(): body for (url, part), body in answers.items()})


class MachineValuesTests(TestCase):
    def test_each_machine_shape_is_found_and_words_are_not(self):
        for html in (
            "<p>read 2026-10-22 09:36:47</p>",
            "<p>manifest to [{'doc_id': 'rb-one'}]</p>",
            "<p>tags ['a', 'b']</p>",
            "<p>{'a': 1}</p>",
            "<p>confirm_prune to True</p>",
            "<table><tr><th>confirm_prune</th></tr></table>",
            '<p>value="web:key" id="id_key"&gt;</p>',
            "<p>&lt;input type</p>",
        ):
            with self.subTest(html=html):
                self.assertTrue(machine_values(html))
        for html in (
            "<p>read Oct 22, 9:36 AM</p>",
            '<p><time datetime="2026-10-22T09:36:47+00:00">Oct 22</time></p>',
            "<p>Confirm prune to yes, manifest to 4 entries</p>",
            "<pre><code>{'a': 1}</code></pre>",
            "<p>tag-True-false</p>",
        ):
            with self.subTest(html=html):
                self.assertEqual(machine_values(html), [])
