"""An uncaught error fails every check that drives a browser, but WebKit's line
for a fetch that was cancelled, which it writes even when the page handled the
rejection."""

import ast
from pathlib import Path
from types import SimpleNamespace

from django.test import SimpleTestCase, tag

from hq.platform.application.tests.source import parsed, source_files
from hq.platform.core.tests.browser.uncaught import Uncaught

ROOT = Path(__file__).resolve().parents[4]
POLL = "https://hq.example.test/health/live/?t=3871"


class Page:
    """What Playwright's page hands its listeners, played by hand."""

    def __init__(self):
        self.listeners = {}

    def on(self, event, listener):
        self.listeners.setdefault(event, []).append(listener)

    def emit(self, event, value):
        for listener in self.listeners.get(event, []):
            listener(value)


def thrown(name, message, stack=""):
    return SimpleNamespace(name=name, message=message, stack=stack)


# WebKit's line for a failed fetch as Playwright hands it on, split at its
# first colon (recorded from a WebKit leg in CI).
FETCH_LINE = thrown("Fetch API cannot load https", "/hq.example.test/health/live/?t=3871 due to access control checks.")
# What WebKit adds when no script handles the rejection.
UNHANDLED = thrown("Unhandled Promise Rejection", "TypeError: Load failed")


class UncaughtTests(SimpleTestCase):
    def setUp(self):
        self.page = Page()
        self.uncaught = Uncaught(self.page, lambda: "/example/")

    def test_a_cancelled_fetch_the_page_handled_is_not_uncaught(self):
        self.page.emit("requestfailed", SimpleNamespace(url=POLL, failure="cancelled"))
        self.page.emit("pageerror", FETCH_LINE)
        self.assertEqual(self.uncaught.so_far(), [])

    def test_a_cancellation_heard_after_its_fetch_s_line_still_answers_it(self):
        self.page.emit("pageerror", FETCH_LINE)
        self.page.emit("requestfailed", SimpleNamespace(url=POLL, failure="cancelled"))
        self.assertEqual(self.uncaught.so_far(), [])

    def test_a_cancelled_fetch_no_script_handled_is_uncaught(self):
        self.page.emit("requestfailed", SimpleNamespace(url=POLL, failure="cancelled"))
        self.page.emit("pageerror", FETCH_LINE)
        self.page.emit("pageerror", UNHANDLED)
        (line,) = self.uncaught.so_far()
        self.assertIn("/example/: uncaught Unhandled Promise Rejection", line)

    def test_a_fetch_that_failed_another_way_is_uncaught(self):
        for failure in (None, "Blocked by Web Inspector"):
            with self.subTest(failure=failure):
                self.page.emit("requestfailed", SimpleNamespace(url=POLL, failure=failure))
                self.page.emit("pageerror", FETCH_LINE)
                (line,) = self.uncaught.so_far()
                self.assertIn("/hq.example.test/health/live/", line)

    def test_a_cancelled_fetch_answers_only_its_own_line(self):
        self.page.emit("requestfailed", SimpleNamespace(url="https://hq.example.test/other/", failure="cancelled"))
        self.page.emit("pageerror", FETCH_LINE)
        self.assertEqual(len(self.uncaught.so_far()), 1)

    def test_a_thrown_error_names_its_page_and_first_frame_once(self):
        self.page.emit("pageerror", thrown("TypeError", "example", "TypeError: example\n    at plant (/x.js:1:2)"))
        self.assertEqual(self.uncaught.so_far(), ["/example/: uncaught TypeError: example at plant (/x.js:1:2)"])
        self.assertEqual(self.uncaught.so_far(), [])


@tag("source", "tree")
class OneListenerTests(SimpleTestCase):
    def test_every_check_hears_what_a_page_throws_through_uncaught(self):
        """A listener of its own would answer an engine's misreport its own way, or not at all."""

        listening = sorted(
            str(path.relative_to(ROOT))
            for path in source_files(ROOT, "*.py")
            for node in ast.walk(parsed(path))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"on", "once", "expect_event", "wait_for_event"}
            and any(isinstance(arg, ast.Constant) and arg.value == "pageerror" for arg in node.args)
        )
        self.assertEqual(listening, ["hq/platform/core/tests/browser/uncaught.py"])
