"""Real-browser layout checks, run by the browser:* tasks (`mise run browser`).

This filename does not match Django's default test*.py discovery, so the plain
suite needs neither Playwright nor a browser. `mise run browser` runs it, and
`mise run ci` always does.

Pages render through their real views over three synthetic example.* estates,
a sparse one (core/tests/test_browser_fixtures.py), a dense, production-shaped
one (core/tests/test_browser_dense_fixtures.py), and every list filled with the
longest values its model holds (core/tests/test_browser_derived_fixtures.py),
then load in a browser whose every request is
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
from collections import deque
from functools import cache, partial, wraps
from itertools import islice, takewhile
from pathlib import Path
from types import MappingProxyType
from typing import override
from unittest import mock
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.staticfiles import finders
from django.db import transaction
from django.test import Client, SimpleTestCase
from django.test.testcases import is_pickable
from django.urls import reverse
lazy from django.template import Context, Template
lazy from django.template.loader import render_to_string
lazy from playwright.sync_api import Error as BrowserError, TimeoutError as LoadTimeout

from hq.platform.core.stylesheets import DIRECTORY, stylesheet
from hq.platform.core.test_runner import stall_watch
from hq.platform.core.tests.test_browser_dense_fixtures import DENSE_PAGES, build_dense_estate, render_dense_pages
from hq.platform.core.tests.test_browser_derived_fixtures import build_derived_estate, render_derived_pages
from hq.platform.core.tests.test_browser_fixtures import PAGES, build_estate, render_pages
lazy from hq.platform.application.asks import Ask
lazy from hq.platform.application.pages import Page, PageAction
lazy from hq.platform.core.templatetags.stylesheet_tags import stylesheets

# The widths the gates read a page at, declared once. A set that reads more
# says why beside it; a test with a width of its own (a dashboard's pairs, a
# wide table on a desktop) names it where it is read.
#
# The phone HQ is read on: its viewport in CSS pixels.
PHONE_SCREEN = (402, 874)
# Where a frame across most of the page has the room to be judged empty.
WIDE = 1360
# A phone, a tablet held upright, a laptop: the screens every sweep reads.
WIDTHS = (PHONE_SCREEN[0], 820, WIDE)
# The narrowest phone a page must still fit.
NARROWEST = 320
# The sparse estate is read across the stylesheet's media queries as well: at
# the narrowest phone, a tablet's short side, and the widest window the
# `max-width: 1280px` rules hold, which WIDE is past.
SPARSE_WIDTHS = (NARROWEST, PHONE_SCREEN[0], 768, 1280)
# What a pressed control says is read where a head has the least room beside
# its title, on the phones, and where it has the most.
SPEAKING_WIDTHS = (NARROWEST, PHONE_SCREEN[0], WIDE)
# The least a control is hit through, either way, in CSS pixels (WCAG 2.5.8),
# which the stylesheet's `--target-min` is held to.
TARGET_MIN = 24


def _found(pattern, css, what):
    match = re.search(pattern, css)
    if not match:
        raise AssertionError(f"The stylesheet no longer says {what} the way the browser gate reads it ({pattern})")
    return match


def wide_tables(css):
    """Where a table's region is wide, and the narrowest viewports that make
    it so: the page's own region, inside the content's padding
    (`--content-pad: clamp(a, b vw, c)`), and one in a card, which adds its
    border and inline padding. Just past the breakpoint the columns that keep
    their line take what they need from the least room the wide rules get;
    narrower, columns drop by tier instead."""

    region = int(_found(r"@container table-region \(width >= (\d+)px\)", css, "where a region is wide").group(1))
    low, share, high = map(
        float, _found(r"--content-pad: clamp\((\d+)px, ([\d.]+)vw, (\d+)px\)", css, "the content's padding").groups()
    )
    space = _found(
        r"\.card \{ padding: var\(--surface-inset, var\(--space-\d+\) var\((--space-\d+)\)\)", css, "a card's padding"
    ).group(1)
    padding = float(_found(rf"{space}: (\d+)px;", css, "a card's inline padding").group(1))
    border = float(_found(r"border: var\(--surface-border, (\d+)px solid", css, "a surface's border").group(1))

    def first(inset):
        width = region
        while width - 2 * (min(max(low, share * width / 100), high) + inset) < region:
            width += 1
        return width

    page, card = first(0), first(padding + border)
    # Two viewports this close stretch the same columns, so the wider holds both.
    return region, (card,) if card - page <= 50 else (page, card)


# Where a table region is wide (tables.css's `width >= 1000px` queries), its
# columns that wrap have room to give, so a name there is never cut as a
# whole; only a part past its cap (figures.css) is cut, inside itself. And the
# laptop windows where a table's region has only just become wide, which a
# sweep that judges a table's columns reads besides WIDTHS.
WIDE_REGION, WIDE_TABLES = wide_tables(stylesheet())
# Every page, from the sparse estate and the dense one. The overflow and
# density checks run all of them at WIDTHS.
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
# the secondary lines are set in. A row's text can be more than six lines of
# its width hold (a name at its field's full length beside a paragraph of
# notes), so a cell is held to the larger of this and the row at its best:
# its wrapping text shared evenly over the room its one-line cells leave, no
# column narrower than its longest word, and a line of slack for each column
# that wraps. A cell past both is a column starved while another has room to
# give.
ROW_LINES = 6
# The stylesheet's phone breakpoint: `@media (max-width: 640px)`.
PHONE = 640
ORIGIN = "http://hq.example.test"
AUDIT = Path(__file__).with_name("browser_audit.js")

# Every class, id and attribute the gate queries, held by
# test_browser_selectors to markup a template or tag library writes: CSS
# selectors only (no `xpath=`), and never an attribute only a script sets.
SELECTORS = {
    # The fragment primitive: a placeholder's failure line, the disclosure
    # holding one, and the calendar it pages in place.
    "fragment_failure": "main > [data-fragment-load] > .notice",
    "fragment_fold": "details:has(> [data-fragment-load]) > summary",
    "calendar_period": "#calendar-period",
    "calendar_next": '#calendar a[rel="next"]',
    "decisions": "[data-attention-item]",
    # What a page says of the readings it asked for on opening.
    "ask_note": "[data-ask-note]",
    # A family's toggle, the row toggle, and a row's own dismiss.
    "decision_family": "[data-queue-family] [data-fold-toggle]",
    "row_toggle": "[data-row-toggle]",
    # A row opened to show all it holds, with every cut in it lifted.
    "open_row": 'tr:has(> * > [data-row-toggle][aria-expanded="true"])',
    "decision_dismiss": "[data-triage] button",
    "decision_title": ".attention-title",
    "decision_steps": ".resolution-workflow",
    "decision_detail": ".attention-detail",
    "decision_actions": ".queue-act .attention-actions",
    # A list page's controls: its search, a filter's menu and a box in one, a
    # toggle beside them, a heading that sorts on the server, and the results
    # they change: the rows, or the empty state that says nothing matches.
    "table_search": "[data-table-toolbar] input[type=search]",
    "table_filter": "[data-table-toolbar] details[data-menu]",
    "table_filter_box": "input[type=checkbox][name]",
    "table_toggle": "[data-table-toolbar] .table-toggle input",
    "table_rows": "main .data-table > tbody > tr",
    "table_empty": "main [data-empty-state]",
    "sort_link": "main a.table-sort-link",
    # The queue's own filter: a choice of urgency, applied by its button.
    "queue_urgency": "form.filter-bar select[name=status]",
    "queue_apply": "form.filter-bar button[type=submit]",
    # The header's menus, and on a phone the drawer that holds them.
    "nav_menu": ".primary-nav details[data-menu]",
    "nav_open": ".nav-toggle-open",
    "highlights": ".dashboard-highlights > .highlight-card",
    # Each reading in the dashboard's row: a mark, then its lines.
    "readings": ".dash-readings .glance-panel-head",
    "patterns": ".dashboard-patterns > .card",
    # What reads as one unit and so never breaks across lines: a state, a
    # time, one part of a value told in several ("Up 1 week", "healthy"), a
    # control's label in a row's controls, a page's head or a list's filters.
    "one_line": (
        ".pill, td time, td .cell-part,"
        ' :is(td[data-kind="control"], .page-head, .filter-bar, .table-toolbar) :is(.btn, button)'
    ),
    # A control one line tall, drawn on its own rather than inside a label
    # that is the control a reader sees.
    "line_control": (
        "input:not([type=checkbox], [type=radio], [type=hidden], [type=file], [type=range], [type=color],"
        " [type=submit], [type=button]):not(label *), select:not(label *)"
    ),
    # A form's field: one label, its control and its help.
    "field": ".field",
    # The grid every form's fields are laid out in.
    "form": ".form",
    # A control a reader fills in or picks from, whose edge is what shows it.
    "fillable": "input:not([type=checkbox], [type=radio], [type=hidden], [type=range], [type=color], [type=submit], [type=button], [type=reset], [type=image]), select, textarea",
    # A row of controls that wraps when its screen is narrow.
    "wrapping_bar": ".filter-bar, .table-toolbar, .table-filters",
    # Everything pressed: a button, a link drawn as one, a disclosure's handle.
    "control": "button, .btn, .icon-link, summary",
    # Everything a pointer presses: a link, a form's control, a disclosure,
    # and whatever else takes focus.
    "target": "a[href], button, summary, select, textarea, input:not([type=hidden]), [tabindex]:not([tabindex='-1'])",
    # Anything in a table cell: its words stay inside its own box, or they are
    # drawn across the next column while every box measures as in place.
    "cell_content": "main td *, main th *",
    # What a control says about the work it asked for, drawn under it.
    "spoken": ".ask-said",
    # An identifier: written through `|breakable` (an entity's name is), or in
    # code. A code block is text laid out by its own rules, and a table lays
    # out its own identifiers.
    "token": ":is(code, kbd, samp, .token, [data-entity]):not(pre *, .data-table *)",
    # A name in a table cell that is not the row's own: cut, never broken
    # inside a hyphenated part. Code in a cell is cut to one line on its own,
    # and a label of several words wraps as prose.
    "cell_name": ".data-table td [data-entity]:not(:has(code), strong > *, .row-gutter > *):has(> .token[data-sole])",
    # A hyphenated part of a token, which a cell cuts and never breaks.
    "token_part": ".whole",
    # Where `|breakable` lets a token's line break (value_tags._SEAM).
    "seam": ".seam",
    # A part of a phrase: a box the cell cuts on its own (figures.css).
    "phrase_part": ".token:not([data-sole]) :is(.whole, .part)",
    # A box a cell cuts a value in, holding out for a few characters of it
    # (`--cut-floor`): a name, a line, an identifier's code, a service's name.
    "cut_box": ".data-table td :is([data-entity], .cell-line, code, .service-name strong)",
    # Prose, whose words wrap as a sentence's do: a paragraph, the blocks the
    # stylesheet holds to `--measure` (Design.md: "prose keeps `--measure`"),
    # and a cell's free text (`cell-data`). A code block lays out by its own
    # rules. Muted text is not prose: `.muted` is a tone, and the line it
    # dresses is as often a name ("on example-host") as a sentence.
    "prose": (
        "p, .page-lede, .empty, .attention-body, .queue-meaning, .resolution-workflow li,"
        " .cell-data, pre, script, style, template"
    ),
    # A row's own name, which wraps at its hyphens by design rather than being
    # cut (figures.css: "it wraps at its hyphens, and the names beside it give
    # way instead"). A hyphen is no seam `|breakable` can mark, so the token
    # rule leaves it to that design: bare, as the row's link, or alone in the
    # row's gutter (figures.css exempts `.row-gutter > *` from the cut), or a
    # queue row's title.
    "row_name": "td > strong:first-child, td > a:first-child > strong, td.row-gutter > [data-entity], td .attention-title",
    # Any token, in a table or not: none leaves a scrap of itself on a line.
    "any_token": ".token",
    # A cell name's own token, the box that cuts it. A name in a muted line
    # is context, cut at the cell's edge by design.
    "sole_token": ":scope > .token[data-sole]",
    "context_line": ".muted",
    # A figure in a band or card: its label, and the value it answers.
    "figure": ".kpi",
    "figure_label": ".label",
    "figure_value": ".value",
    # The second of a label and its value: a fact's value after its term, a
    # node key after its machine, a piece of evidence before its kind.
    "labeled": ":is(.kv, .conn-keys) > dt + dd, .conn-row > code + .conn-kind",
    # What a heading says beside its title: a count, an age, where it is read.
    "heading_aside": ".heading-aside",
    # The same, in a heading or beside it in a section's head.
    "any_aside": ".heading-aside, .section-head .count",
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
    # Where a vendored script draws markup HQ does not write: the API
    # reference's viewer, which ReferenceBrowserTests holds to its own rules.
    "vendored": "#api-reference-root",
    # A band, and what in it is not a cell: a dialog a cell opens, its note.
    "band": (
        ":is(.control-summary, .service-band, .fact-band, .insight-grid, .sweep-grid, .machine-telemetry-metrics,"
        " .kpi-grid:not(.highlight-card > *))"
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
    # Sections paired two to a row, at one height.
    "paired": ".section-flow",
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
# line is the surface's own color.
LINE_CONTRAST = 1.07
# WCAG 1.4.11: what identifies a control, a field's edge, clears 3:1.
FIELD_CONTRAST = 3.0

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

# The part of a rect held by `el` that a reader sees: cut by `el` and every
# box above it that clips what it holds (overflow, `clip-path`), and null when
# nothing is left or one of them is not drawn at all. An absolutely placed box
# escapes the overflow of every box up to the one it is placed against;
# `clip-path` cuts all a box paints. A `display: contents` box has no box to
# clip with.
_PAINTED_PART = r"""
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
  // column, readable in neither. What a box inside the cell cuts (a line cut
  // with an ellipsis) shows only as far as that box, which is held on its own.
  const found = [];
  const cut = (el, cell) => {
    for (let b = el.parentElement; b && b !== cell; b = b.parentElement) {
      if (getComputedStyle(b).overflowX !== 'visible') return true;
    }
    return false;
  };
  for (const cell of document.querySelectorAll('main td, main th')) {
    const edge = box(cell).right;
    for (const el of cell.querySelectorAll('*')) {
      const r = box(el);
      if (r.width < 2 || r.height < 2 || !el.checkVisibility()) continue;
      if (r.right > edge + 1 && !cut(el, cell)) {
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
    // The link's own words, however deep: a box around them (a capped part)
    // also holds the line's leading, which is not where the words sit.
    // Walked without a filter callback, which an engine with script off
    // refuses to call back into.
    const walker = document.createTreeWalker(link, NodeFilter.SHOW_TEXT);
    let text = walker.nextNode();
    while (text && !text.textContent.trim()) text = walker.nextNode();
    const linked = text && rects(text)[0];
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
  try {
    const targets = [];
    for (const el of new Set([...document.querySelectorAll(selector)])) {
      if (el.matches(':disabled') || !el.checkVisibility({visibilityProperty: true})) continue;
      const drawn = box(el);
      // Out of sight until focused (a skip link), or drawn by its label.
      if (drawn.width <= 1 || drawn.height <= 1) continue;
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
  return [...new Set(found)].slice(0, 10);
}"""
)

# A summary says it opens with one mark: the disclosure primitive's
# (surfaces.css), or its own where its details opts out of that one. A second
# reads as a second control. A mark is the browser's own marker, or a glyph a
# pseudo-element draws on the summary or anything in it.
_SUMMARY_MARKS = (
    "() => {"
    + _DESCRIBE
    + r"""
  const found = [];
  const glyph = (el, pseudo) => {
    const match = /^"([^"]*)"$/.exec(getComputedStyle(el, pseudo).content);
    return match && /^\s*[▸▹▶►▾▿▼⌄⌃›»+−＋]\s*$/u.test(match[1]) ? match[1].trim() : '';
  };
  for (const summary of document.querySelectorAll('main summary')) {
    if (!summary.checkVisibility()) continue;
    const style = getComputedStyle(summary);
    const marks = style.display === 'list-item' && style.listStyleType !== 'none' ? ['its marker'] : [];
    for (const el of [summary, ...summary.querySelectorAll('*')]) {
      for (const pseudo of ['::before', '::after']) {
        const mark = glyph(el, pseudo);
        if (mark) marks.push(mark);
      }
    }
    if (marks.length > 1) found.push(`${describe(summary)} "${summary.textContent.trim().slice(0, 30)}" draws ${marks.join(' and ')}`);
  }
  return found;
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
# less than the word, because another column's content took the width. A word
# inside a run with no space in it (a link, a digest) that is itself wider than
# its cell breaks somewhere wherever the column is, so it is not counted.
_BROKEN_WORDS = (
    "() => {"
    + _DESCRIBE
    + """
  const found = [];
  const range = document.createRange();
  const across = (node, start, end) => {
    range.setStart(node, start);
    range.setEnd(node, end);
    return [...range.getClientRects()].filter((r) => r.width);
  };
  for (const cell of document.querySelectorAll('main td')) {
    const style = getComputedStyle(cell);
    const line = cell.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
    const walker = document.createTreeWalker(cell, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      if (!node.parentElement.checkVisibility()) continue;
      for (const run of node.textContent.matchAll(/\\S+/g)) {
        const wide = across(node, run.index, run.index + run[0].length).reduce((sum, r) => sum + r.width, 0) > line;
        for (const match of run[0].matchAll(/[A-Za-z]{4,}/g)) {
          const at = run.index + match.index;
          const lines = new Set(across(node, at, at + match[0].length).map((r) => Math.round(r.top)));
          if (lines.size > 1 && !(wide && match[0].length < run[0].length)) found.push(`${describe(cell)} breaks "${match[0]}"`);
        }
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
    // A row folded away is not drawn at all, which is not drawn as a card.
    for (const row of table.querySelectorAll('tbody tr')) {
      if (!['table-row', 'none'].includes(getComputedStyle(row).display)) {
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
    // Drawn as the phrase is, wherever it lands: a rule that hides whatever
    // follows a row's lead control would otherwise measure nothing.
    probe.style.display = getComputedStyle(pill).display;
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
  const height = (cell) => {
    const top = box(cell).top + parseFloat(getComputedStyle(cell).paddingTop);
    let bottom = top;
    const walker = document.createTreeWalker(cell, NodeFilter.SHOW_TEXT);
    for (let text = walker.nextNode(); text; text = walker.nextNode()) {
      if (!text.textContent.trim() || !text.parentElement.checkVisibility()) continue;
      bottom = Math.max(bottom, shown(text, cell));
    }
    return bottom - top;
  };
  const lineOf = (style) => (style.lineHeight === 'normal' ? parseFloat(style.fontSize) * 1.2 : parseFloat(style.lineHeight));
  const hold = (el, property, value) => {
    const was = [el.style.getPropertyValue(property), el.style.getPropertyPriority(property)];
    el.style.setProperty(property, value, 'important');
    return () => (was[0] ? el.style.setProperty(property, ...was) : el.style.removeProperty(property));
  };
  // Each cell's narrowest: its column's width with the table laid out at
  // min-content, the page left as it was. A table holding its columns while
  // a row is open (tables.js) cannot lay them out again, so each is as
  // narrow as it is held, and the row is judged at those widths.
  const narrowest = (table, cells) => {
    if (table.hasAttribute('data-rows-held')) return cells.map(({cell}) => box(cell).width);
    const released = ['table-layout:auto', 'width:min-content', 'min-width:0', 'max-width:none']
      .map((pair) => hold(table, ...pair.split(':')));
    const widths = cells.map(({cell}) => box(cell).width);
    released.reverse().forEach((release) => release());
    return widths;
  };
  // The row at its best: the height at which the text of its cells that wrap
  // fits the room the cells that keep one line leave, each cell as tall as
  // the next unless its column is already at its narrowest. A cell's text is
  // the area it covers (its content width by its height), which no share of
  // the room makes smaller. The browser shares the room in proportion to
  // each column's longest line, a word at a time, so a column that wraps can
  // stand a line past its exact share: one line each. Past what any layout of
  // the row can reach, it is no measure, and the row is judged by the budget
  // alone.
  const balanced = (row, cells) => {
    const least = narrowest(row.closest('table'), cells);
    let room = box(row).width;
    const wrapping = [];
    cells.forEach(({cell, style, tall}, i) => {
      const width = box(cell).width;
      if (tall <= 1.5 * lineOf(style)) { room -= width; return; }
      const inset = parseFloat(style.paddingLeft) + parseFloat(style.paddingRight);
      room -= inset;
      wrapping.push({area: (width - inset) * tall, least: least[i] - inset});
    });
    const needs = (height) => wrapping.reduce((sum, c) => sum + Math.max(c.least, c.area / height), 0);
    let low = 0, high = Math.max(...cells.map((c) => c.tall)) * 4;
    if (needs(high) > room) return {height: 0, columns: 0};
    for (let step = 0; step < 30; step++) {
      const mid = (low + high) / 2;
      if (needs(mid) > room) low = mid; else high = mid;
    }
    return {height: high, columns: wrapping.length};
  };
  const found = [];
  for (const row of document.querySelectorAll('main tbody tr')) {
    if (!row.getClientRects().length || row.cells.length === 1) continue;
    const cells = [...row.cells].filter((cell) => cell.getClientRects().length)
      .map((cell) => ({cell, style: getComputedStyle(cell), tall: height(cell)}));
    let best = null;
    for (const {cell, style, tall} of cells) {
      const line = lineOf(style);
      if (tall <= (lines + 0.5) * line) continue;
      best ??= balanced(row, cells);
      const shared = best.height + best.columns * line;
      if (tall > Math.max(lines * line, shared) + 0.5 * line) {
        found.push(`${describe(row.closest('table'))} row ${row.rowIndex}: ${describe(cell)} is ${(tall / line).toFixed(1)} lines, ${(shared / line).toFixed(1)} at its row's best: ${cell.textContent.trim().replace(/\\s+/g, ' ').slice(0, 60)}`);
      }
    }
  }
  return [...new Set(found)].slice(0, 10);
}"""
)

# What the browser paints, as an opaque color: any color syntax read back
# through a canvas pixel, a translucent one composited over what is behind it,
# and the WCAG contrast ratio between two.
_COLOR = """
  const canvas = document.createElement('canvas').getContext('2d', {willReadFrequently: true});
  canvas.canvas.width = canvas.canvas.height = 1;
  // Whatever syntax the browser serializes a color in (rgb(), color(srgb),
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
      const color = rgba(getComputedStyle(up).backgroundColor);
      if (color[3] > 0) layers.push(color);
      if (color[3] >= 1) break;
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
    + _COLOR
    + """
  // Every visible element that holds text of its own, measured against the
  // color actually behind it: the nearest ancestor that paints a background,
  // with translucent layers composited down to an opaque one. A color
  // written for one palette only is what this catches: a light-mode ink left
  // on a dark card reads as nothing, and no other check looks at color.
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
# panel's own color: the rows it was meant to part run together.
_FAINT_LINES = (
    "([scope, least]) => {"
    + _DESCRIBE
    + _COLOR
    + """
  const found = [];
  for (const root of document.querySelectorAll(scope)) {
    for (const el of root.querySelectorAll('*')) {
      if (!el.checkVisibility()) continue;
      const style = getComputedStyle(el);
      for (const side of ['Top', 'Right', 'Bottom', 'Left']) {
        if (parseFloat(style[`border${side}Width`]) < 1 || ['none', 'hidden'].includes(style[`border${side}Style`])) continue;
        const color = rgba(style[`border${side}Color`]);
        if (color[3] === 0) continue;
        // A box that paints its own fill is edged against what is outside it,
        // by its border or by the fill itself.
        const fill = rgba(style.backgroundColor);
        const filled = fill[3] > 0 && el.parentElement;
        const behind = filled ? background(el.parentElement) : background(el);
        const measured = Math.max(ratio(over(color, behind), behind), filled ? ratio(over(fill, behind), behind) : 0);
        if (measured < least) found.push(`${describe(el)} ${side.toLowerCase()} ${measured.toFixed(2)}:1 on ${behind.slice(0, 3).map(Math.round)}`);
      }
    }
  }
  return [...new Set(found)].slice(0, 10);
}"""
)

# A field is found by its edge: the border it draws, or, for a control drawn
# bare inside one that acts as the field (a search box's frame, a drop zone),
# the nearest frame around it. That edge, or the frame's own fill, stands out
# from what is behind the frame. A disabled field is exempt, as WCAG exempts it.
_FIELD_EDGES = (
    "([fillable, least]) => {"
    + _DESCRIBE
    + _COLOR
    + """
  const edged = (style) => ['Top', 'Right', 'Bottom', 'Left'].some((side) =>
    parseFloat(style[`border${side}Width`]) >= 1 && !['none', 'hidden'].includes(style[`border${side}Style`])
    && rgba(style[`border${side}Color`])[3] > 0);
  const found = [];
  for (const field of document.querySelectorAll(fillable)) {
    if (field.closest(':disabled, [aria-disabled="true"], [hidden]')) continue;
    let frame = field;
    while (frame && !edged(getComputedStyle(frame))) frame = frame.parentElement;
    if (!frame || !frame.parentElement || !frame.checkVisibility()) continue;
    const style = getComputedStyle(frame);
    const behind = background(frame.parentElement);
    const fill = rgba(style.backgroundColor);
    const sides = ['Top', 'Right', 'Bottom', 'Left'].filter((side) => parseFloat(style[`border${side}Width`]) >= 1);
    const measured = Math.max(
      ...sides.map((side) => ratio(over(rgba(style[`border${side}Color`]), behind), behind)),
      fill[3] > 0 ? ratio(over(fill, behind), behind) : 0,
    );
    if (measured < least) found.push(`${describe(frame)} ${measured.toFixed(2)}:1 on ${behind.slice(0, 3).map(Math.round)}`);
  }
  return [...new Set(found)].slice(0, 10);
}"""
)

# How far each box that paints a fill (a chip, a code span, the item a menu is
# at, a group's heading row) steps from the surface behind it: by its fill or
# by its edge, whichever shows more. Keyed by the box's place in the document,
# so the same box can be read again in the other palette. A vendored widget
# carries its own two palettes, and is not HQ's to hold.
_FILL_STEPS = (
    "([scope, vendored]) => {"
    + _DESCRIBE
    + _COLOR
    + """
  const place = new Map([...document.querySelectorAll('*')].map((el, at) => [el, at]));
  const steps = {};
  for (const root of document.querySelectorAll(scope)) {
    for (const el of root.querySelectorAll('*')) {
      if (!el.parentElement || el.closest(vendored) || !el.checkVisibility()) continue;
      const r = box(el);
      if (r.width < 2 || r.height < 2) continue;
      const style = getComputedStyle(el);
      const fill = rgba(style.backgroundColor);
      if (fill[3] === 0) continue;
      const behind = background(el.parentElement);
      const edges = ['Top', 'Right', 'Bottom', 'Left']
        .filter((side) => parseFloat(style[`border${side}Width`]) >= 1 && !['none', 'hidden'].includes(style[`border${side}Style`]))
        .map((side) => ratio(over(rgba(style[`border${side}Color`]), behind), behind));
      const painted = over(fill, behind);
      const words = el.textContent.trim().replace(/\\s+/g, ' ').slice(0, 30);
      steps[place.get(el)] = [Math.max(ratio(painted, behind), ...edges), `${describe(el)} (${painted.slice(0, 3).map(Math.round)} on ${behind.slice(0, 3).map(Math.round)}): ${words}`];
    }
  }
  return steps;
}"""
)


def lost_fills(steps, least):
    """The boxes a fill sets apart in one palette and not in the other, from
    each palette's ``_FILL_STEPS``: the two palettes are one set of
    distinctions, so a fill one shows and the other loses is a token whose
    other half was never checked. A fill as faint in both is passed: its
    faintness was chosen."""

    found = []
    for at in sorted(steps["light"].keys() | steps["dark"].keys(), key=int):
        read = {palette: steps[palette].get(at, (1, "")) for palette in ("light", "dark")}
        light, dark = read["light"][0], read["dark"][0]
        if min(light, dark) < least <= max(light, dark):
            lost = "dark" if dark < light else "light"
            said = read[lost][1] or read["light" if lost == "dark" else "dark"][1]
            found.append(f"{lost} loses {said}: {light:.2f}:1 in light, {dark:.2f}:1 in dark")
    return found[:10]


# Every color each element is painted in, and the element, in document order:
# what a theme decides. A theme reaches a page two ways, the system's
# preference and the operator's choice (`data-theme`), and the two must paint
# the same page. Autofocus lands in the first frame after a load, which one load
# reaches before it is read and another does not, and a page without script
# runs no frame callback to wait on. So each reading puts focus where autofocus
# would, and a focused page takes no autofocus after it: both loads read the
# same focus, and the focus ring stays among the colors compared.
_PAINT = (
    "() => {"
    + _DESCRIBE
    + """
  if (document.activeElement === document.body) document.querySelector('[autofocus]')?.focus();
  document.getAnimations().forEach((a) => { try { a.finish(); } catch { a.cancel(); } });
  return [...document.querySelectorAll('body, body *')].map((el) => {
    const s = getComputedStyle(el);
    return [describe(el), [s.color, s.backgroundColor, s.borderTopColor, s.borderBottomColor,
      s.outlineColor, s.fill, s.stroke, s.boxShadow].join(' ')];
  });
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
_KEPT_SELECTORS = """(directory) => {
  const sheets = [...document.styleSheets].filter((s) => s.href && new URL(s.href).pathname.includes(directory));
  const found = [];
  const walk = (rules) => {
    for (const rule of rules) {
      if (rule.selectorText !== undefined) found.push(rule.selectorText);
      if (rule.cssRules) walk(rule.cssRules);
    }
  };
  sheets.forEach((sheet) => walk(sheet.cssRules));
  return found;
}"""


def authored_selectors(css: str) -> list[str]:
    """Every style rule's selector as the stylesheet writes it, nested rules
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


def _opaque(color: str) -> tuple[int, ...]:
    """A computed color's red, green and blue, without its opacity, as 0-255:
    ``rgb(255, 255, 255)`` and ``color(srgb 1 1 1 / 0.8)`` are one color."""

    numbers = [float(n) for n in re.findall(r"[\d.]+", color.split("/")[0])]
    if color.startswith("color("):
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
  .map(([box, table]) => (box.getAttribute('aria-label') || table.querySelector('th')?.textContent || '').trim().slice(0, 24)
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
      // A cell that holds only the toggle has no name: the name starts the next cell.
      .map((cell) => (cell && !cell.textContent.trim() ? cell.nextElementSibling : cell))
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
  // words has no label to spill. The label is what the control lays out in
  // flow: its words and boxes, and what those spill in turn. A box placed
  // out of flow (the hit area hung beside a small control) is no label,
  // though it widens the control's scrollable overflow.
  const found = [];
  const words = document.createRange();
  const reach = (el) => {
    let right = -Infinity;
    const take = (r) => { if (r.width > 0) right = Math.max(right, r.right); };
    const text = (parent) => {
      for (const node of parent.childNodes) {
        if (node.nodeType === Node.TEXT_NODE && node.textContent.trim()) {
          words.selectNodeContents(node);
          [...words.getClientRects()].forEach(take);
        } else if (node.nodeType === Node.ELEMENT_NODE && getComputedStyle(node).display === 'contents') {
          text(node);
        }
      }
    };
    const walk = (parent) => {
      text(parent);
      for (const [child, style, r] of inFlow(parent)) {
        take(r);
        if (style.overflowX === 'visible') walk(child);
      }
    };
    walk(el);
    return right;
  };
  for (const el of document.querySelectorAll(selector)) {
    if (!el.checkVisibility() || !el.textContent.trim()) continue;
    const style = getComputedStyle(el);
    if (style.overflowX !== 'visible' || style.display === 'inline') continue;
    const start = el.getBoundingClientRect().left + parseFloat(style.borderLeftWidth);
    const held = Math.round(reach(el) - start + parseFloat(style.paddingRight));
    if (held > el.clientWidth + 1) {
      found.push(`${describe(el)} holds ${held}px of label in ${el.clientWidth}px`);
    }
  }
  return found.slice(0, 10);
}"""
)

_SETTLED = "() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)))"
# Every face the page asked for has arrived (see _LATE_FACE).
_FONTS_READY = "() => document.fonts.ready.then(() => document.fonts.status)"
# The page as the server drew it, with its scripts held back: parsed, styled
# and in its own face, which is what a reader is shown first. An image a sheet
# fetched would arrive after this, unwaited, so no sheet fetches one
# (test_a_sheet_fetches_nothing_but_its_faces).
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
# One frame's look at a page being drawn: until it is, the scripts its parser
# waits for; once it is, where everything stands (null), in the same task.
_UNTIL_DRAWN = f"""() => {{
  if (!({_DRAWN})()) return ({_PARSER_SCRIPTS})();
  ({_DRAWN_BOXES})();
  return null;
}}"""
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
# Colors transition between palettes, so contrast is measured after the
# transition ends.
_FINISH_TRANSITIONS = """() => document.getAnimations().forEach((a) => { try { a.finish(); } catch { a.cancel(); } })"""
# The disclosures a page loads closed, and closing them again: a rule opens
# them to lay out what they hold, and the page read at its next width is the
# page as it loaded.
_KEEP_CLOSED = "() => { window.hqLoadedClosed = [...document.querySelectorAll('details:not([open])')]; }"
_CLOSE_AGAIN = "() => window.hqLoadedClosed.forEach((details) => { details.open = false; })"

_CELLS_OFF_THE_FIRST_LINE = (
    "(scroll) => {"
    + _DESCRIBE
    + """
  // A row reads from its first line: its toggle is placed there, so every
  // cell of the row starts there too, whatever page the table is on.
  const found = [];
  for (const table of document.querySelectorAll('main ' + scroll + ' > table')) {
    for (const cell of table.querySelectorAll(':scope > * > tr > *')) {
      const align = getComputedStyle(cell).verticalAlign;
      if (align !== 'top') found.push(`${describe(cell)} in ${describe(table)} is set ${align}`);
    }
  }
  return [...new Set(found)].slice(0, 10);
}"""
)

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
  // A note already showing is one of the things being made to speak.
  const others = [...document.querySelectorAll('body *')]
    .filter((el) => el.checkVisibility() && !el.closest(spoken) && !el.closest('[data-ask-note]'));
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
# Press every one of these a reader can see: one drawn but not offered is not.
_PRESS_EVERY_SHOWN = "(controls) => controls.forEach((control) => control.checkVisibility() && control.click())"
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
    const centers = icons.map((mark) => mark.left + mark.width / 2);
    const gaps = centers.slice(1).map((center, i) => center - centers[i]);
    if (Math.max(...gaps) - Math.min(...gaps) > 1) found.push(`${describe(el)}: gaps ${gaps.map(Math.round).join(', ')}`);
  }
  return found;
}"""
)

# The dashboard's queue cards end their buttons at one edge: no card's end
# stops more than a pixel short of another's. The full queue is a table, whose
# columns hold its rows to one edge.
# A route's line runs from the center of its first stop's mark to the center
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
    const center = (stop) => { const at = box(stop.querySelector(mark));
      return down ? at.top + at.height / 2 : at.left + at.width / 2; };
    const start = Math.min(...stretches.map((s) => down ? s.top : s.left));
    const end = Math.max(...stretches.map((s) => down ? s.top + s.height : s.left + s.width));
    const first = center(stops[0]);
    const last = center(stops.at(-1));
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
# rules that judge where text breaks. A rule asking whether a part fits its
# line cannot judge a table cell: the column's width is settled after the
# browser chose where the cell's text breaks, so the line it measures is the
# break's outcome. And `checkVisibility()` on a `display: contents` box is
# false, which would skip the text inside it; its text is judged by the box
# it lays out in.
_LINES = r"""
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
  return [...new Set(found)].slice(0, 10);
}"""
)
_TOKEN_SELECTORS = [
    SELECTORS["token"],
    SELECTORS["cell_name"],
    SELECTORS["token_part"],
    f"{SELECTORS['prose']}, {SELECTORS['row_name']}",
    SELECTORS["open_row"],
]

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
  return [...new Set(found)].slice(0, 10);
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
  return [...new Set(found)].slice(0, 10);
}"""
)

_CUT_NAMES = (
    "([region, name, soleToken, context, wide]) => {"
    + _DESCRIBE
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
  return found.slice(0, 10);
}"""
)
# A name of plain parts there that fits within the length it holds out for
# (the table's --name-hold, read in the name's own font) is on one line:
# "hq.example." over "com" means a column that could wrap gave nothing. A name
# with a hyphenated part holds out for that part, which the rule above holds.
_SPLIT_NAMES = (
    "([region, name, context, wide, part]) => {"
    + _DESCRIBE
    + _LINES
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
  return found.slice(0, 10);
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
_PADDED_CUTS = (
    "(selector) => {"
    + _DESCRIBE
    + r"""
  const found = [];
  const range = document.createRange();
  for (const el of document.querySelectorAll(selector)) {
    const style = getComputedStyle(el);
    if (!/grid/.test(style.display) || !el.firstElementChild || !el.checkVisibility()) continue;
    // Its words, and the boxes it holds (a chip's padding is the chip's).
    range.selectNodeContents(el);
    const value = Math.max(range.getBoundingClientRect().right, ...[...el.children].map((child) => child.getBoundingClientRect().right));
    const end = el.getBoundingClientRect().right - parseFloat(style.paddingRight) - parseFloat(style.borderRightWidth);
    const gap = end - value;
    if (gap > 2) found.push(`${describe(el)} "${el.textContent.trim()}": ${Math.round(gap)}px of floor past its end`);
  }
  return found;
}"""
)
_CUT_WITH_ROOM = (
    "([region, openRow, context, phrasePart]) => {"
    + _DESCRIBE
    + _PAINTED_PART
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
  return found.slice(0, 10);
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
_PAST_THE_CLAMP = (
    "() => {"
    + _DESCRIBE
    + _LINES
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
_LONE_ASIDE_WORDS = (
    "(aside) => {"
    + _DESCRIBE
    + _LINES
    + r"""
  const found = [];
  for (const el of document.querySelectorAll(aside)) {
    if (!el.closest('main') || !el.checkVisibility()) continue;
    const own = chars(el).filter((c) => c.top !== null);
    const last = Math.max(...own.map((c) => c.top));
    if (!own.some((c) => c.top < last)) continue;
    const words = own.filter((c) => c.top === last).map((c) => c.ch).join('').trim();
    if (words && !/\s/.test(words)) found.push(`${describe(el)} "${el.textContent.trim().slice(0, 40)}" leaves "${words}" alone on its last line`);
  }
  return found;
}"""
)
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
    + _PAINTED_PART
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
    // A box's own overflow clips what it holds, never the box.
    const r = painted(el.parentElement, box(el));
    if (!r || r.width < 2 || r.height < 2) continue;
    if (r.right > right + 1 || r.left < left - 1) {
      found.push(`${describe(el)} spans ${Math.round(r.left)}..${Math.round(r.right)}px, gutter ${Math.round(left)}..${Math.round(right)}px`);
    }
  }
  if (!text) return [...new Set(found)].slice(0, 15);
  const range = document.createRange();
  const walker = document.createTreeWalker(main, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const el = node.parentElement;
    if (!node.data.trim() || inScroller(el)) continue;
    let block = el;
    while (block !== main && /^(inline|contents)/.test(getComputedStyle(block).display)) block = block.parentElement;
    const style = getComputedStyle(block);
    if (style.overflowX !== 'visible') continue;
    const b = box(block);
    const inner = b.right - parseFloat(style.paddingRight) - parseFloat(style.borderRightWidth);
    range.selectNodeContents(node);
    const lines = [...range.getClientRects()].map((r) => painted(el, r)).filter(Boolean);
    if (!lines.length) continue;
    const far = Math.max(...lines.map((r) => r.right));
    if (far > inner + 1) found.push(`${describe(block)}: "${node.data.trim().slice(0, 40)}" runs ${Math.round(far - inner)}px past its padding`);
  }
  return [...new Set(found)].slice(0, 15);
}"""
)

# What is an identifier, held to its own rule (`a_token_breaks_only_where_it_may`)
# rather than a word's.
_IDENTIFIER = "code, kbd, samp, time, .token, [data-entity]"

# A word of running text broken across lines: a word never breaks.
_BROKEN_WORDS_IN_TEXT = (
    "(identifier) => {"
    + _DESCRIBE
    + r"""
  const main = document.querySelector('main');
  const range = document.createRange();
  const found = [];
  const words = document.createTreeWalker(main, NodeFilter.SHOW_TEXT);
  for (let node = words.nextNode(); node; node = words.nextNode()) {
    const el = node.parentElement;
    if (el.closest(identifier) || el.closest('pre, script, style, template') || !el.checkVisibility()) continue;
    for (const word of node.data.matchAll(/[A-Za-z]{4,}/g)) {
      range.setStart(node, word.index);
      range.setEnd(node, word.index + word[0].length);
      const lines = new Set([...range.getClientRects()].filter((r) => r.width).map((r) => Math.round(r.top)));
      if (lines.size > 1) found.push(`${describe(el)} breaks the word "${word[0]}"`);
    }
  }
  return [...new Set(found)].slice(0, 15);
}"""
)

# Two lines of text drawn over each other, each as much of it as is painted.
_OVERLAPPING_TEXT = (
    "() => {"
    + _DESCRIBE
    + _PAINTED_PART
    + """
  const rects = [];
  const range = document.createRange();
  const walker = document.createTreeWalker(document.querySelector('main'), NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    if (!node.data.trim()) continue;
    range.selectNodeContents(node);
    for (const line of range.getClientRects()) {
      const r = painted(node.parentElement, line);
      if (r && r.width > 2 && r.height > 2) rects.push([node, r]);
    }
  }
  rects.sort((a, b) => a[1].top - b[1].top);
  const found = [];
  for (let i = 0; i < rects.length; i++) {
    const [one, a] = rects[i];
    for (let j = i + 1; j < rects.length && rects[j][1].top < a.bottom - 2; j++) {
      const [other, b] = rects[j];
      if (one === other) continue;
      const x = Math.min(a.right, b.right) - Math.max(a.left, b.left);
      const y = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
      if (x > 2 && y > 2) {
        found.push(`"${one.data.trim().slice(0, 30)}" (${describe(one.parentElement)}) over "${other.data.trim().slice(0, 30)}" (${describe(other.parentElement)})`);
      }
    }
  }
  return [...new Set(found)].slice(0, 10);
}"""
)

# A column no row has anything in.
_EMPTY_COLUMNS = (
    "() => {"
    + _DESCRIBE
    + """
  const found = [];
  const shown = (cell) => cell.textContent.trim()
    || [...cell.querySelectorAll('img, svg, button, input, select, a')].some((el) => el.checkVisibility());
  for (const table of document.querySelectorAll('main table')) {
    if (!table.checkVisibility()) continue;
    const rows = [...table.querySelectorAll(':scope > tbody > tr:not([data-row-detail])')]
      .filter((row) => row.checkVisibility());
    if (!rows.length) continue;
    const width = Math.max(...rows.map((row) => row.cells.length));
    for (let column = 0; column < width; column++) {
      const cells = rows.map((row) => row.cells[column]).filter((cell) => cell && cell.colSpan === 1 && cell.checkVisibility());
      if (cells.length === rows.length && !cells.some(shown)) {
        const head = table.tHead?.rows[0]?.cells[column];
        found.push(`${describe(table)} column ${column + 1} ("${head ? head.textContent.trim() : ''}") is empty in every row`);
      }
    }
  }
  return found;
}"""
)

# What a vendored script draws is not HQ's markup, so the layout rules judge
# the page with it taken out of the box HQ wrote for it, and an empty block as
# tall in its place: the box keeps its own rules and the page its height. The
# script's own tests hold what it draws. Put back by `_PUT_BACK`.
_STAND_IN = """(selector) => {
  window.hqStandIns = [...document.querySelectorAll(selector)].map((box) => {
    const stand = document.createElement('div');
    stand.style.blockSize = `${box.getBoundingClientRect().height}px`;
    const drawn = document.createDocumentFragment();
    drawn.append(...box.childNodes);
    box.append(stand);
    return [box, stand, drawn];
  });
}"""
_PUT_BACK = """() => (window.hqStandIns || []).forEach(([box, stand, drawn]) => {
  stand.remove();
  box.append(drawn);
})"""

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
_PROBE_PAGE = """{% load table_tags stylesheet_tags %}<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
{% stylesheets %}
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


