"""The inner-loop gate: only what the change can have broken.

What it selects from the changed files, and which quick gates it runs beside
them: docs/Development.md, What `fast` runs. ``mise run check`` stays the gate
before a push. The selection is tools/gate/selection.py's, the chain of passed
trees tools/gate/chain.py's.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from hq.platform.core.tests.task import FAST_BASE, Usage, changed_paths, usage
from tests.mise_tasks import HEAVY_LOCK, locks, mise_tasks
from tools.gate import chain, turn as gate_turn
from tools.gate.chain import SENT, fast_base, record_pass
from tools.gate.selection import python_files, selection, source_first, whole_suite
from tools.gate.turn import ONE, in_share, weighed
from tools.gate.verdict import Gate, Step, run

# The tag of the quick gates: each declares the files it reads as its sources,
# and mise's --affected picks those a change touched. Those selected run side
# by side, in one mise run, before any test starts; each Django gate opens a
# database of its own (the django task template).
QUICK = "fast"
# The tag of a gate fast runs beside its tests in its share of the heavy turn
# rather than among the quick gates (docs/Development.md, Heavy gates take
# turns), as it does a gate that takes the turn's lock itself.
HEAVY = "heavy"
# How long fast's tests and the gates beside them may hold their share, as long
# as fast:tests may hold one on its own.
TURN_BOUND = "30m"
# The shell gate also reads every file its lister names, which no glob can, so
# a change selects it when the lister names a path it touched: as this tree has
# it, or as the base had it once deleted.
SHELL = "lint:shell"
SHELL_SOURCES = Path(__file__).resolve().parent / "shell-sources.sh"
# Said when a quick gate failed, which ends the run before any test.
QUICK_FAILED = "[fast] a quick gate failed, so no test and no gate in the heavy turn started"


def quick_gates() -> tuple[str, ...]:
    """Every task tagged QUICK."""

    return tuple(quick_tasks())


def quick_tasks() -> dict[str, dict[str, Any]]:
    """Every task tagged QUICK, by name, as mise lists it, read once per checkout."""

    return _quick_tasks(chain.ROOT)


@functools.cache
def _quick_tasks(root: Path) -> dict[str, dict[str, Any]]:
    """A cache per checkout, so a run against a fixture (a test's) never answers for another."""

    listed = mise_json("tasks", "ls", "--tag", QUICK, "--hidden", "--local", "--json", root=root)
    return {task["name"]: task for task in listed}


# A quick gate whose tool fixes what it finds declares this flag, which writes
# the fixes instead of checking (mise-tasks/lint.toml); fast runs it first.
FIX = re.compile(r'^flag "--fix"', re.MULTILINE)
# A quick gate whose tool reads a file at a time declares this argument, and is
# handed the files the change touched (mise.toml, the hk task template).
FILES = re.compile(r'^arg "\[files\]\.\.\."', re.MULTILINE)
# A file task declares its usage in its own header, which mise's listing leaves out.
FILE_USAGE = re.compile(r"^#USAGE (.*)$", re.MULTILINE)


def fixers(tasks: tuple[str, ...]) -> tuple[str, ...]:
    """Those of ``tasks`` whose usage declares the fix flag."""

    quick = quick_tasks()
    return tuple(task for task in tasks if task in quick and FIX.search(usage_of(quick[task])))


def handed(task: str, paths: list[str]) -> list[str]:
    """The words that run ``task``: with ``paths`` when it reads a file at a time."""

    quick = quick_tasks()
    return [task, *paths] if task in quick and FILES.search(usage_of(quick[task])) else [task]


def usage_of(task: dict[str, Any]) -> str:
    """A task's usage spec, from its listing or a file task's header."""

    if task.get("file"):
        return "\n".join(FILE_USAGE.findall(Path(task["file"]).read_text(encoding="utf-8")))
    return task.get("usage") or ""


# How a verdict names the files the fixes rewrote, which the checkout holds
# uncommitted: a commit of them is the tree the run judged.
REWROTE = "the fixes rewrote, uncommitted:"


def fixed_first(
    gate: Gate, tasks: tuple[str, ...], base: str, paths: tuple[list[str], list[str]], *, whole: bool = False
) -> tuple[tuple[list[str], list[str]], list[str]]:
    """The changed and deleted paths once the fix modes of ``tasks`` have run,
    on the whole tree when ``whole``, and the files they rewrote. The gate is
    set to judge the tree they left, the one a commit of those files gives."""

    fixed = fix(tasks, [] if whole else [*paths[0], *paths[1]])
    if not fixed:
        return paths, []
    print(f"[fast] the quick gates' fixes rewrote: {' '.join(fixed)}", flush=True)
    gate.restart()
    return changed_paths(base, chain.ROOT), fixed


def rewrote(fixed: list[str]) -> str:
    return f"{REWROTE} {' '.join(sorted(set(fixed)))}"


def fixed_before_sending(base: str) -> list[str]:
    """The files the fix modes of the quick gates the change from ``base``
    selects rewrote here, run before the tree is sent to another machine,
    which only checks."""

    changed, deleted = changed_paths(base, chain.ROOT)
    return fix(selected_tasks([*changed, *deleted], base), [*changed, *deleted])


def fix(tasks: tuple[str, ...], paths: list[str]) -> list[str]:
    """Run the fix mode of each of ``tasks`` that has one, in one run of mise, on
    ``paths`` where it reads a file at a time, and return the paths it rewrote,
    so the gates then judge fixed files and formatting never fails a run. What
    a fixer still finds, its gate reports."""

    fixing = fixers(tasks)
    # A run sent from another machine only checks, as CI does: a fix written there never reaches the checkout
    # that sent it, and the run would pass on a tree it changed (tools/gate/remote-host.sh). The sender runs the
    # fixes first (fixed_before_sending).
    if not fixing or os.environ.get(SENT):
        return []
    before = dirty()
    words = [word for task in fixing for word in (":::", *handed(task, paths), "--fix")][1:]
    subprocess.run(["mise", "run", "--continue-on-error", *words], cwd=chain.ROOT, check=False, capture_output=True)
    after = dirty()
    # A path in one alone was rewritten too: one the fix left as HEAD has it is no longer dirty.
    return sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))


