"""A run of tests takes the machine's whole heavy turn when it starts more than
one process, and a share of it otherwise; a task that loads the machine
whatever it is given always takes the turn, and no other task does."""

import json
import os
import re
import shlex
import subprocess
import sys
from unittest import mock

from django.test import tag

from hq.platform.core.test_runner import BROWSER_TESTS
from tests.mise_tasks import HEAVY_LOCK, ROOT, SOURCES_THE_TURN, body, locks, mise_tasks, runs, takes_the_turn
from tools.gate.tests.base import GateTestCase
from tools.gate.turn import BROWSER, CAPACITY, IN_TURN, LOCK, ONE, in_turn, weigh

MODULE = "hq.platform.application.tests.test_pipeline"
# One test class of it, which a parallel run starts one worker for.
CLASS = "ActionPinTests"


class InTurnTests(GateTestCase):
    def test_a_run_in_the_turn_reads_the_arguments_its_task_was_given(self):
        """mise runs the turn task with its own parse and none of its caller's usage_*
        variables, so what the turn runs is handed them: `mise run test --at <ref> <label>`
        runs that label at that ref once it holds the turn."""

        given = {"usage_at": "main", "usage_arguments": "'a.b.Case' --parallel=1"}
        # The environment with no parse, as the turn task runs: this test may itself run in the turn.
        turn = {name: value for name, value in os.environ.items() if not name.startswith("usage_")}
        with mock.patch.dict(os.environ, turn | given, clear=True):
            command = [*in_turn("2h"), sys.executable, "-c", PRINT_PARSE]
        self.assertEqual(command[: len(IN_TURN) + 2], ["env", "HQ_TURN_BOUND=2h", *IN_TURN])
        ran = subprocess.run(command[len(IN_TURN) + 2 :], env=turn, check=True, capture_output=True, text=True)
        self.assertEqual(json.loads(ran.stdout), given)


class WaitWordsTests(GateTestCase):
    def test_a_wait_names_the_lock_and_its_holder_only_when_one_has_said_who_it_is(self):
        from tools.gate import turn

        with mock.patch.object(turn, "holder", return_value=""):
            self.assertEqual(turn.on(LOCK), f"lock {LOCK}")
        with mock.patch.object(turn, "holder", return_value="held by example"):
            self.assertEqual(turn.on(LOCK), f"lock {LOCK}, held by example")


# A run's view of mise's parse.
PRINT_PARSE = "import json, os; print(json.dumps({k: v for k, v in os.environ.items() if k.startswith('usage_')}))"


@tag("source")
class WeightTests(GateTestCase):
    def test_a_module_or_something_in_one_takes_a_share_of_one(self):
        """Django starts a worker for each test class up to the workers asked for, so one class is one process."""

        for argv in (
            [MODULE],
            [f"{MODULE}.{CLASS}.test_every_docker_action_is_pinned_by_digest"],
            ["--tag", "source", "--parallel", "auto", f"{MODULE}.{CLASS}"],
        ):
            with self.subTest(argv=argv):
                self.assertEqual(weigh(argv), ONE)

    def test_a_parallel_run_takes_a_token_for_each_worker_and_the_whole_turn_past_the_budget(self):
        """Django starts a worker for each test class up to the workers asked for: a run of two takes two,
        and a run of modules across the cores (fast's selection, `mise run test <module>`) holds as much
        memory as a suite."""

        self.assertEqual(weigh(["--parallel", "2", MODULE]), 2)
        with mock.patch("tools.gate.turn.get_max_test_processes", return_value=CAPACITY):
            for argv in (
                ["--parallel", "auto", MODULE],
                ["--tag", "source", "--parallel", "auto", MODULE, "tools.gate.tests.test_turn_mark"],
            ):
                with self.subTest(argv=argv):
                    self.assertIsNone(weigh(argv))

    def test_a_serial_run_of_a_tree_takes_a_share(self):
        self.assertEqual(weigh(["hq.platform.api", "hq.domains.contacts", "--parallel", "1"]), ONE)

    def test_a_test_database_does_not_make_a_run_whole(self):
        self.assertEqual(weigh(["hq.platform.core.tests.test_audit_retention", "--parallel", "1"]), ONE)

    def test_everything_or_a_whole_tree_takes_the_whole_turn(self):
        for argv in ([], ["--tag", "source"], ["hq.platform.application.tests"], ["hq/platform/application"]):
            with self.subTest(argv=argv):
                self.assertIsNone(weigh(argv))

    def test_a_serial_browser_run_takes_a_browsers_share_wherever_its_test_is_declared(self):
        # look.py lives outside the browser package and draws through it.
        for label in (f"{BROWSER_TESTS}.estate", "hq.platform.core.tests.look.Look.test_look"):
            with self.subTest(label=label):
                self.assertEqual(weigh([label, "--parallel", "1"]), BROWSER)

    def test_a_serial_run_of_a_label_that_does_not_import_takes_a_share_and_a_parallel_one_the_turn(self):
        label = f"{MODULE}.GateReferenceTests"
        with mock.patch("django.test.runner.DiscoverRunner.build_suite", side_effect=ImportError("example")):
            self.assertEqual(weigh([label, "--parallel", "1"]), ONE)
            self.assertIsNone(weigh([label, "--parallel", "auto"]))

    def test_a_run_manage_py_refuses_takes_the_whole_turn(self):
        self.assertIsNone(weigh(["--example-no-such-option", MODULE]))