# Probes read by more than one rule or check, with their arguments.
_CUT_NAMES_READ = (
    _CUT_NAMES,
    [
        SELECTORS["table_scroll"],
        SELECTORS["cell_name"],
        SELECTORS["sole_token"],
        SELECTORS["context_line"],
        WIDE_REGION,
    ],
)
_CUT_WITH_ROOM_READ = (
    _CUT_WITH_ROOM,
    [SELECTORS["table_scroll"], SELECTORS["open_row"], SELECTORS["context_line"], SELECTORS["phrase_part"]],
)
_SPLIT_NAMES_READ = (
    _SPLIT_NAMES,
    [
        SELECTORS["table_scroll"],
        SELECTORS["cell_name"],
        SELECTORS["context_line"],
        WIDE_REGION,
        SELECTORS["token_part"],
    ],
)
_FRAMES_READ = (_NESTED_FRAMES, [SELECTORS["frame_exempt"], SELECTORS["frame_exempt_within"]])
_CONTRAST_READ = (_CONTRAST, [SELECTORS["contrast_scope"], TEXT_CONTRAST, LARGE_TEXT_CONTRAST])
_LINES_READ = (_FAINT_LINES, [SELECTORS["contrast_scope"], LINE_CONTRAST])
_FIELDS_READ = (_FIELD_EDGES, [SELECTORS["fillable"], FIELD_CONTRAST])
# Every check whose verdict depends on the palette the page is drawn in.
_PALETTE = {"frames": _FRAMES_READ, "contrast": _CONTRAST_READ, "lines": _LINES_READ, "fields": _FIELDS_READ}
_SIDEWAYS_READ = (_SIDEWAYS, SELECTORS["sideways"])
_SPILLED_CONTROLS = (_SPILLED_LABELS, SELECTORS["control"])
_SPILLED_CELLS = (_SPILLED_LABELS, SELECTORS["cell_content"])
_PHRASES_READ = (_WRAPPED_PHRASES, SELECTORS["one_line"])
_ROUTE_ENDS_READ = (_ROUTE_LINE_ENDS, [SELECTORS["route_line"], SELECTORS["route_mark"]])
_EVIDENCE_READ = (_STRANDED_EVIDENCE, [SELECTORS["check_statement"], SELECTORS["check_evidence"]])
_SUMMARIES_READ = (_HIDDEN_IN_SUMMARIES, SELECTORS["summary_value"])
_UNDER_LABELS_READ = (_UNDER_ITS_LABEL, SELECTORS["labeled"])
_SMALL_TARGETS_READ = (_SMALL_TARGETS, [SELECTORS["target"], TARGET_MIN])
_FORM_ROWS_READ = (_HOLED_FORMS, [SELECTORS["form"], SELECTORS["field"]])
_EMPTY_FRAMES_READ = (_EMPTY_FRAMES, [SELECTORS["frame"], SELECTORS["frame_content"]])
_READINGS_OF = f"(selector) => ({_READINGS})([...document.querySelectorAll(selector)])"

