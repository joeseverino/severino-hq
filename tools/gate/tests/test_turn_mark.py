"""A task that waits for the machine's heavy turn without holding its lock says
so in its header, where `mise tasks ls --json` lists it without running
anything: the agent hooks count it beside the tasks that take the lock (docs/Development.md, Heavy gates take turns)."""

import ast
import re
from pathlib import Path
from typing import Any

from django.test import tag

from tests.mise_tasks import HEAVY_LOCK, body, locks, mise_tasks, runs
from tools.gate.tests.base import GateTestCase
from tools.gate.tests.test_turn import code
from tools.tests.test_task_references import MISE_RUN, MISE_RUN_LIST, a_task, tasks_named

# The mark, an env entry of the task's own, as mise lists it.
MARK = "SEVERINO_WAITS_FOR_HEAVY_TURN=1"
# The task a run that weighed itself runs its heavy part through.
TURN = "turn"


def named_files(script: str) -> list[Path]:
    """The files of this checkout a script runs, read as a TOML task's run is read."""

    return runs({"name": "", "file": None, "run": [script]})


def run_by_python(path: Path) -> list[str]:
    """The tasks a Python module runs: a `mise run` command it builds as a list,
    and the turn for each command it hands to ``in_turn``."""

    source = path.read_text()
    called = [
        name for match in MISE_RUN_LIST.finditer(source) for name in tasks_named(re.findall(r'"([^"]*)"', match[1]))
    ]
    in_turn = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", None)) == "in_turn"
    ]
    return [*called, *([TURN] if in_turn else [])]


def run_by(script: str, seen: set[Path]) -> list[str]:
    """The tasks a script runs with `mise run`, itself or through a script or module it runs, comments aside."""

    script = code(script)
    called = [name for match in MISE_RUN.finditer(script) for name in tasks_named(match[1].split())]
    for path in named_files(script):
        if path in seen:
            continue
        seen.add(path)
        if path.suffix == ".py":
            called += run_by_python(path)
        elif path.suffix == ".sh":
            called += run_by(path.read_text(), seen)
    return called


def waiting(tasks: dict[str, dict[str, Any]]) -> set[str]:
    """Each task that holds no heavy lock and runs the turn, a task that holds
    it, or one of these, from its script: mise reports no edge for a `mise run`
    in a script, so the reader has only the mark."""

    holding = {name for name in tasks if HEAVY_LOCK in locks(name)}
    calls = {}
    for name, task in tasks.items():
        named = run_by(body(task), set())
        calls[name] = {other for other in tasks if other != name and any(a_task(n, {other}) for n in named)}
    found: set[str] = set()
    while grown := {
        name for name, called in calls.items() if name not in holding | found and called & (holding | found)
    }:
        found |= grown
    return found


@tag("source")
class TurnMarkTests(GateTestCase):
    def test_every_task_that_waits_for_the_turn_without_its_lock_carries_the_mark_and_no_other_does(self):
        """The agent hooks give such a gate the longest wait and count it among a brief's heavy gates,
        from the listing alone."""
        tasks = mise_tasks()
        derived = waiting(tasks)
        self.assertIn(TURN, {name for name in tasks if HEAVY_LOCK in locks(name)})
        self.assertGreater(len(derived), 5)
        marked = {name for name, task in tasks.items() if MARK in task["env"]}
        self.assertEqual(
            {"unmarked": sorted(derived - marked), "marked without waiting": sorted(marked - derived)},
            {"unmarked": [], "marked without waiting": []},
            f"a task that waits for the heavy turn without its lock declares env {MARK} in its header",
        )
