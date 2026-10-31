"""A gate's last lines name what failed and the command that reruns just that,
so nobody reads back through its output, and above them each failed task's
last lines.

Each failed task is named as mise names it and each failed test as the test
runner records it, and `var/gate/<name>.txt` keeps the lines whole, beside the
run before it in `<name>.prev.txt` (docs/Development.md, The gates).
`python -m tools.gate.verdict <name> <command...>` runs one command so, and
`--step <label> <command...>...` several.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from collections import deque
from collections.abc import Collection
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# The folder a step's test runs write their failed tests into, a JSON list of
# FailedTest records per run (hq/platform/core/test_runner.py, which this
# helper does not import, so it runs where only tools/ is). GateVerdictTests
# runs that runner's record under a gate, which holds the two to one shape.
REPORTS = "HQ_TEST_REPORTS"
# A line of mise's `-c` summary, and the line a task that failed alone ends on,
# under the name of each task it runs inside. Anchored, so a tool that quotes
# such a line from a file (a formatter's diff of a test) names nothing.
TASK = re.compile(r"^(?:\[[^\]\s]+\] )*\[([^\]\s]+)\] (?:exited with status \d+|ERROR task failed)\s*$")
# The runner's heading over a failed test's traceback, under the task whose
# run printed it: the test's id is in the parentheses.
FAILED_UNDER = re.compile(r"^(?:\[[^\]\s]+\] )*\[([^\]\s]+)\] (?:FAIL|ERROR): \S+ \(([^)\s]+)\)")
# The line mise prints after a task that passed, its time as Rust prints a
# Duration: in seconds from one second up, in ms, µs or ns below.
TIMED = re.compile(r"\[([^\]\s]+)\] Finished in (\d+(?:\.\d+)?)(ns|µs|ms|s)\b")
UNITS = {"ns": 1e-9, "µs": 1e-6, "ms": 1e-3, "s": 1.0}
# A terminal escape sequence. mise colors its task lines on a CI runner even
# into a pipe (and wherever CLICOLOR_FORCE is set), so a line is read without
# them while the copy passed through keeps its color.
ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
# How the test loader reports a module that raised on import, whatever the
# runner around it writes: a ref older than the failure record writes none.
UNIMPORTED = re.compile(r"^ImportError: Failed to import test module: (\S+)")
# A traceback kept in the file stops here.
KEPT_LINES = 60
# A failed task's last lines kept in the file, and shown above the verdict, so
# why it failed is read there rather than by running it again.
TAIL_KEPT = 20
TAIL_SHOWN = 5
# Each task's name a line is printed under, outermost first.
PREFIX = re.compile(r"\[([^\]\s]+)\] ?")
# Past this many failing tests the line names the first few and counts the rest,
# and the rerun names their classes.
SHOWN = 10
VERDICTS = ROOT / "var" / "gate"
# The command that reruns a test by label, by the task or gate it failed under.
TESTS_RERUN = {"test:composed": "mise run test --composed"}
TEST_RERUN = "mise run test"
MISE_RUN = "mise run "
# How the test loader names a module that raised on import.
FAILED_IMPORT = "unittest.loader._FailedTest."


@dataclass(frozen=True)
class FailedTest:
    test: str
    exception: str = ""
    traceback: str = ""


def failed_tests(folder: Path) -> list[FailedTest]:
    """Every failed test the runs under a step wrote into its folder."""

    return [FailedTest(**record) for path in sorted(folder.glob("*.json")) for record in json.loads(path.read_text())]


def test_label(test: str) -> str:
    """The label that runs a failed test again: its id, a class fixture's class, an unimported module."""

    if fixture := re.fullmatch(r"\w+ \(([\w.]+)\)", test):
        return fixture[1]
    return test.split(" ", 1)[0].removeprefix(FAILED_IMPORT)


def narrowed(labels: list[str]) -> list[str]:
    """The labels, or past SHOWN of them their classes, so the rerun stays one line."""

    if len(labels) <= SHOWN:
        return labels
    classes = [label.rsplit(".", 1)[0] if label.rsplit(".", 1)[-1].startswith("test") else label for label in labels]
    return list(dict.fromkeys(classes))


def leaves(steps: list[Step]) -> set[str]:
    """Every failed task named beside one of its own `<task>:<leaf>` tasks: an
    aggregate fails because its leaf did, so only the leaf is named."""

    named = {task for step in steps if not step.ok for task in step.tasks}
    return {task for task in named if any(other.startswith(f"{task}:") for other in named)}


def command_line(cmd: list[str]) -> str:
    """A step's command as typed at a prompt, from the checkout, or nothing for
    one no prompt takes on one line (an inline script)."""

    if any("\n" in word for word in cmd):
        return ""
    words = list(cmd)
    if words[:1] == ["env"]:
        words = words[1:]
    if words[:1] == [sys.executable]:
        words[:1] = ["uv", "run", "python"]
    return shlex.join(words)


