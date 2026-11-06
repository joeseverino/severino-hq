"""Every task of this checkout as mise reads it, one list: mise.toml's and those under mise-tasks/, and the files each runs."""

import functools
import json
import re
import subprocess
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


@functools.cache
def mise_tasks() -> dict[str, dict[str, Any]]:
    """Each task by name, templates applied. A file task's ``file`` names its source, which holds its usage."""

    listed = subprocess.run(
        ["mise", "tasks", "ls", "--json", "--hidden", "--local"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return {task["name"]: task for task in json.loads(listed)}


# A task's own var in a script, as a template's run names it: mise's listing
# leaves it unrendered, and renders it from the extending task's vars.
_VAR = re.compile(r"\{\{\s*vars\.(\w+)\s*\}\}")


def body(task: dict[str, Any]) -> str:
    """What the task runs: a file task's whole source, a TOML task's run scripts
    with each ``{{ vars.NAME }}`` its own vars give rendered.

    A step that runs another task is listed as a table, {"task": name, ...}, and is not a script."""

    if task["file"]:
        return Path(task["file"]).read_text()
    scripts = "\n".join(each for each in task["run"] if isinstance(each, str))
    if not _VAR.search(scripts):
        return scripts
    declared = tomllib.loads(Path(task["source"]).read_text())[task["name"]].get("vars", {})
    return _VAR.sub(lambda var: declared[var[1]], scripts)


def runs(task: dict[str, Any]) -> list[Path]:
    """The files of this checkout a task's run names: a script by its path, or
    a module the dotted way (``python -m a.b``, ``manage.py test a.b.Case``).

    A file task is its own script, so it names none."""

    if task["file"]:
        return []
    run = body(task)
    scripts = [ROOT / path for path in re.findall(r"[\w/.-]+\.(?:sh|py)\b", run) if (ROOT / path).is_file()]
    for dotted in re.findall(r"\b[a-z_]+(?:\.\w+)+", run):
        parts = dotted.split(".")
        found = next(
            (
                ROOT.joinpath(*parts[:end]).with_suffix(".py")
                for end in range(len(parts), 0, -1)
                if ROOT.joinpath(*parts[:end]).with_suffix(".py").is_file()
            ),
            None,
        )
        if found and found not in scripts:
            scripts.append(found)
    return scripts


@functools.cache
def run_files() -> frozenset[str]:
    """Every file of this checkout a task's run names, from its root."""

    return frozenset(path.relative_to(ROOT).as_posix() for task in mise_tasks().values() for path in runs(task))


@functools.cache
def locks(name: str) -> list[str]:
    """The locks the task takes, as mise resolves them, templates applied.

    Only `mise tasks info` reports a lock (its task list leaves it out), one task a
    call, so each task is asked once, when a caller first needs it."""

    info = subprocess.run(
        ["mise", "tasks", "info", name, "--json"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout
    # Absent, false or true (the task's own name in its repository), or a list of names.
    lock = json.loads(info).get("lock")
    return lock if isinstance(lock, list) else []


# The machine's heavy turn, the lock tools/gate/turn.py names; hq's tests read
# this module and may not import tools/.
HEAVY_LOCK = "heavy"
# The line a file task sources to run itself again in the turn when its run is heavy.
SOURCES_THE_TURN = re.compile(r'(?m)^\s*\. .*tools/lib/heavy-turn\.sh"?$')


def takes_the_turn(task: dict[str, Any]) -> bool:
    """Whether the task holds the machine's heavy turn: it takes the lock, or its
    file sources the line that runs it again in the turn when its run is heavy."""

    return HEAVY_LOCK in locks(task["name"]) or bool(task["file"] and SOURCES_THE_TURN.search(body(task)))
