"""Probes of boxes: what escapes the page, frames, overlaps, bands, forms and targets."""

from hq.platform.core.tests.browser.probes.describe import _DESCRIBE

_ESCAPES = (
    "() => {"
    + _DESCRIBE
    + """
  const width = document.documentElement.clientWidth;
  const found = [];
  if (document.documentElement.scrollWidth > width + 1) {
    found.push(`page scrolls sideways: ${document.documentElement.scrollWidth}px in ${width}px`);
  }
  for (const el of document.querySelectorAll('body *')) {
    const r = box(el);
    if (r.width < 2 || r.height < 2 || (r.right <= width + 1 && r.left >= -1)) continue;
    if (!el.checkVisibility()) continue;
    // Past the viewport is fine inside a box that scrolls; clipped, it is cut off.
    let held = false;
    for (let up = el.parentElement; up && up !== document.body; up = up.parentElement) {
      if (scrolls(up)) { held = true; break; }
    }
    if (!held) found.push(`${describe(el)} spans ${Math.round(r.left)}..${Math.round(r.right)}px`);
  }
  return found;
}"""
)

_NESTED_FRAMES = (
    "([exemptSelector, withinSelector]) => {"
    + _DESCRIBE
    + r"""
  // One frame per thing. A box that draws its own border inside a box that
  // draws one is two borders, two paddings and often two shadows around the
  // same content; the design has one surface per thing and dividers inside it.
  // Controls, chips, the steps of a path and menus are frames by design.
  const framed = (el) => {
    const style = getComputedStyle(el);
    const sides = ['Top', 'Right', 'Bottom', 'Left'].filter((side) =>
      parseFloat(style[`border${side}Width`]) > 0 && style[`border${side}Style`] !== 'none'
      && !/rgba\(0, 0, 0, 0\)|transparent/.test(style[`border${side}Color`]));
    const r = box(el);
    return sides.length >= 3 && r.width > 60 && r.height > 30;
  };
  const exempt = (el) => ['TABLE', 'TD', 'TH', 'TR', 'INPUT', 'SELECT', 'TEXTAREA', 'BUTTON', 'SUMMARY', 'CODE', 'PRE', 'IMG'].includes(el.tagName)
    || el.matches(exemptSelector)
    || el.closest(withinSelector);
  const found = [];
  for (const el of document.querySelectorAll('main *')) {
    if (exempt(el) || !el.checkVisibility() || !framed(el)) continue;
    for (let up = el.parentElement; up && up.tagName !== 'MAIN'; up = up.parentElement) {
      if (!exempt(up) && framed(up)) { found.push(`${describe(el)} framed inside ${describe(up)}`); break; }
    }
  }
  return found;
}"""
)

_SIDEWAYS = (
    "(allowed) => {"
    + _DESCRIBE
    + """
  // Nothing moves sideways except inside the one box built for it. A chip row,
  // a lane or a code block scrolled past the edge of a phone is content nobody
  // finds; a name cut off with an ellipsis is a deliberate truncation.
  const width = document.documentElement.clientWidth;
  const found = [];
  if (document.documentElement.scrollWidth > width + 1) {
    found.push(`page scrolls sideways: ${document.documentElement.scrollWidth}px in ${width}px`);
  }
  for (const el of document.querySelectorAll('body *')) {
    if (el.closest(allowed) || !el.checkVisibility()) continue;
    const r = box(el);
    if (r.width < 2 || r.height < 2) continue;
    const style = getComputedStyle(el);
    const boxed = ['auto', 'scroll', 'hidden', 'clip'].includes(style.overflowX);
    if (boxed && el.scrollWidth > el.clientWidth + 1 && style.textOverflow !== 'ellipsis') {
      found.push(`${describe(el)} holds ${el.scrollWidth}px in ${el.clientWidth}px (overflow-x: ${style.overflowX})`);
    } else if (r.right > width + 1 || r.left < -1) {
      found.push(`${describe(el)} spans ${Math.round(r.left)}..${Math.round(r.right)}px`);
    }
  }
  return found;
}"""
)

