"""dev:look reads what it was asked only from its task, reads --css as a file
from the checkout, reports page by page, and compares against a base in a
worktree it always removes."""

import os
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase, override_settings, tag

from hq.platform.core import browser_look, look_report

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


class _Popovers:
    """A page's popovers as the look's scripts reach them: some shown, one
    that will not show."""

    def __init__(self, ids, refused=()):
        self.ids, self.refused, self.shown = ids, set(refused), set()

    def evaluate(self, script, place=None):
        if script == browser_look._POPOVERS:
            return len(self.ids)
        popover = self.ids[place]
        if script == browser_look._SHOW_POPOVER:
            if popover in self.refused:
                return None
            self.shown.add(popover)
            return f"#{popover}"
        self.shown.discard(popover)
        return None


class _Lens:
    """The look's walk over popovers, with a rule that finds a box on the page
    beneath, one inside a popover, and two popovers lying over each other."""

    with_each_popover = browser_look.Look.with_each_popover

    def __init__(self, page):
        self.page = page

    def settle(self):
        pass

    def judged(self, _name, _width, _rule):
        shown = sorted(self.page.shown)
        found = ["frames: a box beneath"]
        found += [f"overlaps: {one} over {other}" for one in shown for other in shown if one < other]
        return found + [f"cut: a key in {popover}" for popover in shown]


