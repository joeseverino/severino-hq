"""Every task of this checkout as mise reads it, one list: mise.toml's and those of every file its task_config includes."""

import functools
import json
import subprocess
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


def body(task: dict[str, Any]) -> str:
    """What the task runs: a file task's whole source, a TOML task's run scripts.

    A step that runs another task is listed as a table, {"task": name, ...}, and is not a script."""

    if task["file"]:
        return Path(task["file"]).read_text()
    return "\n".join(each for each in task["run"] if isinstance(each, str))