# A target a pointer cannot reliably hit: under 24 by 24 CSS px (WCAG 2.5.8)
# and none of the criterion's exceptions it can be measured for. The area a
# target is hit through is its box, grown by a pseudo-element it positions
# over the space beside it, and for a tick box by the labels that tick it.
_SMALL_TARGETS = (
    "([selector, least]) => {"
    + _DESCRIBE
    + """
  const found = [];
  // What the stylesheet sizes hit areas from is the criterion's size.
  const token = getComputedStyle(document.documentElement).getPropertyValue('--target-min').trim();
  if (token && token !== `${least}px`) found.push(`--target-min is ${token}, not ${least}px`);
  const px = (value) => parseFloat(value);
  const tick = (el) => el.matches('input[type=checkbox], input[type=radio]');
  const union = (a, b) => ({left: Math.min(a.left, b.left), top: Math.min(a.top, b.top),
    right: Math.max(a.right, b.right), bottom: Math.max(a.bottom, b.bottom)});
  const hung = (el, r, pseudo) => {
    const style = getComputedStyle(el);
    const after = getComputedStyle(el, pseudo);
    if (style.position === 'static' || after.position !== 'absolute' || after.content === 'none'
        || after.display === 'none' || after.pointerEvents === 'none') return r;
    const inset = [after.top, after.right, after.bottom, after.left].map(px);
    if (inset.some(Number.isNaN)) return r;
    return union(r, {
      top: r.top + px(style.borderTopWidth) + inset[0],
      right: r.right - px(style.borderRightWidth) - inset[1],
      bottom: r.bottom - px(style.borderBottomWidth) - inset[2],
      left: r.left + px(style.borderLeftWidth) + inset[3],
    });
  };
  const area = (el) => {
    let r = hung(el, hung(el, box(el), '::before'), '::after');
    if (tick(el)) for (const label of el.labels) if (label.checkVisibility()) r = union(r, box(label));
    return {...r, width: r.right - r.left, height: r.bottom - r.top};
  };
  // A control drawn as the browser draws it: no author style changed its
  // look, and it is the size a copy with no author style is.
  const host = document.createElement('div');
  host.style.setProperty('position', 'absolute');
  host.style.setProperty('visibility', 'hidden');
  document.body.append(host);
  const bare = host.attachShadow({mode: 'open'});
  const pristine = new Map();
  const browsers = (el, r) => {
    if (!el.matches('input, select') || getComputedStyle(el).appearance === 'none') return false;
    const key = `${el.tagName} ${el.type}`;
    if (!pristine.has(key)) {
      const copy = document.createElement(el.tagName);
      if (el.type && el.tagName === 'INPUT') copy.type = el.type;
      bare.append(copy);
      pristine.set(key, box(copy));
    }
    const p = pristine.get(key);
    return Math.abs(p.width - r.width) < .5 && Math.abs(p.height - r.height) < .5;
  };
  // In a sentence: set on a line of text, among words that are no target's.
  // Walked without a filter callback, which an engine with script off
  // refuses to call back into.
  const words = document.createRange();
  const inSentence = (el) => {
    if (!/^(inline|-webkit-inline)/.test(getComputedStyle(el).display)) return false;
    let block = el.parentElement;
    while (block.parentElement && ['inline', 'contents'].includes(getComputedStyle(block).display)) {
      block = block.parentElement;
    }
    const walker = document.createTreeWalker(block, NodeFilter.SHOW_TEXT);
    for (let text = walker.nextNode(); text; text = walker.nextNode()) {
      if (!text.textContent.trim()) continue;
      // Text hidden from the accessibility tree inside a link is no part of
      // its name: a sentence's mark held there (value_tags.breakable) is the
      // sentence's.
      if (!text.parentElement.closest('[aria-hidden="true"]')) {
        if (el.contains(text)) continue;
        const owner = text.parentElement.closest(selector);
        if (owner && block.contains(owner)) continue;
      }
      words.selectNodeContents(text);
      if ([...words.getClientRects()].some((r) => r.width > 1 && r.height > 1)) return true;
    }
    return false;
  };
  // Under something drawn over it (an open menu): no pointer reaches it there,
  // whatever its size. What covers it is an element whose own box holds the
  // target's center, so a neighbor's hit area hung over it is not a cover.
  const covered = (el, r) => {
    const x = (r.left + r.right) / 2, y = (r.top + r.bottom) / 2;
    const top = document.elementFromPoint(x, y);
    if (!top || el.contains(top) || top.contains(el)) return false;
    if (el.labels && [...el.labels].some((label) => label.contains(top))) return false;
    const over = top.getBoundingClientRect();
    return over.left <= x && x <= over.right && over.top <= y && y <= over.bottom;
  };
  try {
    const targets = [];
    for (const el of new Set([...document.querySelectorAll(selector)])) {
      if (el.matches(':disabled') || !el.checkVisibility({visibilityProperty: true})) continue;
      const drawn = box(el);
      // Out of sight until focused (a skip link), or drawn by its label.
      if (drawn.width <= 1 || drawn.height <= 1 || covered(el, drawn)) continue;
      targets.push({el, drawn, r: area(el)});
    }
    const nested = (a, b) => a.el.contains(b.el) || b.el.contains(a.el);
    const meet = (a, b) => Math.min(a.right, b.right) - Math.max(a.left, b.left) > .5
      && Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > .5;
    // An area two targets share is neither's: one hung over another
    // target's area counts only the box it draws.
    const grown = new Map(targets.map((t) => [t, t.r]));
    for (const t of targets) {
      if (targets.some((o) => o !== t && !nested(o, t) && meet(grown.get(t), grown.get(o)))) t.r = t.drawn;
    }
    const center = ({r}) => [(r.left + r.right) / 2, (r.top + r.bottom) / 2];
    const small = new Set(targets.filter(({r}) => r.width < least - .5 || r.height < least - .5));
    // Spacing: a 24px circle on the target crosses no other target and no
    // other undersized target's circle.
    const spaced = (t) => {
      const [x, y] = center(t);
      return !targets.some((o) => {
        if (o === t || nested(o, t)) return false;
        const [ox, oy] = center(o);
        if (small.has(o) && Math.hypot(ox - x, oy - y) < least) return true;
        const dx = Math.max(o.r.left - x, 0, x - o.r.right);
        const dy = Math.max(o.r.top - y, 0, y - o.r.bottom);
        return Math.hypot(dx, dy) < least / 2;
      });
    };
    const unmet = [...small].filter((t) => !browsers(t.el, t.drawn) && !inSentence(t.el) && !spaced(t));
    // Equivalent: the control it names as doing the same job is 24px square
    // and in the same cell or region.
    const sized = new Map(targets.filter((t) => !small.has(t)).map((t) => [t.el, t]));
    const region = (el) => el.closest('td, th, section, [role=region]');
    const standIn = (el) => {
      const named = document.getElementById(el.dataset.targetEquivalent || '');
      return Boolean(named && sized.has(named) && region(named) === region(el));
    };
    for (const t of unmet) {
      if (standIn(t.el)) continue;
      const said = (t.el.textContent.trim() || t.el.getAttribute('aria-label') || t.el.title || '').slice(0, 30);
      found.push(`${describe(t.el)} in ${describe(t.el.parentElement)} is hit through ${Math.round(t.r.width)}x${Math.round(t.r.height)}px: ${said}`);
    }
  } finally {
    host.remove();
  }
  return [...new Set(found)];
}"""
)

