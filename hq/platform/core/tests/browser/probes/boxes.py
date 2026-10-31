"""Probes of boxes: what escapes the page, frames, overlaps, targets, the map and the chrome's insets."""

from hq.platform.core.tests.browser.probes.describe import DESCRIBE, PAINTED_PART

ESCAPES = (
    "() => {"
    + DESCRIBE
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


SIDEWAYS = (
    "(allowed) => {"
    + DESCRIBE
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
SMALL_TARGETS = (
    "([selector, least]) => {"
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
  try {
    const targets = [];
    for (const el of new Set([...document.querySelectorAll(selector)])) {
      if (el.matches(':disabled') || inert(el) || !el.checkVisibility({visibilityProperty: true})) continue;
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


# A note the page says beside something (a live region: an ask's answer, the
# readings a page asked for, a job's note) stands in the page's flow, so it
# covers nothing and its line is held: placed out of flow, by itself or a box
# around it, it is drawn over the page. What a reader opened (a menu, a
# dialog) is drawn over the page by design, and so is its note; one hidden
# for sight (`visually-hidden`) has no box to cover anything with.
NOTES_OUT_OF_FLOW = (
    "([notes, floating]) => {"
    + DESCRIBE
    + """
  const found = [];
  for (const note of document.querySelectorAll(notes)) {
    const r = box(note);
    if (r.width < 2 || r.height < 2 || !note.checkVisibility()) continue;
    for (let up = note; up && up !== document.body && !up.matches(floating); up = up.parentElement) {
      const {position} = getComputedStyle(up);
      if (position !== 'absolute' && position !== 'fixed') continue;
      found.push(`${describe(note)} is drawn over the page (${describe(up)} is ${position})`);
      break;
    }
  }
  return found;
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
