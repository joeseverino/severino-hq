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