# A grid whose items stretch ends each row together, and so does one the
# stylesheet pairs, whatever its own alignment says.
_PAIRS = (
    "(paired) => {"
    + _DESCRIBE
    + """
  const found = [];
  for (const grid of document.querySelectorAll('main *')) {
    const style = getComputedStyle(grid);
    if (!['grid', 'inline-grid'].includes(style.display)) continue;
    if (!grid.matches(paired) && !['normal', 'stretch'].includes(style.alignItems)) continue;
    const rows = new Map();
    for (const [item, own, r] of inFlow(grid)) {
      if (!['auto', 'normal', 'stretch'].includes(own.alignSelf)) continue;
      if (own.gridRowEnd !== 'auto' || own.height !== 'auto' && own.height !== `${r.height}px`) continue;
      const key = Math.round(r.top);
      rows.set(key, [...(rows.get(key) || []), [item, r]]);
    }
    for (const row of rows.values()) {
      const bottoms = row.map(([, r]) => Math.round(r.bottom));
      if (Math.max(...bottoms) - Math.min(...bottoms) > 1) {
        found.push(`${describe(grid)}: ${row.map(([item]) => describe(item)).join(' / ')} end at ${bottoms}`);
      }
    }
  }
  return found;
}"""
)

_OVERLAPS = (
    "() => {"
    + _DESCRIBE
    + """
  const found = [];
  for (const parent of document.querySelectorAll('main *')) {
    const display = getComputedStyle(parent).display;
    if (!/grid|flex/.test(display)) continue;
    // A sticky item is drawn over what scrolls under it; that is what it is for.
    const items = inFlow(parent).filter(([, style]) => style.position !== 'sticky');
    for (let i = 0; i < items.length; i += 1) {
      for (let j = i + 1; j < items.length; j += 1) {
        const [a, , ra] = items[i];
        const [b, , rb] = items[j];
        const x = Math.min(ra.right, rb.right) - Math.max(ra.left, rb.left);
        const y = Math.min(ra.bottom, rb.bottom) - Math.max(ra.top, rb.top);
        if (x > 1 && y > 1) found.push(`${describe(a)} overlaps ${describe(b)} by ${Math.round(x)}x${Math.round(y)}px`);
      }
    }
  }
  return found;
}"""
)