def dirty() -> dict[str, str]:
    """Each path where the checkout differs from HEAD, untracked ones included, by a digest of its bytes."""

    present, _ = changed_paths("HEAD", chain.ROOT)
    return {
        path: hashlib.sha256((chain.ROOT / path).read_bytes()).hexdigest()
        for path in present
        if (chain.ROOT / path).is_file()
    }


def selected_tasks(paths: list[str], base: str) -> tuple[str, ...]:
    """The quick gates a change can have broken: those whose sources or definition it touches, as mise's
    --affected reads the change from ``base`` with this checkout's work, and the shell gate when its lister
    names one of ``paths``. HEAD is named so no CI metadata stands in for it."""

    affected = ("--affected", "--affected-base", base, "--affected-head", "HEAD", "--affected-json")
    selected = tuple(task["name"] for task in mise_json("run", "--tag", QUICK, *affected)["tasks"])
    return selected if SHELL in selected or not shell_files(paths, base) else (*selected, SHELL)


def mise_json(*args: str, root: Path | None = None) -> Any:
    """What a mise command prints as JSON, run in ``root`` or this checkout; its errors print as they come."""

    run = subprocess.run(["mise", *args], cwd=root or chain.ROOT, check=True, stdout=subprocess.PIPE, text=True)
    return json.loads(run.stdout)


def shell_files(paths: list[str], base: str | None = None) -> list[str]:
    """The paths the shell gate's lister names: each as this tree has it, or as ``base`` had it."""

    here = [path for path in paths if (chain.ROOT / path).is_file()]
    found = list_shell(chain.ROOT, here) if here else []
    gone = [path for path in paths if path not in here]
    if base is None or not gone:
        return found
    with tempfile.TemporaryDirectory() as folder:
        for path in gone:
            # A path the base lacks too is no shell file of either.
            held = subprocess.run(
                ["git", "cat-file", "blob", f"{base}:{path}"], cwd=chain.ROOT, capture_output=True, check=False
            )
            if held.returncode == 0:
                (Path(folder) / path).parent.mkdir(parents=True, exist_ok=True)
                (Path(folder) / path).write_bytes(held.stdout)
        return found + list_shell(Path(folder), gone)


