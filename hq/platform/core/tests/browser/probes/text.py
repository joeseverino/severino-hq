"""Probes of running text: broken words, text that overlaps and text past its clamp."""

from decimal import Decimal

from hq.platform.core.tests.browser.probes import Check, Plant
from hq.platform.core.tests.browser.probes.describe import CUTS, DESCRIBE, LINES, PAINTED_PART, TEXT_SHOWN
from hq.platform.core.tests.browser.selectors import SELECTORS, written

# A plain word split across lines inside a table cell: its column was given
# less than the word, because another column's content took the width. A word
# inside a run with no space in it (a link, a digest, an identifier however its
# seams split it) that is itself wider than its line breaks somewhere wherever
# a closed row's column is, so it is not counted there; in an open row only a
# word wider than the room the row owes it may break.
BROKEN_WORDS = (
    "(identifier) => {"
    + DESCRIBE
    + TEXT_SHOWN
    + f"const anywhere = '{written(SELECTORS['breaks_anywhere'])}';"
    + """
  const found = [];
  const range = document.createRange();
  const across = (node, start, end) => {
    range.setStart(node, start);
    range.setEnd(node, end);
    return [...range.getClientRects()].filter((r) => r.width);
  };
  // The line a run lays out in is its box's: the nearest one that is not
  // inline, inside its padding (a chip's is narrower than its cell).
  const lineOf = (node, cell) => {
    let box = node.parentElement;
    while (box !== cell && ['inline', 'contents'].includes(getComputedStyle(box).display)) box = box.parentElement;
    return inside(box);
  };
  const inside = (box) => {
    const style = getComputedStyle(box);
    return box.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
  };
  const widthOf = (node, start, end) => across(node, start, end).reduce((sum, r) => sum + r.width, 0);
  // An identifier (`breakable`'s token, one word) is one run, though its
  // seams split it into several text nodes.
  const tokenWidth = (token) => {
    const walker = document.createTreeWalker(token, NodeFilter.SHOW_TEXT);
    let sum = 0;
    for (let node = walker.nextNode(); node; node = walker.nextNode()) sum += widthOf(node, 0, node.length);
    return sum;
  };
  for (const cell of document.querySelectorAll('main td')) {
    const open = cell.closest('tr:has(> * > [aria-expanded="true"]), tr:has(> * > [aria-expanded="true"]) + tr[data-row-detail]');
    const walker = document.createTreeWalker(cell, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      if (!textShown(node)) continue;
      const line = lineOf(node, cell);
      // What an open row's line beneath owes a value: the whole line, while
      // beside its label the value is left less of it than the label takes.
      const beneath = open && node.parentElement.closest('tr[data-row-detail] > td > *');
      const room = beneath && 2 * line < inside(beneath) ? inside(beneath) : line;
      const token = node.parentElement.closest(identifier);
      const loose = !!node.parentElement.closest(anywhere);
      for (const run of node.textContent.matchAll(/\\S+/g)) {
        const [wide, length] = token
          ? [tokenWidth(token) > line, token.textContent.trim().length]
          : [widthOf(node, run.index, run.index + run[0].length) > line, run[0].length];
        for (const match of run[0].matchAll(/[A-Za-z]{4,}/g)) {
          const at = run.index + match.index;
          const rects = across(node, at, at + match[0].length);
          if (new Set(rects.map((r) => Math.round(r.top))).size < 2) continue;
          // A closed row's run wider than its line breaks wherever its column
          // is, and a name's lone word too, as an identifier's. An open row shows a value whole, breaking it at its seams,
          // spaces and hyphens: a word breaks there only when what it stands
          // in between two of them is wider than all the room the row has.
          const part = [...run[0].matchAll(/[^-]*-?/g)].find((p) => p.index <= match.index && match.index < p.index + p[0].length);
          const must = open
            ? widthOf(node, run.index + part.index, run.index + part.index + part[0].length) > room
            : wide && (loose || match[0].length < length);
          if (!must) found.push(`${describe(cell)} breaks "${match[0]}"`);
        }
      }
    }
  }
  return [...new Set(found)];
}"""
)


# A word or a number of running text broken across lines: neither ever
# breaks. A number is a figure with its sign and currency mark ("\u2212$1,243.52",
# "+5%"), which UAX #14 lets a line end inside (between the sign and the mark,
# both class PR) unless the writer glues them. One inside a run with no space
# in it (a digest, a key) that is itself wider than its line is where that run
# had to break, as BROKEN_WORDS counts it.
BROKEN_WORDS_IN_TEXT = (
    "(identifier) => {"
    + DESCRIBE
    + TEXT_SHOWN
    + r"""
  const WORD = /[A-Za-z]{4,}/g;
  // A figure standing on its own (not the digits inside a word), with what
  // goes before it: a sign, a word joiner, a currency mark.
  const NUMBER = /(?<![\p{L}\p{N}_.,$\u00a3\u20ac\u2060+\u2212])(?:[+\u2212]\u2060?)?[$\u00a3\u20ac]?\d(?:[\d,.]*\d)?%?/gu;
  const main = document.querySelector('main');
  const range = document.createRange();
  const across = (node, start, end) => {
    range.setStart(node, start);
    range.setEnd(node, end);
    return [...range.getClientRects()].filter((r) => r.width);
  };
  // The line a run lays out in is its box's: the nearest one that is not inline.
  const lineOf = (el) => {
    let box = el;
    while (box !== main && ['inline', 'contents'].includes(getComputedStyle(box).display)) box = box.parentElement;
    const style = getComputedStyle(box);
    return box.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
  };
  const found = [];
  const words = document.createTreeWalker(main, NodeFilter.SHOW_TEXT);
  for (let node = words.nextNode(); node; node = words.nextNode()) {
    const el = node.parentElement;
    if (el.closest(identifier) || el.closest('pre, script, style, template') || !textShown(node)) continue;
    const runs = [...node.data.matchAll(/\S+/g)];
    for (const [kind, pattern] of [['word', WORD], ['number', NUMBER]]) {
      for (const token of node.data.matchAll(pattern)) {
        const lines = new Set(across(node, token.index, token.index + token[0].length).map((r) => Math.round(r.top)));
        if (lines.size < 2) continue;
        const run = runs.find((r) => r.index <= token.index && token.index < r.index + r[0].length);
        const wide = across(node, run.index, run.index + run[0].length).reduce((sum, r) => sum + r.width, 0) > lineOf(el);
        if (!(wide && token[0].length < run[0].length)) found.push(`${describe(el)} breaks the ${kind} "${token[0].replace(/\u2060/g, '')}"`);
      }
    }
  }
  return [...new Set(found)];
}"""
)