# A route's mark (a hop's icon, a check's tick) holds its glyph in its middle.
# A glyph keeps the line height of the row around it unless its mark sets its
# own, and a line taller than the mark pushes the glyph off its center.
_OFF_CENTER_MARKS = (
    "(mark) => {"
    + _DESCRIBE
    + """
  const found = [];
  const range = document.createRange();
  for (const el of document.querySelectorAll('main ' + mark)) {
    if (!el.checkVisibility() || !el.textContent.trim() && !el.firstElementChild) continue;
    range.selectNodeContents(el);
    const glyph = range.getBoundingClientRect();
    const at = box(el);
    const dy = glyph.top + glyph.height / 2 - (at.top + at.height / 2);
    const dx = glyph.left + glyph.width / 2 - (at.left + at.width / 2);
    if (Math.abs(dy) > 1 || Math.abs(dx) > 1) found.push(`${describe(el)}: glyph ${dx.toFixed(1)}, ${dy.toFixed(1)} off its center`);
  }
  return [...new Set(found)];
}"""
)

# A table's frame spans the box it sits in, as its heading does: a frame cut to
# its columns leaves the heading's count and the page's edge beyond its end.
_NARROW_FRAMES = (
    "(scroll) => {"
    + _DESCRIBE
    + """
  const found = [];
  for (const el of document.querySelectorAll('main ' + scroll)) {
    if (!el.checkVisibility()) continue;
    const parent = el.parentElement;
    const style = getComputedStyle(parent);
    const room = parent.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
    const width = box(el).width;
    if (width < room - 2) found.push(`${describe(el)} in ${describe(parent)}: ${Math.round(width)}px of ${Math.round(room)}px`);
  }
  return found;
}"""
)

# A band cell with no padding: its content touches the hairlines around it.
# The frame rule zeroes the inset of anything nested in a surface, and a band
# is one, so a component placed in a band loses its padding without a trace.
# A frame across most of the page whose content fills less than half of it (a
# short table stretched wide, a one-line card): the content should set the width.
_EMPTY_FRAMES = (
    "([selector, content]) => {"
    + _DESCRIBE
    + """
  const page = document.querySelector('main').clientWidth;
  const found = [];
  const range = document.createRange();
  for (const frame of document.querySelectorAll('main :is(' + selector + ')')) {
    const r = frame.getBoundingClientRect();
    if (r.width < page * 0.8 || r.height < 40 || !frame.checkVisibility()) continue;
    let right = r.left;
    const walker = document.createTreeWalker(frame, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      if (!node.textContent.trim() || !node.parentElement.checkVisibility()) continue;
      range.selectNodeContents(node);
      for (const line of range.getClientRects()) right = Math.max(right, line.right);
    }
    for (const el of frame.querySelectorAll(content)) {
      const box = el.getBoundingClientRect();
      if (box.width) right = Math.max(right, box.right);
    }
    const used = (right - r.left) / r.width;
    if (used < 0.5) found.push(`${describe(frame)} fills ${Math.round(used * 100)}% of its width`);
  }
  return found;
}"""
)

