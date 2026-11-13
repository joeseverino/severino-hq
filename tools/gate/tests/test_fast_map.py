"""fast reads a change to a file the map of the tests that ran each file
holds as the test modules that ran it, and every other path as it always did."""

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.test import tag

from hq.platform.core.tests.task import Usage
from tools.gate import chain as fast_chain, fast as fast_gate
from tools.gate.tests.base import GateTestCase, fixture_git
from tools.gate.tests.fast_runs import Recorder, everywhere, run_main

VIEWS = "hq/domains/example/views.py"
OTHER = "hq/domains/other/helpers.py"
# Two apps, each with a test module the map says ran a file of it, and one that ran none.
FILES = {
    "hq/__init__.py": "",
    "hq/domains/__init__.py": "",
    "hq/domains/example/__init__.py": "",
    "hq/domains/example/apps.py": 'class ExampleConfig:\n    name = "hq.domains.example"\n',
    "hq/domains/example/models.py": "class Thing: ...\n",
    VIEWS: '"""The example pages."""\n\n\ndef page():\n    """The one page."""\n    return 1\n',
    "hq/domains/example/tests/__init__.py": "",
    "hq/domains/example/tests/test_views.py": "",
    "hq/domains/example/tests/test_app.py": "",
    "hq/domains/other/__init__.py": "",
    "hq/domains/other/apps.py": 'class OtherConfig:\n    name = "hq.domains.other"\n',
    OTHER: "def helper():\n    return 1\n",
    "hq/domains/other/tests/__init__.py": "",
    "hq/domains/other/tests/test_other.py": "",
    "hq/domains/other/tests/test_other_app.py": "",
}
RAN_VIEWS = "hq.domains.example.tests.test_views"
APP_TEST = "hq.domains.example.tests.test_app"
OTHER_TEST = "hq.domains.other.tests.test_other"
OTHER_APP_TEST = "hq.domains.other.tests.test_other_app"
RAN = (RAN_VIEWS, APP_TEST, OTHER_TEST, OTHER_APP_TEST)


@tag("source", "tree")
class FastGateMapSelectionTests(GateTestCase):
    """Each case runs fast's listing in a repository whose base tree has a map."""

    def listed(self, edits: dict[str, str], ran: tuple[str, ...] = RAN) -> list[str]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            git = fixture_git(root)
            for path, text in FILES.items():
                (root / path).parent.mkdir(parents=True, exist_ok=True)
                (root / path).write_text(text)
            git("init", "--quiet")
            git("add", "-A")
            git("commit", "--quiet", "--no-verify", "-m", "base")
            base = git("rev-parse", "HEAD")
            tests = sorted({*ran, RAN_VIEWS, OTHER_TEST})
            the_map = {
                "ran": [tests.index(module) for module in ran],
                "tests": tests,
                "files": {VIEWS: [tests.index(RAN_VIEWS)], OTHER: [tests.index(OTHER_TEST)]},
            }
            maps = root / ".git" / "severino" / "fast-maps"
            maps.mkdir(parents=True)
            (maps / f"{git('rev-parse', 'HEAD^{tree}')}.json").write_text(json.dumps(the_map))
            for path, text in edits.items():
                (root / path).parent.mkdir(parents=True, exist_ok=True)
                (root / path).write_text(text)
            with (
                patch.object(fast_chain, "ROOT", root),
                everywhere("passed_ancestor", return_value=None),
                everywhere("merge_base", return_value=base),
                patch("sys.stdout", new_callable=io.StringIO) as out,
            ):
                self.assertEqual(fast_gate.main(Usage({"list": "true"})), 0)
        return out.getvalue().splitlines()

    def test_a_change_inside_a_function_the_map_saw_run_selects_the_tests_that_ran_it_and_not_its_app(self):
        listed = self.listed({VIEWS: FILES[VIEWS].replace("return 1", "return 2")})
        self.assertIn(RAN_VIEWS, listed)
        self.assertNotIn(APP_TEST, listed)

    def test_a_change_the_map_cannot_see_selects_as_without_one(self):
        """A docstring is read as the module loads, and a file the map does not hold ran in no test it knows."""
        cases = {
            "a docstring": {VIEWS: FILES[VIEWS].replace("The one page.", "The page.")},
            "a module-level line": {VIEWS: FILES[VIEWS] + "LIMIT = 3\n"},
            "a file the map does not hold": {"hq/domains/example/models.py": "class Thing:\n    size = 2\n"},
        }
        for case, edits in cases.items():
            with self.subTest(case):
                # Beside it, a change inside a function the map saw run selects only the tests that ran it.
                listed = self.listed({**edits, OTHER: FILES[OTHER].replace("1", "2")})
                self.assertIn(APP_TEST, listed)
                self.assertIn(OTHER_TEST, listed)
                self.assertNotIn(OTHER_APP_TEST, listed)

    def test_a_test_module_the_recording_did_not_run_selects_by_every_rule(self):
        new = "hq/domains/example/tests/test_new.py"
        listed = self.listed({VIEWS: FILES[VIEWS].replace("return 1", "return 2"), new: ""})
        self.assertIn("hq.domains.example.tests.test_new", listed)
        self.assertNotIn(APP_TEST, listed)
        unran = self.listed({VIEWS: FILES[VIEWS].replace("return 1", "return 2")}, ran=(RAN_VIEWS,))
        self.assertIn(APP_TEST, unran)


@tag("source", "tree")
class FastGateMapRecordTests(GateTestCase):
    def test_the_whole_run_measures_the_map_and_a_narrow_one_does_not(self):
        suite = [f"hq.example.tests.test_{n}" for n in range(10)]
        for whole in (True, False):
            with self.subTest(whole=whole):
                recorder = Recorder()
                run_main(
                    ("hq/example.py",),
                    step=recorder,
                    asked={"whole": "true"} if whole else {},
                    affected=(set(), set()),
                    test_labels=(suite[:2], len(suite)),
                    whole_suite=suite,
                    record_pass=None,
                    touches_shared_ui=False,
                )
                (run,) = [cmd for _, cmd in recorder.calls if fast_gate.TESTS in cmd]
                self.assertEqual("--map" in run, whole)

    def test_a_kept_map_reads_back_as_it_was_and_only_the_newest_are_kept(self):
        from tools.gate import testmap

        the_map = testmap.TestMap(frozenset({RAN_VIEWS, APP_TEST}), {VIEWS: frozenset({RAN_VIEWS})})
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            for n in range(testmap.KEPT + 2):
                kept = testmap.save(folder, f"{n:040}", the_map)
                os.utime(kept, (n, n))
            self.assertEqual(testmap.load(kept), the_map)
            self.assertEqual(len(list(folder.glob("*.json"))), testmap.KEPT)
            self.assertTrue(kept.is_file())


class FastGateMapContextTests(GateTestCase):
    def test_a_test_modules_class_fixtures_and_tests_run_in_its_context(self):
        from hq.platform.core.run_report import ClassTimedSuite

        measuring = SimpleNamespace(context=None, get_option={"run:core": "ctrace"}.get)
        measuring.switch_context = lambda name: setattr(measuring, "context", name)
        seen = []

        # Defined here, so no run discovers it.
        class Recorded(unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                seen.append(measuring.context)

            def test_runs(self):
                seen.append(measuring.context)

        coverage = SimpleNamespace(Coverage=SimpleNamespace(current=lambda: measuring))
        with patch.dict(sys.modules, {"coverage": coverage}):
            ClassTimedSuite([Recorded("test_runs")]).run(unittest.TestResult())
        self.assertEqual(seen, [__name__, __name__])