# Every probe the layout invariants read on a page, by the check it answers.
_GEOMETRY = {
    "sideways": _SIDEWAYS_READ,
    "cells": _CELLS,
    "broken words": _BROKEN_WORDS,
    "words broken in text": (_BROKEN_WORDS_IN_TEXT, _IDENTIFIER),
    "wrapped phrases": _PHRASES_READ,
    "overlaps": _OVERLAPS,
    "overlapping text": _OVERLAPPING_TEXT,
    "grid rows": (_PAIRS, SELECTORS["paired"]),
    "form rows": _FORM_ROWS_READ,
    "bands": (_UNEVEN_BANDS, [SELECTORS["band"], SELECTORS["band_aside"]]),
    "band padding": (_UNPADDED_CELLS, SELECTORS["band_cell"]),
    "tables": _TABLES,
    "tables fit": (_TABLES_THAT_DO_NOT_FIT, SELECTORS["table_scroll"]),
    "unseen columns": (_UNSEEN_COLUMNS, SELECTORS["table_scroll"]),
    "empty columns": _EMPTY_COLUMNS,
    "row heights": (_ROW_HEIGHTS, ROW_LINES),
    "row alignment": (_CELLS_OFF_THE_FIRST_LINE, SELECTORS["table_scroll"]),
    "names cut whole": _CUT_NAMES_READ,
    "short names split": _SPLIT_NAMES_READ,
    "cut with room": _CUT_WITH_ROOM_READ,
    "padded cuts": (_PADDED_CUTS, SELECTORS["cut_box"]),
    "control labels": _SPILLED_CONTROLS,
    "cell labels": _SPILLED_CELLS,
    "raised links": _RAISED_LINKS,
    "small targets": _SMALL_TARGETS_READ,
    "row disclosures": _NESTED_DISCLOSURES,
    "summary marks": _SUMMARY_MARKS,
    "tokens": (_TOKEN_BREAKS, _TOKEN_SELECTORS),
    "stranded marks": (_STRANDED_MARKS, SELECTORS["seam"]),
    "values under labels": _UNDER_LABELS_READ,
    "heading asides": (_ASIDE_BESIDE_ITS_HEADING, SELECTORS["heading_aside"]),
    "lone aside words": (_LONE_ASIDE_WORDS, SELECTORS["any_aside"]),
    "past the clamp": _PAST_THE_CLAMP,
    "titles": (_SQUEEZED_TITLES, [SELECTORS["page_title"], SELECTORS["page_actions"], SELECTORS["page_head"]]),
    # Every box and every line of text, not only a card's or code's.
    "gutter": (_PAST_THE_GUTTER, ["*", True]),
    "audit": _AUDIT,
}