# A line of running text that opens on a stray: a separator (a dot written in
# the text, or one a part of a run draws before itself and nothing clips), a
# line that holds only marks (an icon, an asterisk) apart from the words they
# belong to, or one word alone that the line before cut from its number ("12"
# above "days"; a number holds a digit and no letter, so a name that ends in
# one, "host-01", is none) or, in a cell, that a box set whole after it left behind
# ("on" above a name), where the break between them is a wrap and not a `br`:
# a cell cuts a name, so one never needs a line of its own. A value of a list
# (a user's code before the next) is no running word, so a list wrapping
# between its values leaves none behind. Each block's
# lines are read from what it lays out in them: its own text, and each box set
# in its line whole (a link drawn as a block, an icon) as one piece. A wrapping flex box's items are its lines, and an
# item opening a row opens a line. Text kept from sight (`unseen`) is
# not drawn.
STRAYS_OPENING_A_LINE = (
    "([unseen, identifier]) => {"
    + DESCRIBE
    + PAINTED_PART
    + TEXT_SHOWN
    + r"""
  // A middle dot, a bullet, a bar, an en or an em dash, or a slash standing
  // alone: one a word follows ("/api") is a path.
  const SEPARATOR = /^(?:[·•|\u2013\u2014]|\/(?!\S))/;
  const MARKS_ONLY = /^[^\p{L}\p{N}]*$/u;
  const TYPED = 'textarea, input:not([type=checkbox], [type=radio], [type=image], [type=range], [type=color], [type=file])';
  const range = document.createRange();
  const inlineDisplay = (el) => ['inline', 'contents'].includes(getComputedStyle(el).display);
  const blockOf = (el) => { while (el.parentElement && inlineDisplay(el)) el = el.parentElement; return el; };
  const drawnRect = (el, rect) => rect && rect.width > 0.5 && painted(el, rect);
  // A box set in its block's line whole: an inline block, an inline flex or
  // grid box, a clamped one (`-webkit-inline-box`), or a replaced element (an
  // icon, an image).
  const atomic = (el) => {
    const display = getComputedStyle(el).display;
    return /^(-webkit-)?inline-/.test(display) || (display === 'inline' && el instanceof SVGSVGElement) || el.matches('img, svg, input, select, button');
  };
  // The separator a part draws before itself (`::before`), where its box
  // lands: its own size from its margin before the part's first line.
  const drawnSeparator = (el) => {
    const style = getComputedStyle(el, '::before');
    const content = style.content.match(/^"((?:[^"\\]|\\.)*)"/);
    if (!content || !SEPARATOR.test(content[1].trim())) return null;
    const first = [...el.getClientRects()].find((r) => r.width) || el.getBoundingClientRect();
    const left = first.left + (parseFloat(style.marginLeft) || 0);
    const width = parseFloat(style.width) || parseFloat(style.fontSize) / 2;
    // Only its middle, the glyph a centered mark draws, counts as drawn.
    const middle = left + width / 2;
    const rect = {left: middle - 1, right: middle + 1, top: first.top, bottom: first.bottom, width: 2, height: first.height};
    return painted(el, rect) ? content[1].trim() : null;
  };
  const pieces = new Map();
  const add = (block, piece) => { if (!pieces.has(block)) pieces.set(block, []); pieces.get(block).push(piece); };
  const main = document.querySelector('main');
  const walk = (el) => {
    for (const child of el.childNodes) {
      if (child.nodeType === Node.TEXT_NODE) {
        if (!child.data.trim() || !textShown(child)) continue;
        const block = blockOf(child.parentElement);
        for (let at = 0; at < child.length; at++) {
          if (!child.data[at].trim()) { add(block, {ch: ' '}); continue; }
          range.setStart(child, at);
          range.setEnd(child, at + 1);
          const rect = [...range.getClientRects()].find((r) => drawnRect(child.parentElement, r));
          if (rect) add(block, {ch: child.data[at], rect, el: child.parentElement});
        }
      } else if (child.nodeType === Node.ELEMENT_NODE) {
        // `checkVisibility()` is false on a box with no box of its own (a
        // table's row groups, `display: contents`), whose children are drawn.
        const boxless = getComputedStyle(child).display === 'contents';
        if (child.matches(`${unseen}, script, style, template, pre`) || (!boxless && !child.checkVisibility({visibilityProperty: true}))) continue;
        if (child.matches('br')) { add(blockOf(child.parentElement), {br: true}); continue; }
        const separator = drawnSeparator(child);
        if (separator) add(blockOf(child), {ch: separator, rect: [...child.getClientRects()].find((r) => r.width), el: child});
        if (!atomic(child)) { walk(child); continue; }
        // Set in its parent's line whole: one piece, its words as its text,
        // a field for words as what it shows (what is typed, else its
        // placeholder), and a box with none (an icon, an image, a box to
        // tick) as a mark.
        const rect = child.getBoundingClientRect();
        const words = child.matches(TYPED) ? child.value || child.placeholder || 'field' : child.textContent;
        // Its opening character is drawn by what holds its first text (a
        // path's code), which says whether that character is a separator.
        const texts = document.createTreeWalker(child, NodeFilter.SHOW_TEXT);
        let first = texts.nextNode();
        while (first && !first.data.trim()) first = texts.nextNode();
        if (drawnRect(child, rect)) add(blockOf(child.parentElement), {ch: words.trim() || '▪', rect, el: child, opens: first?.parentElement ?? child, whole: true});
        if (!child.matches('svg, img')) walk(child);
      }
    }
  };
  walk(main);
  const found = [];
  for (const [block, list] of pieces) {
    // Lines in reading order: a piece that starts below the line so far
    // starts the next one.
    const lines = [];
    let gap = false;
    let forced = false;
    for (const piece of list) {
      if (piece.br) { forced = true; continue; }
      if (!piece.rect) { gap = true; continue; }
      const line = lines.at(-1);
      if (!line || piece.rect.top >= line.bottom - Math.min(piece.rect.height, line.bottom - line.top) / 2) {
        lines.push({top: piece.rect.top, bottom: piece.rect.bottom, pieces: [piece], text: piece.ch, forced});
        forced = false;
      } else {
        line.bottom = Math.max(line.bottom, piece.rect.bottom);
        line.text += (gap ? ' ' : '') + piece.ch;
        line.pieces.push(piece);
      }
      gap = false;
    }
    if (lines.length < 2) continue;
    const name = describe(block);
    const inCell = Boolean(block.closest('td, th'));
    lines.forEach((line, index) => {
      const text = line.text.trim();
      const opening = line.pieces.find((piece) => piece.ch.trim());
      if (index && SEPARATOR.test(text) && !(opening.opens ?? opening.el).closest(identifier)) found.push(`${name} opens a line with "${text[0]}": "${text.slice(0, 30)}"`);
      else if (MARKS_ONLY.test(text) && !(opening.opens ?? opening.el).closest(identifier)) found.push(`${name} holds "${text.slice(0, 30)}" on a line of its own (▪ is an icon or an image)`);
      else if (index && !line.forced && /^\p{L}+[.,;:]?$/u.test(text) && /(?:^|\s)[^\p{L}\s]*\p{N}[^\p{L}\s]*$/u.test(lines[index - 1].text.trim())) {
        found.push(`${name} cuts "${text}" from its number "${lines[index - 1].text.trim().split(/\s/).at(-1).slice(-30)}"`);
      } else if (inCell && (!index || line.forced) && !line.pieces.some((piece) => piece.whole || piece.el?.closest(identifier)) && /^\p{L}+$/u.test(text) && lines[index + 1] && !lines[index + 1].forced && lines[index + 1].pieces[0].whole) {
        found.push(`${name} cuts "${text}" from "${lines[index + 1].text.trim().slice(0, 30)}" after it`);
      }
    });
  }
  // A wrapping flex box's rows: the item that opens one opens a line.
  for (const box of main.querySelectorAll('*')) {
    const style = getComputedStyle(box);
    if (!/flex/.test(style.display) || style.flexWrap === 'nowrap' || style.flexDirection.startsWith('column') || !box.checkVisibility()) continue;
    const items = [...box.children].filter((item) => item.checkVisibility() && !['absolute', 'fixed'].includes(getComputedStyle(item).position))
      .map((item) => [item, item.getBoundingClientRect()]).filter(([, r]) => r.width && r.height);
    const rows = [];
    for (const [item, r] of items) {
      const row = rows.find((one) => r.top < one.bottom && r.bottom > one.top);
      if (row) { row.items.push([item, r]); row.bottom = Math.max(row.bottom, r.bottom); } else rows.push({top: r.top, bottom: r.bottom, items: [[item, r]]});
    }
    if (rows.length < 2) continue;
    const start = style.direction === 'rtl' ? (r) => -r.right : (r) => r.left;
    for (const row of rows.slice(1)) {
      const [opening] = row.items.reduce((first, one) => (start(one[1]) < start(first[1]) ? one : first));
      const first = (pieces.get(opening) || []).find((piece) => piece.rect);
      const mark = drawnSeparator(opening) || (first && SEPARATOR.test(first.ch) && !(first.opens ?? first.el).closest(identifier) ? first.ch : null);
      // What the row shows, never the text of what it holds hidden (a closed popover).
      if (mark) found.push(`${describe(box)} opens a row with "${mark[0]}": "${opening.innerText.trim().slice(0, 30)}"`);
    }
  }
  return [...new Set(found)];
}"""
)

