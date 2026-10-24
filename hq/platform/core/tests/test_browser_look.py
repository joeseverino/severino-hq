"""dev:look reads what it was asked only from its task, reads --css as a file
from the checkout, reports page by page, shoots a dialog open as a reader
opens it, and compares against a base in a worktree it always removes."""

import io
import os
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase, TestCase, override_settings, tag

from hq.platform.core import seeds

from . import look, look_report, look_tables
from .browser import stress

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
            look._asked()
        with mock.patch.dict(os.environ, ASKED, clear=True):
            self.assertEqual(look._asked().text("rule"), "")
            self.assertEqual(look._asked().repeated("pages"), ("example",))

    def test_css_is_text_or_a_file_a_relative_path_names_in_the_checkout(self):
        with tempfile.TemporaryDirectory() as checkout, override_settings(BASE_DIR=checkout):
            Path(checkout, "var").mkdir()
            Path(checkout, "var", "defect.css").write_text("main { inline-size: 200vw }", encoding="utf-8")
            self.assertEqual(look._css("var/defect.css"), "main { inline-size: 200vw }")
            self.assertEqual(look._css("main { color: red }"), "main { color: red }")
            self.assertEqual(look._css(""), "")
            with self.assertRaisesMessage(AssertionError, "no file"):
                look._css("var/missing.css")

    def test_eval_is_a_file_a_relative_path_names_in_the_checkout(self):
        with tempfile.TemporaryDirectory() as checkout, override_settings(BASE_DIR=checkout):
            Path(checkout, "var").mkdir()
            Path(checkout, "var", "probe.js").write_text("() => document.title", encoding="utf-8")
            self.assertEqual(look._script("var/probe.js"), "() => document.title")
            self.assertEqual(look._script(""), "")
            with self.assertRaisesMessage(AssertionError, "names no file"):
                look._script("var/missing.js")

    def test_what_eval_returned_reads_under_its_page_and_width(self):
        lines = look._evaluated("example/one", 402, {"inlineSize": "372px", "chain": ["td", "tr"]}).splitlines()
        self.assertEqual(lines[0], "example/one at 402:")
        self.assertTrue(all(line.startswith("  ") for line in lines[1:]))
        self.assertIn('"372px"', "\n".join(lines))
        self.assertEqual(look._evaluated("example/one", 402, "a\nb"), "example/one at 402:\n  a\n  b")
        self.assertEqual(look._evaluated("example/one", 402, None), "example/one at 402:\n  null")


@tag("source")
class AskedFirstTests(SimpleTestCase):
    def refused_before_the_base(self, asked, said):
        """``asked`` refused with ``said`` before any look at a base began."""

        for kept in ("clock_at", "asked"):
            self.addCleanup(lambda kept=kept: kept in vars(look.Look) and delattr(look.Look, kept))
        environment = {**ASKED, "usage_base": "HEAD", "usage_seed": "showcase", **asked}
        with (
            mock.patch.dict(os.environ, environment, clear=True),
            mock.patch.object(look, "_look_at") as base,
            self.assertRaisesMessage(AssertionError, said),
        ):
            look.Look.setUpClass()
        base.assert_not_called()

    def test_a_rule_that_does_not_exist_is_refused_with_the_rules_that_do_before_the_base_is_looked_at(self):
        self.refused_before_the_base(
            {"usage_pages": "dense/action-items", "usage_rule": "example_no_such_rule"},
            "No rule named 'example_no_such_rule'. Rules: ",
        )
        rules = look._rules()
        self.assertIn("survey", rules)
        self.assertIn("a_table_fits_its_box_at_every_width", rules)
        self.assertNotIn("look", rules)

    def test_a_page_that_does_not_exist_is_refused_before_the_base_is_looked_at(self):
        self.refused_before_the_base({"usage_pages": "example/no-such-page"}, "No page example/no-such-page")

    def test_pages_are_read_as_mise_hands_a_variadic_argument_each_by_its_name(self):
        self.addCleanup(lambda: "asked" in vars(look.Look) and delattr(look.Look, "asked"))
        environment = {"usage_pages": "machine control_plane:containers 'estate/contacts'", "usage_seed": "showcase"}
        with (
            mock.patch.dict(os.environ, environment, clear=True),
            mock.patch.object(look.LayoutBrowserTests, "setUpClass"),
        ):
            look.Look.setUpClass()
        self.assertEqual(look.Look.asked, ["machine", "estate/infrastructure/containers", "estate/contacts"])

    def test_a_seed_that_does_not_exist_is_refused_with_the_seeds_that_do_before_the_base_is_looked_at(self):
        self.refused_before_the_base(
            {"usage_pages": "estate/projects", "usage_seed": "example-no-such-seed"}, "showcase, standard"
        )