def geometry_probes(path, width):
    """The geometry's probes on ``path`` at ``width``, as ``(source, arg)``
    by check: a wide frame is judged only where it has the room, and the
    dashboard's readings only on the dashboard."""

    probes = {check: _probe(probe) for check, probe in _GEOMETRY.items()}
    if width >= WIDE:
        probes["wide frames"] = _EMPTY_FRAMES_READ
    if path == "/":
        probes["readings"] = (_READINGS_OF, SELECTORS["readings"])
    return probes


def survey_checks(path, width):
    """The checks the survey runs on ``path`` at ``width``, by name."""

    return [*geometry_probes(path, width), *_PALETTE, "fills"]


def _as_is(value):
    return value


# What turns a probe's value into its check's findings, where that is not the
# value itself.
_AS_FINDINGS = {
    "tables": lambda report: report["found"],
    "audit": lambda report: [v for v in report["violations"] if v["rule"] in AUDIT_RULES],
    "contrast": lambda report: report["found"] if report["sampled"] else ["no text sampled"],
    "readings": reading_problems,
}


def failure_name(params):
    """A failed subtest's screenshot name: its page, width and rule, then
    whatever else names it, as one file name."""

    first = [key for key in ("page", "width", "rule") if key in params]
    keys = [*first, *sorted(key for key in params if key not in first)]
    name = "-".join(str(params[key]) for key in keys)
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-")[:150] or "subtest"


class _Watched:
    """A test's result, marking the test ``failed`` when it fails outside a
    subtest, and having a failed subtest's page shot as it fails.

    A parallel worker's result keeps no list of failures to look in: it
    sends each one to the parent as it happens. The mark is a plain flag on
    the test, which a worker pickles with a failed subtest.
    """

    def __init__(self, result, test):
        self.result = result
        self.test = test

    def __getattr__(self, name):
        return getattr(self.result, name)

    def addError(self, test, err):
        self.test.failed = True
        self.result.addError(test, err)

    def addFailure(self, test, err):
        self.test.failed = True
        self.result.addFailure(test, err)

    def addSubTest(self, test, subtest, err):
        # Reported as the subtest ends, so the page is still the one it failed on.
        if err is not None:
            self.test.subtest_failed(subtest.params)
        self.result.addSubTest(test, subtest, err)


# A face the stylesheet lets the browser skip when it is late (`font-display:
# optional`). A late face is drawn in the fallback for the page's whole life,
# so on a loaded machine a wrapping check would read one page in either face
# from run to run. The gate's copy waits for its face instead, and every
# measure waits for the fonts (`_FONTS_READY`): each page is read in the face
# a reader whose font arrived in time sees.
_LATE_FACE = re.compile(rb"font-display:\s*optional")
# A reader whose Inter came late reads the page in the next face of the
# stylesheet's own stack (`--font`, tokens.css): the platform's system face.
# `HQ_BROWSER_FACE=fallback` (the browser tasks' `--fallback-face`) withholds
# Inter's face, so every page is drawn that way and a layout that holds only
# at Inter's widths fails.
_INTER = re.compile(rb"@font-face\s*\{[^}]*font-family:\s*Inter;[^}]*\}")


def in_the_face(css):
    """``css`` as the gate draws it: in Inter, waited for, or with Inter's
    face withheld, in the next face of the stack."""

    face = os.environ.get("HQ_BROWSER_FACE") or "inter"
    if face not in {"inter", "fallback"}:
        raise ValueError("HQ_BROWSER_FACE must be inter or fallback.")
    if face == "fallback":
        css = _INTER.sub(b"", css)
    return _LATE_FACE.sub(b"font-display: block", css)


