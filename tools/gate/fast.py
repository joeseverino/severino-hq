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
from dataclasses import replace
from pathlib import Path
from typing import Any

from hq.platform.core.tests.task import Usage, changed_paths, usage
from tests.mise_tasks import HEAVY_LOCK, body, locks, mise_tasks
from tools.gate import chain, testmap
from tools.gate.chain import fast_base, maps, record_pass
from tools.gate.selection import (
    affected,
    group_tasks,
    group_tests,
    listed_change,
    python_files,
    selection,
    source_first,
    test_labels,
    walk_for_tree,
    whole_suite,
    with_tree,
)
from tools.gate.turn import IN_TURN
from tools.gate.verdict import Gate, Step, run

# The tag of the quick gates: each declares the files it reads as its sources,
# and mise's --affected picks those a change touched. Those selected run side
# by side, in one mise run, before any test starts; each Django gate opens a
# database of its own (the django task template).
QUICK = "fast"
# The tag of a gate fast runs beside its tests in the heavy turn rather than
# among the quick gates (docs/Development.md, Heavy gates take turns), as it
# does a gate that takes the turn's lock itself.
HEAVY = "heavy"
# How long fast's tests and the gates beside them may hold the turn, as long as
# fast:tests may hold it on its own.
TURN_BOUND = "30m"
# The shell gate also reads every file its lister names, which no glob can, so
# a change selects it when the lister names a path it touched: as this tree has
# it, or as the base had it once deleted.
SHELL = "lint:shell"
SHELL_SOURCES = Path(__file__).resolve().parent / "shell-sources.sh"
# What extensions render through. A change here reaches their pages, which no
# host test imports, so with an extension set named their suites run too.
SHARED_UI = (
    "templates/",
    "static/",
    "hq_sdk/",
    "hq/platform/application/tables.py",
    "hq/platform/application/pages.py",
    "hq/platform/application/ui.py",
    "hq/platform/application/moments.py",
)
# Said when such a change finds no extension set to run.
SKIPPED = (
    "shared UI changed, but mise.local.toml names no extension set, so the extensions' suites were skipped:"
    " `mise run setup:extensions <checkout>...` names one"
)


# Said when a quick gate failed, which ends the run before any test.
QUICK_FAILED = "[fast] a quick gate failed, so no test and no gate in the heavy turn started"


def touches_shared_ui(paths: list[str]) -> bool:
    return any(path.startswith(SHARED_UI) or "/templatetags/" in path for path in paths)


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
# A file task declares its usage in its own header, which mise's listing leaves out.
FILE_USAGE = re.compile(r"^#USAGE (.*)$", re.MULTILINE)


def fixers(tasks: tuple[str, ...]) -> tuple[str, ...]:
    """Those of ``tasks`` whose usage declares the fix flag."""

    quick = quick_tasks()
    return tuple(task for task in tasks if task in quick and FIX.search(usage_of(quick[task])))


def usage_of(task: dict[str, Any]) -> str:
    """A task's usage spec, from its listing or a file task's header."""

    if task.get("file"):
        return "\n".join(FILE_USAGE.findall(Path(task["file"]).read_text(encoding="utf-8")))
    return task.get("usage") or ""


def fixed_first(
    gate: Gate, tasks: tuple[str, ...], base: str, paths: tuple[list[str], list[str]]
) -> tuple[list[str], list[str]]:
    """The changed and deleted paths once the fix modes of ``tasks`` but the
    recordings' have run, and the gate set to judge the tree they left, saying
    what they rewrote."""

    fixed = fix(tuple(task for task in tasks if task not in recordings()))
    if not fixed:
        return paths
    print(f"[fast] the quick gates' fixes rewrote: {' '.join(fixed)}", flush=True)
    gate.restart()
    return changed_paths(base, chain.ROOT)


def fix(tasks: tuple[str, ...]) -> list[str]:
    """Run the fix mode of each of ``tasks`` that has one, in one run of mise,
    and return the paths it rewrote, so the gates then judge fixed files and
    formatting never fails a run. What a fixer still finds, its gate reports."""

    fixing = fixers(tasks)
    if not fixing:
        return []
    before = dirty()
    words = [word for task in fixing for word in (":::", task, "--fix")][1:]
    subprocess.run(["mise", "run", "--continue-on-error", *words], cwd=chain.ROOT, check=False, capture_output=True)
    after = dirty()
    # A path in one alone was rewritten too: one the fix left as HEAD has it is no longer dirty.
    return sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))


# A quick gate tagged this is a recording: it runs a set of test modules and
# compares what they receive with a file (lint:drift-api-examples). It reads
# Python only through those tests, so a change to Python alone selects it
# where it selects one of them. Its fix is the same run written over the file,
# so it runs only once the check has failed, and the gates and tests the
# rewritten files select then judge what it left.
RECORDING = "recording"
# A run of the test labels named after it, and a task a run starts.
TEST_RUN = re.compile(r"\bmanage\.py test((?:\s+[\w.]+)+)")
RUNS_TASK = re.compile(r"\bmise run ([\w:-]+)")


