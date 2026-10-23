"""A gate's last line names what failed, so nobody reads back through its output.

Each step's output goes to the terminal as it arrives and is read on the way
past: mise's `-c` summary names the tasks that failed, and the test runner's
`FAIL:`/`ERROR:` headers name the tests.

The line, with every failed task and test on a line of its own, is also
written to `var/gate/<name>.txt`, whose path is printed above it, for a reader
whose copy of the output was cut. Each failed test's line there carries the
exception it raised, and the tracebacks follow the list, so the file alone
says why without reading back through output an agent's tool may have cut.

`python -m tools.gate.verdict <name> <command...>` runs one command this way.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TASK = re.compile(r"\[([^\]\s]+)\] exited with status \d+")
TEST = re.compile(r"\b(?:FAIL|ERROR): ([\w.]+) \(([\w.]+)\)")
# A test's report: its header at the start of a line (behind the `[task] `
# prefix mise may add), a rule, its traceback, and the rule that closes it.
PREFIX = re.compile(r"^(?:\[[\w:.*-]+\] )*")
RULE = re.compile(r"^(?:={20,}|-{20,})$")
# A traceback kept in the file stops here.
KEPT_LINES = 60
# Past this many failing tests the line names the first few and counts the rest.
SHOWN = 10
VERDICTS = ROOT / "var" / "gate"


@dataclass
class Step:
    label: str
    ok: bool = True
    code: int = 0
    tasks: list[str] = field(default_factory=list)
    tests: list[str] = field(default_factory=list)
    # Each failed test's traceback, as the runner reported it.
    reports: dict[str, list[str]] = field(default_factory=dict)
    reading: str | None = None
    opened: bool = False

    def read(self, line: str) -> None:
        for task in TASK.findall(line):
            if task not in self.tasks:
                self.tasks.append(task)
        for name, where in TEST.findall(line):
            test = test_id(name, where)
            if test not in self.tests:
                self.tests.append(test)
        self.follow(PREFIX.sub("", line.rstrip("\r")))

    def follow(self, line: str) -> None:
        """Keep the report under a FAIL or ERROR header, up to the rule that closes it."""

        if header := TEST.match(line):
            test = test_id(*header.groups())
            # A test's first report is kept; another subtest's adds nothing the name lacks.
            self.reading = None if test in self.reports else test
            self.reports.setdefault(test, [])
            self.opened = False
        elif self.reading:
            # The first rule under the header (after the test's description,
            # when it has one) opens the report; the next one closes it.
            if RULE.match(line):
                self.reading = None if self.opened else self.reading
                self.opened = True
            elif self.opened:
                self.reports[self.reading].append(line)

    def message(self, test: str) -> str:
        """The exception a failed test raised: the first unindented line after its last traceback header."""

        report = self.reports.get(test, [])
        starts = [n for n, line in enumerate(report) if line.startswith("Traceback (most recent call last)")]
        after = report[starts[-1] + 1 :] if starts else report
        return next((line for line in after if line and not line[0].isspace()), "")

    def failures(self) -> list[str]:
        if self.ok:
            return []
        named = list(self.tasks)
        if self.tests:
            more = len(self.tests) - SHOWN
            named.append("tests: " + ", ".join(self.tests[:SHOWN]) + (f" and {more} more" if more > 0 else ""))
        return named or [self.label]

    def everything_failed(self) -> list[str]:
        """Every failed task and test, none counted away, each test with the exception it raised."""

        if self.ok:
            return []
        tests = [f"{test}: {message}" if (message := self.message(test)) else test for test in self.tests]
        return [*self.tasks, *tests] or [self.label]

    def tracebacks(self) -> list[str]:
        """Each failed test's report under its name, cut at KEPT_LINES."""

        if self.ok:
            return []
        lines = []
        for test in self.tests:
            if report := self.reports.get(test):
                cut = ["..."] if len(report) > KEPT_LINES else []
                lines += ["", f"{test}:", *report[:KEPT_LINES], *cut]
        return lines


def test_id(name: str, where: str) -> str:
    """A test's dotted id from the runner's `name (where)`, where may already end in the name."""

    return where if where.endswith(f".{name}") else f"{where}.{name}"


def run(label: str, cmd: list[str], cwd: Path = ROOT) -> Step:
    """Run a command, its output passed through byte for byte and read for failures."""

    step = Step(label)
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    with subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as proc:
        assert proc.stdout
        pending = b""
        try:
            # Chunks, not lines: the runner's progress dots arrive without a newline.
            while chunk := os.read(proc.stdout.fileno(), 65536):
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
                *lines, pending = (pending + chunk).split(b"\n")
                for line in lines:
                    step.read(line.decode(errors="replace"))
            step.read(pending.decode(errors="replace"))
        except KeyboardInterrupt:
            proc.wait()
            raise
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
