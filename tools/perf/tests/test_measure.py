"""A browser visit reads what a page fetched once nothing it asked for is still on its way, and no
page is measured on the browser's first visit."""

from unittest import mock

from django.test import SimpleTestCase

from tools.perf import measure


class LatePart:
    """A page whose load asks for a part, answered a few frames later, that asks for one more."""

    def __init__(self) -> None:
        self.handlers: dict[str, list] = {}
        self.frames = 0
        self.read_while_waiting = None

    def on(self, event: str, handler) -> None:
        self.handlers.setdefault(event, []).append(handler)

    def emit(self, event: str) -> None:
        for handler in self.handlers.get(event, []):
            handler(object())

    def goto(self, _address: str, wait_until: str) -> None:
        assert wait_until == "load"
        self.emit("request")

    def evaluate(self, script: str):
        if script == measure.FETCHED:
            self.read_while_waiting = self.waiting.count
            return {"bytes": 1, "requests": 1}
        self.frames += 1
        if self.frames == 2:
            self.emit("request")
            self.emit("requestfinished")
        if self.frames == 4:
            self.emit("requestfinished")
        return None


class VisitTests(SimpleTestCase):
    def test_what_was_fetched_is_read_once_every_part_asked_for_has_arrived(self):
        page = LatePart()
        page.waiting = measure.InFlight(page)

        measure._visit(page, "http://127.0.0.1:9/", page.waiting)

        self.assertEqual(page.read_while_waiting, 0)
        self.assertGreaterEqual(page.frames, 4)


class BrowserCostTests(SimpleTestCase):
    def test_no_page_is_measured_on_the_browser_s_first_visit(self):
        visits = []

        def visit(_browser, _url, _cookie, page, _viewport):
            visits.append(page)
            return {"script_ms": 16.0 if len(visits) == 1 else 6.0}

        with mock.patch.object(measure, "visit", visit):
            costs = measure.browser_costs(None, "http://127.0.0.1:9", ("s", "k"), ["/b/", "/a/"], [{"width": 402}])

        self.assertEqual({page: seen["402"]["script_ms"] for page, seen in costs.items()}, {"/b/": 6.0, "/a/": 6.0})
