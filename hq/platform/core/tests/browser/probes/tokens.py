"""Probes of tokens: where one may break, the scraps a break leaves, and marks stranded from their token."""

from hq.platform.core.tests.browser.probes.describe import _DESCRIBE, _LINES

# An identifier (an address, a host or record name, a path, an image) that
# fits on its line moves to the next line whole. One longer than its line
# breaks only at a seam `|breakable` marks (value_tags._BREAK_AFTER: after a
# slash, after a colon run, after a dot without a digit on both sides), and
# inside the part between two seams only when that part alone is longer than
# its line. A hyphen is no seam: a name split there reads as two names.
_TOKEN_BREAKS = (
    "([token, cellName, tokenPart, free, openRow]) => {"
    + _DESCRIBE
    + _LINES
    + r"""
  const found = [];
  const digit = /\d/;
  const seamAfter = (list, i) => {
    const prev = list[i - 1].ch, next = list[i].ch, before = i > 1 ? list[i - 2].ch : '';
    return prev === '/' || (prev === ':' && next !== ':') || (prev === '.' && !(digit.test(before) && digit.test(next)));
  };
  const px = (value) => parseFloat(value) || 0;
  const across = (style, edge) => px(style[edge + 'Left' + (edge === 'border' ? 'Width' : '')])
    + px(style[edge + 'Right' + (edge === 'border' ? 'Width' : '')]);
  // The run laid out unbroken where it stands: every box of the token held
  // to one line for the measure and let go, so the width is the browser's own
  // for these characters in these boxes (their font, kerning, and the seams
  // between inline blocks), not a copy's.
  const widthOf = (el, list) => {
    const held = [el, ...el.querySelectorAll('*')].map((box) => {
      const was = [box.style.getPropertyValue('white-space'), box.style.getPropertyPriority('white-space')];
      box.style.setProperty('white-space', 'nowrap', 'important');
      return () => (was[0] ? box.style.setProperty('white-space', ...was) : box.style.removeProperty('white-space'));
    });
    const first = list[0], last = list[list.length - 1];
    range.setStart(first.node, first.at);
    range.setEnd(last.node, last.at + 1);
    const width = range.getBoundingClientRect().width;
    held.forEach((release) => release());
    return width;
  };
  // The line the run is set on when it starts one: the content box of the
  // nearest block container around it, less what each inline box between
  // takes from that line (an inline block's padding, border and margin: a
  // code chip's own padding, say). An inline flex or grid box lays its
  // contents out in items of its own, so its content box is the line.
  const lineOf = (list) => {
    let box = list[0].node.parentElement;
    while (!box.contains(list[list.length - 1].node)) box = box.parentElement;
    const through = [];
    let inset = 0;
    for (;; box = box.parentElement) {
      const style = getComputedStyle(box);
      if (style.display === 'contents') continue;
      if (!/^inline(-block)?$/.test(style.display)) break;
      const taken = across(style, 'padding') + across(style, 'border') + across(style, 'margin');
      inset += taken;
      through.push(`${describe(box)} (${style.display}) takes ${taken.toFixed(2)}`);
    }
    const style = getComputedStyle(box);
    // The fractional border box, less its borders, padding and any scrollbar
    // (the gap between the rounded offset and client widths).
    const scrollbar = Math.max(0, box.offsetWidth - box.clientWidth - Math.round(across(style, 'border')));
    const content = box.getBoundingClientRect().width - across(style, 'border') - across(style, 'padding') - scrollbar;
    return {width: content - inset, said: `line ${content.toFixed(2)} in ${describe(box)} (${style.display})`
      + (through.length ? `, ${through.join(', ')}` : '')};
  };
  const font = (list) => {
    const s = getComputedStyle(list[0].node.parentElement);
    return `font ${s.fontWeight} ${s.fontSize} ${s.fontFamily.split(',')[0]}, letter-spacing ${s.letterSpacing}, `
      + `word-spacing ${s.wordSpacing}, features ${s.fontFeatureSettings}, kerning ${s.fontKerning}`;
  };
  // Both widths come from the same layout, on its grid of 1/64 px; a run
  // that fits its line by less than that grid's step is one the browser may
  // have rounded past it.
  const fits = (width, line) => width <= line - 1 / 64;
  const numbers = (el, list, width, line) => `run ${width.toFixed(2)}px, ${line.said}, `
    + `so ${line.width.toFixed(2)}px for the run; token box ${el.getBoundingClientRect().width.toFixed(2)}px; ${font(list)}`;
  for (const el of document.querySelectorAll('main ' + token)) {
    if (!el.checkVisibility() || el.parentElement.closest(token)) continue;
    const all = chars(el);
    const text = all.map((c) => c.ch).join('');
    for (const word of text.matchAll(/\S+/g)) {
      const list = all.slice(word.index, word.index + word[0].length);
      const at = list.findIndex((c, i) => i > 0 && broke(list, i));
      if (at < 0) continue;
      const shown = (i) => `"${word[0].slice(0, i)}|${word[0].slice(i)}"`.slice(0, 90);
      const line = lineOf(list);
      const whole = widthOf(el, list);
      if (fits(whole, line.width)) {
        found.push(`${describe(el)} breaks ${shown(at)} though it fits its line: ${numbers(el, list, whole, line)}`);
        continue;
      }
      for (let i = 1; i < list.length; i++) {
        if (!broke(list, i) || seamAfter(list, i)) continue;
        let start = i - 1;
        while (start > 0 && !seamAfter(list, start)) start -= 1;
        let end = i;
        while (end < list.length - 1 && !seamAfter(list, end + 1)) end += 1;
        const part = list.slice(start, end + 1);
        const width = widthOf(el, part);
        const its = lineOf(part);
        if (fits(width, its.width)) {
          found.push(`${describe(el)} breaks ${shown(i)} inside a part that fits its line: ${numbers(el, part, width, its)}`);
          break;
        }
      }
    }
  }
  // A name in a table cell is judged by its hyphenated parts alone: whether
  // it fits its line is the column's outcome, but a part broken across lines
  // is never one, since a cell cuts such a part instead. An open row lifts
  // the cut with its columns held (surfaces.css), and the line beneath it
  // shows each value whole (tables.css), so there a part breaks only when it
  // alone is longer than its line.
  const opened = (el) => el.closest(openRow) || el.closest('[data-row-detail]');
  const whole = (c) => c.node.parentElement.closest(tokenPart);
  const mustBreak = (el, part) => {
    const run = chars(part);
    return !fits(widthOf(el, run), lineOf(run).width);
  };
  for (const el of document.querySelectorAll('main ' + cellName)) {
    if (!el.checkVisibility()) continue;
    const all = chars(el);
    const open = opened(el);
    const at = all.findIndex((c, i) => i > 0 && broke(all, i) && whole(c) && whole(c) === whole(all[i - 1])
      && !(open && mustBreak(el, whole(c))));
    if (at < 0) continue;
    const text = all.map((c) => c.ch).join('');
    found.push(`${describe(el)} breaks "${text.slice(0, at)}|${text.slice(at)}" inside a hyphenated part`.slice(0, 140));
  }
  // Every other word in the page that is not prose (a cell, a link, a chip, a
  // heading, a label, a meta line) and has a hyphen or a seam inside it is a
  // name, however it was printed, and breaks only at a seam. Outside a table
  // a part longer than its line alone breaks where it must; a cell cuts a
  // hyphenated one.
  // `free` is prose and a row's own name, which wrap by rules of their own.
  const named = /\w(?:-|\/|:+|\.)\w/;
  const text = document.createTreeWalker(document.querySelector('main'), NodeFilter.SHOW_TEXT);
  for (let node = text.nextNode(); node; node = text.nextNode()) {
    const el = node.parentElement;
    if (!named.test(node.data) || el.closest(free) || el.closest(token)) continue;
    const all = charsOf(node);
    const cell = el.closest('td, th');
    // A hyphenated part runs on past this text node where a scrap of it is
    // held in a box of its own (value_tags._held), so it is measured whole.
    const held = el.closest(tokenPart);
    const partOf = (run, i) => {
      let start = i - 1;
      while (start > 0 && !seamAfter(run, start)) start -= 1;
      let end = i;
      while (end < run.length - 1 && !seamAfter(run, end + 1)) end += 1;
      return run.slice(start, end + 1);
    };
    for (const word of all.length ? node.data.matchAll(/\S+/g) : []) {
      if (!named.test(word[0])) continue;
      const list = all.slice(word.index, word.index + word[0].length);
      const at = list.findIndex((c, i) => {
        if (i === 0 || !broke(list, i) || seamAfter(list, i)) return false;
        const part = partOf(list, i);
        // A cell's width is the outcome of where its text broke, so a part
        // there is judged by what it is: a hyphenated one is cut, never
        // broken; one with no hyphen breaks where it must (Design.md). An open
        // row and the line beneath it show every value whole, so there a part
        // breaks as it would outside a table: only when it must.
        if (cell && !opened(cell)) return part.some((c) => c.ch === '-');
        if (!held) return fits(widthOf(el, part), lineOf(part).width);
        const run = chars(held);
        const unit = partOf(run, run.findIndex((d) => d.node === c.node && d.at === c.at));
        return fits(widthOf(held, unit), lineOf(unit).width);
      });
      if (at > 0) found.push(`${describe(el)} breaks "${word[0].slice(0, at)}|${word[0].slice(at)}" where it has no seam`.slice(0, 140));
    }
  }
  return [...new Set(found)];
}"""
)