def list_shell(root: Path, paths: list[str]) -> list[str]:
    """Those of ``paths``, under ``root``, that the shell gate's lister names."""

    listed = subprocess.run(
        ["/bin/sh", str(SHELL_SOURCES), *paths], cwd=root, check=True, capture_output=True, text=True
    )
    return listed.stdout.splitlines()


# fast's test run, the task that runs it after fast:source
# (mise-tasks/fast/), and the config environment of the one run of mise it shares
# with the quick gates (mise.fast.toml), whose output is mise's summary.
TESTS = "fast:tests"
ENVIRONMENT = "fast"
# The variable fast:tests reads the labels of fast:source from, its dependency.
SOURCE = "HQ_FAST_SOURCE"


def heavy_gates(tasks: tuple[str, ...]) -> tuple[str, ...]:
    """Those of ``tasks`` that run in the heavy turn: tagged HEAVY, or taking
    the turn's lock themselves."""

    listed = mise_tasks()
    return tuple(task for task in tasks if HEAVY in (listed[task].get("tags") or ()) or HEAVY_LOCK in locks(task))


def mise_run(words: list[str], parallel: str) -> list[str]:
    """One run of mise in fast's config environment, which runs every task to
    its end when one fails, so one run names every failure it reaches."""

    jobs = ["--jobs", "1"] if parallel == "1" else []
    return ["mise", "--env", ENVIRONMENT, "run", "--continue-on-error", *jobs, *words]


def quick_command(tasks: tuple[str, ...], parallel: str, paths: list[str]) -> list[str]:
    """The run of the quick gates, side by side, each that reads a file at a time on ``paths``."""

    return mise_run([word for task in tasks for word in (*handed(task, paths), ":::")][:-1], parallel)


def stage_tokens(tasks: tuple[str, ...], source: list[str], labels: list[str], parallel: str) -> int:
    """The tokens of the share fast's tests and the gates beside them (``tasks``) take, side by side: what
    tools/gate/turn.py weighs the tests at (the source checks before them run serially, in one process), and
    one for each gate tagged HEAVY, whose one process loads the machine. A gate that takes the turn's lock
    itself needs the whole machine, so beside one the stage takes every token."""

    if any(HEAVY_LOCK in locks(task) for task in tasks):
        return gate_turn.CAPACITY
    tests = weighed(["--noinput", "--parallel", parallel, *labels], chain.ROOT) if labels else 0
    return min(max(tests, ONE if source else 0) + ONE * len(tasks), gate_turn.CAPACITY)


def run_command(
    tasks: tuple[str, ...], source: list[str], labels: list[str], parallel: str, paths: list[str], base: str
) -> list[str]:
    """The run of the tests beside the heavy turn's gates (``tasks``), in one
    run of mise in one share of the turn sized by ``stage_tokens``.

    The share tells each task under it that it holds the turn, as mise hands a
    lock down, so the gates and the tests go ahead side by side.
    """

    gates = [word for task in tasks for word in (*handed(task, paths), ":::")]
    tests = [TESTS, "--parallel", parallel, *labels]
    command = ["env", f"{SOURCE}={' '.join(source)}", f"{FAST_BASE}={base}", *mise_run([*gates, *tests], parallel)]
    tokens = stage_tokens(tasks, source, labels, parallel)
    return [*in_share(tokens, TURN_BOUND), *command] if tokens else command


def quick_stage(tasks: tuple[str, ...], parallel: str, paths: list[str], again: str = "") -> list[Step]:
    """The run of those of ``tasks`` that are quick, side by side, or none
    when none is, each that reads a file at a time on ``paths``; ``again``
    names a rerun in its label."""

    quick = tuple(task for task in tasks if task not in heavy_gates(tasks))
    if not quick:
        return []
    print(f"[fast] the quick gates that read what changed: {' '.join(quick)}")
    return [step(f"quick gates{again}", quick_command(quick, parallel, paths))]