# A loaded machine runs a page's frames late and its idle work later still, and
# a check that reads a page before a script's frame or idle callback has run
# fails only there. `HQ_BROWSER_SLOW=<ms>` (dev:look's `--slow`) draws every
# page that way on an idle machine: each animation frame comes that many
# milliseconds late, and an idle callback waits three times its timeout (or
# its frame's delay, given none), so a timing flake reproduces at will.
_SLOW = """(late) => {
  const frame = window.requestAnimationFrame.bind(window);
  const unframe = window.cancelAnimationFrame.bind(window);
  const pending = new Map();
  let last = 0;
  window.requestAnimationFrame = (callback) => {
    last += 1;
    const id = last;
    const timer = setTimeout(() => {
      pending.set(id, { frame: frame((time) => { pending.delete(id); callback(time); }) });
    }, late);
    pending.set(id, { timer });
    return id;
  };
  window.cancelAnimationFrame = (id) => {
    const waiting = pending.get(id);
    pending.delete(id);
    if (waiting?.timer !== undefined) clearTimeout(waiting.timer);
    if (waiting?.frame !== undefined) unframe(waiting.frame);
  };
  if (!window.requestIdleCallback) return;
  window.requestIdleCallback = (callback, options) => setTimeout(
    () => callback({ didTimeout: true, timeRemaining: () => 0 }), 3 * (options?.timeout ?? late),
  );
  window.cancelIdleCallback = (id) => clearTimeout(id);
}"""


def slow_frames():
    """The script that slows every page's frames and idle callbacks, when
    ``HQ_BROWSER_SLOW`` asks for it, else None."""

    late = os.environ.get("HQ_BROWSER_SLOW", "")
    if not late:
        return None
    if not late.isdigit() or int(late) == 0:
        raise ValueError("HQ_BROWSER_SLOW is how late each frame comes, a positive number of milliseconds.")
    return f"({_SLOW})({int(late)});"


@cache
def static_file(name):
    """A static file's type and body, read once a process: routing a request
    turns the browser's own cache off, so every load asks for each again."""

    asset = finders.find(name)
    if not asset:
        return None
    content_type = mimetypes.guess_type(asset)[0] or "application/octet-stream"
    body = Path(asset).read_bytes()
    if content_type == "text/css":
        body = in_the_face(body)
    return content_type, body


def add_css(page, css):
    """``css`` added to ``page`` after its own sheets, with script on or off.

    Playwright's ``add_style_tag`` waits, with no timeout, for the style's
    load event on a handler of the page's, which never runs with script off.
    """

    page.evaluate(
        "(css) => document.head.append(Object.assign(document.createElement('style'), { textContent: css }))", css
    )


def with_script(test):
    """Run ``test`` in a context with script on: it checks the enhancement."""

    test.scripted = True
    return test


class ProbeThrew(Exception):
    """A probe threw in the page, so its rule could not be judged."""


@cache
def _in_one_trip(sources):
    """One function that runs each probe in turn, each on its own, and says
    what each returned or threw: ``[[true, value] | [false, error], ...]``.
    A round trip to the page costs milliseconds, a probe often less."""

    probes = ",\n".join(f"({source})" for source in sources)
    return f"""async (args) => {{
  const probes = [{probes}];
  const out = [];
  for (let at = 0; at < probes.length; at += 1) {{
    try {{ out.push([true, await probes[at](args[at])]); }}
    catch (error) {{ out.push([false, String(error)]); }}
  }}
  return out;
}}"""


def _probe(probe):
    return probe if isinstance(probe, tuple) else (probe, None)


def taken(outcome):
    """A probe's value, or what it threw raised as ProbeThrew."""

    returned, value = outcome
    if not returned:
        raise ProbeThrew(value)
    return value


def reads(*probes):
    """A rule that judges only what its probes return: a probe is its source,
    or its source and argument. ``hold`` reads the probes of a run of such
    rules in one round trip; a rule called alone reads its own."""

    probes = tuple(map(_probe, probes))

    def rule(judge):
        @wraps(judge)
        def held(self, name, *found):
            return judge(self, name, *(found or self.read(probes)))

        held.probes = probes
        return held

    return rule


# What Playwright's driver logs while the stall watch is on (its DEBUG and
# DEBUG_FILE, read once when the driver starts): the browser's launch, with its
# arguments and its own output, and every call with its steps, a navigation's
# included. It goes to a file, so a run that does not stall prints none of it.
DRIVER_DEBUG = "pw:browser,pw:api"
DRIVER_LOG_SHOWN = 80


def start_driver(owner, sync_playwright):
    """Playwright's driver for ``owner``'s class, its log shown in a stall report."""

    if not stall_watch.enabled:
        return sync_playwright().start()
    handle, log = tempfile.mkstemp(prefix="hq-playwright-", suffix=".log")
    os.close(handle)
    owner.addClassCleanup(os.unlink, log)
    with mock.patch.dict(os.environ, {"DEBUG": DRIVER_DEBUG, "DEBUG_FILE": log}):
        driver = sync_playwright().start()
    evidence = partial(driver_log, log)
    stall_watch.evidence.append(evidence)
    owner.addClassCleanup(stall_watch.evidence.remove, evidence)
    return driver


def driver_log(path, stream):
    """The end of the driver's log, as a stall report's evidence."""

    with Path(path).open(encoding="utf-8", errors="replace") as log:
        tail = deque(log, maxlen=DRIVER_LOG_SHOWN)
    stream.write(f"--- Playwright {DRIVER_DEBUG}, last {len(tail)} lines ({path}) ---\n")
    stream.writelines(tail)


class BrowserGate(SimpleTestCase):
    """A browser over pages rendered once, every request answered locally.

    A subclass says what it renders (``render``); the browser then asks only
    for those paths and for static files.

    Layout must hold before progressive enhancement, so script is off unless
    the class (``scripted``) or the test (``@with_script``) checks the enhancement
    itself; ``setUp`` starts the context the test runs in.
    """

    databases = {"default"}
    scripted = False

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
        cls.playwright = start_driver(cls, sync_playwright)
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
        """A fresh browser context, replacing any the test already has."""

        if self.context is not None:
            self.context.close()
        test = getattr(self, self._testMethodName, None)
        options.setdefault("java_script_enabled", getattr(test, "scripted", self.scripted))
        # Scripts asked for while this is a list wait in it (moved_once_drawn).
        self.held = None
        self.context = self.browser.new_context(**options)
        self.context.set_default_timeout(5000)
        if slow := slow_frames():
            self.context.add_init_script(slow)
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

    def outcomes(self, probes):
        """What each of ``probes`` returned or threw, in one round trip:
        ``[(True, value) | (False, error), ...]``."""

        if not probes:
            return []
        sources, args = zip(*probes, strict=True)
        return [tuple(outcome) for outcome in self.page.evaluate(_in_one_trip(sources), list(args))]

    def read(self, probes):
        """What each of ``probes`` returned, in one round trip."""

        return [taken(outcome) for outcome in self.outcomes(probes)]

    def respond(self, route):
        if not self.holds(route):
            self.answer(route)

    def answer(self, route):
        path = urlsplit(route.request.url).path
        name = path.strip("/")
        if name in self.pages:
            route.fulfill(content_type=mimetypes.guess_type(name)[0] or "text/html", body=self.pages[name])
            return
        if path.startswith(settings.STATIC_URL) and (asset := static_file(path.removeprefix(settings.STATIC_URL))):
            content_type, body = asset
            route.fulfill(content_type=content_type, body=body)
            return
        route.fulfill(status=404, body="Not in the synthetic fixture")

    @override
    def run(self, result=None):
        self.failed = False
        self.failures = None
        return super().run(result if result is None else _Watched(result, self))

    @override
    def __getstate__(self):
        # A parallel worker sends a failed subtest to the parent with its test,
        # once the test has finished, when Django no longer leaves out what
        # cannot be pickled: the browser it drove and its cleanups stay here.
        return {key: value for key, value in super().__getstate__().items() if is_pickable(value)}

    def shoot(self, name):
        """The page as it stands, saved as ``name``.png in this test's own
        hq-browser-failure-* folder in the temporary directory, every one of
        which CI keeps when a gate fails."""

        if self.failures is None:
            self.failures = Path(tempfile.mkdtemp(prefix=f"hq-browser-failure-{self._testMethodName}-"))
        path = self.failures / f"{name}.png"
        self.page.screenshot(path=str(path), full_page=True)
        print(f"Browser failure screenshot: {path}")

    def subtest_failed(self, params):
        """A failed subtest's page, shot while it is still in the browser and
        named for its page, width and rule. A browser that has gone with the
        failure is said instead."""

        name = failure_name(params)
        try:
            self.shoot(name)
        except BrowserError as gone:
            print(f"No browser failure screenshot of {name}: {str(gone).splitlines()[0]}")

    def capture_failure(self):
        """A test that failed outside its subtests, shot as it ended."""

        if self.failed:
            self.shoot("page")

    def open(self, name, width):
        self.page.set_viewport_size(viewport(width))
        self.page.goto(f"{ORIGIN}/{name}/", wait_until="load")
        self.page.evaluate(_FONTS_READY)

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
            parsing = self.page.evaluate(_UNTIL_DRAWN)
            if parsing is None:
                break
            waiting = [route for route in self.held if route.request.url in set(parsing)]
            for route in waiting:
                self.held.remove(route)
                self.answer(route)
            self.page.evaluate(_SETTLED)
        else:
            raise AssertionError(f"{url} at {width}px was never drawn")
        held, self.held = self.held, None
        for route in held:
            self.answer(route)
        settled()
        return self.page.evaluate(_MOVED_SINCE_DRAWN)

    def geometry(self, path, width):
        """What the layout invariants find on the page as loaded, by check:
        every one the gates hold on a page as a reader has it."""

        return self.checked(geometry_probes(path, width))

    def scheme(self):
        """The palette the system asks the page for."""

        return "dark" if self.page.evaluate("() => matchMedia('(prefers-color-scheme: dark)').matches") else "light"

    def lost_fills(self):
        """The fills one palette shows and the other loses, read in each on
        this load; the page is left in the palette it was in."""

        was = self.scheme()
        steps = {}
        for palette in ("light", "dark"):
            self.page.emulate_media(color_scheme=palette)
            self.page.evaluate(_FINISH_TRANSITIONS)
            steps[palette] = self.page.evaluate(_FILL_STEPS, [SELECTORS["contrast_scope"], SELECTORS["vendored"]])
        self.page.emulate_media(color_scheme=was)
        self.page.evaluate(_FINISH_TRANSITIONS)
        return lost_fills(steps, LINE_CONTRAST)

    def palette(self):
        """What depends on the palette: frames drawn by a border, text, lines
        and fills against what they sit on."""

        return self.checked(_PALETTE)

    def findings(self, path, width):
        """What the layout rules find in HQ's own markup, checks that found
        nothing left out: the geometry and the frames in the palette loaded.
        The vendored markup is stood in for while they read, and put back
        however they end: each probe runs on its own."""

        found = self.checked(
            {
                "stand in": (_STAND_IN, SELECTORS["vendored"]),
                **geometry_probes(path, width),
                **_PALETTE,
                "put back": (_PUT_BACK, None),
            }
        )
        found["fills"] = self.lost_fills()
        return {check: said for check, said in found.items() if said}

    def checked(self, probes):
        """Each check's findings, read in one round trip; a probe that threw
        is a finding of its own check, so it hides no other."""

        outcomes = self.outcomes(list(probes.values()))
        return {
            check: _AS_FINDINGS.get(check, _as_is)(value) if returned else [f"the probe threw: {value}"]
            for check, (returned, value) in zip(probes, outcomes, strict=True)
        }


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
# What a pressed control says about its work, short to long.
SAYINGS = ("Reading", "Reading example-host", "That could not be done. Try again in a minute.")


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
            f"{stylesheets()}</head>"
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
            f"{stylesheets()}</head><body><main>"
            + render_to_string("partials/_page_head.html", {"page": Page(title=title, actions=actions)})
            + "</main></body></html>"
        )
        for name, title in SPEAKING_HEADS.items()
    }


@cache
def stress_estate():
    """Every page of the three stress estates, the derived lists' names, and
    the probes beside them: ``(pages, lists)``. Rendered once a process, so a
    worker handed a second layout class does not build the estates again;
    each estate is rolled back once rendered, so nothing else sees it."""

    with transaction.atomic():
        pages = render_pages(build_estate())
        transaction.set_rollback(True)
    with transaction.atomic():
        pages |= render_dense_pages(build_dense_estate())
        transaction.set_rollback(True)
    with transaction.atomic():
        lists = render_derived_pages(build_derived_estate())
        transaction.set_rollback(True)
    return MappingProxyType(pages | lists | speaking_heads() | shared_parts()), tuple(lists)


