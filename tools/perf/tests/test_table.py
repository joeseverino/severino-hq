"""perf:report's table: a row per page and width, a total, and each number beside its base's."""

import copy

from django.test import SimpleTestCase

from tools.perf import table

# Two pages of a recorded `perf:report` run's JSON.
RECORDED = {
    "commit": "364c3fe324d8",
    "target": "dev:scratch",
    "measured_at": "2026-10-24T02:38:20+00:00",
    "requests": 5,
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
    widths carry the page named on its first."""

    lines = rendered.splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith("page "))
    found, page = {}, ""
    for line in lines[start + 1 :]:
        if not line.strip():
            break
        # Two spaces separate cells; a cell with a base's difference holds one.
        cells = [cell.strip() for cell in line.split("  ") if cell.strip()]
        if not line.startswith(" "):
            page = cells.pop(0)
        server = len(cells) == len(table.HEADERS) - 1
        headers = [name for name in table.HEADERS[1:] if server or name not in {"p50 ms", "p95 ms", "queries"}]
        found[page, cells[0]] = dict(zip(headers, cells, strict=True))
    return found


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

    def test_a_page_the_base_lacks_has_no_difference(self):
        base = copy.deepcopy(RECORDED)
        base["pages"] = base["pages"][:1]

        found = rows(table.render(RECORDED, base))

        self.assertEqual(found["/domains/ (302)", "402"]["p50 ms"], "7.5")