def gates_and_tests(
    tasks: tuple[str, ...],
    labels: list[str],
    files: list[Path],
    parallel: str,
    paths: list[str],
    base: str,
    *,
    again: str = "",
) -> list[Step]:
    """The quick gates among ``tasks``, then, once they pass, the tests beside
    the rest, which run in the heavy turn, each gate that reads a file at a time
    on ``paths``, those in the turn told ``base``; ``again`` names a rerun in
    its labels."""

    steps = quick_stage(tasks, parallel, paths, again)
    if not all(each.ok for each in steps):
        print(QUICK_FAILED, flush=True)
        return steps
    slow = heavy_gates(tasks)
    if slow:
        print(f"[fast] in the heavy turn, beside the tests: {' '.join(slow)}")
    source, rest = source_first(labels, files)
    if source:
        print(f"[fast] first, the source checks that read the browser gate's modules: {' '.join(source)}")
    label = "tests and the gates in the heavy turn" if slow else "tests"
    return [*steps, step(f"{label}{again}", run_command(slow, source, rest, parallel, paths, base))]


def step(label: str, cmd: list[str]) -> Step:
    print(f"[fast] {label}", flush=True)
    return run(label, cmd)


def judge(gate: Gate, steps: list[Step], base: str, *, whole: bool, fixed: list[str]) -> int:
    """Close the run on ``steps``, naming the files its fixes rewrote: a pass records its tree."""

    passed = all(s.ok for s in steps)
    if passed:
        judged = gate.judged()
        record_pass(judged, base, whole=whole)
    said = f"; {rewrote(fixed)}" if fixed else ""
    gate.close(steps, f" (full gate before push: `mise run check`){said}", said)
    return 0 if passed else 1


def main(asked: Usage) -> int:
    # The task's usage reads CHECK_BASE and CHECK_PARALLEL behind its own arguments.
    parallel = asked.text("parallel", "auto")
    list_only = asked.switch("list")
    whole_asked = asked.switch("whole")
    # A run opens on where its verdict is kept; a listing keeps none.
    gate = None if list_only else Gate("fast")
    base, why = fast_base(asked.text("base") or None)
    changed, deleted = changed_paths(base, chain.ROOT)
    selected = selected_tasks([*changed, *deleted], base) if gate else ()
    # The quick gates the change selects run before the tests are selected, so
    # a finding fails the run in the seconds they take.
    early: tuple[str, ...] = ()
    first: list[Step] = []
    fixed: list[str] = []
    # What a gate that reads a file at a time is handed: none, the whole tree, when the whole is asked.
    handing: list[str] = []
    if gate:
        (changed, deleted), fixed = fixed_first(gate, selected, base, (changed, deleted), whole=whole_asked)
        handing = [] if whole_asked else [*changed, *deleted]
        early = quick_gates() if whole_asked else selected
        early = tuple(task for task in early if task not in heavy_gates(early))
        first = quick_stage(early, parallel, handing)
        if not all(each.ok for each in first):
            print(QUICK_FAILED, flush=True)
            steps = [*first, step("patch integrity", ["git", "diff", "--check", base])]
            return judge(gate, steps, base, whole=False, fixed=fixed)
    files = python_files()
    labels, total = selection(changed, deleted, files, base)
    counts = f"{len(changed)} changed, {len(deleted)} deleted, {len(labels)}/{total} test modules"
    print(f"[fast] base {base[:9]}, {why}: {counts}")
    every_test = len(labels) == total > 0
    if gate and not every_test and whole_asked:
        print("[fast] asked for: the whole suite and every quick gate run, so a pass records this tree")
        labels, every_test = whole_suite(files), True

    if gate is None:
        print("\n".join(labels))
        return 0

    tasks = quick_gates() if every_test else selected
    rest = tuple(task for task in tasks if task not in early)
    steps = [*first, *gates_and_tests(rest, labels, files, parallel, handing, base)]
    steps.append(step("patch integrity", ["git", "diff", "--check", base]))
    return judge(gate, steps, base, whole=every_test, fixed=fixed)


if __name__ == "__main__":
    sys.exit(main(usage()))
