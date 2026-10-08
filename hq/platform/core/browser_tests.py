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

import json
import mimetypes
import os
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.staticfiles import finders
from django.db import transaction
from django.test import Client, SimpleTestCase
from django.urls import reverse

from hq.platform.core.tests.test_browser_dense_fixtures import DENSE_PAGES, build_dense_estate, render_dense_pages
from hq.platform.core.tests.test_browser_fixtures import PAGES, build_estate, render_pages

# The phone HQ is read on: its viewport in CSS pixels.
PHONE_SCREEN = (402, 874)
WIDTHS = (320, PHONE_SCREEN[0], 768, 1280)
# A phone, a tablet held upright, a laptop: where only a table may scroll sideways.
OVERFLOW_WIDTHS = (PHONE_SCREEN[0], 820, 1360)
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
    # The fragment primitive: a placeholder's failure line, the disclosure
    # holding one, and the calendar it pages in place.
    "fragment_failure": "main > [data-fragment-load] > .notice",
    "fragment_fold": "details:has(> [data-fragment-load]) > summary",
    "calendar_period": "#calendar-period",
    "calendar_next": '#calendar a[rel="next"]',
    "decisions": "[data-attention-item]",
    "decision_family": "details[data-queue-family] > summary strong",
    "decision_title": ".attention-title",
    "decision_fold": ".resolution-fold",
    "decision_fold_summary": ".resolution-fold > summary",
    "decision_steps": ".resolution-workflow",
    "decision_detail": ".attention-detail",
    "decision_actions": ".attention-actions",
    "highlights": ".dashboard-highlights > .highlight-card",
    # Each reading in the dashboard's row: a mark, then its lines.
    "readings": ".dash-readings .glance-panel-head",
    "patterns": ".dashboard-patterns > .card",
    "pill": ".pill",
    # Everything pressed: a button, a link drawn as one, a disclosure's handle.
    "control": "button, .btn, .icon-link, summary",
    # Anything in a table cell: its words stay inside its own box, or they are
    # drawn across the next column while every box measures as in place.
    "cell_content": "main td *, main th *",
    # What a control says about the work it asked for, drawn under it.
    "spoken": ".ask-said",
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
    "tile_exempt": (".btn, .pill, button, input, select, textarea, table, pre, code, svg, canvas, dialog, [data-menu]"),
    # A control inside a tile is measured by its own box, not its text.
    "tile_control": ".btn, button",
    # A menu and the topology map lay out away from the box they sit in, so
    # what they hold is not measured against it.
    "tile_detached": "dialog, [data-menu], .topology-map",
    # A disclosure in the page, which the tile check opens to lay out.
    "disclosure": "main details:not([data-menu])",
    # Where the API reference mounts its viewer.
    "reference": "#api-reference-root",
    # A band's cells. Stats inside a card are the one band laid out with real
    # gaps instead of padded cells, so the KPI band is not listed.
    "band_cell": (
        ":is(.control-summary, .service-band, .fact-band, .finding-facts, .insight-grid,"
        " .connection-control-grid, .sweep-grid,"
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
  return found.slice(0, 10);
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
  return found.slice(0, 10);
}"""
)

_HEAD_ON_ONE_LINE = """([leadSelector, titleSelector]) => {
  // A narrow head is one line: the title and its lead control, nothing
  // between them and the lede.
  const lead = document.querySelector(leadSelector);
  const title = document.querySelector(titleSelector);
  if (!lead || !title) return null;
  const a = lead.getBoundingClientRect(), t = title.getBoundingClientRect();
  return a.top < t.bottom && a.bottom > t.top;
}"""

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
  return found.slice(0, 10);
}"""
)

_CELLS = (
    "() => {"
    + _DESCRIBE
    + """
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
)

# A link that sits above or below the words it continues: an inline line in a
# smaller size than its cell aligns a clamped link to the cell's taller line.
_RAISED_LINKS = (
    "() => {"
    + _DESCRIBE
    + """
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
)

# A row has one disclosure. The row toggle shows what a row holds back; a
# dropdown inside the row beside it is a second, smaller way to do the same.
_NESTED_DISCLOSURES = (
    "() => {"
    + _DESCRIBE
    + """
  return [...document.querySelectorAll('main table > tbody > tr')]
    .filter((row) => row.querySelector('button[aria-expanded]') && row.querySelector(':is(td, th) details'))
    .map((row) => `${describe(row)} has a row toggle and a dropdown`)
    .slice(0, 10);
}"""
)

# A plain word split across lines inside a table cell: its column was given
# less than the word, because another column's content took the width.
_BROKEN_WORDS = (
    "() => {"
    + _DESCRIBE
    + """
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
)

_TABLES = (
    "() => {"
    + _DESCRIBE
    + """
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
)

_PAIRS = (
    "() => {"
    + _DESCRIBE
    + """
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
)

_WRAPPED_PILLS = (
    "(selector) => {"
    + _DESCRIBE
    + """
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
)