# Two lines of text drawn over each other, each as much of it as is painted.
# A layer that floats over the page (`floating`: an open menu, a dialog) covers
# the page's text on purpose, so a line is held only against lines in its own
# layer.
OVERLAPPING_TEXT = (
    "(floating) => {"
    + DESCRIBE
    + PAINTED_PART
    + """
  const rects = [];
  const range = document.createRange();
  const walker = document.createTreeWalker(document.querySelector('main'), NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    if (!node.data.trim()) continue;
    range.selectNodeContents(node);
    for (const line of range.getClientRects()) {
      const r = painted(node.parentElement, line);
      if (r && r.width > 2 && r.height > 2) rects.push([node, r, node.parentElement.closest(floating)]);
    }
  }
  rects.sort((a, b) => a[1].top - b[1].top);
  const found = [];
  for (let i = 0; i < rects.length; i++) {
    const [one, a, layer] = rects[i];
    for (let j = i + 1; j < rects.length && rects[j][1].top < a.bottom - 2; j++) {
      const [other, b, its] = rects[j];
      if (one === other || layer !== its) continue;
      const x = Math.min(a.right, b.right) - Math.max(a.left, b.left);
      const y = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
      if (x > 2 && y > 2) {
        found.push(`"${one.data.trim().slice(0, 30)}" (${describe(one.parentElement)}) over "${other.data.trim().slice(0, 30)}" (${describe(other.parentElement)})`);
      }
    }
  }
  return [...new Set(found)];
}"""
)

