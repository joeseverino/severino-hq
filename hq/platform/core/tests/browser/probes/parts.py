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

# A dialog a page declares is one a reader can open: some control on the page
# names it, so drawing a dialog without its control is a finding.
DIALOGS_WITHOUT_OPENERS = """([dialogs, openers]) => {
  const named = new Set([...document.querySelectorAll(openers)].map((opener) =>
    'modalOpen' in opener.dataset ? `modal-${opener.dataset.modalOpen}` : opener.getAttribute('aria-controls')));
  return [...document.querySelectorAll(dialogs)]
    .filter((dialog) => !named.has(dialog.id))
    .map((dialog) => `dialog #${dialog.id} has no control that opens it`);
}"""

# An ask in a table's row says how its work stands without growing the row:
# its note is given the longest thing an ask says, as ask.js writes it (the
# state's word and its sentence), the row measured before and after, and the
# note put back as it was.
ASKS_THAT_GROW_THEIR_ROW = """([asks, sentence]) => [...document.querySelectorAll(asks)]
  .filter((ask) => ask.querySelector('[data-ask-button]')?.checkVisibility() && ask.querySelector('[data-ask-note][data-ask-word]'))
  .flatMap((ask) => {
    const row = ask.closest('tr');
    const note = ask.querySelector('[data-ask-note]');
    const kept = [...note.childNodes];
    const part = (name, text) => Object.assign(document.createElement('span'), { className: name, textContent: text });
    const before = row.getBoundingClientRect().height;
    note.replaceChildren(part('ask-word', 'Waiting'), part('ask-sentence', sentence));
    const after = row.getBoundingClientRect().height;
    note.replaceChildren(...kept);
    if (after <= before + 1) return [];
    const name = row.querySelector('td, th')?.textContent.trim().split(/\\s+/).slice(0, 4).join(' ');
    return [`the row "${name}" grows from ${Math.round(before)}px to ${Math.round(after)}px when its ask says how its work stands`];
  })"""

# How many lines an element's words run over: the runs of its text that share
# no height, and where the first starts and the last ends.
LINE_COUNT = """
const lineRange = document.createRange();
const linesOf = (el) => {
  lineRange.selectNodeContents(el);
  const rects = [...lineRange.getClientRects()].filter((r) => r.width && r.height).sort((a, b) => a.top - b.top);
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
