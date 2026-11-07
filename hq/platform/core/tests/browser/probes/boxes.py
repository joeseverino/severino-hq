"""Probes of boxes: what escapes the page, frames, overlaps, targets, the map and the chrome's insets."""

from hq.platform.core.tests.browser.probes.describe import DESCRIBE, PAINTED_PART
from hq.platform.core.tests.browser.selectors import SELECTORS

# Nothing moves sideways except inside a box ``allowed`` selects: past the
# page's edge it is cut off, and a box that clips or scrolls what it holds
# without being allowed to hides it. A chip row, a lane or a code block
# scrolled past the edge of a phone is content nobody finds; a name cut off
# with an ellipsis is a deliberate truncation. ``ANY_SCROLLER`` allows every
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
  for (const el of document.querySelectorAll('body *')) {
    const r = box(el);
    if (r.width < 2 || r.height < 2 || !el.checkVisibility() || held(el)) continue;
    const style = getComputedStyle(el);
    const boxed = ['auto', 'scroll', 'hidden', 'clip'].includes(style.overflowX);
    if (boxed && !allows(el) && el.scrollWidth > el.clientWidth + 1 && style.textOverflow !== 'ellipsis') {
      found.push(`${describe(el)} holds ${el.scrollWidth}px in ${el.clientWidth}px (overflow-x: ${style.overflowX})`);
    } else if (r.right > width + 1 || r.left < -1) {
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
    "([selector, least, floating]) => {"
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
    const region = (el) => el.closest('td, th, """
    + SELECTORS["table_scroll"]
    + """, section, [role=region]');
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
    "() => {"
    + DESCRIBE
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


# A queue item's words (``texts``) fill the column they sit in, and that column
# runs to the item's buttons (``ends``) wherever the two share a line, so no
# empty band stands between what an item says and what can be pressed. The
# column may stand off its buttons by its row's gap and no more.
SHORT_OF_ITS_BUTTONS = (
    "([texts, ends]) => {"
    + DESCRIBE
    + """
  const found = [];
  const edge = (el) => { const s = getComputedStyle(el); return box(el).right - parseFloat(s.paddingRight) - parseFloat(s.borderRightWidth); };
  for (const el of document.querySelectorAll('main ' + texts)) {
    if (!el.checkVisibility()) continue;
    const column = el.parentElement;
    const short = edge(column) - box(el).right;
    if (short > 2) found.push(`${describe(el)} stops ${Math.round(short)}px short of the edge of ${describe(column)}`);
    const end = [...column.parentElement.children].find((sibling) => sibling.matches(ends));
    if (!end || !end.checkVisibility()) continue;
    const [at, by] = [box(column), box(end)];
    if (by.left < at.right - 1 || by.top >= at.bottom || by.bottom <= at.top) continue;
    const gap = parseFloat(getComputedStyle(column.parentElement).columnGap) || 0;
    const band = by.left - at.right;
    if (band > gap + 2) found.push(`${describe(column)} stops ${Math.round(band - gap)}px short of its buttons`);
  }
  return [...new Set(found)];
}"""
)


# A section's heading that follows other content in its box stands at least as
# far from what is above it as from what it heads, so it reads as the start of
# what comes next rather than a caption of what came before.
CRAMPED_HEADS = (
    "(selector) => {"
    + DESCRIBE
    + """
  const found = [];
  for (const head of document.querySelectorAll('main ' + selector)) {
    if (!head.checkVisibility() || !head.parentElement) continue;
    const flow = inFlow(head.parentElement);
    const at = flow.findIndex(([el]) => el === head);
    if (at < 1 || at + 1 >= flow.length) continue;
    const [before, , above] = flow[at - 1];
    const under = flow[at + 1][2];
    const r = box(head);
    // Only a stack: a neighbor beside the heading is a column, not a section.
    if (above.bottom > r.top + 1 || under.top < r.bottom - 1) continue;
    const over = Math.round(r.top - above.bottom);
    const heads = Math.round(under.top - r.bottom);
    if (over < heads) {
      const words = head.textContent.trim().split(/\\s+/).slice(0, 4).join(' ');
      found.push(`${describe(head)} "${words}" is ${over}px under ${describe(before)}, ${heads}px over what it heads`);
    }
  }
  return found;
}"""
)


# What sits beside a section's heading keeps to one line while its row has
# room for that line: a part of the head other than its heading that takes
# more lines than it does with its wrapping held, when its one-line width fits
# beside what shares its row with a pixel to spare (a line that fills its
# room to a fraction of a pixel is as likely to wrap in one engine as not). The heading and its line under it give up room
# down to their least width, which is what they keep beside it.
ASIDES_WRAPPED_WITH_ROOM = (
    "(selector) => {"
    + DESCRIBE
    + """
  const held = (el, css, read) => {
    const before = el.style.cssText;
    el.style.cssText += css;
    const value = read();
    el.style.cssText = before;
    return value;
  };
  const extent = (el) => {
    const range = document.createRange();
    range.selectNodeContents(el);
    return range.getBoundingClientRect().width;
  };
  const isHeading = (el) => el.matches('h1, h2, h3, h4, h5, h6, hgroup');
  const found = [];
  for (const head of document.querySelectorAll('main ' + selector)) {
    if (!head.checkVisibility()) continue;
    const style = getComputedStyle(head);
    const inner = head.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
    const gap = parseFloat(style.columnGap) || 0;
    const items = inFlow(head);
    for (const [part, , r] of items) {
      if (isHeading(part)) continue;
      const [oneLine, oneLineHeight] = held(part, ';white-space: nowrap !important',
        () => [extent(part), box(part).height]);
      if (r.height <= oneLineHeight + 1) continue;
      const beside = items.filter(([el, , o]) => el !== part && o.top < r.bottom && o.bottom > r.top);
      const taken = beside.reduce((sum, [el, s, o]) => sum + (isHeading(el)
        ? (s.minWidth === '0px' ? 0 : held(el, ';flex: none !important; inline-size: min-content !important', () => box(el).width))
        : o.width) + gap, 0);
      if (oneLine + 1 <= inner - taken) {
        const words = part.textContent.trim().replace(/\\s+/g, ' ').slice(0, 40);
        found.push(`${describe(part)} "${words}" takes ${Math.round(r.height / oneLineHeight)} lines, its one line ${oneLine.toFixed(2)}px with ${(inner - taken).toFixed(2)}px of room`);
      }
    }
  }
  return found;
}"""
)


# What a section's frame holds that draws its own edges, cells or fill
# reaches the frame's sides, as a table's region does, rather than drawing a
# box inside the box. A child of a section's body draws them when boxes it or
# its descendants lay out as blocks (a cell, a row, a band's fact, never a
# chip in a line of text) paint a border, a shadow or a fill at both of its
# sides; what they paint must start and end at the frame's inner edges. A box
# rounded at every corner (a code block, a callout) is a thing of its own the
# frame's padding holds, never a cell. A
# section in a section is part of the outer frame, so its body's children
# are measured against that frame.
INSET_IN_THE_FRAME = (
    "(selector) => {"
    + DESCRIBE
    + """
  const seen = (color) => color && !/^(transparent|rgba\\(.*,\\s*0\\))$/.test(color);
  const paints = (el, style) => seen(style.backgroundColor) || style.backgroundImage !== 'none'
    || style.boxShadow !== 'none'
    || ['Top', 'Right', 'Bottom', 'Left'].some((side) => parseFloat(style[`border${side}Width`]) > 0
      && style[`border${side}Style`] !== 'none' && seen(style[`border${side}Color`]));
  const rounded = (style) => ['TopLeft', 'TopRight', 'BottomRight', 'BottomLeft']
    .every((corner) => parseFloat(style[`border${corner}Radius`]) >= 2);
  const frameOf = (body) => {
    let frame = body;
    for (let up = body.parentElement?.closest(selector); up; up = up.parentElement?.closest(selector)) {
      if (parseFloat(getComputedStyle(frame).paddingLeft) > 0) break;
      frame = up;
    }
    return frame;
  };
  const found = [];
  for (const body of document.querySelectorAll('main ' + selector)) {
    if (!body.checkVisibility()) continue;
    const frame = frameOf(body);
    const edges = getComputedStyle(frame);
    const f = box(frame);
    const inner = {left: f.left + parseFloat(edges.borderLeftWidth), right: f.right - parseFloat(edges.borderRightWidth)};
    for (const [child, , c] of inFlow(body)) {
      if ([...child.children].some((inner) => inner.matches(selector))) continue;
      const painted = [child, ...child.querySelectorAll('*')].flatMap((el) => {
        const style = getComputedStyle(el);
        if (style.display.startsWith('inline') || style.display === 'contents' || !el.checkVisibility()) return [];
        if (!paints(el, style) || rounded(style)) return [];
        const r = box(el);
        return r.width >= 1 && r.height >= 1 ? [r] : [];
      });
      if (!painted.some((r) => r.left <= c.left + 1) || !painted.some((r) => r.right >= c.right - 1)) continue;
      const left = Math.min(...painted.map((r) => r.left));
      const right = Math.max(...painted.map((r) => r.right));
      const start = Math.round(left - inner.left);
      const end = Math.round(inner.right - right);
      if (start > 1 || end > 1) found.push(`${describe(child)} paints ${start}px and ${end}px inside its frame's sides`);
    }
  }
  return found;
}"""
)


# Sections that share a row start their frames together, whatever their heads
# hold: two heads followed by their frames, side by side with their tops on
# one line, are followed by frames whose tops are on one line too (a pixel
# apart is the same line in one engine or another). A section whose head stands
# inside its frame is framed by its landmark, the edge around head and body.
FRAMES_OUT_OF_ROW = (
    "([head, frame]) => {"
    + DESCRIBE
    + """
  const sections = [...document.querySelectorAll('main ' + head)].flatMap((h) => {
    const body = h.nextElementSibling;
    if (!h.checkVisibility() || !body?.matches(frame) || !body.checkVisibility()) return [];
    const enclosing = parseFloat(getComputedStyle(h.parentElement).borderTopWidth) > 0;
    return [[h, box(h), box(enclosing ? h.parentElement : body)]];
  });
  const found = [];
  sections.forEach(([a, ha, fa], at) => {
    for (const [b, hb, fb] of sections.slice(at + 1)) {
      if (Math.abs(ha.top - hb.top) > 1 || (ha.left < hb.right && hb.left < ha.right)) continue;
      const apart = Math.abs(fa.top - fb.top);
      if (apart > 1) {
        const named = (h) => `${describe(h.parentElement)} "${h.textContent.trim().split(/\\s+/).slice(0, 4).join(' ')}"`;
        found.push(`${named(a)} and ${named(b)} share a row, their frames ${apart.toFixed(1)}px apart`);
      }
    }
  });
  return found;
}"""
)


# The map scrolls with the page, and draws no lane with nothing in it.
MAP = """([map, lane, node]) => {
  const box = document.querySelector(map);
  if (!box) return null;
  return {
    scrolls: ['auto', 'scroll'].includes(getComputedStyle(box).overflowY),
    empty: [...box.querySelectorAll(lane)]
      .filter((el) => el.checkVisibility() && !el.querySelector(node))
      .map((el) => el.textContent.trim().slice(0, 30)),
  };
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


# A page's head (`partials/_page_head.html`): at most two controls beside the
# title, on one line with the menu, and every control offered once, beside the
# title or in the menu. A menu entry is read by its style rather than its
# visibility, since a closed menu shows none of them.
HEAD_CONTROLS = (
    "([actionsSelector, menuSelector, panelSelector, askSelector]) => {"
    + DESCRIBE
    + r"""
  const said = (el) => ((el.matches(askSelector) && el.querySelector('button')) || el).textContent.trim();
  const shown = (el) => getComputedStyle(el).display !== 'none';
  // A box drawn as its children alone (an ask) is seen when one of them is.
  const seen = (el) => el.checkVisibility() || (getComputedStyle(el).display === 'contents' && [...el.children].some(seen));
  const found = [];
  for (const actions of document.querySelectorAll(actionsSelector)) {
    if (!actions.checkVisibility()) continue;
    const name = describe(actions);
    const menu = actions.querySelector(`:scope > ${menuSelector}`);
    const beside = [...actions.children].filter((el) => el !== menu && seen(el));
    const listed = menu ? [...menu.querySelectorAll(`${panelSelector} > *`)].filter(shown) : [];
    if (beside.length > 2) found.push(`${name} draws ${beside.length} controls beside the title`);
    if (menu && menu.checkVisibility() !== listed.length > 0) found.push(`${name} draws a menu that holds ${listed.length} controls`);
    const besideWords = beside.map(said);
    const listedWords = listed.map(said);
    for (const words of listedWords.filter((words) => besideWords.includes(words))) {
      found.push(`${name} offers "${words}" beside the title and in its menu`);
    }
    const entries = menu ? [...menu.querySelectorAll(`${panelSelector} > *`)] : [];
    const offered = new Set([...actions.children].filter((el) => el !== menu).concat(entries).map(said));
    for (const words of offered) {
      if (!besideWords.includes(words) && !listedWords.includes(words)) found.push(`${name} hides "${words}" with no way to it`);
    }
    // An ask's control is its button: what it says takes the line under the row.
    const drawn = (el) => (el.matches(askSelector) ? [el.querySelector('button')] : [...el.children].filter(seen));
    const row = [...beside, ...(menu && menu.checkVisibility() ? [menu] : [])].flatMap((el) => [...el.getClientRects(), ...(el.getClientRects().length ? [] : drawn(el).map((child) => child.getBoundingClientRect()))]);
    if (row.some((r) => r.top >= row[0].bottom || r.bottom <= row[0].top)) found.push(`${name} wraps its controls onto two lines`);
  }
  return found;
}"""
)


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