# Two runs of text set side by side on one line with no space drawn between
# them and less than a space's width between their glyphs, each as much of it
# as is painted: a heading and the status beside it in a flex row, where the
# space between them in the markup is dropped, read as one word. Runs in one
# block's line with nothing between their glyphs, or set a fraction of a
# pixel into each other (a search match marked inside its word), are one word
# the markup wrote across elements, as is a name `breakable` split at its
# seams (`identifier`);
# a run that ends opening, or starts closing, a phrase or a figure (a bracket,
# a comma, a dash) is set against its neighbor on purpose. A line is held only
# against lines in its own layer, as overlapping text holds it.
TOUCHING_TEXT = (
    "([floating, identifier]) => {"
    + DESCRIBE
    + PAINTED_PART
    + r"""
  const range = document.createRange();
  const blockOf = (el) => {
    while (el.parentElement && ['inline', 'contents'].includes(getComputedStyle(el).display)) el = el.parentElement;
    return el;
  };
  // A space as the element draws one, in the face the page loaded: no rule
  // of the page sizes the mark, and out of flow no grid or flex stretches it.
  const spaces = new Map();
  const space = (el) => {
    const style = getComputedStyle(el);
    const face = [style.fontStyle, style.fontWeight, style.fontSize, style.fontFamily, style.wordSpacing, style.letterSpacing].join(' ');
    if (!spaces.has(face)) {
      const mark = document.createElement('span');
      mark.style.cssText = 'all:unset;position:absolute;white-space:pre';
      mark.textContent = ' ';
      el.append(mark);
      spaces.set(face, mark.getBoundingClientRect().width);
      mark.remove();
    }
    return spaces.get(face);
  };
  // Whether a space is drawn between two runs, the second after the first:
  // the end of the one, the start of the other, or a node between them.
  const spaced = (node, from, to) => {
    if (from >= to) return false;
    range.setStart(node, from);
    range.setEnd(node, to);
    return [...range.getClientRects()].some((r) => r.width > 0.5);
  };
  const spacedBetween = (a, b) => {
    if (spaced(a.node, a.at, a.node.length) || spaced(b.node, 0, b.at)) return true;
    if (!(a.node.compareDocumentPosition(b.node) & Node.DOCUMENT_POSITION_FOLLOWING)) return false;
    const between = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    between.currentNode = a.node;
    for (let node = between.nextNode(); node && node !== b.node; node = between.nextNode()) {
      if (node.data.trim()) return false;
      if (spaced(node, 0, node.length)) return true;
    }
    return false;
  };
  const ends = [];
  const starts = [];
  const walker = document.createTreeWalker(document.querySelector('main'), NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const first = node.data.search(/\S/);
    if (first < 0) continue;
    const last = node.data.trimEnd().length;
    range.setStart(node, first);
    range.setEnd(node, last);
    const lines = [...range.getClientRects()].map((line) => painted(node.parentElement, line)).filter((r) => r && r.width > 1 && r.height > 2);
    if (!lines.length) continue;
    const el = node.parentElement;
    const run = {node, el, block: blockOf(el), layer: el.closest(floating), token: el.closest(identifier)};
    ends.push({...run, rect: lines.at(-1), ch: node.data[last - 1], at: last});
    starts.push({...run, rect: lines[0], ch: node.data[first], at: first});
  }
  starts.sort((a, b) => a.rect.top - b.rect.top);
  // A run on a's line starts at most the tallest run's height above it.
  const tallest = starts.reduce((most, b) => Math.max(most, b.rect.height), 0);
  const OPENS = /[\p{Ps}\p{Pi}\p{Pd}\p{Pc}\/]/u;
  const CLOSES = /[\p{Pe}\p{Pf}\p{Po}\p{Pd}\p{Pc}\/]/u;
  const found = [];
  for (const a of ends) {
    if (OPENS.test(a.ch)) continue;
    let low = 0, high = starts.length;
    while (low < high) {
      const mid = (low + high) >> 1;
      if (starts[mid].rect.top < a.rect.top - tallest) low = mid + 1; else high = mid;
    }
    for (let j = low; j < starts.length && starts[j].rect.top < a.rect.bottom; j++) {
      const b = starts[j];
      if (b.node === a.node || b.layer !== a.layer || CLOSES.test(b.ch)) continue;
      if (a.token && a.token === b.token) continue;
      const shared = Math.min(a.rect.bottom, b.rect.bottom) - Math.max(a.rect.top, b.rect.top);
      if (shared <= Math.min(a.rect.height, b.rect.height) / 2) continue;
      const gap = b.rect.left - a.rect.right;
      if (gap <= -2 || gap >= Math.min(space(a.el), space(b.el)) - 0.5) continue;
      if (a.block === b.block && gap <= 0.5) continue;
      if (spacedBetween(a, b)) continue;
      found.push(`"${a.node.data.trim().slice(-30)}" (${describe(a.el)}) touches "${b.node.data.trim().slice(0, 30)}" (${describe(b.el)}), ${gap.toFixed(1)}px apart`);
    }
  }
  return [...new Set(found)];
}"""
)

# A chip (an identifier, a tag, or any box set in a line, or in a row of
# them, that paints a background or an edge of its own) draws all its text on
# what it paints, its own box and the boxes inside it that paint (a cell's
# identifier, a grid around its token, paints only through them, tables.css),
# and a word of it laid out past them reads as a stray line beneath the chip. What a
# box cuts away is not drawn, so a chip cut short draws nothing past itself.
CHIPS_PAST_THEIR_PAINT = (
    "([chips, unseen]) => {"
    + DESCRIBE
    + PAINTED_PART
    + TEXT_SHOWN
    + r"""
  const range = document.createRange();
  const shown = (color) => !/^(transparent|rgba\(.*,\s*0\)|color\(.*\/\s*0\))$/.test(color);
  const paints = (el) => {
    const style = getComputedStyle(el);
    return shown(style.backgroundColor) || style.backgroundImage !== 'none'
      || ['Top', 'Right', 'Bottom', 'Left'].some((side) => parseFloat(style[`border${side}Width`]) > 0
        && style[`border${side}Style`] !== 'none' && shown(style[`border${side}Color`]));
  };
  const inLine = (el) => getComputedStyle(el).display.startsWith('inline')
    || /^inline-(flex|grid)$/.test(getComputedStyle(el.parentElement).display);
  const rects = (el) => [...el.getClientRects()].filter((r) => r.width && r.height);
  const found = [];
  // A chip inside one already found is the same chip's text.
  const off = [];
  for (const chip of document.querySelectorAll('main *')) {
    if (off.some((outer) => outer.contains(chip))) continue;
    if (!(chip.matches(chips) || (inLine(chip) && paints(chip))) || chip.closest(unseen) || !chip.checkVisibility()) continue;
    // What it paints: its own box and the boxes inside it that paint.
    const boxes = [chip, ...chip.querySelectorAll('*')].filter(paints).flatMap(rects);
    if (!boxes.length) continue;
    const within = (r) => boxes.some((b) => r.left >= b.left - 1 && r.right <= b.right + 1 && r.top >= b.top - 1 && r.bottom <= b.bottom + 1);
    const walker = document.createTreeWalker(chip, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const first = node.data.search(/\S/);
      if (first < 0 || node.parentElement.closest(unseen) || !textShown(node)) continue;
      range.setStart(node, first);
      range.setEnd(node, node.data.trimEnd().length);
      const drawn = [...range.getClientRects()].map((r) => painted(node.parentElement, r)).filter((r) => r && r.width > 1);
      if (drawn.every(within)) continue;
      found.push(`${describe(chip)} "${chip.textContent.trim().slice(0, 40)}" draws "${node.data.trim().slice(0, 30)}" off its chip`);
      if (off.at(-1) !== chip) off.push(chip);
    }
  }
  return [...new Set(found)];
}"""
)


