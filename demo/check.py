"""Checks the built demo (``mise run browser:demo``).

Serves the build over plain HTTP and checks what demo/README.md lists under
"What the checks hold".
"""

import json
import os
import re
import unittest
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit

from demo.serve import serving
from hq.platform.core.tests.browser.uncaught import Uncaught

# Where demo:build saved it. This file doesn't import Django.
OUT = Path(os.environ.get("HQ_DEMO_OUT") or Path(__file__).resolve().parents[1] / "var" / "demo")

# The policy a saved page carries, and each script tag it holds.
_POLICY = re.compile(r'<meta http-equiv="Content-Security-Policy" content="([^"]*)">')
_SCRIPT = re.compile(r"<script\b[^>]*>", re.IGNORECASE)
_ADDRESS = re.compile(r'(?:href|src|data-[\w-]+-url)="(/[^"#?]*)')
# The main pages, including the one with cards to dismiss.
_VISITED = ("/", "/hq/", "/infrastructure/findings/", "/action-items/", "/infrastructure/", "/connection/")
# What a healthy homelab never says on its dashboard.
_ALARMS = ("No reading yet", "Not set up yet", "not fully set up", "not answering", " failed", "not confirmed")
# The showcase leaves three things to do on purpose (an image update, a
# certificate to renew, an agent's request to approve); anything more is the
# seed missing a reading.
_MOST_ITEMS = 3


_MARK = re.compile(r'\sclass="([^"]*)"|\s(data-[\w-]+)')
# The repository the About dialog links into.
_SOURCE = "https://github.com/joeseverino/severino-hq"
# The one link the dialog makes outside the repository: who made it.
_AUTHOR = "https://jseverino.com"


class _Anchors(HTMLParser):
    """Each link on a page, its attributes by name, and the ids of the
    dialogs it sits in."""

    def __init__(self):
        super().__init__()
        self.anchors: list[tuple[dict[str, str], tuple[str, ...]]] = []
        self._dialogs: list[str] = []

    def handle_starttag(self, tag, attrs):
        found = {name: value or "" for name, value in attrs}
        if tag == "dialog":
            self._dialogs.append(found.get("id", ""))
        elif tag == "a":
            self.anchors.append((found, tuple(self._dialogs)))

    def handle_endtag(self, tag):
        if tag == "dialog" and self._dialogs:
            self._dialogs.pop()


def _anchors(page: str) -> list[tuple[dict[str, str], tuple[str, ...]]]:
    parser = _Anchors()
    parser.feed(page)
    return parser.anchors


_ROOT = Path(__file__).resolve().parents[1]
# The pages of a list demo/build.py saves, from its first (its _MOST_PAGES_OF_A_LIST).
_MOST_PAGES_OF_A_LIST = 6
# The widths HQ's browser gate reads (WIDTHS in core/tests/browser/widths.py), and a small phone's.
_WIDTHS = (390, 402, 820, 1360)
# Each control About this demo is drawn over at some scroll stop: where its
# box meets the control's, the point at the middle of the two is the chip's.
# A control under the sticky header there is the header's, and not counted.
_COVERED_BY_ABOUT = """() => {
  const chip = document.querySelector('.demo-about');
  const targets = [...document.querySelectorAll(
    "a[href], button, summary, select, textarea, input:not([type=hidden]), [tabindex]:not([tabindex='-1'])"
  )].filter((el) => !chip.contains(el) && !el.closest('dialog') && el.checkVisibility());
  const covered = new Set();
  const root = document.documentElement;
  for (let y = 0; ; y += innerHeight / 2) {
    scrollTo(0, y);
    const c = chip.getBoundingClientRect();
    for (const el of targets) {
      const r = el.getBoundingClientRect();
      const left = Math.max(c.left, r.left), right = Math.min(c.right, r.right);
      const top = Math.max(c.top, r.top), bottom = Math.min(c.bottom, r.bottom);
      if (right - left < 1 || bottom - top < 1) continue;
      const hit = document.elementFromPoint((left + right) / 2, (top + bottom) / 2);
      if (!hit || !chip.contains(hit)) continue;
      const said = (el.textContent || el.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').trim();
      covered.add(`${el.tagName.toLowerCase()} ${said.slice(0, 40)}`);
    }
    if (y + innerHeight >= root.scrollHeight) break;
  }
  scrollTo(0, 0);
  return [...covered].slice(0, 10);
}"""


