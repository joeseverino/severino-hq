"""The script every in-page probe is built on: how an element is named, measured and seen."""

# In-page probes. Each returns a list of findings; empty is the pass.
DESCRIBE = """
const describe = (el) => el.tagName.toLowerCase()
  + (el.id ? '#' + el.id : '')
  + (typeof el.className === 'string' && el.className.trim()
     ? '.' + el.className.trim().split(/\\s+/).join('.') : '');
const box = (el) => { const r = el.getBoundingClientRect();
  return {top: r.top, bottom: r.bottom, left: r.left, right: r.right,
          width: r.width, height: r.height}; };
const scrolls = (el) => ['auto', 'scroll'].includes(getComputedStyle(el).overflowX);
const inFlow = (parent) => {
  const found = [];
  const walk = (el) => {
    for (const child of el.children) {
      const style = getComputedStyle(child);
      if (style.display === 'contents') { walk(child); continue; }
      if (style.display === 'none' || ['absolute', 'fixed'].includes(style.position)) continue;
      const r = box(child);
      if (r.width < 2 || r.height < 2) continue;
      found.push([child, style, r]);
    }
  };
  walk(parent);
  return found;
};
"""

# The part of a rect held by `el` that a reader sees: cut by `el` and every
# box above it that clips what it holds (overflow, `clip-path`), and null when
# nothing is left or one of them is not drawn at all. An absolutely placed box
# escapes the overflow of every box up to the one it is placed against;
# `clip-path` cuts all a box paints. A `display: contents` box has no box to
# clip with.
PAINTED_PART = r"""
const painted = (el, rect) => {
  let shown = el;
  while (shown.parentElement && getComputedStyle(shown).display === 'contents') shown = shown.parentElement;
  if (!shown.checkVisibility({opacityProperty: true, visibilityProperty: true})) return null;
  let {left, top, right, bottom} = rect;
  const cut = (r) => {
    left = Math.max(left, r.left); right = Math.min(right, r.right);
    top = Math.max(top, r.top); bottom = Math.min(bottom, r.bottom);
  };
  const length = (value, size) => (value.endsWith('%') ? (parseFloat(value) / 100) * size : parseFloat(value)) || 0;
  let placed = null;
  for (let up = el; up && up !== document.documentElement; up = up.parentElement) {
    const style = getComputedStyle(up);
    if (style.display === 'contents') continue;
    const b = up.getBoundingClientRect();
    const transformed = style.transform !== 'none';
    const holds = placed === null
      || (placed === 'absolute' && (style.position !== 'static' || transformed))
      || (placed === 'fixed' && transformed);
    if (holds) placed = null;
    // Overflow clips what a box holds, and a run of inline text holds nothing.
    if (holds && style.display !== 'inline') {
      const inner = {left: b.left + up.clientLeft, top: b.top + up.clientTop};
      if (style.overflowX !== 'visible') cut({left: inner.left, right: inner.left + up.clientWidth, top: -Infinity, bottom: Infinity});
      if (style.overflowY !== 'visible') cut({top: inner.top, bottom: inner.top + up.clientHeight, left: -Infinity, right: Infinity});
    }
    if (style.clipPath !== 'none') {
      const inset = /^inset\(([^)]*?)(?:\s+round\b[^)]*)?\)$/.exec(style.clipPath);
      const [t, r = t, bo = t, l = r] = inset ? inset[1].trim().split(/\s+/) : ['0px'];
      cut({left: b.left + length(l, b.width), right: b.right - length(r, b.width),
           top: b.top + length(t, b.height), bottom: b.bottom - length(bo, b.height)});
    }
    if (placed === null && ['absolute', 'fixed'].includes(style.position)) placed = style.position;
    if (right - left <= 0 || bottom - top <= 0) return null;
  }
  return {left, top, right, bottom, width: right - left, height: bottom - top};
};
"""

# Each character of an element's text with the line it is drawn on, for the
# rules that judge where text breaks. A rule asking whether a part fits its
# line cannot judge a table cell: the column's width is settled after the
# browser chose where the cell's text breaks, so the line it measures is the
# break's outcome. And `checkVisibility()` on a `display: contents` box is
# false, which would skip the text inside it; its text is judged by the box
# it lays out in.
LINES = r"""
const range = document.createRange();
const charsOf = (node) => {
  // A box with no box of its own (`display: contents`) is never "visible":
  // its text is, as far as the box it lays out in is.
  let box = node.parentElement;
  while (box.parentElement && getComputedStyle(box).display === 'contents') box = box.parentElement;
  if (!box.checkVisibility()) return [];
  const out = [];
  for (let i = 0; i < node.length; i++) {
    range.setStart(node, i);
    range.setEnd(node, i + 1);
    const rect = [...range.getClientRects()].find((r) => r.width);
    out.push({ch: node.data[i], top: rect ? Math.round(rect.top) : null, bottom: rect ? rect.bottom : 0, node, at: i});
  }
  return out;
};
const chars = (root) => {
  const out = [];
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) out.push(...charsOf(node));
  return out;
};
const broke = (list, i) => list[i].top !== null && list[i - 1].top !== null && list[i].top > list[i - 1].top + 1;
"""