class StressPagesTests(TestCase):
    def test_a_stress_page_asked_builds_only_the_estates_it_needs_and_renders_only_itself(self):
        built = []

        @contextmanager
        def spied(name):
            built.append(name)
            with real(name) as owner:
                yield owner

        real = seeds.built
        with mock.patch.object(seeds, "built", spied):
            pages = stress.stress_pages(["machine", "example/not-a-stress-page"])
        # The dense estate answers what the sparse page asks on opening.
        self.assertEqual(built, ["sparse", "dense"])
        self.assertEqual(set(pages) & {*stress.ALL_PAGES, "example/not-a-stress-page"}, {"machine"})


class _Popovers:
    """A page's popovers as the look's scripts reach them: some shown, one
    that will not show."""

    def __init__(self, ids, refused=(), kinds=None):
        self.ids, self.refused, self.shown = ids, set(refused), set()
        self.kinds = kinds or ids

    def evaluate(self, script, place=None):
        if script == look._POPOVER_KINDS:
            return list(self.kinds)
        popover = self.ids[place]
        if script == look._SHOW_POPOVER:
            if popover in self.refused:
                return None
            self.shown.add(popover)
            return [f"#{popover}", popover]
        self.shown.discard(popover)
        return None


class _Lens:
    """The look's walk over popovers, with a rule that finds a box on the page
    beneath, one inside a popover, and two popovers lying over each other."""

    with_each_popover = look.Look.with_each_popover

    def __init__(self, page):
        self.page, self.shots, self.shot_while_shown = page, [], []

    def settle(self):
        pass

    def shoot_popover(self, name, width, popover):
        self.shot_while_shown.append(sorted(self.page.shown))
        return f"var/look/{name}-{width}.popover-{popover}.png"

    def judged(self, _name, _width, _rule):
        shown = sorted(self.page.shown)
        found = ["frames: a box beneath"]
        found += [f"overlaps: {one} over {other}" for one in shown for other in shown if one < other]
        return found + [f"cut: a key in {popover}" for popover in shown]


@tag("source")
class PopoverTests(SimpleTestCase):
    def test_each_popover_is_judged_alone_and_only_for_what_it_adds_to_the_page(self):
        page = _Popovers(["origin-one", "origin-two", "origin-three"], refused=["origin-three"])
        lens = _Lens(page)
        found = lens.with_each_popover("example/one", 820, "survey", ["frames: a box beneath"])
        self.assertEqual(
            found,
            [
                "with #origin-one shown (var/look/example/one-820.popover-origin-one.png): cut: a key in origin-one",
                "with #origin-two shown (var/look/example/one-820.popover-origin-two.png): cut: a key in origin-two",
            ],
        )
        self.assertEqual(page.shown, set())

    def test_each_popover_judged_is_shot_shown_on_its_own_and_the_shot_is_named_beside_it(self):
        page = _Popovers(["origin-one", "origin-two"])
        lens = _Lens(page)
        lens.with_each_popover("example/one", 820, "survey", [])
        self.assertEqual(lens.shot_while_shown, [["origin-one"], ["origin-two"]])
        self.assertEqual(
            lens.shots,
            [
                ("var/look/example/one-820.popover-origin-one.png", "with #origin-one shown"),
                ("var/look/example/one-820.popover-origin-two.png", "with #origin-two shown"),
            ],
        )
        self.assertEqual(
            look._shot("example/one", 820, "popover-origin-one").name, "example-one-820.popover-origin-one.png"
        )

    def test_one_popover_stands_for_its_kind_and_the_next_stands_in_for_one_that_will_not_show(self):
        page = _Popovers(
            ["origin-one", "origin-two", "origin-three", "origin-four"],
            refused=["origin-one"],
            kinds=["origin-panel 0 2", "origin-panel 0 2", "origin-panel 0 2", "origin-panel 1 0"],
        )
        found = _Lens(page).with_each_popover("example/one", 820, "survey", ["frames: a box beneath"])
        self.assertEqual(
            found,
            [
                "with #origin-two and 2 like it shown (var/look/example/one-820.popover-origin-two.png):"
                " cut: a key in origin-two",
                "with #origin-four shown (var/look/example/one-820.popover-origin-four.png): cut: a key in origin-four",
            ],
        )


