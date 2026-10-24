"""perf's table: a row per page and width, a total, and each number beside its base's."""

import copy
import re

from django.test import SimpleTestCase

from tools.perf import table

# Two pages of a recorded `perf` run's JSON.
RECORDED = {
    "commit": "364c3fe324d8",
    "target": "dev:scratch",
    "measured_at": "2026-10-24T02:38:20+00:00",
    "requests": 5,
    "plan": {"seed": "showcase"},
    "widths": [402, 1360],
    "pages": [
        {
            "page": "/",
            "server": {"status": 200, "p50_ms": 37.21, "p95_ms": 38.05, "queries": 42},
            "browser": {
                "402": {
                    "first": {"bytes": 563445, "requests": 25},
                    "repeat": {"bytes": 20835, "requests": 24},
                    "script_ms": 19.4,
                    "long_tasks": 1,
                    "long_task_ms": 50,
                    "cls": 0.0103,
                },
                "1360": {
                    "first": {"bytes": 563389, "requests": 25},
                    "repeat": {"bytes": 20913, "requests": 24},
                    "script_ms": 4.2,
                    "long_tasks": 0,
                    "long_task_ms": 0,
                    "cls": 0,
                },
            },
        },
        {
            "page": "/domains/",
            "server": {"status": 302, "p50_ms": 7.48, "p95_ms": 7.74, "queries": None},
            "browser": {
                "402": {
                    "first": {"bytes": 557687, "requests": 25},
                    "repeat": {"bytes": 14968, "requests": 24},
                    "script_ms": 6.5,
                    "long_tasks": 0,
                    "long_task_ms": 0,
                    "cls": 0,
                },
                "1360": {
                    "first": {"bytes": 557636, "requests": 25},
                    "repeat": {"bytes": 14948, "requests": 24},
                    "script_ms": 6.3,
                    "long_tasks": 0,
                    "long_task_ms": 0,
                    "cls": 0,
                },
            },
        },
    ],
    "memory": {"idle_bytes": 129434224, "after_pass_bytes": 147309192, "measured": "footprint of the serving process"},
    "cold_start": {"runs_ms": [2921.9, 2748.1, 2831.9], "median_ms": 2831.9},
}


def rows(rendered: str) -> dict[tuple[str, str], dict[str, str]]:
    """Each table row by (page, width), its cells by header: a page's later
    widths carry the page named on its first. A cell but the page is right
    aligned under its header, and a blank one is left out."""

    lines = rendered.splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith("page "))
    head = lines[start]
    names = re.split(r" {2,}", head.strip())
    ends, at = [], 0
    for name in names:
        at = head.index(name, at) + len(name)
        ends.append(at)
    found, page = {}, ""
    for line in lines[start + 1 :]:
        if not line.strip():
            break
        if not line.startswith(" "):
            page = line.split("  ")[0]
        cells = {name: line[:end].rsplit("  ", 1)[-1].strip() for name, end in zip(names[1:], ends[1:], strict=True)}
        found[page, cells["width"]] = {name: cell for name, cell in cells.items() if cell}
    return found


def compared() -> tuple[dict, dict]:
    """RECORDED beside a base: its home page's server time moved 2 ms with a spread of 1, its
    other page's a little, inside its spread."""

    report, base = copy.deepcopy(RECORDED), copy.deepcopy(RECORDED)
    base["commit"] = "0123456789ab"
    report["pages"][0]["server"] |= {"moved_ms": 2.0, "spread_ms": 1.0}
    report["pages"][1]["server"] |= {"moved_ms": 0.4, "spread_ms": 1.0}
    return report, base


