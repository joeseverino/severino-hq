"""Probes of running text: broken words, text that overlaps and text past its clamp."""

from hq.platform.core.tests.browser.probes import Check
from hq.platform.core.tests.browser.probes.describe import CUTS, DESCRIBE, LINES, PAINTED_PART, TEXT_SHOWN
from hq.platform.core.tests.browser.selectors import SELECTORS

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
      for (const run of node.textContent.matchAll(/\\S+/g)) {
        const [wide, length] = token
          ? [tokenWidth(token) > line, token.textContent.trim().length]
          : [widthOf(node, run.index, run.index + run[0].length) > line, run[0].length];
        for (const match of run[0].matchAll(/[A-Za-z]{4,}/g)) {
          const at = run.index + match.index;
          const rects = across(node, at, at + match[0].length);
          if (new Set(rects.map((r) => Math.round(r.top))).size < 2) continue;
          // A closed row's run wider than its line breaks wherever its column
          // is. An open row shows a value whole, breaking it at its seams,
          // spaces and hyphens: a word breaks there only when what it stands
          // in between two of them is wider than all the room the row has.
          const part = [...run[0].matchAll(/[^-]*-?/g)].find((p) => p.index <= match.index && match.index < p.index + p[0].length);
          const must = open
            ? widthOf(node, run.index + part.index, run.index + part.index + part[0].length) > room
            : wide && match[0].length < length;
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
# above "days") or, in a cell, that a box set whole after it left behind
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
  // A middle dot, a bullet, a bar, a slash, an en or an em dash.
  const SEPARATOR = /^[·•|\/\u2013\u2014]/;
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
        if (child.matches(`${unseen}, script, style, template, pre`) || !child.checkVisibility({visibilityProperty: true})) continue;
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
      else if (MARKS_ONLY.test(text)) found.push(`${name} holds "${text.slice(0, 30)}" on a line of its own (▪ is an icon or an image)`);
      else if (index && !line.forced && /^\p{L}+[.,;:]?$/u.test(text) && /\p{N}%?$/u.test(lines[index - 1].text.trim())) {
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
      const mark = drawnSeparator(opening) || (first && SEPARATOR.test(first.ch) && !first.el.closest(identifier) ? first.ch : null);
      if (mark) found.push(`${describe(box)} opens a row with "${mark[0]}": "${opening.textContent.trim().slice(0, 30)}"`);
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
  }
  return found;
}"""
)


# Text in a box that is never cut, by any box up to and including that one
# (`CUTS`), marked or not: what a card's own buttons decide (approving on half
# of what a change does is not deciding it), or a name in a sentence. `runs`
# narrows which text in the box counts.
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


# How a probe reads where text sits on its line: the text a reader is shown
# under a root (never what matches the probe's `unseen`), the line a run of it
# is drawn on, the box its line is laid out in, and its baseline, read where a
# box of no size lands, put beside the text or the box. `before` is the
# running words just before an element in its own block, when there are any:
# a chip or a pill there is a box with a baseline of its own.
BASELINES = r"""
const range = document.createRange();
const rects = (node) => { range.selectNodeContents(node); return [...range.getClientRects()].filter((r) => r.width); };
// Text in content the browser skips (a closed fold, `hidden="until-found"`)
// is not drawn, though WebKit gives it rects once any read lays it out; it is
// judged by the box that holds it, since a box of `display: contents` has
// none to judge.
const skipped = (el) => {
  while (el && getComputedStyle(el).display === 'contents') el = el.parentElement;
  return !!el && !el.checkVisibility();
};
const drawn = (root) => {
  const found = [];
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    if (node.data.trim() && !node.parentElement.closest(unseen) && !skipped(node.parentElement) && rects(node).length) found.push(node);
  }
  return found;
};
const baseline = (node, after) => {
  const mark = document.createElement('span');
  mark.style.cssText = 'display:inline-block;inline-size:0;block-size:0;margin:0;padding:0;border:0';
  if (after) node.after(mark); else node.before(mark);
  const at = mark.getBoundingClientRect().top;
  mark.remove();
  return at;
};
const sameLine = (a, b) => Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > Math.min(a.height, b.height) / 2;
const blockOf = (el) => {
  while (['inline', 'contents'].includes(getComputedStyle(el).display)) el = el.parentElement;
  return el;
};
const before = (el) => {
  const block = blockOf(el.parentElement);
  return drawn(block)
    .filter((node) => el.compareDocumentPosition(node) & Node.DOCUMENT_POSITION_PRECEDING)
    .filter((node) => blockOf(node.parentElement) === block)
    .at(-1);
};
"""


# A value and the one it leads to (`partials/_change.html`): what shares a
# line shares a baseline, the first value, the arrow, the second and the words
# before them, and the arrow is on the line of the value it points at.
CHANGES_OFF_THEIR_LINE = (
    "([selector, arrowSelector, toSelector, unseen]) => {"
    + DESCRIBE
    + BASELINES
    + r"""
  const found = [];
  for (const change of document.querySelectorAll(selector)) {
    if (!change.checkVisibility()) continue;
    const arrow = change.querySelector(arrowSelector);
    const to = change.querySelector(toSelector);
    const from = drawn(change).filter((node) => !to.contains(node));
    const into = drawn(to).filter((node) => !arrow.contains(node));
    const name = `${describe(change.parentElement)} "${change.textContent.trim().slice(0, 40)}"`;
    if (!from.length || !into.length || !rects(arrow.firstChild).length) {
      found.push(`${name} shows no old value, arrow or new value`);
      continue;
    }
    const arrowLine = rects(arrow.firstChild)[0];
    if (!sameLine(arrowLine, rects(into[0])[0])) found.push(`${name} leaves its arrow on a line apart from the new value`);
    const parts = [['the arrow', arrowLine, baseline(arrow.firstChild, true)], ['the new value', rects(into[0])[0], baseline(into[0], false)]];
    const last = from.at(-1);
    parts.push(['the old value', rects(last).at(-1), baseline(last, true)]);
    const words = before(change);
    if (words) parts.push(['the words before it', rects(words).at(-1), baseline(words, true)]);
    const firstLine = rects(from[0])[0];
    const fromFirst = ['the old value', firstLine, baseline(from[0], false)];
    for (const [what, line, at] of parts.slice(1)) {
      const against = what === 'the words before it' ? fromFirst : parts[0];
      if (sameLine(line, against[1]) && Math.abs(at - against[2]) > 0.5) {
        found.push(`${name}: ${what} sits ${(at - against[2]).toFixed(1)}px off the baseline of ${against[0]}`);
      }
    }
  }
  return found;
}"""
)


# A chip (an identifier, a tag) set in a line sits on that line's baseline: its
# first line on the baseline of the line it starts on, its last on the one it
# ends on. The line's baseline is read where a box of no size lands beside the
# chip, so a line of pills and links and no plain words is held too. A chip
# that is an item of a flex or grid box is that box's to place.
CHIPS_OFF_THEIR_LINE = (
    "([selector, unseen]) => {"
    + DESCRIBE
    + BASELINES
    + r"""
  const found = [];
  for (const chip of document.querySelectorAll(selector)) {
    if (!chip.checkVisibility() || !getComputedStyle(chip).display.startsWith('inline')) continue;
    const text = drawn(chip);
    if (!text.length) continue;
    const block = blockOf(chip.parentElement);
    const others = drawn(block).filter((node) => !chip.contains(node)).flatMap(rects);
    const name = `${describe(chip.parentElement)} "${chip.textContent.trim().slice(0, 40)}"`;
    const ends = [[rects(text[0])[0], baseline(text[0], false), baseline(chip, false), 'starts'], [rects(text.at(-1)).at(-1), baseline(text.at(-1), true), baseline(chip, true), 'ends']];
    for (const [line, at, its, where] of ends) {
      if (its < line.top - 0.5 || its > line.bottom + 0.5 || !others.some((other) => sameLine(other, line))) continue;
      if (Math.abs(at - its) > 0.5) found.push(`${name} sits ${(at - its).toFixed(1)}px off the baseline of the line it ${where} on`);
    }
  }
  return [...new Set(found)];
}"""
)


# The facts of a band that share a row share a baseline: each value's first
# line has its baseline where the first value of its row has it, a zero beside a linked
# count alike. A row is the facts whose cells start level.
VALUES_OFF_THEIR_ROW = (
    "([band, fact, value, unseen]) => {"
    + DESCRIBE
    + BASELINES
    + r"""
  const found = [];
  for (const each of document.querySelectorAll(band)) {
    if (!each.checkVisibility()) continue;
    const rows = new Map();
    for (const cell of each.querySelectorAll(`:scope > ${fact}`)) {
      const held = cell.querySelector(value);
      if (!held || !drawn(held).length) continue;
      const top = Math.round(cell.getBoundingClientRect().top);
      if (!rows.has(top)) rows.set(top, []);
      // The baseline of the value's own first line, where a box of no size
      // lands first in it: a chip in that line is the chips check's.
      rows.get(top).push([held, baseline(held.firstChild, false)]);
    }
    for (const row of rows.values()) {
      const [[first, at]] = row;
      for (const [held, its] of row.slice(1)) {
        if (Math.abs(its - at) > 0.5) {
          found.push(`${describe(each)} "${held.textContent.trim().slice(0, 40)}" sits ${(its - at).toFixed(1)}px off the baseline of "${first.textContent.trim().slice(0, 40)}"`);
        }
      }
    }
  }
  return found;
}"""
)


# What a fact draws: its words and its icon's mark, as the rects a reader sees.
_FACT_DRAWN = r"""
const factRects = (cell) => [
  ...drawn(cell).flatMap(rects),
  ...[...cell.querySelectorAll('svg')].filter((mark) => mark.checkVisibility()).map((mark) => mark.getBoundingClientRect()),
].filter((r) => r.width && r.height);
const extent = (found) => found.length && {
  top: Math.min(...found.map((r) => r.top)), bottom: Math.max(...found.map((r) => r.bottom)),
  left: Math.min(...found.map((r) => r.left)), right: Math.max(...found.map((r) => r.right)),
};
"""


# The facts of a band share its width: its tracks take the whole band, so a
# few facts spread across a wide card rather than collecting at its start
# beside an empty end. Read from each fact's cell, which is its track, at the
# band's width and with the band set wider for the length of one layout and
# put back before it paints: a track sized to what its fact says (a
# `max-content` bound, a cap on the fact) stops short of the band's end once
# the band is wider than its facts, whatever width the survey reads it at.
FACTS_SHORT_OF_THEIR_BAND = (
    "([band, fact]) => {"
    + DESCRIBE
    + r"""
  const WIDER = 240;
  const short = (each) => {
    const style = getComputedStyle(each);
    const rtl = style.direction === 'rtl';
    const r = each.getBoundingClientRect();
    const end = rtl
      ? r.left + parseFloat(style.borderLeftWidth) + parseFloat(style.paddingLeft)
      : r.right - parseFloat(style.borderRightWidth) - parseFloat(style.paddingRight);
    const cells = [...each.querySelectorAll(`:scope > ${fact}`)].filter((cell) => cell.checkVisibility());
    if (!cells.length) return null;
    const reach = rtl
      ? Math.min(...cells.map((cell) => cell.getBoundingClientRect().left))
      : Math.max(...cells.map((cell) => cell.getBoundingClientRect().right));
    const left = rtl ? reach - end : end - reach;
    return left > 1 ? left : null;
  };
  const found = [];
  for (const each of document.querySelectorAll(band)) {
    if (!each.checkVisibility()) continue;
    const now = short(each);
    const style = each.getAttribute('style');
    each.style.setProperty('inline-size', `${each.getBoundingClientRect().width + WIDER}px`, 'important');
    each.style.setProperty('max-inline-size', 'none', 'important');
    const wider = short(each);
    if (style === null) each.removeAttribute('style');
    else each.setAttribute('style', style);
    if (now !== null) found.push(`${describe(each)} ends its facts ${Math.round(now)}px short of its end`);
    else if (wider !== null) found.push(`${describe(each)} ends its facts ${Math.round(wider)}px short of its end once ${WIDER}px wider`);
  }
  return [...new Set(found)];
}"""
)


# A fact's detail is running words: its lines are the ones its markup
# declares (a `<small>` each), so no word of it is laid out as a row of its
# own by a flex or grid box, which makes each run of text between two
# elements an item, a dot or a link on a line alone.
DETAIL_WORDS_IN_ROWS = (
    "([detail, unseen]) => {"
    + DESCRIBE
    + BASELINES
    + r"""
  const found = [];
  for (const each of document.querySelectorAll(detail)) {
    if (!each.checkVisibility()) continue;
    for (const box of [each, ...each.querySelectorAll('*')]) {
      if (!/flex|grid/.test(getComputedStyle(box).display)) continue;
      const loose = [...box.childNodes].find((node) => node.nodeType === Node.TEXT_NODE && node.data.trim()
        && !box.closest(unseen) && rects(node).length);
      if (loose) found.push(`${describe(box)} lays out "${loose.data.trim().slice(0, 30)}" as a row of its own`);
    }
  }
  return [...new Set(found)];
}"""
)


# A fact keeps its frame's inset: what it draws stands at least `least`
# pixels inside the sides of the frame that holds it (the nearest box that
# draws an edge all round) and below any rule drawn above it there, a
# section head's or its own line's among them.
FACTS_AGAINST_THEIR_FRAME = (
    "([fact, unseen, least]) => {"
    + DESCRIBE
    + BASELINES
    + _FACT_DRAWN
    + r"""
  const seen = (color) => color && !/^(transparent|rgba\(.*,\s*0\))$/.test(color);
  const edge = (style, side) => parseFloat(style[`border${side}Width`]) > 0 && style[`border${side}Style`] !== 'none'
    && seen(style[`border${side}Color`]);
  const framed = (style) => ['Top', 'Right', 'Bottom', 'Left'].every((side) => edge(style, side));
  const found = [];
  for (const cell of document.querySelectorAll(`main ${fact}`)) {
    if (!cell.checkVisibility()) continue;
    const at = extent(factRects(cell));
    if (!at) continue;
    let frame = cell.parentElement;
    while (frame && frame !== document.body && !framed(getComputedStyle(frame))) frame = frame.parentElement;
    if (!frame || frame === document.body) continue;
    const style = getComputedStyle(frame);
    const f = frame.getBoundingClientRect();
    const sides = {
      left: at.left - (f.left + parseFloat(style.borderLeftWidth)),
      right: f.right - parseFloat(style.borderRightWidth) - at.right,
      top: at.top - (f.top + parseFloat(style.borderTopWidth)),
    };
    // A rule above the fact inside its frame: a bottom edge some box draws
    // across the fact's columns, ending above what the fact draws.
    for (const ruled of frame.querySelectorAll('*')) {
      if (ruled.contains(cell) || !edge(getComputedStyle(ruled), 'Bottom')) continue;
      const r = ruled.getBoundingClientRect();
      if (r.bottom > at.top + 0.5 || r.right <= at.left || r.left >= at.right) continue;
      sides.top = Math.min(sides.top, at.top - r.bottom);
    }
    for (let up = cell.parentElement; up !== frame; up = up.parentElement) {
      const its = getComputedStyle(up);
      if (edge(its, 'Top')) sides.top = Math.min(sides.top, at.top - up.getBoundingClientRect().top - parseFloat(its.borderTopWidth));
    }
    const near = Object.entries(sides).filter(([, gap]) => gap < least - 0.5);
    if (near.length) {
      found.push(`${describe(cell)} "${cell.textContent.trim().slice(0, 30)}" stands ${near.map(([side, gap]) => `${Math.round(gap)}px from its frame's ${side}`).join(', ')}`);
    }
  }
  return [...new Set(found)];
}"""
)


# A block box set in a line that clips what it holds takes its bottom margin
# edge as its baseline (CSS 2.1 §10.8.1), so on the line's baseline its text
# rides a descent above the words beside it. That is an inline block (and
# Chromium's inline -webkit-box) whose overflow is anything but visible, `clip`
# included since WebKit counts it; an inline flex or grid box takes its items'
# baseline whatever its overflow, and `baseline-source`, which Chromium honors
# there, WebKit does not. It is read from the style each box that draws text
# ends up with (a select's options are not drawn in it), since its display and
# its overflow come from different rules: `vertical-align: top` or `bottom`
# sets it on the line by its edges instead.
RAISED_TEXT = (
    "(unseen) => {"
    + DESCRIBE
    + BASELINES
    + r"""
  const found = [];
  const edges = ['top', 'bottom', 'middle', 'text-top', 'text-bottom'];
  for (const el of document.querySelectorAll('body *')) {
    const style = getComputedStyle(el);
    if (!['inline-block', '-webkit-inline-box'].includes(style.display) || edges.includes(style.verticalAlign)) continue;
    if (style.overflowX === 'visible' && style.overflowY === 'visible') continue;
    if (!el.checkVisibility() || !drawn(el).length) continue;
    found.push(`${describe(el)} "${el.textContent.trim().slice(0, 40)}" rides above its line (${style.display}, overflow ${style.overflowX} ${style.overflowY}, vertical-align ${style.verticalAlign})`);
  }
  return [...new Set(found)];
}"""
)


# An identifier (`|breakable`'s token) drawn on more than one line in a table
# that still has a column to let go: the table should have let it go
# (tables.js) rather than break the name. A table in a narrow box has none
# left, a line past a clamp is not drawn, and what wraps by design (`wraps`,
# figures.css: a row's own name, free text, a context line) and an opened row
# are not counted.
BROKEN_TOKENS = (
    "([tokens, wraps]) => {"
    + DESCRIBE
    + PAINTED_PART
    + r"""
  const found = [];
  const range = document.createRange();
  for (const token of document.querySelectorAll(tokens)) {
    if (token.closest(wraps) || token.closest('[data-narrow], tr:has(> * > [aria-expanded="true"])')) continue;
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

CHANGES_READ = (
    CHANGES_OFF_THEIR_LINE,
    [SELECTORS["change"], SELECTORS["change_arrow"], SELECTORS["change_to"], SELECTORS["unseen"]],
)

CHIPS_READ = (CHIPS_OFF_THEIR_LINE, [SELECTORS["chip"], SELECTORS["unseen"]])

FACT_ROWS_READ = (
    VALUES_OFF_THEIR_ROW,
    [SELECTORS["fact_band"], SELECTORS["band_fact_cell"], SELECTORS["fact_value"], SELECTORS["unseen"]],
)

# A fact's detail: every part of a fact after its value.
_FACT_DETAIL = f"{SELECTORS['band_fact_cell']} > dd:not({SELECTORS['fact_value']})"

# How far inside its frame a fact's words and mark stand at the least.
FACT_INSET = 8

# The survey checks these probes answer (`probes/__init__.py`).
CHECKS = {
    "broken words": Check((BROKEN_WORDS, SELECTORS["identifier"]), table=True),
    "words broken in text": (BROKEN_WORDS_IN_TEXT, IDENTIFIER),
    "lines opening on a stray": (STRAYS_OPENING_A_LINE, [SELECTORS["unseen"], IDENTIFIER]),
    "broken identifiers": Check((BROKEN_TOKENS, [SELECTORS["cell_token"], SELECTORS["wraps_in_a_cell"]]), table=True),
    "overlapping text": (OVERLAPPING_TEXT, SELECTORS["floating"]),
    "touching text": (TOUCHING_TEXT, [SELECTORS["floating"], SELECTORS["identifier"]]),
    "chips off their paint": (CHIPS_PAST_THEIR_PAINT, [SELECTORS["chip"], SELECTORS["unseen"]]),
    "past the clamp": PAST_THE_CLAMP,
    "cut decisions": (CUT_DECISIONS, [SELECTORS["decision_detail"], "*", SELECTORS["unseen"]]),
    "cut code in text": (CUT_DECISIONS, [SELECTORS["running_text"], SELECTORS["code"], SELECTORS["unseen"]]),
    "changes": CHANGES_READ,
    "chips": CHIPS_READ,
    "fact rows": FACT_ROWS_READ,
    "facts across their band": (FACTS_SHORT_OF_THEIR_BAND, [SELECTORS["fact_band"], SELECTORS["band_fact_cell"]]),
    "fact details in words": (DETAIL_WORDS_IN_ROWS, [_FACT_DETAIL, SELECTORS["unseen"]]),
    # A link in a fact's detail, or a word there that opens its tip, sits on
    # the line of the words beside it, as a chip does.
    "links in a fact's detail": (
        CHIPS_OFF_THEIR_LINE,
        [f"{_FACT_DETAIL} :is({SELECTORS['link']}, [tabindex])", SELECTORS["unseen"]],
    ),
    "facts inside their frame": (
        FACTS_AGAINST_THEIR_FRAME,
        [SELECTORS["band_fact_cell"], SELECTORS["unseen"], FACT_INSET],
    ),
    "raised text": (RAISED_TEXT, SELECTORS["unseen"]),
}
