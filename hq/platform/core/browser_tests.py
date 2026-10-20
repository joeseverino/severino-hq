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
import re
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.staticfiles import finders
from django.db import transaction
from django.test import Client, SimpleTestCase
from django.urls import reverse
lazy from django.template import Context, Template
lazy from django.template.loader import render_to_string

from hq.platform.core.tests.test_browser_dense_fixtures import DENSE_PAGES, build_dense_estate, render_dense_pages
from hq.platform.core.tests.test_browser_fixtures import PAGES, build_estate, render_pages
lazy from hq.platform.application.asks import Ask
lazy from hq.platform.application.pages import Page, PageAction

# The phone HQ is read on: its viewport in CSS pixels.
PHONE_SCREEN = (402, 874)
WIDTHS = (320, PHONE_SCREEN[0], 768, 1280)
# A phone, a tablet held upright, a laptop: where only a table may scroll sideways.
OVERFLOW_WIDTHS = (PHONE_SCREEN[0], 820, 1360)
# Every page, from the sparse estate and the dense one. The overflow and
# density checks run all of them at OVERFLOW_WIDTHS.
ALL_PAGES = (*PAGES, *DENSE_PAGES)
# The pages that draw a route, sparse and dense.
ROUTE_PAGES = ("service", "connections", "dense/service", "dense/connections")
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
AUDIT = Path(__file__).with_name("browser_audit.js")

SELECTORS = {
    # The fragment primitive: a placeholder's failure line, the disclosure
    # holding one, and the calendar it pages in place.
    "fragment_failure": "main > [data-fragment-load] > .notice",
    "fragment_fold": "details:has(> [data-fragment-load]) > summary",
    "calendar_period": "#calendar-period",
    "calendar_next": '#calendar a[rel="next"]',
    "decisions": "[data-attention-item]",
    # A family's toggle, the row toggle, and a row's own dismiss.
    "decision_family": "[data-queue-family] [data-fold-toggle]",
    "row_toggle": "[data-row-toggle]",
    "decision_dismiss": "[data-triage] button",
    "decision_title": ".attention-title",
    "decision_steps": ".resolution-workflow",
    "decision_detail": ".attention-detail",
    "decision_actions": ".queue-act .attention-actions",
    "highlights": ".dashboard-highlights > .highlight-card",
    # Each reading in the dashboard's row: a mark, then its lines.
    "readings": ".dash-readings .glance-panel-head",
    "patterns": ".dashboard-patterns > .card",
    # What reads as one unit and so never breaks across lines: a state, a
    # time.
    "one_line": ".pill, td time",
    # A control one line tall, drawn on its own rather than inside a label
    # that is the control a reader sees.
    "line_control": (
        "input:not([type=checkbox], [type=radio], [type=hidden], [type=file], [type=range], [type=color],"
        " [type=submit], [type=button]):not(label *), select:not(label *)"
    ),
    # A form's field: one label, its control and its help.
    "field": ".field",
    # A row of controls that wraps when its screen is narrow.
    "wrapping_bar": ".filter-bar, .table-toolbar, .table-filters",
    # Everything pressed: a button, a link drawn as one, a disclosure's handle.
    "control": "button, .btn, .icon-link, summary",
    # Anything in a table cell: its words stay inside its own box, or they are
    # drawn across the next column while every box measures as in place.
    "cell_content": "main td *, main th *",
    # What a control says about the work it asked for, drawn under it.
    "spoken": ".ask-said",
    # An identifier: written through `|breakable` (an entity's name is), or in
    # code. A code block is text laid out by its own rules, and a table lays
    # out its own identifiers.
    "token": ":is(code, kbd, samp, .token, [data-entity]):not(pre *, .data-table *)",
    # What a heading says beside its title: a count, an age, where it is read.
    "heading_aside": ".heading-aside",
    # A page's title, and the controls that share its head.
    "page_head": ".page-head",
    "page_title": ".page-head h1",
    "page_actions": ".page-actions",
    # What holds the content and so stays inside its gutter: a card, a block
    # of code.
    "gutter_bound": "pre, .card, .boxed-facts",
    # The box built to scroll a table sideways.
    "table_scroll": ".table-scroll",
    # The boxes allowed to scroll sideways: a table's, and a block of code,
    # whose lines are as written.
    "sideways": ".table-scroll, pre",
    # A table whose rows can be chosen, which the table script wires up last.
    "selectable_table": "[data-selectable-table]",
    # What a parser leaves where a <p> was closed early by a block inside it.
    "broken_paragraph": "main p:empty:not([hidden])",
    # A box that holds content: a card, a table's frame.
    "frame": ".card, .table-scroll",
    # What reaches across a frame besides its text.
    "frame_content": "img, svg, canvas, button, .btn, input, select, pre",
    # Frames by design: a control, a chip, a diagram's nodes.
    "frame_exempt": ".btn, .pill, .topology-node, label",
    # Everything inside a menu or a dialog, which float over the page.
    "frame_exempt_within": "dialog, [popover], [data-menu]",
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
    # A band, and what in it is not a cell: a dialog a cell opens, its note.
    "band": (
        ":is(.control-summary, .service-band, .fact-band, .insight-grid, .sweep-grid, .machine-telemetry-metrics)"
    ),
    "band_aside": "dialog, .band-meta",
    # A route's hops, and the map's lanes and the box that holds them.
    "route": ".request-path",
    # A dashboard queue card, and what can be pressed at its end.
    "queue_card": ".attention-row",
    "queue_card_end": ".attention-end",
    "route_icon": ".hop-icon",
    # Every route that draws a line through its stops, and the mark on the
    # line at each stop: the route strip and the connection panel's path.
    "route_line": ":is(.request-path, .conn-path > .conn-layers)",
    "route_mark": ":is(.hop-icon, .conn-tick)",
    # A check in the connection panel: what it says, then what it was read from.
    "check_statement": ".conn-layer-label",
    "check_evidence": ".conn-layer-evidence",
    # A value a disclosure's handle names.
    "summary_value": "main summary code",
    "map": ".topology-map",
    "map_lane": ".topology-lane",
    "map_node": ".topology-node",
    # A band's cells. Stats inside a card are the one band laid out with real
    # gaps instead of padded cells, so the KPI band is not listed.
    "band_cell": (
        ":is(.control-summary, .service-band, .fact-band, .insight-grid,"
        " .connection-control-grid, .sweep-grid,"
        " .machine-telemetry-metrics) > *"
    ),
}
# WCAG AA: body text, and text large enough to need less (24px, or 18.66px bold).
TEXT_CONTRAST = 4.5
LARGE_TEXT_CONTRAST = 3.0
# The least a dividing line may differ from its surface. The faint line token
# on a card is 1.15:1, the plain one on a tinted panel 1.08:1; below this a
# line is the surface's own colour.
LINE_CONTRAST = 1.07

# Structural rules from browser_audit.js that hold on every page at
# every width. A card with a band of empty frame under its last line
# (empty-card-bottom) is one: a card is as tall as what it holds. The audit's
# other density heuristics (card-too-wide, row-height-hole, horizontal-slack)
# judge proportions that depend on how much data a page has, so they stay a
# by-eye tool rather than a gate.
AUDIT_RULES = {
    "unwanted-scrollbar",
    "clipped-content",
    "empty-card-bottom",
    "uneven-controls",
    "dead-tooltip",
    "seam-mismatch",
    "sentence-in-a-chip-row",
}
_AUDIT = (
    "async () => { const audit = "
    + AUDIT.read_text(encoding="utf-8").strip()
    + ";\nreturn await audit({ evaluate: (probe) => probe() }); }"
)

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

_HEAD_ON_ONE_LINE = """([leadSelector, titleSelector, headSelector]) => {
  // A narrow head is one line: the title and its lead control, nothing
  // between them and the lede. The control takes a row of its own only when
  // the title's widest word or name could not sit whole beside the controls.
  const lead = document.querySelector(leadSelector);
  const title = document.querySelector(titleSelector);
  if (!lead || !title) return null;
  const a = lead.getBoundingClientRect(), t = title.getBoundingClientRect();
  if (a.top < t.bottom && a.bottom > t.top) return true;
  const head = title.closest(headSelector);
  const style = getComputedStyle(head);
  const room = head.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
  const kept = title.getAttribute('style');
  title.style.flex = 'none';
  title.style.width = 'min-content';
  const narrowest = title.getBoundingClientRect().width;
  if (kept === null) title.removeAttribute('style'); else title.setAttribute('style', kept);
  return narrowest + parseFloat(style.columnGap) + lead.parentElement.getBoundingClientRect().width > room + 1;
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

_WRAPPED_PHRASES = (
    "(selector) => {"
    + _DESCRIBE
    + """
  // A pill or a time is one phrase: no taller than the same element holding one letter.
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

# What the browser paints, as an opaque colour: any colour syntax read back
# through a canvas pixel, a translucent one composited over what is behind it,
# and the WCAG contrast ratio between two.
_COLOUR = """
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
"""

