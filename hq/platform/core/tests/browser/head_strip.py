"""The dashboard's head, drawn by the shared parts: a head's line of facts in
its title's column beside an aside."""

from hq.platform.core.tests.browser.layout import LayoutBrowserTests
from hq.platform.core.tests.browser.selectors import SELECTORS
from hq.platform.core.tests.browser.widths import WIDE

# A head's title, its line of facts and the readings beside them, each a box.
HEAD_COLUMN = """([title, facts, readings]) => {
  const box = (r) => ({left: r.left, right: r.right, top: r.top, bottom: r.bottom});
  const marks = [...document.querySelectorAll(readings)].map((reading) => reading.getBoundingClientRect());
  return {
    title: box(document.querySelector(title).getBoundingClientRect()),
    facts: box(document.querySelector(facts).getBoundingClientRect()),
    readings: {
      left: Math.min(...marks.map((r) => r.left)),
      top: Math.min(...marks.map((r) => r.top)),
      bottom: Math.max(...marks.map((r) => r.bottom)),
    },
  };
}"""


class HeadAndStripBrowserTests(LayoutBrowserTests):
    """The parts the dashboard's head draws through, as they read."""

    def test_what_needs_you_is_said_under_the_greeting_beside_the_readings(self):
        """With an aside beside it, a head's line of facts stands in its
        title's column: under the greeting at its start, with the readings
        beside both, never on a line of its own under the readings."""

        for width in (WIDE, 1900):
            with self.subTest(width=width):
                self.open("dashboard", width)
                head = self.page.evaluate(
                    HEAD_COLUMN, self.css([SELECTORS["page_title"], SELECTORS["head_facts"], SELECTORS["glance"]])
                )
                title, facts, readings = head["title"], head["facts"], head["readings"]
                self.assertEqual(round(facts["left"]), round(title["left"]), head)
                self.assertGreaterEqual(round(facts["top"]), round(title["bottom"]), head)
                self.assertGreater(readings["left"], max(title["right"], facts["right"]), head)
                self.assertLess(facts["top"], readings["bottom"], head)