# A piece of a token shorter than three characters, alone on a line where the
# token broke at a seam or a hyphen ("portainer-agent-" over "1"), reads as a
# stray mark. Only what shows counts: a cut name's lines past its clamp are
# hidden, and a part broken inside itself, where it had to be, is a run of
# letters with no seam to begin at.
_SCRAP = 3

_SCRAPS = (
    "([token, scrap]) => {"
    + _DESCRIBE
    + _LINES
    + r"""
  const found = [];
  const seam = /[-/.:]/;
  const clips = new Map();
  const clipped = (box) => {
    if (!clips.has(box)) clips.set(box, getComputedStyle(box).overflowX !== 'visible');
    return clips.get(box);
  };
  const shows = (c) => {
    range.setStart(c.node, c.at);
    range.setEnd(c.node, c.at + 1);
    const r = range.getBoundingClientRect();
    const x = r.left + r.width / 2, y = r.top + r.height / 2;
    for (let box = c.node.parentElement; box && box !== document.body; box = box.parentElement) {
      if (!clipped(box)) continue;
      const b = box.getBoundingClientRect();
      if (x < b.left || x > b.right || y < b.top || y > b.bottom) return false;
    }
    return true;
  };
  for (const el of document.querySelectorAll('main ' + token)) {
    if (!el.checkVisibility() || el.parentElement.closest(token)) continue;
    const all = chars(el).filter((c) => c.top !== null && /\S/.test(c.ch) && shows(c));
    const lines = [];
    for (const c of all) {
      if (!lines.length || Math.abs(c.top - lines[lines.length - 1][0].top) > 1) lines.push([]);
      lines[lines.length - 1].push(c);
    }
    if (lines.length < 2) continue;
    const text = all.map((c) => c.ch).join('');
    lines.forEach((line, i) => {
      if (line.length >= scrap) return;
      const before = i ? lines[i - 1][lines[i - 1].length - 1].ch : '';
      if (i && !seam.test(before)) return;
      const end = line[line.length - 1].ch;
      if (i < lines.length - 1 && !seam.test(end)) return;
      found.push(`${describe(el)} leaves "${line.map((c) => c.ch).join('')}" alone on a line of "${text}"`.slice(0, 160));
    });
  }
  return [...new Set(found)];
}"""
)

