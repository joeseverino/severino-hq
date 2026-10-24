"""Every task of this checkout as mise reads it, one list: mise.toml's and those under mise-tasks/."""

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
