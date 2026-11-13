"""Which CI legs a pull request runs, as mise's --affected reads each leg's sources, and that every
tracked file is read by one, so no change is left untested."""

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
    git("commit", "--quiet", "--no-verify", "--allow-empty", "-m", message)


def repository(root: Path) -> Path:
    root.mkdir()
    fixture_git(root)("init", "--quiet")
    return root


@tag("source")
class LegSelectionTests(SimpleTestCase):
    def select(self, *changed: str):
        """The legs a change to ``changed`` runs, on a fixture holding this checkout's task definitions."""

        from tools.pipeline import legs

        with tempfile.TemporaryDirectory() as folder:
            root = repository(Path(folder, "tree"))
            config(root)
            write(root, list(changed), "")
            commit(root, "base")
            base = fixture_git(root)("rev-parse", "HEAD")
            write(root, list(changed), "# changed\n")
            commit(root, "change")
            with patch.dict(os.environ, trusted_mise(root, Path(folder, "state"))):
                return legs.select(base, "HEAD", root)

    def runs(self, *changed: str) -> set[str]:
        return {leg for leg, runs in self.select(*changed).runs.items() if runs}

    def test_a_path_no_leg_reads_runs_every_leg_and_is_named(self):
        selection = self.select("unplaced/example.txt", "docs/Example.md")
        self.assertEqual(selection.unread, ("unplaced/example.txt",))
        self.assertTrue(all(selection.runs.values()), selection.runs)

    def test_a_docs_change_runs_lint_alone(self):
        self.assertEqual(self.runs("docs/Example.md"), {"lint"})

    def test_a_systemd_drop_in_runs_the_tests_that_read_units_and_no_browser(self):
        """The Go host checks hold every unit and drop-in; the app opens none of these."""
        self.assertEqual(
            self.runs("deploy/systemd/severino-hq-secrets.service.d/30-sandbox.conf"), {"lint", "test", "controller"}
        )

    def test_a_template_change_runs_the_browser_and_leaves_out_the_go_tests(self):
        self.assertEqual(self.runs("templates/base.html"), {"lint", "test", "browser"})

    def test_a_go_change_runs_the_go_tests_and_no_browser(self):
        self.assertEqual(self.runs("controller/host/verify.go"), {"lint", "test", "controller"})

    def test_a_change_to_the_workflow_runs_every_leg(self):
        self.assertEqual(self.runs(".github/workflows/ci.yml"), {"lint", "test", "controller", "browser"})


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
