"""Probes of controls drawn as controls: their words on one line, and one height to a row."""

from hq.platform.core.tests.browser.probes.describe import DESCRIBE, TEXT_SHOWN
from hq.platform.core.tests.browser.selectors import SELECTORS

# A control drawn as one: a select, or a box the selector names that
# paints an edge, a fill or a shadow. A bare button is a link set in a
# button's element (a row's toggle, a heading's sort), so its words are text,
# and a summary holding a heading is a folded section's head.
_DRAWN = r"""
const seen = (color) => color && !/^(transparent|rgba\(.*,\s*0\))$/.test(color);
const paints = (style) => seen(style.backgroundColor) || style.backgroundImage !== 'none'
  || style.boxShadow !== 'none'
  || ['Top', 'Right', 'Bottom', 'Left'].some((side) => parseFloat(style[`border${side}Width`]) > 0
    && style[`border${side}Style`] !== 'none' && seen(style[`border${side}Color`]));
// A disclosure's face: what a reader presses to open it is the thing it
// discloses, drawn as itself, never a label: a heading that opens its section,
// a reading that opens its panel (partials/_reading.html), tinted while open
// and holding its lead, its quiet lines and its standing, which wrap as text.
const DISCLOSURE_FACE = 'summary:has(h1, h2, h3, h4, h5, h6, hgroup), .reading-head';
const drawn = (el) => !el.matches(DISCLOSURE_FACE)
  && (el.matches('select') || paints(getComputedStyle(el)));
// The words a control says itself, a control inside it left out.
const ownWords = (el, controls) => {
  const copy = el.cloneNode(true);
  copy.querySelectorAll(controls).forEach((inner) => inner.remove());
  return (copy.textContent || el.getAttribute('aria-label') || '').trim().replace(/\s+/g, ' ').slice(0, 40);
};
// What a line of `text` takes in `el`'s face, measured off the page's flow.
const oneLine = (el, text) => {
  const style = getComputedStyle(el);
  const line = document.createElement('span');
  line.textContent = text;
  for (const name of ['fontFamily', 'fontSize', 'fontStyle', 'fontWeight', 'fontStretch', 'fontVariant',
    'fontFeatureSettings', 'letterSpacing', 'wordSpacing', 'textTransform']) line.style[name] = style[name];
  Object.assign(line.style, {position: 'fixed', insetBlockStart: '0', insetInlineStart: '0', visibility: 'hidden', whiteSpace: 'pre'});
  document.body.append(line);
  const width = line.getBoundingClientRect().width;
  line.remove();
  return width;
};
"""

# A control's own words keep one line: a pressed control drawn as one whose
# words lay out on more than one line in any box of it, or a select or an
# input button whose chosen value (which the engine draws, so no range reads
# it) is wider in its face than the box it is drawn in. Words of a control
# inside it are that control's, and words kept from sight are no label.
WRAPPED_CONTROLS = (
    "([pressed, choice, unseen, vendored]) => {"
    + DESCRIBE
    + TEXT_SHOWN
    + _DRAWN
    + """
  const controls = `${pressed}, ${choice}`;
  const words = document.createRange();
  const found = [];
  const lines = (el) => {
    const blocks = new Map();
    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const parent = node.parentElement;
      if (!node.data.trim() || parent.closest(controls) !== el || parent.closest(unseen) || !textShown(node)) continue;
      let block = parent;
      let placed = false;
      while (block !== el) {
        const style = getComputedStyle(block);
        if (['absolute', 'fixed'].includes(style.position)) placed = true;
        if (style.display !== 'inline' && style.display !== 'contents') break;
        block = block.parentElement;
      }
      if (placed) continue;
      words.selectNodeContents(node);
      const rects = [...words.getClientRects()].filter((r) => r.width > 0 && r.height > 0);
      blocks.set(block, [...(blocks.get(block) || []), ...rects]);
    }
    let most = 1;
    for (const rects of blocks.values()) {
      rects.sort((a, b) => a.top - b.top);
      let count = 0;
      let bottom = -Infinity;
      for (const r of rects) {
        if ((r.top + r.bottom) / 2 > bottom) { count += 1; bottom = r.bottom; }
      }
      most = Math.max(most, count);
    }
    return most;
  };
  for (const el of document.querySelectorAll(controls)) {
    if (!el.checkVisibility() || el.closest(vendored) || !drawn(el)) continue;
    const width = Math.round(box(el).width);
    if (el.matches('select, input')) {
      if (el.multiple || el.size > 1) continue;
      const value = el.matches('select') ? (el.selectedOptions[0]?.label ?? '') : el.value;
      if (!value.trim()) continue;
      const style = getComputedStyle(el);
      const room = el.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
      const needs = oneLine(el, value.trim());
      if (needs > room + 1) {
        found.push(`${describe(el)} "${value.trim().replace(/\\s+/g, ' ').slice(0, 40)}" needs ${needs.toFixed(1)}px for its line, has ${room.toFixed(1)}px (${width}px wide)`);
      }
      continue;
    }
    const count = lines(el);
    if (count > 1) found.push(`${describe(el)} "${ownWords(el, controls)}" takes ${count} lines at ${width}px`);
  }
  return found;
}"""
)