_OVERLAPS = (
    "() => {"
    + _DESCRIBE
    + """
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
)

_ROW_HEIGHTS = (
    "(lines) => {"
    + _DESCRIBE
    + """
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
)

_CONTRAST = (
    "([scope, body, large]) => {"
    + _DESCRIBE
    + """
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
)


# A band cell with no padding: its content touches the hairlines around it.
# The frame rule zeroes the inset of anything nested in a surface, and a band
# is one, so a component placed in a band loses its padding without a trace.
_UNPADDED_CELLS = (
    "(selector) => {"
    + _DESCRIBE
    + """
  return [...document.querySelectorAll(selector)]
    .filter((el) => el.getBoundingClientRect().width > 0)
    .filter((el) => {
      const style = getComputedStyle(el);
      return parseFloat(style.paddingLeft) < 4 || parseFloat(style.paddingTop) < 4;
    })
    .map(describe);
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
)


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
    rows: [...table.querySelectorAll('tbody > tr:not([data-row-detail]):not(:has(> th[scope="rowgroup"]))')]
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
_SPILLED_LABELS = (
    "(selector) => {"
    + _DESCRIBE
    + """
  // A control is at least as wide as what it says. One sized for a glyph and
  // given a word draws the word over whatever stands beside it.
  const found = [];
  for (const el of document.querySelectorAll(selector)) {
    if (!el.checkVisibility()) continue;
    const style = getComputedStyle(el);
    if (style.overflowX !== 'visible' || style.display === 'inline') continue;
    if (el.scrollWidth > el.clientWidth + 1) {
      found.push(`${describe(el)} holds ${el.scrollWidth}px of label in ${el.clientWidth}px`);
    }
  }
  return found.slice(0, 10);
}"""
)

_SETTLED = "() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)))"

_UNSEEN_COLUMNS = (
    "(scroll) => {"
    + _DESCRIBE
    + """
  // A table's width belongs to the columns that are showing. A cell left
  // spanning columns that were dropped keeps them in the table, and a table
  // held to its box then shares its width with columns nobody sees.
  const found = [];
  for (const table of document.querySelectorAll('main ' + scroll + ' > table')) {
    if (!table.checkVisibility()) continue;
    const shown = [...table.querySelectorAll(':scope > thead > tr:last-child > th')]
      .filter((heading) => getComputedStyle(heading).display !== 'none');
    if (!shown.length) continue;
    const used = shown.reduce((sum, heading) => sum + box(heading).width, 0);
    const width = box(table).width;
    if (used < width - 2) {
      found.push(`${describe(table)} shows ${shown.length} columns in ${Math.round(used)}px of ${Math.round(width)}px`);
    }
  }
  return found;
}"""
)

_MOVED_BY_SPEAKING = (
    "async ([said, spoken]) => {"
    + _DESCRIBE
    + """
  // Pressing a control moves nothing. What it says about the work it asked
  // for is drawn over the page, so every other box is where it was, the page
  // is the size it was, and the words themselves are on the screen.
  const asks = [...document.querySelectorAll('[data-ask]:has([data-ask-note])')].filter((ask) => ask.checkVisibility());
  if (!asks.length) return null;
  const others = [...document.querySelectorAll('body *')]
    .filter((el) => el.checkVisibility() && !el.closest(spoken));
  const page = document.documentElement;
  const size = () => [page.scrollWidth, page.scrollHeight];
  const place = (el) => { const r = box(el); return [r.left, r.top, r.width, r.height].map(Math.round).join(' '); };
  const before = others.map(place);
  const was = size();
  for (const ask of asks) {
    ask.dataset.askState = 'running';
    ask.querySelector('[data-ask-note]').textContent = said;
    ask.querySelector('[data-ask-note]').hidden = false;
    const elapsed = ask.querySelector('[data-ask-elapsed]');
    if (elapsed) elapsed.textContent = '7 s';
  }
  await new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)));
  const found = [];
  others.forEach((el, index) => {
    if (place(el) !== before[index]) found.push(`${describe(el)} moved from ${before[index]} to ${place(el)}`);
  });
  if (String(size()) !== String(was)) found.push(`the page went from ${was} to ${size()}`);
  for (const ask of asks) {
    const note = ask.querySelector(spoken) || ask.querySelector('[data-ask-note]');
    const r = box(note);
    // Inside a box that scrolls, it goes where its button goes.
    let held = false;
    for (let up = ask.parentElement; up && up !== document.body; up = up.parentElement) {
      if (scrolls(up)) { held = true; break; }
    }
    if (r.width < 2) found.push(`${describe(ask)} says nothing that shows`);
    else if (!held && (r.left < -1 || r.right > page.clientWidth + 1)) {
      found.push(`${describe(note)} spans ${Math.round(r.left)}..${Math.round(r.right)}px`);
    }
  }
  return found.slice(0, 10);
}"""
)

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
_PRESS_FIRST_ROW_TOGGLE = (
    "(scroll) => document.querySelector('main ' + scroll + ' tbody button[aria-expanded]').click()"
)


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


