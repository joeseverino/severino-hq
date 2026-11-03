"""Probes of a page's parts: disclosures, labels, the dashboard's readings and a check's evidence."""

from hq.platform.core.tests.browser.probes.describe import DESCRIBE

# Press a heading's sort control and read its column back, top to bottom.
SPILLED_LABELS = (
    "(selector) => {"
    + DESCRIBE
    + """
  // A control is at least as wide as what it says. One sized for a glyph and
  // given a word draws the word over whatever stands beside it. One with no
  // words has no label to spill. The label is what the control lays out in
  // flow: its words and boxes, and what those spill in turn. A box placed
  // out of flow (the hit area hung beside a small control) is no label,
  // though it widens the control's scrollable overflow.
  const found = [];
  const words = document.createRange();
  const reach = (el) => {
    let right = -Infinity;
    const take = (r) => { if (r.width > 0) right = Math.max(right, r.right); };
    const text = (parent) => {
      for (const node of parent.childNodes) {
        if (node.nodeType === Node.TEXT_NODE && node.textContent.trim()) {
          words.selectNodeContents(node);
          [...words.getClientRects()].forEach(take);
        } else if (node.nodeType === Node.ELEMENT_NODE && getComputedStyle(node).display === 'contents') {
          text(node);
        }
      }
    };
    const walk = (parent) => {
      text(parent);
      for (const [child, style, r] of inFlow(parent)) {
        take(r);
        if (style.overflowX === 'visible') walk(child);
      }
    };
    walk(el);
    return right;
  };
  for (const el of document.querySelectorAll(selector)) {
    if (!el.checkVisibility() || !el.textContent.trim()) continue;
    const style = getComputedStyle(el);
    if (style.overflowX !== 'visible' || style.display === 'inline') continue;
    const start = el.getBoundingClientRect().left + parseFloat(style.borderLeftWidth);
    const held = Math.round(reach(el) - start + parseFloat(style.paddingRight));
    if (held > el.clientWidth + 1) {
      found.push(`${describe(el)} holds ${held}px of label in ${el.clientWidth}px`);
    }
  }
  return found;
}"""
)


# Each reading's lines, as laid out: those that start under its mark rather
# than beside it, and those cut short.
READINGS = """(heads) => heads.map((head) => {
  const [mark, ...lines] = [...head.children].filter((el) => el.checkVisibility());
  const at = mark.getBoundingClientRect();
  return {
    under_the_mark: lines.filter((line) => line.getBoundingClientRect().left < at.right - 1)
      .map((line) => line.textContent.trim().slice(0, 30)),
    cut: lines.filter((line) => line.scrollWidth > line.clientWidth + 1)
      .map((line) => line.textContent.trim().slice(0, 30)),
  };
})"""


def reading_problems(readings):
    """What a reader loses of the readings: a line drawn under its mark, or
    cut short."""

    found: list[str] = []
    for reading in readings:
        found.extend(f"under the mark: {line}" for line in reading["under_the_mark"])
        found.extend(f"cut short: {line}" for line in reading["cut"])
    return found


# A check's evidence follows its statement: on the same line within a short
# gap, or on the next line starting where the statement starts.
STRANDED_EVIDENCE = (
    "([statement, evidence]) => {"
    + DESCRIBE
    + """
  const found = [];
  for (const el of document.querySelectorAll('main ' + evidence)) {
    const said = el.previousElementSibling;
    if (!el.checkVisibility() || !said || !said.matches(statement)) continue;
    const e = box(el);
    const s = box(said);
    const besideIt = e.top < s.bottom - 1;
    if (besideIt ? e.left - s.right > 48 : Math.abs(e.left - s.left) > 2) {
      found.push(`${describe(el)}: ${Math.round(e.left - (besideIt ? s.right : s.left))}px from its statement`);
    }
  }
  return found;
}"""
)

# What a disclosure's handle names is shown wherever the handle is.
HIDDEN_IN_SUMMARIES = (
    "(value) => {"
    + DESCRIBE
    + """
  return [...document.querySelectorAll(value)]
    .filter((el) => el.closest('summary').checkVisibility() && !el.checkVisibility())
    .map(describe);
}"""
)

# Every select that opens to its choices wears HQ's chevron and no arrow of
# the browser's own, whichever way the engine draws it (base-select or
# native), so no two forms read as two kinds of control. The chevron is
# compared with the token painted where the select sits, since its ink is
# light-dark(). A vendored widget draws its own.
SELECT_ARROWS = """([dropdowns, vendored]) => {
  const named = (el) => el.name || el.getAttribute('aria-label') || el.id || 'unnamed';
  return [...document.querySelectorAll(dropdowns)]
    .filter((el) => !el.multiple && el.size <= 1 && !(vendored && el.closest(vendored)))
    .flatMap((el) => {
      const style = getComputedStyle(el);
      const own = style.appearance === 'base-select'
        ? getComputedStyle(el, '::picker-icon').display !== 'none'
        : style.appearance !== 'none';
      const mark = document.createElement('span');
      mark.style.setProperty('background-image', 'var(--chevron-image)');
      el.parentElement.append(mark);
      const chevron = getComputedStyle(mark).backgroundImage;
      mark.remove();
      return [
        own && `select ${named(el)} draws the browser's own arrow`,
        style.backgroundImage !== chevron && `select ${named(el)} does not wear HQ's chevron`,
      ].filter(Boolean);
    });
}"""

