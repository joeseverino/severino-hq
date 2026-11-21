"""Probes of boxes: what escapes the page, frames, overlaps, targets, the map and the chrome's insets."""

from hq.platform.core.included_parts import TOPOLOGY_MAP
from hq.platform.core.templatetags.table_tags import DATA_TABLE
from hq.platform.core.tests.browser.probes import Check, Plant
from hq.platform.core.tests.browser.probes.describe import DESCRIBE, PAINTED_PART
from hq.platform.core.tests.browser.selectors import SELECTORS
from hq.platform.core.tests.browser.widths import TARGET_MIN

# Nothing moves sideways except inside a box ``allowed`` selects: past the
# page's edge it is cut off, and a box that clips or scrolls what it holds
# without being allowed to hides it. A chip row, a lane or a code block
# scrolled past the edge of a phone is content nobody finds; a name cut off
# with an ellipsis is a deliberate truncation. What is hidden (`visibility`),
# such as a filter bar's sizer, is seen nowhere. ``ANY_SCROLLER`` allows every
# box that scrolls, whatever it is.
SIDEWAYS = (
    "(allowed) => {"
    + DESCRIBE
    + """
  const width = document.documentElement.clientWidth;
  const found = [];
  if (document.documentElement.scrollWidth > width + 1) {
    found.push(`page scrolls sideways: ${document.documentElement.scrollWidth}px in ${width}px`);
  }
  const allows = (el) => (allowed === null ? scrolls(el) : el.matches(allowed));
  const held = (el) => {
    for (let up = el.parentElement; up && up !== document.body; up = up.parentElement) {
      if (allows(up)) return true;
    }
    return false;
  };
  // The part of a box in sight: what a box that clips its content shows of
  // it. What such a box cuts is held on its own (the cut checks), and what
  // it clips away entirely (a heading drawn as nothing) is drawn nowhere.
  const seen = (el, r) => {
    let left = r.left;
    let right = r.right;
    for (let up = el.parentElement; up && up !== document.body; up = up.parentElement) {
      if (!['hidden', 'clip'].includes(getComputedStyle(up).overflowX)) continue;
      const by = up.getBoundingClientRect();
      left = Math.max(left, by.left);
      right = Math.min(right, by.right);
    }
    return right > left ? {left, right} : {left: 0, right: 0};
  };
  for (const el of document.querySelectorAll('body *')) {
    const r = box(el);
    if (r.width < 2 || r.height < 2 || !el.checkVisibility({ visibilityProperty: true }) || held(el)) continue;
    const style = getComputedStyle(el);
    const boxed = ['auto', 'scroll', 'hidden', 'clip'].includes(style.overflowX);
    if (boxed && !allows(el) && el.scrollWidth > el.clientWidth + 1 && style.textOverflow !== 'ellipsis') {
      found.push(`${describe(el)} holds ${el.scrollWidth}px in ${el.clientWidth}px (overflow-x: ${style.overflowX})`);
    } else if (seen(el, r).right > width + 1 || seen(el, r).left < -1) {
      found.push(`${describe(el)} spans ${Math.round(r.left)}..${Math.round(r.right)}px`);
    }
  }
  return found;
}"""
)

ANY_SCROLLER = None

