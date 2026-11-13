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

# A folded section's words sit on a panel, as its neighbors' do, open or
# closed: some box between them and the page paints a background. A table's
# region is one; words in a part drawn only once the fold opens are read when
# it has drawn.
BARE_FOLDS = (
    "([folds, drawnWhenOpened]) => {"
    + DESCRIBE
    + """
  const page = new Set([document.documentElement, document.body]);
  const clear = (paint) => paint === 'transparent' || /(?:,\\s*|\\/\\s*)0\\)$/.test(paint);
  const painted = (el) => {
    for (let at = el; at && !page.has(at); at = at.parentElement) {
      if (!clear(getComputedStyle(at).backgroundColor)) return true;
    }
    return false;
  };
  const found = [];
  for (const fold of document.querySelectorAll('main ' + folds)) {
    const words = document.createTreeWalker(fold, NodeFilter.SHOW_TEXT);
    for (let node = words.nextNode(); node; node = words.nextNode()) {
      const el = node.parentElement;
      if (!node.textContent.trim() || el.closest('summary') || el.closest(drawnWhenOpened)) continue;
      if (!painted(el)) {
        found.push(`${describe(fold)}: "${node.textContent.trim().slice(0, 40)}" sits on the page`);
        break;
      }
    }
  }
  return found;
}"""
)

# Every select that opens to its choices, and every filter button that opens
# to them, wears HQ's chevron, and a select no arrow of the browser's own,
# whichever way the engine draws it (base-select or native), so no two forms
# read as two kinds of control. The chevron is
# compared with the token painted where the select sits, since its ink is
# light-dark(). A vendored widget draws its own.
SELECT_ARROWS = """([dropdowns, vendored]) => {
  const named = (el) => el.name || el.getAttribute('aria-label') || el.id || 'unnamed';
  return [...document.querySelectorAll(dropdowns)]
    .filter((el) => !(el instanceof HTMLSelectElement && (el.multiple || el.size > 1)) && !(vendored && el.closest(vendored)))
    .flatMap((el) => {
      const style = getComputedStyle(el);
      const own = el instanceof HTMLSelectElement && (style.appearance === 'base-select'
        ? getComputedStyle(el, '::picker-icon').display !== 'none'
        : style.appearance !== 'none');
      const mark = document.createElement('span');
      mark.style.setProperty('background-image', 'var(--chevron-image)');
      el.parentElement.append(mark);
      const chevron = getComputedStyle(mark).backgroundImage;
      mark.remove();
      return [
        own && `select ${named(el)} draws the browser's own arrow`,
        style.backgroundImage !== chevron && `${el.localName} ${named(el) === 'unnamed' ? el.textContent.trim().slice(0, 30) : named(el)} does not wear HQ's chevron`,
      ].filter(Boolean);
    });
}"""