class LayoutBrowserTests(BrowserGate):
    """The layout rules over the stress estate.

    Each page is opened once per width and per configuration (script off,
    script on), and every rule that holds there is read off that one load,
    each in a subtest named for the rule, the page and the width. The rules
    that change the page (open its disclosures, dispatch a resize) run after
    the ones that only read it.

    The tests are the classes below, a sweep or a share of one each: Django
    hands a parallel worker a whole class, so they run side by side, and a
    run lasts as long as its longest class.
    """

    @classmethod
    def render(cls):
        pages, cls.lists = stress_estate()
        return pages

    def loaded(self, name, width, **where):
        """Open ``name`` at ``width``; one that does not load fails once, not once per rule."""

        opened = False
        # subTest records a failure and carries on, so `opened` stays False.
        with self.subTest(page=name, width=width, **where):
            self.open(name, width)
            opened = True
        return opened

    def hold(self, name, width, rules, **where):
        """Hold each rule on the page as it stands, one subtest each, in
        order. The probes of a run of rules that only read are read in one
        round trip before the first of them is judged; a probe that threw
        fails its own rule only."""

        read = {}
        for at, rule in enumerate(rules):
            if hasattr(rule, "probes") and at not in read:
                read |= self.read_ahead(rules, at)
            with self.subTest(rule=rule.__name__, page=name, width=width, **where):
                if at in read:
                    rule(name, *map(taken, read[at]))
                else:
                    rule(name)

    def read_ahead(self, rules, at):
        """The outcomes of each rule's probes, by its place in ``rules``, for
        the run of reading rules that starts at ``at``."""

        run = list(takewhile(lambda rule: hasattr(rule, "probes"), rules[at:]))
        outcomes = iter(self.outcomes([probe for rule in run for probe in rule.probes]))
        return {at + offset: list(islice(outcomes, len(rule.probes))) for offset, rule in enumerate(run)}

    def in_scheme(self, scheme):
        """The same load in the other palette. Its colors are finished rather
        than waited for: without script no animation frame runs."""

        self.page.emulate_media(color_scheme=scheme)
        self.page.evaluate(_FINISH_TRANSITIONS)

    # Without script: the page as the server drew it. Layout must hold before
    # progressive enhancement.

    def unscripted_reads(self, name, width):
        """The rules that only read the page: on the sparse estate's pages at
        every SPARSE_WIDTHS width, and on every page at every WIDTHS width."""

        rules = []
        if name in PAGES and width in SPARSE_WIDTHS:
            rules += [
                self.every_stylesheet_loads,
                self.nothing_escapes_the_page_sideways,
                self.tables_scroll_inside_their_own_container,
                self.grid_rows_end_together,
                self.a_pill_never_wraps,
                self.nothing_overlaps,
                self.layout_audit_structural_rules,
            ]
        if width in WIDTHS:
            rules += self.overflow_reads(name)
        if width == PHONE_SCREEN[0]:
            rules.append(self.a_narrow_head_keeps_its_lead_control_beside_the_title)
        if width == WIDTHS[-1]:
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
            self.a_row_reads_from_its_first_line,
            self.no_column_is_narrower_than_its_words,
            self.a_link_sits_on_the_line_it_continues,
            self.a_row_has_one_disclosure,
            self.a_summary_draws_one_mark,
            self.a_band_cell_keeps_its_padding,
            self.a_band_lays_out_as_whole_rows,
            self.a_queue_ends_its_cards_at_one_edge,
            self.a_route_line_runs_from_its_first_stop_to_its_last,
            self.a_mark_holds_its_glyph_in_its_middle,
            self.a_checks_evidence_follows_its_statement,
            self.nothing_a_summary_names_is_hidden,
            self.a_token_breaks_only_where_it_may,
            self.no_token_leaves_a_scrap_on_a_line,
            self.a_mark_keeps_to_the_words_it_punctuates,
            self.a_name_in_a_wide_table_is_whole,
            self.a_short_name_in_a_wide_table_is_one_line,
            self.nothing_is_held_back_while_its_table_has_room,
            self.a_figure_sits_under_its_label,
            self.a_value_sits_beside_its_label,
            self.an_aside_wraps_only_under_its_heading,
            self.an_aside_leaves_no_word_alone,
            self.a_clamped_box_shows_only_its_lines,
            self.a_title_is_not_split_beside_its_controls,
            self.code_and_cards_keep_inside_the_gutter,
            self.controls_in_a_row_share_one_height,
            self.a_wrapped_bar_fills_its_rows,
            self.a_form_fills_its_rows,
        ]
        if name in DENSE_PAGES:
            rules.append(self.dense_pages_keep_to_their_boxes)
        if name in ROUTE_PAGES:
            rules.extend((self.a_route_is_one_row_or_one_column, self.a_route_across_spaces_its_hops_evenly))
        return rules

    def unscripted_openings(self, name, width):
        """The rules that open the page's disclosures, after every read."""

        rules = []
        if width in WIDTHS:
            rules.append(self.a_filled_box_keeps_its_padding)
        rules.append(self.open_every_disclosure)
        if name in PAGES and width in SPARSE_WIDTHS:
            rules.append(self.nothing_escapes_the_page_sideways_with_every_disclosure_open)
        if width in WIDTHS:
            rules += [
                self.only_a_table_scrolls_sideways_with_every_disclosure_open,
                self.a_route_line_runs_from_its_first_stop_to_its_last,
                self.a_checks_evidence_follows_its_statement,
                self.a_value_sits_beside_its_label,
                self.nothing_a_summary_names_is_hidden,
                self.a_line_shows_on_its_surface,
                self.a_fill_shows_in_both_palettes,
            ]
        return rules

    @reads(_STYLESHEETS)
    def every_stylesheet_loads(self, _name, sheets):
        self.assertGreater(sheets["links"], 0)
        self.assertEqual(sheets["loaded"], sheets["links"])
        self.assertEqual(sheets["empty"], [])

    @reads(_ESCAPES)
    def nothing_escapes_the_page_sideways(self, _name, found):
        self.assertEqual(found, [])

    def open_every_disclosure(self, _name):
        """Every disclosure and popover open, for the rules that follow."""

        self.page.evaluate("() => document.querySelectorAll('details').forEach((d) => { d.open = true; })")

    @reads(_ESCAPES)
    def nothing_escapes_the_page_sideways_with_every_disclosure_open(self, _name, found):
        self.assertEqual(found, [])

    def no_block_breaks_a_paragraph(self, _name):
        # A block written inside a <p> closes it early and leaves an empty <p>
        # where its end tag was: the rest of the line falls out of it.
        self.assertEqual(self.page.locator(SELECTORS["broken_paragraph"]).count(), 0)

    @reads(_FRAMES_READ)
    def nothing_is_framed_inside_a_frame(self, _name, found):
        self.assertEqual(found, [])

    @reads((_HEAD_ON_ONE_LINE, [SELECTORS["head_lead"], SELECTORS["head_title"], SELECTORS["page_head"]]))
    def a_narrow_head_keeps_its_lead_control_beside_the_title(self, _name, beside):
        self.assertIn(beside, (None, True))

    @reads(_SPILLED_CONTROLS, _SPILLED_CELLS)
    def words_stay_inside_the_box_that_holds_them(self, _name, controls, cells):
        self.assertEqual(controls, [])
        self.assertEqual(cells, [])

    @reads(_SIDEWAYS_READ)
    def only_a_table_scrolls_sideways(self, _name, found):
        """At a phone, tablet and laptop width; again with every disclosure open."""

        self.assertEqual(found, [])

    @reads(_SIDEWAYS_READ)
    def only_a_table_scrolls_sideways_with_every_disclosure_open(self, _name, found):
        self.assertEqual(found, [])

    @reads(_CELLS)
    def nothing_runs_out_of_its_table_cell(self, _name, found):
        self.assertEqual(found, [])

    @reads((_ROW_HEIGHTS, ROW_LINES))
    def a_table_row_stays_within_its_height_budget(self, _name, found):
        self.assertEqual(found, [])

    @reads((_CELLS_OFF_THE_FIRST_LINE, SELECTORS["table_scroll"]))
    def a_row_reads_from_its_first_line(self, _name, found):
        self.assertEqual(found, [])

    @reads(_ESCAPES, _OVERLAPS, _PHRASES_READ)
    def dense_pages_keep_to_their_boxes(self, _name, escapes, overlaps, phrases):
        """Nothing escapes, overlaps, or wraps a pill with production-sized data."""

        self.assertEqual(escapes, [])
        self.assertEqual(overlaps, [])
        self.assertEqual(phrases, [])

    @reads(_TABLES)
    def tables_scroll_inside_their_own_container(self, name, tables):
        """On a phone a wide table scrolls sideways; it never stacks into cards."""

        self.assertEqual(tables["found"], [])
        if "<table" in self.pages[name]:
            self.assertGreater(tables["seen"], 0, "the probe measured nothing")

    @reads((_PAIRS, SELECTORS["paired"]))
    def grid_rows_end_together(self, _name, found):
        self.assertEqual(found, [])

    @reads(_PHRASES_READ)
    def a_pill_never_wraps(self, _name, found):
        self.assertEqual(found, [])

    @reads(_OVERLAPS)
    def nothing_overlaps(self, _name, found):
        self.assertEqual(found, [])

    @reads(_AUDIT)
    def layout_audit_structural_rules(self, _name, report):
        self.assertEqual(_AS_FINDINGS["audit"](report), [])

    @reads((_NARROW_FRAMES, SELECTORS["table_scroll"]))
    def a_table_frame_spans_its_box(self, _name, found):
        self.assertEqual(found, [])

    @reads((_OFF_CENTER_MARKS, SELECTORS["route_mark"]))
    def a_mark_holds_its_glyph_in_its_middle(self, _name, found):
        self.assertEqual(found, [])

    @reads(_MOVING_SUMMARIES)
    def opening_a_disclosure_moves_nothing_before_it(self, _name, found):
        self.assertEqual(found, [])

    @reads(_LINES_READ)
    def a_line_shows_on_its_surface(self, _name, found):
        self.assertEqual(found, [])

    def a_fill_shows_in_both_palettes(self, _name):
        self.assertEqual(self.lost_fills(), [])

    def every_palette_check_holds(self, _name):
        """Text, lines, fills and frames against what they sit on, in the
        palette the page is drawn in."""

        if found := {check: said for check, said in self.palette().items() if said}:
            self.fail(json.dumps(found, indent=1))

    @reads("() => matchMedia('(prefers-color-scheme: dark)').matches", _CONTRAST_READ)
    def text_reads_in_the_dark_theme(self, _name, dark, report):
        """Text clears WCAG AA on whatever it sits on, in the dark palette.

        Color lives in tokens and each token carries both palettes, so a
        light-only color can only come from a component that went round them.
        The stylesheet test catches a literal; this catches the rest (a token
        whose dark half was never checked, ink meant for one surface drawn on
        another) by measuring what the browser actually paints.
        """

        self.assertEqual(dark, True)
        self.assertGreater(report["sampled"], 0)
        self.assertEqual(report["found"], [])

    def a_chosen_theme_paints_as_the_systems(self, name):
        """The operator's choice of theme paints what the system's preference
        does: each palette chosen against the other system palette paints
        every element as that palette does when the system asks for it. A
        rule written under `prefers-color-scheme` rather than through the
        tokens' `light-dark()` reaches only one of the two. The server writes
        the choice into the page it renders, so the page is loaded with it,
        and left as it was found."""

        was, differ, html = self.scheme(), [], self.page.content()
        try:
            for theme, other in (("dark", "light"), ("light", "dark")):
                self.page.emulate_media(color_scheme=theme)
                self.load_as(name, html, None)
                system = self.page.evaluate(_PAINT)
                self.page.emulate_media(color_scheme=other)
                self.load_as(name, html, theme)
                chosen = self.page.evaluate(_PAINT)
                differ += [
                    f"{theme} chosen paints {el} {mine}, the system's {its}"
                    for (el, mine), (_, its) in zip(chosen, system, strict=True)
                    if mine != its
                ]
        finally:
            self.page.emulate_media(color_scheme=was)
            self.load_as(name, html, None)
        self.assertEqual(differ[:10], [])

    # How long, in milliseconds, a page loaded again from its own HTML may
    # take: every part of it is answered from memory.
    reload_within = 5_000

    def load_as(self, name, html, theme):
        """Page ``name`` loaded again from ``html``, as the server renders it
        for an operator who chose ``theme`` (none: the system's), or a failure
        naming it once the load outlasts ``reload_within``."""

        if theme:
            html = re.sub(r"<html\b", f'<html data-theme="{theme}"', html, count=1)
        address = self.page.url.split("#")[0]

        def answer(route):
            route.fulfill(content_type="text/html", body=html)

        self.page.route(address, answer)
        try:
            self.page.reload(wait_until="load", timeout=self.reload_within)
        except LoadTimeout:
            chosen = f"{theme} chosen" if theme else "no theme chosen"
            self.fail(f"{name} did not load again with {chosen} within {self.reload_within} ms")
        finally:
            self.page.unroute(address, answer)
        self.page.evaluate(_FINISH_TRANSITIONS)

    @reads(
        (
            _UNPADDED_TILES,
            [SELECTORS["tile_exempt"], SELECTORS["tile_control"], SELECTORS["tile_detached"], SELECTORS["disclosure"]],
        )
    )
    def a_filled_box_keeps_its_padding(self, _name, found):
        """It opens every disclosure in the page to lay out what they hold."""

        self.assertEqual(found, [])

    @reads(_BROKEN_WORDS)
    def no_column_is_narrower_than_its_words(self, _name, found):
        self.assertEqual(found, [])

    @reads(_RAISED_LINKS)
    def a_link_sits_on_the_line_it_continues(self, _name, found):
        self.assertEqual(found, [])

    @reads(_SMALL_TARGETS_READ)
    def every_control_is_big_enough_to_hit(self, _name, found):
        """A target is hit through at least 24 by 24 CSS px (`TARGET_MIN`,
        WCAG 2.5.8 Target Size Minimum), counting the area its `::before` or
        `::after` hangs over the space beside it, unless one of the
        criterion's exceptions holds:

        - Spacing: "if a 24 CSS pixel diameter circle is centered on the
          bounding box of each, the circles do not intersect another target or
          the circle for another undersized target". An area a target hangs
          over another target's is shared, and counts for neither.
        - Equivalent: "The function can be achieved through a different
          control on the same page that meets this criterion": the control
          a target names in `data-target-equivalent` (by its id) is at least
          24 by 24 and in the same cell or region.
        - Inline: "The target is in a sentence or its size is otherwise
          constrained by the line-height of non-target text": set inline
          among words that are no target's.
        - User agent control: "The size of the target is determined by the
          user agent and is not modified by the author": a control the browser
          draws, the size of a copy no author style reaches. One HQ draws or
          sizes, a styled tick box included, is held to the size.

        A tick box is hit through its labels too. The stylesheet's
        `--target-min` is held to the same size. Held on every page and width
        the layout checks read (`every_layout_check_holds`).
        """

        if found:
            self.fail("\n".join(found))

    @reads(_EMPTY_FRAMES_READ)
    def a_wide_frame_is_filled(self, _name, found):
        self.assertEqual(found, [])

    @reads(_NESTED_DISCLOSURES)
    def a_row_has_one_disclosure(self, _name, found):
        self.assertEqual(found, [])

    @reads(_SUMMARY_MARKS)
    def a_summary_draws_one_mark(self, _name, found):
        self.assertEqual(found, [])

    @reads((_UNPADDED_CELLS, SELECTORS["band_cell"]))
    def a_band_cell_keeps_its_padding(self, _name, found):
        self.assertEqual(found, [])

    @reads((_UNEVEN_BANDS, [SELECTORS["band"], SELECTORS["band_aside"]]))
    def a_band_lays_out_as_whole_rows(self, _name, found):
        self.assertEqual(found, [])

    @reads((_WRAPPED_ROUTES, SELECTORS["route"]))
    def a_route_is_one_row_or_one_column(self, _name, found):
        self.assertEqual(found, [])

    @reads((_FIGURE_UNDER_LABEL, [SELECTORS[key] for key in ("figure", "figure_label", "figure_value")]))
    def a_figure_sits_under_its_label(self, _name, found):
        self.assertEqual(found, [])

    @reads(_UNDER_LABELS_READ)
    def a_value_sits_beside_its_label(self, _name, found):
        """Closed, and again with every disclosure open."""

        self.assertEqual(found, [])

    @reads((_TOKEN_BREAKS, _TOKEN_SELECTORS))
    def a_token_breaks_only_where_it_may(self, _name, found):
        self.assertEqual(found, [])

    @reads((_SCRAPS, [SELECTORS["any_token"], _SCRAP]))
    def no_token_leaves_a_scrap_on_a_line(self, _name, found):
        self.assertEqual(found, [])

    @reads((_STRANDED_MARKS, SELECTORS["seam"]))
    def a_mark_keeps_to_the_words_it_punctuates(self, _name, found):
        self.assertEqual(found, [])

    @reads(_CUT_NAMES_READ)
    def a_name_in_a_wide_table_is_whole(self, _name, found):
        self.assertEqual(found, [])

    @reads(_SPLIT_NAMES_READ)
    def a_short_name_in_a_wide_table_is_one_line(self, _name, found):
        """A wide table is squeezed most just past WIDE_REGION: at WIDE_TABLES."""

        self.assertEqual(found, [])

    @reads(_CUT_WITH_ROOM_READ)
    def nothing_is_held_back_while_its_table_has_room(self, _name, found):
        self.assertEqual(found, [])

    @reads((_PADDED_CUTS, SELECTORS["cut_box"]))
    def a_cut_value_ends_where_its_words_end(self, _name, found):
        self.assertEqual(found, [])

    def every_layout_check_holds(self, name):
        """Every check the layout rules make, on the page as a reader has it:
        what `dev:look --rule survey` reports, held."""

        if found := self.findings(name, self.page.viewport_size["width"]):
            self.fail(json.dumps(found, indent=1))

    @reads((_ASIDE_BESIDE_ITS_HEADING, SELECTORS["heading_aside"]))
    def an_aside_wraps_only_under_its_heading(self, _name, found):
        self.assertEqual(found, [])

    @reads((_LONE_ASIDE_WORDS, SELECTORS["any_aside"]))
    def an_aside_leaves_no_word_alone(self, _name, found):
        self.assertEqual(found, [])

    @reads(_PAST_THE_CLAMP)
    def a_clamped_box_shows_only_its_lines(self, _name, found):
        self.assertEqual(found, [])

    @reads((_SQUEEZED_TITLES, [SELECTORS["page_title"], SELECTORS["page_actions"], SELECTORS["page_head"]]))
    def a_title_is_not_split_beside_its_controls(self, _name, found):
        self.assertEqual(found, [])

    @reads((_PAST_THE_GUTTER, [SELECTORS["gutter_bound"], False]))
    def code_and_cards_keep_inside_the_gutter(self, _name, found):
        self.assertEqual(found, [])

    @reads((_UNEVEN_CONTROL_ROWS, [SELECTORS["line_control"], SELECTORS["field"]]))
    def controls_in_a_row_share_one_height(self, _name, found):
        self.assertEqual(found, [])

    @reads((_RAGGED_BARS, SELECTORS["wrapping_bar"]))
    def a_wrapped_bar_fills_its_rows(self, _name, found):
        self.assertEqual(found, [])

    @reads(_FORM_ROWS_READ)
    def a_form_fills_its_rows(self, _name, found):
        self.assertEqual(found, [])

    @reads((_RAGGED_QUEUES, [SELECTORS["queue_card"], SELECTORS["queue_card_end"]]))
    def a_queue_ends_its_cards_at_one_edge(self, _name, found):
        self.assertEqual(found, [])

    @reads((_UNEVEN_ROUTES, [SELECTORS["route"], SELECTORS["route_icon"]]))
    def a_route_across_spaces_its_hops_evenly(self, _name, found):
        self.assertEqual(found, [])

    @reads(_ROUTE_ENDS_READ)
    def a_route_line_runs_from_its_first_stop_to_its_last(self, _name, found):
        self.assertEqual(found, [])

    @reads(_EVIDENCE_READ)
    def a_checks_evidence_follows_its_statement(self, _name, found):
        self.assertEqual(found, [])

    @reads(_SUMMARIES_READ)
    def nothing_a_summary_names_is_hidden(self, _name, found):
        self.assertEqual(found, [])

    # With script: the page a person reads, sorted, selectable and with its
    # cut values marked.

    def scripted_reads(self):
        """What a scripted page holds beside `every_layout_check_holds`."""

        return [
            self.opening_a_disclosure_moves_nothing_before_it,
            self.a_tables_names_share_one_edge_whether_or_not_a_row_opens,
        ]

    @reads((_NAME_EDGES, SELECTORS["table_scroll"]))
    def a_tables_names_share_one_edge_whether_or_not_a_row_opens(self, _name, tables):
        """A row with a toggle and a row without start their names on the same
        line down the column: the toggle has a gutter every row keeps.
        """

        for edges in tables:
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

    def wait_for_parts(self, route):
        if route.request.headers.get("x-requested-with") != "XMLHttpRequest":
            route.fallback()

    def loaded_and_still(self):
        self.page.wait_for_load_state("load")
        self.settle()
        self.page.evaluate(_SETTLED)

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