# A box the stylesheet clamps to some lines shows those lines and no more: a
# line past its clamp that is still drawn (a box of its own on it, which a
# browser may leave out of the clamp) reads as a row that grew.
PAST_THE_CLAMP = (
    "() => {"
    + DESCRIBE
    + LINES
    + r"""
  const found = [];
  for (const el of document.querySelectorAll('main *')) {
    const clamp = parseInt(getComputedStyle(el).webkitLineClamp, 10);
    if (!clamp || !el.checkVisibility()) continue;
    const box = el.getBoundingClientRect();
    const shown = chars(el).filter((c) => c.top !== null && c.ch.trim() && c.top >= box.top - 1 && c.bottom <= box.bottom + 1)
      .sort((a, b) => a.top - b.top);
    let lines = 0, bottom = -Infinity;
    for (const c of shown) {
      if (c.top >= bottom - 1) { lines += 1; bottom = c.bottom; } else bottom = Math.max(bottom, c.bottom);
    }
    if (lines > clamp) found.push(`${describe(el)} "${el.textContent.trim().slice(0, 40)}" shows ${lines} lines, clamped to ${clamp}`);
    // A box drawn taller than its clamp (stretched to its row) shows a sliver
    // of the next line, cut through its glyphs by its own edge.
    const style = getComputedStyle(el);
    const foot = box.bottom - parseFloat(style.borderBottomWidth);
    const sliver = chars(el).find((c) => c.top !== null && c.ch.trim() && c.top < foot - 1 && c.bottom > foot + 1);
    if (sliver) found.push(`${describe(el)} "${el.textContent.trim().slice(0, 40)}" cuts a line through its glyphs at its foot`);
    // A cut the clamp marks takes no second mark: a part cut at its own end
    // (`text-overflow`) on the clamp's last line draws a second ellipsis
    // beside the clamp's, and one whose value runs on past it (the rest of
    // its token, drawn after the mark) cuts that value twice. The clamp marks
    // a cut only once it holds all its lines and more follow.
    if (lines < clamp || el.scrollHeight <= el.clientHeight + 1) continue;
    const line = parseFloat(style.lineHeight) || parseFloat(style.fontSize) * 1.5;
    const rest = document.createRange();
    const runsOn = (part) => {
      const token = part.parentElement?.closest('.token');
      if (!token || !el.contains(token)) return false;
      rest.setStartAfter(part);
      rest.setEnd(token, token.childNodes.length);
      return Boolean(rest.toString().trim()) && [...rest.getClientRects()].some((r) => r.width > 0 && r.top < foot);
    };
    const twice = [...el.querySelectorAll('*')].find((part) => {
      const s = getComputedStyle(part);
      if (s.textOverflow !== 'ellipsis' || part.scrollWidth <= part.clientWidth + 1 || !part.checkVisibility()) return false;
      const r = part.getBoundingClientRect();
      return r.top < foot && (r.bottom > foot - line || runsOn(part));
    });
    if (twice) found.push(`${describe(el)} "${el.textContent.trim().slice(0, 40)}" marks one cut twice: ${describe(twice)} and the clamp`);
  }
  return found;
}"""
)


# Text in a box that is never cut, by any box up to and including that one
# (`CUTS`), marked or not: a name in a sentence. `runs` narrows which text in
# the box counts.
CUT_DECISIONS = (
    "([boxes, runs, unseen]) => {"
    + DESCRIBE
    + CUTS
    + r"""
  const found = [];
  for (const el of document.querySelectorAll(`main ${boxes}`)) {
    if (!el.checkVisibility()) continue;
    for (const {node, boxes: by} of cutsIn(el, unseen)) {
      if (node.parentElement.closest(runs)) found.push(`${describe(by.at(-1))} "${by.at(-1).textContent.trim().slice(0, 40)}" is cut short`);
    }
  }
  return [...new Set(found)];
}"""
)


# An identifier (`|breakable`'s token) drawn on more than one line in a table
# that still has a column to let go: the table should have let it go
# (tables.css) rather than break the name. A table in a narrow box has none
# left, a line past a clamp is not drawn, and what wraps by design (`wraps`,
# figures.css: a row's own name, free text, a context line) and an opened row
# are not counted. A name is held to its line where its table says so: by
# `nowrap`, or in a short value's box (`cell-short`), as wide as the value
# up to its share (`max-inline-size`, tables.css), which breaks at its seams
# only once it is that wide.
BROKEN_TOKENS = (
    "([tokens, wraps]) => {"
    + DESCRIBE
    + PAINTED_PART
    + r"""
  const found = [];
  const range = document.createRange();
  for (const token of document.querySelectorAll(tokens)) {
    const table = token.closest('table');
    const narrow = narrowBox(table);
    if (narrow || token.closest(wraps) || token.closest('tr:has(> * > [aria-expanded="true"])')) continue;
    const style = getComputedStyle(token);
    const box = token.closest('.cell-short');
    const held = box && box.getBoundingClientRect().width < parseFloat(getComputedStyle(box).maxInlineSize) - 1;
    if (!held && !style.whiteSpace.startsWith('nowrap') && style.textWrapMode !== 'nowrap') continue;
    const lines = [];
    const walker = document.createTreeWalker(token, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      for (let at = 0; at < node.length; at++) {
        if (!node.data[at].trim()) continue;
        range.setStart(node, at);
        range.setEnd(node, at + 1);
        const rect = [...range.getClientRects()].find((r) => r.width);
        if (rect && painted(node.parentElement, rect) && !lines.some((top) => Math.abs(top - rect.top) < rect.height / 2)) lines.push(rect.top);
      }
    }
    if (lines.length > 1) found.push(`${describe(token.closest('td'))} breaks "${token.textContent.trim().slice(0, 40)}"`);
  }
  return [...new Set(found)];
}"""
)


# What is an identifier rather than a word, which the broken-word rule leaves
# to break where it must.
IDENTIFIER = "code, kbd, samp, time, .token, [data-entity]"