class _Folds:
    """A page with a fold of rows, as tables.js answers presses: pressing the
    fold shows its rows, and marks each as cut, which shows its toggle, only
    once the timer the press set has run."""

    def __init__(self, rows):
        self.rows, self.folded, self.timer, self.marked, self.opened = rows, False, False, False, set()

    def evaluate(self, script, toggles=None):
        if script == look._AFTER_THE_PAGES_TIMERS:
            self.marked = self.marked or self.timer
        elif script == look._PRESS_SHOWN and toggles == look._FOLD_TOGGLES:
            self.folded = self.timer = True
        elif script == look._PRESS_SHOWN and toggles == look._ROW_TOGGLES and self.marked:
            self.opened |= set(self.rows)


class _Expander:
    expand_rows = look.Look.expand_rows

    def __init__(self, page):
        self.page = page

    def settle(self):
        pass


@tag("source")
class ExpandTests(SimpleTestCase):
    def test_the_rows_a_fold_shows_are_opened_once_the_page_has_marked_them(self):
        page = _Folds(["example-row-one", "example-row-two"])
        _Expander(page).expand_rows()
        self.assertTrue(page.folded)
        self.assertEqual(page.opened, {"example-row-one", "example-row-two"})


class _Dialogs:
    """A page's dialogs as the look's scripts and presses reach them: the
    connection dialog's control sits in a closed menu, and its region loads
    only once the dialog is open and the page has had a turn. Pressing a
    control scrolls it into view and leaves the pointer on it. What each
    screenshot showed is kept, as the page stood when it was taken."""

    def __init__(self, ids, in_menu=()):
        self.open = dict.fromkeys(ids, False)
        self.loaded = dict.fromkeys(ids, False)
        self.in_menu, self.menu_open, self.shots = set(in_menu), False, []
        self.keyboard = self.mouse = self
        self.at, self.hovered = (0, 0), None

    def named(self, openers):
        return next(dialog for dialog in self.open if look._OPENERS.format(id=dialog) == openers)

    def evaluate(self, script, arg=None):
        if script == look._DIALOGS:
            return list(self.open)
        if script == look._REACH:
            if self.named(arg) in self.in_menu:
                self.menu_open = True
            return 0
        if script == look._DIALOG_CLOSED:
            return not self.open[arg]
        if script == look._DIALOG_EXTENT:
            return [600, 600]
        if script == look._WINDOW_AT:
            return list(self.at)
        if script == look._WINDOW_TO:
            self.at = tuple(arg)
            return None
        if script == look._LEAVE:
            self.menu_open = False
            return None
        raise AssertionError(f"a script this page does not know: {script[:40]}")

    def locator(self, openers):
        dialog = self.named(openers)
        page = self

        class Opener:
            def nth(self, _place):
                return self

            def click(self):
                if dialog in page.in_menu and not page.menu_open:
                    raise AssertionError(f"pressed the control for {dialog} inside a closed menu")
                page.open[dialog] = True
                page.at, page.hovered = (0, 1800), dialog

        return Opener()

    def wait_for_function(self, script, arg):
        # The page's turn: an open dialog's region answers.
        self.loaded |= {dialog: True for dialog, shown in self.open.items() if shown}
        dialog = arg[0] if script == look._DIALOG_READY else arg
        held = self.open[dialog] and self.loaded[dialog] if script == look._DIALOG_READY else not self.open[dialog]
        if not held:
            raise AssertionError(f"waited for {dialog} forever")

    def move(self, _x, _y):
        self.hovered = None

    def press(self, key):
        if key == "Escape":
            self.open = dict.fromkeys(self.open, False)
            self.loaded = dict.fromkeys(self.loaded, False)

    def screenshot(self, path, animations):
        shown = [dialog for dialog, state in self.open.items() if state]
        self.shots.append((path.name, shown, [self.loaded[dialog] for dialog in shown], self.menu_open))