# A mark that closes what it follows (".", ",", ")") or opens what follows it
# ("("), on a different line from it. A line may break on either side of a box
# drawn inline (a token, code), even beside punctuation, so a mark written
# outside the box is held to it only by markup that says so (`.held-mark`,
# value_tags.breakable). A mark after a seam begins the piece the seam lets
# the line break before.
_STRANDED_MARKS = (
    "(seam) => {"
    + _DESCRIBE
    + r"""
  const found = [];
  const range = document.createRange();
  const charBox = (node, at) => {
    range.setStart(node, at);
    range.setEnd(node, at + 1);
    return range.getBoundingClientRect();
  };
  const rects = (el) => [...el.getClientRects()].filter((r) => r.width || r.height);
  const beside = (node, way) => {
    let next = node[way];
    while (next && next.nodeType === Node.COMMENT_NODE) next = next[way];
    return next && next.nodeType === Node.ELEMENT_NODE && next.checkVisibility() ? next : null;
  };
  const walker = document.createTreeWalker(document.querySelector('main') || document.body, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const text = node.data;
    if (!text.trim() || !node.parentElement.checkVisibility()) continue;
    const before = /^[.,;:!?)\]]/.test(text) && beside(node, 'previousSibling');
    if (before && !before.matches(seam)) {
      const last = rects(before).at(-1);
      if (last && charBox(node, 0).top >= last.bottom - 1) {
        found.push(`"${text.trim().slice(0, 12)}" starts a line apart from ${describe(before)} "${before.textContent.trim().slice(-30)}"`);
      }
    }
    const after = /[(\[]$/.test(text) && beside(node, 'nextSibling');
    if (after) {
      const first = rects(after)[0];
      if (first && first.top >= charBox(node, text.length - 1).bottom - 1) {
        found.push(`"${text.trim().slice(-12)}" ends a line apart from ${describe(after)} "${after.textContent.trim().slice(0, 30)}"`);
      }
    }
  }
  return [...new Set(found)];
}"""
)