class UnscriptedLayoutTests(LayoutBrowserTests):
    """Every page without script: the page as the server drew it.

    Without script nothing on a page reads the width it was loaded at, so a
    page is loaded once, at its narrowest width, and resized for the rest
    (``across``): what a rule opened is closed again before the next width.
    The sweep is split between the classes below.
    """

    def sweep(self, names):
        for name in names:
            for width in self.across(name, {*WIDTHS, *(SPARSE_WIDTHS if name in PAGES else ())}):
                self.hold(name, width, self.unscripted_reads(name, width))
                if width in WIDTHS:
                    self.in_scheme("dark")
                    self.hold(name, width, [self.text_reads_in_the_dark_theme, self.a_line_shows_on_its_surface])
                    self.in_scheme("light")
                if width == WIDE:
                    self.hold(name, width, [self.a_chosen_theme_paints_as_the_systems])
                self.hold(name, width, self.unscripted_openings(name, width))

    def across(self, name, widths):
        """``name`` at each of ``widths``, narrowest first, from one load; a
        page that does not load fails once."""

        first, *rest = sorted(widths)
        if not self.loaded(name, first):
            return
        self.page.evaluate(_KEEP_CLOSED)
        yield first
        for width in rest:
            self.page.evaluate(_CLOSE_AGAIN)
            self.page.set_viewport_size(viewport(width))
            # A rule the new width changes may transition; the load it stands
            # for would have drawn it finished.
            self.page.evaluate(_FINISH_TRANSITIONS)
            yield width


class UnscriptedSparseLayoutTests(UnscriptedLayoutTests):
    def test_every_sparse_page_keeps_the_layout_rules_without_script(self):
        self.sweep(PAGES)

    def test_a_shared_part_escapes_nothing_sideways(self):
        """A component filled the way an extension fills it, closed and opened."""

        for name in SHARED_PARTS:
            for width in self.across(name, SPARSE_WIDTHS):
                self.hold(
                    name,
                    width,
                    [
                        self.nothing_escapes_the_page_sideways,
                        self.open_every_disclosure,
                        self.nothing_escapes_the_page_sideways_with_every_disclosure_open,
                    ],
                )


class UnscriptedDenseLayoutTests(UnscriptedLayoutTests):
    def test_every_dense_page_keeps_the_layout_rules_without_script(self):
        self.sweep(DENSE_PAGES)

    def test_every_list_keeps_its_rows_to_their_height_without_script(self):
        """Every list filled with the longest values its model holds, as the
        server drew it."""

        for name in self.lists:
            for width in self.across(name, {*WIDTHS, *WIDE_TABLES}):
                self.hold(name, width, [self.a_table_row_stays_within_its_height_budget])


class ScriptedLayoutTests(LayoutBrowserTests):
    """Every page once its script has run. The sweep is split between the
    classes below, so two workers run it side by side."""

    scripted = True

    def sweep(self, names):
        """Every layout check holds, at a laptop window's width too; pressing
        a control moves nothing; a table fits its box with every disclosure
        open. Script reads the width a page loads at, so each width is a load."""

        self.spoke = 0
        for name in names:
            widths = SPEAKING_WIDTHS if name in SPEAKING_HEADS else (*WIDTHS, *SPEAKING_WIDTHS, *WIDE_TABLES)
            for width in sorted(set(widths)):
                if not self.loaded(name, width, script="on"):
                    continue
                self.page.evaluate(_SETTLED)
                read = name in ALL_PAGES and width in (*WIDTHS, *WIDE_TABLES)
                if read and width in WIDTHS:
                    self.hold(name, width, self.scripted_reads(), script="on")
                    # Light is held by every layout check below.
                    self.in_scheme("dark")
                    self.hold(name, width, [self.every_palette_check_holds], script="on", scheme="dark")
                    self.in_scheme("light")
                if read:
                    self.hold(name, width, [self.every_layout_check_holds], script="on")
                if width in SPEAKING_WIDTHS:
                    self.pressing_a_control_moves_nothing(name, width)
                if read:
                    self.hold(name, width, [self.a_table_fits_its_box_at_every_width], script="on")


class ScriptedSparseLayoutTests(ScriptedLayoutTests):
    def test_every_sparse_page_keeps_the_layout_rules_once_its_script_has_run(self):
        self.sweep(PAGES)


class ScriptedDenseLayoutTests(ScriptedLayoutTests):
    def test_every_dense_page_and_head_keeps_the_layout_rules_once_its_script_has_run(self):
        self.sweep((*DENSE_PAGES, *SPEAKING_HEADS))
        self.assertGreater(self.spoke, len(SPEAKING_HEADS), "no page in the fixture has a control that speaks")


class SettledLayoutTests(LayoutBrowserTests):
    """What the scripts leave once a page is drawn: nothing moved. The sweep
    is split between the classes below."""

    scripted = True

    def sweep(self, names):
        """What the server draws is where things stay. The stress estate
        answers no part a page fetches, so those requests are left waiting:
        a placeholder stays a placeholder, and what moves is the scripts'."""

        self.page.route("**/*", self.wait_for_parts)
        for name in names:
            for width in WIDTHS:
                with self.subTest(page=name, width=width):
                    moved = self.moved_once_drawn(f"{ORIGIN}/{name}/", width, self.loaded_and_still)
                    if moved:
                        self.fail("\n".join(moved))


class SettledSparseLayoutTests(SettledLayoutTests):
    def test_nothing_moves_once_a_sparse_page_is_drawn(self):
        self.sweep(PAGES)


class SettledDenseLayoutTests(SettledLayoutTests):
    def test_nothing_moves_once_a_dense_page_is_drawn(self):
        self.sweep(DENSE_PAGES)


class LongestValueLayoutTests(LayoutBrowserTests):
    """Every list in its box with the longest values its model holds."""

    scripted = True

    def test_every_list_fits_its_box_with_the_longest_values_its_model_holds(self):
        """Every list, its rows derived from its model's fields
        (test_browser_derived_fixtures), as it loads and with every row open."""

        held = [self.a_table_fits_its_box_at_every_width, self.a_token_breaks_only_where_it_may]
        for name in self.lists:
            for width in (*WIDTHS, *WIDE_TABLES):
                if not self.loaded(name, width, script="on"):
                    continue
                self.page.evaluate(_SETTLED)
                self.hold(name, width, [self.every_layout_check_holds, *held], script="on")
                self.page.locator(SELECTORS["row_toggle"]).evaluate_all(_PRESS_EVERY_SHOWN)
                self.page.evaluate(_SETTLED)
                self.hold(name, width, [self.nothing_runs_out_of_its_table_cell, *held], script="on", rows="open")