# A row's words fill each line the row gives them: a line of a row's title or
# of what it says that ends with room for the next word was broken short. The
# room is the words' own box, to its edge: a table's column is every row's,
# so the room a later column leaves beside one row's words is not theirs
# (WebKit's `pretty`, a balanced rag, breaks inside the box). A run with no space in it (a name, a path) is one word,
# however its pieces are drawn, as is a run joined by a no-break space (a
# count and its noun) or held whole by its box (`nowrap`), and its width
# holds the edges of the inline
# boxes it opens or closes (a `code`'s padding and border, and what a box
# draws after its words, a link's mark), which take the line's room as its
# letters do; an inline block moves whole, one word, and an item of a
# wrapping flex line (a dot list's part) stands its line's gap from the one
# before. A line
# the markup ends (`<br>`) is no break.
LINES_SHORT_OF_THEIR_ROOM = (
    "(unseen) => {"
    + DESCRIBE
    + """
  const found = [];
  const range = document.createRange();
  for (const block of document.querySelectorAll('.data-table > tbody > tr > * :is(p, dd, .row-line > *)')) {
    if (!block.checkVisibility() || block.closest(unseen)) continue;
    const style = getComputedStyle(block);
    // Words the browser skips drawing (`hidden="until-found"`, a row not
    // open) keep the boxes they last had, which are no line of the page.
    if (style.contentVisibility === 'hidden') continue;
    if (style.whiteSpace.startsWith('nowrap') || style.textWrapMode === 'nowrap') continue;
    // A line ends at the content edge of the block it is laid in (a notice's padded body inside a
    // value), never the value's own.
    const edgeOf = (home) => {
      const css = getComputedStyle(home);
      return home.getBoundingClientRect().right - parseFloat(css.borderRightWidth) - parseFloat(css.paddingRight);
    };
    // The edges of the inline boxes a piece of a word opens and closes (a
    // `code`'s padding and border): those whose words start or end with it.
    const bare = document.createRange();
    // A box that clones its edges onto each line it is drawn on (a chip that
    // wraps, `box-decoration-break: clone`) closes the line a word ends with
    // its padding and border, wherever in the box the word stands.
    const edges = (node, start, end) => {
      let before = 0;
      let after = 0;
      let closes = 0;
      let outer = null;
      for (let el = node.parentElement; el && el !== block; el = el.parentElement) {
        const css = getComputedStyle(el);
        if (css.display === 'contents') continue;
        if (css.display !== 'inline') break;
        const right = parseFloat(css.borderRightWidth) + parseFloat(css.paddingRight);
        bare.setStart(el, 0);
        bare.setEnd(node, start);
        if (!bare.toString().trim()) before += parseFloat(css.marginLeft) + parseFloat(css.borderLeftWidth) + parseFloat(css.paddingLeft);
        bare.setStart(node, end);
        bare.setEnd(el, el.childNodes.length);
        if (!bare.toString().trim()) {
          after += parseFloat(css.marginRight) + right;
          outer = el;
        } else if ((css.boxDecorationBreak || css.webkitBoxDecorationBreak) === 'clone') closes += right;
      }
      return [before, after, closes, outer];
    };
    // A word that closes a box ends where the box is drawn to, past anything
    // the box draws after its words (`::after`), and its margin.
    const reach = (outer, rect, after) => {
      if (!outer) return after;
      const drawn = [...outer.getClientRects()].at(-1);
      return drawn ? Math.max(after, drawn.right + parseFloat(getComputedStyle(outer).marginRight) - rect.right) : after;
    };
    // An inline block (a chip an open row sets whole), or an item of a
    // wrapping flex line (a dot list's part), moves to the next line as one
    // box, so it is one word, as wide as its box. Its line is the nearest
    // box around it, past any drawn as its contents.
    const boxAround = (el) => {
      let up = el.parentElement;
      while (up && up !== block && getComputedStyle(up).display === 'contents') up = up.parentElement;
      return up;
    };
    const atomic = (el) => {
      const css = getComputedStyle(el);
      if (/^inline-(block|flex|grid|table)$/.test(css.display)) return true;
      const line = el !== block && css.display !== 'contents' ? boxAround(el) : null;
      return !!line && /flex$/.test(getComputedStyle(line).display) && getComputedStyle(line).flexWrap === 'wrap';
    };
    // What stands between an item of a wrapping flex line and the one before
    // it is the line's gap, never a word's space.
    const gap = (el) => {
      const line = boxAround(el);
      if (!line || !/flex$/.test(getComputedStyle(line).display)) return null;
      return parseFloat(getComputedStyle(line).columnGap) || 0;
    };
    // The block a word's line is laid in: a word in another block (a value
    // that opens with a host on a line of its own) starts a line the markup
    // ended, as a `<br>` ends one.
    const home = (node) => {
      let el = node.parentElement;
      while (el !== block && /^(inline|contents)$/.test(getComputedStyle(el).display)) el = el.parentElement;
      return el;
    };
    // The box that holds a word's line whole (`white-space: nowrap`, a count
    // and its noun), inside the block: its words move as one.
    const held = (node) => {
      for (let el = node.parentElement; el && el !== block; el = el.parentElement) {
        const css = getComputedStyle(el);
        if (css.whiteSpace.startsWith('nowrap') || css.textWrapMode === 'nowrap') return el;
      }
      return null;
    };
    // Each word's pieces, a word running on across the pieces it is drawn in.
    const words = [];
    let open = false;
    let inside = null;
    const walker = document.createTreeWalker(block, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      if (inside?.contains(node)) continue;
      // A line the markup ends (`<br>`) was broken where it was written.
      if (node.nodeType === Node.ELEMENT_NODE) {
        if (node.tagName === 'BR' && words.length) words.at(-1).ended = true;
        if (node.tagName === 'BR') open = false;
        const rect = node.getBoundingClientRect();
        if (atomic(node) && node.checkVisibility() && rect.width > 0) {
          const css = getComputedStyle(node);
          words.push(Object.assign([{ rect, before: parseFloat(css.marginLeft), after: parseFloat(css.marginRight) }], { home: home(node), gap: gap(node) }));
          inside = node;
          open = false;
        }
        continue;
      }
      // A box drawn as its contents (`display: contents`) has no box to be
      // seen by, so its words are seen as the box around it is.
      let drawn = node.parentElement;
      while (drawn !== block && getComputedStyle(drawn).display === 'contents') drawn = drawn.parentElement;
      if (!drawn.checkVisibility()) continue;
      const text = node.textContent;
      for (const match of text.matchAll(/[^ \\t\\n\\r\\f]+/g)) {
        const end = match.index + match[0].length;
        range.setStart(node, match.index);
        range.setEnd(node, end);
        const rects = [...range.getClientRects()].filter((rect) => rect.width > 0);
        if (!rects.length) continue;
        const [before, after, closes, outer] = edges(node, match.index, end);
        const pieces = rects.map((rect, index) => ({
          rect,
          before: index ? 0 : before,
          after: index === rects.length - 1 ? reach(outer, rect, after) : 0,
          closes: index === rects.length - 1 ? closes : 0,
        }));
        const whole = held(node);
        if ((open && match.index === 0 && words.length) || (whole && words.at(-1)?.held === whole)) words.at(-1).push(...pieces);
        else words.push(Object.assign(pieces, { home: home(node), held: whole }));
        open = end === text.length;
      }
      if (/[ \\t\\n\\r\\f]$/.test(text) || text === '') open = false;
    }
    const space = parseFloat(style.fontSize) * 0.35;
    for (let at = 0; at + 1 < words.length; at += 1) {
      if (words[at].ended || words[at].home !== words[at + 1].home) continue;
      const lastPiece = words[at].at(-1);
      const last = lastPiece.rect;
      const next = words[at + 1];
      if (next[0].rect.top < last.bottom - 1) continue;
      const width = next.reduce((sum, piece) => sum + piece.before + piece.rect.width + piece.after, 0) + (next.at(-1).closes ?? 0);
      const right = last.right + lastPiece.after;
      const edge = edgeOf(words[at].home);
      if (right + (next.gap ?? space) + width < edge - 1) {
        found.push(`${describe(block)} breaks before "${block.textContent.trim().slice(0, 24)}…" ${Math.round(edge - right)}px short of its row's room`);
        break;
      }
    }
  }
  return found;
}"""
)

