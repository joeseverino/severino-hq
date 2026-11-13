"""The CI legs a pull request's change can affect, as mise's ``--affected`` reads each leg's sources
(docs/Development.md, What a pull request runs).

    python3 -m tools.pipeline.legs <base> [head]

prints each leg and whether it runs, and with ``GITHUB_OUTPUT`` set writes ``<output>=true|false``
there and the table to ``GITHUB_STEP_SUMMARY``. Stdlib only: the job that runs it sets up no
toolchain.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Each leg's CI output name, and the task whose sources select it.
LEGS = {"lint": "lint", "test": "test", "controller": "test:controller", "browser": "browser"}


@dataclass(frozen=True)
class Selection:
    """Which legs a change runs, and the changed paths no leg's sources name."""

    runs: dict[str, bool]
    unread: tuple[str, ...]


def changed(base: str, head: str, root: Path = ROOT) -> list[str]:
    """The paths the change touches, as mise's ``--affected`` diffs them: from the merge base."""

    listed = subprocess.run(
        ["git", "-C", str(root), "diff", "--name-only", "-z", "--no-renames", f"{base}...{head}", "--"],
        check=True,
        capture_output=True,
    ).stdout
    return [path for path in listed.decode().split("\0") if path]


def reasons(base: str, head: str, root: Path = ROOT) -> dict[str, list[dict[str, str]]]:
    """Each leg's task mise finds affected, with its reasons; a leg it leaves out is absent."""

    tasks: list[str] = []
    for task in LEGS.values():
        tasks += [":::", task] if tasks else [task]
    answer = subprocess.run(
        [
            "mise",
            "run",
            "--affected",
            "--affected-base",
            base,
            "--affected-head",
            head,
            "--affected-committed",
            "--affected-json",
            *tasks,
        ],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return {task["name"]: task["reasons"] for task in json.loads(answer)["tasks"]}


def select(base: str, head: str, root: Path = ROOT) -> Selection:
    paths = changed(base, head, root)
    found = reasons(base, head, root)
    read = {reason["path"] for each in found.values() for reason in each if "path" in reason}
    unread = tuple(path for path in paths if path not in read)
    return Selection({output: bool(unread) or task in found for output, task in LEGS.items()}, unread)


def report(selection: Selection) -> list[str]:
    """The selection as a Markdown table, then each path no leg reads."""

    lines = ["| Leg | Runs |", "| --- | --- |"]
    lines += [f"| {LEGS[output]} | {'yes' if runs else 'no'} |" for output, runs in selection.runs.items()]
    if selection.unread:
        lines += ["", "No leg's sources name these, so every leg runs:", ""]
        lines += [f"- `{path}`" for path in selection.unread]
    return lines


def main(argv: Sequence[str]) -> int:
    if not 1 <= len(argv) <= 2:
        print("usage: python3 -m tools.pipeline.legs <base> [head]", file=sys.stderr)
        return 2
    selection = select(argv[0], argv[1] if len(argv) == 2 else "HEAD")
    table = "\n".join(report(selection)) + "\n"
    print(table, end="")
    if output := os.environ.get("GITHUB_OUTPUT"):
        with Path(output).open("a", encoding="utf-8") as file:
            file.writelines(f"{name}={str(runs).lower()}\n" for name, runs in selection.runs.items())
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(summary).open("a", encoding="utf-8") as file:
            file.write(table)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
