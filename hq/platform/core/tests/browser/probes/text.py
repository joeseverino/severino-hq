"""Probes of running text: broken words, wrapped phrases, asides, squeezed titles and text that overlaps."""

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

WRAPPED_PHRASES = (
    "(selector) => {"
    + DESCRIBE
    + """
  // A pill or a time is one phrase: no taller than the same element holding one letter.
  const found = [];
  for (const pill of document.querySelectorAll(selector)) {
    if (!pill.getClientRects().length) continue;
    const probe = pill.cloneNode(false);
    probe.textContent = 'X';
    // Drawn as the phrase is, wherever it lands: a rule that hides whatever
    // follows a row's lead control would otherwise measure nothing.
    probe.style.display = getComputedStyle(pill).display;
    pill.after(probe);
    const one = probe.getBoundingClientRect().height;
    probe.remove();
    if (box(pill).height > one + 1) found.push(describe(pill) + ': ' + pill.textContent.trim() + ' (' + Math.round(box(pill).height) + 'px, one line ' + Math.round(one) + 'px, parent ' + getComputedStyle(pill.parentElement).display + ')');
  }
  return found;
}"""
)

LONE_ASIDE_WORDS = (
    "(aside) => {"
    + DESCRIBE
    + LINES
    + r"""
  const found = [];
  for (const el of document.querySelectorAll(aside)) {
    if (!el.closest('main') || !el.checkVisibility()) continue;
    const own = chars(el).filter((c) => c.top !== null);
    const last = Math.max(...own.map((c) => c.top));
    if (!own.some((c) => c.top < last)) continue;
    const words = own.filter((c) => c.top === last).map((c) => c.ch).join('').trim();
    if (words && !/\s/.test(words)) found.push(`${describe(el)} "${el.textContent.trim().slice(0, 40)}" leaves "${words}" alone on its last line`);
  }
  return found;
}"""
)

ASIDE_BESIDE_ITS_HEADING = (
    "(aside) => {"
    + DESCRIBE
    + LINES
    + r"""
  const found = [];
  for (const el of document.querySelectorAll('main ' + aside)) {
    const heading = el.closest('h1, h2, h3, h4');
    if (!heading || !el.checkVisibility()) continue;
    const own = chars(el).filter((c) => c.top !== null);
    if (new Set(own.map((c) => c.top)).size < 2) continue;
    const first = own[0];
    const words = [];
    const walker = document.createTreeWalker(heading, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      if (el.contains(node) || !node.data.trim()) continue;
      range.selectNodeContents(node);
      words.push(...[...range.getClientRects()].filter((r) => r.width));
    }
    if (words.some((r) => r.top < first.bottom - 1 && r.bottom > first.top + 1)) {
      found.push(`${describe(el)} wraps beside its heading "${heading.textContent.trim().slice(0, 30)}"`);
    }
  }
  return found;
}"""
)

# A page's title breaks at its spaces, or inside a word only when its
# controls have already taken a row of their own and the word alone is
# longer than the line: squeezed beside them, a name splits at a hyphen.
SQUEEZED_TITLES = (
    "([title, actions, pageHead]) => {"
    + DESCRIBE
    + LINES
    + r"""
  const found = [];
  for (const el of document.querySelectorAll('main ' + title)) {
    if (!el.checkVisibility()) continue;
    const list = chars(el);
    const breaks = list.map((c, i) => i > 0 && broke(list, i) && /\S/.test(list[i - 1].ch) && /\S/.test(c.ch));
    const at = breaks.indexOf(true);
    if (at < 0) continue;
    const head = el.closest(pageHead) || el.parentElement;
    const controls = head.querySelector(actions);
    const t = el.getBoundingClientRect();
    const beside = controls && controls.checkVisibility() && (() => {
      const c = controls.getBoundingClientRect();
      return c.top < t.bottom && c.bottom > t.top;
    })();
    if (beside) {
      const text = list.map((c) => c.ch).join('');
      found.push(`${describe(el)} breaks "${text.slice(0, at)}|${text.slice(at)}" beside its controls`.slice(0, 140));
    }
  }
  return found;
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