# A block written inside a <p> closes it early and leaves an empty <p> where
# its end tag was: the rest of the line falls out of it.
BROKEN_PARAGRAPHS = (
    "(selector) => {"
    + DESCRIBE
    + """
  return [...document.querySelectorAll(selector)].map((p) => `${describe(p.parentElement)} holds an empty paragraph`
    + (p.previousElementSibling ? ` after ${describe(p.previousElementSibling)}` : ''));
}"""
)

# A run of monospace words in a box of its own, as a value's line draws one.
_MONO = "margin: 0; padding: 0; font: 16px/1.5 monospace"

# A chip as wide as its cell holds, the cell's line short of what the chip's
# padding asks.
_FIT_THE_CHIP = """() => {
  const chip = document.querySelector('main table span');
  const words = document.createRange();
  words.selectNodeContents(chip.firstChild);
  chip.closest('table').style.cssText = `table-layout: fixed; inline-size: ${words.getBoundingClientRect().width + 4}px`;
}"""

# Lines laid over one another in the page, in an open menu's one panel, and
# under the panel.
_LINE = '<p style="position: fixed; top: {}; left: 40px; margin: 0">example {}</p>'
_LINES_OVER_ONE_ANOTHER = (
    _LINE.format("300px", "covered")
    + '<div data-menu><div popover="manual">'
    + "".join(
        _LINE.format(top, words) for top, words in (("300px", "covering"), ("400px", "first"), ("400px", "second"))
    )
    + "</div></div>"
    + _LINE.format("500px", "clashed")
    + _LINE.format("500px", "under")
)

# Words in cells of a value's lines, each line ending as its room does: after
# a link's mark (``::after``), a wrapping list's gap, a word held whole, a
# padded block's edge; and one a balanced rag ends short of its room.
_LINE_ENDS = (
    '<style>.example-marked::after { content: "____"; }</style><table class="data-table"><tbody><tr>'
    + "".join(
        f'<td><dd style="{_MONO}; inline-size: {room}">{words}</dd></td>'
        for words, room in (
            ('<a class="example-marked" href="#">aaaa</a> bb', "10ch"),
            (
                (
                    '<ul style="display: flex; flex-wrap: wrap; gap: 0 4ch; margin: 0; padding: 0; list-style: none">'
                    "<li>cccc</li><li>dddd</li></ul>"
                ),
                "10ch",
            ),
            ('eeee <span style="white-space: nowrap">ff gggg</span>', "10ch"),
            ('<div style="padding-inline-end: 8ch">hhhh iiii jjjj</div>', "20ch"),
            ("example aaaa bbbb cccc dddd", "24ch; text-wrap: balance"),
        )
    )
    + "</tr></tbody></table>"
)

