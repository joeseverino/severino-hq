"""A run of tests takes the machine's heavy turn only when it runs everything,
a whole tree of it, or a browser."""

import re
import shlex

from django.test import tag

from hq.platform.core.test_runner import BROWSER_TESTS
from tests.mise_tasks import body, mise_tasks
from tools.gate.tests.base import GateTestCase
from tools.gate.turn import narrow

MODULE = "hq.platform.application.tests.test_pipeline"


@tag("source")
class NarrowRunTests(GateTestCase):
    def test_a_module_or_something_in_one_is_narrow(self):
        for argv in (
            [MODULE],
            [f"{MODULE}.GateReferenceTests.test_every_task_a_workflow_runs_exists"],
            ["--tag", "source", "--parallel", "auto", MODULE],
        ):
            with self.subTest(argv=argv):
                self.assertTrue(narrow(argv))

    def test_a_test_database_does_not_make_a_run_heavy(self):
        self.assertTrue(narrow(["hq.platform.core.tests.test_audit_retention", "--parallel", "1"]))

    def test_everything_or_a_whole_tree_is_heavy(self):
        for argv in ([], ["--tag", "source"], ["hq.platform.application.tests"], ["hq/platform/application"]):
            with self.subTest(argv=argv):
                self.assertFalse(narrow(argv))

    def test_a_browser_is_heavy_wherever_its_test_is_declared(self):
        # look.py lives outside the browser package and draws through it.
        for label in (f"{BROWSER_TESTS}.estate", "hq.platform.core.tests.look.Look.test_look"):
            with self.subTest(label=label):
                self.assertFalse(narrow([label]))

    def test_a_run_manage_py_refuses_is_heavy(self):
        self.assertFalse(narrow(["--example-no-such-option", MODULE]))


@tag("source")
class HeavyTaskTests(GateTestCase):
    def test_a_task_takes_the_heavy_turn_when_its_run_is_heavy(self):
        """Two suites at once exhaust a small machine's memory; tools/lib/heavy.sh makes them take turns.
        tools/gate/turn.py says which runs of tests are heavy: a task whose tests are fixed takes the turn
        exactly when they are, and one that runs the tests it is given hands them to that decision."""
        runs = {name: task for name, task in mise_tasks().items() if "manage.py test" in body(task)}
        self.assertGreater(len(runs), 3)
        for name, task in runs.items():
            run = body(task)
            (line,) = {
                line.partition("manage.py test")[2]
                for line in run.splitlines()
                if "manage.py test" in line and not line.lstrip().startswith("#")
            }
            words = shlex.split(re.split(r"\|\||&&|;|\\$", line)[0])
            with self.subTest(task=name):
                if any("$" in word for word in words if not word.startswith(("2>", ">"))):
                    # Arguments of the run's own: the decision sees them before the turn is taken.
                    self.assertRegex(run, r'(?m)^test_arguments=".*\$\{usage_\w+:-\}"$')
                    self.assertLess(run.index("test_arguments="), run.index("tools/lib/heavy-turn.sh\n"))
                    continue
                arguments = [word for word in words if not word.startswith(("2>", ">"))]
                takes_it = (task["shell"] or "").startswith("tools/lib/heavy.sh ") or (
                    task["file"] and re.search(r"(?m)^\. .*tools/lib/heavy-turn\.sh\"?$", run)
                )
                if re.search(r"(?m)^\s*light_run=", run):
                    # dev:look's weight is the pages it names, which it weighs itself before the turn.
                    self.assertTrue(takes_it)
                    continue
                self.assertEqual(bool(takes_it), not narrow(arguments), " ".join(arguments))