@tag("source")
class PopoverTests(SimpleTestCase):
    def test_each_popover_is_judged_alone_and_only_for_what_it_adds_to_the_page(self):
        page = _Popovers(["origin-one", "origin-two", "origin-three"], refused=["origin-three"])
        found = _Lens(page).with_each_popover("example/one", 820, "survey", ["frames: a box beneath"])
        self.assertEqual(
            found,
            ["with #origin-one shown: cut: a key in origin-one", "with #origin-two shown: cut: a key in origin-two"],
        )
        self.assertEqual(page.shown, set())


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
        lines = look_report.grouped(self.FOUND, "survey").splitlines()
        for page in ("example/one", "example/two", "example/three"):
            self.assertEqual(lines.count(page), 1)
        three = lines.index("example/three")
        width = next(index for index, line in enumerate(lines[three:], three) if line.strip().startswith("402"))
        self.assertEqual([line.strip() for line in lines[width + 1 : width + 3]], self.FOUND["example/three", 402])

    def test_the_verdict_names_every_page_that_failed_and_no_other(self):
        verdict = look_report.verdict_line("survey", self.FOUND)
        self.assertIn("example/one", verdict)
        self.assertIn("example/three", verdict)
        self.assertNotIn("example/two", verdict)
        self.assertIn("holds", look_report.verdict_line("survey", {("example/two", 402): []}))

    def test_against_a_base_only_new_findings_fail_and_fixed_ones_are_named(self):
        base = {
            ("example/one", 402): ["overflow: a box past the edge", "overlap: gone now"],
            ("example/gone", 402): ["x"],
        }
        branch = {("example/one", 402): ["overflow: a box past the edge", "frames: a new frame"]}
        new, fixed = look_report.compare(base, branch)
        self.assertEqual(new, {("example/one", 402): ["frames: a new frame"]})
        # A page the branch did not look at is not fixed by it.
        self.assertEqual(fixed, {("example/one", 402): ["overlap: gone now"]})
        report, printed, failed = look_report.against("example-ref", "survey", base, branch)
        self.assertTrue(failed)
        self.assertIn("frames: a new frame", report)
        self.assertIn("overlap: gone now", report)
        # The base's finding is kept, under all this checkout found, never as new.
        self.assertNotIn("a box past the edge", report.split("Fixed against")[0])
        self.assertIn("a box past the edge", report)
        # Printed: each new finding under its page and width, then the verdict.
        self.assertEqual(len(printed), 2)
        self.assertIn("example/one at 402", printed[0])
        self.assertIn("frames: a new frame", printed[0])
        self.assertTrue(printed[-1].startswith("survey against example-ref"))
        _, printed, failed = look_report.against("example-ref", "survey", base, {("example/one", 402): []})
        self.assertFalse(failed)
        self.assertEqual(len(printed), 1)

    def test_a_run_prints_a_line_per_new_finding_and_its_verdict_and_reports_the_rest(self):
        base = {key: said[:1] for key, said in self.FOUND.items()}
        report, printed, failed = look_report.closing("survey", self.FOUND, base, "example-ref")
        self.assertTrue(failed)
        self.assertEqual(len(printed), 1 + 1)
        self.assertIn("frames: a frame", printed[0])
        self.assertIn("overlap: two names", report)
        report, printed, failed = look_report.closing("survey", self.FOUND, None, "")
        self.assertTrue(failed)
        self.assertEqual(printed, [look_report.verdict_line("survey", self.FOUND)])
        for line in ("overflow: a box past the edge", "overlap: two names", "frames: a frame"):
            self.assertIn(line, report)
        _, printed, failed = look_report.closing("survey", self.FOUND, self.FOUND, "example-ref")
        self.assertFalse(failed)
        self.assertEqual(len(printed), 1)

    def test_against_a_base_a_finding_whose_numbers_moved_is_neither_new_nor_fixed(self):
        base = {("example/one", 402): ["overflow: row 3 is 412px wide", "overflow: row 5 is 430px wide"]}
        branch = {("example/one", 402): ["overflow: row 4 is 418.5px wide", "frames: row 2 framed twice"]}
        new, fixed = look_report.compare(base, branch)
        self.assertEqual(new, {("example/one", 402): ["frames: row 2 framed twice"]})
        # One counterpart each: two findings of a shape against one is one fixed.
        self.assertEqual(fixed, {("example/one", 402): ["overflow: row 5 is 430px wide"]})

    # Findings as two runs of the same checkout wrote them: each run makes its
    # records anew, so their UUIDs and ids differ, and a finding cuts its text.
    FIRST_RUN = [
        "cells: div#origin-hqauditapprovala9fe19de-601b-4d02-9088-41d7ba7d5f01.modal.origin-panel"
        " runs 102px past its cell: an-example-agent-with-a-long-name wants",
        "cells: dd runs 168px past its cell: hq.audit:approval:a9fe19de-601b-4d02-908",
        "cells: div#origin-hqinfrastructurefindingcontroller-sweep-stalecontrollere3d7aae8ff21.modal.origin-panel"
        " runs 40px past its cell: The controller on example-controller has",
    ]
    SECOND_RUN = [
        "cells: div#origin-hqauditapproval53dac582-0ef6-43e8-a5e8-80f01bcdc43b.modal.origin-panel"
        " runs 135px past its cell: an-example-agent-with-a-long-name wants",
        "cells: dd runs 79px past its cell: hq.audit:approval:53dac582-0ef6-43e8-a5e",
        "cells: div#origin-hqinfrastructurefindingcontroller-sweep-stalecontrollerfa0be9c1d7aa.modal.origin-panel"
        " runs 41px past its cell: The controller on example-controller has",
    ]

    def test_a_finding_naming_what_each_run_makes_anew_is_the_same_finding_on_every_run(self):
        for first, second in zip(self.FIRST_RUN, self.SECOND_RUN, strict=True):
            self.assertEqual(look_report.finding_key(first), look_report.finding_key(second))
        new, fixed = look_report.compare(
            {("example/one", 402): self.FIRST_RUN}, {("example/one", 402): self.SECOND_RUN}
        )
        self.assertEqual((new, fixed), ({}, {}))

    def test_findings_that_differ_in_what_they_say_stay_apart(self):
        keys = {
            look_report.finding_key(line)
            for line in (
                self.FIRST_RUN[0],
                self.FIRST_RUN[1],
                "cells: dt runs 168px past its cell: hq.audit:approval:a9fe19de-601b-4d02-908",
                "overflow: dd runs 168px past its cell: hq.audit:approval:a9fe19de-601b-4d02-908",
                # The same box, past its cell by an order of magnitude more.
                "cells: dd runs 900px past its cell: hq.audit:approval:a9fe19de-601b-4d02-908",
                "cells: dd runs 2px past its cell: hq.audit:approval:a9fe19de-601b-4d02-908",
                "contrast: span.muted 1.50:1 on 255,255,255",
                "contrast: span.muted 4.40:1 on 255,255,255",
            )
        }
        self.assertEqual(len(keys), 8)
        # A few pixels either way, or a contrast a hair off, is the same finding.
        for one, other in (
            ("cells: dd runs 412px past its cell", "cells: dd runs 430px past its cell"),
            ("contrast: span.muted 2.10:1 on 255,255,255", "contrast: span.muted 2.30:1 on 255,255,255"),
            # The same box with the same fault, on another record's row.
            (
                "small targets: a in td.row-gutter is hit through 112x16px: Tailnet hardening",
                "small targets: a in td.row-gutter is hit through 101x16px: Offsite backups",
            ),
        ):
            self.assertEqual(look_report.finding_key(one), look_report.finding_key(other))

    def test_alike_findings_are_counted_so_one_more_of_them_is_new(self):
        base = {("example/one", 402): [self.FIRST_RUN[1]]}
        branch = {("example/one", 402): [self.SECOND_RUN[1], self.FIRST_RUN[1]]}
        new, fixed = look_report.compare(base, branch)
        self.assertEqual(sum(map(len, new.values())), 1)
        self.assertEqual(fixed, {})
        # Matched within its page and width only.
        new, fixed = look_report.compare(base, {("example/one", 820): [self.FIRST_RUN[1]]})
        self.assertEqual(list(new), [("example/one", 820)])

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
        lines = look_report.beside("example-ref", ours, theirs).splitlines()
        self.assertEqual(lines[:3], ["example/one at 402:", "  inlineSize 372", "example-ref: inlineSize 360"])
        # A line the two share is said once; one only the base has is still said.
        self.assertEqual(lines.count("  blockSize 90"), 1)
        self.assertNotIn("example-ref: blockSize 90", lines)
        self.assertEqual(lines[-1], "example-ref: extra 1")
        self.assertIn("example-ref: (no such line)", look_report.beside("example-ref", "a\nb", "a"))
        self.assertIn("example-ref: (not measured)", look_report.beside("example-ref", "a", None))


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

    def test_a_report_names_the_commit_it_was_looked_at(self):
        with tempfile.TemporaryDirectory() as folder, override_settings(BASE_DIR=folder):
            checkout = _repository(Path(folder))
            head = _git("rev-parse", "--short", "HEAD", cwd=checkout).strip()
            self.assertEqual(browser_look._commit("HEAD"), head)
            (checkout / "page.txt").write_text("changed\n", encoding="utf-8")
            self.assertEqual(browser_look._commit("HEAD"), f"{head} with uncommitted changes")
            self.assertIn("unknown", browser_look._commit("example-missing"))

    def test_a_ref_that_names_nothing_is_said_and_leaves_nothing(self):
        with tempfile.TemporaryDirectory() as folder:
            checkout = _repository(Path(folder))
            with (
                self.assertRaisesMessage(AssertionError, "--base example-missing"),
                browser_look._worktree(checkout, "example-missing"),
            ):
                self.fail("a worktree of nothing")
            self.assertEqual(_git("worktree", "list", "--porcelain", cwd=checkout).count("worktree "), 1)
