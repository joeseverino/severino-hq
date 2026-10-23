"""What names a value (a term in a list of facts, a band's or a tile's
label) is said in sentence case; capitals are for a kicker over a group
(`.eyebrow`), never for a value's name."""

import re

from django.test import SimpleTestCase

from hq.platform.core.stylesheets import stylesheet

# A selector whose subject names a value: a term, a band's label, a tile's.
_NAMES_A_VALUE = re.compile(r"(?:\bdt|\.band-label|\.kpi \.label)$")
_RULE = re.compile(r"([^{}]+)\{([^{}]*)\}")


def shouted_names(css: str) -> list[str]:
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    return [
        selector.strip()
        for selectors, body in _RULE.findall(css)
        if "text-transform: uppercase" in body
        for selector in selectors.split(",")
        if _NAMES_A_VALUE.search(selector.strip())
    ]


class LabelCaseTests(SimpleTestCase):
    def test_no_value_is_named_in_capitals(self):
        self.assertEqual(shouted_names(stylesheet()), [])

    def test_a_term_in_capitals_is_refused_and_a_kicker_passes(self):
        self.assertEqual(shouted_names(".facts dt { text-transform: uppercase; }"), [".facts dt"])
        self.assertEqual(shouted_names(".eyebrow { text-transform: uppercase; }"), [])
        self.assertEqual(shouted_names(".facts dt { color: red; }"), [])
