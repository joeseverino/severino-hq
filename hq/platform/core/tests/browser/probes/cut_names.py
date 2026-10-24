"""Probes of a name cut to fit: where it is cut, split or clamped, and whether it had the room."""

from hq.platform.core.tests.browser.probes.describe import DESCRIBE, LINES, PAINTED_PART

CUT_NAMES = (
    "([region, name, soleToken, context, wide]) => {"
    + DESCRIBE
    + """
  const found = [];
  for (const scroll of document.querySelectorAll('main ' + region)) {
    if (scroll.clientWidth < wide) continue;
    for (const el of scroll.querySelectorAll(name)) {
      if (!el.checkVisibility() || el.closest(context)) continue;
      const token = el.querySelector(soleToken);
      if (token.scrollWidth > token.clientWidth + 1 || token.scrollHeight > token.clientHeight + 1) {
        found.push(`${describe(el)} "${el.textContent.trim()}" is cut in a ${scroll.clientWidth}px region`);
      }
    }
  }
  return found;
}"""
)

# A name of plain parts there that fits within the length it holds out for
# (the table's --name-hold, read in the name's own font) is on one line:
# "hq.example." over "com" means a column that could wrap gave nothing. A name
# with a hyphenated part holds out for that part, which the rule above holds.
SPLIT_NAMES = (
    "([region, name, context, wide, part]) => {"
    + DESCRIBE
    + LINES
    + """
  const found = [];
  for (const scroll of document.querySelectorAll('main ' + region)) {
    if (scroll.clientWidth < wide) continue;
    for (const el of scroll.querySelectorAll(name)) {
      if (!el.checkVisibility() || el.closest(context) || el.querySelector(part)) continue;
      if (!getComputedStyle(el).getPropertyValue('--name-hold').trim()) {
        found.push(`${describe(el)} has no --name-hold to hold out for`);
        continue;
      }
      const all = chars(el).filter((c) => c.top !== null);
      if (new Set(all.map((c) => c.top)).size < 2) continue;
      const held = [el, ...el.querySelectorAll('*')].map((box) => {
        const was = box.style.getPropertyValue('white-space');
        box.style.setProperty('white-space', 'nowrap', 'important');
        return () => (was ? box.style.setProperty('white-space', was) : box.style.removeProperty('white-space'));
      });
      range.selectNodeContents(el);
      const width = range.getBoundingClientRect().width;
      held.forEach((release) => release());
      const probe = el.appendChild(document.createElement('span'));
      probe.style.cssText = 'display: block; width: var(--name-hold)';
      const cap = probe.getBoundingClientRect().width;
      probe.remove();
      if (width <= cap) {
        found.push(`${describe(el)} "${el.textContent.trim()}" (${width.toFixed(1)}px, holds out for ${cap.toFixed(1)}px) `
          + `is split in a ${scroll.clientWidth}px region`);
      }
    }
  }
  return found;
}"""
)

# A value a cell cuts with an ellipsis is cut only while its table has no room
# for two more characters of it. Room is width no content uses: the table
# narrower than its box, or a column wider than its widest line, its header's
# included. Space a wrapping cell leaves at the end of a shorter line is not
# room: the cell's widest line still needs its column. What a cell draws is
# its text, its inline boxes (a link's arrow, a chip's padding) and its atomic
# ones (a control, an icon, a floor a value holds out for), each as far as the
# boxes above it let it show. A name in a context line (`.muted`: "on
# example-host") is not judged: the line says what the row shares with its
# neighbors, asks its column for nothing, and is cut at the column's edge by
# design (figures.css), so the row's own values get the room first.
# A box that holds out for a few characters of a value shorter than that:
# the floor pads it past its last character, a gulf before the words after it
# on its line ("app-host        · another-host").
PADDED_CUTS = (
    "(selector) => {"
    + DESCRIBE
    + r"""
  const found = [];
  const range = document.createRange();
  // What ::after draws after the value (a link's arrow), which no range or
  // child holds: its text, measured in its own font.
  const pen = document.createElement('canvas').getContext('2d');
  const generated = (el) => {
    const after = getComputedStyle(el, '::after');
    const text = after.content.match(/^"(.*)"$/s);
    if (!text || after.display === 'none' || after.position === 'absolute') return 0;
    pen.font = after.font;
    return pen.measureText(text[1]).width;
  };
  for (const el of document.querySelectorAll(selector)) {
    const style = getComputedStyle(el);
    if (!/grid/.test(style.display) || !el.firstElementChild || !el.checkVisibility()) continue;
    // Its words, and the boxes it holds (a chip's padding is the chip's).
    range.selectNodeContents(el);
    const value = Math.max(range.getBoundingClientRect().right, ...[...el.children].map((child) => child.getBoundingClientRect().right))
      + generated(el);
    const end = el.getBoundingClientRect().right - parseFloat(style.paddingRight) - parseFloat(style.borderRightWidth);
    const gap = end - value;
    if (gap > 2) found.push(`${describe(el)} "${el.textContent.trim()}": ${Math.round(gap)}px of floor past its end`);
  }
  return found;
}"""
)

