"""A gate's last line names what failed, so nobody reads back through its output.

Each step's output reaches the terminal unchanged, and what failed is named by
what saw it fail: mise's `-c` summary (`[task] exited with status N`), read on
the way past, names each task, and the test runner writes each failed test,
the exception it raised and its traceback, into the folder the step names in
HQ_TEST_REPORTS (hq/platform/core/test_runner.py).

The line, with every failed task and test on a line of its own, is also
written to `var/gate/<name>.txt`, whose path is printed above it, for a reader
whose copy of the output was cut. Each failed test's line there carries the
exception it raised, and the tracebacks follow the list, so the file alone
says why.

`python -m tools.gate.verdict <name> <command...>` runs one command this way.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# The folder a step's test runs write their failed tests into, a JSON list of
# FailedTest records per run (hq/platform/core/test_runner.py, which this
# helper does not import, so it runs where only tools/ is). GateVerdictTests
# runs that runner's record under a gate, which holds the two to one shape.
REPORTS = "HQ_TEST_REPORTS"
# A line of mise's `-c` summary.
TASK = re.compile(r"\[([^\]\s]+)\] exited with status \d+")
# A traceback kept in the file stops here.
KEPT_LINES = 60
# Past this many failing tests the line names the first few and counts the rest.
SHOWN = 10
VERDICTS = ROOT / "var" / "gate"


@dataclass(frozen=True)
class FailedTest:
    test: str
    exception: str = ""
    traceback: str = ""


def failed_tests(folder: Path) -> list[FailedTest]:
    """Every failed test the runs under a step wrote into its folder."""

    return [FailedTest(**record) for path in sorted(folder.glob("*.json")) for record in json.loads(path.read_text())]


@dataclass
class Step:
    label: str
    ok: bool = True
    code: int = 0
    tasks: list[str] = field(default_factory=list)
    tests: list[FailedTest] = field(default_factory=list)

    def read(self, line: str) -> None:
        for task in TASK.findall(line):
            if task not in self.tasks:
                self.tasks.append(task)

    def failures(self) -> list[str]:
        if self.ok:
            return []
        named = list(self.tasks)
        if self.tests:
            more = len(self.tests) - SHOWN
            shown = ", ".join(failed.test for failed in self.tests[:SHOWN])
            named.append(f"tests: {shown}" + (f" and {more} more" if more > 0 else ""))
        return named or [self.label]

    def everything_failed(self) -> list[str]:
        """Every failed task and test, none counted away, each test with the exception it raised."""

        if self.ok:
            return []
        tests = [f"{failed.test}: {failed.exception}" if failed.exception else failed.test for failed in self.tests]
        return [*self.tasks, *tests] or [self.label]

    def tracebacks(self) -> list[str]:
        """Each failed test's traceback under its name, cut at KEPT_LINES."""

        if self.ok:
            return []
        lines = []
        for failed in self.tests:
            if report := failed.traceback.splitlines():
                cut = ["..."] if len(report) > KEPT_LINES else []
                lines += ["", f"{failed.test}:", *report[:KEPT_LINES], *cut]
        return lines


def pass_through(output: int, step: Step) -> None:
    """Copy the command's output to ours byte for byte, each line read on the way past."""

    pending = b""
    # Chunks, not lines: the runner's progress dots arrive without a newline.
    while chunk := os.read(output, 65536):
        sys.stdout.buffer.write(chunk)
        sys.stdout.buffer.flush()
        *lines, pending = (pending + chunk).split(b"\n")
        for line in lines:
            step.read(line.decode(errors="replace"))
    step.read(pending.decode(errors="replace"))


def run(label: str, cmd: list[str], cwd: Path = ROOT) -> Step:
    """Run a command, its output passed through, and gather what failed in it."""

    step = Step(label)
    with tempfile.TemporaryDirectory(prefix="gate-") as reports:
        env = {**os.environ, "PYTHONUNBUFFERED": "1", REPORTS: reports}
        with subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as proc:
            assert proc.stdout
            try:
                pass_through(proc.stdout.fileno(), step)
            except KeyboardInterrupt:
                # The terminal's interrupt reached the command too.
                proc.wait()
                raise
            except BaseException:
                proc.terminate()
                proc.wait()
                raise
        step.tests = failed_tests(Path(reports))
    step.code = proc.returncode
    step.ok = step.code == 0
    return step


def verdict(name: str, started: float, steps: list[Step], passed: str = "", outcome: str = "FAILED") -> str:
    elapsed = f"{time.monotonic() - started:.1f}s"
    failed = [failure for step in steps for failure in step.failures()]
    if failed:
        return f"[{name}] {outcome} in {elapsed}: {'; '.join(failed)}"
    return f"[{name}] passed in {elapsed}{passed}"


def close(
    name: str, started: float, steps: list[Step], passed: str = "", failed: str = "", outcome: str = "FAILED"
) -> None:
    """Print the closing verdict, after the path of the file that keeps it whole."""

    named = [failure for step in steps for failure in step.everything_failed()]
    line = verdict(name, started, steps, passed, outcome) + (failed if named else "")
    path = VERDICTS / f"{re.sub(r'[^\w.-]+', '-', name).strip('-')}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    kept = [traceback for step in steps for traceback in step.tracebacks()]
    path.write_text("\n".join([line, *named, *kept]) + "\n")
    print(f"[{name}] verdict kept in {path}", flush=True)
    print(line, flush=True)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        sys.exit("usage: python -m tools.gate.verdict <name> <command...>")
    name, cmd = argv[0], argv[1:]
    started = time.monotonic()
    try:
        step = run(name, cmd)
    except KeyboardInterrupt:
        return 130
    close(name, started, [step])
    return 0 if step.ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
