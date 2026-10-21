"""Checks the built demo (``mise run browser:demo``).

Serves the build over plain HTTP and checks that every link points to a file
that exists, that pages load in WebKit with no errors and nothing from another
site, that a dismissal survives a reload until Reset demo clears it, and that
the first pages a visitor sees show a healthy homelab with demo data locked on.
"""

import os
import re
import unittest
from html import unescape
from pathlib import Path

from demo.serve import serving

# Where demo:build saved it. This file doesn't import Django.
OUT = Path(os.environ.get("HQ_DEMO_OUT") or Path(__file__).resolve().parents[1] / "var" / "demo")

_ADDRESS = re.compile(r'(?:href|src|data-[\w-]+-url)="(/[^"#?]*)')
# The main pages, including the one with cards to dismiss.
_VISITED = ("/", "/hq/", "/infrastructure/findings/", "/action-items/", "/infrastructure/", "/connection/")
# What a healthy homelab never says on its dashboard.
_ALARMS = ("No reading yet", "Not set up yet", "not fully set up", "not answering", " failed", "not confirmed")
# The showcase leaves three things to do on purpose (an image update, a
# certificate to renew, a route to approve); anything more is the seed missing
# a reading.
_MOST_ITEMS = 3


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

    def test_the_first_glance_is_a_healthy_homelab(self):
        dashboard = (OUT / "index.html").read_text(encoding="utf-8")
        for alarm in _ALARMS:
            with self.subTest(alarm=alarm):
                self.assertNotIn(alarm, dashboard)
        items = (OUT / "action-items" / "index.html").read_text(encoding="utf-8").count('class="attention-title"')
        self.assertLessEqual(items, _MOST_ITEMS)
        connection = (OUT / "connection" / "index.html").read_text(encoding="utf-8")
        self.assertEqual(re.findall(r'class="conn-layer conn-layer-(?!holds")(\w+)"', connection), [])

    def test_each_thing_to_do_offers_a_way_to_do_it(self):
        queue = (OUT / "action-items" / "index.html").read_text(encoding="utf-8")
        rows = re.split(r"<tr [^>]*data-attention-item", queue)[1:]
        self.assertEqual(len(rows), _MOST_ITEMS)
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
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("console", lambda message: message.type == "error" and errors.append(message.text))
            page.on("request", lambda request: request.url.startswith(self.origin) or elsewhere.append(request.url))
            for path in _VISITED:
                with self.subTest(path=path):
                    page.goto(self.origin + path, wait_until="networkidle")
                    self.assertTrue(page.locator(".demo-reset").count(), "Reset demo is in the user menu")
                    switch = page.locator('form[action="/demo/"] [role="switch"]')
                    self.assertEqual(switch.get_attribute("aria-checked"), "true")
                    self.assertTrue(switch.is_disabled(), "Demo data can't be turned off")
            browser.close()
        self.assertEqual(errors, [])
        self.assertEqual(elsewhere, [])

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