# The survey checks these probes answer (`probes/__init__.py`).
CHECKS = {
    "broken paragraphs": Check(
        (BROKEN_PARAGRAPHS, SELECTORS["broken_paragraph"]), reader_sees="a line of text split off its paragraph"
    ),
    "broken words": Check(
        (BROKEN_WORDS, SELECTORS["identifier"]),
        table=True,
        reader_sees="a word split across the lines of a cell",
        plants=(
            # A run is measured against the box it lays out in: a digest that
            # fits its cell but not the padded chip in it breaks wherever the
            # column is, while a word that would fit its line, broken anyway,
            # is found.
            Plant(
                '<table><tr><td style="padding: 0"><span style="display: block; font-family: monospace; '
                'overflow-wrap: anywhere; padding: 0 10px">0123456789abcdef0123456789abcdef</span></td></tr></table>'
                '<table><tr><td><span style="display: block; font-family: monospace; word-break: break-all; '
                'inline-size: 10ch">an exampleword</span></td></tr></table>',
                setup=_FIT_THE_CHIP,
                finds=("example",),
            ),
            # An identifier wider than its line breaks at its seam and then
            # wherever it must, though the seam leaves its last part a text
            # node of its own: the run is the identifier. A plain word broken
            # in the same line is found.
            Plant(
                '<table style="table-layout: fixed; inline-size: 6ch">'
                '<tr><td style="padding: 0; overflow-wrap: anywhere"><span class="token">plant:<span class="seam">'
                "</span>somewhere</span></td></tr>"
                '<tr><td style="padding: 0; overflow-wrap: anywhere">everywhere</td></tr></table>',
                finds=('"everywhere"',),
            ),
            # In the line beneath an open row a value left less of the line
            # than its label takes breaks a word the whole line holds, which
            # is found: the row shows a value whole. A word longer than the
            # whole line breaks where it must.
            Plant(
                '<table><tr><td><button aria-expanded="true"></button></td></tr><tr data-row-detail><td>'
                '<dl style="display: grid; grid-template-columns: 7rem 4ch; inline-size: 20rem; margin: 0; '
                'overflow-wrap: anywhere">'
                + "".join(
                    f'<dt>Label</dt><dd style="margin: 0">{word}</dd>' for word in ("Unrecognized", "Example" * 12)
                )
                + "</dl></td></tr></table>",
                finds=('"Unrecognized"',),
            ),
        ),
    ),
    "words broken in text": Check(
        (BROKEN_WORDS_IN_TEXT, IDENTIFIER),
        reader_sees="a word or a number split across lines",
        plants=(
            # In a box narrower than any amount, every amount HQ writes stays
            # one unit, while a sign written loose before its currency mark
            # ends a line.
            Plant(
                "{% load value_tags %}"
                '<p class="example-written" style="{{ box }}">{{ owed|money }} {{ five|money_whole }}</p>'
                '<p class="example-loose" style="{{ box }}">\u2212$1,243.52</p>',
                values={
                    "owed": Decimal("-1243.52"),
                    "five": Decimal(-5),
                    # Only the breaks the text itself allows: none forced past them.
                    "box": "inline-size: 1ch; overflow-wrap: normal; word-break: normal",
                },
                finds=("example-loose",),
            ),
            # A digest in a sentence is wider than its line, so it breaks
            # inside the letters it holds and that is no broken word, while a
            # word of the same sentence broken in its box is found.
            Plant(
                "".join(
                    f'<p class="example-{name}" style="inline-size: 12ch; word-break: normal; '
                    f'overflow-wrap: anywhere">Names the account {words}</p>'
                    for name, words in (("digest", "abcdefghijklmnop1" * 8), ("word", "examplelongerthanitsline"))
                ),
                finds=("example-word",),
            ),
        ),
    ),
    "lines opening on a stray": Check(
        # The empty value's mark (`present`'s) is a value standing for none, never a separator.
        (STRAYS_OPENING_A_LINE, [SELECTORS["unseen"], f"{IDENTIFIER}, .empty-value"]),
        reader_sees="a line opening on a lone separator, a mark, or a word cut from its number",
        plants=(
            # A dash opening a wrapping flex row reads as a separator; the
            # empty value's mark opening one, a button's words, is a value.
            Plant(
                "".join(
                    '<div style="display: flex; flex-wrap: wrap; inline-size: 12ch">'
                    f'<span style="inline-size: 12ch">Example</span><span>{mark} example-{name}</span></div>'
                    for name, mark in (
                        ("loose", "–"),
                        ("none", '<button type="button"><span class="empty-value">–</span></button>'),
                    )
                ),
                finds=('"– example-loose"',),
            ),
            # A slash standing between two words reads as a separator when a
            # line opens on it; a path ("/api") opening a line is a value.
            Plant(
                "".join(
                    f'<dd style="{_MONO}; inline-size: 13ch; text-wrap: wrap">{words}</dd>'
                    for words in ("example cccc /dddd", "example aaaa / bbbb")
                ),
                finds=('"/ bbbb"',),
            ),
            # A mark inside a wrapping identifier is part of the value.
            Plant(
                "".join(
                    f'<span{name} style="display: block; inline-size: 1ch; {_MONO}; overflow-wrap: anywhere; '
                    f'white-space: normal">{words}</span>'
                    for words, name in (("c-d", ""), ("a-b", ' class="token"'))
                ),
                finds=('holds "-"',),
            ),
        ),
    ),
    "broken identifiers": Check(
        (BROKEN_TOKENS, [SELECTORS["cell_token"], SELECTORS["wraps_in_a_cell"]]),
        table=True,
        reader_sees="a name broken across lines while its table had a column to let go",
        # A short name its table holds on its line (`nowrap`) and something
        # inside breaks anyway is found; one its table lets break at its
        # seams, in a box too narrow to hold it, is not.
        plants=(
            Plant(
                '<table class="data-table" style="table-layout: fixed; inline-size: 12ch"><tbody><tr>'
                + "".join(
                    '<td style="padding: 0; overflow-wrap: anywhere"><div class="cell-short" style="display: inline">'
                    f'<span class="token" style="white-space: {hold}"><span style="white-space: normal">{name}</span>'
                    "</span></div></td>"
                    for name, hold in (("example-held-name", "nowrap"), ("example-free-name", "normal"))
                )
                + "</tr></tbody></table>",
                finds=('"example-held-name"',),
            ),
        ),
    ),
    "overlapping text": Check(
        (OVERLAPPING_TEXT, SELECTORS["floating"]),
        reader_sees="lines of text drawn over each other",
        # An open menu's panel covers the page's text on purpose; two lines
        # drawn over each other in the page, or in the one panel, still
        # overlap.
        plants=(
            Plant(
                _LINES_OVER_ONE_ANOTHER,
                setup="() => document.querySelector('main [popover]').showPopover()",
                finds=('"example clashed"', '"example first"'),
            ),
        ),
    ),
    "touching text": Check(
        (TOUCHING_TEXT, [SELECTORS["floating"], SELECTORS["identifier"]]),
        reader_sees="two runs of text touching, read as one word",
        # A heading and its status as items of a flex row lose the space
        # between them in the markup and read as one word; the same pair with
        # a space drawn between them, and a name ``breakable`` split into
        # boxes at its seams, are each one run of words. Two marks each
        # centered in a button of its own, a gap between the buttons, stand
        # apart.
        plants=(
            Plant(
                "{% load value_tags %}<style>.example-seams .token > * { display: inline-block; }"
                ".example-buttons :is(a, span) { display: grid; place-items: center; inline-size: 28px; "
                "block-size: 28px; font-size: 20px; }</style>"
                '<div style="display: flex; align-items: baseline"><h2 style="margin: 0">Example heading</h2> '
                "<span>Example touching</span></div>"
                '<div><h2 style="display: inline-block; margin: 0">Example spaced</h2> '
                '<span style="display: inline-block">Example status</span></div>'
                '<p class="example-seams">{{ host|breakable }}</p>'
                '<nav class="example-buttons" style="display: flex; gap: 4px"><a href="#">x</a><a href="#">y</a></nav>',
                values={"host": "status.example-host-name.example.com"},
                finds=('"Example heading"',),
            ),
        ),
    ),
    "chips off their paint": Check(
        (CHIPS_PAST_THEIR_PAINT, [SELECTORS["chip"], SELECTORS["unseen"]]),
        reader_sees="a chip's words spilling off the box it paints",
        plants=(
            Plant(
                '{% load value_tags %}<p><code style="display: inline-block; inline-size: 4ch; block-size: 1lh; '
                'background: #eee">{{ words|breakable }}</code></p>',
                values={"words": "Example overrun"},
                finds=("code",),
            ),
        ),
    ),
    "past the clamp": Check(PAST_THE_CLAMP, reader_sees="a line drawn past the last line its box shows"),
    "cut code in text": Check(
        (CUT_DECISIONS, [SELECTORS["running_text"], SELECTORS["code"], SELECTORS["unseen"]]),
        reader_sees="a name in a sentence cut off",
    ),
    "row words to the end of their line": Check(
        (LINES_SHORT_OF_THEIR_ROOM, SELECTORS["unseen"]),
        reader_sees="a row's words broken onto a new line while their line had room",
        # A link's mark drawn after its words takes its line's room, as a
        # wrapping list's gap does between its items, so the word that would
        # not fit beside either starts the next line rightly, and a line laid
        # in a padded block ends at that block's edge; a line a balanced rag
        # ends short of its room is found.
        plants=(Plant(_LINE_ENDS, finds=('breaks before "exam',)),),
    ),
}