@dataclass
class Step:
    label: str
    ok: bool = True
    code: int = 0
    tasks: list[str] = field(default_factory=list)
    tests: list[FailedTest] = field(default_factory=list)
    seconds: float = 0.0
    took: dict[str, float] = field(default_factory=dict)
    unimported: list[str] = field(default_factory=list)
    # The command the step ran, which reruns a failure it names nothing in.
    command: list[str] = field(default_factory=list)
    # Each failed test's label, by the task whose run printed it.
    under: dict[str, str] = field(default_factory=dict)
    # The last lines printed under each task's name, and the step's own under "".
    said: dict[str, deque[str]] = field(default_factory=dict)

    def read(self, line: str) -> None:
        line = ESCAPE.sub("", line)
        self.hear(line)
        if (module := UNIMPORTED.match(line)) and module[1] not in self.unimported:
            self.unimported.append(module[1])
        if (task := TASK.match(line)) and task[1] not in self.tasks:
            self.tasks.append(task[1])
        if failed := FAILED_UNDER.match(line):
            self.under.setdefault(test_label(failed[2]), failed[1])
        for task, amount, unit in TIMED.findall(line):
            self.took[task] = float(amount) * UNITS[unit]

    def hear(self, line: str) -> None:
        """Keep ``line`` among the last said under each task it is printed under,
        past mise's own line that the task failed."""

        if TASK.match(line) or not line.strip():
            return
        self.said.setdefault("", deque(maxlen=TAIL_KEPT)).append(line)
        at = 0
        while prefix := PREFIX.match(line, at):
            at = prefix.end()
            if line[at:].strip():
                self.said.setdefault(prefix[1], deque(maxlen=TAIL_KEPT)).append(line[at:])

    def tails(self, aggregates: Collection[str] = ()) -> dict[str, list[str]]:
        """Each failed leaf task's last lines, or the step's own when it names
        no task: whatever failed but a test, whose traceback says why."""

        if self.ok:
            return {}
        named = [task for task in self.tasks if task not in aggregates]
        keys = [(task, task) for task in named if task not in self.under.values()]
        if not named and not self.tests:
            keys = [(self.label, "")]
        return {name: list(self.said[key]) for name, key in keys if self.said.get(key)}

    def failures(self, aggregates: Collection[str] = ()) -> list[str]:
        if self.ok:
            return []
        named = [task for task in self.tasks if task not in aggregates]
        if self.tests:
            more = len(self.tests) - SHOWN
            shown = ", ".join(failed.test for failed in self.tests[:SHOWN])
            named.append(f"tests: {shown}" + (f" and {more} more" if more > 0 else ""))
        return named or [self.label]

    def everything_failed(self, aggregates: Collection[str] = ()) -> list[str]:
        """Every failed task and test, none counted away, each test with the exception it raised."""

        if self.ok:
            return []
        tests = [f"{failed.test}: {failed.exception}" if failed.exception else failed.test for failed in self.tests]
        return [*(task for task in self.tasks if task not in aggregates), *tests] or [self.label]

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


def hand_on(tests: list[FailedTest]) -> None:
    """Write a step's failed tests into the folder of the gate this one runs under, when it runs under one.

    A gate that runs another (`check` runs `test --composed`) hands its step a
    folder of its own, so without this the outer verdict would name the inner
    gate's task and lose its tests.
    """

    if (outer := os.environ.get(REPORTS)) and tests:
        handle, _ = tempfile.mkstemp(suffix=".json", dir=outer)
        with os.fdopen(handle, "w") as record:
            json.dump([asdict(failed) for failed in tests], record)


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

    step = Step(label, command=cmd)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="gate-") as reports:
        # mise turns its Finished line off in a task's own environment, so a mise run
        # inside a task prints it only when asked.
        env = {**os.environ, "PYTHONUNBUFFERED": "1", REPORTS: reports, "MISE_TASK_TIMINGS": "1"}
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
    hand_on(step.tests)
    step.seconds = time.monotonic() - started
    step.code = proc.returncode
    step.ok = step.code == 0
    return step


def verdict(name: str, started: float, steps: list[Step], passed: str = "", outcome: str = "FAILED") -> str:
    elapsed = f"{time.monotonic() - started:.1f}s"
    aggregates = leaves(steps)
    failed = [failure for step in steps for failure in step.failures(aggregates)]
    if failed:
        return f"[{name}] {outcome} in {elapsed}: {'; '.join(failed)}"
    return f"[{name}] passed in {elapsed}{passed}"


