"""Real-browser layout checks: manage.py test core.browser_tests --parallel=1.

This filename does not match Django's default test*.py discovery, so the plain
suite needs neither Playwright nor a browser. `scripts/check.sh` runs it with
CHECK_BROWSER=1 and `scripts/ci-local.sh` always does.

Pages render through their real views over a synthetic example.* estate
(core/tests/test_browser_fixtures.py), then load in a browser whose every request is
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

from core.tests.test_browser_fixtures import PAGES, build_estate, render_pages

WIDTHS = (320, 390, 768, 1280)
# A phone, a tablet held upright, a laptop: where only a table may scroll sideways.
OVERFLOW_WIDTHS = (375, 820, 1360)
# The stylesheet's phone breakpoint: `@media (max-width: 640px)`.
PHONE = 640
ORIGIN = "http://hq.example.test"
AUDIT = Path(settings.BASE_DIR) / "scripts" / "layout-audit.js"

SELECTORS = {
    "highlights": ".dashboard-highlights > .highlight-card",
    "patterns": ".dashboard-patterns > .card",
    "pill": ".pill",
    # The one box allowed to scroll sideways.
    "table_scroll": ".table-scroll",
}

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
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Playwright is not installed. Run: .venv/bin/python -m pip install "
                "--require-hashes -r requirements-browser.txt && "
                ".venv/bin/python -m playwright install chromium"
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
        # Layout must hold before progressive enhancement, so script is off.
        self.context = self.browser.new_context(java_script_enabled=False)
        self.addCleanup(self.context.close)
        self.context.set_default_timeout(5000)
        self.context.route("**/*", self.respond)
        self.page = self.context.new_page()
        self.addCleanup(self.capture_failure)

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

    def test_nothing_escapes_the_page_sideways(self):
        """Closed, and with every disclosure and popover open."""

        def check(_name):
            self.assertEqual(self.page.evaluate(_ESCAPES), [])
            self.page.evaluate(
                "() => document.querySelectorAll('details').forEach((d) => { d.open = true; })"
            )
            self.assertEqual(self.page.evaluate(_ESCAPES), [])

        self.each(check)

    def test_only_a_table_scrolls_sideways(self):
        """At a phone, tablet and laptop width, with every disclosure open."""

        for name in PAGES:
            for width in OVERFLOW_WIDTHS:
                with self.subTest(page=name, width=width):
                    self.open(name, width)
                    self.assertEqual(self.page.evaluate(_SIDEWAYS, SELECTORS["table_scroll"]), [])
                    self.page.evaluate(
                        "() => document.querySelectorAll('details').forEach((d) => { d.open = true; })"
                    )
                    self.assertEqual(self.page.evaluate(_SIDEWAYS, SELECTORS["table_scroll"]), [])

    def test_nothing_runs_out_of_its_table_cell(self):
        for name in PAGES:
            for width in OVERFLOW_WIDTHS:
                with self.subTest(page=name, width=width):
                    self.open(name, width)
                    self.assertEqual(self.page.evaluate(_CELLS), [])

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
        for key in SELECTORS:
            with self.subTest(cards=key):
                self.assertEqual(self.boxes(key), [])