# Controls side by side in one row (a toolbar, a page's or a section's head)
# are one height, the control size the tokens set: a control drawn as one
# that shares a line of a flex or grid row with another of a different
# height, and is not the control size itself. A row of compact controls all
# one height is a size of its own. A control in a control (a label's select)
# is part of the outer one; a table's cell is a row of its own, which a table
# lays out.
CONTROL_HEIGHTS = (
    "([pressed, fillable, vendored]) => {"
    + DESCRIBE
    + _DRAWN
    + """
  const controls = `${pressed}, ${fillable}`;
  const size = (() => {
    const probe = document.createElement('div');
    Object.assign(probe.style, {position: 'fixed', visibility: 'hidden', blockSize: 'var(--control-size)', inlineSize: '1px'});
    document.body.append(probe);
    const height = probe.getBoundingClientRect().height;
    probe.remove();
    return height;
  })();
  const rowOf = (el) => {
    for (let up = el.parentElement; up && up !== document.body; up = up.parentElement) {
      if (up.matches('td, th')) return null;
      const display = getComputedStyle(up).display;
      if (display.endsWith('grid')) return up;
      if (display.endsWith('flex') && getComputedStyle(up).flexDirection.startsWith('row')) return up;
    }
    return null;
  };
  const rows = new Map();
  for (const el of document.querySelectorAll(controls)) {
    if (!el.checkVisibility() || el.closest(vendored) || el.matches('textarea, select[multiple], select[size]:not([size="1"]), input[type=checkbox], input[type=radio]')) continue;
    const outer = el.parentElement.closest(controls);
    if (!drawn(el) || (outer && drawn(outer))) continue;
    const row = rowOf(el);
    if (row) rows.set(row, [...(rows.get(row) || []), [el, box(el)]]);
  }
  const found = [];
  for (const [row, members] of rows) {
    for (const [el, r] of members) {
      const beside = members.filter(([other, o]) => other !== el
        && Math.min(r.bottom, o.bottom) - Math.max(r.top, o.top) > Math.min(r.height, o.height) / 2);
      const heights = [...new Set(beside.map(([, o]) => Math.round(o.height)))];
      const differs = beside.some(([, o]) => Math.abs(o.height - r.height) > 1);
      if (!differs || Math.abs(r.height - size) <= 1) continue;
      found.push(`${describe(el)} "${ownWords(el, controls)}" in ${describe(row)} is ${r.height.toFixed(1)}px beside controls of ${heights.join(', ')}px (the control size ${size}px)`);
    }
  }
  return found;
}"""
)

