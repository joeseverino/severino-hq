"""Probes of controls drawn as controls: their words on one line, and one height to a row."""

from hq.platform.core.tests.browser.probes import Check
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
    // Controls on a line of their own beneath the words (a row stacked in a
    // narrow box, tables.css) stand beside none of them.
    if (box(cell).top >= box(widest).bottom - 1) continue;
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


# A control of words shows them: one whose words are drawn at no size while a
# glyph its stylesheet writes stands in their place (a lone "x" for Dismiss)
# leaves a reader to guess what it does.
WORDS_SWAPPED_FOR_A_GLYPH = (
    "(selector) => {"
    + DESCRIBE
    + """
  const found = [];
  const drawn = (el, pseudo) => !['none', 'normal'].includes(getComputedStyle(el, pseudo).content);
  for (const el of document.querySelectorAll(selector)) {
    if (!el.checkVisibility() || !/\\p{L}/u.test(el.textContent)) continue;
    if (!drawn(el, '::before') && !drawn(el, '::after')) continue;
    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    let shown = false;
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      if (/\\p{L}/u.test(node.textContent) && parseFloat(getComputedStyle(node.parentElement).fontSize) > 0) shown = true;
    }
    if (!shown) found.push(`${describe(el)} draws "${el.textContent.trim().slice(0, 24)}" at no size behind a glyph`);
  }
  return found;
}"""
)


# A control's words are one box: a control is a flex box (page.css, Buttons),
# where each run of text and each word box `breakable` draws (a hyphenated
# name, a path) is an item of its own, set apart by the gap instead of the
# space between them. `button_words` holds them in one.
CONTROL_WORDS_IN_ONE_BOX = (
    "(selector) => {"
    + DESCRIBE
    + """
  const found = [];
  const piece = (node) => (node.nodeType === Node.TEXT_NODE
    ? node.textContent.trim() !== '' : node.nodeType === Node.ELEMENT_NODE && node.matches('.token, .seam'));
  for (const el of document.querySelectorAll(selector)) {
    if (!el.checkVisibility() || !/flex|grid/.test(getComputedStyle(el).display)) continue;
    const pieces = [...el.childNodes].filter(piece);
    if (pieces.length > 1 && pieces.some((node) => node.nodeType === Node.ELEMENT_NODE)) {
      found.push(`${describe(el)} draws "${el.textContent.trim().replace(/\\s+/g, ' ').slice(0, 32)}" as ${pieces.length} items`);
    }
  }
  return found;
}"""
)


# The survey checks these probes answer (`probes/__init__.py`).
CHECKS = {
    "row actions beside squeezed words": Check(
        (
            ACTIONS_BESIDE_SQUEEZED_WORDS,
            [SELECTORS["row_actions_cell"], ROW_WORDS],
        ),
        reader_sees="a row's words squeezed a few to a line beside its buttons",
    ),
    "words swapped for a glyph": Check(
        (WORDS_SWAPPED_FOR_A_GLYPH, SELECTORS["pressed"]),
        reader_sees="a control showing a glyph where its words belong",
    ),
    "control words in one box": Check(
        (CONTROL_WORDS_IN_ONE_BOX, SELECTORS["pressed"]),
        reader_sees="a control's words drawn apart, as pieces with gaps between them",
    ),
    "control words on one line": Check(
        (
            WRAPPED_CONTROLS,
            [SELECTORS["pressed"], SELECTORS["choice"], SELECTORS["unseen"], SELECTORS["vendored"]],
        ),
        reader_sees="a control's words broken across lines, or a select's choice cut short",
    ),
}