_CONTRAST = (
    "([scope, body, large]) => {"
    + _DESCRIBE
    + _COLOUR
    + """
  // Every visible element that holds text of its own, measured against the
  // colour actually behind it: the nearest ancestor that paints a background,
  // with translucent layers composited down to an opaque one. A colour
  // written for one palette only is what this catches: a light-mode ink left
  // on a dark card reads as nothing, and no other check looks at colour.
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


# A line drawn to separate things (a divider, a frame's edge) is seen against
# the surface it is drawn on. A faint token on a tinted panel is a line in the
# panel's own colour: the rows it was meant to part run together.
_FAINT_LINES = (
    "([scope, least]) => {"
    + _DESCRIBE
    + _COLOUR
    + """
  const found = [];
  for (const root of document.querySelectorAll(scope)) {
    for (const el of root.querySelectorAll('*')) {
      if (!el.checkVisibility()) continue;
      const style = getComputedStyle(el);
      for (const side of ['Top', 'Right', 'Bottom', 'Left']) {
        if (parseFloat(style[`border${side}Width`]) < 1 || ['none', 'hidden'].includes(style[`border${side}Style`])) continue;
        const colour = rgba(style[`border${side}Color`]);
        if (colour[3] === 0) continue;
        // A box that paints its own fill is edged against what is outside it,
        // by its border or by the fill itself.
        const fill = rgba(style.backgroundColor);
        const filled = fill[3] > 0 && el.parentElement;
        const behind = filled ? background(el.parentElement) : background(el);
        const measured = Math.max(ratio(over(colour, behind), behind), filled ? ratio(over(fill, behind), behind) : 0);
        if (measured < least) found.push(`${describe(el)} ${side.toLowerCase()} ${measured.toFixed(2)}:1 on ${behind.slice(0, 3).map(Math.round)}`);
      }
    }
  }
  return [...new Set(found)].slice(0, 10);
}"""
)


# A route's mark (a hop's icon, a check's tick) holds its glyph in its middle.
# A glyph keeps the line height of the row around it unless its mark sets its
# own, and a line taller than the mark pushes the glyph off its centre.
_OFF_CENTRE_MARKS = (
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
    if (Math.abs(dy) > 1 || Math.abs(dx) > 1) found.push(`${describe(el)}: glyph ${dx.toFixed(1)}, ${dy.toFixed(1)} off its centre`);
  }
  return [...new Set(found)].slice(0, 10);
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
  return found.slice(0, 10);
}"""
)


