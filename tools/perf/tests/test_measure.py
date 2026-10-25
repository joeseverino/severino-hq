"""A browser visit reads what a page fetched once nothing it asked for is still on its way, and no
page is measured on the browser's first visit. Beside a base, every page is asked of both servers in
turn, which goes first alternating round by round, and a difference is told from noise by its own
spread."""

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


PAGE = '<main><table class="data-table"><tbody>{}</tbody></table></main>'


class FakeServer:
    """A server answering every page with ``rows`` rows and the server time ``times`` gives in turn,
    noting each answer in ``asked`` by its name."""

    def __init__(self, name: str, rows: int, times: list[float | None], asked: list[str]) -> None:
        self.name, self.rows, self.times, self.asked = name, rows, iter(times), asked

    def get(self, _page: str) -> measure.Answer:
        self.asked.append(self.name)
        body = PAGE.format("<tr><td>x</td></tr>" * self.rows).encode()
        return measure.Answer(200, next(self.times), body)

    def close(self) -> None:
        pass


def asked_of(base_times, here_times, rounds: int, here_rows: int = 3):
    asked: list[str] = []
    servers = {
        "base": FakeServer("base", 3, base_times, asked),
        "here": FakeServer("here", here_rows, here_times, asked),
    }
    with mock.patch.object(measure, "Server", lambda url, _cookie: servers[url]):
        found = measure.server_times({"base": ("base", ("s", "k")), "here": ("here", ("s", "k"))}, ["/"], rounds)
    return found, asked


class AlternationTests(SimpleTestCase):
    def test_each_round_alternates_which_server_goes_first(self):
        _, asked = asked_of([1.0] * (measure.WARM + 4), [1.0] * (measure.WARM + 4), rounds=4)

        timed = asked[2 * measure.WARM :]
        self.assertEqual(timed, ["base", "here", "here", "base", "base", "here", "here", "base"])

    def test_the_time_is_the_server_s_own_and_the_rows_are_counted_from_the_html(self):
        warm = [1.0] * measure.WARM
        found, _ = asked_of([*warm, 4.0, 4.0], [*warm, 6.0, None], rounds=2, here_rows=5)

        self.assertEqual(found["here"]["/"]["times_ms"], [6.0, None])
        self.assertEqual(found["here"]["/"]["p50_ms"], 6.0)
        self.assertEqual(found["here"]["/"]["rows"], 5)
        self.assertEqual(found["base"]["/"]["rows"], 3)


class MovedTests(SimpleTestCase):
    def test_a_difference_past_its_spread_is_marked_and_one_inside_it_is_not(self):
        moved = measure.moved([4.0, 4.1, 3.9, 4.0], [6.0, 6.1, 5.9, 6.0])
        noise = measure.moved([4.0, 6.0, 3.0, 5.0], [5.0, 3.0, 6.0, 4.0])

        self.assertAlmostEqual(moved["moved_ms"], 2.0)
        self.assertTrue(measure.past_spread(moved))
        self.assertFalse(measure.past_spread(noise))

    def test_a_difference_past_half_its_spread_but_inside_the_whole_is_noise(self):
        # Across forty pages a half-spread bar is passed by chance about once a run.
        self.assertFalse(measure.past_spread({"moved_ms": 0.8, "spread_ms": 1.0}))
        self.assertTrue(measure.past_spread({"moved_ms": -1.2, "spread_ms": 1.0}))

    def test_only_rounds_with_both_times_are_compared(self):
        self.assertEqual(measure.moved([4.0, None], [6.0, 9.0])["moved_ms"], 2.0)
        self.assertIsNone(measure.moved([None], [6.0]))
        self.assertFalse(measure.past_spread({}))