def recordings() -> tuple[str, ...]:
    """Every quick gate tagged RECORDING."""

    return tuple(name for name, task in quick_tasks().items() if RECORDING in (task.get("tags") or ()))


def recorded_labels(name: str) -> list[str]:
    """The test labels a recording runs, in its own run or one a task its run starts."""

    tasks = mise_tasks()
    own = body(tasks[name])
    scripts = [own, *(body(tasks[started]) for started in RUNS_TASK.findall(own) if started in tasks)]
    return [label for script in scripts for run in TEST_RUN.findall(script) for label in run.split()]


def unreached_recordings(tasks: tuple[str, ...], paths: list[str], labels: list[str]) -> list[str]:
    """The recordings among ``tasks`` that a change to Python files alone
    (``paths``) selected, though the tests it selects (``labels``) hold none
    of those they record. A recording that names no test is always kept."""

    if not paths or not all(path.endswith(".py") for path in paths):
        return []
    return [
        name
        for name in tasks
        if name in recordings()
        and (recorded := recorded_labels(name))
        and not any(label == held or label.startswith(f"{held}.") for label in labels for held in recorded)
    ]


def fixed_after(gate: Gate, steps: list[Step], files: list[Path], parallel: str) -> list[Step]:
    """``steps``, and when a recording failed in one, its fix and the run
    again of the gates and tests the files it rewrote select, the fixed
    recordings among them. A step's failure stands unless it failed on the
    fixed recordings alone, and the run judges the tree the fix left."""

    failed = [task for each in steps for task in each.tasks if task in recordings()]
    fixed = fix(tuple(failed)) if failed else []
    if not fixed:
        return steps
    print(f"[fast] {' '.join(failed)} failed, so its fix ran and rewrote: {' '.join(fixed)}", flush=True)
    gate.restart()
    # HEAD as the base: what the fix rewrote is in the checkout, beside any work not yet committed.
    tasks = (*failed, *(task for task in selected_tasks(fixed, "HEAD") if task not in failed))
    labels = with_tree(test_labels(files, *affected(fixed, [], files))[0], fixed)
    kept = [
        replace(each, ok=True, tasks=[])
        if set(each.tasks) & set(failed) and set(each.tasks) <= set(failed) and not each.tests and not each.unimported
        else each
        for each in steps
    ]
    return [*kept, *gates_and_tests(tasks, labels, files, parallel, again=", again on the fix")]


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
    the turn's lock themselves. Every task that takes the lock declares a
    timeout (HeavyTaskTests), so only those are asked for their locks, which
    mise reports one task a call."""

    listed = mise_tasks()
    return tuple(
        task
        for task in tasks
        if HEAVY in (listed[task].get("tags") or ()) or (listed[task].get("timeout") and HEAVY_LOCK in locks(task))
    )


def mise_run(words: list[str], parallel: str) -> list[str]:
    """One run of mise in fast's config environment, which runs every task to
    its end when one fails, so one run names every failure it reaches."""

    jobs = ["--jobs", "1"] if parallel == "1" else []
    return ["mise", "--env", ENVIRONMENT, "run", "--continue-on-error", *jobs, *words]


def quick_command(tasks: tuple[str, ...], parallel: str) -> list[str]:
    """The run of the quick gates, side by side."""

    return mise_run([word for task in tasks for word in (task, ":::")][:-1], parallel)


def run_command(
    tasks: tuple[str, ...], source: list[str], labels: list[str], parallel: str, *, record: bool = False
) -> list[str]:
    """The run of the tests beside the gates that run in the heavy turn
    (``tasks``), in one run of mise. With such a gate it runs inside the turn,
    taken once: mise hands the lock down to every task under it, so each gate
    that takes the lock itself and the tests' task go ahead at once and side
    by side, never queued on the turn their own run holds. Without one the
    tests' task takes the turn itself when its run is heavy. A test both
    selected and tagged tree runs once, since Django's runner drops a repeated
    test. A recording run (``record``) measures the tests for the map of the
    tests that ran each file (``record_map``).

    A task's own output setting outranks the config's but not ``--output``, so
    the summary is the environment's: the tests' task prints as it runs, beside
    each gate's line as it ends."""

    gates = [word for task in tasks for word in (task, ":::")]
    tests = [TESTS, "--parallel", parallel, *(["--map", str(chain.ROOT / testmap.DATA)] if record else []), *labels]
    command = ["env", f"{SOURCE}={' '.join(source)}", *mise_run([*gates, *tests], parallel)]
    return ["env", f"HQ_TURN_BOUND={TURN_BOUND}", *IN_TURN, *command] if tasks else command