# A target a pointer cannot reliably hit: under 24 by 24 CSS px (WCAG 2.5.8)
# and none of the criterion's exceptions it can be measured for. The area a
# target is hit through is its box, grown by a pseudo-element it positions
# over the space beside it, and for a tick box by the labels that tick it.
# ``floating`` names the panels drawn over the page, each a layer of its own.
SMALL_TARGETS = (
    "([selector, least, floating, scroll]) => {"
    + DESCRIBE
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
  const clippedAway = (el, r) => {
    for (let up = el.parentElement; up; up = up.parentElement) {
      const style = getComputedStyle(up);
      if (![style.overflowX, style.overflowY].some((value) => value === 'hidden' || value === 'clip')) continue;
      const b = up.getBoundingClientRect();
      if (r.bottom <= b.top || r.top >= b.bottom || r.right <= b.left || r.left >= b.right) return true;
    }
    return false;
  };
  // Inert: marked so, or outside the modal dialog open over the page, which
  // makes the rest of the document inert. No pointer reaches it.
  const modal = document.querySelector('dialog:modal');
  const inert = (el) => Boolean(el.closest('[inert]')) || Boolean(modal && !modal.contains(el));
  // The layer a target lies in: the floating panel out of flow that holds it
  // (a menu's, a popover, a dialog), or the page. A panel shown over the page
  // is dismissed by the next press beside it, so what lies beneath is out of
  // reach while it shows and back in reach once it is gone: a target in one
  // layer is never a neighbor of one in another (WCAG 2.5.8 leaves out a
  // target "while obscured by content displayed as a result of a user
  // interaction" and measures the new content's own).
  const layer = (el) => {
    for (let up = el.closest(floating); up; up = up.parentElement?.closest(floating)) {
      if (['absolute', 'fixed'].includes(getComputedStyle(up).position)) return up;
    }
    return null;
  };
  try {
    const targets = [];
    // Under a panel shown over the page: out of reach until it is dismissed,
    // and the target's size again then, so it still stands in for another.
    const beneath = [];
    for (const el of new Set([...document.querySelectorAll(selector)])) {
      if (el.matches(':disabled') || inert(el) || !el.checkVisibility({visibilityProperty: true})) continue;
      const drawn = box(el);
      // Out of sight until focused (a skip link), or drawn by its label.
      if (drawn.width <= 1 || drawn.height <= 1) continue;
      // Clipped wholly away by a box around it (the parts a `line_list` has
      // no room for): no pointer reaches it until what holds it shows it.
      if (clippedAway(el, drawn)) continue;
      if (covered(el, drawn)) beneath.push(el);
      else targets.push({el, drawn, r: area(el), layer: layer(el)});
    }
    // Neither's neighbor: one holds the other, or they lie in different layers.
    const apart = (a, b) => a.el.contains(b.el) || b.el.contains(a.el) || a.layer !== b.layer;
    const meet = (a, b) => Math.min(a.right, b.right) - Math.max(a.left, b.left) > .5
      && Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > .5;
    // An area two targets share is neither's: one hung over another
    // target's area counts only the box it draws.
    const grown = new Map(targets.map((t) => [t, t.r]));
    for (const t of targets) {
      if (targets.some((o) => o !== t && !apart(o, t) && meet(grown.get(t), grown.get(o)))) t.r = t.drawn;
    }
    const center = ({r}) => [(r.left + r.right) / 2, (r.top + r.bottom) / 2];
    const small = new Set(targets.filter(({r}) => r.width < least - .5 || r.height < least - .5));
    // Spacing: a 24px circle on the target crosses no other target and no
    // other undersized target's circle.
    const spaced = (t) => {
      const [x, y] = center(t);
      return !targets.some((o) => {
        if (o === t || apart(o, t)) return false;
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
    const sized = new Set(targets.filter((t) => !small.has(t)).map((t) => t.el));
    for (const el of beneath) {
      const r = area(el);
      if (r.width >= least - .5 && r.height >= least - .5) sized.add(el);
    }
    // A table's box is its region, whatever role it carries.
    const region = (el) => el.closest(`td, th, ${scroll}, section, [role=region]`);
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


OVERLAPS = (
    "(floating) => {"
    + DESCRIBE
    + """
  // What a reader sees of an item is what it draws: its words and the boxes
  // of what it holds to press or fill in, else its own box, so two items
  // whose boxes cross are judged by what each draws there. A dialog or a
  // popover it holds is drawn in the top layer, over the page by design, so
  // none of what that shows is the item's.
  const pressed = 'img, svg, canvas, video, iframe, input, select, textarea, button, a.btn, .pill';
  const own = (el, part) => { const layer = part.closest(floating); return !layer || layer === el || !el.contains(layer); };
  const drawn = (el) => {
    const rects = [];
    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      if (!node.textContent.trim() || !node.parentElement.checkVisibility() || !own(el, node.parentElement)) continue;
      const range = document.createRange();
      range.selectNodeContents(node);
      rects.push(...range.getClientRects());
    }
    for (const part of el.querySelectorAll(pressed)) if (part.checkVisibility() && own(el, part)) rects.push(part.getBoundingClientRect());
    return rects.length ? rects : [el.getBoundingClientRect()];
  };
  const cross = (ra, rb) => [
    Math.min(ra.right, rb.right) - Math.max(ra.left, rb.left),
    Math.min(ra.bottom, rb.bottom) - Math.max(ra.top, rb.top),
  ];
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
        const [x, y] = cross(ra, rb);
        if (x <= 1 || y <= 1) continue;
        const inA = drawn(a);
        const inB = drawn(b);
        const worst = inA.flatMap((da) => inB.map((db) => cross(da, db))).filter(([dx, dy]) => dx > 1 && dy > 1)
          .sort((m, n) => n[0] * n[1] - m[0] * m[1])[0];
        if (worst) found.push(`${describe(a)} overlaps ${describe(b)} by ${Math.round(worst[0])}x${Math.round(worst[1])}px`);
      }
    }
  }
  return found;
}"""
)


# A box in a row of boxes (a figure's tile, a card in a grid) keeps what it
# holds to itself: something it holds that is drawn past its edge, onto a box
# beside it, covers that box's words. OVERLAPS compares the boxes; this
# compares what each holds with the boxes beside it. Something placed out of
# flow (a tip, a hit area, a menu) is drawn over the page by design, and what
# a box clips is not drawn at all.
INTO_A_NEIGHBOR = (
    "() => {"
    + DESCRIBE
    + PAINTED_PART
    + """
  const found = [];
  const placed = (el, item) => {
    for (let up = el; up && up !== item; up = up.parentElement) {
      if (['absolute', 'fixed'].includes(getComputedStyle(up).position)) return true;
    }
    return false;
  };
  for (const parent of document.querySelectorAll('main *')) {
    if (!/grid|flex/.test(getComputedStyle(parent).display)) continue;
    const items = inFlow(parent).filter(([, style]) => style.position !== 'sticky');
    if (items.length < 2) continue;
    for (const [item, , held] of items) {
      for (const el of item.querySelectorAll('*')) {
        const r = box(el);
        if (r.width < 2 || r.height < 2) continue;
        if (r.left >= held.left - 1 && r.right <= held.right + 1 && r.top >= held.top - 1 && r.bottom <= held.bottom + 1) continue;
        if (placed(el, item)) continue;
        const drawn = painted(el, r);
        if (!drawn) continue;
        for (const [other, , beside] of items) {
          if (other === item) continue;
          const x = Math.min(drawn.right, beside.right) - Math.max(drawn.left, beside.left);
          const y = Math.min(drawn.bottom, beside.bottom) - Math.max(drawn.top, beside.top);
          if (x > 1 && y > 1) found.push(`${describe(el)} in ${describe(item)} is drawn over ${describe(other)} by ${Math.round(x)}x${Math.round(y)}px`);
        }
      }
    }
  }
  return [...new Set(found)];
}"""
)


# A table's frame spans the box it sits in, as its heading does: a frame cut to
# its columns leaves the heading's count and the page's edge beyond its end.
NARROW_FRAMES = (
    "(scroll) => {"
    + DESCRIBE
    + """
  const found = [];
  let read = 0;
  for (const el of document.querySelectorAll('main ' + scroll)) {
    if (!el.checkVisibility()) continue;
    read += 1;
    const parent = el.parentElement;
    const style = getComputedStyle(parent);
    const room = parent.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
    const width = box(el).width;
    if (width < room - 2) found.push(`${describe(el)} in ${describe(parent)}: ${Math.round(width)}px of ${Math.round(room)}px`);
  }
  return {read, found};
}"""
)


# The map scrolls with the page, and draws no lane with nothing in it.
MAP = """([map, lane, node]) => {
  const box = document.querySelector(map);
  if (!box) return {read: 0, found: []};
  const lanes = [...box.querySelectorAll(lane)].filter((el) => el.checkVisibility());
  const found = lanes
    .filter((el) => !el.querySelector(node))
    .map((el) => `an empty lane: ${el.textContent.trim().slice(0, 30)}`);
  if (['auto', 'scroll'].includes(getComputedStyle(box).overflowY)) found.unshift('the map scrolls inside itself');
  return {read: lanes.length, found};
}"""

# The insets a sticky part sits under are the stylesheet's, never measured by a
# script, so each must be as tall as the chrome it stands for: the header, and
# a page's section nav where there is one.
CHROME_INSETS = """() => {
  const inset = (value) => {
    const probe = document.createElement('div');
    probe.style.cssText = `position: absolute; visibility: hidden; block-size: ${value}`;
    document.body.append(probe);
    const size = probe.getBoundingClientRect().height;
    probe.remove();
    return size;
  };
  const found = [];
  for (const [selector, token] of [['.site-header', '--site-header-height'], ['.page-nav', '--page-nav-height']]) {
    const drawn = document.querySelector(selector)?.getBoundingClientRect().height || 0;
    const said = inset(`var(${token})`);
    if (Math.abs(drawn - said) > 0.5) found.push(`${token} is ${said}px, ${selector} is ${drawn}px`);
  }
  return found;
}"""


# A focus ring drawn whole: each control a reader can reach, focused, rings
# outside its box (an outline, or a ring a box shadow spreads) as far as its
# style says, and every box above it that clips what it holds (overflow,
# `clip-path`) leaves that ring whole wherever the control itself is shown
# whole. A hover ring is the same ring, so this reads both. A control cut by
# what holds it is another check's; one scrolled out of its box is not seen.
# The ring's reach is read from the control's own style while it is focused,
# so a ring drawn inside its box reaches nowhere and is never cut. Only a
# control a box clips near its edge is focused, the rest of the page is read
# once, and focus is put back where it was.
CUT_RINGS = (
    "(selector) => {"
    + DESCRIBE
    + PAINTED_PART
    + r"""
  const found = [];
  // Past this a ring would be drawn into its neighbors: no style reaches it.
  const FAR = 8;
  const grown = (r, by) => ({left: r.left - by, top: r.top - by, right: r.right + by, bottom: r.bottom + by});
  const cutBy = (whole, shown) => ['left', 'top', 'right', 'bottom'].filter((side) =>
    shown === null || Math.abs(shown[side] - whole[side]) > 0.5);
  // A shadow around the box, not cast to one side: how far it spreads.
  const spread = (shadow) => Math.max(0, ...shadow.split(/,(?![^(]*\))/)
    .filter((one) => !/\binset\b/.test(one))
    .map((one) => (one.replace(/[a-z-]+\([^)]*\)/g, '').match(/-?[\d.]+px/g) || []).map(parseFloat))
    .filter(([x = 0, y = 0]) => x === 0 && y === 0)
    .map(([, , blur = 0, size = 0]) => size + blur));
  const reach = (style) => Math.max(
    style.outlineStyle === 'none' ? 0 : parseFloat(style.outlineOffset) + parseFloat(style.outlineWidth),
    style.boxShadow === 'none' ? 0 : spread(style.boxShadow));
  const near = [];
  for (const el of document.querySelectorAll(selector)) {
    if (!el.checkVisibility({visibilityProperty: true})) continue;
    const r = box(el);
    if (r.width < 1 || r.height < 1) continue;
    const shown = painted(el, r);
    if (!shown || cutBy(r, shown).length) continue;
    const room = grown(r, FAR);
    if (cutBy(room, painted(el, room, true)).length) near.push([el, r]);
  }
  const was = document.activeElement;
  for (const [el, r] of near) {
    el.focus({preventScroll: true});
    if (document.activeElement !== el) continue;
    const by = reach(getComputedStyle(el));
    if (by <= 0) continue;
    const ring = grown(r, by);
    const sides = cutBy(ring, painted(el, ring, true));
    if (!sides.length) continue;
    let clips = null;
    for (let up = el.parentElement; up && !clips; up = up.parentElement) {
      const style = getComputedStyle(up);
      if (style.overflowX !== 'visible' || style.overflowY !== 'visible' || style.clipPath !== 'none') {
        const b = up.getBoundingClientRect();
        const inner = {left: b.left + up.clientLeft, top: b.top + up.clientTop};
        const edge = {...inner, right: inner.left + up.clientWidth, bottom: inner.top + up.clientHeight};
        if (style.clipPath !== 'none' || sides.some((side) => (side === 'left' || side === 'top') ? ring[side] < edge[side] - 0.5 : ring[side] > edge[side] + 0.5)) clips = up;
      }
    }
    found.push(`${describe(el)}: its ${by}px ring is cut at its ${sides.join(', ')} by ${clips ? describe(clips) : 'a box above it'}`);
  }
  if (was && was !== document.body) was.focus({preventScroll: true});
  else document.activeElement?.blur();
  return [...new Set(found)];
}"""
)


def _link(name, place, size="font-size: 8px; line-height: 8px", more=""):
    """A link drawn as an ``x``, placed in the window at ``place`` (top, left)."""

    top, left = place
    return f'<a href="#" class="{name}"{more} style="position: fixed; top: {top}; left: {left}; {size}">x</a>'


def _cover(top, width, height):
    """A box drawn over what lies under it, as an open menu's panel is."""

    return (
        f'<div style="position: fixed; top: {top}; left: 0; width: {width}; height: {height}; '
        'z-index: 10; background: white"></div>'
    )


# A popover's foot laid in the 3px a heading's ``::after`` hangs above it:
# 1px above the heading's box.
_LAY_THE_POPOVER = """() => {
  const popover = document.querySelector('main [popover]');
  popover.showPopover();
  const foot = () => popover.querySelector('.example-cover').getBoundingClientRect().bottom
    - document.querySelector('main .example-beneath').getBoundingClientRect().top;
  popover.style.setProperty('top', `${200 - foot() - 1}px`);
}"""
_POPOVER_FOOT = """() => {
  const foot = document.querySelector('main .example-cover').getBoundingClientRect().bottom
    - document.querySelector('main .example-beneath').getBoundingClientRect().top;
  return foot === -1 ? [] : [`the popover's foot is ${foot}px from the heading, not -1px`];
}"""

# The panel an item holds, shown over the button beside it.
_SHOW_THE_HELD_PANEL = """() => {
  const item = document.querySelector('main .example-held');
  const at = item.nextElementSibling.getBoundingClientRect();
  const panel = item.querySelector('[popover]');
  panel.style.cssText = `margin: 0; inset: auto; left: ${at.left}px; top: ${at.top}px`;
  panel.showPopover();
}"""
_HELD_OVER_ITS_NEIGHBOR = """() => {
  const item = document.querySelector('main .example-held');
  const panel = item.querySelector('[popover]').getBoundingClientRect();
  const at = item.nextElementSibling.getBoundingClientRect();
  const over = panel.left < at.right && panel.right > at.left && panel.top < at.bottom && panel.bottom > at.top;
  return over ? [] : ['the held panel is not shown over its neighbor'];
}"""

_GRID_ITEM = (
    '<div style="display: grid; grid-template-columns: 200px 200px; inline-size: 400px">'
    '<div class="{}" style="grid-area: 1 / 1 / 2 / 3; align-self: start; margin: 0; white-space: nowrap">{}</div>'
    '<button style="grid-area: 1 / 2 / 2 / 3; align-self: start; margin: 0">Approve</button></div>'
)


# The survey checks these probes answer (`probes/__init__.py`).
CHECKS = {
    "sideways": Check(
        (SIDEWAYS, SELECTORS["sideways"]), reader_sees="content past the page's edge or clipped out of its box"
    ),
    "overlaps": Check(
        (OVERLAPS, SELECTORS["floating"]),
        reader_sees="one box drawn over another",
        # A grid item laid across the track of the item beside it (a phone
        # row's name cell under its buttons) is judged by what it draws: a
        # popover it holds is drawn in the top layer, so shown over the
        # neighbor it is no overlap, while the item's own words run under the
        # neighbor are.
        plants=(
            Plant(
                _GRID_ITEM.format("example-held", 'A name<div popover="manual">Why it needs you</div>')
                + _GRID_ITEM.format("example-spilled", "A name long enough to run on under the button"),
                setup=_SHOW_THE_HELD_PANEL,
                premise=(_HELD_OVER_ITS_NEIGHBOR, None),
                finds=("div.example-spilled overlaps button",),
            ),
        ),
    ),
    "into a neighbor": Check(INTO_A_NEIGHBOR, reader_sees="what a box holds drawn over the box beside it"),
    "small targets": Check(
        (SMALL_TARGETS, [SELECTORS["target"], TARGET_MIN, SELECTORS["floating"], SELECTORS["table_scroll"]]),
        reader_sees="a control too small to hit",
        # Each pair of small targets side by side, so neither is spaced clear
        # of the other.
        plants=(
            # A target drawn under an open menu is out of a pointer's reach;
            # the same target in view still fails.
            Plant(
                "".join(
                    _link(name, (top, left))
                    for name, top in (("example-hidden", "300px"), ("example-shown", "500px"))
                    for left in ("40px", "50px")
                )
                + _cover("260px", "200px", "100px"),
                finds=("a.example-shown",),
            ),
            # A target the size of one, covered by a menu, is that size again
            # once the menu is dismissed, so a small target that names it as
            # doing the same job passes; one naming a small stand-in fails.
            Plant(
                '<section><a href="#" id="example-stand-in" class="example-stand-in" style="display: block; '
                'width: 30px; height: 30px; position: fixed; top: 300px; left: 40px">x</a>'
                '<a href="#" id="example-small-stand-in" class="example-stand-in" style="display: block; '
                'width: 10px; height: 10px; position: fixed; top: 300px; left: 300px">x</a>'
                + "".join(
                    _link(
                        f"example-named-{size}",
                        ("400px", f"{left + step}px"),
                        "display: block; width: 8px; height: 8px",
                        f' data-target-equivalent="{named}"',
                    )
                    for named, size, left in (
                        ("example-stand-in", "sized", 40),
                        ("example-small-stand-in", "small", 300),
                    )
                    for step in (0, 10)
                )
                + "</section>"
                + _cover("285px", "400px", "60px"),
                finds=("a.example-named-small",),
            ),
            # A popover's target reaching over the area a page target hangs
            # beside its box leaves that area the page target's: the two lie
            # in different layers. The popover's own small targets still fail.
            Plant(
                '<a href="#" class="subtle-link example-beneath" style="position: fixed; top: 300px; left: 40px; '
                'font-size: 12px; line-height: 18px">Example heading</a>'
                '<div popover="manual" style="inset: auto; margin: 0; padding: 0; position: fixed; top: 200px; '
                'left: 40px"><div style="position: absolute; top: 8px; left: 0">'
                + "".join(
                    f'<a href="#" class="example-within" style="position: absolute; left: {left}; font-size: 8px; '
                    'line-height: 8px">x</a>'
                    for left in ("0", "10px")
                )
                + '</div><button type="button" class="example-cover" style="display: block; box-sizing: border-box; '
                'width: 200px; height: 30px; margin: 40px 0 0">Example cover</button></div>',
                setup=_LAY_THE_POPOVER,
                premise=(_POPOVER_FOOT, None),
                finds=("a.example-within",),
            ),
            # A modal dialog makes the rest of the document inert, so no
            # pointer reaches a target behind it; its own still fail.
            Plant(
                "".join(_link("example-beneath", ("8px", left)) for left in ("40px", "50px"))
                + "<dialog>"
                + "".join(_link("example-within", ("8px", left)) for left in ("40px", "50px"))
                + "</dialog>",
                setup="() => document.querySelector('main dialog').showModal()",
                finds=("a.example-within",),
            ),
        ),
    ),
    "cut rings": Check((CUT_RINGS, SELECTORS["target"]), reader_sees="a focus ring cut off by what holds its control"),
    "frames short of their box": Check(
        (NARROW_FRAMES, SELECTORS["table_scroll"]),
        reader_sees="a table's frame ending short of the box it heads",
        part=DATA_TABLE,
    ),
    "map": Check(
        (MAP, [SELECTORS["map"], SELECTORS["map_lane"], SELECTORS["map_node"]]),
        reader_sees="a map scrolling inside the page, or a lane with nothing in it",
        part=TOPOLOGY_MAP,
        # The stylesheet hides a lane with no node in it, so the plant is a
        # lane shown empty, as a sheet that lost that rule draws it.
        plants=(
            Plant(
                '<div class="topology-map"><div class="topology-lane" style="display: block">Example lane</div>'
                '<div class="topology-lane"><div class="topology-node">Example node</div></div></div>',
                finds=("an empty lane: Example lane",),
            ),
        ),
    ),
    "chrome insets": Check(CHROME_INSETS, reader_sees="a sticky part drawn under the header or the page's nav"),
}
