"""Each lint:drift-* gate's sources match every file of this checkout its run reads, so no change to
one of them can leave the gate unrun: mise's --affected, which fast selects by, reads only sources.

Each gate runs as mise runs it with reads/ on PYTHONPATH, whose sitecustomize logs every file
each Python process the gate starts opens to read; the drift gates are all Python."""

import os
import subprocess
import tempfile
from collections.abc import Iterable, Sequence
from pathlib import Path, PurePath
from typing import Any, override

from django.test import SimpleTestCase, tag

from tests.mise_tasks import ROOT, mise_tasks

DRIFT = "lint:drift-"
READS = Path(__file__).resolve().parent / "reads"


def matched(path: PurePath, sources: Sequence[str]) -> bool:
    """Whether mise counts ``path`` among ``sources``: the last entry it matches decides, one
    starting with ``!`` taking it out. A glob is gitignore's, as globset reads it: ``*`` stays within
    a segment and ``**`` spans any number, a trailing one reaching the files beneath, which is
    what PurePath.full_match answers."""

    counted = False
    for entry in sources:
        if path.full_match(entry.removeprefix("!")):
            counted = not entry.startswith("!")
    return counted


def inputs(opened: Iterable[str], root: Path) -> set[Path]:
    """The files of the checkout at ``root`` among the paths ``opened``: what git would track,
    so neither the virtual environment, a gate's own scratch, nor anything outside the checkout."""

    found = {
        path
        for path in map(Path, opened)
        if path.is_relative_to(root) and ".git" not in path.relative_to(root).parts and path.is_file()
    }
    named = "\0".join(str(path.relative_to(root)) for path in found)
    ignored = subprocess.run(
        ["git", "check-ignore", "--stdin", "-z"], cwd=root, input=named, capture_output=True, text=True, check=False
    ).stdout
    return found - {root / name for name in ignored.split("\0") if name}


def misses(task: dict[str, Any], read: Iterable[Path]) -> list[str]:
    """Each file of ``read`` the task's sources do not match, as its sources name it: from its dir."""

    return sorted(
        rel
        for rel in (os.path.relpath(path, task["dir"]) for path in read)
        if not matched(PurePath(rel), task["sources"])
    )


def reads(name: str) -> tuple[subprocess.CompletedProcess[str], set[Path]]:
    """Run gate ``name`` as mise does, and the files of this checkout it read."""

    with tempfile.TemporaryDirectory() as folder:
        log = Path(folder, "reads")
        env = {key: value for key, value in os.environ.items() if key != "VIRTUAL_ENV"} | {
            "HQ_READS_LOG": str(log),
            "PYTHONPATH": os.pathsep.join(filter(None, (str(READS), os.environ.get("PYTHONPATH")))),
        }
        done = subprocess.run(["mise", "run", name], cwd=ROOT, env=env, capture_output=True, text=True, check=False)
        opened = log.read_text().splitlines() if log.exists() else []
    return done, inputs(opened, ROOT)


@tag("source")
class DriftSourcesTests(SimpleTestCase):
    tasks: dict[str, dict[str, Any]]
    runs: dict[str, tuple[subprocess.CompletedProcess[str], set[Path]]]

    @classmethod
    @override
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.tasks = {name: task for name, task in mise_tasks().items() if name.startswith(DRIFT)}
        cls.runs = {name: reads(name) for name in cls.tasks}

    def test_each_drift_gate_names_every_file_it_reads_in_its_sources(self) -> None:
        self.assertTrue(self.tasks, "no task is named lint:drift-*, so this holds nothing")
        for name, task in self.tasks.items():
            with self.subTest(name):
                done, read = self.runs[name]
                self.assertEqual(done.returncode, 0, f"{name} fails, so what it reads is not all seen:\n{done.stderr}")
                self.assertTrue(read, f"{name} read no file of this checkout, so its run was never seen")
                self.assertFalse(
                    [entry for entry in task["sources"] if "{" in entry],
                    f"{name}: a brace alternative is mise's alone, so name each path in a source of its own",
                )
                missed = misses(task, read)
                self.assertFalse(
                    missed,
                    f"{name} reads {', '.join(missed)}, which its sources in mise-tasks/lint.toml do not match,"
                    " so a change there would not run it: add each to them",
                )

    def test_a_file_read_that_its_sources_leave_out_is_named(self) -> None:
        """A source taken out of a gate's list is the file the guard names, and only it."""

        name = "lint:drift-connections-reference"
        held = "docs/Connections.md"
        task = self.tasks[name]
        _, read = self.runs[name]
        self.assertIn(ROOT / held, read, f"{name} no longer reads {held}; plant another of its sources")
        others = [entry for entry in task["sources"] if entry != held]
        listed = set(misses({**task, "sources": [*others, held]}, read))
        self.assertNotIn(held, listed)
        for sources in (others, [*others, held, f"!{held}"]):
            with self.subTest(sources=sources[len(others) :]):
                self.assertEqual(set(misses({**task, "sources": sources}, read)) - listed, {held})
        self.assertNotIn(held, misses({**task, "sources": [*others, f"!{held}", held]}, read))