class ComponentLayoutTests(LayoutBrowserTests):
    """Rules with a setup of their own: one page, probe or control each."""

    def test_with_no_script_every_queue_row_shows_its_face(self):
        """No toggle to press: every row of every family, what is wrong, what
        it means and its buttons are on the page."""

        for width in WIDTHS:
            with self.subTest(width=width):
                self.open("action-items", width)
                rows = self.page.locator(SELECTORS["decisions"])
                self.assertGreater(rows.count(), 0)
                for row in rows.all():
                    for part in ("decision_title", "decision_detail", "decision_actions"):
                        for shown in row.locator(SELECTORS[part]).all():
                            self.assertTrue(shown.is_visible(), part)

    @with_script
    def test_a_queue_row_is_its_words_and_buttons_and_opens_to_its_steps(self):
        """Closed, a row is what is wrong, what it means and how to dismiss it;
        its steps are a press away, beneath it, and a family is a press away
        from its rows."""

        for width in (PHONE_SCREEN[0], WIDTHS[-1]):
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

    @with_script
    def test_a_family_opens_to_its_rows_and_closes_them(self):
        """A family's toggle shows the rows folded under it and says so, and a
        second press folds them away again."""

        for width in (PHONE_SCREEN[0], WIDTHS[-1]):
            with self.subTest(width=width):
                self.open("action-items", width)
                toggle = self.page.locator(SELECTORS["decision_family"]).first
                family = toggle.evaluate("(toggle) => toggle.closest('tr').id")
                rows = self.page.locator(f'tr[data-folded="{family}"]')
                self.assertGreater(rows.count(), 0)

                def shown(rows=rows):
                    return [row.is_visible() for row in rows.all()]

                self.assertNotIn(True, shown())
                toggle.click()
                self.assertEqual(toggle.get_attribute("aria-expanded"), "true")
                self.assertNotIn(False, shown())
                toggle.click()
                self.assertEqual(toggle.get_attribute("aria-expanded"), "false")
                self.assertNotIn(True, shown())

    @with_script
    def test_an_opened_row_keeps_its_words_in_their_cells(self):
        """Opening a row lifts every cut in it, and what was cut breaks inside
        its column rather than running under the next one."""

        for name in ALL_PAGES:
            for width in (PHONE_SCREEN[0], 820):
                with self.subTest(page=name, width=width):
                    self.open(name, width)
                    # A family first, so the rows it folds show their own toggles.
                    self.page.locator(SELECTORS["decision_family"]).evaluate_all(_PRESS_EVERY_SHOWN)
                    self.page.locator(SELECTORS["row_toggle"] + ":not([data-fold-toggle])").evaluate_all(
                        _PRESS_EVERY_SHOWN
                    )
                    self.page.evaluate(_SETTLED)
                    self.assertEqual(self.page.evaluate(_CELLS), [])
                    self.assertEqual(self.page.evaluate(_TABLES_THAT_DO_NOT_FIT, SELECTORS["table_scroll"]), [])

    def test_a_probe_that_throws_hides_no_other(self):
        """Probes read in one round trip each run on their own."""

        self.open(next(iter(PAGES)), WIDE)
        found = self.outcomes([("() => 1", None), ("() => { throw new Error('example'); }", None), ("(n) => n + 1", 2)])
        self.assertEqual([returned for returned, _value in found], [True, False, True])
        self.assertIn("example", found[1][1])
        self.assertEqual(found[2][1], 3)

    def test_a_hit_area_hung_past_a_control_is_no_label_and_a_word_past_it_is(self):
        """The hit area a small control hangs beside itself widens what it
        scrolls, never what it says; a word wider than its control spills."""

        self.open(next(iter(PAGES)), WIDE)
        widened = self.page.evaluate("""() => {
          const star = document.createElement('button');
          star.className = 'pin-toggle example-star';
          star.textContent = '☆';
          const word = document.createElement('button');
          word.className = 'example-word';
          word.textContent = 'example label';
          word.style.setProperty('width', '2rem');
          word.style.setProperty('white-space', 'nowrap');
          document.querySelector('main').append(star, word);
          return star.scrollWidth > star.clientWidth;
        }""")
        self.assertTrue(widened)
        found = self.page.evaluate(_SPILLED_LABELS, "main :is(.example-star, .example-word)")
        self.assertEqual([spilled.split(" holds ")[0] for spilled in found], ["button.example-word"])

    def test_every_selector_parses(self):
        authored = authored_selectors(stylesheet())
        self.open(next(iter(PAGES)), WIDTHS[-1])
        kept = {_plain_selector(selector) for selector in self.page.evaluate(_KEPT_SELECTORS, f"/{DIRECTORY}/")}

        self.assertEqual([selector for selector in authored if _plain_selector(selector) not in kept], [])

    def test_the_dashboard_pairs_its_cards_on_a_wide_screen(self):
        self.open("dashboard", 1280)
        for key in ("highlights", "patterns"):
            with self.subTest(cards=key):
                self.assertGreater(max(len(row) for row in self.card_rows(key)), 1)
                self.assert_rows_filled(key)

    def test_the_dashboard_stacks_its_cards_on_a_phone(self):
        for width in (NARROWEST, PHONE_SCREEN[0]):
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

    @with_script
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

    @with_script
    def test_a_wide_table_fits_a_phone_and_opening_a_row_moves_nothing(self):
        """A table of four columns or more keeps the column that names each row
        and the ones marked key, and fits the screen; it does not stack into
        cards and is not swiped through. What left is listed when a row is
        opened, beneath that row, and opening it changes no column's width and
        no other row's height: the reader's place in the table holds.
        """

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

    @with_script
    def test_a_fragment_that_is_not_percent_encoding_breaks_no_table(self):
        """`#%` is a legal fragment and no encoding of a row's id: it names no
        row, and everything the table script sets up after landing still runs."""

        errors = []
        self.page.on("pageerror", lambda error: errors.append(str(error)))
        self.page.goto(f"{ORIGIN}/projects/#%", wait_until="load")
        ready = self.page.eval_on_selector_all(
            SELECTORS["selectable_table"], "(tables) => tables.map((table) => table.dataset.selectionReady)"
        )
        self.assertEqual(errors, [])
        self.assertTrue(ready)
        self.assertEqual(set(ready), {"true"})

    @with_script
    def test_a_wide_table_on_a_desktop_keeps_every_column_and_holds_them_when_a_row_opens(self):
        self.open("connections", 1440)
        closed = self.page.evaluate(_PHONE_TABLE, SELECTORS["table_scroll"])
        self.assertEqual(len(closed["columns"]), closed["headings"])

        if closed["toggles"]:
            pressed = self.page.evaluate(_PRESS_FIRST_ROW_TOGGLE, SELECTORS["table_scroll"])
            opened = self.page.evaluate(_PHONE_TABLE, SELECTORS["table_scroll"])
            self.assertEqual(opened["columns"], closed["columns"])
            self.assertEqual(others(opened["rows"], pressed), others(closed["rows"], pressed))

    @with_script
    def test_any_table_sorts_by_a_heading(self):
        """A table whose server does not sort it sorts on the page: ascending,
        then descending, then as it came, each section keeping its own rows.
        """

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

    @with_script
    def test_what_the_dashboard_says_of_its_readings_covers_none_of_them(self):
        """Opened while a reading is due, the dashboard asks for it and says it
        is waiting for the controller. That line stands where a trail would,
        and the dashboard's readings stand there instead: it is said on a line
        no other text is drawn on, whichever row the readings take."""

        for width in WIDTHS:
            with self.subTest(width=width):
                self.open("dense/dashboard", width)
                self.page.locator(SELECTORS["ask_note"]).wait_for(state="visible")
                self.assertEqual(self.page.evaluate(_OVERLAPPING_TEXT), [])

    def test_the_map_scrolls_with_the_page_and_draws_no_empty_lane(self):
        for width in WIDTHS:
            with self.subTest(width=width):
                self.open("topology", width)
                found = self.page.evaluate(_MAP, [SELECTORS["map"], SELECTORS["map_lane"], SELECTORS["map_node"]])
                self.assertIsNotNone(found)
                self.assertFalse(found["scrolls"])
                self.assertEqual(found["empty"], [])

    def test_a_palette_only_the_systems_preference_reaches_is_caught(self):
        """A rule written under `prefers-color-scheme` paints a page whose
        operator chose the other palette as the system's, which the rule on
        chosen themes names; without it the same page passes."""

        name = next(iter(PAGES))
        self.open(name, WIDE)
        was = self.scheme()
        self.a_chosen_theme_paints_as_the_systems(name)
        add_css(self.page, "@media (prefers-color-scheme: dark) { body { background: rgb(1, 2, 3) } }")
        with self.assertRaises(AssertionError) as caught:
            self.a_chosen_theme_paints_as_the_systems(name)
        for chosen in ("dark", "light"):
            self.assertIn(f"{chosen} chosen paints body", str(caught.exception))
        self.assertEqual(self.scheme(), was)

    @with_script
    def test_a_page_that_never_loads_again_fails_the_chosen_theme_naming_it(self):
        """A reload that never reaches its load event fails the rule within
        its bound, naming the page, rather than holding the gate."""

        name = next(iter(PAGES))
        self.open(name, WIDE)
        # Every script asked for from here on waits unanswered, and the load
        # event waits for them.
        self.held, self.reload_within = [], 500
        with self.assertRaises(AssertionError) as caught:
            self.a_chosen_theme_paints_as_the_systems(name)
        self.assertIn(f"{name} did not load again", str(caught.exception))

    @with_script
    def test_a_blank_column_is_not_drawn_and_a_short_word_is_not_split(self):
        self.pages = {**self.pages, "blank-column-probe": probe(_BLANK_TABLE)}
        for width in WIDTHS:
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
REFERENCE_DESKTOP = (1024, WIDE)
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
      const color = getComputedStyle(up).backgroundColor;
      if (!/rgba\\(0, 0, 0, 0\\)|transparent/.test(color)) return color;
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
# Follows a sidebar entry, as a click, and answers the address it sends the page to.
_REFERENCE_FOLLOW = f"""(path) => {{
  const link = ({_REFERENCE_ENTRY})(path);
  if (!link) return null;
  link.click();
  return link.hash;
}}"""
# Where the page stands on an address: the heading of the section it names, once
# the page is at that address. The viewer names a section by the address under
# its document's slug, so the section is found by the end of its id.
_REFERENCE_HEADING = """(address) => {
  if (location.hash !== address) return null;
  const name = '/' + decodeURIComponent(address.slice(1));
  const section = [...document.querySelectorAll('main main section[id]')].find((s) => s.id.endsWith(name));
  const heading = section && section.querySelector('h2, h3');
  if (!heading || !heading.checkVisibility()) return null;
  return {top: Math.round(heading.getBoundingClientRect().top), title: heading.textContent,
    floor: Math.round(document.querySelector('header').getBoundingClientRect().bottom),
    window: innerHeight};
}"""
# The page is at the address and its heading has stopped moving: the same place
# on three polls running. A busy browser can take longer than that to act on a
# click, and until it does the page is still at the address it had before.
_REFERENCE_AT_REST = f"""(address) => {{
  const found = ({_REFERENCE_HEADING})(address);
  if (!found) return false;
  const last = (window.resting ??= {{}})[address] ?? {{runs: 0}};
  window.resting[address] = {{top: found.top, runs: last.top === found.top ? last.runs + 1 : 0}};
  return window.resting[address].runs >= 3;
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

    scripted = True

    @classmethod
    def render(cls):
        pages = render_reference()
        cls.document = json.loads(pages[reverse("hq_api:openapi").strip("/")])
        return pages

    def start(self, **options):
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

        self.visit(WIDE)
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
        # surface, so they match in color whatever the header's opacity.
        self.assertEqual(_opaque(paint["sidebar"]), _opaque(paint["header"]))
        self.assertEqual(paint["title"], paint["ink"])
        self.assertEqual(paint["title_face"], paint["face"])

    def test_both_themes_are_painted_from_hqs_tokens(self):
        """The system's theme when none is chosen, the chosen one when it is."""

        painted = {}
        for scheme in ("light", "dark"):
            with self.subTest(scheme=scheme):
                self.start(color_scheme=scheme)
                self.visit(WIDE)
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
            self.visit(WIDE, REFERENCE_DARK)
            self.assertEqual(self.page.evaluate(_REFERENCE_PAINT), painted["dark"])

    def test_the_viewer_offers_only_what_works_here(self):
        """One search that says it is the reference's, no request sent from the page,
        no token asked for, no way out to the vendor, and nothing fetched from it."""

        self.visit(REFERENCE_PHONE)
        self.page.evaluate(_REFERENCE_OPEN_MENU, SELECTORS["reference"])
        self.settle()
        self.assertEqual(self.page.evaluate(_REFERENCE_CHROME)["leaves"], [])
        self.visit(WIDE)
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

        def landed(address):
            self.page.wait_for_function(_REFERENCE_AT_REST, arg=address, polling=100)
            heading = self.page.evaluate(_REFERENCE_HEADING, address)
            self.assertIn(title, heading["title"])
            self.assertGreaterEqual(heading["top"], heading["floor"])
            self.assertLess(heading["top"], heading["window"] * 0.6)

        group = next(group["summary"] for group in self.document["tags"] if group["name"] == tag["parent"])
        # A reader opens the group, then the domain in it, then the operation.
        self.visit(WIDE)
        self.assertTrue(self.page.evaluate(_REFERENCE_FOLLOW, [group]))
        self.page.wait_for_function(_REFERENCE_LISTED, arg=[group, tag["summary"]])
        domain = self.page.evaluate(_REFERENCE_FOLLOW, [group, tag["summary"]])
        self.assertTrue(domain)
        self.page.wait_for_function(_REFERENCE_LISTED, arg=[group, tag["summary"], title])
        self.page.wait_for_function(_REFERENCE_AT_REST, arg=domain, polling=100)
        fragment = self.page.evaluate(_REFERENCE_FOLLOW, [group, tag["summary"], title])
        self.assertTrue(fragment)
        landed(fragment)
        self.page.reload(wait_until="load")
        wait_for_reference(self.page)
        self.assertEqual(self.page.evaluate("() => location.hash"), fragment)
        landed(fragment)
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

    scripted = True

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