def test_reruns(name: str, steps: list[Step], tests: str) -> dict[str, list[str]]:
    """Each failed test's label, under the command that reruns it."""

    labels: dict[str, dict[str, None]] = {}
    for step in steps:
        for failed in step.tests:
            label = test_label(failed.test)
            command = tests or TESTS_RERUN.get(step.under.get(label, "")) or TESTS_RERUN.get(name, TEST_RERUN)
            labels.setdefault(command, {})[label] = None
    return {command: list(named) for command, named in labels.items()}


def rerun(name: str, steps: list[Step], tests: str = "") -> str:
    """The command that runs again just what failed: each failed leaf task, the
    failed tests by label rather than the task that ran them, and a failed step
    that names neither by its own command. Every `mise run` among them is one,
    `:::` between its runs."""

    aggregates = leaves(steps)
    failed = [step for step in steps if not step.ok]
    runs = [
        task for step in failed for task in step.tasks if task not in aggregates and task not in step.under.values()
    ]
    others = [typed for step in failed if not step.tasks and not step.tests and (typed := command_line(step.command))]
    others += [
        shlex.join([*shlex.split(command), *narrowed(named)])
        for command, named in test_reruns(name, failed, tests).items()
    ]
    runs += [command.removeprefix(MISE_RUN) for command in others if command.startswith(MISE_RUN)]
    others = [command for command in others if not command.startswith(MISE_RUN)]
    if runs := list(dict.fromkeys(runs)):
        others.insert(0, MISE_RUN + ("-c " if len(runs) > 1 else "") + " ::: ".join(runs))
    return "; ".join(others)


def timings(steps: list[Step]) -> list[str]:
    """How long each task took, slowest first, and each step's whole time when there are several."""

    took = {task: seconds for step in steps for task, seconds in step.took.items()}
    if len(steps) > 1:
        took |= {step.label: step.seconds for step in steps}
    ranked = sorted(took.items(), key=lambda item: -item[1])
    return ["", "Slowest first:", *(f"{seconds:8.1f}s  {task}" for task, seconds in ranked)] if ranked else []


def close(
    name: str,
    started: float,
    steps: list[Step],
    passed: str = "",
    failed: str = "",
    outcome: str = "FAILED",
    tests: str = "",
) -> None:
    """Print the closing verdict, ending on the command that reruns what failed,
    after the path of the file that keeps it whole, the last run's beside it.

    ``tests`` is the command a failed test reruns under, when it is not
    ``mise run test``."""

    aggregates = leaves(steps)
    named = [failure for step in steps for failure in step.everything_failed(aggregates)]
    line = verdict(name, started, steps, passed, outcome)
    if named:
        line += failed + (f"; rerun: {command}" if (command := rerun(name, steps, tests)) else "")
    stem = re.sub(r"[^\w.-]+", "-", name).strip("-")
    path = VERDICTS / f"{stem}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.replace(VERDICTS / f"{stem}.prev.txt")
    kept = [traceback for step in steps for traceback in step.tracebacks()]
    tails = {task: said for step in steps for task, said in step.tails(aggregates).items()}
    ended = [part for task, said in tails.items() for part in ("", f"{task} ended on:", *said)]
    path.write_text("\n".join([line, *named, *ended, *timings(steps), *kept]) + "\n")
    for task, said in tails.items():
        shown = (f"[{name}]   {said_line}" for said_line in said[-TAIL_SHOWN:])
        print(f"[{name}] {task} ended on:", *shown, sep="\n", flush=True)
    print(f"[{name}] verdict kept in {path}", flush=True)
    print(line, flush=True)


USAGE = "usage: python -m tools.gate.verdict <name> (<command...> | --step <label> <command...>...)"


def commands(name: str, argv: list[str]) -> list[tuple[str, list[str]]]:
    """(label, command) for each step: the one command under the gate's name, or each `--step`."""

    if not argv:
        sys.exit(USAGE)
    if argv[0] != "--step":
        return [(name, argv)]
    words: list[list[str]] = []
    for word in argv:
        if word == "--step":
            words.append([])
        else:
            words[-1].append(word)
    if any(len(step) < 2 for step in words):
        sys.exit(USAGE)
    return [(label, cmd) for label, *cmd in words]


def main(argv: list[str]) -> int:
    if not argv:
        sys.exit(USAGE)
    name, planned = argv[0], commands(argv[0], argv[1:])
    started = time.monotonic()
    steps = []
    try:
        for label, cmd in planned:
            if len(planned) > 1:
                print(f"[{name}] {label}", flush=True)
            steps.append(run(label, cmd))
    except KeyboardInterrupt:
        return 130
    close(name, started, steps)
    return 0 if all(step.ok for step in steps) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
