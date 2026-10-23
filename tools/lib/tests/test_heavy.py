"""A gate waiting for the heavy turn gives up on a holder that uses no CPU, and waits for one that works."""

import os
import subprocess
import tempfile
from pathlib import Path

from django.test import SimpleTestCase, tag

HEAVY = Path(__file__).resolve().parents[1] / "heavy.sh"
# Spins until the file named first exists: a holder that is working.
BUSY = "import os, sys\nwhile not os.path.exists(sys.argv[1]):\n    pass\n"
# Every run here ends on its own long before this; it bounds a regression that hangs.
BOUND = 120


@tag("source")
class IdleHolderTests(SimpleTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.env = {
            key: value
            for key, value in os.environ.items()
            if key not in {"CI", "SEVERINO_HEAVY_HOLDER", "SEVERINO_HEAVY_TASK", "MISE_TASK_NAME"}
        }
        self.env |= {
            "XDG_CACHE_HOME": str(self.folder),
            "SEVERINO_HEAVY_EVERY": "1",
            "SEVERINO_HEAVY_IDLE": "2",
            "SEVERINO_HEAVY_WAIT": "30",
        }

    def heavy(self, *command: str, **options) -> subprocess.Popen[str]:
        process = subprocess.Popen(
            ["/bin/sh", str(HEAVY), *command], env=self.env, stderr=subprocess.PIPE, text=True, **options
        )
        self.addCleanup(self.stop, process)
        return process

    @staticmethod
    def stop(process: subprocess.Popen[str]) -> None:
        """Kill the run if it is still going, and close its pipes."""

        process.kill()
        process.communicate(timeout=BOUND)

    def holding(self, *command: str, **options) -> subprocess.Popen[str]:
        """A heavy run of the command, once it has the turn."""

        holder = self.heavy(*command, **options)
        assert holder.stderr
        for line in holder.stderr:
            if "the turn starts" in line:
                return holder
        self.fail("the holder never took the turn")

    def test_a_holder_that_uses_no_cpu_is_named_with_the_kill_that_stops_it_and_the_waiter_gives_up(self):
        holder = self.holding("cat", stdin=subprocess.PIPE)

        waiter = self.heavy("true")
        _, err = waiter.communicate(timeout=BOUND)

        self.assertEqual(waiter.returncode, 75)
        verdict = err.strip().splitlines()[-1]
        self.assertIn(f"pid {holder.pid}", verdict)
        killed = verdict.rsplit("kill ", 1)[-1].split()
        self.assertIn(str(holder.pid), killed)
        self.assertIsNone(holder.poll(), "the waiter stops nothing")

    def test_a_holder_that_works_is_waited_for(self):
        done = self.folder / "done"
        self.addCleanup(done.touch)
        self.holding("python3", "-c", BUSY, str(done))

        waiter = self.heavy("true")
        assert waiter.stderr
        waits = 0
        # Past the idle bound twice over, then the holder finishes.
        for line in waiter.stderr:
            waits += "still waiting" in line
            if waits == 4:
                done.touch()
                break
        waiter.communicate(timeout=BOUND)

        self.assertEqual(waiter.returncode, 0)