# A filled box whose text or buttons touch its edge. The frame rule strips a
# nested surface's border and padding together; one that keeps its own fill is
# still a tile, and without padding its content sits on the fill's edge.
_UNPADDED_TILES = (
    "([exempt, control, detached, disclosure]) => {"
    + _DESCRIBE
    + """
  // What a disclosure holds is laid out only when it is open.
  for (const details of document.querySelectorAll(disclosure)) details.open = true;
  const fill = (el) => {
    const c = getComputedStyle(el).backgroundColor;
    return c === 'transparent' || /rgba\\(.*, 0\\)$/.test(c) ? '' : c;
  };
  const behind = (el) => {
    for (let at = el.parentElement; at; at = at.parentElement) {
      const c = fill(at);
      if (c) return c;
    }
    return '';
  };
  const found = [];
  for (const el of document.querySelectorAll('main *')) {
    if (el.closest(exempt) || !el.checkVisibility()) continue;
    const own = fill(el);
    if (!own || own === behind(el)) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 120 || r.height < 48) continue;
    const style = getComputedStyle(el);
    if (parseFloat(style.paddingLeft) >= 4 && parseFloat(style.paddingBottom) >= 4) continue;
    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    const range = document.createRange();
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const holder = node.parentElement.getBoundingClientRect();
      if (!node.textContent.trim() || !node.parentElement.checkVisibility() || holder.width <= 1) continue;
      const within = node.parentElement;
      if (within.closest(detached)) continue;
      if (within.closest(exempt) && !within.closest(control)) continue;
      // A control that draws no box (a bare glyph, its touch target hung
      // into the padding) is measured by what it draws.
      const pressed = node.parentElement.closest(control);
      const drawn = pressed && (fill(pressed) || parseFloat(getComputedStyle(pressed).borderBottomWidth) > 0);
      const box = drawn ? pressed : node;
      let at;
      if (box === node) { range.selectNodeContents(node); at = range.getBoundingClientRect(); }
      else at = box.getBoundingClientRect();
      if (at.width && (at.left - r.left < 4 || r.bottom - at.bottom < 4)) {
        found.push(`${describe(el)}: ${node.textContent.trim().slice(0, 40)}`);
        break;
      }
    }
  }
  return [...new Set(found)];
}"""
)

# A band of up to six cells lays out as whole even rows: every row it wraps
# to holds as many cells as the first (Design.md, "One band"). A band of more
# lays out in even columns: every row but its last holds as many cells as the
# first, and every cell is as wide as the others.
_UNEVEN_BANDS = (
    "([band, aside]) => {"
    + _DESCRIBE
    + """
  const found = [];
  for (const el of document.querySelectorAll('main ' + band)) {
    if (!el.checkVisibility()) continue;
    const cells = [...el.children].filter((cell) => !cell.matches(aside) && cell.checkVisibility());
    if (cells.length < 2) continue;
    const rows = new Map();
    for (const cell of cells) {
      const top = Math.round(box(cell).top);
      rows.set(top, (rows.get(top) || 0) + 1);
    }
    const counts = [...rows.values()];
    const full = cells.length > 6 ? counts.slice(0, -1) : counts;
    const widths = cells.map((cell) => box(cell).width);
    const uneven = new Set(full).size > 1 || (cells.length > 6 && Math.max(...widths) - Math.min(...widths) > 1);
    if (uneven) found.push(`${describe(el)}: ${counts.join(' + ')}, cells ${Math.round(Math.min(...widths))}..${Math.round(Math.max(...widths))}px`);
  }
  return found;
}"""
)

