"""A landing goes ahead of every heavy run that has not yet started: the gate
it holds (tools.gate.turn.land) keeps them waiting while its own heavy tasks
take the turn. Run against mise itself, in a project and state directory of
the test's own, so the machine's real turn is never touched."""

import fcntl
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import override
from unittest import mock

from hq.platform.core.tests.task import trusted_mise
from tests.mise_tasks import ROOT
from tools.gate.tests import test_turn
from tools.gate.tests.base import GateTestCase
from tools.gate.turn import GATE, LOCK, lock_file

# Two heavy tasks the way HQ declares one: `hold` keeps the turn until the
# test writes its release, `step` writes who ran it, in the order they ran.
PROJECT = f"""
[tasks.hold]
lock = ["{GATE}", "{LOCK}"]
run = 'echo held > "$MARK"; read line < "$RELEASE"'

[tasks.step]
lock = ["{GATE}", "{LOCK}"]
run = 'echo "$WHO" >> "$ORDER"'
"""

# What mise-tasks/landing runs its command under.
LANDING = (sys.executable, "-m", "tools.gate.turn", "--landing")


class LandingTests(GateTestCase):
    @override
    def setUp(self):
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (self.dir / "mise.toml").write_text(PROJECT)
        for fifo in ("mark", "release"):
            os.mkfifo(self.dir / fifo)
        # Nothing of the run this test is in: an inherited MISE_TASK_LOCKS would read as held.
        outside = {name: value for name, value in os.environ.items() if not name.startswith("MISE_")}
        self.env = outside | {
            **trusted_mise(self.dir, self.dir / "state"),
            "MISE_CEILING_PATHS": str(self.dir.parent),
            "MISE_GLOBAL_CONFIG_FILE": str(self.dir / "none.toml"),
            "MISE_SYSTEM_CONFIG_FILE": str(self.dir / "none.toml"),
            "MARK": str(self.dir / "mark"),
            "RELEASE": str(self.dir / "release"),
            "ORDER": str(self.dir / "order"),
        }

    def start(self, *command: str, **env: str) -> subprocess.Popen[str]:
        run = subprocess.Popen(
            command,
            cwd=self.dir,
            env=self.env | env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert run.stderr
        self.addCleanup(run.stderr.close)
        self.addCleanup(run.wait)
        self.addCleanup(run.kill)
        return run

    def task(self, name: str, *before: str, **env: str) -> subprocess.Popen[str]:
        """A run of the test project's task ``name``, after ``before`` (the landing) when it is given."""

        return self.start(*before, "mise", "run", name, **env)

    def hold_the_turn(self) -> subprocess.Popen[str]:
        """A heavy task in the turn, once it says so."""

        held = self.task("hold")
        self.addCleanup(self.release)
        self.assertEqual((self.dir / "mark").read_text().strip(), "held")
        return held

    def release(self) -> None:
        if not getattr(self, "released", False):
            self.released = True
            with (self.dir / "release").open("w") as release:
                release.write("go\n")

    def waits_for(self, run: subprocess.Popen[str], lock: str) -> None:
        """Read the run's output until mise says it waits for ``lock``."""

        assert run.stderr
        for line in run.stderr:
            if f"waiting for lock {lock}" in line:
                return
        self.fail(f"the run ended without waiting for {lock}")

    def test_the_gate_is_the_lock_every_heavy_task_names(self):
        self.assertEqual(GATE, test_turn.GATE)

    def test_the_lock_file_is_the_one_mise_takes(self):
        """The landing takes the gate by mise's own file; one of another name would hold nothing back."""

        self.hold_the_turn()
        with mock.patch.dict(os.environ, self.env, clear=True):
            files = [lock_file(name) for name in (GATE, LOCK)]
        for path in files:
            with self.subTest(lock=path.name), path.open("rb") as held, self.assertRaises(BlockingIOError):
                fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_a_landing_runs_before_a_heavy_run_that_was_waiting_first(self):
        """A worker already waiting for the turn when the landing starts still runs after it:
        the landing waits behind only the run that holds the turn."""

        held = self.hold_the_turn()
        worker = self.task("step", WHO="worker")
        self.waits_for(worker, GATE)
        landing = self.task("step", *LANDING, WHO="landing", PYTHONPATH=str(ROOT))
        # The landing's own heavy task holds the gate as inherited and waits only for the turn.
        self.waits_for(landing, LOCK)
        self.release()
        self.assertEqual(held.wait(), 0)
        self.assertEqual(landing.wait(), 0)
        self.assertEqual(worker.wait(), 0)
        self.assertEqual((self.dir / "order").read_text().split(), ["landing", "worker"])
