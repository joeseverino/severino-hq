"""dev:look's --open-folds: the survey judged again with every fold open, a
fold within a fold and one a region brings in included, saying only what
that adds, and the folds closed again after."""

import io
import os
from unittest import mock

from django.test import SimpleTestCase, tag

from . import look
from .browser import survey

ASKED = {"usage_pages": "example"}


class _Folded:
    """A page whose folds are closed, one inside another, and a region the
    outer one brings in on opening that holds a fold of its own, as the
    look's scripts reach them."""

    def __init__(self, folds=("example-outer", "example-inner")):
        self.open, self.brought = dict.fromkeys(folds, False), False
        self.keyboard = self

    def press(self, _key):
        pass

    def evaluate(self, script, _arg=None):
        if script == survey.OPEN_ONCE:
            closed = [fold for fold, shown in self.open.items() if not shown]
            self.open |= dict.fromkeys(closed, True)
            return len(closed)
        if script == survey.CLOSE_OPENED:
            self.open = dict.fromkeys(self.open, False)
        return None

    def wait_for_function(self, _script, arg=None):
        pass

    def settle(self):
        # The region the outer fold asked for has answered, with its fold closed.
        if self.open.get("example-outer") and not self.brought:
            self.brought, self.open["example-region"] = True, False


class _FoldLens:
    """The look at one page, its rule finding a box on the page as drawn and
    a defect in each fold that is open."""

    css, script, dialogs, on_reference = "", "", [], False

    def __init__(self, page):
        self.page, self.judging, self.shown = page, [], []

    def __getattr__(self, name):
        return getattr(look.Look, name).__get__(self)

    def refused(self, _name, _width):
        return ""

    def settle(self):
        self.page.settle()

    def take_shot(self, _name, _width, _take, state="", shows="", frame=None):
        self.shown.append(shows)
        return []

    def judged(self, _name, _width, _rule):
        self.judging.append(sorted(fold for fold, shown in self.page.open.items() if shown))
        return ["frames: a box beneath", *(f"cells: cut in {fold}" for fold in self.judging[-1])]


@tag("source")
class FoldsOpenTests(SimpleTestCase):
    def looked(self, page, rule="survey", folds="true"):
        lens = _FoldLens(page)
        asked = {**ASKED, "usage_open_folds": folds}
        with mock.patch.dict(os.environ, asked), mock.patch("sys.stdout", new_callable=io.StringIO):
            return lens, lens.look("example/one", 402, rule)

    def test_the_survey_judges_again_with_every_fold_open_nested_and_brought_in_saying_only_what_that_adds(self):
        page = _Folded()
        lens, said = self.looked(page)
        self.assertEqual(lens.judging[-1], ["example-inner", "example-outer", "example-region"])
        self.assertEqual(
            said,
            [
                "frames: a box beneath",
                "with every fold open: cells: cut in example-inner",
                "with every fold open: cells: cut in example-outer",
                "with every fold open: cells: cut in example-region",
            ],
        )
        self.assertIn("with every fold open", lens.shown)
        # Closed again, so what follows opens from the page as it loaded.
        self.assertFalse(any(page.open.values()))

    def test_a_page_with_no_fold_closed_is_judged_once(self):
        lens, said = self.looked(_Folded(folds=()))
        self.assertEqual(said, ["frames: a box beneath"])
        self.assertEqual(lens.judging, [[]])

    def test_without_open_folds_the_survey_judges_the_page_as_drawn(self):
        page = _Folded()
        lens, said = self.looked(page, folds="false")
        self.assertEqual(said, ["frames: a box beneath"])
        self.assertEqual(lens.judging, [[]])

    def test_a_rule_of_one_check_judges_the_page_as_its_gate_does_with_its_folds_closed(self):
        page = _Folded()
        lens, _said = self.looked(page, rule="a_table_fits_its_box_at_every_width")
        self.assertEqual(lens.judging, [[]])
        self.assertFalse(any(page.open.values()))
