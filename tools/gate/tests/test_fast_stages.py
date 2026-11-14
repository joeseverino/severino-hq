"""fast runs in two stages: the quick gates first, and only once they pass the
tests, beside the gates that load the machine, in one share of the heavy turn
sized by what they run."""

import os
import re
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import tag

from hq.platform.core.tests.task import trusted_mise
from tests.mise_tasks import HEAVY_LOCK, ROOT, body, locks, mise_tasks, takes_the_turn
from tools.gate import fast as fast_gate, selection as fast_select, turn as gate_turn, verdict as gate_verdict
from tools.gate.tests.base import GateTestCase
from tools.gate.tests.fast_runs import Recorder, run_main
from tools.gate.turn import GATE, LOCK, in_share

# What a command that runs what follows it in a share starts with.
SHARE = in_share(1, "1m")[:4]


def in_a_share(cmd: list[str]) -> bool:
    """Whether ``cmd`` runs what follows it in a share of the heavy turn."""

    return cmd[: len(SHARE)] == SHARE


@tag("source", "tree")
class FastGateStageTests(GateTestCase):
    def run_fast(self, selected: tuple[str, ...], quick_ok: bool = True) -> list[list[str]]:
        """The commands a run on a change to one module runs, the quick gates' run passing when ``quick_ok``."""

        recorder = Recorder()

        def step(label: str, cmd: list[str]) -> gate_verdict.Step:
            recorder(label, cmd)
            ok = quick_ok or fast_gate.TESTS in cmd or in_a_share(cmd)
            return gate_verdict.Step(label, ok=ok, tasks=[] if ok else [cmd[-1]])

        run_main(
            ("hq/example.py",),
            selected,
            step,
            affected=(set(), set()),
            test_labels=(["hq.example.tests.test_x"], 2),
            unreached_recordings=[],
            touches_shared_ui=False,
        )
        return [cmd for _, cmd in recorder.calls if cmd[:2] != ["git", "diff"]]

    def test_a_quick_gate_that_fails_starts_no_test(self):
        """A lint finding is named in the seconds the quick gates take, never after the suite."""

        quick = tuple(
            task for task in fast_gate.quick_gates() if task not in fast_gate.heavy_gates(fast_gate.quick_gates())
        )
        # Nor does it wait on the selection of the tests, which reads the whole tree.
        with patch.object(fast_gate, "selection", side_effect=AssertionError("the tests were selected")):
            (ran,) = self.run_fast(quick, quick_ok=False)
        self.assertLessEqual(set(quick), set(ran))
        self.assertNotIn(fast_gate.TESTS, ran)

    def test_the_tests_start_once_the_quick_gates_pass_and_take_one_share_with_every_heavy_gate(self):
        every = fast_gate.quick_gates()
        heavy = set(fast_gate.heavy_gates(every))
        self.assertLessEqual({task for task in every if HEAVY_LOCK in locks(task)}, heavy)
        first, then = self.run_fast(every)
        self.assertEqual(set(first) & set(every), set(every) - heavy)
        self.assertNotIn(fast_gate.TESTS, first)
        self.assertFalse(in_a_share(first))
        self.assertTrue(in_a_share(then))
        self.assertEqual(sum(cmd[n : n + len(SHARE)] == SHARE for cmd in (first, then) for n in range(len(cmd))), 1)
        self.assertLessEqual(heavy | {fast_gate.TESTS}, set(then))
        self.assertTrue(takes_the_turn(mise_tasks()[fast_gate.TESTS]))
        self.assertFalse(takes_the_turn(mise_tasks()["fast"]))

    def test_the_share_holds_what_the_tests_weigh_and_each_heavy_gate_and_every_token_beside_a_gate_that_takes_the_lock(
        self,
    ):
        """Beside another checkout's run, fast takes only the tokens it uses: its test workers and one process for
        each gate tagged heavy. A gate that takes the turn's lock needs the whole machine, so beside one it takes
        every token, and so does a stage that asks for more than the machine holds."""

        tagged = tuple(sorted(t for t in fast_gate.heavy_gates(fast_gate.quick_gates()) if HEAVY_LOCK not in locks(t)))
        locking = next(t for t in fast_gate.heavy_gates(fast_gate.quick_gates()) if HEAVY_LOCK in locks(t))
        self.assertGreaterEqual(len(tagged), 2)
        self.enterContext(patch.object(gate_turn, "CAPACITY", 12))
        tokens = fast_gate.stage_tokens
        with patch.object(fast_gate, "weighed", return_value=3) as weighed:
            self.assertEqual(tokens(tagged[:2], [], ["hq.example.tests.test_x"], "auto"), 5)
            self.assertEqual(weighed.call_args.args[0], ["--noinput", "--parallel", "auto", "hq.example.tests.test_x"])
            self.assertEqual(tokens((tagged[0], locking), [], ["hq.example.tests.test_x"], "auto"), 12)
            # The source checks run serially before the tests, in one process.
            self.assertEqual(tokens((), ["hq.example.tests.test_y"], [], "auto"), 1)
            self.assertEqual(tokens((), [], [], "auto"), 0)
        with patch.object(fast_gate, "weighed", return_value=12):
            self.assertEqual(tokens(tagged[:2], [], ["hq.example.tests.test_x"], "auto"), 12)
        # A stage that runs nothing heavy takes no share.
        with patch.object(fast_gate, "weighed", return_value=0):
            command = fast_gate.run_command((), [], [], "auto")
        self.assertFalse(in_a_share(command))

    def test_every_gate_fast_runs_is_quick_or_runs_in_the_heavy_turn(self):
        """A gate fast runs is one of its quick gates, run before any test, or runs beside the tests in the turn:
        so a gate that is not tagged as a quick one, such as a dependency group's test task, never runs beside
        another checkout's heavy run unnoticed."""

        runs = {*fast_gate.quick_gates(), *fast_select.group_tests().values()}
        heavy = set(fast_gate.heavy_gates(tuple(runs)))
        for name in runs:
            with self.subTest(task=name):
                self.assertTrue(fast_gate.QUICK in (mise_tasks()[name]["tags"] or ()) or name in heavy)

    def test_the_share_is_held_as_long_as_the_tests_may_hold_one_alone(self):
        bound = re.search(r"(?m)^turn_bound=(\S+)$", body(mise_tasks()[fast_gate.TESTS]))
        assert bound
        self.assertEqual(fast_gate.TURN_BOUND, bound[1])


