"""Real-browser layout checks: manage.py test core.browser_tests --parallel=1.

This filename does not match Django's default test*.py discovery, so the plain
suite needs neither Playwright nor a browser. `mise run browser` runs it, and
`mise run ci` always does.

Pages render through their real views over two synthetic example.* estates,
a sparse one (core/tests/test_browser_fixtures.py) and a dense, production-shaped
one (core/tests/test_browser_dense_fixtures.py), then load in a browser whose every request is
answered locally: nothing here can reach a real endpoint. Assertions are
invariants rather than pixels, so a page can change and still pass as long as
it stays readable.

Every CSS selector the checks use is in SELECTORS, and
core/tests/test_browser_selectors.py fails when one names nothing the templates
render. JavaScript below queries by tag only.
"""

import mimetypes
import os
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib.staticfiles import finders
from django.db import transaction
from django.test import SimpleTestCase

from hq.platform.core.tests.test_browser_dense_fixtures import DENSE_PAGES, build_dense_estate, render_dense_pages
from hq.platform.core.tests.test_browser_fixtures import PAGES, build_estate, render_pages

WIDTHS = (320, 390, 768, 1280)
# A phone, a tablet held upright, a laptop: where only a table may scroll sideways.
OVERFLOW_WIDTHS = (375, 820, 1360)
# Every page, from the sparse estate and the dense one. The overflow and
# density checks run all of them at OVERFLOW_WIDTHS.
ALL_PAGES = (*PAGES, *DENSE_PAGES)
# How tall a table row may be, in lines of its own body text. The tallest row
# the tables are designed for is a connection's: its name, endpoint, who holds
# its secret, where it reaches from, and a closed "Can see" disclosure, one
# line each, with room for the owner line to wrap once at a tablet: six. A
# name is clamped to two lines (.table-clamp, `[data-entity]` in a cell), so a
# row past six is a list put in a cell (a machine's every hostname, a
# container's every port) that belongs on the record's own page, with a count
# or the first few in the row. Half a line of slack absorbs the smaller type
# the secondary lines are set in.
ROW_LINES = 6
# The stylesheet's phone breakpoint: `@media (max-width: 640px)`.
PHONE = 640
ORIGIN = "http://hq.example.test"
AUDIT = Path(settings.BASE_DIR) / "scripts" / "layout-audit.js"

SELECTORS = {
    "decisions": "[data-attention-item]",
    "decision_family": "details[data-queue-family] > summary strong",
    "decision_title": ".attention-title",
    "decision_evidence": ".attention-evidence",
    "decision_summary": ".attention-evidence > summary",
    "decision_detail": ".attention-detail",
    "decision_actions": ".attention-actions",
    "highlights": ".dashboard-highlights > .highlight-card",
    # Each reading in the dashboard's row: a mark, then its lines.
    "readings": ".dash-readings .glance-panel-head",
    "patterns": ".dashboard-patterns > .card",
    "pill": ".pill",
    # The one box allowed to scroll sideways.
    "table_scroll": ".table-scroll",
    # Frames by design: a control, a chip, a path's steps, a diagram's nodes.
    "frame_exempt": ".btn, .pill, .request-path li, .topology-node, label",
    # Everything inside a menu or a dialog, which float over the page.
    "frame_exempt_within": "dialog, [data-menu]",
    # The lead control renders first, so it is the one a narrow head keeps.
    "head_lead": ".page-head .page-actions > :first-child",
    "head_title": ".page-head .page-title-row",
    # Where the contrast check samples text: the page and the header over it.
    "contrast_scope": "main, .site-header",
    # Filled boxes that are not tiles: controls, tables, code, charts, and
    # anything floating over the page.
    "tile_exempt": (
        ".btn, .pill, button, input, select, textarea, table, pre, code, svg, canvas,"
        " dialog, [data-menu]"
    ),
    # A control inside a tile is measured by its own box, not its text.
    "tile_control": ".btn, button",
    # A menu and the topology map lay out away from the box they sit in, so
    # what they hold is not measured against it.
    "tile_detached": "dialog, [data-menu], .topology-map",
    # A disclosure in the page, which the tile check opens to lay out.
    "disclosure": "main details:not([data-menu])",
    # A band's cells. Stats inside a card are the one band laid out with real
    # gaps instead of padded cells, so the KPI band is not listed.
    "band_cell": (
        ":is(.control-summary, .service-band, .fact-band, .finding-facts, .insight-grid,"
        " .command-preview-path, .connection-control-grid, .sweep-grid,"
        " .machine-telemetry-metrics) > *"
    ),
}
# WCAG AA: body text, and text large enough to need less (24px, or 18.66px bold).
TEXT_CONTRAST = 4.5
LARGE_TEXT_CONTRAST = 3.0

# Structural rules from scripts/layout-audit.js that hold on every page at
# every width. The audit's density heuristics (card-too-wide, empty-card-bottom,
# row-height-hole, horizontal-slack) judge proportions that depend on how much
# data a page has, so they stay a by-eye tool rather than a gate.
AUDIT_RULES = {
    "unwanted-scrollbar",
    "clipped-content",
    "uneven-pair",
    "uneven-controls",
    "dead-tooltip",
    "seam-mismatch",
    "sentence-in-a-chip-row",
}

