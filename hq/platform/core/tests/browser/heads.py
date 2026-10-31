"""The layout rules held over a page head's trail and a figure, each drawn by
its shared partial with what an extension may give it and no host page does:
a step one short word long, a value that wraps."""

from typing import override

from django.template.loader import render_to_string

from hq.platform.application.pages import Page
from hq.platform.application.ui import Kpi
from hq.platform.core.tests.browser.layout import LayoutBrowserTests
from hq.platform.core.tests.browser.probes.boxes import SMALL_TARGETS
from hq.platform.core.tests.browser.probes.text import OVERLAPPING_TEXT
from hq.platform.core.tests.browser.selectors import SELECTORS
from hq.platform.core.tests.browser.stress import part_page
from hq.platform.core.tests.browser.widths import TARGET_MIN, WIDTHS

TRAIL = "part/short-trail"
FIGURES = "part/wrapped-figures"
# Each figure's lines, by its value's height over its line's.
_LINES = """(values) => [...document.querySelectorAll(values)].map((value) =>
  Math.round(value.getBoundingClientRect().height / parseFloat(getComputedStyle(value).lineHeight)))"""


def _parts():
    trail = (("Ex", "/example/"), ("Ex", "/example/ex/"), ("Example", "/example/ex/example/"))
    figures = [Kpi("Example range", "10.5–12.5 mi"), Kpi("Example phrase", "up to 2 a week")]
    return {
        TRAIL: render_to_string("partials/_page_head.html", {"page": Page(title="Example", trail=trail)}),
        # A cell held narrow, as three figures to a row on a phone hold it.
        FIGURES: '<div style="inline-size: 110px">'
        + render_to_string("partials/_kpi_grid.html", {"items": figures})
        + "</div>",
    }


class HeadLayoutTests(LayoutBrowserTests):
    @classmethod
    @override
    def render(cls):
        return {name: part_page(markup) for name, markup in _parts().items()}

    def test_a_trail_of_short_steps_is_spaced_clear_to_hit(self):
        """A step of a trail is a small target, so it holds by its spacing: a
        word of two letters is clear of the next step, the slash between them
        belonging to neither."""

        for width in WIDTHS:
            with self.subTest(width=width):
                self.open(TRAIL, width)
                self.assertEqual(self.page.evaluate(SMALL_TARGETS, [SELECTORS["trail_steps"], TARGET_MIN]), [])

    def test_a_figure_that_wraps_keeps_its_lines_apart(self):
        """A range beside its unit, and a phrase, wrap in a narrow cell, and
        their lines are drawn clear of each other."""

        self.open(FIGURES, WIDTHS[0])

        lines = self.page.evaluate(_LINES, SELECTORS["figure_value"])
        self.assertTrue(all(count > 1 for count in lines), f"a value fits its cell, so nothing wraps: {lines}")
        self.assertEqual(self.page.evaluate(OVERLAPPING_TEXT, SELECTORS["floating"]), [])