class _Opener:
    """The look's walk over dialogs, with a rule that finds a box on the page
    beneath and one inside each dialog open."""

    with_each_dialog = look.Look.with_each_dialog
    open_dialog = look.Look.open_dialog
    shoot_dialog = look.Look.shoot_dialog
    close_dialog = look.Look.close_dialog
    script = ""

    def __init__(self, page, dialogs):
        self.page, self.dialogs, self.shots = page, dialogs, []

    def settle(self):
        pass

    def judged(self, _name, _width, _rule):
        found = ["frames: a box beneath"] + [f"cut: a key in {d}" for d, shown in self.page.open.items() if shown]
        # The page beneath, judged otherwise than it was on its own.
        moved = self.page.at != (0, 0) or self.page.hovered
        return found + ["overlaps: the page beneath scrolled or hovered"] * bool(moved)


@tag("source")
class DialogTests(SimpleTestCase):
    def look(self, page, dialogs, rule="survey"):
        with (
            tempfile.TemporaryDirectory() as out,
            mock.patch.object(look, "OUT", Path(out)),
            mock.patch.dict(os.environ, ASKED),
        ):
            return _Opener(page, dialogs).with_each_dialog("estate", 402, rule, ["frames: a box beneath"])

    def test_a_dialog_named_is_shot_open_with_its_parts_loaded_by_pressing_the_control_in_its_menu(self):
        page = _Dialogs(["connection", "whatif"], in_menu=["connection"])
        found = self.look(page, ["connection"])
        self.assertEqual(page.shots, [("estate-402.dialog-connection.png", ["connection"], [True], True)])
        self.assertEqual(found, ["with #modal-connection open: cut: a key in connection"])
        # Left as it was found, so the next dialog opens from the page.
        self.assertEqual((page.open, page.menu_open), ({"connection": False, "whatif": False}, False))

    def test_all_shoots_every_dialog_the_page_declares_each_on_its_own(self):
        page = _Dialogs(["connection", "whatif"], in_menu=["connection"])
        self.look(page, ["all"], rule="")
        self.assertEqual(
            page.shots,
            [
                ("estate-402.dialog-connection.png", ["connection"], [True], True),
                ("estate-402.dialog-whatif.png", ["whatif"], [True], False),
            ],
        )

    def test_a_dialog_the_page_does_not_declare_is_said_and_not_shot(self):
        page = _Dialogs(["connection"])
        with mock.patch("builtins.print") as said:
            self.assertEqual(self.look(page, ["example"]), [])
        self.assertEqual(page.shots, [])
        self.assertIn("connection", said.call_args.args[0])


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

    def test_against_a_base_only_a_new_error_fails_and_without_one_any_does(self):
        base = {("example/one", 402): ["/one/: console error: Failed to load 12"]}
        same = {("example/one", 402): ["/one/: console error: Failed to load 13"]}
        _, printed, failed = look_report.closing(look._ERRORS, same, base, "example-ref")
        self.assertFalse(failed)
        self.assertNotIn("new", " ".join(printed[:-1]))
        new = {("example/one", 402): [*same["example/one", 402], "/one/: the policy's script-src refused inline"]}
        _, printed, failed = look_report.closing(look._ERRORS, new, base, "example-ref")
        self.assertTrue(failed)
        self.assertEqual(printed[:-1], ["new: example/one at 402: /one/: the policy's script-src refused inline"])
        _, printed, failed = look_report.closing(look._ERRORS, same, None, "")
        self.assertTrue(failed)
        self.assertIn("example/one", printed[-1])
        self.assertFalse(look_report.closing(look._ERRORS, {("example/one", 402): []}, None, "")[2])

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
        line = look_tables.columns_line("example/services", 1086, table)
        self.assertNotIn("\n", line)
        head, columns = line.split(": ", 1)
        for fact in ("example/services", "table 2", "Example services", "1086", "1100", "610", "1720"):
            self.assertIn(fact, head)
        self.assertEqual(
            columns.split(" · "), ["Name 312 (min 180, max 640)", "Status 96 (min 88, max 96)", "Owner gone"]
        )
        unheld = look_tables.columns_line("example/services", 402, {**table, "region": None, "label": ""})
        self.assertNotIn(" in ", unheld.split(": ", 1)[0])
        self.assertNotIn("held", line)

    def test_a_table_an_open_row_holds_says_it_is_drawn_at_the_held_widths(self):
        table = {
            "index": 1, "label": "", "region": None, "drawn": 402, "held": True, "min": 402, "max": 402,
            "columns": [{"name": "Name", "shown": True, "drawn": 402, "min": 402, "max": 402}],
        }  # fmt: skip
        head = look_tables.columns_line("example/services", 402, table).split(": ", 1)[0]
        self.assertIn("held by an open row", head)

    def test_findings_and_measures_survive_the_trip_from_the_base_run(self):
        measured = {("example/one", 402): {"columns": "example/one table 1 at 402", "eval": "example/one at 402:\n  1"}}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, "found.json")
            errors = {("example/one", 402): ["/one/: console error: example"]}
            look._write_found(path, self.FOUND, measured, errors)
            found, read, heard = look._read_found(path)
        self.assertEqual(found, self.FOUND)
        self.assertEqual(heard[("example/one", 402)], errors[("example/one", 402)])
        self.assertEqual(heard[("example/two", 402)], [])
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
        bands = look._bands(2000, 900)
        self.assertEqual(bands, [(0, 900), (900, 900), (1800, 200)])
        self.assertEqual(sum(height for _, height in bands), 2000)

    def test_a_page_the_window_holds_is_not_cut(self):
        self.assertEqual(look._bands(900, 900), [])
        self.assertEqual(look._bands(400, 900), [])