class BrowserGate(SimpleTestCase):
    """A browser over pages rendered once, every request answered locally.

    A subclass says what it renders (``render``); the browser then asks only
    for those paths and for static files.
    """

    databases = {"default"}

    @classmethod
    def render(cls):
        """Path to response body, for every page this class opens."""

        raise NotImplementedError

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Rendered, and rolled back, before the browser starts: Django refuses
        # database access while Playwright's event loop runs on this thread.
        cls.pages = cls.render()
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError("Playwright is not installed. Run: mise run browser") from exc
        cls.playwright = sync_playwright().start()
        cls.addClassCleanup(cls.playwright.stop)
        engine = os.environ.get("HQ_BROWSER_ENGINE", "webkit")
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
            route.fulfill(content_type=mimetypes.guess_type(name)[0] or "text/html", body=self.pages[name])
            return
        if path.startswith(settings.STATIC_URL):
            asset = finders.find(path.removeprefix(settings.STATIC_URL))
            if asset:
                route.fulfill(
                    content_type=mimetypes.guess_type(asset)[0] or "application/octet-stream",
                    body=Path(asset).read_bytes(),
                )
                return
        route.fulfill(status=404, body="Not in the synthetic fixture")

    def capture_failure(self):
        result = self._outcome.result
        if any(getattr(test, "test_case", test) is self for test, _ in result.failures + result.errors):
            folder = Path(tempfile.mkdtemp(prefix="hq-browser-failure-"))
            self.page.screenshot(path=str(folder / "page.png"), full_page=True)
            print(f"Browser failure screenshot: {folder / 'page.png'}")

    def open(self, name, width):
        height = PHONE_SCREEN[1] if width <= PHONE else 900
        self.page.set_viewport_size({"width": width, "height": height})
        self.page.goto(f"{ORIGIN}/{name}/", wait_until="load")


# Heads whose lead control is one that says what it is doing beside itself,
# drawn by the head's own partial: a one-word title, two words, and a name.
SPEAKING_HEADS = {
    "head/word": "Reports",
    "head/words": "Two words",
    "head/name": "example-host.example.com",
}


# A shared component filled the way an extension's page fills it, which no host
# page does: a section's heading with a long name and states beside it.
SHARED_PARTS = {
    "part/section-head": (
        '<section class="card"><div class="section-head">'
        "<h2>An example heading long enough to need the row</h2>"
        '<span class="pill pill-good">Example state</span>'
        '<span class="pill pill-neutral">Another example state</span>'
        "</div><p>Example.</p></section>"
    ),
}


def shared_parts():
    return {
        name: (
            '<!doctype html><html><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<link rel="stylesheet" href="{settings.STATIC_URL}css/app.css"></head>'
            f'<body><main class="content">{markup}</main></body></html>'
        )
        for name, markup in SHARED_PARTS.items()
    }


def speaking_heads():
    from django.template.loader import render_to_string

    from hq.platform.application.asks import Ask
    from hq.platform.application.pages import Page, PageAction

    actions = (
        Ask("Read everything", "/probe/", primary=True),
        PageAction("First other", "/probe/first/"),
        PageAction("Second other", "/probe/second/"),
    )
    return {
        name: (
            f'<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<link rel="stylesheet" href="{settings.STATIC_URL}css/app.css"></head><body><main>'
            + render_to_string("partials/_page_head.html", {"page": Page(title=title, actions=actions)})
            + "</main></body></html>"
        )
        for name, title in SPEAKING_HEADS.items()
    }


