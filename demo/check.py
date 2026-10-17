"""Checks the built demo (``mise run browser:demo``).

Serves the build over plain HTTP and checks that every link points to a file
that exists, that pages load in WebKit with no errors and nothing from another
site, and that a dismissal survives a reload until Reset demo clears it.
"""

import os
import re
import threading
import unittest
from functools import partial
from html import unescape
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Where demo:build saved it. This file doesn't import Django.
OUT = Path(os.environ.get("HQ_DEMO_OUT") or Path(__file__).resolve().parents[1] / "var" / "demo")

_ADDRESS = re.compile(r'(?:href|src)="(/[^"#?]*)')
# The main pages, including the one with cards to dismiss.
_VISITED = ("/", "/hq/", "/infrastructure/findings/", "/action-items/", "/infrastructure/")


def _exists(path: str) -> bool:
    target = OUT / path.lstrip("/")
    return target.is_file() or (target / "index.html").is_file()


class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class DemoSiteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (OUT / "index.html").is_file():
            raise unittest.SkipTest(f"no demo at {OUT}: run demo:build first")
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), partial(_Quiet, directory=str(OUT)))
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.origin = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_every_address_a_page_names_is_in_the_demo(self):
        missing = set()
        for page in OUT.rglob("*.html"):
            for address in _ADDRESS.findall(page.read_text(encoding="utf-8")):
                if not _exists(unescape(address)):
                    missing.add(f"{page.relative_to(OUT)} -> {address}")
        self.assertEqual(sorted(missing)[:20], [])

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
                    page.goto(self.origin + path, wait_until="load")
                    self.assertTrue(page.locator(".demo-reset").count(), "Reset demo is in the user menu")
            browser.close()
        self.assertEqual(errors, [])
        self.assertEqual(elsewhere, [])

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
            page.evaluate("document.querySelector('.demo-reset').click()")
            page.wait_for_load_state("load")
            page.goto(self.origin + "/action-items/", wait_until="load")
            again = page.locator(f'[data-attention-item]:has(button[value="{key}"])').first
            self.assertIsNone(again.get_attribute("data-dismissed"))
            browser.close()