# Where a row's first line of words stands: the padding above it in the
# row's first cell of words, a cell of controls taking some of its own out
# for a control taller than a line.
_ROW_WORDS = """
const wordsOf = (cell) => [...cell.parentElement.children].find((other) => other !== cell
  && other.matches('td, th') && !other.matches(CONTROL_CELL) && other.checkVisibility()) || cell;
const lineOf = (style) => parseFloat(style.lineHeight) || 1.2 * parseFloat(style.fontSize);
"""

# Each control a row's actions cell holds on its first line whose middle is
# off that line's middle, where the words in the cells beside it are set: a
# button taller than a line, set on the line's top, sits lower than the row's
# text. The cell is a row's actions (`{% row_actions %}`) or any cell of a
# column of controls.
ACTIONS_OFF_THE_LINE = (
    "() => {"
    + DESCRIBE
    + f"const CONTROL_CELL = '{SELECTORS['row_actions_cell']}';"
    + _ROW_WORDS
    + """
  const found = [];
  const pressed = 'a, button, form, [role="button"], [data-ask]';
  for (const cell of document.querySelectorAll(CONTROL_CELL)) {
    if (!cell.checkVisibility()) continue;
    const style = getComputedStyle(cell);
    const words = getComputedStyle(wordsOf(cell));
    const line = lineOf(style);
    const middle = box(cell).top + parseFloat(style.borderTopWidth) + parseFloat(words.paddingTop) + line / 2;
    const controls = [...cell.children].filter((el) => el.matches(pressed) && el.checkVisibility() && box(el).height >= 2);
    if (!controls.length) continue;
    const first = box(controls[0]);
    for (const el of controls) {
      const r = box(el);
      if (r.top >= first.bottom) continue;
      const off = r.top + r.height / 2 - middle;
      if (Math.abs(off) > 1.5) {
        found.push(`${describe(el)} sits ${Math.round(Math.abs(off))}px ${off > 0 ? 'below' : 'above'} its row's first line: ${el.textContent.trim().slice(0, 40)}`);
      }
    }
  }
  return [...new Set(found)];
}"""
)

# Each cell of a row's controls whose controls, on one line, make the row
# taller than a line of words: a button taller than the line, set inside the
# cell's whole padding, grows its row past the rows of words beside it. A cell
# holding an ask keeps a line under its button for what the ask says
# (tables.css), so it is left to its own check.
ACTIONS_GROW_THE_ROW = (
    "() => {"
    + DESCRIBE
    + f"const CONTROL_CELL = '{SELECTORS['row_actions_cell']}';"
    + _ROW_WORDS
    + """
  const found = [];
  const pressed = 'a, button, form, [role="button"]';
  for (const cell of document.querySelectorAll(CONTROL_CELL)) {
    if (!cell.checkVisibility() || cell.querySelector(':scope > [data-ask]')) continue;
    const controls = [...cell.children].filter((el) => el.matches(pressed) && el.checkVisibility() && box(el).height >= 2);
    if (!controls.length) continue;
    const first = box(controls[0]);
    const line = controls.map(box).filter((r) => r.top < first.bottom);
    if (line.length < controls.length) continue;
    const style = getComputedStyle(cell);
    const words = getComputedStyle(wordsOf(cell));
    const top = box(cell).top;
    const needs = Math.max(...line.map((r) => r.bottom)) - top + parseFloat(style.paddingBottom) + parseFloat(style.borderBottomWidth);
    const row = parseFloat(words.paddingTop) + lineOf(style) + parseFloat(words.paddingBottom)
      + parseFloat(style.borderTopWidth) + parseFloat(style.borderBottomWidth);
    if (needs > row + 1.5) {
      found.push(`${describe(cell)} holds controls ${Math.round(needs - row)}px taller than its row's line of words: ${cell.textContent.trim().replace(/\\s+/g, ' ').slice(0, 40)}`);
    }
  }
  return [...new Set(found)];
}"""
)