class LayoutBrowserTests(BrowserGate):
    @classmethod
    def render(cls):
        with transaction.atomic():
            pages = render_pages(build_estate())
            transaction.set_rollback(True)
        with transaction.atomic():
            pages |= render_dense_pages(build_dense_estate())
            transaction.set_rollback(True)
        return pages | speaking_heads() | shared_parts()

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

    def assert_decision_face(self, row):
        """What a card means is on its face; only long steps fold, and one
        press of a native summary opens them."""

        for detail in row.locator(SELECTORS["decision_detail"]).all():
            self.assertTrue(detail.is_visible())
        fold = row.locator(SELECTORS["decision_fold"])
        if not fold.count():
            for steps in row.locator(SELECTORS["decision_steps"]).all():
                self.assertTrue(steps.is_visible())
            return
        steps = fold.locator(SELECTORS["decision_steps"])
        self.assertFalse(steps.is_visible())
        row.locator(SELECTORS["decision_fold_summary"]).click()
        self.assertTrue(steps.is_visible())
        row.locator(SELECTORS["decision_fold_summary"]).click()

    def test_a_card_shows_what_is_wrong_and_its_buttons_without_a_toggle_or_script(self):
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
                    self.assert_decision_face(row)

    def test_nothing_escapes_the_page_sideways(self):
        """Closed, and with every disclosure and popover open."""

        def check(_name):
            self.assertEqual(self.page.evaluate(_ESCAPES), [])
            self.page.evaluate("() => document.querySelectorAll('details').forEach((d) => { d.open = true; })")
            self.assertEqual(self.page.evaluate(_ESCAPES), [])

        self.each(check)
        for name in SHARED_PARTS:
            for width in WIDTHS:
                with self.subTest(page=name, width=width):
                    self.open(name, width)
                    check(name)

    def test_nothing_is_framed_inside_a_frame(self):
        def check(_name):
            self.assertEqual(
                self.page.evaluate(_NESTED_FRAMES, [SELECTORS["frame_exempt"], SELECTORS["frame_exempt_within"]]),
                [],
            )

        self.across(check)

    def test_a_narrow_head_keeps_its_lead_control_beside_the_title(self):
        for name in ALL_PAGES:
            with self.subTest(page=name):
                self.open(name, PHONE_SCREEN[0])
                self.assertIn(
                    self.page.evaluate(_HEAD_ON_ONE_LINE, [SELECTORS["head_lead"], SELECTORS["head_title"]]),
                    (None, True),
                )

    def scripted(self, check, pages=ALL_PAGES, widths=(PHONE_SCREEN[0], 820)):
        """Run ``check(name)`` with the page's script on: the page as it is read."""

        self.start(java_script_enabled=True)
        for name in pages:
            for width in widths:
                with self.subTest(page=name, width=width):
                    self.open(name, width)
                    self.page.evaluate(_SETTLED)
                    check(name)

    def test_the_rules_hold_once_the_script_has_laid_the_page_out(self):
        """The page a person reads is the one its script has fitted: the rules
        that hold without script hold there too."""

        def check(_name):
            self.assertEqual(self.page.evaluate(_SIDEWAYS, SELECTORS["table_scroll"]), [])
            self.assertEqual(self.page.evaluate(_CELLS), [])
            self.assertEqual(self.page.evaluate(_BROKEN_WORDS), [])
            self.assertEqual(self.page.evaluate(_WRAPPED_PILLS, SELECTORS["pill"]), [])
            self.assertEqual(self.page.evaluate(_UNSEEN_COLUMNS, SELECTORS["table_scroll"]), [])
            self.assertEqual(self.page.evaluate(_SPILLED_LABELS, SELECTORS["control"]), [])
            self.assertEqual(self.page.evaluate(_SPILLED_LABELS, SELECTORS["cell_content"]), [])

        self.scripted(check, widths=(PHONE_SCREEN[0], 820, 1360))

    def test_words_stay_inside_the_box_that_holds_them(self):
        def check(_name):
            self.assertEqual(self.page.evaluate(_SPILLED_LABELS, SELECTORS["control"]), [])
            self.assertEqual(self.page.evaluate(_SPILLED_LABELS, SELECTORS["cell_content"]), [])

        self.across(check)

    def test_pressing_a_control_moves_nothing(self):
        seen = 0
        self.start(java_script_enabled=True)
        for name in (*ALL_PAGES, *SPEAKING_HEADS):
            for said in ("Reading", "Reading example-host", "That could not be done. Try again in a minute."):
                for width in (320, PHONE_SCREEN[0], 1360):
                    with self.subTest(page=name, said=said, width=width):
                        self.open(name, width)
                        found = self.page.evaluate(_MOVED_BY_SPEAKING, [said, SELECTORS["spoken"]])
                        if found is None:
                            continue
                        seen += 1
                        self.assertEqual(found, [])
        self.assertGreater(seen, len(SPEAKING_HEADS), "no page in the fixture has a control that speaks")

    def test_only_a_table_scrolls_sideways(self):
        """At a phone, tablet and laptop width, with every disclosure open."""

        def check(_name):
            self.assertEqual(self.page.evaluate(_SIDEWAYS, SELECTORS["table_scroll"]), [])
            self.page.evaluate("() => document.querySelectorAll('details').forEach((d) => { d.open = true; })")
            self.assertEqual(self.page.evaluate(_SIDEWAYS, SELECTORS["table_scroll"]), [])

        self.across(check)

    def test_nothing_runs_out_of_its_table_cell(self):
        self.across(lambda _name: self.assertEqual(self.page.evaluate(_CELLS), []))

    def test_a_table_row_stays_within_its_height_budget(self):
        self.across(lambda _name: self.assertEqual(self.page.evaluate(_ROW_HEIGHTS, ROW_LINES), []))

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
        self.each(lambda _name: self.assertEqual(self.page.evaluate(_WRAPPED_PILLS, SELECTORS["pill"]), []))

    def test_nothing_overlaps(self):
        self.each(lambda _name: self.assertEqual(self.page.evaluate(_OVERLAPS), []))

    def test_layout_audit_structural_rules(self):
        audit = AUDIT.read_text(encoding="utf-8").strip()
        script = "async () => { const audit = " + audit + ";\nreturn await audit({ evaluate: (probe) => probe() }); }"

        def check(_name):
            report = self.page.evaluate(script)
            self.assertEqual([v for v in report["violations"] if v["rule"] in AUDIT_RULES], [])

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
            self.assertEqual(self.page.evaluate("() => matchMedia('(prefers-color-scheme: dark)').matches"), True)
            report = self.page.evaluate(_CONTRAST, [SELECTORS["contrast_scope"], TEXT_CONTRAST, LARGE_TEXT_CONTRAST])
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
        for width in (320, PHONE_SCREEN[0]):
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
                self.open(name, PHONE_SCREEN[0])
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
            self.page.evaluate("() => document.querySelectorAll('details').forEach((d) => { d.open = true; })")
            self.page.evaluate("() => window.dispatchEvent(new Event('resize'))")
            self.page.wait_for_timeout(200)
            self.assertEqual(self.page.evaluate(_TABLES_THAT_DO_NOT_FIT, SELECTORS["table_scroll"]), [])

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

        for width in (PHONE_SCREEN[0], 820, 1280, 1440):
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
                    [
                        SELECTORS["tile_exempt"],
                        SELECTORS["tile_control"],
                        SELECTORS["tile_detached"],
                        SELECTORS["disclosure"],
                    ],
                ),
                [],
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