def quick_stage(tasks: tuple[str, ...], parallel: str, again: str = "") -> list[Step]:
    """The run of those of ``tasks`` that are quick, side by side, or none
    when none is; ``again`` names a rerun in its label."""

    quick = tuple(task for task in tasks if task not in heavy_gates(tasks))
    if not quick:
        return []
    print(f"[fast] the quick gates that read what changed: {' '.join(quick)}")
    return [step(f"quick gates{again}", quick_command(quick, parallel))]


def gates_and_tests(
    tasks: tuple[str, ...],
    labels: list[str],
    files: list[Path],
    parallel: str,
    *,
    record: bool = False,
    again: str = "",
) -> list[Step]:
    """The quick gates among ``tasks``, then, once they pass, the tests beside
    the rest, which run in the heavy turn; ``again`` names a rerun in its labels."""

    steps = quick_stage(tasks, parallel, again)
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
    return [*steps, step(f"{label}{again}", run_command(slow, source, rest, parallel, record=record))]


def step(label: str, cmd: list[str]) -> Step:
    print(f"[fast] {label}", flush=True)
    return run(label, cmd)


def extension_suites(parallel: str) -> list[Step]:
    """The extensions' suites, composed, when the shared UI changed: run with a set named, else said skipped."""

    if not os.environ.get("HQ_LOCAL_PLUGINS"):
        print(f"[fast] {SKIPPED}", flush=True)
        return []
    # test:composed hands what follows to manage.py test.
    command = ["mise", "run", "test:composed", "--", "--installed-plugins", "--parallel", parallel]
    return [step("extension suites, composed (shared UI changed)", command)]


def record_map(tree: str | None) -> None:
    """Keep the map a whole run measured as ``tree``'s, when the run passed on
    a tree that held still, and say what it holds. The data goes once read:
    the map holds what it says."""

    data = chain.ROOT / testmap.DATA
    if not tree or not any(data.parent.glob(f"{data.name}.*")):
        return
    found = testmap.read(data, chain.ROOT)
    data.unlink(missing_ok=True)
    testmap.save(maps(), tree, found)
    held = f"{len(found.files)} files, {len(found.ran)} test modules"
    print(f"[fast] the map of the tests that ran each file is kept for tree {tree[:9]}: {held}", flush=True)


def judge(gate: Gate, steps: list[Step], base: str, *, whole: bool) -> int:
    """Close the run on ``steps``: a pass records its tree, and a whole run's its map too."""

    passed = all(s.ok for s in steps)
    if passed:
        judged = gate.judged()
        record_pass(judged, base, whole=whole)
        if whole:
            record_map(judged)
    gate.close(steps, " (full gate before push: `mise run check`)")
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
    # a finding fails the run in the seconds they take. A recording waits on
    # the selection, which says whether it runs.
    early: tuple[str, ...] = ()
    first: list[Step] = []
    if gate:
        changed, deleted = fixed_first(gate, selected, base, (changed, deleted))
        early = tuple(task for task in (quick_gates() if whole_asked else selected) if task not in recordings())
        early = tuple(task for task in early if task not in heavy_gates(early))
        first = quick_stage(early, parallel)
        if not all(each.ok for each in first):
            print(QUICK_FAILED, flush=True)
            return judge(gate, [*first, step("patch integrity", ["git", "diff", "--check", base])], base, whole=False)
    walking = walk_for_tree() if changed or deleted else None
    files = python_files()
    labels, total, (reached, touched) = selection(changed, deleted, files)
    counts = f"{len(changed)} changed, {len(deleted)} deleted, {len(labels)}/{total} test modules"
    print(f"[fast] base {base[:9]}, {why}: {counts}")
    every_test = len(labels) == total > 0
    if gate and not every_test and whole_asked:
        print("[fast] asked for: the whole suite and every quick gate run, so a pass records this tree and its map")
        labels, every_test = whole_suite(files), True

    paths = [*changed, *deleted]
    # Read before the tests tagged tree join: their selection says nothing of the change's reach.
    unreached = unreached_recordings(selected, paths, labels) if gate and not every_test else []
    labels = with_tree(labels, paths, listed_change(base, deleted) if paths else [], walking)
    if gate is None:
        print("\n".join(labels))
        return 0

    if unreached:
        print(
            f"[fast] not run, as the change is Python alone and selects none of the tests each records: {' '.join(unreached)}"
        )
    whole = (*quick_gates(), *group_tests().values())
    kept = tuple(task for task in selected if task not in unreached)
    tasks = whole if every_test else (*kept, *group_tasks(files, reached, touched))
    whole_run = every_test and set(tasks) == set(whole)
    rest = tuple(task for task in tasks if task not in early)
    steps = [*first, *gates_and_tests(rest, labels, files, parallel, record=whole_run)]
    steps = fixed_after(gate, steps, files, parallel)
    if touches_shared_ui([*changed, *deleted]):
        steps.extend(extension_suites(parallel))
    steps.append(step("patch integrity", ["git", "diff", "--check", base]))
    return judge(gate, steps, base, whole=whole_run)


if __name__ == "__main__":
    sys.exit(main(usage()))