# A row's controls stand side by side while its table has room for them: a
# cell of controls (``cells``) whose controls lay out on more than one line,
# when the table laid out at its least width, this cell's column widened to
# hold them on one line, still fits its region with a pixel to spare. The
# controls are the outermost pressable boxes in the cell, and their line is
# their widths and the least gap between two of them on a shared line (none
# when no two share one), so a row a table truly cannot widen is left alone.
ACTIONS_STACKED_WITH_ROOM = (
    "(cells) => {"
    + DESCRIBE
    + """
  const pressed = 'a[href], button, .btn, [role="button"], input:is([type=submit], [type=button])';
  const held = (el, css, read) => {
    const before = el.style.cssText;
    el.style.cssText += css;
    const value = read();
    el.style.cssText = before;
    return value;
  };
  const least = new Map();
  const leastOf = (table) => {
    if (!least.has(table)) {
      least.set(table, held(table, ';inline-size: min-content !important; min-inline-size: 0 !important; max-inline-size: none !important',
        () => new Map([[table, box(table).width],
          ...[...table.querySelectorAll(cells)].map((cell) => [cell, box(cell).width])])));
    }
    return least.get(table);
  };
  const found = [];
  for (const cell of document.querySelectorAll(cells)) {
    if (!cell.checkVisibility()) continue;
    const controls = [...cell.querySelectorAll(pressed)]
      .filter((el) => el.checkVisibility() && !el.parentElement.closest(pressed))
      .map((el) => [el, box(el)])
      .filter(([, r]) => r.width >= 2 && r.height >= 2);
    if (controls.length < 2) continue;
    const overlaps = (a, b) => Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > Math.min(a.height, b.height) / 2;
    const [, first] = controls[0];
    if (controls.every(([, r]) => overlaps(r, first))) continue;
    let gap = Infinity;
    for (const [, a] of controls) {
      for (const [, b] of controls) {
        if (a !== b && overlaps(a, b) && b.left >= a.right) gap = Math.min(gap, b.left - a.right);
      }
    }
    if (gap === Infinity) gap = 0;
    const line = controls.reduce((sum, [, r]) => sum + r.width, 0) + gap * (controls.length - 1);
    const table = cell.closest('table');
    const region = table.parentElement;
    const widths = leastOf(table);
    const style = getComputedStyle(cell);
    const frame = parseFloat(style.paddingLeft) + parseFloat(style.paddingRight)
      + parseFloat(style.borderLeftWidth) + parseFloat(style.borderRightWidth);
    const column = widths.get(cell) ?? box(cell).width;
    const wanted = widths.get(table) - column + Math.max(column, line + frame);
    if (wanted + 1 <= region.clientWidth) {
      const words = controls.map(([el]) => el.textContent.trim().replace(/\\s+/g, ' ').slice(0, 24)).join(' / ');
      found.push(`${describe(cell)} stands "${words}" on several lines: one line takes ${line.toFixed(1)}px and its table would be ${wanted.toFixed(1)}px of ${region.clientWidth}px`);
    }
  }
  return found;
}"""
)


# The narrowest line a row's words wrap to beside its controls, which the
# stylesheet's `--row-words` is held to.
ROW_WORDS = "30ch"