# The API reference: a vendored viewer mounted inside HQ's frame. Its markup is
# the vendor's, so the probes below find it by tag and ARIA, never by class.
REFERENCE = "api/docs"
REFERENCE_DARK = "api/docs/dark"
# Where the viewer keeps its sidebar open beside the content.
REFERENCE_DESKTOP = (1024, 1360)
REFERENCE_PHONE = PHONE_SCREEN[0]
# The viewer is one large script: parsing it takes longer than a page's load.
REFERENCE_READY = 30_000
_REFERENCE_MOUNTED = "() => document.querySelector('main main h1') !== null"
# The sidebar as drawn: each top-level entry, and what stands under a group.
_REFERENCE_OUTLINE = """() => [...document.querySelectorAll('aside > ul > li')].map((item) => {
  const said = (el) => el.textContent.trim();
  const link = item.querySelector(':scope > a, :scope > div > a');
  if (link) return [link.getAttribute('href'), said(link), []];
  return ['', [...item.children].filter((part) => part.tagName !== 'UL').map(said).join(''),
    [...item.querySelectorAll(':scope > ul > li > div > a')].map(said)];
})"""
# What stands over HQ's header, what of the viewer's is pinned under it, and
# any trail the viewer is showing.
_REFERENCE_FRAME = """(root) => {
  const header = document.querySelector('header');
  const bar = header.getBoundingClientRect();
  const over = [0.05, 0.25, 0.5, 0.75, 0.95]
    .map((share) => document.elementFromPoint(bar.left + bar.width * share, bar.top + bar.height / 2))
    .filter((el) => !header.contains(el))
    .map((el) => el.tagName.toLowerCase());
  const pinned = [...document.querySelector(root).querySelectorAll('aside, nav, header, [data-scalar-scroll-header]')]
    .filter((el) => el.checkVisibility() && ['sticky', 'fixed'].includes(getComputedStyle(el).position))
    .map((el) => [el.tagName.toLowerCase(), Math.round(el.getBoundingClientRect().top)])
    .filter(([, top]) => top < Math.round(bar.bottom) - 1);
  const trails = [...document.querySelectorAll('nav[aria-label="Breadcrumb"]')]
    .filter((el) => el.checkVisibility())
    .map((el) => el.textContent.trim());
  return {header: Math.round(bar.top), over, pinned, trails,
    sideways: document.documentElement.scrollWidth - document.documentElement.clientWidth};
}"""
# What the viewer paints, beside what HQ paints.
_REFERENCE_PAINT = """() => {
  const behind = (el) => {
    for (let up = el; up; up = up.parentElement) {
      const colour = getComputedStyle(up).backgroundColor;
      if (!/rgba\\(0, 0, 0, 0\\)|transparent/.test(colour)) return colour;
    }
    return '';
  };
  const title = document.querySelector('main main h1');
  const page = getComputedStyle(document.body);
  return {
    mode: document.body.className,
    page: page.backgroundColor, viewer: behind(title),
    ink: page.color, title: getComputedStyle(title).color,
    face: page.fontFamily, title_face: getComputedStyle(title).fontFamily,
    header: behind(document.querySelector('header')), sidebar: behind(document.querySelector('aside')),
  };
}"""
_REFERENCE_CHROME = """() => ({
  said: document.querySelector('main').innerText,
  leaves: [...document.querySelectorAll('main a[href]')]
    .filter((a) => a.origin !== location.origin && a.checkVisibility())
    .map((a) => a.href),
  search: document.querySelector('aside button').innerText,
})"""
_REFERENCE_FOLLOW = """(title) => {
  const link = [...document.querySelectorAll('aside a[href]')]
    .find((a) => a.textContent.trim().startsWith(title));
  if (!link) return false;
  link.click();
  return true;
}"""
_REFERENCE_HEADING = """(title) => {
  const heading = [...document.querySelectorAll('main main h2, main main h3')]
    .find((h) => h.textContent.includes(title) && h.checkVisibility());
  if (!heading) return null;
  return {top: Math.round(heading.getBoundingClientRect().top),
    floor: Math.round(document.querySelector('header').getBoundingClientRect().bottom),
    window: innerHeight, hash: location.hash};
}"""
_REFERENCE_OPEN_MENU = "(root) => document.querySelector(root).querySelector('header button').click()"