class _Pages:
    """The look's walk over pages, with each look keeping what had been said
    by the time it began."""

    look_at_every_page = look.Look.look_at_every_page

    def __init__(self, asked, said, heard=None):
        self.asked, self.said, self.began, self.errors = asked, said, [], {}
        self.heard, self.refusals = heard or {}, []

    def look(self, name, width, _rule):
        self.began.append(((name, width), self.said.getvalue()))
        self.shots.append((f"var/look/{name}-{width}.png", ""))
        self.refusals += self.heard.get((name, width), [])
        return []

    def refusals_so_far(self):
        refusals, self.refusals = self.refusals, []
        return refusals


@tag("source")
class ProgressTests(SimpleTestCase):
    def test_each_page_and_width_is_said_before_it_is_looked_at(self):
        with (
            tempfile.TemporaryDirectory() as folder,
            mock.patch.object(look, "OUT", Path(folder)),
            mock.patch("sys.stdout", new_callable=io.StringIO) as said,
        ):
            pages = _Pages(["example/one", "example/two"], said)
            pages.look_at_every_page([402, 1360], "survey")
        for (name, width), before in pages.began:
            self.assertEqual(before.splitlines()[-1], f"Looking at {name} at {width}")
        self.assertEqual(len(pages.began), 4)

    def test_each_page_and_width_says_its_shots_and_every_error_it_had_and_keeps_them_apart(self):
        heard = {("example/one", 1360): ["/one/ at 1360px: uncaught TypeError: example"]}
        with (
            tempfile.TemporaryDirectory() as folder,
            mock.patch.object(look, "OUT", Path(folder)),
            mock.patch("sys.stdout", new_callable=io.StringIO) as said,
        ):
            pages = _Pages(["example/one"], said, heard)
            pages.look_at_every_page([402, 1360], "survey")
        lines = said.getvalue().splitlines()
        at_wide = lines.index("Looking at example/one at 1360")
        self.assertIn("  shot var/look/example/one-402.png", lines[:at_wide])
        self.assertFalse([line for line in lines[:at_wide] if line.startswith("  error:")])
        self.assertIn("  error: /one/ at 1360px: uncaught TypeError: example", lines[at_wide:])
        self.assertEqual(pages.errors, {("example/one", 402): [], ("example/one", 1360): heard["example/one", 1360]})

    def test_the_base_pass_is_said_line_by_line_after_its_ref_and_kept_whole_in_its_log(self):
        sink = io.StringIO()
        with mock.patch("sys.stdout", new_callable=io.StringIO) as said:
            look._relay([sys.executable, "-c", "print('Looking at example at 402'); print('done')"], sink, "base")
        self.assertEqual(said.getvalue(), "base: Looking at example at 402\nbase: done\n")
        self.assertEqual(sink.getvalue(), "Looking at example at 402\ndone\n")


