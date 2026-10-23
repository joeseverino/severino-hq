"""dev:look reads what it was asked only from its task, reads --css as a file
from the checkout, reports page by page, and compares against a base in a
worktree it always removes."""

import os
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase, override_settings, tag

from hq.platform.core import browser_look

ASKED = {"usage_pages": "example"}


def _git(*args, cwd):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout


def _repository(folder: Path) -> Path:
    """A repository of one commit, made with no identity or hook of the machine's."""

    _git("init", "--quiet", cwd=folder)
    (folder / "page.txt").write_text("example\n", encoding="utf-8")
    _git("add", "page.txt", cwd=folder)
    _git(
        "-c", "user.name=Example", "-c", "user.email=example@example.com", "-c", "commit.gpgsign=false",
        "commit", "--quiet", "--no-verify", "-m", "example", cwd=folder,
    )  # fmt: skip
    return folder


# Tests of a developer's tool over a checkout and git, which no image ships.
@tag("source")
class AskedTests(SimpleTestCase):
    def test_a_run_without_the_tasks_environment_is_refused_rather_than_read_as_nothing_asked(self):
        with mock.patch.dict(os.environ, clear=True), self.assertRaisesMessage(RuntimeError, "mise run dev:look"):
            browser_look._usage("usage_rule")
        with mock.patch.dict(os.environ, ASKED, clear=True):
            self.assertEqual(browser_look._usage("usage_rule"), "")
            self.assertEqual(browser_look._listed("usage_pages"), ["example"])

    def test_css_is_text_or_a_file_a_relative_path_names_in_the_checkout(self):
        with tempfile.TemporaryDirectory() as checkout, override_settings(BASE_DIR=checkout):
            Path(checkout, "var").mkdir()
            Path(checkout, "var", "defect.css").write_text("main { inline-size: 200vw }", encoding="utf-8")
            self.assertEqual(browser_look._css("var/defect.css"), "main { inline-size: 200vw }")
            self.assertEqual(browser_look._css("main { color: red }"), "main { color: red }")
            self.assertEqual(browser_look._css(""), "")
            with self.assertRaisesMessage(AssertionError, "no file"):
                browser_look._css("var/missing.css")

    def test_eval_is_a_file_a_relative_path_names_in_the_checkout(self):
        with tempfile.TemporaryDirectory() as checkout, override_settings(BASE_DIR=checkout):
            Path(checkout, "var").mkdir()
            Path(checkout, "var", "probe.js").write_text("() => document.title", encoding="utf-8")
            self.assertEqual(browser_look._script("var/probe.js"), "() => document.title")
            self.assertEqual(browser_look._script(""), "")
            with self.assertRaisesMessage(AssertionError, "names no file"):
                browser_look._script("var/missing.js")

    def test_what_eval_returned_reads_under_its_page_and_width(self):
        lines = browser_look._evaluated("example/one", 402, {"inlineSize": "372px", "chain": ["td", "tr"]}).splitlines()
        self.assertEqual(lines[0], "example/one at 402:")
        self.assertTrue(all(line.startswith("  ") for line in lines[1:]))
        self.assertIn('"372px"', "\n".join(lines))
        self.assertEqual(browser_look._evaluated("example/one", 402, "a\nb"), "example/one at 402:\n  a\n  b")
        self.assertEqual(browser_look._evaluated("example/one", 402, None), "example/one at 402:\n  null")


