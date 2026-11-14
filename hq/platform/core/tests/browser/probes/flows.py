"""Probes of the flow (`.section-flow`): the order it paints its members in,
and the rows they share."""

from hq.platform.core.tests.browser.probes import Check
from hq.platform.core.tests.browser.probes.describe import DESCRIBE
from hq.platform.core.tests.browser.selectors import SELECTORS

# A flow paints its members in the order the markup holds them, row by row
# and along each row from its inline start, so what a reader hears and where
# the keyboard goes is the order the page is drawn in (WCAG 1.3.2, 2.4.3). A
# member painted on an earlier row than the one before it, or before it on
# the same row, was moved by the stylesheet (`order`, a reversed direction).
FLOWS_IN_READING_ORDER = (
    "(flow) => {"
    + DESCRIBE
    + """
  const found = [];
  for (const el of document.querySelectorAll(flow)) {
    if (!el.checkVisibility()) continue;
    const rtl = getComputedStyle(el).direction === 'rtl';
    const start = (r) => (rtl ? -r.right : r.left);
    const members = inFlow(el);
    for (let i = 1; i < members.length; i++) {
      const [before, , a] = members[i - 1];
      const [member, , b] = members[i];
      const sameRow = Math.abs(b.top - a.top) <= 1;
      if (b.top < a.top - 1 || (sameRow && start(b) < start(a) - 1)) {
        found.push(`${describe(el)} paints ${describe(member)} before ${describe(before)}, which the markup puts first`);
      }
    }
  }
  return found;
}"""
)


CHECKS = {
    "flows in reading order": Check(
        (FLOWS_IN_READING_ORDER, SELECTORS["flow"]),
        reader_sees="the keyboard and a screen reader going through a flow out of the order it is drawn in",
    ),
}