class TableTests(SimpleTestCase):
    def test_a_row_per_page_and_width_with_the_server_on_the_first(self):
        found = rows(table.render(RECORDED))

        self.assertEqual(found["/", "402"]["queries"], "42")
        self.assertEqual(found["/", "402"]["p50 ms"], "37.2")
        self.assertNotIn("queries", found["/", "1360"])
        self.assertEqual(found["/", "1360"]["script ms"], "4.2")
        self.assertEqual(found["/", "402"]["first KB"], f"{563445 / 1024:.1f}")

    def test_a_page_that_did_not_answer_200_says_so_and_its_missing_count_is_a_dash(self):
        found = rows(table.render(RECORDED))

        self.assertEqual(found["/domains/ (302)", "402"]["queries"], "-")

    def test_a_page_s_records_and_html_size_sit_beside_its_server_time(self):
        report = copy.deepcopy(RECORDED)
        report["pages"][0]["server"] |= {"rows": 50, "html_bytes": 40960}
        base = copy.deepcopy(report)
        base["pages"][0]["server"] |= {"rows": 25, "html_bytes": 20480}

        found = rows(table.render(report, base))

        self.assertEqual(found["/", "402"]["rows"], "50 +25")
        self.assertEqual(found["/", "402"]["HTML KB"], "40.0 +20.0")

    def test_each_visit_s_paint_times_sit_beside_its_bytes_and_a_report_without_them_shows_a_dash(self):
        report = copy.deepcopy(RECORDED)
        seen = report["pages"][0]["browser"]["402"]
        seen["first"] |= {"fcp_ms": 212.4, "lcp_ms": 388.0}
        seen["repeat"] |= {"fcp_ms": 61.6, "lcp_ms": None}

        found = rows(table.render(report))

        self.assertEqual(found["/", "402"]["first FCP ms"], "212")
        self.assertEqual(found["/", "402"]["first LCP ms"], "388")
        self.assertEqual(found["/", "402"]["repeat FCP ms"], "62")
        self.assertEqual(found["/", "402"]["repeat LCP ms"], "-")
        self.assertEqual(found["/", "1360"]["first FCP ms"], "-")

    def test_the_total_sums_every_page(self):
        found = rows(table.render(RECORDED))

        self.assertEqual(found["total", "402"]["queries"], "42")
        self.assertEqual(found["total", "402"]["p50 ms"], f"{37.21 + 7.48:.1f}")
        self.assertEqual(found["total", "1360"]["first req"], "50")

    def test_memory_and_cold_start_follow_the_table(self):
        rendered = table.render(RECORDED)

        self.assertIn(f"memory idle: {129434224 / 1024**2:.1f} MB", rendered)
        self.assertIn("cold start to /health/ready/, median: 2.83 s", rendered)

    def test_beside_a_base_each_number_says_how_far_it_moved(self):
        base = copy.deepcopy(RECORDED)
        base["commit"] = "0123456789ab"
        base["pages"][0]["server"]["queries"] = 40
        base["pages"][0]["browser"]["402"]["script_ms"] = 20.4
        base["memory"]["idle_bytes"] -= 2 * 1024**2

        rendered = table.render(RECORDED, base)
        found = rows(rendered)

        self.assertIn("beside 0123456789ab", rendered.splitlines()[0])
        self.assertEqual(found["/", "402"]["queries"], "42 +2")
        self.assertEqual(found["/", "402"]["script ms"], "19.4 -1.0")
        self.assertEqual(found["/", "402"]["p50 ms"], "37.2 0")
        self.assertEqual(found["total", "402"]["queries"], "42 +2")
        self.assertIn("MB", next(line for line in rendered.splitlines() if line.startswith("memory idle")))
        self.assertIn("+2.0", next(line for line in rendered.splitlines() if line.startswith("memory idle")))

    def test_beside_a_base_a_page_whose_server_time_moved_past_its_spread_is_marked_and_noise_is_not(self):
        found = rows(table.render(*compared()))

        self.assertEqual(found["/", "402"]["moved ms"], "2.00")
        self.assertEqual(found["/", "402"]["spread ms"], "1.00")
        self.assertEqual(found["/", "402"].get("moved"), "moved")
        self.assertIsNone(found["/domains/ (302)", "402"].get("moved"))

    def test_a_page_the_base_lacks_has_no_difference(self):
        base = copy.deepcopy(RECORDED)
        base["pages"] = base["pages"][:1]

        found = rows(table.render(RECORDED, base))

        self.assertEqual(found["/domains/ (302)", "402"]["p50 ms"], "7.5")


class SummaryTests(SimpleTestCase):
    """The session log shows the summary whole, so it holds the totals and names the table, never every page."""

    def test_the_summary_holds_the_totals_and_names_the_table_of_every_page(self):
        said = table.summary(RECORDED, None, "var/perf/run.table")
        lines = said.splitlines()

        self.assertTrue(any(line.startswith("total ") for line in lines))
        self.assertIn("memory idle", said)
        self.assertIn("var/perf/run.table", lines[-1])
        rows = [line for line in lines if line.startswith("/") or line.startswith(" ")]
        self.assertFalse([line for line in rows if line.split()[1:2] in (["402"], ["1360"])], "no page's width row")

    def test_beside_a_base_it_names_the_pages_whose_server_time_moved_past_its_spread_furthest_first(self):
        report, base = compared()
        report["pages"].append(copy.deepcopy(report["pages"][0]) | {"page": "/far/"})
        report["pages"][-1]["server"] |= {"moved_ms": -9.0, "spread_ms": 1.0}

        listed = table.moved(report, base)

        self.assertIn("past its spread", listed[0])
        self.assertEqual([line.split()[0] for line in listed[2:]], ["/far/", "/"])

    def test_beside_a_base_where_nothing_moved_past_its_spread_it_says_so(self):
        report, base = compared()
        report["pages"][0]["server"]["spread_ms"] = 9.0

        self.assertEqual(table.moved(report, base), ["No page's server time moved past its spread."])

    def test_without_a_base_it_names_the_slowest_pages(self):
        listed = table.moved(RECORDED)

        self.assertIn("slowest", listed[0])
        self.assertTrue(listed[2].startswith("/ "), listed)
