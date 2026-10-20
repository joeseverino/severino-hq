"""A gate's last line names what failed, so nobody reads back through its output.

Each step's output goes to the terminal as it arrives and is read on the way
past: mise's `-c` summary names the tasks that failed, and the test runner's
`FAIL:`/`ERROR:` headers name the tests.

`python scripts/gate_verdict.py <name> <command...>` runs one command this way.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TASK = re.compile(r"\[([^\]\s]+)\] exited with status \d+")
TEST = re.compile(r"\b(?:FAIL|ERROR): ([\w.]+) \(([\w.]+)\)")
# Past this many failing tests the line names the first few and counts the rest.
SHOWN = 10


@dataclass
class Step:
    label: str
    ok: bool = True
    tasks: list[str] = field(default_factory=list)
    tests: list[str] = field(default_factory=list)

    def read(self, line: str) -> None:
        for task in TASK.findall(line):
            if task not in self.tasks:
                self.tasks.append(task)
        for name, where in TEST.findall(line):
            test = where if where.endswith(f".{name}") else f"{where}.{name}"
            if test not in self.tests:
                self.tests.append(test)

    def failures(self) -> list[str]:
        if self.ok:
            return []
        named = list(self.tasks)
        if self.tests:
            more = len(self.tests) - SHOWN
            named.append("tests: " + ", ".join(self.tests[:SHOWN]) + (f" and {more} more" if more > 0 else ""))
        return named or [self.label]


def run(label: str, cmd: list[str]) -> Step:
    """Run a command, its output passed through byte for byte and read for failures."""

    step = Step(label)
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    with subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as proc:
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
    step.ok = proc.returncode == 0
    return step


def verdict(name: str, started: float, steps: list[Step], passed: str = "") -> str:
    elapsed = f"{time.monotonic() - started:.1f}s"
    failed = [failure for step in steps for failure in step.failures()]
    if failed:
        return f"[{name}] FAILED in {elapsed}: {'; '.join(failed)}"
    return f"[{name}] passed in {elapsed}{passed}"


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        sys.exit("usage: gate_verdict.py <name> <command...>")
    name, cmd = argv[0], argv[1:]
    started = time.monotonic()
    try:
        step = run(name, cmd)
    except KeyboardInterrupt:
        return 130
    print(verdict(name, started, [step]), flush=True)
    return 0 if step.ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