# A form is two columns or one: each row of its fields holds one or two and
# runs from its first edge to its last, so no field has a hole beside it.
_HOLED_FORMS = (
    "([form, field]) => {"
    + _DESCRIBE
    + """
  const found = [];
  for (const el of document.querySelectorAll('main ' + form)) {
    if (!el.checkVisibility()) continue;
    const style = getComputedStyle(el);
    const frame = box(el);
    const left = frame.left + parseFloat(style.paddingLeft) + parseFloat(style.borderLeftWidth);
    const right = frame.right - parseFloat(style.paddingRight) - parseFloat(style.borderRightWidth);
    const rows = new Map();
    for (const [item, , r] of inFlow(el)) {
      const top = Math.round(r.top);
      rows.set(top, [...(rows.get(top) || []), [item, r]]);
    }
    for (const row of rows.values()) {
      const fields = row.filter(([item]) => item.matches(field)).length;
      if (!fields) continue;
      const names = row.map(([item]) => describe(item)).join(' / ');
      if (fields > 2) found.push(`${describe(el)}: ${fields} fields in one row (${names})`);
      const before = Math.min(...row.map(([, r]) => r.left)) - left;
      const after = right - Math.max(...row.map(([, r]) => r.right));
      if (before > 1 || after > 1) found.push(`${describe(el)}: ${names} leave ${Math.round(before)}px before and ${Math.round(after)}px after`);
    }
  }
  return found;
}"""
)

# A figure reads with its label: a value under the label it answers, never
# left at the foot of a box grown to its neighbor's height. The budget is a
# wrapped label's second line and the row's gap.
_FIGURE_UNDER_LABEL = r"""([figure, label_, value_]) => {
  const found = [];
  for (const kpi of document.querySelectorAll('main ' + figure)) {
    const label = kpi.querySelector(':scope > ' + label_), value = kpi.querySelector(':scope ' + value_);
    if (!label || !value || !kpi.checkVisibility()) continue;
    // The words, not their boxes: a grid stretches a box to its track.
    const words = (el) => { const range = document.createRange(); range.selectNodeContents(el); return range.getBoundingClientRect(); };
    const gap = words(value).top - words(label).bottom;
    if (gap > 32) found.push(`"${label.textContent.trim()}": its value stands ${Math.round(gap)}px under its label`);
  }
  return found;
}"""

# A value sits beside its label wherever the box holding both has room for
# each on one line, and goes under it only where it does not. Measured on the
# words' first lines, since a grid stretches a box to its track; the room is
# each one's max-content width, read from a copy laid out beside them.
_UNDER_ITS_LABEL = r"""(labeled) => {
  const found = [];
  const firstLine = (el) => {
    const range = document.createRange();
    range.selectNodeContents(el);
    return [...range.getClientRects()].find((rect) => rect.width) || null;
  };
  const widest = (el) => {
    const copy = el.cloneNode(true);
    copy.style.cssText = 'position: absolute; visibility: hidden; display: inline-block; inline-size: max-content; max-inline-size: none';
    el.parentElement.append(copy);
    const width = copy.getBoundingClientRect().width;
    copy.remove();
    return width;
  };
  for (const second of document.querySelectorAll('main :is(' + labeled + ')')) {
    const first = second.previousElementSibling;
    if (!first.checkVisibility() || !second.checkVisibility()) continue;
    const a = firstLine(first), b = firstLine(second);
    if (!a || !b) continue;
    const level = Math.abs(a.top - b.top) < Math.min(a.height, b.height);
    if (level && (b.left >= a.right - 1 || b.right <= a.left + 1)) continue;
    const box = second.parentElement, style = getComputedStyle(box);
    const room = box.clientWidth - parseFloat(style.paddingInlineStart) - parseFloat(style.paddingInlineEnd);
    const needed = widest(first) + (parseFloat(style.columnGap) || 0) + widest(second);
    if (needed > room) continue;
    const words = (el) => el.textContent.trim().replace(/\s+/g, ' ').slice(0, 40);
    found.push(`"${words(first)}": "${words(second)}" goes under it with ${Math.round(room)}px for the ${Math.round(needed)}px both take`);
  }
  return found;
}"""

# The map scrolls with the page, and draws no lane with nothing in it.
_MAP = """([map, lane, node]) => {
  const box = document.querySelector(map);
  if (!box) return null;
  return {
    scrolls: ['auto', 'scroll'].includes(getComputedStyle(box).overflowY),
    empty: [...box.querySelectorAll(lane)]
      .filter((el) => el.checkVisibility() && !el.querySelector(node))
      .map((el) => el.textContent.trim().slice(0, 30)),
  };
}"""