# An identifier whose parts are text (`display: contents`, as a table makes
# them) inside a flex or grid box: each run of text and each seam between them
# is then an item of the box, so the name stands as side-by-side columns
# rather than one run that wraps, and its least width is every part's summed.
# A token that is one box, or text inside a box of its own, is one item.
_NAMES_IN_TRACKS = (
    "(part) => {"
    + _DESCRIBE
    + r"""
  const shown = (el) => getComputedStyle(el).display;
  const tracks = ['flex', 'inline-flex', 'grid', 'inline-grid'];
  // The box the part's pieces are laid out in: its nearest ancestor that is one.
  const boxOf = (el) => {
    let up = el.parentElement;
    while (up && shown(up) === 'contents') up = up.parentElement;
    return up;
  };
  // The items the part's pieces make: a run of text is one item however many
  // nodes it spans, and every other piece that draws is one of its own.
  const items = (el) => {
    let count = 0, inText = false;
    const walk = (node) => node.childNodes.forEach((child) => {
      if (child.nodeType === Node.TEXT_NODE) {
        if (child.data.trim() && !inText) { count += 1; inText = true; }
        return;
      }
      if (child.nodeType !== Node.ELEMENT_NODE) return;
      const display = shown(child);
      if (display === 'contents') { walk(child); return; }
      if (display === 'none' || ['absolute', 'fixed'].includes(getComputedStyle(child).position)) return;
      count += 1;
      inText = false;
    });
    walk(el);
    return count;
  };
  const found = [];
  document.querySelectorAll('main ' + part).forEach((el) => {
    if (shown(el) !== 'contents') return;
    // A part inside another counts once, as the outer one's pieces.
    const holder = el.parentElement.closest(part);
    if (holder && shown(holder) === 'contents' && boxOf(el) === boxOf(holder)) return;
    const box = boxOf(el);
    if (!box || !tracks.includes(shown(box)) || !box.checkVisibility()) return;
    if (items(el) > 1) found.push(`${describe(box)} lays "${el.textContent.trim().slice(0, 40)}" out as ${items(el)} items`);
  });
  return [...new Set(found)];
}"""
)