@tag("source")
class HeavyTaskTests(GateTestCase):
    def test_a_task_takes_the_heavy_turn_when_its_run_is_heavy(self):
        """Two suites at once exhaust a small machine's memory; mise's task lock makes them take turns.
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
                    self.assertLess(run.index("test_arguments="), sourced(run))
                    # It weighs the run the task makes: every option the run passes, in its order.
                    (weighed,) = re.findall(r'(?m)^test_arguments="(.*)"$', run)
                    own = [word for word in words if word != "$@" and not word.startswith(("2>", ">"))]
                    left = iter(shlex.split(weighed))
                    self.assertTrue(all(word in left for word in own), f"weighs {weighed}, runs {' '.join(own)}")
                    continue
                arguments = [word for word in words if not word.startswith(("2>", ">"))]
                if re.search(r"(?m)^\s*turn_share=", run):
                    # dev:look's run is one process whatever pages it names, and declares its share.
                    self.assertTrue(takes_the_turn(task))
                    continue
                self.assertEqual(takes_the_turn(task), weigh(arguments) is None, " ".join(arguments))

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

        # The turn task runs what a task that weighed its own run hands it.
        taking = {name: task for name, task in mise_tasks().items() if takes_the_turn(task) and name != "turn"}
        self.assertGreater(len(taking), 10)
        for name, task in taking.items():
            run = "\n".join(code(text) for text in (body(task), *(path.read_text() for path in runs(task))))
            with self.subTest(task=name):
                self.assertTrue(HEAVY.search(run), f"{name} takes the turn and runs nothing that loads the machine")

    def test_a_task_that_weighs_its_run_bounds_its_heavy_run_itself(self):
        """The turn task's timeout is its caller's: a file task names its bound before it takes the turn.
        One that takes the lock in its header is bounded by its own timeout."""

        weighing = {
            name: body(task)
            for name, task in mise_tasks().items()
            if task["file"] and SOURCES_THE_TURN.search(body(task)) and LOCK not in locks(name)
        }
        self.assertGreater(len(weighing), 3)
        for name, run in weighing.items():
            with self.subTest(task=name):
                bound = re.search(r"(?m)^\s*turn_bound=\d+[smh]$", run)
                self.assertIsNotNone(bound)
                assert bound
                self.assertLess(bound.start(), sourced(run))

    def test_the_turn_is_one_lock_by_one_name(self):
        """A task that weighs its run goes ahead inside the turn by finding the lock's
        name in MISE_TASK_LOCKS, and the tests find the tasks that take it by the same name."""

        self.assertEqual(HEAVY_LOCK, LOCK)
        self.assertIn(f"*,{LOCK},*)", (ROOT / "tools" / "lib" / "heavy-turn.sh").read_text())

    def test_every_task_that_takes_the_lock_is_bounded_once_it_holds_it(self):
        """A hung holder would keep every other checkout's heavy gate waiting; its timeout,
        which mise starts once the task holds the lock, stops it."""

        locking = {name: task for name, task in mise_tasks().items() if LOCK in locks(name)}
        self.assertGreater(len(locking), 10)
        self.assertIn("turn", locking)
        for name, task in locking.items():
            with self.subTest(task=name):
                self.assertRegex(task["timeout"] or "", r"^\d+[smh]$")

    def test_every_task_that_takes_the_lock_waits_at_the_gate_first(self):
        """A landing holds the gate (mise run landing), so a heavy task that took the
        turn without it would start while the landing waits, and could again after
        each of its own runs. The gate sorts first: mise takes a task's locks in
        sorted order, so a task waiting at the gate holds no turn."""

        self.assertLess(GATE, LOCK)
        locking = {name for name in mise_tasks() if LOCK in locks(name)}
        self.assertGreater(len(locking), 10)
        self.assertIn("turn", locking)
        for name in locking:
            with self.subTest(task=name):
                self.assertIn(GATE, locks(name))

    def test_every_task_that_takes_the_lock_runs_under_the_measurer(self):
        """Each heavy run ends with the line on what its process tree spent
        (tools/lib/measured): a TOML task through its template's shell, a file
        task through the line it sources, and the turn task by running its command
        under it. A task that weighs its own run reaches it through the turn."""

        locking = {name: task for name, task in mise_tasks().items() if LOCK in locks(name)}
        self.assertGreater(len(locking), 10)
        for name, task in locking.items():
            with self.subTest(task=name):
                if not task["file"]:
                    self.assertRegex(task["shell"] or "", rf"^{re.escape(MEASURER)} ")
                elif name == "turn":
                    self.assertRegex(body(task), rf"(?m)^exec \./{re.escape(MEASURER)} \"\$@\"$")
                else:
                    sourced(code(body(task)))


# The lock a landing holds (tools/gate/turn.py's GATE, which test_landing holds to this).
GATE = "gate"
# What ends a heavy run with the line on what its process tree spent.
MEASURER = "tools/lib/measured"

# A command that loads the machine whatever its arguments.
LOADS = re.compile(
    r"\bdocker (?:build|buildx build|run)\b|\bcodeql database\b|\bgo (?:-C \S+ )?test\b|tests/shell/test-|\btrivy image\b"
)
# What may hold the turn: a command that loads the machine, the VM a task boots
# for its run, a browser, a run of tests, or a look that weighs its own pages.
HEAVY = re.compile(rf"(?m){LOADS.pattern}|\bimage:docker start\b|--group browser\b|\bmanage\.py test\b|^turn_share=")


def sourced(run: str) -> int:
    """Where a file task's run sources the line that takes the turn."""

    line = SOURCES_THE_TURN.search(run)
    assert line, "the task does not source tools/lib/heavy-turn.sh"
    return line.start()


def code(text: str) -> str:
    """A script's lines less its comments."""

    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
