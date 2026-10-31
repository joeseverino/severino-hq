"""Probes of running text: broken words, text that overlaps and text past its clamp."""

from hq.platform.core.tests.browser.probes.describe import DESCRIBE, LINES, PAINTED_PART

# A plain word split across lines inside a table cell: its column was given
# less than the word, because another column's content took the width. A word
# inside a run with no space in it (a link, a digest) that is itself wider than
# its cell breaks somewhere wherever the column is, so it is not counted.
BROKEN_WORDS = (
    "() => {"
    + DESCRIBE
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
    const style = getComputedStyle(box);
    return box.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
  };
  for (const cell of document.querySelectorAll('main td')) {
    const walker = document.createTreeWalker(cell, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      if (!node.parentElement.checkVisibility()) continue;
      const line = lineOf(node, cell);
      for (const run of node.textContent.matchAll(/\\S+/g)) {
        const wide = across(node, run.index, run.index + run[0].length).reduce((sum, r) => sum + r.width, 0) > line;
        for (const match of run[0].matchAll(/[A-Za-z]{4,}/g)) {
          const at = run.index + match.index;
          const lines = new Set(across(node, at, at + match[0].length).map((r) => Math.round(r.top)));
          if (lines.size > 1 && !(wide && match[0].length < run[0].length)) found.push(`${describe(cell)} breaks "${match[0]}"`);
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
# both class PR) unless the writer glues them.
BROKEN_WORDS_IN_TEXT = (
    "(identifier) => {"
    + DESCRIBE
    + r"""
  const WORD = /[A-Za-z]{4,}/g;
  // A figure standing on its own (not the digits inside a word), with what
  // goes before it: a sign, a word joiner, a currency mark.
  const NUMBER = /(?<![\p{L}\p{N}_.,$\u00a3\u20ac\u2060+\u2212])(?:[+\u2212]\u2060?)?[$\u00a3\u20ac]?\d(?:[\d,.]*\d)?%?/gu;
  const main = document.querySelector('main');
  const range = document.createRange();
  const found = [];
  const words = document.createTreeWalker(main, NodeFilter.SHOW_TEXT);
  for (let node = words.nextNode(); node; node = words.nextNode()) {
    const el = node.parentElement;
    if (el.closest(identifier) || el.closest('pre, script, style, template') || !el.checkVisibility()) continue;
    for (const [kind, pattern] of [['word', WORD], ['number', NUMBER]]) {
      for (const token of node.data.matchAll(pattern)) {
        range.setStart(node, token.index);
        range.setEnd(node, token.index + token[0].length);
        const lines = new Set([...range.getClientRects()].filter((r) => r.width).map((r) => Math.round(r.top)));
        if (lines.size > 1) found.push(`${describe(el)} breaks the ${kind} "${token[0].replace(/\u2060/g, '')}"`);
      }
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


# What a card's own buttons decide, cut short anywhere: wider or taller than
# its box shows. Approving on half of what a change does is not deciding it.
CUT_DECISIONS = (
    "(selector) => {"
    + DESCRIBE
    + r"""
  const found = [];
  for (const el of document.querySelectorAll(`main ${selector}`)) {
    if (!el.checkVisibility()) continue;
    if (el.scrollWidth > el.clientWidth + 1 || el.scrollHeight > el.clientHeight + 1)
      found.push(`${describe(el)} "${el.textContent.trim().slice(0, 40)}" is cut short`);
  }
  return found;
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
const drawn = (root) => {
  const found = [];
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    if (node.data.trim() && !node.parentElement.closest(unseen) && rects(node).length) found.push(node);
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