# A row's words keep a line of ``measure`` (ROW_WORDS) beside its controls: a
# cell of controls (``cells``) beside a widest cell of words whose room is
# under that measure, resolved in the words' own font, some text of which runs
# onto a second line. A row in that shape reads a few words to a line beside a
# column of buttons, where its buttons belong in the line beneath (a narrow
# box, tables.css).
ACTIONS_BESIDE_SQUEEZED_WORDS = (
    "([cells, measure]) => {"
    + DESCRIBE
    + """
  const pressed = 'a[href], button, .btn, [role="button"], input:is([type=submit], [type=button])';
  const found = [];
  // What the stylesheet keeps the words to is the criterion's measure.
  const token = getComputedStyle(document.documentElement).getPropertyValue('--row-words').trim();
  if (token && token !== measure) found.push(`--row-words is ${token}, not ${measure}`);
  const range = document.createRange();
  const wraps = (cell) => {
    const walker = document.createTreeWalker(cell, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      if (!node.textContent.trim() || !node.parentElement.checkVisibility()) continue;
      range.selectNodeContents(node);
      const tops = new Set([...range.getClientRects()].filter((r) => r.width > 0).map((r) => Math.round(r.top)));
      if (tops.size > 1) return true;
    }
    return false;
  };
  const measureIn = (cell) => {
    const ruler = document.createElement('span');
    ruler.style.cssText = `position: absolute; visibility: hidden; inline-size: ${measure}`;
    cell.append(ruler);
    const width = box(ruler).width;
    ruler.remove();
    return width;
  };
  for (const cell of document.querySelectorAll(cells)) {
    if (!cell.checkVisibility()) continue;
    const controls = [...cell.querySelectorAll(pressed)]
      .filter((el) => el.checkVisibility() && !el.parentElement.closest(pressed) && box(el).width >= 2);
    if (!controls.length) continue;
    const words = [...cell.parentElement.children]
      .filter((other) => other !== cell && other.matches('td, th') && !other.matches(cells) && other.checkVisibility());
    if (!words.length) continue;
    const widest = words.reduce((most, other) => (box(other).width > box(most).width ? other : most));
    const style = getComputedStyle(widest);
    const room = box(widest).width - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
    const line = measureIn(widest);
    if (room + 1 < line && wraps(widest)) {
      const said = controls.map((el) => el.textContent.trim().replace(/\\s+/g, ' ').slice(0, 24)).join(' / ');
      found.push(`${describe(cell)} stands "${said}" beside words wrapped in ${room.toFixed(1)}px, under their ${line.toFixed(1)}px line`);
    }
  }
  return found;
}"""
)


# A filter bar's choice (a select, a filter's menu, the sort) is as wide as
# its options, never stretched over the bar: one drawn wider than it lays out
# at its own width, the bar's room being its search's. One a bar too narrow
# for it shrinks stays within its own width, so it passes.
CHOICES_AS_WIDE_AS_THEIR_OPTIONS = (
    "(choices) => {"
    + DESCRIBE
    + """
  const found = [];
  for (const el of document.querySelectorAll(choices)) {
    if (!el.checkVisibility()) continue;
    const drawn = box(el).width;
    const before = el.style.cssText;
    el.style.cssText += ';flex: none !important; inline-size: auto !important';
    const own = box(el).width;
    el.style.cssText = before;
    if (drawn > own + 1) found.push(`${describe(el)} is ${drawn.toFixed(1)}px wide, its options ${own.toFixed(1)}px`);
  }
  return found;
}"""
)


# The survey checks these probes answer (`probes/__init__.py`).
CHECKS = {
    "actions on the first line": ACTIONS_OFF_THE_LINE,
    "actions no taller than the row": ACTIONS_GROW_THE_ROW,
    "row actions stacked with room": (
        ACTIONS_STACKED_WITH_ROOM,
        f"{SELECTORS['row_actions_cell']}, {SELECTORS['queue_actions_cell']}",
    ),
    "row actions beside squeezed words": (
        ACTIONS_BESIDE_SQUEEZED_WORDS,
        [f"{SELECTORS['row_actions_cell']}, {SELECTORS['queue_actions_cell']}", ROW_WORDS],
    ),
    "control words on one line": (
        WRAPPED_CONTROLS,
        [SELECTORS["pressed"], SELECTORS["choice"], SELECTORS["unseen"], SELECTORS["vendored"]],
    ),
    "one height to a row of controls": (
        CONTROL_HEIGHTS,
        [SELECTORS["pressed"], SELECTORS["fillable"], SELECTORS["vendored"]],
    ),
    "filter choices as wide as their options": (
        CHOICES_AS_WIDE_AS_THEIR_OPTIONS,
        ".filter-bar-controls > :is(select, .table-filter-menu, .table-sort)",
    ),
}
