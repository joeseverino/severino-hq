"""A dev:look survey under the gate verdict says the pages, widths and flags
it judged and the look that reruns what failed, and fails when it found
anything or judged nothing, so the record mise-tasks/dev/look keeps of it,
var/gate/look-survey-<engine>.txt, says FAILED."""

import io
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

from django.conf import settings
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

    def test_a_survey_record_holds_across_a_change_outside_its_inputs_and_not_across_a_template(self):
        found = {("example/one", 402): []}
        _, said, _ = self.close(found, found)
        inputs = next(line for line in said.splitlines() if line.startswith("inputs: ")).split()[1:]
        with tempfile.TemporaryDirectory() as folder:
            env = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "PATH": os.environ.get("PATH", "")}

            def git(*args, check=True):
                return subprocess.run(
                    ["git", "-C", folder, *args], check=check, env=env, capture_output=True, text=True
                )

            def tree(**files):
                for name, text in files.items():
                    Path(folder, name).parent.mkdir(parents=True, exist_ok=True)
                    Path(folder, name).write_text(text, encoding="utf-8")
                git("add", "--all")
                return git("write-tree").stdout.strip()

            git("init", "--quiet")
            judged = tree(
                **{"templates/example/page.html": "a", "tests/shell/test-example.sh": "a", "docs/Example.md": "a"}
            )
            outside = tree(**{"tests/shell/test-example.sh": "b", "docs/Example.md": "b"})
            drawn = tree(**{"templates/example/page.html": "b"})

            kept = git("diff", "--quiet", judged, outside, "--", *inputs, check=False)
            self.assertEqual(kept.returncode, 0, f"a shell test and a doc are no input of a survey: {kept.stderr}")
            moved = git("diff", "--name-only", "-z", judged, drawn, "--", *inputs)
            named = moved.stdout.rstrip("\0").split("\0")
            self.assertEqual(named, ["templates/example/page.html"], "a template is an input, named")

    def test_another_checkouts_survey_names_no_inputs_of_this_one(self):
        found = {("example/one", 402): []}
        _, said, _ = self.close(found, found, HQ_GATE_TREE=tempfile.gettempdir())
        self.assertNotIn("inputs:", said, "an extension's record holds only for the tree it judged")


# Every module of HQ's checkout the look and the pages it draws load: the look
# itself, every view the URLs reach and every template library, in a fresh
# interpreter so no other test's imports count.
_LOADED = """
import sys, django
django.setup()
from django.template import engines
from django.urls import get_resolver
from hq.platform.core.tests import look

def walk(patterns):
    for pattern in patterns:
        walk(getattr(pattern, "url_patterns", ()))

walk(get_resolver().url_patterns)
engines["django"].engine.template_libraries
print("\\n".join(sorted({getattr(m, "__file__", None) or "" for m in list(sys.modules.values())})))
"""


@tag("source", "tree")
class SurveyInputsTests(SimpleTestCase):
    """A survey's inputs leave out no file the look or a page loads, so a
    survey landing keeps across a change never covers a change it read."""

    def test_every_module_the_look_and_its_pages_load_is_an_input(self):
        root = Path(settings.BASE_DIR).resolve()

        def tracked(*pathspecs):
            listed = subprocess.run(
                ["git", "-C", root, "ls-files", "-z", "--", *pathspecs], check=True, capture_output=True
            )
            return set(listed.stdout.decode().split("\0")) - {""}

        env = os.environ | {"DJANGO_SETTINGS_MODULE": settings.SETTINGS_MODULE}
        run = [sys.executable, "-c", _LOADED]
        loaded = subprocess.run(run, check=False, cwd=root, env=env, capture_output=True, text=True)
        self.assertEqual(loaded.returncode, 0, loaded.stderr)
        files = {Path(path).resolve() for path in loaded.stdout.splitlines() if path}
        ours = {str(path.relative_to(root)) for path in files if path.is_relative_to(root)} & tracked()
        self.assertTrue(ours, "the look loads modules of this checkout")
        self.assertEqual(sorted(ours - tracked(*look.SURVEY_INPUTS)), [], "a module a page loads is outside the inputs")