# The tasks the share runs, by name.
LEFT, RIGHT = "left", "right"
# Two tasks that each take the turn's lock and finish only together: one
# writes a pipe the other reads, so either alone waits on the other.
SHARE_PROJECT = f"""
[tasks.{LEFT}]
lock = ["{GATE}", "{LOCK}"]
run = 'echo met > "$PIPE"'

[tasks.{RIGHT}]
lock = ["{GATE}", "{LOCK}"]
run = 'read line < "$PIPE"; echo "$line" > "$MET"'
"""


class InTheShareTests(GateTestCase):
    def test_gates_that_take_the_lock_run_side_by_side_inside_a_share(self):
        """fast's tests and the gates beside them run in the share it took: the share tells them they hold the
        turn as mise hands a lock down (MISE_TASK_LOCKS), so a gate that takes the lock itself goes ahead at
        once, beside the others, and never waits on the share its own run holds. Run against mise itself, in a
        project and a state directory of the test's own."""

        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (folder / "mise.toml").write_text(SHARE_PROJECT)
        os.mkfifo(folder / "pipe")
        none = folder / "none.toml"
        none.touch()
        # Nothing of the run this test is in: an inherited lock or share would read as held.
        env = {
            name: value for name, value in os.environ.items() if not name.startswith(("MISE_", "HQ_TURN", "HQ_MEAS"))
        }
        env |= trusted_mise(folder, folder / "state") | {
            "MISE_CEILING_PATHS": str(folder.parent),
            "MISE_GLOBAL_CONFIG_FILE": str(none),
            "MISE_SYSTEM_CONFIG_FILE": str(none),
            "PIPE": str(folder / "pipe"),
            "MET": str(folder / "met"),
        }
        share = [*in_share(2, "5m"), "mise", "-C", str(folder), "run", LEFT, ":::", RIGHT]
        run = subprocess.Popen(share, cwd=ROOT, env=env, stdout=subprocess.DEVNULL)
        self.addCleanup(run.wait)
        self.addCleanup(run.kill)
        # Bounds a deadlock, which would otherwise hold the suite open.
        self.assertEqual(run.wait(timeout=300), 0)
        self.assertEqual((folder / "met").read_text().strip(), "met")
