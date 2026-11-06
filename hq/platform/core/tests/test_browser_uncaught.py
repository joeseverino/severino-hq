"""An uncaught error fails every check that drives a browser, but WebKit's line
for a failed fetch, which it writes even when the page handled the rejection."""

from types import SimpleNamespace

from django.test import SimpleTestCase

from hq.platform.core.tests.browser.uncaught import Uncaught


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

    def test_a_failed_fetch_the_page_handled_is_not_uncaught(self):
        self.page.emit("pageerror", FETCH_LINE)
        self.assertEqual(self.uncaught.so_far(), [])

    def test_a_failed_fetch_no_script_handled_is_uncaught(self):
        self.page.emit("pageerror", FETCH_LINE)
        self.page.emit("pageerror", UNHANDLED)
        (line,) = self.uncaught.so_far()
        self.assertIn("/example/: uncaught Unhandled Promise Rejection", line)

    def test_a_thrown_error_names_its_page_and_first_frame_once(self):
        self.page.emit("pageerror", thrown("TypeError", "example", "TypeError: example\n    at plant (/x.js:1:2)"))
        self.assertEqual(self.uncaught.so_far(), ["/example/: uncaught TypeError: example at plant (/x.js:1:2)"])
        self.assertEqual(self.uncaught.so_far(), [])