@tag("source", "tree")
class WorktreeTests(SimpleTestCase):
    def test_a_report_names_the_commit_it_was_looked_at(self):
        with tempfile.TemporaryDirectory() as folder, override_settings(BASE_DIR=folder):
            checkout = _repository(Path(folder))
            head = _git("rev-parse", "--short", "HEAD", cwd=checkout).strip()
            self.assertEqual(look._commit("HEAD"), head)
            (checkout / "page.txt").write_text("changed\n", encoding="utf-8")
            self.assertEqual(look._commit("HEAD"), f"{head} with uncommitted changes")
            self.assertIn("unknown", look._commit("example-missing"))

    def test_the_base_is_looked_at_under_this_trees_test_files(self):
        with tempfile.TemporaryDirectory() as folder, override_settings(BASE_DIR=folder):
            checkout = _repository(Path(folder))
            Path(checkout, "hq", "tests").mkdir(parents=True)
            Path(checkout, "hq", "tests", "test_new.py").write_text("new\n", encoding="utf-8")
            seen = {}

            def relay(_command, _sink, _ref, cwd, env):
                seen["test"] = Path(cwd, "hq", "tests", "test_new.py").read_text(encoding="utf-8")
                seen["database"] = env.get("SEVERINO_TEST_DATABASE_PATH")
                Path(env["usage_found_into"]).write_text("[]", encoding="utf-8")

            with (
                mock.patch.dict(
                    os.environ, {**ASKED, "SEVERINO_TEST_DATABASE_PATH": "var/example.sqlite3"}, clear=True
                ),
                mock.patch.object(look, "LENS", ()),
                mock.patch.object(look, "_relay", relay),
                mock.patch("sys.stdout", new_callable=io.StringIO),
            ):
                self.assertEqual(look._look_at("HEAD", "survey", 0.0), ({}, {}, {}))
            self.assertEqual(seen["test"], "new\n")
            self.assertIsNone(seen["database"], "the base migrates a database of its own")

    def test_a_base_that_names_nothing_is_refused(self):
        with tempfile.TemporaryDirectory() as folder, override_settings(BASE_DIR=folder):
            _repository(Path(folder))
            with (
                mock.patch.dict(os.environ, ASKED, clear=True),
                self.assertRaisesMessage(AssertionError, "--base example-missing"),
            ):
                look._look_at("example-missing", "survey", 0.0)


class _Unanswered:
    """A look whose estate page answered 404, closing with no rule and no base."""

    close = look.Look.close

    def __init__(self):
        self.base, self.base_errors, self.errors = None, {}, {("estate/example", 402): []}
        self.unanswered = {"estate/example": 404}


@tag("source")
class UnansweredTests(SimpleTestCase):
    def test_a_page_that_does_not_answer_200_fails_the_look_naming_the_page_and_its_status(self):
        with mock.patch.dict(os.environ, ASKED, clear=True), mock.patch("sys.stdout", new_callable=io.StringIO) as said:
            failed = _Unanswered().close("", {})
        self.assertEqual(failed, ["estate/example answered 404, not 200"])
        self.assertIn("estate/example answered 404, not 200", said.getvalue().splitlines())