# In-page probes. Each returns a list of findings; empty is the pass.
_DESCRIBE = """
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

_STYLESHEETS = """() => {
  const links = [...document.querySelectorAll('link[rel="stylesheet"]')];
  const loaded = [...document.styleSheets].filter((sheet) => sheet.href);
  return {links: links.length,
          empty: loaded.filter((sheet) => !sheet.cssRules.length).map((s) => s.href),
          loaded: loaded.length};
}"""

_ESCAPES = "() => {" + _DESCRIBE + """
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
  return found.slice(0, 10);
}"""

_NESTED_FRAMES = "([exemptSelector, withinSelector]) => {" + _DESCRIBE + r"""
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
  return found.slice(0, 10);
}"""

_HEAD_ON_ONE_LINE = """([leadSelector, titleSelector]) => {
  // A narrow head is one line: the title and its lead control, nothing
  // between them and the lede.
  const lead = document.querySelector(leadSelector);
  const title = document.querySelector(titleSelector);
  if (!lead || !title) return null;
  const a = lead.getBoundingClientRect(), t = title.getBoundingClientRect();
  return a.top < t.bottom && a.bottom > t.top;
}"""

_SIDEWAYS = "(allowed) => {" + _DESCRIBE + """
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
  return found.slice(0, 10);
}"""

_CELLS = "() => {" + _DESCRIBE + """
  // A cell's content stays in the cell. Inside a table that scrolls, running
  // past the edge is not caught by anything else: it lands under the next
  // column, readable in neither.
  const found = [];
  for (const cell of document.querySelectorAll('main td, main th')) {
    const edge = box(cell).right;
    for (const el of cell.querySelectorAll('*')) {
      const r = box(el);
      if (r.width < 2 || r.height < 2 || !el.checkVisibility()) continue;
      if (r.right > edge + 1) {
        found.push(`${describe(el)} runs ${Math.round(r.right - edge)}px past its cell: ${el.textContent.trim().slice(0, 40)}`);
      }
    }
  }
  return [...new Set(found)].slice(0, 10);
}"""

# A link that sits above or below the words it continues: an inline line in a
# smaller size than its cell aligns a clamped link to the cell's taller line.
_RAISED_LINKS = "() => {" + _DESCRIBE + """
  const found = [];
  const rects = (node) => {
    const range = document.createRange();
    range.selectNodeContents(node);
    return [...range.getClientRects()].filter((r) => r.width);
  };
  for (const link of document.querySelectorAll('main td [data-entity]')) {
    if (!link.checkVisibility() || !link.firstChild) continue;
    const before = link.previousSibling;
    if (!before || before.nodeType !== Node.TEXT_NODE || !before.textContent.trim()) continue;
    const words = rects(before).at(-1);
    const linked = rects(link.firstChild)[0];
    if (!words || !linked) continue;
    // Wrapped onto a line of its own: nothing beside it to sit off.
    if (linked.top >= words.bottom || linked.bottom <= words.top) continue;
    if (Math.abs(words.bottom - linked.bottom) > 1.5) {
      found.push(`${describe(link)} sits ${Math.round(words.bottom - linked.bottom)}px off the words before it`);
    }
  }
  return [...new Set(found)].slice(0, 10);
}"""

# A row has one disclosure. The row toggle shows what a row holds back; a
# dropdown inside the row beside it is a second, smaller way to do the same.
_NESTED_DISCLOSURES = "() => {" + _DESCRIBE + """
  return [...document.querySelectorAll('main table > tbody > tr')]
    .filter((row) => row.querySelector('button[aria-expanded]') && row.querySelector(':is(td, th) details'))
    .map((row) => `${describe(row)} has a row toggle and a dropdown`)
    .slice(0, 10);
}"""

# A plain word split across lines inside a table cell: its column was given
# less than the word, because another column's content took the width.
_BROKEN_WORDS = "() => {" + _DESCRIBE + """
  const found = [];
  const range = document.createRange();
  for (const cell of document.querySelectorAll('main td')) {
    const walker = document.createTreeWalker(cell, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      if (!node.parentElement.checkVisibility()) continue;
      for (const match of node.textContent.matchAll(/[A-Za-z]{4,}/g)) {
        range.setStart(node, match.index);
        range.setEnd(node, match.index + match[0].length);
        const lines = new Set([...range.getClientRects()].filter((r) => r.width).map((r) => Math.round(r.top)));
        if (lines.size > 1) found.push(`${describe(cell)} breaks "${match[0]}"`);
      }
    }
  }
  return [...new Set(found)].slice(0, 10);
}"""

_TABLES = "() => {" + _DESCRIBE + """
  const width = document.documentElement.clientWidth;
  const found = [];
  let seen = 0;
  for (const table of document.querySelectorAll('main table')) {
    const r = box(table);
    if (!r.width) continue;
    seen += 1;
    const name = describe(table);
    if (getComputedStyle(table).display !== 'table') found.push(`${name} is not laid out as a table`);
    const head = table.querySelector('thead');
    if (head && (getComputedStyle(head).display !== 'table-header-group'
                 || getComputedStyle(head).position === 'absolute')) {
      found.push(`${name} hides its column headings`);
    }
    for (const row of table.querySelectorAll('tbody tr')) {
      if (getComputedStyle(row).display !== 'table-row') {
        found.push(`${name} stacks its rows`); break;
      }
    }
    let scroller = table.parentElement;
    while (scroller && scroller !== document.body && !scrolls(scroller)) scroller = scroller.parentElement;
    if (!scroller || scroller === document.body) {
      const room = box(table.parentElement);
      if (r.right > room.right + 1) found.push(`${name} overflows with no scroll container of its own`);
      continue;
    }
    const s = box(scroller);
    if (s.right > width + 1 || s.left < -1) found.push(`${name} scrolls in a box wider than the page`);
  }
  return {seen, found};
}"""

_PAIRS = "() => {" + _DESCRIBE + """
  const found = [];
  for (const grid of document.querySelectorAll('main *')) {
    const style = getComputedStyle(grid);
    if (!['grid', 'inline-grid'].includes(style.display)) continue;
    if (!['normal', 'stretch'].includes(style.alignItems)) continue;
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

_WRAPPED_PILLS = "(selector) => {" + _DESCRIBE + """
  // A pill is one short state: no taller than the same pill holding one letter.
  const found = [];
  for (const pill of document.querySelectorAll(selector)) {
    if (!pill.getClientRects().length) continue;
    const probe = pill.cloneNode(false);
    probe.textContent = 'X';
    pill.after(probe);
    const one = probe.getBoundingClientRect().height;
    probe.remove();
    if (box(pill).height > one + 1) found.push(describe(pill) + ': ' + pill.textContent.trim() + ' (' + Math.round(box(pill).height) + 'px, one line ' + Math.round(one) + 'px, parent ' + getComputedStyle(pill.parentElement).display + ')');
  }
  return found;
}"""


_OVERLAPS = "() => {" + _DESCRIBE + """
  const found = [];
  for (const parent of document.querySelectorAll('main *')) {
    const display = getComputedStyle(parent).display;
    if (!/grid|flex/.test(display)) continue;
    const items = inFlow(parent);
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
  return found.slice(0, 10);
}"""

_ROW_HEIGHTS = "(lines) => {" + _DESCRIBE + """
  // A row is as tall as its tallest cell's content. The cell itself is
  // stretched to the row, so what is measured is the visible text in it: not
  // a closed disclosure's body, not the lines a clamp cuts off.
  const shown = (text, cell) => {
    const range = document.createRange();
    range.selectNodeContents(text);
    let bottom = range.getBoundingClientRect().bottom;
    for (let up = text.parentElement; up && up !== cell; up = up.parentElement) {
      if (getComputedStyle(up).overflowY !== 'visible') bottom = Math.min(bottom, box(up).bottom);
    }
    return bottom;
  };
  const found = [];
  for (const row of document.querySelectorAll('main tbody tr')) {
    if (!row.getClientRects().length || row.cells.length === 1) continue;
    for (const cell of row.cells) {
      const style = getComputedStyle(cell);
      const size = parseFloat(style.fontSize);
      const line = style.lineHeight === 'normal' ? size * 1.2 : parseFloat(style.lineHeight);
      const top = box(cell).top + parseFloat(style.paddingTop);
      let bottom = top;
      const walker = document.createTreeWalker(cell, NodeFilter.SHOW_TEXT);
      for (let text = walker.nextNode(); text; text = walker.nextNode()) {
        if (!text.textContent.trim() || !text.parentElement.checkVisibility()) continue;
        bottom = Math.max(bottom, shown(text, cell));
      }
      const height = bottom - top;
      if (height > (lines + 0.5) * line) {
        found.push(`${describe(row.closest('table'))} row ${row.rowIndex}: ${describe(cell)} is ${(height / line).toFixed(1)} lines: ${cell.textContent.trim().replace(/\\s+/g, ' ').slice(0, 60)}`);
      }
    }
  }
  return [...new Set(found)].slice(0, 10);
}"""

_CONTRAST = "([scope, body, large]) => {" + _DESCRIBE + """
  // Every visible element that holds text of its own, measured against the
  // colour actually behind it: the nearest ancestor that paints a background,
  // with translucent layers composited down to an opaque one. A colour
  // written for one palette only is what this catches: a light-mode ink left
  // on a dark card reads as nothing, and no other check looks at colour.
  const canvas = document.createElement('canvas').getContext('2d', {willReadFrequently: true});
  canvas.canvas.width = canvas.canvas.height = 1;
  // Whatever syntax the browser serialises a colour in (rgb(), color(srgb),
  // oklch from a color-mix), paint it and read the pixel back.
  const cache = new Map();
  const rgba = (value) => {
    if (cache.has(value)) return cache.get(value);
    canvas.clearRect(0, 0, 1, 1);
    canvas.fillStyle = '#000'; canvas.fillStyle = value;
    canvas.fillRect(0, 0, 1, 1);
    const [r, g, b, a] = canvas.getImageData(0, 0, 1, 1).data;
    const parsed = [r, g, b, a / 255];
    cache.set(value, parsed);
    return parsed;
  };
  const over = (top, under) => {
    const a = top[3];
    return [0, 1, 2].map((i) => top[i] * a + under[i] * (1 - a)).concat(1);
  };
  const background = (el) => {
    const layers = [];
    for (let up = el; up; up = up.parentElement) {
      const colour = rgba(getComputedStyle(up).backgroundColor);
      if (colour[3] > 0) layers.push(colour);
      if (colour[3] >= 1) break;
    }
    let result = rgba(getComputedStyle(document.documentElement).backgroundColor);
    if (result[3] < 1) result = [255, 255, 255, 1];
    for (const layer of layers.reverse()) result = over(layer, result);
    return result;
  };
  const luminance = ([r, g, b]) => {
    const [R, G, B] = [r, g, b].map((c) => {
      c /= 255;
      return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
    });
    return 0.2126 * R + 0.7152 * G + 0.0722 * B;
  };
  const ratio = (a, b) => {
    const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
    return (hi + 0.05) / (lo + 0.05);
  };
  // Inactive controls and deliberately faded content are exempt, as WCAG
  // exempts them: they say they are not in play.
  const inactive = (el) => el.closest(':disabled, [aria-disabled="true"], [hidden]')
    || [...function* () { for (let up = el; up; up = up.parentElement) yield up; }()]
      .some((up) => parseFloat(getComputedStyle(up).opacity) < 1);
  const found = [];
  let sampled = 0;
  for (const root of document.querySelectorAll(scope)) {
    for (const el of root.querySelectorAll('*')) {
      const own = [...el.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim());
      if (!own || !el.checkVisibility()) continue;
      const r = box(el);
      if (r.width < 2 || r.height < 2 || inactive(el)) continue;
      const style = getComputedStyle(el);
      const size = parseFloat(style.fontSize);
      // Compact calendar bars retain accessible text but paint no glyphs.
      if (size === 0) continue;
      const bold = parseInt(style.fontWeight, 10) >= 700;
      const needed = size >= 24 || (bold && size >= 18.66) ? large : body;
      const behind = background(el);
      const ink = over(rgba(style.color), behind);
      const measured = ratio(ink, behind);
      sampled += 1;
      if (measured < needed) {
        found.push(`${describe(el)} ${measured.toFixed(2)}:1 (${style.color} on ${behind.slice(0, 3).map(Math.round)}): ${el.textContent.trim().slice(0, 40)}`);
      }
    }
  }
  return {sampled, found: [...new Set(found)].slice(0, 10)};
}"""


# A band cell with no padding: its content touches the hairlines around it.
# The frame rule zeroes the inset of anything nested in a surface, and a band
# is one, so a component placed in a band loses its padding without a trace.
_UNPADDED_CELLS = "(selector) => {" + _DESCRIBE + """
  return [...document.querySelectorAll(selector)]
    .filter((el) => el.getBoundingClientRect().width > 0)
    .filter((el) => {
      const style = getComputedStyle(el);
      return parseFloat(style.paddingLeft) < 4 || parseFloat(style.paddingTop) < 4;
    })
    .map(describe);
}"""


# A filled box whose text or buttons touch its edge. The frame rule strips a
# nested surface's border and padding together; one that keeps its own fill is
# still a tile, and without padding its content sits on the fill's edge.
_UNPADDED_TILES = "([exempt, control, detached, disclosure]) => {" + _DESCRIBE + """
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
      const box = node.parentElement.closest(control) || node;
      let at;
      if (box === node) { range.selectNodeContents(node); at = range.getBoundingClientRect(); }
      else at = box.getBoundingClientRect();
      if (at.width && (at.left - r.left < 4 || r.bottom - at.bottom < 4)) {
        found.push(`${describe(el)}: ${node.textContent.trim().slice(0, 40)}`);
        break;
      }
    }
  }
  return [...new Set(found)].slice(0, 10);
}"""


# The first table on a page as a phone lays it out: whether it runs past its
# box, how wide each showing column is, and how tall each record's row is.
# The row toggle and the line an open row adds are made by tables.js, so no
# template renders them and they are found by tag and attribute.
_PHONE_TABLE = """(scroll) => {
  const box = document.querySelector('main ' + scroll);
  const table = box && box.querySelector('table');
  if (!table) return null;
  const showing = (el) => getComputedStyle(el).display !== 'none';
  return {
    sideways: Math.round(box.scrollWidth - box.clientWidth),
    headings: table.querySelectorAll('thead th').length,
    columns: [...table.querySelectorAll('thead th')].filter(showing)
      .map((heading) => Math.round(heading.getBoundingClientRect().width)),
    rows: [...table.querySelectorAll('tbody > tr:not([data-row-detail])')]
      .map((row) => Math.round(row.getBoundingClientRect().height)),
    toggles: table.querySelectorAll('tbody button[aria-expanded]').length,
    details: [...table.querySelectorAll('tr[data-row-detail] dt')].map((term) => term.textContent),
    held: table.hasAttribute('data-rows-held'),
  };
}"""
# Every table that is wider than the box it scrolls in, and for each table
# that has row toggles, how many different left edges its rows' names start on.
_TABLES_THAT_DO_NOT_FIT = """(scroll) => [...document.querySelectorAll('main ' + scroll)]
  .filter((box) => box.checkVisibility() && !box.closest('details:not([open])'))
  .map((box) => [box, box.querySelector(':scope > table')])
  .filter(([box, table]) => table && table.querySelector('tbody td')
    && table.getBoundingClientRect().width > box.clientWidth + 1)
  .map(([box, table]) => (table.querySelector('th') || table).textContent.trim().slice(0, 24)
    + ' +' + Math.round(table.getBoundingClientRect().width - box.clientWidth))"""
_NAME_EDGES = """(scroll) => [...document.querySelectorAll('main ' + scroll + ' > table')]
  .filter((table) => table.checkVisibility() && table.querySelector('tbody button[aria-expanded]'))
  .map((table) => {
    const toggle = table.querySelector('tbody button[aria-expanded]');
    const column = toggle.closest('td, th').cellIndex;
    const edges = [...table.querySelectorAll('tbody > tr:not([data-row-detail])')]
      .map((row) => row.children[column])
      .filter((cell) => cell && !cell.hasAttribute('colspan') && cell.textContent.trim())
      .map((cell) => {
        const range = document.createRange();
        range.selectNodeContents(cell);
        const first = [...range.getClientRects()].find((rect) => rect.width > 1);
        return first ? Math.round(first.left) : null;
      })
      .filter((edge) => edge !== null);
    return new Set(edges).size;
  })"""
# Press a heading's sort control and read its column back, top to bottom.
_SORT_BY = """([scroll, column]) => {
  const table = document.querySelector('main ' + scroll + ' > table');
  const heading = table.querySelectorAll('thead th')[column];
  const control = heading.querySelector('button');
  if (!control) return null;
  control.click();
  return {
    sort: heading.getAttribute('aria-sort'),
    values: [...table.querySelectorAll('tbody')].map((body) =>
      [...body.querySelectorAll(':scope > tr')]
        .filter((row) => row.children.length > column && !row.querySelector('[colspan]'))
        .map((row) => row.children[column].textContent.trim().replace(/\\s+/g, ' '))),
  };
}"""
_PRESS_FIRST_ROW_TOGGLE = "(scroll) => document.querySelector('main ' + scroll + ' tbody button[aria-expanded]').click()"


# Each reading's mark and lines, as laid out: where the mark sits, and for each
# line whether it starts beside the mark, whether it is cut short, and where
# its top is.
_READINGS = """(heads) => heads.map((head) => {
  const [mark, ...lines] = [...head.children].filter((el) => el.checkVisibility());
  const at = mark.getBoundingClientRect();
  return {
    top: Math.round(head.getBoundingClientRect().top),
    mark: Math.round((at.top + at.bottom) / 2),
    lead: (() => { const r = lines[0].getBoundingClientRect(); return Math.round((r.top + r.bottom) / 2); })(),
    under_the_mark: lines.filter((line) => line.getBoundingClientRect().left < at.right - 1)
      .map((line) => line.textContent.trim().slice(0, 30)),
    cut: lines.filter((line) => line.scrollWidth > line.clientWidth + 1)
      .map((line) => line.textContent.trim().slice(0, 30)),
    second: lines[1] ? Math.round(lines[1].getBoundingClientRect().top - head.getBoundingClientRect().top) : null,
    size: getComputedStyle(lines[0].querySelector('strong') || lines[0]).fontSize,
  };
})"""


# A table with one row whose only clipped text is for screen readers, beside
# a decoration hidden from them, and one row whose value really is cut off.
_ROW_TOGGLE_PROBE = f"""<!doctype html><html><head>
<link rel="stylesheet" href="{settings.STATIC_URL}css/app.css">
<script defer src="{settings.STATIC_URL}js/tables.js"></script>
</head><body><main><div class="table-scroll"><table class="data-table"><tbody>
<tr data-probe="screen-reader"><th scope="row">Chest</th>
<td><span class="cadence-mark is-hit" aria-hidden="true"></span>
<span class="visually-hidden">One session in the week of Aug 10, said in full</span></td></tr>
<tr data-probe="clamped"><th scope="row">Name</th>
<td><div style="max-width: 40px; overflow: hidden; white-space: nowrap">a value far too long to fit</div></td></tr>
</tbody></table></div></main></body></html>"""
_ROW_TOGGLES = """() => [...document.querySelectorAll('tbody tr')].map((row) => ({
  probe: row.dataset.probe,
  toggles: row.querySelectorAll('button[aria-expanded]').length,
  beside_name: row.querySelector('th button[aria-expanded]') !== null,
  in_decoration: row.querySelector('[aria-hidden=true] button[aria-expanded]') !== null,
}))"""


class LayoutBrowserTests(SimpleTestCase):
    databases = {"default"}

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Rendered, and rolled back, before the browser starts: Django refuses
        # database access while Playwright's event loop runs on this thread.
        with transaction.atomic():
            cls.pages = render_pages(build_estate())
            transaction.set_rollback(True)
        with transaction.atomic():
            cls.pages |= render_dense_pages(build_dense_estate())
            transaction.set_rollback(True)
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Playwright is not installed. Run: mise run browser"
            ) from exc
        cls.playwright = sync_playwright().start()
        cls.addClassCleanup(cls.playwright.stop)
        engine = os.environ.get("HQ_BROWSER_ENGINE", "chromium")
        if engine not in {"chromium", "firefox", "webkit"}:
            raise ValueError("HQ_BROWSER_ENGINE must be chromium, firefox, or webkit.")
        options = {"headless": True}
        if channel := os.environ.get("HQ_BROWSER_CHANNEL"):
            options["channel"] = channel
        cls.browser = getattr(cls.playwright, engine).launch(**options)
        cls.addClassCleanup(cls.browser.close)

    def setUp(self):
        self.context = None
        self.start()
        self.addCleanup(lambda: self.context.close())
        self.addCleanup(self.capture_failure)

    def start(self, **options):
        """A fresh browser context, replacing any the test already has.

        Layout must hold before progressive enhancement, so script is off
        unless a test is checking the enhancement itself.
        """

        if self.context is not None:
            self.context.close()
        options.setdefault("java_script_enabled", False)
        self.context = self.browser.new_context(**options)
        self.context.set_default_timeout(5000)
        self.context.route("**/*", self.respond)
        self.page = self.context.new_page()

    def respond(self, route):
        path = urlsplit(route.request.url).path
        name = path.strip("/")
        if name in self.pages:
            route.fulfill(content_type="text/html", body=self.pages[name])
            return
        if path.startswith(settings.STATIC_URL):
            asset = finders.find(path.removeprefix(settings.STATIC_URL))
            if asset:
                route.fulfill(
                    content_type=mimetypes.guess_type(asset)[0]
                    or "application/octet-stream",
                    body=Path(asset).read_bytes(),
                )
                return
        route.fulfill(status=404, body="Not in the synthetic fixture")

    def capture_failure(self):
        result = self._outcome.result
        if any(
            getattr(test, "test_case", test) is self
            for test, _ in result.failures + result.errors
        ):
            folder = Path(tempfile.mkdtemp(prefix="hq-browser-failure-"))
            self.page.screenshot(path=str(folder / "page.png"), full_page=True)
            print(f"Browser failure screenshot: {folder / 'page.png'}")

    def open(self, name, width):
        self.page.set_viewport_size({"width": width, "height": 900})
        self.page.goto(f"{ORIGIN}/{name}/", wait_until="load")

    def each(self, check):
        """Run ``check(name)`` on every page at every width, one subtest each."""

        for name in PAGES:
            for width in WIDTHS:
                with self.subTest(page=name, width=width):
                    self.open(name, width)
                    check(name)

    def across(self, check, pages=ALL_PAGES):
        """Run ``check(name)`` on ``pages`` at every OVERFLOW_WIDTHS width."""

        for name in pages:
            for width in OVERFLOW_WIDTHS:
                with self.subTest(page=name, width=width):
                    self.open(name, width)
                    check(name)

    def boxes(self, key):
        return self.page.locator(SELECTORS[key]).evaluate_all(
            "elements => elements.map(e => { const r = e.getBoundingClientRect(); "
            "return {top: r.top, bottom: r.bottom, left: r.left}; })"
        )

    def test_every_stylesheet_loads(self):
        def check(_name):
            sheets = self.page.evaluate(_STYLESHEETS)
            self.assertGreater(sheets["links"], 0)
            self.assertEqual(sheets["loaded"], sheets["links"])
            self.assertEqual(sheets["empty"], [])

        self.each(check)

    def assert_decision_evidence(self, row):
        evidence = row.locator(SELECTORS["decision_evidence"])
        if not evidence.count():
            return
        detail = evidence.locator(SELECTORS["decision_detail"])
        if detail.count():
            self.assertFalse(detail.is_visible())
        row.locator(SELECTORS["decision_summary"]).click()
        if detail.count():
            self.assertTrue(detail.is_visible())
        row.locator(SELECTORS["decision_summary"]).click()

    def test_decisions_keep_actions_visible_and_evidence_reachable_without_script(self):
        for width in OVERFLOW_WIDTHS:
            with self.subTest(width=width):
                self.open("action-items", width)
                # A family of the same kind of item is folded behind its own
                # native summary: one press, no script, and its rows are there.
                for family in self.page.locator(SELECTORS["decision_family"]).all():
                    self.assertTrue(family.is_visible())
                    family.click()
                rows = self.page.locator(SELECTORS["decisions"])
                self.assertGreater(rows.count(), 0)
                for row in rows.all():
                    self.assertTrue(row.locator(SELECTORS["decision_title"]).is_visible())
                    for action in row.locator(SELECTORS["decision_actions"]).all():
                        self.assertTrue(action.is_visible())
                    self.assert_decision_evidence(row)

    def test_nothing_escapes_the_page_sideways(self):
        """Closed, and with every disclosure and popover open."""

        def check(_name):
            self.assertEqual(self.page.evaluate(_ESCAPES), [])
            self.page.evaluate(
                "() => document.querySelectorAll('details').forEach((d) => { d.open = true; })"
            )
            self.assertEqual(self.page.evaluate(_ESCAPES), [])

        self.each(check)

    def test_nothing_is_framed_inside_a_frame(self):
        def check(_name):
            self.assertEqual(
                self.page.evaluate(
                    _NESTED_FRAMES, [SELECTORS["frame_exempt"], SELECTORS["frame_exempt_within"]]
                ),
                [],
            )

        self.across(check)

    def test_a_narrow_head_keeps_its_lead_control_beside_the_title(self):
        for name in ALL_PAGES:
            with self.subTest(page=name):
                self.open(name, 390)
                self.assertIn(
                    self.page.evaluate(
                        _HEAD_ON_ONE_LINE, [SELECTORS["head_lead"], SELECTORS["head_title"]]
                    ),
                    (None, True),
                )

    def test_only_a_table_scrolls_sideways(self):
        """At a phone, tablet and laptop width, with every disclosure open."""

        def check(_name):
            self.assertEqual(self.page.evaluate(_SIDEWAYS, SELECTORS["table_scroll"]), [])
            self.page.evaluate(
                "() => document.querySelectorAll('details').forEach((d) => { d.open = true; })"
            )
            self.assertEqual(self.page.evaluate(_SIDEWAYS, SELECTORS["table_scroll"]), [])

        self.across(check)

    def test_nothing_runs_out_of_its_table_cell(self):
        self.across(lambda _name: self.assertEqual(self.page.evaluate(_CELLS), []))

    def test_a_table_row_stays_within_its_height_budget(self):
        self.across(
            lambda _name: self.assertEqual(self.page.evaluate(_ROW_HEIGHTS, ROW_LINES), [])
        )

    def test_dense_pages_keep_to_their_boxes(self):
        """Nothing escapes, overlaps, or wraps a pill with production-sized data."""

        def check(_name):
            self.assertEqual(self.page.evaluate(_ESCAPES), [])
            self.assertEqual(self.page.evaluate(_OVERLAPS), [])
            self.assertEqual(self.page.evaluate(_WRAPPED_PILLS, SELECTORS["pill"]), [])

        self.across(check, DENSE_PAGES)

    def test_tables_scroll_inside_their_own_container(self):
        """On a phone a wide table scrolls sideways; it never stacks into cards."""

        def check(name):
            tables = self.page.evaluate(_TABLES)
            self.assertEqual(tables["found"], [])
            if "<table" in self.pages[name]:
                self.assertGreater(tables["seen"], 0, "the probe measured nothing")

        self.each(check)

    def test_grid_rows_end_together(self):
        self.each(lambda _name: self.assertEqual(self.page.evaluate(_PAIRS), []))

    def test_a_pill_never_wraps(self):
        self.each(
            lambda _name: self.assertEqual(
                self.page.evaluate(_WRAPPED_PILLS, SELECTORS["pill"]), []
            )
        )

    def test_nothing_overlaps(self):
        self.each(lambda _name: self.assertEqual(self.page.evaluate(_OVERLAPS), []))

    def test_layout_audit_structural_rules(self):
        audit = AUDIT.read_text(encoding="utf-8").strip()
        script = (
            "async () => { const audit = " + audit + ";\n"
            "return await audit({ evaluate: (probe) => probe() }); }"
        )

        def check(_name):
            report = self.page.evaluate(script)
            self.assertEqual(
                [v for v in report["violations"] if v["rule"] in AUDIT_RULES], []
            )

        self.each(check)

    def test_text_reads_in_the_dark_theme(self):
        """Text clears WCAG AA on whatever it sits on, in the dark palette.

        Colour lives in tokens and each token carries both palettes, so a
        light-only colour can only come from a component that went round them.
        The stylesheet test catches a literal; this catches the rest (a token
        whose dark half was never checked, ink meant for one surface drawn on
        another) by measuring what the browser actually paints.
        """

        self.start(color_scheme="dark")

        def check(_name):
            self.assertEqual(
                self.page.evaluate("() => matchMedia('(prefers-color-scheme: dark)').matches"), True
            )
            report = self.page.evaluate(
                _CONTRAST, [SELECTORS["contrast_scope"], TEXT_CONTRAST, LARGE_TEXT_CONTRAST]
            )
            self.assertGreater(report["sampled"], 0)
            self.assertEqual(report["found"], [])

        self.across(check)

    def test_the_dashboard_pairs_its_cards_on_a_wide_screen(self):
        self.open("dashboard", 1280)
        for key in ("highlights", "patterns"):
            with self.subTest(cards=key):
                first, second = self.boxes(key)
                self.assertAlmostEqual(first["top"], second["top"], delta=1)
                self.assertAlmostEqual(first["bottom"], second["bottom"], delta=1)

    def test_the_dashboard_stacks_its_cards_on_a_phone(self):
        for width in (320, 390):
            self.open("dashboard", width)
            for key in ("highlights", "patterns"):
                with self.subTest(cards=key, width=width):
                    first, second = self.boxes(key)
                    self.assertGreater(second["top"], first["bottom"])
                    self.assertAlmostEqual(first["left"], second["left"], delta=1)

    def test_a_dashboard_without_contributors_draws_no_empty_pairs(self):
        self.open("dashboard-bare", 1280)
        # The dashboard's card rows, not every selector the gate knows: a
        # bare dashboard still has buttons and labels.
        for key in ("highlights", "patterns"):
            with self.subTest(cards=key):
                self.assertEqual(self.boxes(key), [])

    def test_a_row_toggle_is_owed_only_to_what_a_reader_cannot_see(self):
        """The row expander measures what is cut off, so it runs with script on.

        Screen-reader text is clipped to a pixel by design and a decoration is
        hidden from assistive technology; neither is content a row withholds.
        A row that really is cut gets one toggle, beside the cell that names it.
        """

        self.start(java_script_enabled=True)
        self.pages = {**self.pages, "row-toggle-probe": _ROW_TOGGLE_PROBE}
        self.open("row-toggle-probe", 1280)
        rows = {row["probe"]: row for row in self.page.evaluate(_ROW_TOGGLES)}
        self.assertEqual(rows["screen-reader"]["toggles"], 0)
        self.assertEqual(rows["clamped"]["toggles"], 1)
        self.assertTrue(rows["clamped"]["beside_name"])
        self.assertFalse(rows["clamped"]["in_decoration"])

    def test_a_wide_table_fits_a_phone_and_opening_a_row_moves_nothing(self):
        """A table of four columns or more keeps the column that names each row
        and the ones marked key, and fits the screen; it does not stack into
        cards and is not swiped through. What left is listed when a row is
        opened, beneath that row, and opening it changes no column's width and
        no other row's height: the reader's place in the table holds.
        """

        self.start(java_script_enabled=True)
        for name in ("connections", "services", "containers", "projects"):
            with self.subTest(page=name):
                self.open(name, 390)
                closed = self.page.evaluate(_PHONE_TABLE, SELECTORS["table_scroll"])
                self.assertGreaterEqual(closed["headings"], 4)
                self.assertLess(len(closed["columns"]), closed["headings"])
                self.assertLessEqual(closed["sideways"], 1)
                self.assertGreater(closed["toggles"], 0)

                self.page.evaluate(_PRESS_FIRST_ROW_TOGGLE, SELECTORS["table_scroll"])
                opened = self.page.evaluate(_PHONE_TABLE, SELECTORS["table_scroll"])
                self.assertTrue(opened["details"])
                self.assertEqual(opened["columns"], closed["columns"])
                self.assertEqual(opened["rows"][1:], closed["rows"][1:])
                self.assertLessEqual(opened["sideways"], 1)

                self.page.evaluate(_PRESS_FIRST_ROW_TOGGLE, SELECTORS["table_scroll"])
                again = self.page.evaluate(_PHONE_TABLE, SELECTORS["table_scroll"])
                self.assertEqual(again["details"], [])
                self.assertFalse(again["held"])
                self.assertEqual(again["columns"], closed["columns"])
                self.assertEqual(again["rows"], closed["rows"])

    def test_a_wide_table_on_a_desktop_keeps_every_column_and_holds_them_when_a_row_opens(self):
        self.start(java_script_enabled=True)
        self.open("connections", 1440)
        closed = self.page.evaluate(_PHONE_TABLE, SELECTORS["table_scroll"])
        self.assertEqual(len(closed["columns"]), closed["headings"])

        if closed["toggles"]:
            self.page.evaluate(_PRESS_FIRST_ROW_TOGGLE, SELECTORS["table_scroll"])
            opened = self.page.evaluate(_PHONE_TABLE, SELECTORS["table_scroll"])
            self.assertEqual(opened["columns"], closed["columns"])
            self.assertEqual(opened["rows"][1:], closed["rows"][1:])

    def test_a_table_fits_its_box_at_every_width(self):
        """Not only on a phone. A table wider than its box wraps, and where
        wrapping would cramp it gives up columns from the far end, so nothing
        a reader came for is off the edge at a tablet or a laptop width
        either. Every page, with every disclosure open.
        """

        self.start(java_script_enabled=True)

        def check(_name):
            self.page.evaluate(
                "() => document.querySelectorAll('details').forEach((d) => { d.open = true; })"
            )
            self.page.evaluate("() => window.dispatchEvent(new Event('resize'))")
            self.page.wait_for_timeout(200)
            self.assertEqual(
                self.page.evaluate(_TABLES_THAT_DO_NOT_FIT, SELECTORS["table_scroll"]), []
            )

        self.across(check)

    def test_a_tables_names_share_one_edge_whether_or_not_a_row_opens(self):
        """A row with a toggle and a row without start their names on the same
        line down the column: the toggle has a gutter every row keeps.
        """

        self.start(java_script_enabled=True)

        def check(_name):
            for edges in self.page.evaluate(_NAME_EDGES, SELECTORS["table_scroll"]):
                self.assertEqual(edges, 1)

        self.across(check)

    def test_any_table_sorts_by_a_heading(self):
        """A table whose server does not sort it sorts on the page: ascending,
        then descending, then as it came, each section keeping its own rows.
        """

        self.start(java_script_enabled=True)
        self.open("services", 1440)
        came = self.page.evaluate(
            "(scroll) => [...document.querySelectorAll('main ' + scroll + ' > table tbody')]"
            ".map((body) => [...body.querySelectorAll(':scope > tr')]"
            ".filter((row) => !row.querySelector('[colspan]'))"
            ".map((row) => row.children[0].textContent.trim().replace(/\\s+/g, ' ')))",
            SELECTORS["table_scroll"],
        )
        up = self.page.evaluate(_SORT_BY, [SELECTORS["table_scroll"], 0])
        self.assertEqual(up["sort"], "ascending")
        self.assertEqual(up["values"], [sorted(group, key=str.lower) for group in came])
        down = self.page.evaluate(_SORT_BY, [SELECTORS["table_scroll"], 0])
        self.assertEqual(down["sort"], "descending")
        self.assertEqual(down["values"], [sorted(group, key=str.lower, reverse=True) for group in came])
        back = self.page.evaluate(_SORT_BY, [SELECTORS["table_scroll"], 0])
        self.assertEqual(back["sort"], "none")
        self.assertEqual(back["values"], came)

    def test_the_dashboards_readings_are_one_shape(self):
        """Each is a mark, a lead and its lines, and they are the same shape:
        the mark beside the lead and nothing set under it, every lead one size,
        every second line starting the same distance down, and no line cut
        short. A caption that fell into the mark's own narrow column read as
        "Via h" and "Has", and nothing else in the gate would have said so.
        """

        for width in (390, 820, 1280, 1440):
            with self.subTest(width=width):
                self.open("dashboard", width)
                readings = self.page.locator(SELECTORS["readings"]).evaluate_all(_READINGS)
                self.assertGreaterEqual(len(readings), 3)
                for reading in readings:
                    self.assertEqual(reading["under_the_mark"], [])
                    self.assertEqual(reading["cut"], [])
                    self.assertAlmostEqual(reading["mark"], reading["lead"], delta=3)
                self.assertEqual(len({reading["size"] for reading in readings}), 1)
                self.assertEqual(len({reading["second"] for reading in readings if reading["second"]}), 1)

    def test_a_filled_box_keeps_its_padding(self):
        self.across(
            lambda _name: self.assertEqual(
                self.page.evaluate(
                    _UNPADDED_TILES,
                    [SELECTORS["tile_exempt"], SELECTORS["tile_control"], SELECTORS["tile_detached"], SELECTORS["disclosure"]],
                ), []
            )
        )

    def test_no_column_is_narrower_than_its_words(self):
        self.across(lambda _name: self.assertEqual(self.page.evaluate(_BROKEN_WORDS), []))

    def test_a_link_sits_on_the_line_it_continues(self):
        self.across(lambda _name: self.assertEqual(self.page.evaluate(_RAISED_LINKS), []))

    def test_a_row_has_one_disclosure(self):
        self.across(lambda _name: self.assertEqual(self.page.evaluate(_NESTED_DISCLOSURES), []))

    def test_a_band_cell_keeps_its_padding(self):
        def check(_name):
            self.assertEqual(self.page.evaluate(_UNPADDED_CELLS, SELECTORS["band_cell"]), [])

        self.across(check)