# A link that draws an underline draws it under all of its words. A line a
# link draws never reaches into a box of its own inside it (an inline-block
# part of a name), so each word set in one is read from the nearest such box
# between it and the link.
PARTS_NOT_UNDERLINED = """([links, vendored]) => {
  const atomic = (el) => !['inline', 'contents'].includes(getComputedStyle(el).display);
  const underlined = (el) => getComputedStyle(el).textDecorationLine.includes('underline');
  const found = [];
  for (const link of document.querySelectorAll(links)) {
    if ((vendored && link.closest(vendored)) || !underlined(link)) continue;
    const walker = document.createTreeWalker(link, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      if (!node.textContent.trim()) continue;
      const range = document.createRange();
      range.selectNodeContents(node);
      if (![...range.getClientRects()].some((r) => r.width && r.height)) continue;
      let box = node.parentElement;
      while (box !== link && !atomic(box)) box = box.parentElement;
      if (box !== link && !underlined(box)) {
        found.push(`link ${link.textContent.trim().slice(0, 60)} leaves ${node.textContent.trim().slice(0, 40)} without its underline`);
        break;
      }
    }
  }
  return found;
}"""

# A dialog a page declares is one a reader can open: some control on the page
# names it, so drawing a dialog without its control is a finding.
DIALOGS_WITHOUT_OPENERS = """([dialogs, openers]) => {
  const named = new Set([...document.querySelectorAll(openers)].map((opener) =>
    'modalOpen' in opener.dataset ? `modal-${opener.dataset.modalOpen}` : opener.getAttribute('aria-controls')));
  return [...document.querySelectorAll(dialogs)]
    .filter((dialog) => !named.has(dialog.id))
    .map((dialog) => `dialog #${dialog.id} has no control that opens it`);
}"""

# How many lines an element's words run over: the runs of its text that share
# no height, and where the first starts and the last ends. Its text alone: a
# range over the element would also give the box of each element inside it,
# and a list's box spans every line its names wrap to.
LINE_COUNT = """
const lineRange = document.createRange();
const linesOf = (el) => {
  const drawn = [];
  const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    lineRange.selectNodeContents(node);
    drawn.push(...lineRange.getClientRects());
  }
  const rects = drawn.filter((r) => r.width && r.height).sort((a, b) => a.top - b.top);
  if (!rects.length) return {lines: 0, top: 0, bottom: 0};
  let lines = 1;
  let bottom = rects[0].bottom;
  for (const r of rects.slice(1)) {
    if (r.top >= bottom - 2) {
      lines += 1;
      bottom = r.bottom;
    } else bottom = Math.max(bottom, r.bottom);
  }
  return {lines, top: rects[0].top, bottom: Math.max(...rects.map((r) => r.bottom))};
};
"""

# What keeps its line wherever it is drawn: a count beside a legend.
WRAPPED = (
    "(selector) => {"
    + DESCRIBE
    + LINE_COUNT
    + """
  return [...document.querySelectorAll(selector)]
    .filter((el) => el.checkVisibility() && linesOf(el).lines > 1)
    .map((el) => `${describe(el)} "${el.textContent.trim().replace(/\\s+/g, ' ')}" runs over ${linesOf(el).lines} lines`);
}"""
)

# A fact whose value is a list of names keeps no other pair on its row while
# it wraps: beside them it makes their row as tall as itself.
WRAPPED_BESIDE_A_PAIR = (
    "(values) => {"
    + DESCRIBE
    + LINE_COUNT
    + """
  const found = [];
  for (const value of document.querySelectorAll(values)) {
    if (!value.checkVisibility()) continue;
    const {lines, top, bottom} = linesOf(value);
    if (lines < 2) continue;
    const beside = [...value.parentElement.children].some((other) => {
      if (other === value || other === value.previousElementSibling || !other.checkVisibility()) return false;
      const r = other.getBoundingClientRect();
      return r.top < bottom - 1 && r.bottom > top + 1;
    });
    const name = value.previousElementSibling?.textContent.trim().replace(/\\s+/g, ' ');
    if (beside) found.push(`${describe(value)} "${name}" wraps to ${lines} lines beside another pair`);
  }
  return found;
}"""
)