@tag("source")
class ReportTests(SimpleTestCase):
    FOUND = {
        ("example/one", 402): ["overflow: a box past the edge"],
        ("example/one", 1360): [],
        ("example/two", 402): [],
        ("example/two", 1360): [],
        ("example/three", 402): ["overlap: two names", "frames: a frame"],
        ("example/three", 1360): [],
    }

    def test_findings_are_grouped_under_their_page_and_width(self):
        lines = browser_look._grouped(self.FOUND, "survey").splitlines()
        for page in ("example/one", "example/two", "example/three"):
            self.assertEqual(lines.count(page), 1)
        three = lines.index("example/three")
        width = next(index for index, line in enumerate(lines[three:], three) if line.strip().startswith("402"))
        self.assertEqual([line.strip() for line in lines[width + 1 : width + 3]], self.FOUND["example/three", 402])

    def test_the_verdict_names_every_page_that_failed_and_no_other(self):
        verdict = browser_look._verdict("survey", self.FOUND)
        self.assertIn("example/one", verdict)
        self.assertIn("example/three", verdict)
        self.assertNotIn("example/two", verdict)
        self.assertIn("holds", browser_look._verdict("survey", {("example/two", 402): []}))

    def test_against_a_base_only_new_findings_fail_and_fixed_ones_are_named(self):
        base = {
            ("example/one", 402): ["overflow: a box past the edge", "overlap: gone now"],
            ("example/gone", 402): ["x"],
        }
        branch = {("example/one", 402): ["overflow: a box past the edge", "frames: a new frame"]}
        new, fixed = browser_look._compare(base, branch)
        self.assertEqual(new, {("example/one", 402): ["frames: a new frame"]})
        # A page the branch did not look at is not fixed by it.
        self.assertEqual(fixed, {("example/one", 402): ["overlap: gone now"]})
        report, failed = browser_look._against("example-ref", "survey", base, branch)
        self.assertTrue(failed)
        self.assertIn("frames: a new frame", report)
        self.assertNotIn("a box past the edge", report)
        _, failed = browser_look._against("example-ref", "survey", base, {("example/one", 402): []})
        self.assertFalse(failed)

    def test_against_a_base_a_finding_whose_numbers_moved_is_neither_new_nor_fixed(self):
        base = {("example/one", 402): ["overflow: row 3 is 412px wide", "overflow: row 5 is 430px wide"]}
        branch = {("example/one", 402): ["overflow: row 4 is 418.5px wide", "frames: row 2 framed twice"]}
        new, fixed = browser_look._compare(base, branch)
        self.assertEqual(new, {("example/one", 402): ["frames: row 2 framed twice"]})
        # One counterpart each: two findings of a shape against one is one fixed.
        self.assertEqual(fixed, {("example/one", 402): ["overflow: row 5 is 430px wide"]})

    def test_a_tables_columns_read_as_drawn_and_at_their_intrinsic_sizes_under_their_headings(self):
        table = {
            "index": 2,
            "label": "Example services",
            "region": 1086,
            "drawn": 1100,
            "held": False,
            "min": 610,
            "max": 1720,
            "columns": [
                {"name": "Name", "shown": True, "drawn": 312, "min": 180, "max": 640},
                {"name": "Status", "shown": True, "drawn": 96, "min": 88, "max": 96},
                {"name": "Owner", "shown": False, "drawn": 0, "min": 0, "max": 0},
            ],
        }
        line = browser_look._columns("example/services", 1086, table)
        self.assertNotIn("\n", line)
        head, columns = line.split(": ", 1)
        for fact in ("example/services", "table 2", "Example services", "1086", "1100", "610", "1720"):
            self.assertIn(fact, head)
        self.assertEqual(
            columns.split(" · "), ["Name 312 (min 180, max 640)", "Status 96 (min 88, max 96)", "Owner gone"]
        )
        unheld = browser_look._columns("example/services", 402, {**table, "region": None, "label": ""})
        self.assertNotIn(" in ", unheld.split(": ", 1)[0])
        self.assertNotIn("held", line)

    def test_a_table_an_open_row_holds_says_it_is_drawn_at_the_held_widths(self):
        table = {
            "index": 1, "label": "", "region": None, "drawn": 402, "held": True, "min": 402, "max": 402,
            "columns": [{"name": "Name", "shown": True, "drawn": 402, "min": 402, "max": 402}],
        }  # fmt: skip
        head = browser_look._columns("example/services", 402, table).split(": ", 1)[0]
        self.assertIn("held by an open row", head)

    def test_findings_and_measures_survive_the_trip_from_the_base_run(self):
        measured = {("example/one", 402): {"columns": "example/one table 1 at 402", "eval": "example/one at 402:\n  1"}}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, "found.json")
            browser_look._write_found(path, self.FOUND, measured)
            found, read = browser_look._read_found(path)
        self.assertEqual(found, self.FOUND)
        self.assertEqual(read[("example/one", 402)], measured[("example/one", 402)])
        self.assertEqual(read[("example/two", 402)], {})

    def test_against_a_base_each_number_the_base_measured_otherwise_follows_its_line(self):
        ours = "example/one at 402:\n  inlineSize 372\n  blockSize 90"
        theirs = "example/one at 402:\n  inlineSize 360\n  blockSize 90\n  extra 1"
        lines = browser_look._beside("example-ref", ours, theirs).splitlines()
        self.assertEqual(lines[:3], ["example/one at 402:", "  inlineSize 372", "example-ref: inlineSize 360"])
        # A line the two share is said once; one only the base has is still said.
        self.assertEqual(lines.count("  blockSize 90"), 1)
        self.assertNotIn("example-ref: blockSize 90", lines)
        self.assertEqual(lines[-1], "example-ref: extra 1")
        self.assertIn("example-ref: (no such line)", browser_look._beside("example-ref", "a\nb", "a"))
        self.assertIn("example-ref: (not measured)", browser_look._beside("example-ref", "a", None))


@tag("source")
class BandTests(SimpleTestCase):
    def test_a_tall_page_is_cut_into_window_sized_bands_that_cover_it_once(self):
        bands = browser_look._bands(2000, 900)
        self.assertEqual(bands, [(0, 900), (900, 900), (1800, 200)])
        self.assertEqual(sum(height for _, height in bands), 2000)

    def test_a_page_the_window_holds_is_not_cut(self):
        self.assertEqual(browser_look._bands(900, 900), [])
        self.assertEqual(browser_look._bands(400, 900), [])


@tag("source")
class EstateAddressTests(SimpleTestCase):
    def test_an_estate_page_keeps_its_query_and_its_path_its_slash(self):
        self.assertEqual(browser_look._estate_path("estate/audit/?awaiting=1"), "/audit/?awaiting=1")
        self.assertEqual(browser_look._estate_path("estate/audit?awaiting=1"), "/audit/?awaiting=1")
        self.assertEqual(browser_look._estate_path("estate/audit"), "/audit/")
        self.assertEqual(browser_look._estate_path("estate"), "/")


@tag("source")
class WorktreeTests(SimpleTestCase):
    def test_the_base_worktree_is_removed_however_the_look_ends(self):
        with tempfile.TemporaryDirectory() as folder:
            checkout = _repository(Path(folder))
            with self.assertRaises(KeyError), browser_look._worktree(checkout, "HEAD") as tree:
                self.assertTrue((tree / "page.txt").is_file())
                raise KeyError("the look broke")
            self.assertFalse(tree.parent.exists())
            self.assertNotIn(str(tree), _git("worktree", "list", "--porcelain", cwd=checkout))

    def test_a_ref_that_names_nothing_is_said_and_leaves_nothing(self):
        with tempfile.TemporaryDirectory() as folder:
            checkout = _repository(Path(folder))
            with (
                self.assertRaisesMessage(AssertionError, "--base example-missing"),
                browser_look._worktree(checkout, "example-missing"),
            ):
                self.fail("a worktree of nothing")
            self.assertEqual(_git("worktree", "list", "--porcelain", cwd=checkout).count("worktree "), 1)
