"""fast runs its light quick gates beside the tests, prints each step's output
whole under its label, and tells a light gate's failure while the tests go on."""

import io
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from django.test import tag

from tools.gate.tests.base import GateTestCase
from tools.gate.tests.fast_runs import run_main

ROOT = Path(__file__).resolve().parents[3]


# Each side opens the pipe the other holds: opening a FIFO waits until both
# ends are open, so neither finishes unless the two run at once. The alarm
# bounds a run where they never meet.
MEETS = """
import signal, sys
signal.alarm(20)
side, pipe, exit_code = sys.argv[1:]
print(f"{side} starts", flush=True)
if side == "tests":
    with open(pipe, "w") as end:
        end.write("met")
else:
    with open(pipe) as end:
        end.read()
print(f"{side} ends", flush=True)
sys.exit(int(exit_code))
"""


@tag("source", "tree")
class FastOverlapTests(GateTestCase):
    """fast runs its light quick gates beside the tests."""

    def run_fast(self, quick_exit: int = 0, tests_exit: int = 0) -> tuple[int, list[str]]:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        script = Path(folder.name, "meet.py")
        script.write_text(MEETS)
        os.mkfifo(script.with_name("pipe"))

        def meets(side: str, exit_code: int) -> list[str]:
            return [sys.executable, str(script), side, str(script.with_name("pipe")), str(exit_code)]

        # The patch integrity step runs for real, against a base that resolves.
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True)
        return run_main(
            selected=("example:quick",),
            merge_base=head.stdout.strip(),
            affected=(set(), set()),
            test_labels=([], 0),
            turn_takers=(),
            tasks_command=meets("quick", quick_exit),
            test_command=meets("tests", tests_exit),
        )

    def test_the_light_quick_gates_run_while_the_tests_do_each_output_whole_under_its_label(self):
        # Each side ends only once the other has opened its end.
        _, printed = self.run_fast()
        labels = set()
        for side in ("tests", "quick"):
            with self.subTest(side=side):
                starts = printed.index(f"{side} starts")
                self.assertEqual(printed[starts + 1], f"{side} ends", "\n".join(printed))
                self.assertTrue(printed[starts - 1].startswith("[fast] "), printed[starts - 1])
                labels.add(printed[starts - 1])
        self.assertEqual(len(labels), 2)

    def test_the_verdict_names_every_failed_step_and_its_rerun(self):
        code, printed = self.run_fast(quick_exit=1, tests_exit=1)
        self.assertEqual(code, 1)
        named, _, rerun = printed[-1].partition("; rerun: ")
        self.assertIn("FAILED", named)
        for side in ("tests", "quick"):
            with self.subTest(side=side):
                label = printed[printed.index(f"{side} starts") - 1].removeprefix("[fast] ")
                self.assertIn(label, named)
                self.assertIn(f"meet.py {side}", rerun)


# A light gate failing as mise's summary output shows it: its lines once it
# ends, while the run goes on (here until the tests end), and the run's lines
# naming it only at the end. The tests end only once they are let go, which
# the harness does when fast has printed the failed gate's rerun. The alarm
# bounds a run that never lets them go.
TOLD = """
import signal, sys
signal.alarm(10)
side, go, done = sys.argv[1:]
if side == "tests":
    print("tests starts", flush=True)
    with open(go) as end:
        end.read(2)
    print("tests ends", flush=True)
    with open(done, "w") as end:
        end.write("done")
    sys.exit(0)
print("[lint:complexity] $ example-lint .", flush=True)
print("[lint:complexity] example.py: planted_function is over the bound", flush=True)
with open(done) as end:
    end.read()
print("[lint:complexity] exited with status 1", flush=True)
print("[lint:complexity] ERROR task failed", flush=True)
sys.exit(1)
"""


class LetsGo(io.BytesIO):
    """fast's output, which lets the tests end once it holds ``said``."""

    def __init__(self, said: bytes, go: Path) -> None:
        super().__init__()
        self.said = said
        # Opened before the tests open it, so the end that writes opens at once when it is let go.
        self.reader: int | None = os.open(go, os.O_RDONLY | os.O_NONBLOCK)
        self.go = go
        self.writer: int | None = None

    def write(self, data: Any) -> int:
        written = super().write(data)
        if self.writer is None and self.said in self.getvalue():
            self.writer = os.open(self.go, os.O_WRONLY | os.O_NONBLOCK)
            os.write(self.writer, b"go")
        return written

    def close(self) -> None:
        # Closed by the text wrapper over it and by the test's cleanup, whichever comes first.
        for end in (self.reader, self.writer):
            if end is not None:
                os.close(end)
        self.reader = self.writer = None
        super().close()


@tag("source", "tree")
class FastToldFailureTests(GateTestCase):
    """A light gate's failure is told with its rerun as it fails, while the tests go on."""

    def test_a_light_gate_that_fails_is_told_with_its_rerun_before_the_tests_end(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        script, go, done = (Path(folder.name, name) for name in ("told.py", "go", "done"))
        script.write_text(TOLD)
        os.mkfifo(go)
        os.mkfifo(done)
        printed = LetsGo(b"mise run lint:complexity", go)
        self.addCleanup(printed.close)

        def side(name: str) -> list[str]:
            return [sys.executable, str(script), name, str(go), str(done)]

        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True)
        code, lines = run_main(
            selected=("lint:complexity",),
            printed=printed,
            merge_base=head.stdout.strip(),
            affected=(set(), set()),
            test_labels=([], 0),
            turn_takers=(),
            tasks_command=side("quick"),
            test_command=side("tests"),
        )
        output = "\n".join(lines)
        self.assertIn("tests ends", lines, output)
        told = next(n for n, line in enumerate(lines) if "mise run lint:complexity" in line)
        self.assertLess(told, lines.index("tests ends"), output)
        self.assertEqual(code, 1)
        self.assertIn("lint:complexity", lines[-1])
