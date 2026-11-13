"""Probes of how a heading and a cell's quiet link are set: the case of the one,
the line of the other."""

from hq.platform.core.tests.browser.probes.describe import DESCRIBE
from hq.platform.core.tests.browser.selectors import SELECTORS

# A heading is said in sentence case: capitals are for a kicker over a group
# (``kicker``), which is a label, never the heading of a card or a section.
# Read from the drawn text-transform, so a class on the heading, a rule on its
# parent and an inline style are all caught.
HEADINGS_IN_CAPITALS = (
    "(kicker) => {"
    + DESCRIBE
    + """
  const found = [];
  for (const el of document.querySelectorAll('main :is(h1, h2, h3, h4, h5, h6, [role="heading"])')) {
    if (!el.checkVisibility() || !el.textContent.trim() || el.matches(kicker)) continue;
    if (getComputedStyle(el).textTransform === 'uppercase') {
      found.push(`${describe(el)} "${el.textContent.trim().slice(0, 40)}" is drawn in capitals`);
    }
  }
  return found;
}"""
)

# A quiet link that follows a value in a table cell ("Change" after a name)
# starts a line of its own: set in the line, it lands beside the value on one
# row and under it on the next, and reads as part of the value. Nothing drawn
# before it in the cell may end below its top.
QUIET_LINKS_IN_THE_LINE = (
    "(quiet) => {"
    + DESCRIBE
    + """
  const found = [];
  const words = document.createRange();
  // What the cell draws before the link, each of its children by its own box:
  // a box that cuts its words is read where it ends, not where they would.
  const boxes = (node) => {
    if (node.nodeType === Node.ELEMENT_NODE) return [node.getBoundingClientRect()];
    if (node.nodeType !== Node.TEXT_NODE || !node.textContent.trim()) return [];
    words.selectNodeContents(node);
    return [...words.getClientRects()];
  };
  for (const link of document.querySelectorAll('main td > ' + quiet)) {
    if (!link.checkVisibility()) continue;
    const cell = link.parentElement;
    const earlier = [...cell.childNodes].slice(0, [...cell.childNodes].indexOf(link));
    const drawn = earlier.flatMap(boxes).filter((r) => r.width > 0 && r.height > 0);
    if (!drawn.length) continue;
    const top = link.getBoundingClientRect().top;
    const ends = Math.max(...drawn.map((r) => r.bottom));
    if (ends > top + 1) {
      found.push(`${describe(cell)}: "${link.textContent.trim()}" shares a line with the value before it`);
    }
  }
  return found;
}"""
)


# The survey checks these probes answer (`probes/__init__.py`).
CHECKS = {
    "headings in capitals": (HEADINGS_IN_CAPITALS, SELECTORS["kicker"]),
    "quiet links in the line": (QUIET_LINKS_IN_THE_LINE, SELECTORS["quiet_link"]),
}