# A link's underline runs under its name alone. What a link draws in its
# line (its shown text and the content of every pseudo-element laid out in
# it) never ends in a sentence's mark (`breakable`'s, or a comma, semicolon or
# colon, or a period after a one-word name) nor begins or ends with a
# separator, which belong to the sentence or list around it; and a tag (a
# status pill) is never set inside a link it is not itself, since a link laid
# out as a box draws its line under every box within it. Every link whether or
# not it is underlined at rest, since hover underlines it, but a card: a link
# holding a block of its own, stacked rather than in its row, lays out
# paragraphs, whose own sentences end it.
LINED_PAST_THE_NAME = """([links, vendored, tag, mark]) => {
  const MARK = /[,;:]$|^\\S+\\.$/u;
  const BLOCK = /^(block|flex|grid|table|list-item|flow-root)$/;
  // A box laid out in a row with its siblings (a flex item of a row) is still
  // a part of the link's one line.
  const inRow = (el) => {
    const style = getComputedStyle(el.parentElement);
    return /flex/.test(style.display) && style.flexDirection.startsWith('row');
  };
  const SEPARATOR = /^[·•|]|[·•|]$/u;
  const quoted = (content) => (content.match(/^"((?:[^"\\\\]|\\\\.)*)"/) || [null, ''])[1];
  const inFlow = (style) => style.display !== 'none' && style.visibility === 'visible'
    && !['absolute', 'fixed'].includes(style.position) && style.float === 'none';
  const pseudo = (el, which) => {
    const style = getComputedStyle(el, which);
    return inFlow(style) ? quoted(style.content) : '';
  };
  const drawn = (el) => {
    if (getComputedStyle(el).display === 'none') return '';
    let text = pseudo(el, '::before');
    for (const node of el.childNodes) {
      if (node.nodeType === Node.TEXT_NODE) {
        if (getComputedStyle(node.parentElement).visibility === 'visible') text += node.textContent;
      } else if (node.nodeType === Node.ELEMENT_NODE) text += drawn(node);
    }
    return text + pseudo(el, '::after');
  };
  const found = [];
  for (const link of document.querySelectorAll(links)) {
    if ((vendored && link.closest(vendored)) || !link.getClientRects().length) continue;
    if ([...link.querySelectorAll('*')].some((el) => BLOCK.test(getComputedStyle(el).display) && !inRow(el))) continue;
    const line = drawn(link).replace(/[\\s\\u200b\\u2060]+/gu, ' ').trim();
    if (!line) continue;
    const name = line.slice(0, 60);
    const shown = [...link.querySelectorAll(mark)].some((el) => getComputedStyle(el).visibility === 'visible');
    if (shown || MARK.test(line)) found.push(`link "${name}" draws its sentence's mark in its line`);
    if (SEPARATOR.test(line)) found.push(`link "${name}" draws a separator in its line`);
    const inner = [...link.querySelectorAll(tag)].find((el) => el.getClientRects().length);
    if (inner) found.push(`link "${name}" holds the tag "${inner.textContent.trim().slice(0, 30)}"`);
  }
  return found;
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

# A fact whose value is a list of names keeps no other pair on its row while
# it wraps: beside them it makes their row as tall as itself.
WRAPPED_BESIDE_A_PAIR = (
    "([values, pairs]) => {"
    + DESCRIBE
    + LINE_COUNT
    + """
  const found = [];
  for (const value of document.querySelectorAll(values)) {
    if (!value.checkVisibility()) continue;
    const {lines, top, bottom} = linesOf(value);
    if (lines < 2) continue;
    const pair = value.closest(pairs);
    const beside = [...pair.parentElement.children].some((other) => {
      if (other === pair || !other.checkVisibility()) return false;
      const r = other.getBoundingClientRect();
      return r.top < bottom - 1 && r.bottom > top + 1;
    });
    const name = pair.querySelector(':scope > dt')?.textContent.trim().replace(/\\s+/g, ' ');
    if (beside) found.push(`${describe(value)} "${name}" wraps to ${lines} lines beside another pair`);
  }
  return found;
}"""
)


EVIDENCE_READ = (STRANDED_EVIDENCE, [SELECTORS["check_statement"], SELECTORS["check_evidence"]])

SUMMARIES_READ = (HIDDEN_IN_SUMMARIES, SELECTORS["summary_value"])

_READINGS_OF = f"(selector) => ({READINGS})([...document.querySelectorAll(selector)])"

# The survey checks these probes answer (`probes/__init__.py`).
CHECKS = {
    "control labels": (SPILLED_LABELS, SELECTORS["control"]),
    "cell labels": Check((SPILLED_LABELS, SELECTORS["cell_content"]), table=True),
    "dialogs": (DIALOGS_WITHOUT_OPENERS, [SELECTORS["dialog"], SELECTORS["dialog_opener"]]),
    "select arrows": (SELECT_ARROWS, [SELECTORS["dropdown"], SELECTORS["vendored"]]),
    "link underlines": (PARTS_NOT_UNDERLINED, [SELECTORS["link"], SELECTORS["vendored"]]),
    "links at rest": (LINKS_AT_REST, [SELECTORS["link"], SELECTORS["vendored"]]),
    "lined past the name": (
        LINED_PAST_THE_NAME,
        [SELECTORS["link"], SELECTORS["vendored"], SELECTORS["tag"], SELECTORS["mark"]],
    ),
    "lists of facts": (WRAPPED_BESIDE_A_PAIR, [SELECTORS["fact_list"], SELECTORS["band_fact_cell"]]),
    "bare folds": (BARE_FOLDS, [SELECTORS["section_fold"], SELECTORS["drawn_when_opened"]]),
    # The dashboard's readings, which no other page draws.
    "readings": Check((_READINGS_OF, SELECTORS["readings"]), only_on="/", findings=reading_problems),
}
