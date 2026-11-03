"""A run of tests takes the machine's heavy turn only when it runs everything
or a whole tree of it in parallel, or a browser; a task that loads the machine
whatever it is given always takes it, and no other task does."""

import re
import shlex

from django.test import tag

from hq.platform.core.test_runner import BROWSER_TESTS
from tests.mise_tasks import body, mise_tasks, runs, takes_the_turn
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

    def test_a_serial_run_of_a_tree_is_narrow(self):
        self.assertTrue(narrow(["hq.platform.api", "hq.domains.contacts", "--parallel", "1"]))

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
                self.assertFalse(narrow([label, "--parallel", "1"]))

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
                if re.search(r"(?m)^\s*light_run=", run):
                    # dev:look's weight is the pages it names, which it weighs itself before the turn.
                    self.assertTrue(takes_the_turn(task))
                    continue
                self.assertEqual(takes_the_turn(task), not narrow(arguments), " ".join(arguments))

    def test_a_task_that_loads_the_machine_whatever_it_is_given_takes_the_heavy_turn(self):
        """An image build or container, CodeQL's database, Go's tests and every
        shell suite at once each hold memory a second heavy run would need, run
        alone or under a gate. Read in the task's run and the scripts it names."""

        loading = {
            name: task
            for name, task in mise_tasks().items()
            if LOADS.search("\n".join(code(text) for text in (body(task), *(path.read_text() for path in runs(task)))))
        }
        self.assertGreater(len(loading), 10)
        for name, task in loading.items():
            with self.subTest(task=name):
                self.assertTrue(takes_the_turn(task))

    def test_a_task_takes_the_heavy_turn_only_to_run_what_loads_the_machine(self):
        """A turn held by a light step (an aggregate between its gates, one Django
        process on an empty database) makes every other checkout's light step
        queue behind it for nothing: a task that takes the turn runs a command
        that loads the machine, a browser, Docker's VM or a run of tests whose
        weight the first test of this class decides."""

        taking = {name: task for name, task in mise_tasks().items() if takes_the_turn(task)}
        self.assertGreater(len(taking), 10)
        for name, task in taking.items():
            run = "\n".join(code(text) for text in (body(task), *(path.read_text() for path in runs(task))))
            with self.subTest(task=name):
                self.assertTrue(HEAVY.search(run), f"{name} takes the turn and runs nothing that loads the machine")


# A command that loads the machine whatever its arguments.
LOADS = re.compile(
    r"\bdocker (?:build|buildx build|run)\b|\bcodeql database\b|\bgo (?:-C \S+ )?test\b|tests/shell/test-|\btrivy image\b"
)
# What may hold the turn: a command that loads the machine, the VM a task boots
# for its run, a browser, a run of tests, or a look that weighs its own pages.
HEAVY = re.compile(rf"(?m){LOADS.pattern}|\bimage:docker start\b|--group browser\b|\bmanage\.py test\b|^light_run=")


def code(text: str) -> str:
    """A script's lines less its comments."""

    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
