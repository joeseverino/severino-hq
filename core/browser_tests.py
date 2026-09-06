"""Opt-in real-browser checks: manage.py test core.browser_tests --parallel=1.

This filename intentionally does not match Django's default test*.py discovery.
The normal host gate needs neither Playwright nor a browser installation.
"""

import mimetypes
import os
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from django.conf import settings
from django.test import SimpleTestCase

from core.test_browser_fixtures import dashboard_html


class DashboardBrowserTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Install requirements-browser.txt to run browser checks."
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
        # Layout must work before progressive enhancement. Routing fulfills every
        # request locally: the fixture cannot contact a real endpoint or account.
        self.context = self.browser.new_context(java_script_enabled=False)
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.addCleanup(self.capture_failure)

    def capture_failure(self):
        result = self._outcome.result
        if any(
            getattr(test, "test_case", test) is self
            for test, _ in result.failures + result.errors
        ):
            folder = Path(tempfile.mkdtemp(prefix="hq-browser-failure-"))
            self.page.screenshot(path=str(folder / "viewport.png"))
            print(f"Browser failure screenshot: {folder / 'viewport.png'}")

    def open_dashboard(self, width=1359, *, populated=True, contacts=False):
        html = dashboard_html(populated=populated, contacts=contacts)
        static_root = (Path(settings.BASE_DIR) / "static").resolve()

        def respond(route):
            path = urlsplit(route.request.url).path
            if path == "/":
                route.fulfill(content_type="text/html", body=html)
                return
            asset = (static_root / path.removeprefix("/static/")).resolve()
            if (
                path.startswith("/static/")
                and asset.is_relative_to(static_root)
                and asset.is_file()
            ):
                route.fulfill(
                    content_type=mimetypes.guess_type(asset)[0]
                    or "application/octet-stream",
                    body=asset.read_bytes(),
                )
            else:
                route.fulfill(status=404, body="Not in the synthetic fixture")

        self.page.set_viewport_size({"width": width, "height": 844})
        self.context.unroute_all()
        self.context.route("**/*", respond)
        self.page.goto("http://example.invalid/", wait_until="load")
        self.assertEqual(
            self.page.locator(".dashboard-board").evaluate(
                "e => getComputedStyle(e).display"
            ),
            "grid",
            "The real stylesheet must load before measuring layout.",
        )

    def boxes(self, selector):
        return self.page.locator(selector).evaluate_all(
            "elements => elements.map(e => { const r = e.getBoundingClientRect(); "
            "return {top:r.top, bottom:r.bottom, left:r.left, right:r.right}; })"
        )

    def test_paired_cards_align_despite_different_content_heights(self):
        self.open_dashboard()
        for selector in (".highlight-card", ".dashboard-patterns > .card"):
            with self.subTest(selector=selector):
                boxes = self.boxes(selector)
                self.assertEqual(len(boxes), 2)
                self.assertAlmostEqual(boxes[0]["top"], boxes[1]["top"], delta=1)
                self.assertAlmostEqual(boxes[0]["bottom"], boxes[1]["bottom"], delta=1)

    def test_tall_sidebar_cannot_push_activity_down(self):
        for contacts in (False, True):
            with self.subTest(contacts=contacts):
                self.open_dashboard(contacts=contacts)
                before = self.boxes(".activity-tail")[0]["top"]
                self.page.locator(".command-side").evaluate(
                    "e => e.style.minHeight = '3000px'"
                )
                after = self.boxes(".activity-tail")[0]["top"]
                self.assertAlmostEqual(before, after, delta=1)
                stack = self.boxes(".dashboard-main > *")
                gap = self.page.locator(".dashboard-main").evaluate(
                    "e => parseFloat(getComputedStyle(e).rowGap)"
                )
                for previous, current in zip(stack, stack[1:]):
                    self.assertAlmostEqual(
                        current["top"] - previous["bottom"], gap, delta=1
                    )

    def test_narrow_layout_contains_content_and_stacks_cards(self):
        for width in (320, 390, 768):
            with self.subTest(width=width):
                self.open_dashboard(width)
                self.assertLessEqual(
                    self.page.evaluate("document.documentElement.scrollWidth"), width
                )
                if width < 640:
                    cards = self.boxes(".highlight-card")
                    self.assertGreater(cards[1]["top"], cards[0]["bottom"])
                    self.assertAlmostEqual(cards[0]["left"], cards[1]["left"], delta=1)
                    self.assertEqual(
                        len(
                            {
                                box["top"]
                                for box in self.boxes(
                                    ".highlight-card:first-child .kpi"
                                )
                            }
                        ),
                        2,
                    )

    def test_host_without_overview_contributors_still_flows(self):
        self.open_dashboard(populated=False)
        self.assertEqual(self.page.locator(".highlight-card").count(), 0)
        stack = self.boxes(".dashboard-main > *")
        self.assertGreater(stack[-1]["top"], stack[0]["bottom"])
        self.assertLess(stack[-1]["top"] - stack[0]["bottom"], 32)