CUT_WITH_ROOM = (
    "([region, openRow, context, phrasePart]) => {"
    + DESCRIBE
    + PAINTED_PART
    + r"""
  const found = [];
  const range = document.createRange();
  const atomic = (el) => /^inline-/.test(getComputedStyle(el).display)
    || ['IMG', 'svg', 'INPUT', 'SELECT', 'TEXTAREA', 'BUTTON', 'CANVAS'].includes(el.tagName);
  const drawn = (cell) => {
    const rects = [];
    const walker = document.createTreeWalker(cell, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT);
    // An atomic box is drawn whole; what it holds lies inside it.
    let whole = null;
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      if (whole && whole.contains(node)) continue;
      let own, holder;
      if (node.nodeType === Node.TEXT_NODE) {
        if (!node.data.trim()) continue;
        range.selectNodeContents(node);
        own = [...range.getClientRects()];
        holder = node.parentElement;
      } else {
        const display = getComputedStyle(node).display;
        if (display === 'inline') own = [...node.getClientRects()];
        else if (atomic(node)) { own = [node.getBoundingClientRect()]; whole = node; }
        else continue;
        holder = node;
      }
      for (const rect of own) {
        const seen = rect.width > 0 && painted(holder, rect);
        if (seen) rects.push(seen);
      }
    }
    return rects;
  };
  // Whether any line of the cell goes on to a second: its words and its
  // atomic boxes (a chip) are measured line by line, grouped by the block
  // they lay out in. Words in an element with no box of its own (`display:
  // contents`: a token's parts in code) are shown by the box around it.
  const wraps = (cell) => {
    const lines = new Map();
    const blockOf = (el) => {
      let block = el;
      while (block !== cell && ['inline', 'contents'].includes(getComputedStyle(block).display)) block = block.parentElement;
      return block;
    };
    const boxed = (el) => {
      while (el !== cell && getComputedStyle(el).display === 'contents') el = el.parentElement;
      return el;
    };
    const add = (block, rects) => lines.set(block, [...(lines.get(block) || []), ...rects.filter((r) => r.width > 0)]);
    const walker = document.createTreeWalker(cell, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      if (node.nodeType === Node.ELEMENT_NODE) {
        if (atomic(node) && node.checkVisibility()) add(blockOf(node.parentElement), [node.getBoundingClientRect()]);
        continue;
      }
      if (!node.data.trim() || !boxed(node.parentElement).checkVisibility()) continue;
      range.selectNodeContents(node);
      add(blockOf(node.parentElement), [...range.getClientRects()]);
    }
    return [...lines.values()].some((rects) => rects.some((r) => rects.some((o) => o.top >= r.bottom - 1)));
  };
  // The width of the cell's content box its widest line leaves unused: none
  // in a cell that wraps, whose next word did not fit; null when the cell
  // draws nothing.
  const edges = (cell) => {
    const style = getComputedStyle(cell), b = cell.getBoundingClientRect();
    return [b.left + parseFloat(style.borderLeftWidth) + parseFloat(style.paddingLeft),
      b.right - parseFloat(style.borderRightWidth) - parseFloat(style.paddingRight)];
  };
  const inside = (cell) => { const [left, right] = edges(cell); return right - left; };
  const spare = (cell) => {
    const rects = drawn(cell);
    if (!rects.length) return null;
    if (wraps(cell)) return 0;
    const [left, right] = edges(cell);
    const from = Math.max(left, Math.min(...rects.map((r) => r.left)));
    const to = Math.min(right, Math.max(...rects.map((r) => r.right)));
    return Math.max(0, right - left - Math.max(0, to - from));
  };
  // How far the value runs past the box that cuts it, measured on its words:
  // scrollWidth and clientWidth are rounded to whole pixels, and a value
  // past its box by a fraction of one still loses its last character to the
  // ellipsis.
  const wordsOf = (el) => { range.selectNodeContents(el); return range.getBoundingClientRect().width; };
  const cutBy = (el) => {
    const style = getComputedStyle(el);
    const words = wordsOf(el);
    const inner = el.getBoundingClientRect().width - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight)
      - parseFloat(style.borderLeftWidth) - parseFloat(style.borderRightWidth);
    return Math.max(el.scrollWidth - el.clientWidth - 1, words - inner);
  };
  // A heading's own words, without its sort glyph.
  const label = (th) => [...th.querySelectorAll('[aria-hidden="true"]')]
    .reduce((words, el) => words.replace(el.textContent, ''), th.textContent).trim();
  for (const scroll of document.querySelectorAll('main ' + region)) {
    const table = scroll.querySelector(':scope > table');
    if (!table || !table.checkVisibility()) continue;
    const slack = scroll.clientWidth - table.getBoundingClientRect().width;
    if (slack < -1) continue;
    const cut = [...table.querySelectorAll(':scope > tbody > tr > td:not([colspan]) *')].filter((el) =>
      !el.closest(openRow) && !el.closest(context) && el.checkVisibility() && el.clientWidth > 0
      && getComputedStyle(el).textOverflow === 'ellipsis' && cutBy(el) > 0.05);
    const innermost = cut.filter((el) => !cut.some((other) => other !== el && el.contains(other)));
    if (!innermost.length) continue;
    const columns = new Map();
    for (const row of table.querySelectorAll(':scope > thead > tr, :scope > tbody > tr')) {
      if (row.querySelector(':scope > [colspan]')) continue;
      [...row.children].forEach((cell, at) => {
        if (!cell.checkVisibility()) return;
        const unused = spare(cell);
        if (unused === null) return;
        const column = columns.get(at);
        if (!column) columns.set(at, {name: (table.tHead && label(table.tHead.rows[0].cells[at] || table.tHead)) || `column ${at + 1}`, unused});
        else column.unused = Math.min(column.unused, unused);
      });
    }
    const unused = [...columns.values()].filter((column) => column.unused >= 1);
    const room = Math.max(0, slack) + unused.reduce((sum, column) => sum + column.unused, 0);
    for (const el of innermost) {
      const text = el.textContent.trim();
      // A part of a phrase held to one line is cut with room when its cell's
      // line, less what the boxes around it (a chip) keep for their own
      // edges, would hold its words whole, measured as finely as they are
      // drawn: it was held short of its cell, not by it.
      // A box between that cuts (a control's label, cut as one line) bounds
      // that line at its own. A name alone is held by the rules for names
      // above, and a line clamped to a preview is cut at its end by design.
      const clamped = (cell) => {
        for (let box = el.parentElement; box && box !== cell; box = box.parentElement) {
          if (getComputedStyle(box).webkitLineClamp !== 'none') return true;
        }
        return false;
      };
      const line = (cell) => {
        let width = inside(cell);
        for (let box = el.parentElement; box && box !== cell; box = box.parentElement) {
          const style = getComputedStyle(box);
          if (style.display === 'contents') continue;
          const padding = parseFloat(style.paddingLeft) + parseFloat(style.paddingRight);
          width -= padding + parseFloat(style.borderLeftWidth) + parseFloat(style.borderRightWidth)
            + parseFloat(style.marginLeft) + parseFloat(style.marginRight);
          if (style.overflowX !== 'visible') width = Math.min(width, box.clientWidth - padding);
        }
        return width;
      };
      const cell = el.closest('td');
      if (el.matches(phrasePart) && !clamped(cell) && getComputedStyle(el).whiteSpace === 'nowrap' && wordsOf(el) <= line(cell)) {
        found.push(`${describe(el)} "${text.slice(0, 40)}" is cut by ${cutBy(el).toFixed(1)}px `
          + `while its line in its cell holds ${Math.round(line(cell))}px`);
        continue;
      }
      const grow = 2 * el.scrollWidth / Math.max(text.length, 1);
      if (room < grow) continue;
      const where = unused.map((column) => `${column.name} ${Math.round(column.unused)}px`);
      if (slack >= 1) where.push(`the table ${Math.round(slack)}px`);
      found.push(`${describe(el)} "${text.slice(0, 40)}" is cut by ${cutBy(el).toFixed(1)}px `
        + `while its table leaves ${Math.round(room)}px unused (${where.join(', ')}; ${Math.round(scroll.clientWidth)}px region)`);
    }
  }
  return found;
}"""
)

# A heading's aside sits beside it on one line, or starts on a line of its
# own under it: wrapped beside the heading, its lines keep the heading's line
# box and read as a loose paragraph set in the heading.
# What a heading says beside it, wrapped, leaves no word alone on its last
# line: a word that could share it reads as cut from the line above.
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
