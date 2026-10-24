"""perf:ab asks both sides alike, page by page and alternating, and tells a page that
draws more rows from code that got slower."""

from django.test import SimpleTestCase

from tools.perf import ab

PAGE = '<main><table class="data-table"><tbody>{}</tbody></table></main>'


class FakeSide:
    """A side answering every page with ``rows`` rows and the server time ``times`` gives in turn."""

    def __init__(self, name: str, rows: int, times: list[float], asked: list[str]) -> None:
        self.name, self.rows, self.times, self.asked = name, rows, iter(times), asked

    def ask(self, page: str, **flags: bool) -> dict:
        self.asked.append(self.name)
        said = {"status": 200, "timing": f"app;dur={next(self.times)}", "wall_ms": 999.0, "bytes": 2048}
        if flags.get("html"):
            said["html"] = PAGE.format("<tr><td>x</td></tr>" * self.rows)
        if flags.get("queries"):
            said["queries"] = 9
        return said


def sides(base_times: list[float], here_times: list[float], here_rows: int = 3):
    asked: list[str] = []
    return FakeSide("base", 3, base_times, asked), FakeSide("here", here_rows, here_times, asked), asked


class CompareTests(SimpleTestCase):
    def test_each_round_alternates_which_side_goes_first(self):
        warm = 1 + ab.WARM
        base, here, asked = sides([1.0] * (warm + 4), [1.0] * (warm + 4))

        ab.compare(base, here, ["/"], rounds=4)

        timed = asked[2 * warm :]
        self.assertEqual(timed, ["base", "here", "here", "base", "base", "here", "here", "base"])

    def test_the_time_is_the_server_s_own_and_the_rows_are_counted_from_the_html(self):
        warm = 1 + ab.WARM
        base, here, _ = sides([1.0] * warm + [4.0, 4.0], [1.0] * warm + [6.0, 6.0], here_rows=5)

        found = ab.compare(base, here, ["/"], rounds=2)

        self.assertEqual(found["/"]["here"]["ms"], [6.0, 6.0])
        self.assertEqual(found["/"]["here"]["rows"], 5)
        self.assertEqual(found["/"]["base"]["rows"], 3)


class SummaryTests(SimpleTestCase):
    def found(self, base_ms: list[float], here_ms: list[float], here_rows: int = 3) -> list[dict]:
        def side(ms, rows):
            return {"status": 200, "rows": rows, "html_bytes": 2048, "queries": 9, "ms": ms}

        return ab.summarize({"/": {"base": side(base_ms, 3), "here": side(here_ms, here_rows)}})

    def test_a_difference_past_its_spread_is_marked_and_one_inside_it_is_not(self):
        moved = self.found([4.0, 4.1, 3.9, 4.0], [6.0, 6.1, 5.9, 6.0])[0]
        noise = self.found([4.0, 6.0, 3.0, 5.0], [5.0, 3.0, 6.0, 4.0])[0]

        self.assertAlmostEqual(moved["moved_ms"], 2.0)
        self.assertEqual(ab.render([moved])[1].split()[-1], "moved")
        self.assertNotEqual(ab.render([noise])[1].split()[-1], "moved")

    def test_more_rows_on_one_side_reads_beside_the_time(self):
        row = ab.render(self.found([4.0, 4.0], [6.0, 6.0], here_rows=6))[1]

        self.assertIn("6 +3", row)
