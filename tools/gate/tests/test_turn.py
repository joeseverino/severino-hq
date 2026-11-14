"""A run takes a share of the machine's tokens sized by what it runs, every
token at most; only a task that needs the whole machine whatever it is given
takes the turn to itself, and each one says why."""

import contextlib
import fcntl
import fnmatch
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from unittest import mock

from django.test import tag

from hq.platform.core.test_runner import BROWSER_TESTS
from tests.mise_tasks import HEAVY_LOCK, ROOT, SOURCES_THE_TURN, body, locks, mise_tasks, runs, takes_the_turn
from tools.gate import turn
from tools.gate.tests.base import GateTestCase
from tools.gate.turn import BROWSER, CAPACITY, LOCK, ONE, in_share, weigh

MODULE = "hq.platform.application.tests.test_pipeline"
# One test class of it, which a parallel run starts one worker for.
CLASS = "ActionPinTests"


class CapacityTests(GateTestCase):
    def test_the_budget_is_a_token_a_core_within_one_for_each_2_gb(self):
        """A share's token is about a core and 600 MB, so a larger machine runs more shares at once."""
        from tools.gate.room import budget

        gb = 2**30
        self.assertEqual(budget(8, 8 * gb), 4)
        self.assertEqual(budget(12, 30 * gb), 12)
        self.assertEqual(budget(4, 16 * gb), 4)
        self.assertEqual(budget(2, gb), 1)

    def test_a_machine_that_declares_its_tokens_has_that_many(self):
        """A machine whose cores are slow or throttled holds fewer runs than its cores and memory say, so the
        count measured on it, declared once in its clone's git config, is its budget; one that is not a count
        is refused rather than read as some other budget."""
        from tools.gate.room import DECLARED, budget, capacity

        clone = Path(self.enterContext(tempfile.TemporaryDirectory()))
        subprocess.run(["git", "init", "--quiet", str(clone)], check=True)
        fallback = budget(os.cpu_count() or 1, os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE"))
        self.assertEqual(capacity(clone), fallback)
        for said, tokens in (("3", 3), (str(fallback + 5), fallback + 5)):
            subprocess.run(["git", "-C", str(clone), "config", DECLARED, said], check=True)
            self.assertEqual(capacity(clone), tokens)
        for said in ("0", "many", "-2"):
            subprocess.run(["git", "-C", str(clone), "config", DECLARED, said], check=True)
            with self.subTest(said=said), self.assertRaises(ValueError):
                capacity(clone)


class FreeTests(GateTestCase):
    def test_the_free_tokens_are_the_slots_no_share_holds_and_none_while_the_turn_or_gate_is_held(self):
        """What a sender weighs a run against on each machine: a token a share holds is taken, a run holding
        the turn whole or a closed gate leaves none, and a run that holds the gate itself (a landing's) is
        not kept out by it."""
        from tools.gate import room

        state = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(mock.patch.dict(os.environ, {"MISE_STATE_DIR": state}))
        self.assertEqual(room.free(4), 4)
        with taken(room.slot(1), fcntl.LOCK_EX), taken(room.slot(3), fcntl.LOCK_EX):
            self.assertEqual(room.free(4), 2)
        with taken(room.LOCK, fcntl.LOCK_SH):
            self.assertEqual(room.free(4), 4)
        with taken(room.LOCK, fcntl.LOCK_EX):
            self.assertEqual(room.free(4), 0)
        with taken(room.GATE, fcntl.LOCK_EX):
            self.assertEqual(room.free(4), 0)
            self.assertEqual(room.free(4, gate_held=True), 4)


@contextlib.contextmanager
def taken(name: str, mode: int) -> Iterator[None]:
    """``name`` held in ``mode`` by another process, as another run on the machine holds it."""
    from tools.gate.room import lock_file

    path = lock_file(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    holder = subprocess.Popen(
        [sys.executable, "-c", HOLD, str(path), str(mode)], stdin=subprocess.PIPE, stdout=subprocess.PIPE
    )
    assert holder.stdout and holder.stdin
    holder.stdout.readline()
    try:
        yield
    finally:
        holder.stdin.close()
        holder.wait()
        holder.stdout.close()


# Take the lock file named first in the mode named second, say so, and hold it until the input closes.
HOLD = (
    "import fcntl, os, sys; d = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600);"
    " fcntl.flock(d, int(sys.argv[2])); print('held', flush=True); sys.stdin.read()"
)


class ShareTests(GateTestCase):
    def test_a_share_holds_no_more_than_the_machine_and_tells_its_command_what_it_holds(self):
        """A run asking past the budget takes all of it, so a small machine runs no more at once than it holds.
        Its command reads its tokens, the arguments its task was given, and that it holds the gate and the turn
        as mise hands a lock down, so a task under it that takes the lock goes ahead inside the share."""

        state = self.enterContext(tempfile.TemporaryDirectory())
        given = {"usage_at": "main", "usage_arguments": "'a.b.Case' --parallel=1"}
        outside = {
            k: v for k, v in os.environ.items() if not k.startswith(("MISE_TASK", "HQ_TURN", "HQ_MEAS", "usage_"))
        }
        command = [*in_share(CAPACITY + 5, "1m"), sys.executable, "-c", PRINT_HELD]
        ran = subprocess.run(
            command,
            env=outside | given | {"MISE_STATE_DIR": state},
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        held = json.loads(ran.stdout.splitlines()[0])
        self.assertEqual(held.pop("HQ_TURN_SHARE"), str(CAPACITY))
        self.assertLessEqual({"gate", LOCK}, set(held.pop("MISE_TASK_LOCKS").split(",")))
        self.assertEqual(held, given)


class WaitWordsTests(GateTestCase):
    def test_a_wait_names_the_lock_and_its_holder_only_when_one_has_said_who_it_is(self):
        from tools.gate import turn

        with mock.patch.object(turn, "holder", return_value=""):
            self.assertEqual(turn.on(LOCK), f"lock {LOCK}")
        with mock.patch.object(turn, "holder", return_value="held by example"):
            self.assertEqual(turn.on(LOCK), f"lock {LOCK}, held by example")


# A run's view of mise's parse and of what it holds.
PRINT_HELD = (
    "import json, os; print(json.dumps({k: v for k, v in os.environ.items()"
    " if k.startswith('usage_') or k in ('HQ_TURN_SHARE', 'MISE_TASK_LOCKS')}))"
)


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

    def test_a_parallel_run_takes_a_token_for_each_worker_and_every_token_past_the_budget(self):
        """Django starts a worker for each test class up to the workers asked for: a run of two takes two,
        and a run of modules across the cores (fast's selection, `mise run test <module>`) takes the budget."""

        self.assertEqual(weigh(["--parallel", "2", MODULE]), 2)
        with mock.patch("tools.gate.turn.get_max_test_processes", return_value=CAPACITY + 3):
            for argv in (
                ["--parallel", "auto", MODULE],
                ["--tag", "source", "--parallel", "auto", MODULE, "tools.gate.tests.test_turn_mark"],
            ):
                with self.subTest(argv=argv):
                    self.assertEqual(weigh(argv), CAPACITY)

    def test_a_serial_run_of_a_tree_takes_a_share(self):
        self.assertEqual(weigh(["hq.platform.api", "hq.domains.contacts", "--parallel", "1"]), ONE)

    def test_a_test_database_does_not_make_a_run_whole(self):
        self.assertEqual(weigh(["hq.platform.core.tests.test_audit_retention", "--parallel", "1"]), ONE)

    def test_everything_or_a_tree_takes_a_browser_for_each_worker_unloaded(self):
        """Its test classes outnumber its workers, any of which may draw a browser, and loading it to see would
        cost as long as a short run."""

        with mock.patch("django.test.runner.DiscoverRunner.build_suite", side_effect=AssertionError("loaded")):
            for argv in ([], ["--tag", "source"], ["hq.platform.application.tests"], ["hq/platform/application"]):
                for workers in (2, 6):
                    with self.subTest(argv=argv, workers=workers):
                        self.assertEqual(weigh(["--parallel", str(workers), *argv]), min(workers * BROWSER, CAPACITY))

    def test_a_serial_browser_run_takes_a_browsers_share_wherever_its_test_is_declared(self):
        # look.py lives outside the browser package and draws through it.
        for label in (f"{BROWSER_TESTS}.estate", "hq.platform.core.tests.look.Look.test_look"):
            with self.subTest(label=label):
                self.assertEqual(weigh([label, "--parallel", "1"]), BROWSER)

    def test_a_serial_run_of_a_label_that_does_not_import_takes_one_and_a_parallel_one_every_token(self):
        label = f"{MODULE}.GateReferenceTests"
        with mock.patch("django.test.runner.DiscoverRunner.build_suite", side_effect=ImportError("example")):
            self.assertEqual(weigh([label, "--parallel", "1"]), ONE)
            self.assertEqual(weigh([label, "--parallel", "auto"]), CAPACITY)

    def test_a_run_manage_py_refuses_takes_every_token(self):
        self.assertEqual(weigh(["--example-no-such-option", MODULE]), CAPACITY)


@tag("source")
class HeavyTaskTests(GateTestCase):
    def test_a_task_takes_the_heavy_turn_when_its_run_is_heavy(self):
        """Two suites at once exhaust a small machine's memory; mise's task lock makes them take turns.
        tools/gate/turn.py says what a run of tests weighs: a task whose tests are fixed takes the turn exactly
        when they need every token, and one that runs the tests it is given hands them to that decision. A fixed
        run is weighed against the smallest budget it is declared for, an 8 GB Mac's four: a larger machine still
        gives it the whole turn."""
        self.enterContext(mock.patch.object(turn, "CAPACITY", 4))
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
                self.assertEqual(takes_the_turn(task), weigh(arguments) >= turn.CAPACITY, " ".join(arguments))

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

    def test_a_task_that_weighs_its_run_bounds_its_heavy_run_itself(self):
        """A share is stopped at its task's bound: a file task names its bound before it takes its share.
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
        """A task that weighs its run goes ahead inside the turn or a share by finding the lock's
        name in MISE_TASK_LOCKS, and the tests find the tasks that take it by the same name."""

        self.assertEqual(HEAVY_LOCK, LOCK)
        self.assertIn(f"*,{LOCK},*)", (ROOT / "tools" / "lib" / "heavy-turn.sh").read_text())

    def test_every_task_that_takes_the_lock_is_bounded_once_it_holds_it(self):
        """A hung holder would keep every other checkout's heavy gate waiting; its timeout,
        which mise starts once the task holds the lock, stops it."""

        locking = {name: task for name, task in mise_tasks().items() if LOCK in locks(name)}
        self.assertGreater(len(locking), 10)
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
        for name in locking:
            with self.subTest(task=name):
                self.assertIn(GATE, locks(name))

    def test_every_task_that_takes_the_lock_runs_under_the_measurer(self):
        """Each heavy run ends with the line on what its process tree spent
        (tools/lib/measured): a TOML task through its template's shell, a file
        task through the line it sources. A task that weighs its own run reaches
        it through its share."""

        locking = {name: task for name, task in mise_tasks().items() if LOCK in locks(name)}
        self.assertGreater(len(locking), 10)
        for name, task in locking.items():
            with self.subTest(task=name):
                if not task["file"]:
                    self.assertRegex(task["shell"] or "", rf"^{re.escape(MEASURER)} ")
                else:
                    sourced(code(body(task)))


@tag("source")
class WholeMachineTests(GateTestCase):
    def test_only_a_run_declared_to_need_the_whole_machine_takes_it(self):
        """Every other run takes a share sized by what it runs, so a machine with room to spare starts it at once:
        a task takes the turn to itself (its lock in its header or template) or every token of a share
        (`turn_share=all`) only when WHOLE_MACHINE names it with why, and every entry still names such a task."""

        tasks = mise_tasks()
        taking = {name for name in tasks if LOCK in locks(name)}
        taking |= {name for name, task in tasks.items() if task["file"] and TAKES_ALL.search(code(body(task)))}
        self.assertGreater(len(taking), 10)
        declared = {name: [p for p in WHOLE_MACHINE if fnmatch.fnmatchcase(name, p)] for name in tasks}
        self.assertEqual(sorted(name for name in taking if not declared[name]), [], "undeclared whole-machine runs")
        unused = [p for p in WHOLE_MACHINE if not any(fnmatch.fnmatchcase(name, p) for name in taking)]
        self.assertEqual(unused, [], "entries that name no task taking the whole machine")


# Each run that needs the whole machine whatever it is given, by name or
# pattern, and why. Everything else takes a share of what it uses.
WHOLE_MACHINE = {
    "image:*": "Docker's VM, which holds memory of its own",
    "audit:image": "Docker's VM, which holds memory of its own",
    "build": "Docker's VM, which holds memory of its own, across both image builds",
    "browser": "with --linux, Docker's VM and the browsers in it",
    "smoke:runner": "with --image, Docker's VM",
    "browser:*": "a browser for each core",
    "dev:look:docs": "a survey of every docs page across the cores",
    "test:suite": "the whole suite across the cores, under coverage",
    "test:controller": "Go's tests under the race detector, a package for each core",
    "fuzz": "Go's fuzzing, a worker for each core",
    "scan:codeql": "CodeQL's database of the whole tree",
    "lint:shell": "every shell suite at once, among them image builds and the gates' own servers",
    "ci:caches": "every dependency group and Go build CI restores",
}
# How a file task asks for every token of a share.
TAKES_ALL = re.compile(r"(?m)^\s*turn_share=all$")
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
