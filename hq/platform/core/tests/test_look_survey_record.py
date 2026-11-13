"""A dev:look survey under the gate verdict says the pages, widths and flags
it judged and the look that reruns what failed, and fails when it found
anything or judged nothing, so the record mise-tasks/dev/look keeps of it,
var/gate/look-survey-<engine>.txt, says FAILED."""

import io
import os
import tempfile
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase, override_settings, tag

from . import look

ASKED = {"usage_pages": "example/one example/two", "usage_rule": "survey", "usage_engine": "webkit"}


class _Surveyed:
    """A survey closing with no base, on the pages and widths it judged."""

    close = look.Look.close
    shooting = False

    def __init__(self, judged, undrawn=()):
        self.undrawn = undrawn
        self.base, self.base_errors, self.errors = None, {}, {key: [] for key in judged}
        self.unanswered_pages = {}
        self.judged_at = dict.fromkeys(judged)
        self.tree = "example-tree"

    def __getattr__(self, name):
        # Any other method of the look's, bound to this stand-in.
        return getattr(look.Look, name).__get__(self)


@tag("source")
class SurveyRecordTests(SimpleTestCase):
    """A survey says what it judged to the gate verdict that keeps it, so a
    record of one page reads apart from one of all, and the look that reruns
    what failed; it fails when it found anything or judged nothing, so its
    record says FAILED."""

    def close(self, found, judged, undrawn=(), **asked):
        """What the survey failed on, what it said it judged to the gate and
        what it said reruns it, each None for nothing."""

        with tempfile.TemporaryDirectory() as folder, override_settings(BASE_DIR=folder):
            run, detail, again = Path(folder, "run"), Path(folder, "detail.txt"), Path(folder, "rerun.txt")
            run.mkdir()
            gate = {"HQ_GATE_DETAIL": str(detail), "HQ_GATE_RERUN": str(again)}
            with (
                mock.patch.object(look, "this_run", lambda: run),
                mock.patch.object(look, "tree_of", lambda _root: "example-tree"),
                mock.patch.dict(os.environ, {**ASKED, **asked, **gate}, clear=True),
                mock.patch("sys.stdout", new_callable=io.StringIO),
            ):
                failed = _Surveyed(judged, undrawn).close("survey", found)
            said = [path.read_text(encoding="utf-8") if path.is_file() else None for path in (detail, again)]
            return failed, *said

    def test_a_survey_that_found_something_fails_and_says_the_pages_and_widths_it_judged(self):
        found = {
            ("example/one", 402): ["overflow: a box past the edge"],
            ("example/one", 1360): [],
            ("example/two", 402): [],
        }
        failed, said, again = self.close(found, found, usage_widths="402,1360")
        self.assertTrue(failed)
        self.assertIsNotNone(said, "a survey under a gate says what it judged")
        self.assertIn("example/one", said)
        self.assertIn("402", said)
        self.assertIn("1360", said)
        self.assertIn("example/one example/two", said, "what was asked, so a survey of one page reads apart from all")
        self.assertTrue(again.startswith("mise run dev:look "), again)
        rerun = again.split()
        for word in ("--rule", "survey", "--engine", "webkit", "--widths", "402,1360", "example/one"):
            self.assertIn(word, rerun, "the rerun is the same survey of the page that failed")
        self.assertNotIn("example/two", rerun, "a page that held is not looked at again")

    def test_a_survey_that_judged_nothing_fails(self):
        failed, said, again = self.close({}, {})
        self.assertTrue(failed, "a survey of no page never passes")
        self.assertIsNotNone(said)
        self.assertIn("example/two", again.split(), "with no page to blame, the rerun is every page asked")

    def test_a_survey_that_judged_every_page_clean_passes(self):
        found = {("example/one", 402): [], ("example/two", 402): []}
        failed, said, _ = self.close(found, found)
        self.assertEqual(failed, [])
        self.assertIn("example/two", said)

    def test_a_bases_run_says_nothing_to_the_gate_of_the_run_that_asked_for_it(self):
        found = {("example/one", 402): []}
        _, said, again = self.close(found, found, usage_found_into="example-found.json")
        self.assertIsNone(said)
        self.assertIsNone(again)

    def test_a_survey_of_undrawn_templates_alone_passes_and_names_them(self):
        failed, said, _ = self.close({}, {}, ("template:example/_none.html",))
        self.assertEqual(failed, [])
        self.assertIn("undrawn: template:example/_none.html\njudged: none", said)
