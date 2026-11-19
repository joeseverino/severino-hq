"""Every tracked file is read by a CI leg, as mise's --affected reads each leg's sources, so no change
is left untested."""

import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase, tag

from hq.platform.core.tests.task import names, trusted_mise
from tools.gate.tests.base import fixture_git

ROOT = Path(__file__).resolve().parents[3]
# What mise reads as it loads this checkout's tasks, which a fixture copies whole: the Go pin is read
# from go.mod. Every other path is an empty file, since mise matches a change by its name alone.
CONFIG = ("mise.toml", "mise.lock", "tools/pipeline/toolchain.env", "controller/go.mod")


def config(root: Path) -> None:
    """This checkout's task definitions and what mise reads with them, copied into ``root``."""

    shutil.copytree(ROOT / "mise-tasks", root / "mise-tasks")
    for path in CONFIG:
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / path, root / path)


def write(root: Path, paths: list[str], text: str) -> None:
    for path in paths:
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        with (root / path).open("a", encoding="utf-8") as file:
            file.write(text)


def commit(root: Path, message: str) -> None:
    git = fixture_git(root)
    git("add", "-A")
    git("commit", "--quiet", "--allow-empty", "-m", message)


def repository(root: Path) -> Path:
    root.mkdir()
    fixture_git(root)("init", "--quiet")
    return root


@tag("source", "tree")
class EveryFileHasALegTests(SimpleTestCase):
    def test_every_tracked_file_is_read_by_a_leg(self):
        """A path no leg names would run every leg on each change to it: it is placed in the sources of
        each leg that reads it (docs/Development.md, What a pull request runs, names where)."""

        from tools.pipeline import legs

        tracked = names("ls-files", root=ROOT)
        with tempfile.TemporaryDirectory() as folder:
            # From an empty commit, so the change is every tracked path, the task definitions included.
            root = repository(Path(folder, "tree"))
            commit(root, "base")
            base = fixture_git(root)("rev-parse", "HEAD")
            config(root)
            write(root, [path for path in tracked if not (root / path).exists()], "")
            commit(root, "every file")
            with patch.dict(os.environ, trusted_mise(root, Path(folder, "state"))):
                selection = legs.select(base, "HEAD", root)
        self.assertGreater(len(tracked), 100)
        self.assertEqual(selection.unread, (), "name each in the sources of every leg that reads it")
