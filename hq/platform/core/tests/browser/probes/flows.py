"""Probes of the flow (`.section-flow`): the order it paints its members in,
and the rows they share."""

from hq.platform.core.part_examples.layout import SHORT_BESIDE_LONG
from hq.platform.core.templatetags.layout_tags import LAYOUT
from hq.platform.core.tests.browser.probes import Check, Plant
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


# A section or card a layout lays in a row is as tall as what it holds
# (layout.css): room left under its content is a frame stretched to a
# neighbor's height. What a box draws ends at its own bottom where it holds
# words or inline parts, or is no block, flex or grid container; otherwise at
# the end of its lowest member, past its own padding and border.
FRAMES_TALLER_THAN_THEIR_CONTENT = (
    "(laid) => {"
    + DESCRIBE
    + """
  const containers = ['block', 'flow-root', 'flex', 'grid', 'list-item'];
  const end = (el) => {
    const style = getComputedStyle(el);
    const members = inFlow(el);
    const words = [...el.childNodes].some((node) => node.nodeType === Node.TEXT_NODE && node.textContent.trim());
    if (words || !members.length || !containers.includes(style.display)
        || members.some(([, s]) => s.display.startsWith('inline'))) return box(el).bottom;
    return Math.max(...members.map(([member, s]) => end(member) + parseFloat(s.marginBottom)))
      + parseFloat(style.paddingBottom) + parseFloat(style.borderBottomWidth);
  };
  const room = 1.5 * parseFloat(getComputedStyle(document.documentElement).fontSize);
  const read = [...document.querySelectorAll(laid)].filter((el) => el.checkVisibility());
  const found = [];
  for (const el of read) {
    const left = box(el).bottom - end(el);
    if (left > room) found.push(`${describe(el)} leaves ${Math.round(left)}px empty under what it holds`);
  }
  return {read: read.length, found};
}"""
)

CHECKS = {
    "flows in reading order": Check(
        (FLOWS_IN_READING_ORDER, SELECTORS["flow"]),
        reader_sees="the keyboard and a screen reader going through a flow out of the order it is drawn in",
    ),
    "frames taller than their content": Check(
        (FRAMES_TALLER_THAN_THEIR_CONTENT, SELECTORS["laid"]),
        reader_sees="a card stretched to its neighbor's height, empty under what it holds",
        part=LAYOUT,
        plants=(
            Plant(
                f'<div class="section-flow" style="align-items: stretch">{SHORT_BESIDE_LONG}</div>',
                finds=("leaves",),
            ),
        ),
    ),
}