# Opening a disclosure adds what it holds after its summary and moves nothing
# before it: the summary pressed stays under the pointer that pressed it.
_MOVING_SUMMARIES = (
    "() => {"
    + _DESCRIBE
    + """
  const found = [];
  for (const details of document.querySelectorAll('main details:not([open])')) {
    const summary = details.querySelector(':scope > summary');
    if (!summary || !summary.checkVisibility()) continue;
    const before = box(summary);
    details.open = true;
    const after = box(summary);
    details.open = false;
    if (Math.abs(after.top - before.top) > 1 || Math.abs(after.left - before.left) > 1) {
      found.push(`${describe(details)}: "${summary.textContent.trim().slice(0, 30)}" moved ${Math.round(after.left - before.left)}, ${Math.round(after.top - before.top)} when opened`);
    }
  }
  return found.slice(0, 10);
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


# Every style rule's selector the browser kept, nested rules included. A rule
# whose selector does not parse is dropped whole and silently: the stylesheet
# still loads and nothing reports it.
_KEPT_SELECTORS = """() => {
  const sheet = [...document.styleSheets].find((s) => s.href && s.href.includes('/css/app.css'));
  const found = [];
  const walk = (rules) => {
    for (const rule of rules) {
      if (rule.selectorText !== undefined) found.push(rule.selectorText);
      if (rule.cssRules) walk(rule.cssRules);
    }
  };
  walk(sheet.cssRules);
  return found;
}"""


def authored_selectors(css: str) -> list[str]:
    """Every style rule's selector as ``app.css`` writes it, nested rules
    included: the text before each ``{`` that does not open an at-rule."""

    css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    found, start, quote = [], 0, ""
    for at, char in enumerate(css):
        if quote:
            quote = "" if char == quote else quote
        elif char in "\"'":
            quote = char
        elif char in "{};":
            if char == "{":
                header = css[start:at].strip()
                # A keyframe's step is not a selector.
                if header and not header.startswith("@") and not re.fullmatch(r"[\d.%,\s]+|from|to", header):
                    found.append(header)
            start = at + 1
    return found


def _opaque(colour: str) -> tuple[int, ...]:
    """A computed colour's red, green and blue, without its opacity, as 0-255:
    ``rgb(255, 255, 255)`` and ``color(srgb 1 1 1 / 0.8)`` are one colour."""

    numbers = [float(n) for n in re.findall(r"[\d.]+", colour.split("/")[0])]
    if colour.startswith("color("):
        return tuple(round(n * 255) for n in numbers[:3])
    return tuple(round(n) for n in numbers[:3])


def _plain_selector(selector: str) -> str:
    """A selector as the browser writes it back: no whitespace, double quotes,
    no ``*`` before what already names an element, and every attribute value
    quoted."""

    plain = re.sub(r"\s+", "", selector).replace("'", '"')
    plain = re.sub(r"\*(?=[:.\[#])", "", plain)
    plain = plain.replace("(odd)", "(2n+1)").replace("(even)", "(2n)")
    return re.sub(r'(\[[^\]=]+?[~|^$*]?=)([^"\]]+)\]', r'\1"\2"]', plain)


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
  return [...new Set(found)].slice(0, 10);
}"""
)


# The first table on a page as a phone lays it out: whether it runs past its
# box, how wide each showing column is, how tall each record's row is, and how
# many rows show their toggle. The line an open row adds is made by tables.js,
# so no template renders it and it is found by attribute.
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
    toggles: [...table.querySelectorAll('tbody button[aria-expanded]')].filter((el) => el.checkVisibility()).length,
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
# A frame has been drawn and nothing is moving.
_PAINTED = "() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)))"
_STILL = """() => document.getAnimations()
  .every((motion) => motion.playState !== 'running' || motion.effect?.getComputedTiming().iterations === Infinity)"""
_NAME_EDGES = """(scroll) => [...document.querySelectorAll('main ' + scroll + ' > table')]
  .map((table) => [table, [...table.querySelectorAll('tbody button[aria-expanded]')].find((el) => el.checkVisibility())])
  .filter(([table, toggle]) => table.checkVisibility() && toggle)
  .map(([table, toggle]) => {
    const column = toggle.closest('td, th').cellIndex;
    const edges = [...table.querySelectorAll('tbody > tr:not([data-row-detail])')]
      .map((row) => row.children[column])
      // A gutter that holds only the toggle has no name: the name starts the next cell.
      .map((cell) => (cell && cell.matches('.row-gutter') && !cell.textContent.trim() ? cell.nextElementSibling : cell))
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
  // given a word draws the word over whatever stands beside it. One with no
  // words has no label to spill: what reaches past it is a hit area.
  const found = [];
  for (const el of document.querySelectorAll(selector)) {
    if (!el.checkVisibility() || !el.textContent.trim()) continue;
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
# The page as the server drew it, with its scripts held back: parsed, styled
# and in its own face, which is what a reader is shown first.
_DRAWN = """() => document.readyState !== 'loading'
  && [...document.querySelectorAll('link[rel=stylesheet]')].every((link) => link.sheet)
  && document.fonts.status === 'loaded'"""
# The scripts the parser stops for, which are drawn before the page is.
_PARSER_SCRIPTS = "() => [...document.scripts].filter((s) => s.src && !s.defer && !s.async).map((s) => s.src)"
# Where every element that shows stands on the page as first drawn, kept on
# the window for _MOVED_SINCE_DRAWN to read once the scripts have run.
_DRAWN_BOXES = """() => {
  const boxes = new Map();
  for (const el of document.body.querySelectorAll('*')) {
    const r = el.getBoundingClientRect();
    if (!el.checkVisibility() || (!r.width && !r.height)) continue;
    boxes.set(el, [r.left + scrollX, r.top + scrollY, r.width, r.height]);
  }
  window.drawnBoxes = boxes;
  return boxes.size;
}"""
# Every element that still shows and no longer stands where it was drawn. A
# child carried by its parent, the same distance and at the same size, is the
# parent's move and is not named again.
_MOVED_SINCE_DRAWN = (
    "() => {"
    + _DESCRIBE
    + """
  const drawn = window.drawnBoxes;
  const now = (el) => { const r = el.getBoundingClientRect();
    return [r.left + scrollX, r.top + scrollY, r.width, r.height]; };
  const change = (el) => now(el).map((value, index) => value - drawn.get(el)[index]);
  const still = (moved) => moved.every((by) => Math.abs(by) < 0.5);
  const found = [];
  for (const [el, was] of drawn) {
    if (!el.isConnected || !el.checkVisibility()) continue;
    const moved = change(el);
    if (still(moved)) continue;
    const parent = el.parentElement;
    if (drawn.has(parent) && parent.checkVisibility() && still(moved.slice(2))) {
      const by = change(parent);
      if (still([moved[0] - by[0], moved[1] - by[1]])) continue;
    }
    const words = (el.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 30);
    const at = (box) => box.map(Math.round).join(',');
    found.push(`${describe(el)} "${words}" ${at(was)} -> ${at(now(el))}`);
  }
  return found;
}"""
)
# Colours transition between palettes, so contrast is measured after the
# transition ends.
_FINISH_TRANSITIONS = """() => document.getAnimations().forEach((a) => { try { a.finish(); } catch { a.cancel(); } })"""

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
  // Each control as it was drawn, to put back once it has been measured.
  const drawn = asks.map((ask) => [ask, ask.dataset.askState,
    ...[...ask.querySelectorAll('[data-ask-note], [data-ask-elapsed]')].map((el) => [el, el.hidden, [...el.childNodes]])]);
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
  // Back as it loaded, so the next probe reads the page the server drew.
  for (const [ask, state, ...parts] of drawn) {
    if (state === undefined) delete ask.dataset.askState; else ask.dataset.askState = state;
    for (const [el, hidden, nodes] of parts) { el.replaceChildren(...nodes); el.hidden = hidden; }
  }
  await new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)));
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
# Press the first toggle that shows in the page's first table, and say which
# of its records (counted as _PHONE_TABLE counts them) it opened.
_PRESS_FIRST_ROW_TOGGLE = """(scroll) => {
  const table = document.querySelector('main ' + scroll).querySelector('table');
  const records = [...table.querySelectorAll('tbody > tr:not([data-row-detail]):not(:has(> th[scope="rowgroup"]))')];
  const toggle = [...table.querySelectorAll('tbody button[aria-expanded]')].find((el) => el.checkVisibility());
  toggle.click();
  return records.indexOf(toggle.closest('tr'));
}"""


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


def reading_problems(readings):
    """What breaks the readings' one shape: a line under the mark or cut short,
    a mark off its lead, leads of two sizes, rows of different counts, second
    lines at different depths."""

    found = []
    for reading in readings:
        found.extend(f"under the mark: {line}" for line in reading["under_the_mark"])
        found.extend(f"cut short: {line}" for line in reading["cut"])
        if abs(reading["mark"] - reading["lead"]) > 3:
            found.append(f"mark {reading['mark']} beside a lead at {reading['lead']}")
    if len({reading["size"] for reading in readings}) > 1:
        found.append(f"leads in {sorted({reading['size'] for reading in readings})}")
    rows = {}
    for reading in readings:
        rows.setdefault(round(reading["top"]), []).append(reading)
    if len({len(row) for row in rows.values()}) > 1:
        found.append(f"rows of {[len(row) for row in rows.values()]}")
    if len({reading["second"] for reading in readings if reading["second"]}) > 1:
        found.append(f"second lines at {sorted({r['second'] for r in readings if r['second']})}")
    return found


# A band of up to six cells lays out as whole even rows: every row it wraps
# to holds as many cells as the first (Design.md, "One band").
_UNEVEN_BANDS = (
    "([band, aside]) => {"
    + _DESCRIBE
    + """
  const found = [];
  for (const el of document.querySelectorAll('main ' + band)) {
    if (!el.checkVisibility()) continue;
    const cells = [...el.children].filter((cell) => !cell.matches(aside) && cell.checkVisibility());
    if (cells.length < 2 || cells.length > 6) continue;
    const rows = new Map();
    for (const cell of cells) {
      const top = Math.round(box(cell).top);
      rows.set(top, (rows.get(top) || 0) + 1);
    }
    const counts = [...rows.values()];
    if (new Set(counts).size > 1) found.push(`${describe(el)}: ${counts.join(' + ')}`);
  }
  return found;
}"""
)

# One-line controls side by side are one height: a select beside a field
# beside a date, in one row of a form or of a bar. A control in a field is in
# the row its field is; any other is in the row its own top is.
_UNEVEN_CONTROL_ROWS = (
    "([control, field]) => {"
    + _DESCRIBE
    + """
  const rows = new Map();
  for (const el of document.querySelectorAll('main ' + control)) {
    if (!el.checkVisibility()) continue;
    const top = Math.round(box(el.closest(field) || el).top);
    const at = el.closest(field)?.parentElement || el.parentElement;
    const key = describe(at) + '@' + top;
    rows.set(key, [...(rows.get(key) || []), el]);
  }
  const found = [];
  for (const [key, controls] of rows) {
    const heights = controls.map((el) => Math.round(box(el).height));
    if (Math.max(...heights) - Math.min(...heights) > 1) {
      found.push(`${key}: ${controls.map((el, i) => describe(el) + ' ' + heights[i] + 'px').join(', ')}`);
    }
  }
  return found;
}"""
)

# A bar of controls that wraps is whole rows: each row it wraps to reaches
# its far edge, rather than a ragged run of controls of their own widths.
_RAGGED_BARS = (
    "(bar) => {"
    + _DESCRIBE
    + """
  const found = [];
  for (const el of document.querySelectorAll('main ' + bar)) {
    if (!el.checkVisibility()) continue;
    // Controls only: a count at the end of a bar is words, which keep their own width.
    const items = [...el.children].filter((child) => child.tagName !== 'P' && child.checkVisibility() && getComputedStyle(child).position !== 'absolute');
    const rows = new Map();
    for (const item of items) {
      const top = Math.round(box(item).top);
      rows.set(top, Math.max(rows.get(top) || 0, box(item).right));
    }
    if (rows.size < 2) continue;
    const style = getComputedStyle(el);
    const edge = box(el).right - parseFloat(style.paddingRight) - parseFloat(style.borderRightWidth);
    const short = [...rows.values()].filter((end) => edge - end > 1);
    if (short.length) found.push(`${describe(el)}: ${short.length} of ${rows.size} rows stop short, by ${short.map((end) => Math.round(edge - end)).join(', ')}px`);
  }
  return found;
}"""
)

# A route is one row across its box or one column down it, never a row that
# wrapped with its line running off the edge.
_WRAPPED_ROUTES = (
    "(route) => {"
    + _DESCRIBE
    + """
  const found = [];
  for (const el of document.querySelectorAll('main ' + route)) {
    const hops = [...el.children].filter((hop) => hop.checkVisibility()).map(box);
    if (hops.length < 2) continue;
    const tops = new Set(hops.map((hop) => Math.round(hop.top)));
    const lefts = new Set(hops.map((hop) => Math.round(hop.left)));
    if (tops.size > 1 && lefts.size > 1) found.push(`${describe(el)}: ${tops.size} rows of hops`);
  }
  return found;
}"""
)

# A route across its box spaces its icons evenly: no gap between two hops'
# icons is a pixel longer than another.
_UNEVEN_ROUTES = (
    "([route, icon]) => {"
    + _DESCRIBE
    + """
  const found = [];
  for (const el of document.querySelectorAll('main ' + route)) {
    const icons = [...el.querySelectorAll(icon)].filter((mark) => mark.checkVisibility()).map(box);
    if (icons.length < 3 || new Set(icons.map((mark) => Math.round(mark.top))).size > 1) continue;
    const centres = icons.map((mark) => mark.left + mark.width / 2);
    const gaps = centres.slice(1).map((centre, i) => centre - centres[i]);
    if (Math.max(...gaps) - Math.min(...gaps) > 1) found.push(`${describe(el)}: gaps ${gaps.map(Math.round).join(', ')}`);
  }
  return found;
}"""
)

# The dashboard's queue cards end their buttons at one edge: no card's end
# stops more than a pixel short of another's. The full queue is a table, whose
# columns hold its rows to one edge.
# A route's line runs from the centre of its first stop's mark to the centre
# of its last, opened or closed: each stop draws its stretch as `::before`,
# read back from its computed box.
_ROUTE_LINE_ENDS = (
    "([route, mark]) => {"
    + _DESCRIBE
    + """
  const found = [];
  for (const el of document.querySelectorAll('main ' + route)) {
    const stops = [...el.children].filter((stop) => stop.checkVisibility());
    if (stops.length < 2) continue;
    const stretches = stops.flatMap((stop) => {
      const line = getComputedStyle(stop, '::before');
      if (line.display === 'none' || ['none', 'normal'].includes(line.content)) return [];
      const at = box(stop);
      return [{top: at.top + parseFloat(line.top), left: at.left + parseFloat(line.left),
               width: parseFloat(line.width), height: parseFloat(line.height)}];
    });
    if (!stretches.length) { found.push(`${describe(el)}: no line`); continue; }
    const down = stretches[0].height > stretches[0].width;
    const centre = (stop) => { const at = box(stop.querySelector(mark));
      return down ? at.top + at.height / 2 : at.left + at.width / 2; };
    const start = Math.min(...stretches.map((s) => down ? s.top : s.left));
    const end = Math.max(...stretches.map((s) => down ? s.top + s.height : s.left + s.width));
    const first = centre(stops[0]);
    const last = centre(stops.at(-1));
    if (Math.abs(start - first) > 2 || Math.abs(end - last) > 2) {
      found.push(`${describe(el)}: line ${Math.round(start)} to ${Math.round(end)}, marks ${Math.round(first)} to ${Math.round(last)}`);
    }
  }
  return found;
}"""
)

# A check's evidence follows its statement: on the same line within a short
# gap, or on the next line starting where the statement starts.
_STRANDED_EVIDENCE = (
    "([statement, evidence]) => {"
    + _DESCRIBE
    + """
  const found = [];
  for (const el of document.querySelectorAll('main ' + evidence)) {
    const said = el.previousElementSibling;
    if (!el.checkVisibility() || !said || !said.matches(statement)) continue;
    const e = box(el);
    const s = box(said);
    const besideIt = e.top < s.bottom - 1;
    if (besideIt ? e.left - s.right > 48 : Math.abs(e.left - s.left) > 2) {
      found.push(`${describe(el)}: ${Math.round(e.left - (besideIt ? s.right : s.left))}px from its statement`);
    }
  }
  return found;
}"""
)

# What a disclosure's handle names is shown wherever the handle is.
_HIDDEN_IN_SUMMARIES = (
    "(value) => {"
    + _DESCRIBE
    + """
  return [...document.querySelectorAll(value)]
    .filter((el) => el.closest('summary').checkVisibility() && !el.checkVisibility())
    .map(describe);
}"""
)

# A section of queue cards ends every card's buttons at one edge: no card's
# end stops more than a pixel short of another's in the same section.
_RAGGED_QUEUES = (
    "([card, end]) => {"
    + _DESCRIBE
    + """
  const found = [];
  for (const list of document.querySelectorAll('main section:has(' + card + ')')) {
    const ends = [...list.querySelectorAll(card + ' > ' + end)]
      .filter((el) => el.checkVisibility()).map((el) => Math.round(box(el).right));
    if (ends.length > 1 && Math.max(...ends) - Math.min(...ends) > 1) found.push(`${describe(list)}: ends at ${[...new Set(ends)].join(', ')}`);
  }
  return found;
}"""
)

# Each character of an element's text with the line it is drawn on, for the
# rules that judge where text breaks. `chars(el)` and `room(el)`: the width of
# the box whose lines it is set in.
_LINES = r"""
const range = document.createRange();
const chars = (root) => {
  const out = [];
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    if (!node.parentElement.checkVisibility()) continue;
    for (let i = 0; i < node.length; i++) {
      range.setStart(node, i);
      range.setEnd(node, i + 1);
      const rect = [...range.getClientRects()].find((r) => r.width);
      out.push({ch: node.data[i], top: rect ? Math.round(rect.top) : null, bottom: rect ? rect.bottom : 0, width: rect ? rect.width : 0});
    }
  }
  return out;
};
const room = (el) => {
  let line = el.parentElement;
  while (getComputedStyle(line).display.startsWith('inline') || getComputedStyle(line).display === 'contents') line = line.parentElement;
  const style = getComputedStyle(line);
  return line.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
};
const broke = (list, i) => list[i].top !== null && list[i - 1].top !== null && list[i].top > list[i - 1].top + 1;
"""

# An identifier (an address, a host or record name, a path, an image) that
# fits on its line moves to the next line whole. One longer than its line
# breaks only at a seam `|breakable` marks (value_tags._BREAK_AFTER: after a
# slash, after a colon run, after a dot without a digit on both sides), and
# inside the part between two seams only when that part alone is longer than
# its line. A hyphen is no seam: a name split there reads as two names.
_TOKEN_BREAKS = (
    "(token) => {"
    + _DESCRIBE
    + _LINES
    + r"""
  const found = [];
  const digit = /\d/;
  const seamAfter = (list, i) => {
    const prev = list[i - 1].ch, next = list[i].ch, before = i > 1 ? list[i - 2].ch : '';
    return prev === '/' || (prev === ':' && next !== ':') || (prev === '.' && !(digit.test(before) && digit.test(next)));
  };
  const widthOf = (list) => list.reduce((sum, c) => sum + c.width, 0);
  for (const el of document.querySelectorAll('main ' + token)) {
    if (!el.checkVisibility() || el.parentElement.closest(token)) continue;
    const all = chars(el);
    const space = room(el);
    const text = all.map((c) => c.ch).join('');
    for (const word of text.matchAll(/\S+/g)) {
      const list = all.slice(word.index, word.index + word[0].length);
      const at = list.findIndex((c, i) => i > 0 && broke(list, i));
      if (at < 0) continue;
      const shown = (i) => `"${word[0].slice(0, i)}|${word[0].slice(i)}"`.slice(0, 90);
      if (widthOf(list) <= space - 1) {
        found.push(`${describe(el)} breaks ${shown(at)} though its ${Math.round(widthOf(list))}px fit its ${Math.round(space)}px line`);
        continue;
      }
      for (let i = 1; i < list.length; i++) {
        if (!broke(list, i) || seamAfter(list, i)) continue;
        let start = i - 1;
        while (start > 0 && !seamAfter(list, start)) start -= 1;
        let end = i;
        while (end < list.length - 1 && !seamAfter(list, end + 1)) end += 1;
        if (widthOf(list.slice(start, end + 1)) <= space - 1) {
          found.push(`${describe(el)} breaks ${shown(i)} inside a part that fits its ${Math.round(space)}px line`);
          break;
        }
      }
    }
  }
  return [...new Set(found)].slice(0, 10);
}"""
)

# A heading's aside sits beside it on one line, or starts on a line of its
# own under it: wrapped beside the heading, its lines keep the heading's line
# box and read as a loose paragraph set in the heading.
_ASIDE_BESIDE_ITS_HEADING = (
    "(aside) => {"
    + _DESCRIBE
    + _LINES
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
_SQUEEZED_TITLES = (
    "([title, actions, pageHead]) => {"
    + _DESCRIBE
    + _LINES
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

# Nothing reaches past the content's side gutter: drawn there, it touches the
# edge of the screen. `text` also holds every line of text to the box that
# holds it.
_PAST_THE_GUTTER = (
    "([selector, text]) => {"
    + _DESCRIBE
    + """
  const main = document.querySelector('main');
  const pad = getComputedStyle(main);
  const edge = main.getBoundingClientRect();
  const left = edge.left + parseFloat(pad.paddingLeft);
  const right = edge.right - parseFloat(pad.paddingRight);
  const found = [];
  const inScroller = (el) => {
    for (let up = el.parentElement; up && up !== main; up = up.parentElement) {
      if (scrolls(up)) return true;
    }
    return false;
  };
  for (const el of main.querySelectorAll(selector)) {
    if (!el.checkVisibility() || inScroller(el)) continue;
    const r = box(el);
    if (r.width < 2 || r.height < 2) continue;
    if (r.right > right + 1 || r.left < left - 1) {
      found.push(`${describe(el)} spans ${Math.round(r.left)}..${Math.round(r.right)}px, gutter ${Math.round(left)}..${Math.round(right)}px`);
    }
  }
  if (!text) return [...new Set(found)].slice(0, 15);
  const range = document.createRange();
  const walker = document.createTreeWalker(main, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const el = node.parentElement;
    if (!node.data.trim() || !el.checkVisibility() || inScroller(el)) continue;
    let block = el;
    while (block !== main && getComputedStyle(block).display.startsWith('inline')) block = block.parentElement;
    const style = getComputedStyle(block);
    if (style.overflowX !== 'visible') continue;
    const b = box(block);
    const inner = b.right - parseFloat(style.paddingRight) - parseFloat(style.borderRightWidth);
    range.selectNodeContents(node);
    const far = Math.max(...[...range.getClientRects()].map((r) => r.right));
    if (far > inner + 1) found.push(`${describe(block)}: "${node.data.trim().slice(0, 40)}" runs ${Math.round(far - inner)}px past its padding`);
  }
  return [...new Set(found)].slice(0, 15);
}"""
)

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

# A probe page: a table drawn through the table tags, as a template draws one,
# with the stylesheet and the table script.
_PROBE_PAGE = """{% load table_tags %}<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="{{ static }}css/app.css">
<script defer src="{{ static }}js/tables.js"></script>
</head><body><main>"""


def probe(markup):
    """``markup``, in the table tags, as a page."""

    return Template(_PROBE_PAGE + markup + "</main></body></html>").render(Context({"static": settings.STATIC_URL}))


# A table with a column of dashes, a column of marks with no words, and a
# column of short words: the server marks the dashes blank and they are not
# drawn, the marks are, and no word is split however the column's width rounds.
_BLANK_TABLE = """{% table_region "Runbooks" %}<table class="data-table">
<thead><tr>{% table_heading "Title" %}{% table_heading "Type" %}{% table_heading "Nothing" %}
{% table_heading "Runs" %}{% table_heading "Note" %}</tr></thead><tbody>
<tr><td>Upgrade a container to a new release</td><td>Runbook</td><td><span class="empty-value">–</span></td>
<td><span class="kpi-dots"><span class="kpi-dot is-filled" title="Succeeded"></span></span></td><td>A sentence long enough to take the room it is given and wrap.</td></tr>
<tr><td>Rotate the signing keys</td><td>Runbook</td><td>–</td>
<td><span class="kpi-dots"><span class="kpi-dot is-filled" title="Succeeded"></span></span></td><td>Another sentence that wraps in its column.</td></tr>
</tbody></table>{% endtable_region %}"""
_COLUMNS_SHOWN = """() => [...document.querySelectorAll('thead th')]
  .filter((th) => getComputedStyle(th).display !== 'none').map((th) => th.textContent.replace(/[^A-Za-z ]/g, '').trim())"""


# Two tables whose every row the server made openable. In the first a narrow
# box drops a column: one row has something in it, one has a dash there. One
# row's only clipped text is for screen readers, beside a decoration hidden
# from them, and one row's value really is cut off. In the second a row folds
# part of a list, so the gutter is kept at every width, and a row's value is
# cut off there too.
_ROW_TOGGLE_TABLES = """
{% table_region "Dropping" narrow=1 %}<table class="data-table"><thead><tr>
{% table_heading "Name" gutter=True %}{% table_heading "Value" %}{% table_heading "Note" drop=1 %}</tr></thead><tbody>
<tr data-probe="dropped"><td class="row-gutter">Dropped{% row_toggle %}</td><td>Short</td><td>Something to say</td></tr>
<tr data-probe="dropped-blank"><td class="row-gutter">Blank{% row_toggle %}</td><td>Short</td><td>–</td></tr>
<tr data-probe="screen-reader"><td class="row-gutter">Chest{% row_toggle %}</td>
<td><span class="cadence-mark is-hit" aria-hidden="true"></span>
<span class="visually-hidden">One session in the week of Aug 10, said in full</span></td><td>–</td></tr>
<tr data-probe="cut"><td class="row-gutter">Name{% row_toggle %}</td>
<td><div style="max-width: 40px; overflow: hidden; white-space: nowrap">a value far too long to fit</div></td><td>–</td></tr>
</tbody></table>{% endtable_region %}
{% table_region "Folding" %}<table class="data-table"><thead><tr>
{% table_heading "Name" gutter=True %}{% table_heading "Value" %}</tr></thead><tbody>
<tr data-probe="folded"><td class="row-gutter">Folded{% row_toggle %}</td>
<td><span class="tag">One</span><span class="muted row-less">+1</span><span class="row-more"><span class="tag">Two</span></span></td></tr>
<tr data-probe="cut-in-a-kept-gutter"><td class="row-gutter">Name{% row_toggle %}</td>
<td><div style="max-width: 40px; overflow: hidden; white-space: nowrap">a value far too long to fit</div></td></tr>
</tbody></table>{% endtable_region %}"""
_ROW_TOGGLES = """() => [...document.querySelectorAll('tbody tr')].map((row) => ({
  probe: row.dataset.probe,
  toggles: row.querySelectorAll('button[aria-expanded]').length,
  shown: [...row.querySelectorAll('button[aria-expanded]')].some((toggle) => toggle.checkVisibility()),
  beside_name: row.querySelector('button[aria-expanded]')?.parentElement === row.cells[0],
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
        # Scripts asked for while this is a list wait in it (moved_once_drawn).
        self.held = None
        self.context = self.browser.new_context(**options)
        self.context.set_default_timeout(5000)
        self.context.route("**/*", self.respond)
        self.page = self.context.new_page()

    def settle(self):
        """Wait until the page has drawn what it just did and nothing is moving."""

        self.page.evaluate(_PAINTED)
        self.page.wait_for_function(_STILL)

    def holds(self, route):
        """Whether a script is being held back, and so not answered yet."""

        if self.held is None or route.request.resource_type != "script":
            return False
        self.held.append(route)
        return True

    def respond(self, route):
        if not self.holds(route):
            self.answer(route)

    def answer(self, route):
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
        self.page.set_viewport_size(viewport(width))
        self.page.goto(f"{ORIGIN}/{name}/", wait_until="load")

    def moved_once_drawn(self, url, width, settled):
        """What moves on ``url`` after it is first drawn.

        The page is drawn with its deferred scripts held back, which is what a
        reader sees before they run, and every box that shows is noted. A
        script the parser waits for is part of that drawing, so it is let
        through. Then the rest run, ``settled`` waits for what they start, and
        every element that stands somewhere else is named.
        """

        self.held = []
        self.page.set_viewport_size(viewport(width))
        self.page.goto(url, wait_until="commit")
        for _frame in range(600):
            if self.page.evaluate(_DRAWN):
                break
            parsing = set(self.page.evaluate(_PARSER_SCRIPTS))
            waiting = [route for route in self.held if route.request.url in parsing]
            for route in waiting:
                self.held.remove(route)
                self.answer(route)
            self.page.evaluate(_SETTLED)
        else:
            raise AssertionError(f"{url} at {width}px was never drawn")
        self.page.evaluate(_DRAWN_BOXES)
        held, self.held = self.held, None
        for route in held:
            self.answer(route)
        settled()
        return self.page.evaluate(_MOVED_SINCE_DRAWN)


def viewport(width):
    return {"width": width, "height": PHONE_SCREEN[1] if width <= PHONE else 900}


def others(rows, opened):
    """Every row's height but the one that opened."""

    return rows[:opened] + rows[opened + 1 :]


# Heads whose lead control is one that says what it is doing beside itself,
# drawn by the head's own partial: a one-word title, two words, and a name.
SPEAKING_HEADS = {
    "head/word": "Reports",
    "head/words": "Two words",
    "head/name": "example-host.example.com",
}
# What a pressed control says about its work, short to long, and where.
SAYINGS = ("Reading", "Reading example-host", "That could not be done. Try again in a minute.")
SPEAKING_WIDTHS = (320, PHONE_SCREEN[0], 1360)


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
    """The layout rules over the stress estate.

    Each page is opened once per width and per configuration (script off,
    script on), and every rule that holds there is read off that one load,
    each in a subtest named for the rule, the page and the width. The rules
    that change the page (open its disclosures, dispatch a resize) run after
    the ones that only read it.
    """

    @classmethod
    def render(cls):
        with transaction.atomic():
            pages = render_pages(build_estate())
            transaction.set_rollback(True)
        with transaction.atomic():
            pages |= render_dense_pages(build_dense_estate())
            transaction.set_rollback(True)
        return pages | speaking_heads() | shared_parts()

    def loaded(self, name, width, **where):
        """Open ``name`` at ``width``; one that does not load fails once, not once per rule."""

        opened = False
        # subTest records a failure and carries on, so `opened` stays False.
        with self.subTest(page=name, width=width, **where):
            self.open(name, width)
            opened = True
        return opened

    def hold(self, name, width, rules, **where):
        """Hold each rule on the page as it stands, one subtest each, in order."""

        for rule in rules:
            with self.subTest(rule=rule.__name__, page=name, width=width, **where):
                rule(name)

    def in_scheme(self, scheme):
        """The same load in the other palette. Without script no animation
        frame runs, so the colours are not waited for: they are finished."""

        self.page.emulate_media(color_scheme=scheme)
        self.page.evaluate(_FINISH_TRANSITIONS)

    # Without script: the page as the server drew it. Layout must hold before
    # progressive enhancement.

    def unscripted_reads(self, name, width):
        """The rules that only read the page: on the sparse estate's pages at
        every WIDTHS width, and on every page at every OVERFLOW_WIDTHS width."""

        rules = []
        if name in PAGES and width in WIDTHS:
            rules += [
                self.every_stylesheet_loads,
                self.nothing_escapes_the_page_sideways,
                self.tables_scroll_inside_their_own_container,
                self.grid_rows_end_together,
                self.a_pill_never_wraps,
                self.nothing_overlaps,
                self.layout_audit_structural_rules,
            ]
        if width in OVERFLOW_WIDTHS:
            rules += self.overflow_reads(name)
        if width == PHONE_SCREEN[0]:
            rules.append(self.a_narrow_head_keeps_its_lead_control_beside_the_title)
        if width == OVERFLOW_WIDTHS[-1]:
            rules.append(self.a_wide_frame_is_filled)
        return rules

    def overflow_reads(self, name):
        rules = [
            self.no_block_breaks_a_paragraph,
            self.nothing_is_framed_inside_a_frame,
            self.words_stay_inside_the_box_that_holds_them,
            self.only_a_table_scrolls_sideways,
            self.nothing_runs_out_of_its_table_cell,
            self.a_table_frame_spans_its_box,
            self.a_table_row_stays_within_its_height_budget,
            self.no_column_is_narrower_than_its_words,
            self.a_link_sits_on_the_line_it_continues,
            self.a_row_has_one_disclosure,
            self.a_band_cell_keeps_its_padding,
            self.a_band_lays_out_as_whole_rows,
            self.a_queue_ends_its_cards_at_one_edge,
            self.a_route_line_runs_from_its_first_stop_to_its_last,
            self.a_mark_holds_its_glyph_in_its_middle,
            self.a_checks_evidence_follows_its_statement,
            self.nothing_a_summary_names_is_hidden,
            self.a_token_breaks_only_where_it_may,
            self.an_aside_wraps_only_under_its_heading,
            self.a_title_is_not_split_beside_its_controls,
            self.code_and_cards_keep_inside_the_gutter,
            self.controls_in_a_row_share_one_height,
            self.a_wrapped_bar_fills_its_rows,
        ]
        if name in DENSE_PAGES:
            rules.append(self.dense_pages_keep_to_their_boxes)
        if name in ROUTE_PAGES:
            rules.extend((self.a_route_is_one_row_or_one_column, self.a_route_across_spaces_its_hops_evenly))
        return rules

    def unscripted_openings(self, name, width):
        """The rules that open the page's disclosures, after every read."""

        rules = []
        if width in OVERFLOW_WIDTHS:
            rules.append(self.a_filled_box_keeps_its_padding)
        rules.append(self.open_every_disclosure)
        if name in PAGES and width in WIDTHS:
            rules.append(self.nothing_escapes_the_page_sideways_with_every_disclosure_open)
        if width in OVERFLOW_WIDTHS:
            rules += [
                self.only_a_table_scrolls_sideways_with_every_disclosure_open,
                self.a_route_line_runs_from_its_first_stop_to_its_last,
                self.a_checks_evidence_follows_its_statement,
                self.nothing_a_summary_names_is_hidden,
                self.a_line_shows_on_its_surface,
            ]
        return rules

    def test_every_page_keeps_the_layout_rules_without_script(self):
        for name in ALL_PAGES:
            for width in sorted({*OVERFLOW_WIDTHS, *(WIDTHS if name in PAGES else ())}):
                if not self.loaded(name, width):
                    continue
                self.hold(name, width, self.unscripted_reads(name, width))
                if width in OVERFLOW_WIDTHS:
                    self.in_scheme("dark")
                    self.hold(name, width, [self.text_reads_in_the_dark_theme, self.a_line_shows_on_its_surface])
                    self.in_scheme("light")
                self.hold(name, width, self.unscripted_openings(name, width))

    def test_a_shared_part_escapes_nothing_sideways(self):
        """A component filled the way an extension fills it, closed and opened."""

        for name in SHARED_PARTS:
            for width in WIDTHS:
                if self.loaded(name, width):
                    self.hold(
                        name,
                        width,
                        [
                            self.nothing_escapes_the_page_sideways,
                            self.open_every_disclosure,
                            self.nothing_escapes_the_page_sideways_with_every_disclosure_open,
                        ],
                    )

    def every_stylesheet_loads(self, _name):
        sheets = self.page.evaluate(_STYLESHEETS)
        self.assertGreater(sheets["links"], 0)
        self.assertEqual(sheets["loaded"], sheets["links"])
        self.assertEqual(sheets["empty"], [])

    def nothing_escapes_the_page_sideways(self, _name):
        self.assertEqual(self.page.evaluate(_ESCAPES), [])

    def open_every_disclosure(self, _name):
        """Every disclosure and popover open, for the rules that follow."""

        self.page.evaluate("() => document.querySelectorAll('details').forEach((d) => { d.open = true; })")

    def nothing_escapes_the_page_sideways_with_every_disclosure_open(self, _name):
        self.assertEqual(self.page.evaluate(_ESCAPES), [])

    def no_block_breaks_a_paragraph(self, _name):
        # A block written inside a <p> closes it early and leaves an empty <p>
        # where its end tag was: the rest of the line falls out of it.
        self.assertEqual(self.page.locator(SELECTORS["broken_paragraph"]).count(), 0)

    def nothing_is_framed_inside_a_frame(self, _name):
        self.assertEqual(
            self.page.evaluate(_NESTED_FRAMES, [SELECTORS["frame_exempt"], SELECTORS["frame_exempt_within"]]),
            [],
        )

    def a_narrow_head_keeps_its_lead_control_beside_the_title(self, _name):
        self.assertIn(
            self.page.evaluate(
                _HEAD_ON_ONE_LINE, [SELECTORS["head_lead"], SELECTORS["head_title"], SELECTORS["page_head"]]
            ),
            (None, True),
        )

    def words_stay_inside_the_box_that_holds_them(self, _name):
        self.assertEqual(self.page.evaluate(_SPILLED_LABELS, SELECTORS["control"]), [])
        self.assertEqual(self.page.evaluate(_SPILLED_LABELS, SELECTORS["cell_content"]), [])

    def only_a_table_scrolls_sideways(self, _name):
        """At a phone, tablet and laptop width; again with every disclosure open."""

        self.assertEqual(self.page.evaluate(_SIDEWAYS, SELECTORS["sideways"]), [])

    def only_a_table_scrolls_sideways_with_every_disclosure_open(self, _name):
        self.assertEqual(self.page.evaluate(_SIDEWAYS, SELECTORS["sideways"]), [])

    def nothing_runs_out_of_its_table_cell(self, _name):
        self.assertEqual(self.page.evaluate(_CELLS), [])

    def a_table_row_stays_within_its_height_budget(self, _name):
        self.assertEqual(self.page.evaluate(_ROW_HEIGHTS, ROW_LINES), [])

    def dense_pages_keep_to_their_boxes(self, _name):
        """Nothing escapes, overlaps, or wraps a pill with production-sized data."""

        self.assertEqual(self.page.evaluate(_ESCAPES), [])
        self.assertEqual(self.page.evaluate(_OVERLAPS), [])
        self.assertEqual(self.page.evaluate(_WRAPPED_PHRASES, SELECTORS["one_line"]), [])

    def tables_scroll_inside_their_own_container(self, name):
        """On a phone a wide table scrolls sideways; it never stacks into cards."""

        tables = self.page.evaluate(_TABLES)
        self.assertEqual(tables["found"], [])
        if "<table" in self.pages[name]:
            self.assertGreater(tables["seen"], 0, "the probe measured nothing")

    def grid_rows_end_together(self, _name):
        self.assertEqual(self.page.evaluate(_PAIRS), [])

    def a_pill_never_wraps(self, _name):
        self.assertEqual(self.page.evaluate(_WRAPPED_PHRASES, SELECTORS["one_line"]), [])

    def nothing_overlaps(self, _name):
        self.assertEqual(self.page.evaluate(_OVERLAPS), [])

    def layout_audit_structural_rules(self, _name):
        report = self.page.evaluate(_AUDIT)
        self.assertEqual([v for v in report["violations"] if v["rule"] in AUDIT_RULES], [])

    def a_table_frame_spans_its_box(self, _name):
        self.assertEqual(self.page.evaluate(_NARROW_FRAMES, SELECTORS["table_scroll"]), [])

    def a_mark_holds_its_glyph_in_its_middle(self, _name):
        self.assertEqual(self.page.evaluate(_OFF_CENTRE_MARKS, SELECTORS["route_mark"]), [])

    def opening_a_disclosure_moves_nothing_before_it(self, _name):
        self.assertEqual(self.page.evaluate(_MOVING_SUMMARIES), [])

    def a_line_shows_on_its_surface(self, _name):
        self.assertEqual(self.page.evaluate(_FAINT_LINES, [SELECTORS["contrast_scope"], LINE_CONTRAST]), [])

    def text_reads_in_the_dark_theme(self, _name):
        """Text clears WCAG AA on whatever it sits on, in the dark palette.

        Colour lives in tokens and each token carries both palettes, so a
        light-only colour can only come from a component that went round them.
        The stylesheet test catches a literal; this catches the rest (a token
        whose dark half was never checked, ink meant for one surface drawn on
        another) by measuring what the browser actually paints.
        """

        self.assertEqual(self.page.evaluate("() => matchMedia('(prefers-color-scheme: dark)').matches"), True)
        report = self.page.evaluate(_CONTRAST, [SELECTORS["contrast_scope"], TEXT_CONTRAST, LARGE_TEXT_CONTRAST])
        self.assertGreater(report["sampled"], 0)
        self.assertEqual(report["found"], [])

    def a_filled_box_keeps_its_padding(self, _name):
        """It opens every disclosure in the page to lay out what they hold."""

        self.assertEqual(
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

    def no_column_is_narrower_than_its_words(self, _name):
        self.assertEqual(self.page.evaluate(_BROKEN_WORDS), [])

    def a_link_sits_on_the_line_it_continues(self, _name):
        self.assertEqual(self.page.evaluate(_RAISED_LINKS), [])

    def a_wide_frame_is_filled(self, _name):
        self.assertEqual(self.page.evaluate(_EMPTY_FRAMES, [SELECTORS["frame"], SELECTORS["frame_content"]]), [])

    def a_row_has_one_disclosure(self, _name):
        self.assertEqual(self.page.evaluate(_NESTED_DISCLOSURES), [])

    def a_band_cell_keeps_its_padding(self, _name):
        self.assertEqual(self.page.evaluate(_UNPADDED_CELLS, SELECTORS["band_cell"]), [])

    def a_band_lays_out_as_whole_rows(self, _name):
        self.assertEqual(self.page.evaluate(_UNEVEN_BANDS, [SELECTORS["band"], SELECTORS["band_aside"]]), [])

    def a_route_is_one_row_or_one_column(self, _name):
        self.assertEqual(self.page.evaluate(_WRAPPED_ROUTES, SELECTORS["route"]), [])

    def a_token_breaks_only_where_it_may(self, _name):
        self.assertEqual(self.page.evaluate(_TOKEN_BREAKS, SELECTORS["token"]), [])

    def an_aside_wraps_only_under_its_heading(self, _name):
        self.assertEqual(self.page.evaluate(_ASIDE_BESIDE_ITS_HEADING, SELECTORS["heading_aside"]), [])

    def a_title_is_not_split_beside_its_controls(self, _name):
        found = self.page.evaluate(
            _SQUEEZED_TITLES, [SELECTORS["page_title"], SELECTORS["page_actions"], SELECTORS["page_head"]]
        )
        self.assertEqual(found, [])

    def code_and_cards_keep_inside_the_gutter(self, _name):
        self.assertEqual(self.page.evaluate(_PAST_THE_GUTTER, [SELECTORS["gutter_bound"], False]), [])

    def controls_in_a_row_share_one_height(self, _name):
        self.assertEqual(self.page.evaluate(_UNEVEN_CONTROL_ROWS, [SELECTORS["line_control"], SELECTORS["field"]]), [])

    def a_wrapped_bar_fills_its_rows(self, _name):
        self.assertEqual(self.page.evaluate(_RAGGED_BARS, SELECTORS["wrapping_bar"]), [])

    def a_queue_ends_its_cards_at_one_edge(self, _name):
        self.assertEqual(self.page.evaluate(_RAGGED_QUEUES, [SELECTORS["queue_card"], SELECTORS["queue_card_end"]]), [])

    def a_route_across_spaces_its_hops_evenly(self, _name):
        self.assertEqual(self.page.evaluate(_UNEVEN_ROUTES, [SELECTORS["route"], SELECTORS["route_icon"]]), [])

    def a_route_line_runs_from_its_first_stop_to_its_last(self, _name):
        self.assertEqual(self.page.evaluate(_ROUTE_LINE_ENDS, [SELECTORS["route_line"], SELECTORS["route_mark"]]), [])

    def a_checks_evidence_follows_its_statement(self, _name):
        found = self.page.evaluate(_STRANDED_EVIDENCE, [SELECTORS["check_statement"], SELECTORS["check_evidence"]])
        self.assertEqual(found, [])

    def nothing_a_summary_names_is_hidden(self, _name):
        self.assertEqual(self.page.evaluate(_HIDDEN_IN_SUMMARIES, SELECTORS["summary_value"]), [])

    # With script: the page a person reads, sorted, selectable and with its
    # cut values marked.

    def test_every_page_keeps_the_layout_rules_once_its_script_has_run(self):
        """The rules that hold without script hold there too; pressing a
        control moves nothing; a table fits its box with every disclosure open."""

        self.start(java_script_enabled=True)
        self.spoke = 0
        for name in (*ALL_PAGES, *SPEAKING_HEADS):
            widths = SPEAKING_WIDTHS if name in SPEAKING_HEADS else (*OVERFLOW_WIDTHS, *SPEAKING_WIDTHS)
            for width in sorted(set(widths)):
                if not self.loaded(name, width, script="on"):
                    continue
                self.page.evaluate(_SETTLED)
                if width in OVERFLOW_WIDTHS and name in ALL_PAGES:
                    self.hold(name, width, self.scripted_reads(), script="on")
                if width in SPEAKING_WIDTHS:
                    self.pressing_a_control_moves_nothing(name, width)
                if width in OVERFLOW_WIDTHS and name in ALL_PAGES:
                    self.hold(name, width, [self.a_table_fits_its_box_at_every_width], script="on")
        self.assertGreater(self.spoke, len(SPEAKING_HEADS), "no page in the fixture has a control that speaks")

    def scripted_reads(self):
        return [
            self.only_a_table_scrolls_sideways,
            self.nothing_runs_out_of_its_table_cell,
            self.opening_a_disclosure_moves_nothing_before_it,
            self.no_column_is_narrower_than_its_words,
            self.a_pill_never_wraps,
            self.a_table_shares_its_width_only_with_columns_that_show,
            self.words_stay_inside_the_box_that_holds_them,
            self.a_tables_names_share_one_edge_whether_or_not_a_row_opens,
            self.a_token_breaks_only_where_it_may,
        ]

    def a_table_shares_its_width_only_with_columns_that_show(self, _name):
        self.assertEqual(self.page.evaluate(_UNSEEN_COLUMNS, SELECTORS["table_scroll"]), [])

    def a_tables_names_share_one_edge_whether_or_not_a_row_opens(self, _name):
        """A row with a toggle and a row without start their names on the same
        line down the column: the toggle has a gutter every row keeps.
        """

        for edges in self.page.evaluate(_NAME_EDGES, SELECTORS["table_scroll"]):
            self.assertEqual(edges, 1)

    def pressing_a_control_moves_nothing(self, name, width):
        """Each saying is drawn and then taken back, so the next is measured
        on the page as it loaded."""

        for said in SAYINGS:
            with self.subTest(rule="pressing_a_control_moves_nothing", page=name, width=width, said=said):
                found = self.page.evaluate(_MOVED_BY_SPEAKING, [said, SELECTORS["spoken"]])
                if found is None:
                    continue
                self.spoke += 1
                self.assertEqual(found, [])

    def a_table_fits_its_box_at_every_width(self, name):
        """Not only on a phone. A table's columns leave by its region's tier
        in the order its headings rank them, so nothing a reader came for is
        off the edge at a tablet or a laptop width either. Every page, with
        every disclosure open.
        """

        self.open_every_disclosure(name)
        self.page.evaluate(_SETTLED)
        self.assertEqual(self.page.evaluate(_TABLES_THAT_DO_NOT_FIT, SELECTORS["table_scroll"]), [])

    def test_nothing_moves_once_a_page_is_drawn(self):
        """What the server draws is where things stay. The stress estate
        answers no part a page fetches, so those requests are left waiting:
        a placeholder stays a placeholder, and what moves is the scripts'."""

        self.start(java_script_enabled=True)
        self.page.route("**/*", self.wait_for_parts)
        for name in ALL_PAGES:
            for width in OVERFLOW_WIDTHS:
                with self.subTest(page=name, width=width):
                    moved = self.moved_once_drawn(f"{ORIGIN}/{name}/", width, self.loaded_and_still)
                    if moved:
                        self.fail("\n".join(moved))

    def wait_for_parts(self, route):
        if route.request.headers.get("x-requested-with") != "XMLHttpRequest":
            route.fallback()

    def loaded_and_still(self):
        self.page.wait_for_load_state("load")
        self.settle()
        self.page.evaluate(_SETTLED)

    # Rules with a setup of their own.

    def test_with_no_script_every_queue_row_shows_its_face(self):
        """No toggle to press: every row of every family, what is wrong, what
        it means and its buttons are on the page."""

        for width in OVERFLOW_WIDTHS:
            with self.subTest(width=width):
                self.open("action-items", width)
                rows = self.page.locator(SELECTORS["decisions"])
                self.assertGreater(rows.count(), 0)
                for row in rows.all():
                    for part in ("decision_title", "decision_detail", "decision_actions"):
                        for shown in row.locator(SELECTORS[part]).all():
                            self.assertTrue(shown.is_visible(), part)

    def test_a_queue_row_is_its_words_and_buttons_and_opens_to_its_steps(self):
        """Closed, a row is what is wrong, what it means and how to dismiss it;
        its steps are a press away, beneath it, and a family is a press away
        from its rows."""

        self.start(java_script_enabled=True)
        for width in (PHONE_SCREEN[0], OVERFLOW_WIDTHS[-1]):
            with self.subTest(width=width):
                self.open("action-items", width)
                families = self.page.locator(SELECTORS["decision_family"])
                self.assertGreater(families.count(), 0)
                for family in families.all():
                    family.click()
                rows = self.page.locator(SELECTORS["decisions"])
                opened = 0
                for row in rows.all():
                    self.assertTrue(row.locator(SELECTORS["decision_title"]).is_visible())
                    self.assertTrue(row.locator(SELECTORS["decision_dismiss"]).is_visible())
                    # Held by its node: opening the row moves it to the line beneath.
                    steps = row.locator(SELECTORS["decision_steps"]).element_handles()
                    if not steps:
                        continue
                    self.assertFalse(steps[0].is_visible())
                    row.locator(SELECTORS["row_toggle"]).click()
                    self.assertTrue(steps[0].is_visible())
                    row.locator(SELECTORS["row_toggle"]).click()
                    self.assertFalse(steps[0].is_visible())
                    opened += 1
                self.assertGreater(opened, 0)

    def test_every_selector_parses(self):
        authored = authored_selectors(Path(finders.find("css/app.css")).read_text())
        self.open(next(iter(PAGES)), OVERFLOW_WIDTHS[-1])
        kept = {_plain_selector(selector) for selector in self.page.evaluate(_KEPT_SELECTORS)}

        self.assertEqual([selector for selector in authored if _plain_selector(selector) not in kept], [])

    def card_rows(self, key):
        """The cards of one dashboard grid, grouped by the row they sit on."""
        rows = {}
        for box in self.page.locator(SELECTORS[key]).evaluate_all(
            "elements => elements.map(e => { const r = e.getBoundingClientRect(), "
            "edge = e.parentElement.getBoundingClientRect().right; "
            "return {top: r.top, bottom: r.bottom, left: r.left, right: r.right, edge}; })"
        ):
            rows.setdefault(round(box["top"]), []).append(box)
        return list(rows.values())

    def assert_rows_filled(self, key):
        # Cards in a row are the same height, and the last one reaches the
        # grid's right edge.
        for row in self.card_rows(key):
            self.assertEqual({round(box["bottom"]) for box in row}, {round(row[0]["bottom"])}, row)
            self.assertAlmostEqual(row[-1]["right"], row[-1]["edge"], delta=1)

    def test_the_dashboard_pairs_its_cards_on_a_wide_screen(self):
        self.open("dashboard", 1280)
        for key in ("highlights", "patterns"):
            with self.subTest(cards=key):
                self.assertGreater(max(len(row) for row in self.card_rows(key)), 1)
                self.assert_rows_filled(key)

    def test_the_dashboard_stacks_its_cards_on_a_phone(self):
        for width in (320, PHONE_SCREEN[0]):
            self.open("dashboard", width)
            for key in ("highlights", "patterns"):
                with self.subTest(cards=key, width=width):
                    rows = self.card_rows(key)
                    self.assertGreater(len(rows), 1)
                    self.assertEqual({len(row) for row in rows}, {1})
                    self.assertEqual({round(row[0]["left"]) for row in rows}, {round(rows[0][0]["left"])})

    def test_a_dashboard_without_contributors_draws_no_empty_pairs(self):
        self.open("dashboard-bare", 1280)
        # Without extensions, the cards that remain still fill their rows.
        for key in ("highlights", "patterns"):
            with self.subTest(cards=key):
                self.assert_rows_filled(key)

    def test_a_row_toggle_is_owed_only_to_what_a_reader_cannot_see(self):
        """Every row the server made openable has its toggle in the HTML, in
        the cell that names it, and shows it only while the row holds
        something back: a column a narrow box dropped with something in it, a
        part of a list it folds, or a value cut short, which opens its row only
        where the gutter is kept already. A narrow box keeps it for every table
        whose rows open, since touch has no hover title; a wide one only for a
        table with something else to open. A dash in a dropped column says
        nothing, screen-reader text is clipped to a pixel by design and a
        decoration is hidden from assistive technology: none is content a row
        withholds. Whether a value is cut is measured, so script is on.
        """

        self.start(java_script_enabled=True)
        self.pages = {**self.pages, "row-toggle-probe": probe(_ROW_TOGGLE_TABLES)}
        shown_at = {
            PHONE_SCREEN[0]: {"dropped", "cut", "folded", "cut-in-a-kept-gutter"},
            1280: {"folded", "cut-in-a-kept-gutter"},
        }
        for width, expected in shown_at.items():
            with self.subTest(width=width):
                self.open("row-toggle-probe", width)
                self.page.evaluate(_SETTLED)
                rows = self.page.evaluate(_ROW_TOGGLES)
                self.assertEqual({(row["toggles"], row["beside_name"]) for row in rows}, {(1, True)})
                self.assertEqual({row["probe"] for row in rows if row["shown"]}, expected)

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

                pressed = self.page.evaluate(_PRESS_FIRST_ROW_TOGGLE, SELECTORS["table_scroll"])
                opened = self.page.evaluate(_PHONE_TABLE, SELECTORS["table_scroll"])
                self.assertTrue(opened["details"])
                self.assertEqual(opened["columns"], closed["columns"])
                self.assertEqual(others(opened["rows"], pressed), others(closed["rows"], pressed))
                self.assertLessEqual(opened["sideways"], 1)

                self.page.evaluate(_PRESS_FIRST_ROW_TOGGLE, SELECTORS["table_scroll"])
                again = self.page.evaluate(_PHONE_TABLE, SELECTORS["table_scroll"])
                self.assertEqual(again["details"], [])
                self.assertFalse(again["held"])
                self.assertEqual(again["columns"], closed["columns"])
                self.assertEqual(again["rows"], closed["rows"])

    def test_a_fragment_that_is_not_percent_encoding_breaks_no_table(self):
        """`#%` is a legal fragment and no encoding of a row's id: it names no
        row, and everything the table script sets up after landing still runs."""

        self.start(java_script_enabled=True)
        errors = []
        self.page.on("pageerror", lambda error: errors.append(str(error)))
        self.page.goto(f"{ORIGIN}/projects/#%", wait_until="load")
        ready = self.page.eval_on_selector_all(
            SELECTORS["selectable_table"], "(tables) => tables.map((table) => table.dataset.selectionReady)"
        )
        self.assertEqual(errors, [])
        self.assertTrue(ready)
        self.assertEqual(set(ready), {"true"})

    def test_a_wide_table_on_a_desktop_keeps_every_column_and_holds_them_when_a_row_opens(self):
        self.start(java_script_enabled=True)
        self.open("connections", 1440)
        closed = self.page.evaluate(_PHONE_TABLE, SELECTORS["table_scroll"])
        self.assertEqual(len(closed["columns"]), closed["headings"])

        if closed["toggles"]:
            pressed = self.page.evaluate(_PRESS_FIRST_ROW_TOGGLE, SELECTORS["table_scroll"])
            opened = self.page.evaluate(_PHONE_TABLE, SELECTORS["table_scroll"])
            self.assertEqual(opened["columns"], closed["columns"])
            self.assertEqual(others(opened["rows"], pressed), others(closed["rows"], pressed))

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
                self.assertEqual(reading_problems(readings), [])

    def test_the_map_scrolls_with_the_page_and_draws_no_empty_lane(self):
        for width in OVERFLOW_WIDTHS:
            with self.subTest(width=width):
                self.open("topology", width)
                found = self.page.evaluate(_MAP, [SELECTORS["map"], SELECTORS["map_lane"], SELECTORS["map_node"]])
                self.assertIsNotNone(found)
                self.assertFalse(found["scrolls"])
                self.assertEqual(found["empty"], [])

    def test_a_blank_column_is_not_drawn_and_a_short_word_is_not_split(self):
        self.start(java_script_enabled=True)
        self.pages = {**self.pages, "blank-column-probe": probe(_BLANK_TABLE)}
        for width in (PHONE_SCREEN[0], 820, 1360):
            with self.subTest(width=width):
                self.open("blank-column-probe", width)
                self.page.evaluate(_SETTLED)
                shown = self.page.evaluate(_COLUMNS_SHOWN)
                self.assertNotIn("Nothing", shown)
                self.assertIn("Title", shown)
                self.assertIn("Runs", shown)
                self.assertEqual(self.page.evaluate(_BROKEN_WORDS), [])


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


def wait_for_reference(page):
    """Until the viewer has drawn the reference."""

    page.wait_for_function(_REFERENCE_MOUNTED, timeout=REFERENCE_READY)


# The sidebar is a list of entries, each named by its own link. An entry that
# opens to show more carries the control that opens it; an operation is a leaf.
_REFERENCE_ENTRY = """(path) => {
  let entry = document.querySelector('aside');
  for (const title of path) {
    entry = [...entry.querySelectorAll(':scope > ul > li')].find((item) =>
      item.querySelector(':scope > a, :scope > div > a').textContent.trim().startsWith(title));
    if (!entry) return null;
  }
  return entry.querySelector(':scope > a, :scope > div > a');
}"""
# The sidebar as drawn: each top-level entry, and the entries under it that open in turn.
_REFERENCE_OUTLINE = """() => {
  const link = (item) => item.querySelector(':scope > a, :scope > div > a');
  return [...document.querySelectorAll('aside > ul > li')].map((item) => [
    link(item).getAttribute('href'), link(item).textContent.trim(),
    [...item.querySelectorAll(':scope > ul > li')]
      .filter((child) => child.querySelector(':scope > div > [aria-expanded]'))
      .map((child) => link(child).textContent.trim()),
  ]);
}"""
# Every top-level group opened, as a reader opens it.
_REFERENCE_OPEN_GROUPS = """() => document
  .querySelectorAll('aside > ul > li > div > [aria-expanded="false"]').forEach((control) => control.click())"""
_REFERENCE_GROUPS_OPEN = """() => [...document.querySelectorAll('aside > ul > li > div > [aria-expanded]')]
  .every((control) => control.getAttribute('aria-expanded') === 'true'
    && control.closest('li').querySelector(':scope > ul') !== null)"""
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
  // Every link drawn anywhere on the page, the viewer's sidebar and its foot included.
  leaves: [...document.links]
    .filter((a) => a.origin !== location.origin && a.checkVisibility())
    .map((a) => a.href),
  search: document.querySelector('aside button').innerText,
})"""
_REFERENCE_FOLLOW = f"""(path) => {{
  const link = ({_REFERENCE_ENTRY})(path);
  if (!link) return false;
  link.click();
  return true;
}}"""
_REFERENCE_HEADING = """(title) => {
  const heading = [...document.querySelectorAll('main main h2, main main h3')]
    .find((h) => h.textContent.includes(title) && h.checkVisibility());
  if (!heading) return null;
  return {top: Math.round(heading.getBoundingClientRect().top),
    floor: Math.round(document.querySelector('header').getBoundingClientRect().bottom),
    window: innerHeight, hash: location.hash};
}"""
# The heading has been scrolled to and has stopped moving: the same place on three polls running.
_REFERENCE_AT_REST = f"""(title) => {{
  const found = ({_REFERENCE_HEADING})(title);
  if (!found || !found.hash) return false;
  const last = (window.resting ??= {{}})[title] ?? {{runs: 0}};
  window.resting[title] = {{top: found.top, runs: last.top === found.top ? last.runs + 1 : 0}};
  return window.resting[title].runs >= 3;
}}"""
_REFERENCE_OPEN_MENU = "(root) => document.querySelector(root).querySelector('header button').click()"
_REFERENCE_LISTED = f"(path) => ({_REFERENCE_ENTRY})(path) !== null"


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


def render_reference():
    """The reference in each theme, the document its viewer reads and the count
    the header asks for, by path."""

    with transaction.atomic():
        client = Client()
        client.force_login(get_user_model().objects.create_user(username="operator"))
        page = reverse("api_reference:reference")
        pages = {
            url.strip("/"): client.get(url).content.decode()
            for url in (page, reverse("hq_api:openapi"), reverse("action_item_count"))
        }
        client.post(reverse("theme"), {"theme": "dark"})
        pages[REFERENCE_DARK] = client.get(page).content.decode()
        transaction.set_rollback(True)
    return pages


class ReferenceBrowserTests(BrowserGate):
    """The API reference reads as a page of HQ's, at every width and in both themes."""

    @classmethod
    def render(cls):
        pages = render_reference()
        cls.document = json.loads(pages[reverse("hq_api:openapi").strip("/")])
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
        wait_for_reference(self.page)

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
        self.page.evaluate(_REFERENCE_OPEN_GROUPS)
        self.page.wait_for_function(_REFERENCE_GROUPS_OPEN)
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
                    self.settle()
                    self.assert_framed(self.frame())

    def test_a_phone_reaches_the_sidebar_under_the_header(self):
        self.visit(REFERENCE_PHONE)
        self.assert_framed(self.frame())
        self.page.evaluate(_REFERENCE_OPEN_MENU, SELECTORS["reference"])
        self.settle()
        self.assert_framed(self.frame())
        entries = self.page.evaluate(_REFERENCE_OUTLINE)
        self.assertGreater(len(entries), 1)

    def assert_painted_as_hq(self, paint, mode):
        self.assertIn(mode, paint["mode"].split())
        self.assertEqual(paint["viewer"], paint["page"])
        # The header lets the page show through its surface; the sidebar is that
        # surface, so they match in colour whatever the header's opacity.
        self.assertEqual(_opaque(paint["sidebar"]), _opaque(paint["header"]))
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

        self.visit(REFERENCE_PHONE)
        self.page.evaluate(_REFERENCE_OPEN_MENU, SELECTORS["reference"])
        self.settle()
        self.assertEqual(self.page.evaluate(_REFERENCE_CHROME)["leaves"], [])
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
            self.page.wait_for_function(_REFERENCE_AT_REST, arg=title, polling=100)
            heading = self.page.evaluate(_REFERENCE_HEADING, title)
            self.assertIsNotNone(heading)
            self.assertGreaterEqual(heading["top"], heading["floor"])
            self.assertLess(heading["top"], heading["window"] * 0.6)
            return heading["hash"]

        group = next(group["summary"] for group in self.document["tags"] if group["name"] == tag["parent"])
        # A reader opens the group, then the domain in it, then the operation.
        self.visit(1360)
        self.assertTrue(self.page.evaluate(_REFERENCE_FOLLOW, [group]))
        self.page.wait_for_function(_REFERENCE_LISTED, arg=[group, tag["summary"]])
        self.assertTrue(self.page.evaluate(_REFERENCE_FOLLOW, [group, tag["summary"]]))
        self.page.wait_for_function(_REFERENCE_LISTED, arg=[group, tag["summary"], title])
        self.page.wait_for_function(_REFERENCE_AT_REST, arg=tag["summary"], polling=100)
        self.assertTrue(self.page.evaluate(_REFERENCE_FOLLOW, [group, tag["summary"], title]))
        fragment = landed()
        self.assertNotEqual(fragment, "")
        self.page.reload(wait_until="load")
        wait_for_reference(self.page)
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
                (reverse("action_item_count"), ""): client.get(reverse("action_item_count")).content.decode(),
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
        self.page.clock.install()
        self.page.goto(f"{ORIGIN}/probe/", wait_until="load")
        self.page.evaluate(
            "() => { const first = document.querySelector('[data-fragment-part=\"first\"]');"
            " first.kept = true; first.open = true; }"
        )
        # Its first turn reads it again and keeps polling; the second is told to stop.
        self.page.clock.run_for(1000)
        self.page.wait_for_function(
            "() => document.querySelector('[data-fragment-part=\"second\"]').textContent.includes('read again')"
        )
        self.page.clock.run_for(1000)
        self.page.wait_for_function("() => document.querySelector('[data-fragment-poll]') === null")
        state = self.page.evaluate(_PROBE_STATE, SELECTORS["fragment_failure"])

        # The part that did not change is the same node, still open; the one
        # that did was replaced.
        self.assertTrue(state["first"])
        self.assertTrue(state["firstOpen"])
        self.assertEqual(state["second"].strip(), "Secondread again")
        asked = self.asked.count(("/probe-strip/", ""))
        self.page.clock.run_for(5000)
        # A request after those turns is answered after any they made.
        self.page.evaluate("() => fetch('/probe-slot/')")
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
