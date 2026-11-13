"""The dashboard's head and its strip of counts, drawn by the shared parts: a
head's line of facts in its title's column beside an aside, and a card of only
a line of facts as a strip."""

from hq.platform.core.tests.browser.layout import LayoutBrowserTests
from hq.platform.core.tests.browser.selectors import SELECTORS
from hq.platform.core.tests.browser.widths import PHONE_SCREEN, WIDE

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

# What the card holding the strip adds above and below its line, the block
# inset a card of content takes (a bare card drawn beside it and taken away),
# and the sizes of a value and a label.
STRIP_INSET = """([strip, valued, labeled]) => {
  const line = document.querySelector(strip);
  const card = line.parentElement;
  const bare = card.parentElement.appendChild(document.createElement('div'));
  bare.className = 'card';
  const padding = getComputedStyle(bare);
  const inset = parseFloat(padding.paddingTop) + parseFloat(padding.paddingBottom);
  bare.remove();
  return {
    added: card.getBoundingClientRect().height - line.getBoundingClientRect().height,
    inset,
    value: parseFloat(getComputedStyle(line.querySelector(valued)).fontSize),
    label: parseFloat(getComputedStyle(line.querySelector(labeled)).fontSize),
  };
}"""


class HeadAndStripBrowserTests(LayoutBrowserTests):
    """The parts the dashboard's head and strip draw through, as they read."""

    def test_what_needs_you_is_said_under_the_greeting_beside_the_readings(self):
        """With an aside beside it, a head's line of facts stands in its
        title's column: under the greeting at its start, with the readings
        beside both, never on a line of its own under the readings."""

        for width in (WIDE, 1900):
            with self.subTest(width=width):
                self.open("dashboard", width)
                head = self.page.evaluate(
                    HEAD_COLUMN, [SELECTORS["page_title"], SELECTORS["head_facts"], SELECTORS["readings"]]
                )
                title, facts, readings = head["title"], head["facts"], head["readings"]
                self.assertEqual(round(facts["left"]), round(title["left"]), head)
                self.assertGreaterEqual(round(facts["top"]), round(title["bottom"]), head)
                self.assertGreater(readings["left"], max(title["right"], facts["right"]), head)
                self.assertLess(facts["top"], readings["bottom"], head)

    def test_a_card_of_only_counts_is_a_strip_narrower_than_a_card_of_content(self):
        """A card holding nothing but a line of facts adds less above and below
        it than a card of content's inset, and its words are a size under its
        values, as a strip of counts reads."""

        for width in (PHONE_SCREEN[0], WIDE, 1900):
            with self.subTest(width=width):
                self.open("dashboard", width)
                strip = self.page.evaluate(
                    STRIP_INSET, [SELECTORS["dashboard_counts"], SELECTORS["fact_value"], SELECTORS["fact_label"]]
                )
                self.assertLess(strip["added"], strip["inset"], strip)
                self.assertLess(strip["label"], strip["value"], strip)