def _sample() -> list[str]:
    """The address of every saved page that draws a class or a script hook no
    earlier page drew, so each template, and each part a template can show, is
    loaded at least once without loading every record's page."""

    seen: set[str] = set()
    picked = []
    for page in sorted(page for page in OUT.rglob("index.html") if "__fragments__" not in page.parts):
        text = page.read_text(encoding="utf-8")
        if 'http-equiv="refresh"' in text:
            continue
        marks = {mark for classes, hook in _MARK.findall(text) for mark in (classes.split() if classes else [hook])}
        if marks - seen:
            seen |= marks
            path = page.parent.relative_to(OUT).as_posix()
            picked.append("/" if path == "." else f"/{path}/")
    return picked


def _pages_linked(here: str, page: str) -> list[str]:
    """The saved-copy key of each page of a list that ``page``, shown at
    ``here``, links under its list, up to the pages the build keeps."""

    keys = []
    for nav in re.findall(r'<nav class="pagination">.*?</nav>', page, re.DOTALL):
        for href in re.findall(r'href="([^"]+)"', nav):
            address = urlsplit(urljoin(here, unescape(href)))
            pairs = sorted((name, value) for name, value in parse_qsl(address.query) if value)
            if pairs and int(dict(pairs).get("page", "1")) <= _MOST_PAGES_OF_A_LIST:
                keys.append(f"{address.path}?{urlencode(pairs)}")
    return keys


def _script_sources(page: str) -> list[str]:
    """The script-src of the policy ``page`` carries, empty when it has none."""

    policy = _POLICY.search(page)
    directives = (directive.split() for directive in unescape(policy[1] if policy else "").split(";"))
    return next((sources for name, *sources in directives if name == "script-src"), [])


def _exists(path: str) -> bool:
    target = OUT / path.lstrip("/")
    return target.is_file() or (target / "index.html").is_file()


def _visible_lines(page: str) -> list[str]:
    """The text a reader sees in ``<main>``, one line per block."""

    main = re.search(r"<main.*?</main>", page, re.DOTALL)
    if not main:
        return []
    text = re.sub(r"<(script|style|template)[^>]*>.*?</\1>", "", main.group(0), flags=re.DOTALL)
    # A newline in the markup is a space on screen; only a block starts a line.
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"</?(?:p|div|li|dd|dt|td|th|h\d|section|header|footer|ul|ol|dl)\b[^>]*>", "\n", text)
    text = unescape(re.sub(r"<[^>]+>", "", text))
    return [line for line in (re.sub(r"\s+", " ", raw).strip() for raw in text.split("\n")) if line]


class DemoSiteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (OUT / "index.html").is_file():
            raise unittest.SkipTest(f"no demo at {OUT}: run demo:build first")
        cls.origin = cls.enterClassContext(serving(OUT))

    def test_every_address_a_page_names_is_in_the_demo(self):
        missing = set()
        for page in OUT.rglob("*.html"):
            for address in _ADDRESS.findall(page.read_text(encoding="utf-8")):
                if not _exists(unescape(address)):
                    missing.add(f"{page.relative_to(OUT)} -> {address}")
        self.assertEqual(sorted(missing)[:20], [])

    def test_every_script_is_one_its_page_s_policy_runs(self):
        """A static page's policy admits same-origin files by 'self': a nonce
        saved into the HTML is the same on every visit and admits whatever
        copies it, and 'strict-dynamic' beside no nonce admits nothing."""

        wrong = []
        for page in sorted(OUT.rglob("*.html")):
            text = page.read_text(encoding="utf-8")
            scripts = _SCRIPT.findall(text)
            if not scripts:
                continue
            sources = _script_sources(text)
            if "'self'" not in sources or any(s == "'strict-dynamic'" or s.startswith("'nonce-") for s in sources):
                wrong.append(f"{page.relative_to(OUT)}: script-src {sources}")
            wrong += [
                f"{page.relative_to(OUT)}: {tag}"
                for tag in scripts
                if "nonce=" in tag or not re.search(r'\ssrc="/(?!/)', tag)
            ]
        self.assertEqual(wrong[:20], [])

    def test_the_first_glance_is_a_healthy_homelab(self):
        dashboard = (OUT / "index.html").read_text(encoding="utf-8")
        for alarm in _ALARMS:
            with self.subTest(alarm=alarm):
                self.assertNotIn(alarm, dashboard)
        items = (OUT / "action-items" / "index.html").read_text(encoding="utf-8").count('class="attention-title"')
        self.assertLessEqual(items, _MOST_ITEMS)
        connection = (OUT / "connection" / "index.html").read_text(encoding="utf-8")
        self.assertEqual(re.findall(r'class="conn-layer conn-layer-(?!holds")(\w+)"', connection), [])

    def test_a_list_s_next_page_is_saved(self):
        # Next under a list, filtered or not, opens a saved copy, up to the
        # pages demo/build.py keeps.
        variants = json.loads((OUT / "__fragments__" / "variants.json").read_text(encoding="utf-8"))["variants"]
        missing = set()
        for file in (page for page in OUT.rglob("index.html") if "__fragments__" not in page.parts):
            text = file.read_text(encoding="utf-8")
            shown = re.search(r'name="hq-demo-address" content="([^"]+)"', text)
            saved = file.parent.relative_to(OUT).as_posix()
            here = unescape(shown.group(1)) if shown else ("/" if saved == "." else f"/{saved}/")
            missing.update(f"{here} -> {key}" for key in _pages_linked(here, text) if key not in variants)
        self.assertEqual(sorted(missing)[:20], [])

    def test_every_expense_has_its_receipt(self):
        # The README's account of the showcase: a year of costs, each receipted.
        expenses = [page for page in (OUT / "expenses").glob("*/index.html") if page.parent.name.isdigit()]
        self.assertTrue(expenses, "the showcase has expenses")
        for page in expenses:
            with self.subTest(expense=page.parent.name):
                lines = _visible_lines(page.read_text(encoding="utf-8"))
                self.assertFalse([line for line in lines if line.startswith("No receipts.")])

    def test_each_thing_to_do_offers_a_way_to_do_it(self):
        queue = (OUT / "action-items" / "index.html").read_text(encoding="utf-8")
        rows = re.split(r"<tr [^>]*data-attention-item", queue)[1:]
        self.assertEqual(len(rows), _MOST_ITEMS)
        # One of them is the change the example agent asked for, held for a person.
        self.assertEqual(len([row for row in rows if 'href="/audit/approval/' in row]), 1)
        for row in rows:
            title = re.search(r'class="attention-title"[^>]*>([^<]*)', row)
            with self.subTest(item=title.group(1) if title else row[:80]):
                actions = re.search(r'class="attention-actions[^"]*">(.*?)</div>', row, re.DOTALL)
                self.assertTrue(actions and re.search(r"<(?:a|button)\b", actions.group(1)))

    def test_what_describes_the_reader_says_it_is_an_example(self):
        connection = (OUT / "connection" / "index.html").read_text(encoding="utf-8")
        self.assertIn("This is an example connection, not yours.", connection)
        dashboard = (OUT / "index.html").read_text(encoding="utf-8")
        # The dashboard's connection reading; the menu's line links to the panel above.
        self.assertEqual(dashboard.count('<span class="example-connection">example connection</span>'), 1)

    def test_no_separator_is_left_with_nothing_after_it(self):
        dangling = set()
        for page in OUT.rglob("index.html"):
            for line in _visible_lines(page.read_text(encoding="utf-8")):
                if line.endswith("·"):
                    dangling.add(f"{page.parent.relative_to(OUT)}: {line[:80]}")
        self.assertEqual(sorted(dangling)[:20], [])

    def test_the_agents_page_shows_an_agent_at_work(self):
        page = (OUT / "agents" / "index.html").read_text(encoding="utf-8")
        seen = [line for line in _visible_lines(page) if "last seen" in line.lower()]
        self.assertTrue(seen, "an agent is listed")
        self.assertRegex(seen[0].lower(), r"last seen (?:\d+ (?:second|minute)s?|just now)")
        refused = re.search(r"<strong>(\d+)</strong> refused in the last day", page)
        self.assertTrue(refused and int(refused.group(1)) > 0, "a refusal is counted")

    def test_every_page_is_drawn_in_the_reader_s_scheme(self):
        # A page without it is painted white first, a flash in dark mode.
        plain = []
        for page in OUT.rglob("*.html"):
            text = page.read_text(encoding="utf-8")
            # A document, not a saved answer such as the liveness check's JSON.
            if "__fragments__" in page.parts or not text.lstrip().lower().startswith("<!doctype html"):
                continue
            if 'name="color-scheme"' not in text:
                plain.append(str(page.relative_to(OUT)))
        self.assertEqual(plain[:20], [])

    def test_pages_load_whole_and_from_nowhere_else(self):
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.webkit.launch()
            page = browser.new_page()
            errors, elsewhere = [], []
            at = [""]
            uncaught = Uncaught(page, lambda: at[0])
            page.on("console", lambda message: message.type == "error" and errors.append(f"{at[0]}: {message.text}"))
            page.on("request", lambda request: request.url.startswith(self.origin) or elsewhere.append(request.url))
            for path in dict.fromkeys([*_VISITED, *_sample()]):
                at[0] = path
                page.goto(self.origin + path, wait_until="networkidle")
                if path not in _VISITED:
                    continue
                with self.subTest(path=path):
                    self.assertTrue(page.locator(".demo-reset").count(), "Reset demo is in the user menu")
                    switch = page.locator('form[action="/demo/"] [role="switch"]')
                    self.assertEqual(switch.get_attribute("aria-checked"), "true")
                    self.assertTrue(switch.is_disabled(), "Demo data can't be turned off")
            errors += uncaught.so_far()
            browser.close()
        self.assertEqual(errors[:20], [])
        self.assertEqual(elsewhere, [])

    def test_the_about_links_lead_to_files_in_the_repository_or_to_its_author(self):
        # The dialog's links and the corner control's own address, which is
        # where it leads without script. Each names a file on main, held here
        # to the checkout the demo was built from.
        dashboard = (OUT / "index.html").read_text(encoding="utf-8")
        about = re.search(r'<dialog[^>]*id="modal-demo-about".*?</dialog>', dashboard, re.DOTALL)
        self.assertTrue(about, "the About dialog is on the page")
        anchors = _anchors(dashboard)
        marks = [found["href"] for found, _ in anchors if "demo-about" in found.get("class", "").split()]
        self.assertEqual(len(marks), 1, "the corner control is a link")
        inside = [found["href"] for found, dialogs in anchors if "modal-demo-about" in dialogs and "href" in found]
        links = [*marks, *inside]
        self.assertIn(_SOURCE, links)
        self.assertIn(_AUTHOR, links)
        for link in links:
            with self.subTest(link=link):
                if link in (_SOURCE, _AUTHOR):
                    continue
                self.assertTrue(link.startswith(f"{_SOURCE}/blob/main/"), link)
                file = link.removeprefix(f"{_SOURCE}/blob/main/")
                self.assertTrue((_ROOT / file).is_file(), f"{file} is in the repository")
        # The day it was saved, written the way HQ writes a day.
        self.assertRegex(about.group(0), r'Saved <time datetime="[^"]+" title="[^"]+">[^<]+</time>\.')

    def test_about_this_demo_opens_by_keyboard_from_the_page_s_end(self):
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.webkit.launch()
            for width in _WIDTHS:
                page = browser.new_page(viewport={"width": width, "height": 700})
                page.goto(self.origin + "/", wait_until="load")
                with self.subTest(width=width):
                    control = page.get_by_role("link", name="About this demo")
                    self.assertTrue(control.is_visible())
                    page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
                    content_end = page.evaluate("document.getElementById('content').getBoundingClientRect().bottom")
                    self.assertLessEqual(content_end, control.bounding_box()["y"], "it follows the page's content")
                    self._assert_opens_and_closes(page, control)
                page.close()
            browser.close()

    def test_about_this_demo_covers_no_control(self):
        # Pages with controls along their left edge and at their foot: a
        # list's tick boxes and pages, the dashboard's calendar. Read at every
        # scroll stop, so a control drawn over the page at any one is found.
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.webkit.launch()
            for width in _WIDTHS:
                page = browser.new_page(viewport={"width": width, "height": 700})
                for path in ("/", "/infrastructure/containers/", "/action-items/"):
                    page.goto(self.origin + path, wait_until="load")
                    with self.subTest(path=path, width=width):
                        self.assertEqual(page.evaluate(_COVERED_BY_ABOUT), [])
                page.close()
            browser.close()

    def _assert_opens_and_closes(self, page, trigger):
        dialog = page.locator("#modal-demo-about")
        trigger.focus()
        page.keyboard.press("Enter")
        self.assertTrue(dialog.evaluate("dialog => dialog.open"), "Enter opens it")
        self.assertTrue(dialog.get_by_role("heading", name="About this demo").is_visible())
        page.keyboard.press("Escape")
        self.assertFalse(dialog.evaluate("dialog => dialog.open"), "Escape closes it")

    def test_a_missing_address_is_answered_by_hq_s_own_not_found_page(self):
        # GitHub Pages serves 404.html for an address it has no file for.
        page = (OUT / "404.html").read_text(encoding="utf-8")
        self.assertIn('class="site-header"', page)
        self.assertIn("/static/js/demo.js", page)

    def test_the_api_reference_names_no_server_of_the_demo_s_own(self):
        document = json.loads((OUT / "api" / "v2" / "openapi.json").read_text(encoding="utf-8"))
        self.assertTrue(document.get("servers"), "Scalar would offer the page's own origin")

    def test_the_dashboard_keeps_its_readings_once_it_settles(self):
        # The strip asks for its due readings as the page opens; the demo
        # answers, and the homelab and the forecast are still there after.
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.webkit.launch()
            page = browser.new_page()
            page.goto(self.origin + "/", wait_until="networkidle")
            homelab = page.locator('.glance-panel[data-fragment-part^="machine-"]')
            self.assertTrue(homelab.first.is_visible(), "the homelab's reading is on the dashboard")
            self.assertTrue(homelab.first.locator('a[href="/infrastructure/containers/"]').count())
            weather = page.locator('.glance-panel[data-fragment-part="weather"]')
            self.assertTrue(weather.is_visible(), "the forecast is on the dashboard")
            self.assertIn("°", weather.locator(".glance-lead").inner_text())
            self.assertTrue(weather.locator(".glance-hours li").count())
            for width in (402, 1360):
                page.set_viewport_size({"width": width, "height": 900})
                with self.subTest(width=width):
                    self.assertEqual(self._lines_past_their_reading(page), [])
            browser.close()

    @staticmethod
    def _lines_past_their_reading(page) -> list[str]:
        """Each reading's quiet line that runs past the reading it is under."""

        return page.locator(".dash-readings .glance-caption").evaluate_all(
            """lines => lines.filter(line => {
                const own = line.closest('.glance-panel, .dash-reading').getBoundingClientRect();
                return line.getBoundingClientRect().right > own.right + 1;
            }).map(line => line.textContent.trim())"""
        )

    def test_a_machine_s_addresses_read_as_one_list_that_fits(self):
        from playwright.sync_api import sync_playwright

        machines = sorted(
            {
                f"/infrastructure/machines/{page.parent.name}/"
                for page in (OUT / "infrastructure" / "machines").glob("*/index.html")
            }
        )
        self.assertTrue(machines)
        with sync_playwright() as playwright:
            browser = playwright.webkit.launch()
            for width in (402, 1360):
                page = browser.new_page(viewport={"width": width, "height": 900})
                for path in machines:
                    page.goto(self.origin + path, wait_until="load")
                    addresses = page.locator("dt:text-is('Addresses') + dd")
                    if not addresses.count():
                        continue
                    with self.subTest(path=path, width=width):
                        self._assert_one_list(addresses, whole=width > 900)
                page.close()
            browser.close()

    def _assert_one_list(self, addresses, *, whole: bool):
        """Every address named by its network, the networks in one column and
        the addresses in the next, each inside its value; on a wide page, each
        on one line."""

        rows = addresses.evaluate(
            """dd => [...dd.querySelectorAll('li')].map(li => {
                const [label, code] = [li.querySelector('span'), li.querySelector('code')];
                const box = code.getBoundingClientRect();
                return {label: label?.textContent.trim(), labelX: label?.getBoundingClientRect().left,
                        x: box.left, overrun: box.right - dd.getBoundingClientRect().right,
                        lines: code.getClientRects().length, address: code.textContent};
            })"""
        )
        self.assertTrue(rows, "the addresses are a list")
        self.assertTrue(all(row["label"] for row in rows), rows)
        self.assertEqual(len({round(row["labelX"]) for row in rows}), 1, rows)
        self.assertEqual(len({round(row["x"]) for row in rows}), 1, rows)
        self.assertTrue(all(row["overrun"] <= 1 for row in rows), rows)
        self.assertEqual(len({row["address"] for row in rows}), len(rows), rows)
        if whole:
            self.assertTrue(all(row["lines"] == 1 for row in rows), rows)

    def test_a_dismissal_is_kept_until_the_demo_is_reset(self):
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.webkit.launch()
            page = browser.new_page()
            page.goto(self.origin + "/action-items/", wait_until="load")
            row = page.locator("[data-attention-item]:has([data-triage] button)").first
            key = row.locator("[data-triage] button").get_attribute("value")
            row.locator("[data-triage] button").click()
            page.reload(wait_until="load")
            kept = page.locator(f'[data-attention-item]:has(button[value="{key}"])').first
            self.assertIsNotNone(kept.get_attribute("data-dismissed"))
            with page.expect_navigation(wait_until="load"):
                page.evaluate("document.querySelector('.demo-reset').click()")
            page.goto(self.origin + "/action-items/", wait_until="load")
            again = page.locator(f'[data-attention-item]:has(button[value="{key}"])').first
            self.assertIsNone(again.get_attribute("data-dismissed"))
            browser.close()