def reference_outline(document):
    """The sidebar HQ's own document declares: groups, and the domains under each."""

    under: dict[str, list[str]] = {}
    for tag in document["tags"]:
        if "parent" in tag:
            under.setdefault(tag["parent"], []).append(tag["summary"])
    return [
        [tag["summary"], under.get(tag["name"], [])]
        for tag in document["tags"]
        if "parent" not in tag and tag.get("kind") != "badge"
    ]


class ReferenceBrowserTests(BrowserGate):
    """The API reference reads as a page of HQ's, at every width and in both themes."""

    @classmethod
    def render(cls):
        with transaction.atomic():
            client = Client()
            client.force_login(get_user_model().objects.create_user(username="operator"))
            document = reverse("hq_api:openapi")
            page = reverse("api_reference:reference")
            # The document the viewer reads, and the count the header asks for.
            pages = {
                url.strip("/"): client.get(url).content.decode()
                for url in (page, document, reverse("action_item_count"))
            }
            client.post(reverse("theme"), {"theme": "dark"})
            pages[REFERENCE_DARK] = client.get(page).content.decode()
            transaction.set_rollback(True)
        cls.document = json.loads(pages[document.strip("/")])
        return pages

    def start(self, **options):
        options.setdefault("java_script_enabled", True)
        super().start(**options)
        self.errors = []
        self.requests = []
        self.refused = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.on(
            "console",
            lambda message: self.errors.append(message.text) if message.type == "error" else None,
        )
        self.page.on("request", lambda request: self.requests.append(request.url))
        self.page.on(
            "response",
            lambda response: self.refused.append(response.url) if response.status >= 400 else None,
        )

    def visit(self, width, name=REFERENCE, fragment=""):
        self.page.set_viewport_size({"width": width, "height": 900})
        self.page.goto(f"{ORIGIN}/{name}/{fragment}", wait_until="load")
        self.page.wait_for_function(_REFERENCE_MOUNTED, timeout=REFERENCE_READY)

    def frame(self):
        return self.page.evaluate(_REFERENCE_FRAME, SELECTORS["reference"])

    def assert_framed(self, frame):
        """HQ's header on top, the viewer's pinned parts below it, no trail, no sideways scroll."""

        self.assertEqual(frame["header"], 0)
        self.assertEqual(frame["over"], [])
        self.assertEqual(frame["pinned"], [])
        self.assertEqual(frame["trails"], [])
        self.assertLessEqual(frame["sideways"], 1)

    def test_the_sidebar_is_hqs_navigation(self):
        """The groups and domains the document declares, in its order, and nothing else:
        no section for the version badge, none for the schemas."""

        self.visit(1360)
        drawn = [
            [title, under]
            for href, title, under in self.page.evaluate(_REFERENCE_OUTLINE)
            if not href.startswith("#description")
        ]
        self.assertEqual(drawn, reference_outline(self.document))
        self.assertTrue(any(under for _, under in drawn))

    def test_the_header_is_never_covered_and_no_trail_is_shown(self):
        for width in REFERENCE_DESKTOP:
            self.visit(width)
            height = self.page.evaluate("() => document.documentElement.scrollHeight")
            for offset in (0, 600, height // 2, height):
                with self.subTest(width=width, offset=offset):
                    self.page.evaluate("(y) => window.scrollTo(0, y)", offset)
                    self.page.wait_for_timeout(150)
                    self.assert_framed(self.frame())

    def test_a_phone_reaches_the_sidebar_under_the_header(self):
        self.visit(REFERENCE_PHONE)
        self.assert_framed(self.frame())
        self.page.evaluate(_REFERENCE_OPEN_MENU, SELECTORS["reference"])
        self.page.wait_for_timeout(300)
        self.assert_framed(self.frame())
        entries = self.page.evaluate(_REFERENCE_OUTLINE)
        self.assertGreater(len(entries), 1)

    def assert_painted_as_hq(self, paint, mode):
        self.assertIn(mode, paint["mode"].split())
        self.assertEqual(paint["viewer"], paint["page"])
        self.assertEqual(paint["sidebar"], paint["header"])
        self.assertEqual(paint["title"], paint["ink"])
        self.assertEqual(paint["title_face"], paint["face"])

    def test_both_themes_are_painted_from_hqs_tokens(self):
        """The system's theme when none is chosen, the chosen one when it is."""

        painted = {}
        for scheme in ("light", "dark"):
            with self.subTest(scheme=scheme):
                self.start(color_scheme=scheme)
                self.visit(1360)
                painted[scheme] = self.page.evaluate(_REFERENCE_PAINT)
                self.assert_painted_as_hq(painted[scheme], f"{scheme}-mode")
                report = self.page.evaluate(
                    _CONTRAST, [SELECTORS["contrast_scope"], TEXT_CONTRAST, LARGE_TEXT_CONTRAST]
                )
                self.assertGreater(report["sampled"], 0)
                self.assertEqual(report["found"], [])
        self.assertNotEqual(painted["light"]["page"], painted["dark"]["page"])
        with self.subTest(chosen="dark", system="light"):
            self.start(color_scheme="light")
            self.visit(1360, REFERENCE_DARK)
            self.assertEqual(self.page.evaluate(_REFERENCE_PAINT), painted["dark"])

    def test_the_viewer_offers_only_what_works_here(self):
        """One search that says it is the reference's, no request sent from the page,
        no token asked for, no way out to the vendor, and nothing fetched from it."""

        self.visit(1360)
        chrome = self.page.evaluate(_REFERENCE_CHROME)
        for words in ("Test Request", "Bearer Token", "Open API Client", "Powered by", "Client Libraries"):
            with self.subTest(words=words):
                self.assertNotIn(words, chrome["said"])
        self.assertEqual(chrome["leaves"], [])
        self.assertIn("Search the reference", chrome["search"])
        self.assertFalse(chrome["search"].strip().lower().endswith("k"))
        self.assertEqual([url for url in self.requests if not url.startswith(ORIGIN)], [])
        self.assertEqual(self.refused, [])
        self.assertEqual(self.errors, [])

    def test_a_link_to_an_operation_survives_a_reload(self):
        tag = next(tag for tag in self.document["tags"] if "parent" in tag)
        title = next(
            operation["summary"]
            for item in self.document["paths"].values()
            for operation in item.values()
            if operation["tags"][0] == tag["name"]
        )

        def landed():
            self.page.wait_for_timeout(1500)
            heading = self.page.evaluate(_REFERENCE_HEADING, title)
            self.assertIsNotNone(heading)
            self.assertGreaterEqual(heading["top"], heading["floor"])
            self.assertLess(heading["top"], heading["window"] * 0.6)
            return heading["hash"]

        self.visit(1360)
        self.assertTrue(self.page.evaluate(_REFERENCE_FOLLOW, tag["summary"]))
        self.page.wait_for_timeout(500)
        self.assertTrue(self.page.evaluate(_REFERENCE_FOLLOW, title))
        fragment = landed()
        self.assertNotEqual(fragment, "")
        self.page.reload(wait_until="load")
        self.page.wait_for_function(_REFERENCE_MOUNTED, timeout=REFERENCE_READY)
        self.assertEqual(landed(), fragment)
        self.assertEqual(self.refused, [])
        self.assertEqual(self.errors, [])


# A region that polls and names its parts, a placeholder behind a closed
# disclosure, and one whose address answers a whole page.
_FRAGMENT_PROBE = f"""<!doctype html><html><head><title>Probe</title>
<script defer src="{settings.STATIC_URL}js/fragment.js"></script>
</head><body><main>
<section data-fragment="/probe-strip/" data-fragment-poll="1">
<details data-fragment-part="first"><summary>First</summary>as drawn</details>
<details data-fragment-part="second"><summary>Second</summary>as drawn</details>
</section>
<details data-probe="fold"><summary>More</summary>
<div data-fragment="/probe-slot/" data-fragment-load data-fragment-failure="Could not be read.">waiting</div>
</details>
<div data-fragment="/probe-page/" data-fragment-load data-fragment-failure="Could not be read.">waiting</div>
</main></body></html>"""
_PROBE_STRIP = """<section data-fragment="/probe-strip/"{poll}>
<details data-fragment-part="first"><summary>First</summary>as drawn</details>
<details data-fragment-part="second"><summary>Second</summary>read again</details>
</section>"""
_PROBE_STATE = """(failure) => ({
  first: document.querySelector('[data-fragment-part="first"]').kept === true,
  firstOpen: document.querySelector('[data-fragment-part="first"]').open,
  second: document.querySelector('[data-fragment-part="second"]').textContent,
  polling: document.querySelector('[data-fragment-poll]') !== null,
  slot: document.querySelector('[data-probe="fold"]').textContent,
  page: [...document.querySelectorAll(failure)].map((line) => line.textContent),
  titles: document.querySelectorAll('main title, main main').length,
})"""


class FragmentBrowserTests(BrowserGate):
    """The fragment primitive, in a browser: what it swaps, keeps and stops."""

    @classmethod
    def render(cls):
        with transaction.atomic():
            client = Client()
            client.force_login(get_user_model().objects.create_superuser(username="operator"))
            url = reverse("calendar:month")
            page = client.get(url)
            following = page.context["next_url"]
            cls.calendar, cls.following = url, following
            answers = {
                (url, ""): page.content.decode(),
                (following, "calendar"): client.get(following, headers={"X-Fragment": "calendar"}).content.decode(),
                (reverse("action_item_count"), ""): '{"count": 0}',
            }
            transaction.set_rollback(True)
        return answers

    def start(self, **options):
        options.setdefault("java_script_enabled", True)
        super().start(**options)
        self.errors = []
        self.asked = []
        self.strip = [' data-fragment-poll="1"', ""]
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))

    def respond(self, route):
        request = route.request
        parts = urlsplit(request.url)
        address = parts.path + (f"?{parts.query}" if parts.query else "")
        part = request.headers.get("x-fragment", "")
        self.asked.append((address, part))
        if parts.path == "/probe/":
            return route.fulfill(content_type="text/html", body=_FRAGMENT_PROBE)
        if parts.path == "/probe-strip/":
            poll = self.strip.pop(0) if self.strip else ""
            return route.fulfill(content_type="text/html", body=_PROBE_STRIP.format(poll=poll))
        if parts.path == "/probe-slot/":
            return route.fulfill(content_type="text/html", body="<p>read when opened</p>")
        if parts.path == "/probe-page/":
            return route.fulfill(content_type="text/html", body=_FRAGMENT_PROBE)
        if (address, part) in self.pages:
            return route.fulfill(content_type="text/html", body=self.pages[(address, part)])
        if parts.path.startswith(settings.STATIC_URL):
            return super().respond(route)
        return route.fulfill(status=404, body="Not in the synthetic fixture")

    def test_a_polled_region_takes_only_what_changed_and_stops_when_told(self):
        self.page.goto(f"{ORIGIN}/probe/", wait_until="load")
        self.page.evaluate(
            "() => { const first = document.querySelector('[data-fragment-part=\"first\"]');"
            " first.kept = true; first.open = true; }"
        )
        self.page.wait_for_function("() => document.querySelector('[data-fragment-poll]') === null")
        state = self.page.evaluate(_PROBE_STATE, SELECTORS["fragment_failure"])

        # The part that did not change is the same node, still open; the one
        # that did was replaced.
        self.assertTrue(state["first"])
        self.assertTrue(state["firstOpen"])
        self.assertEqual(state["second"].strip(), "Secondread again")
        asked = self.asked.count(("/probe-strip/", ""))
        self.page.wait_for_timeout(2500)
        self.assertEqual(self.asked.count(("/probe-strip/", "")), asked)
        self.assertEqual(self.errors, [])

    def test_a_placeholder_waits_for_its_disclosure_and_refuses_a_whole_page(self):
        self.page.goto(f"{ORIGIN}/probe/", wait_until="load")
        self.page.wait_for_selector(SELECTORS["fragment_failure"])
        state = self.page.evaluate(_PROBE_STATE, SELECTORS["fragment_failure"])

        self.assertNotIn(("/probe-slot/", ""), self.asked)
        self.assertEqual(state["page"], ["Could not be read."])
        self.assertEqual(state["titles"], 0)

        self.page.locator(SELECTORS["fragment_fold"]).click()
        self.page.wait_for_function(
            "() => document.querySelector('[data-probe=\"fold\"]').textContent.includes('read when opened')"
        )
        self.assertEqual(self.asked.count(("/probe-slot/", "")), 1)
        self.assertEqual(self.errors, [])

    def test_paging_the_calendar_asks_for_its_part_and_keeps_the_page(self):
        self.page.goto(f"{ORIGIN}{self.calendar}", wait_until="load")
        was = self.page.locator(SELECTORS["calendar_period"]).text_content()
        self.page.evaluate("() => { window.kept = true; }")
        self.page.locator(SELECTORS["calendar_next"]).focus()
        self.page.keyboard.press("Enter")
        self.page.wait_for_function(
            "([period, was]) => document.querySelector(period).textContent !== was",
            arg=[SELECTORS["calendar_period"], was],
        )

        self.assertIn((self.following, "calendar"), self.asked)
        self.assertTrue(self.page.evaluate("() => window.kept === true"))
        self.assertTrue(self.page.url.endswith(self.following))
        # The keyboard is where it was: on the control that was pressed.
        self.assertEqual(self.page.evaluate("() => document.activeElement.getAttribute('rel')"), "next")
        self.assertEqual(self.errors, [])
