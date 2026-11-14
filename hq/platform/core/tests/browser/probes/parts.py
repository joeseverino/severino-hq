"""Probes of a page's parts: disclosures, labels, the dashboard's readings and a check's evidence."""

from hq.platform.core.tests.browser.probes import Check
from hq.platform.core.tests.browser.probes.describe import DESCRIBE
from hq.platform.core.tests.browser.selectors import SELECTORS

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
    // A box whose content the browser skips (`hidden="until-found"`) draws
    // no label until it is found.
    if (style.overflowX !== 'visible' || style.display === 'inline' || style.contentVisibility === 'hidden') continue;
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


# A closed row of a table in a narrow box shows every control it shows in a
# whole box: what a row offers stands in the row (beneath its words, on a
# phone), never only behind its toggle. The row's own table is drawn in a
# whole box for a moment to read what it offers there.
ROW_ACTIONS_BEHIND_THE_TOGGLE = (
    "([rows, pressed]) => {"
    + DESCRIBE
    + """
  const found = [];
  const said = (el) => el.textContent.trim().replace(/\\s+/g, ' ');
  const offered = (row) => new Set([...row.querySelectorAll(pressed)]
    .filter((el) => el.checkVisibility() && !el.matches('[data-row-toggle], [data-fold-toggle]') && said(el))
    .map(said));
  for (const row of document.querySelectorAll(rows)) {
    if (!row.checkVisibility() || row.matches('.is-expanded')) continue;
    const table = row.closest('table');
    if (getComputedStyle(table).getPropertyValue('--box').trim() !== 'narrow') continue;
    const narrow = offered(row);
    const before = [table.style.getPropertyValue('--box'), table.style.getPropertyPriority('--box')];
    table.style.setProperty('--box', 'whole', 'important');
    const whole = offered(row);
    table.style.setProperty('--box', ...before);
    if (!before[0]) table.style.removeProperty('--box');
    const held = [...whole].filter((label) => !narrow.has(label));
    if (held.length) found.push(`${describe(row)} shows "${held.join(' / ')}" only once opened in its narrow box`);
  }
  return found;
}"""
)


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


# A link among words differs from them by more than its color (WCAG 1.4.1):
# it is underlined at rest, or set in a weight, size, style or face of its
# own, the cues axe's link-in-text-block accepts. A link that is the whole of
# its line, row, item or chip is never underlined at rest. The words around a
# link are the text its nearest box lays out in line with it, outside every
# link and up to a forced break; a version or a count alone is no words, nor
# is a box inside that line (a chip, a badge), and a link laid out as a block
# stands alone unless it is an item in a row of words. Read from the rendered
# page, so every link on every page is judged at every width, whatever markup
# or class drew it.
LINKS_AT_REST = """([links, vendored]) => {
  const style = (el) => getComputedStyle(el);
  const atomic = (el) => !['inline', 'contents'].includes(style(el).display);
  const inLine = (el) => /inline/.test(style(el).display);
  const row = (el) => /flex|grid/.test(style(el).display);
  const shown = (node) => {
    const range = document.createRange();
    range.selectNodeContents(node);
    return [...range.getClientRects()].some((r) => r.width > 1 && r.height > 1);
  };
  const lettered = (node) => /\\p{L}/u.test(node.textContent) && shown(node);
  const among = (link, box) => {
    const inBox = (el) => {
      while (el !== box && !atomic(el)) el = el.parentElement;
      return el === box;
    };
    const walker = document.createTreeWalker(box, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
    let line = 0;
    let own = -1;
    const worded = new Map();
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      if (node === link) own = line;
      else if (node.nodeType === Node.ELEMENT_NODE && node.localName === 'br' && inBox(node.parentElement)) line += 1;
      else if (node.nodeType === Node.TEXT_NODE && !node.parentElement.closest(links) && inBox(node.parentElement) && lettered(node)) {
        worded.set(line, worded.get(line) || node.parentElement);
      }
    }
    return worded.get(own);
  };
  const face = (el) => ['font-weight', 'font-size', 'font-style', 'font-family'].map((p) => style(el).getPropertyValue(p)).join();
  const ownText = (link) => {
    const walker = document.createTreeWalker(link, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) if (lettered(node)) return node.parentElement;
    return link;
  };
  const found = [];
  for (const link of document.querySelectorAll(links)) {
    if ((vendored && link.closest(vendored)) || !link.textContent.trim() || !shown(link)) continue;
    if (link.matches(':hover, :focus-visible')) continue;
    let box = link.parentElement;
    while (box && !atomic(box)) box = box.parentElement;
    const words = box && (inLine(link) || (link.parentElement === box && row(box))) && among(link, box);
    const underlined = style(link).textDecorationLine.includes('underline');
    const name = link.textContent.trim().replace(/\\s+/g, ' ').slice(0, 60);
    const where = box ? [box.localName, ...box.classList].join('.') : 'the page';
    if (words && !underlined && face(ownText(link)) === face(words)) {
      found.push(`link ${name} sits among words in ${where} and differs from them only by its color`);
    }
    if (!words && underlined) found.push(`link ${name} stands alone in ${where} and is underlined at rest`);
  }
  return found;
}"""

# A dialog a page declares is one a reader can open: some control on the page
# names it, so drawing a dialog without its control is a finding.
DIALOGS_WITHOUT_OPENERS = """([dialogs, openers]) => {
  const opened = (dialog) => document.querySelector(openers.replaceAll('{id}', dialog.id.slice('modal-'.length)));
  return [...document.querySelectorAll(dialogs)]
    .filter((dialog) => !opened(dialog))
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


EVIDENCE_READ = (STRANDED_EVIDENCE, [SELECTORS["check_statement"], SELECTORS["check_evidence"]])

SUMMARIES_READ = (HIDDEN_IN_SUMMARIES, SELECTORS["summary_value"])

_READINGS_OF = f"(selector) => ({READINGS})([...document.querySelectorAll(selector)])"

# The survey checks these probes answer (`probes/__init__.py`).
CHECKS = {
    "control labels": Check(
        (SPILLED_LABELS, SELECTORS["control"]), reader_sees="a control's label spilling out of its box"
    ),
    "cell labels": Check(
        (SPILLED_LABELS, SELECTORS["cell_content"]), table=True, reader_sees="a cell's words spilling out of their box"
    ),
    "dialogs": Check(
        (DIALOGS_WITHOUT_OPENERS, [SELECTORS["dialog"], SELECTORS["dialog_opener"]]),
        reader_sees="a dialog nothing on the page opens",
    ),
    "links at rest": Check(
        (LINKS_AT_REST, [SELECTORS["link"], SELECTORS["vendored"]]),
        reader_sees="a link among words told apart from them by color alone",
    ),
    "readings": Check(
        (_READINGS_OF, SELECTORS["readings"]),
        findings=reading_problems,
        reader_sees="a reading's line cut short or pushed under its mark",
    ),
    "row actions behind the toggle": Check(
        (
            ROW_ACTIONS_BEHIND_THE_TOGGLE,
            [f"main {SELECTORS['controls_row']}", SELECTORS["pressed"]],
        ),
        reader_sees="what a row offers hidden until the row is opened",
    ),
}
