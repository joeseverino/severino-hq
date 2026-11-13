"""Which quick gates fast runs beside its tests for a change, as mise's --affected reads each one's sources,
and the shell gate's lister it asks besides."""

import os
import shutil
import subprocess
import tempfile
import tomllib
from pathlib import Path, PurePath
from unittest.mock import patch

from django.test import tag

from hq.platform.core.tests.task import names, trusted_mise
from tests.mise_tasks import mise_tasks
from tools.gate import fast as fast_gate
from tools.gate.tests.base import GateTestCase, fixture_git
from tools.structural.classify import TEST

ROOT = Path(__file__).resolve().parents[3]
# The shell gate's own lister, which fast asks rather than restating.
SOURCES = ROOT / "tools" / "gate" / "shell-sources.sh"
# What every quick gate reads: the tool pins and the task definitions, and the
# dependencies of each that runs Python, so each one's sources name them all
# and a change to one runs every quick gate that reads it.
EVERY = ("mise.toml", "mise.lock", "mise-tasks/*.toml", "tools/pipeline/toolchain.env")
PYTHON = ("pyproject.toml", "uv.lock")
# The Go tests, the quick gate that reads no Python dependency.
GO = "test:controller"
# Each lint gate fast never selects, and why: it reaches the network, so it is
# left to `mise run check`, or fast runs its check another way. A lint task
# neither tagged fast nor named here is one fast would skip unseen.
EXEMPT = {
    "lint:audit": "queries the advisory services (uv audit, osv-scanner); left to `mise run check`",
    "lint:govulncheck": "queries vuln.go.dev for the advisories the controller reaches; left to `mise run check`",
    "lint:codeql-bundle": "fetches the pinned CodeQL action's defaults from GitHub; left to `mise run check`",
    "lint:scorecard": "its Vulnerabilities check queries OSV over an export of the tree; left to `mise run check`",
    "lint:units": "systemd-analyze is Linux's, so CI's lint job scores the units",
    "lint:patch": "fast's own patch integrity step runs `git diff --check` over the change",
    "lint:source": "fast runs the source-tagged tests among its labels, since a labeled run leaves no tag out",
    "lint:shell:suites": "runs within lint:shell, a quick gate",
    "lint:shell:suite": "runs within lint:shell, a quick gate",
}


# What mise reads as it loads this checkout's tasks, besides mise-tasks/, which a
# fixture repository copies: the Go pin is read from go.mod.
CONFIG = ("mise.toml", "mise.lock", "tools/pipeline/toolchain.env", "controller/go.mod")


def selected(*changed: str, deleted: tuple[str, ...] = ()) -> set[str]:
    """The quick gates fast selects for a change to ``changed`` that removes ``deleted``, made in a fixture
    repository holding this checkout's task definitions and, at its base, each of those paths."""

    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder, "tree")
        shutil.copytree(ROOT / "mise-tasks", root / "mise-tasks")
        for path in (*CONFIG, *changed, *deleted):
            if (ROOT / path).is_file() and not (root / path).exists():
                (root / path).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / path, root / path)
        git = fixture_git(root)
        git("init", "--quiet")
        git("add", "-A")
        git("commit", "--quiet", "--no-verify", "--allow-empty", "-m", "base")
        for path in changed:
            (root / path).parent.mkdir(parents=True, exist_ok=True)
            with (root / path).open("a", encoding="utf-8") as file:
                file.write("# changed\n")
        for path in deleted:
            (root / path).unlink()
        with (
            patch.object(fast_gate, "ROOT", root),
            patch.dict(os.environ, trusted_mise(root, Path(folder, "state"))),
        ):
            return set(fast_gate.selected_tasks([*changed, *deleted], git("rev-parse", "HEAD")))


def exclusions(task: str) -> list[str]:
    """The paths a gate's sources take out, each pattern without its `!`."""

    return [pattern[1:] for pattern in mise_tasks()[task]["sources"] if pattern.startswith("!")]


@tag("source", "tree")
class FastGateTaskSelectionTests(GateTestCase):
    def test_every_quick_gate_reads_files_of_this_tree_and_what_every_gate_reads_and_is_never_skipped_as_fresh(self):
        files = [PurePath(path) for path in names("ls-files", root=ROOT)]
        tasks = mise_tasks()
        for task in fast_gate.quick_gates():
            with self.subTest(task=task):
                sources = tasks[task]["sources"]
                # Sources and no outputs would have mise skip a direct run of the gate whose sources it saw pass.
                self.assertEqual(tasks[task]["outputs"], [], f"{task}: declare outputs = [], so mise never skips it")
                every = {*EVERY, *(PYTHON if task != GO else ())}
                self.assertLessEqual(every, set(sources), f"{task}: its sources name what every gate reads")
                for pattern in (p for p in sources if not p.startswith("!")):
                    self.assertTrue(any(f.full_match(pattern) for f in files), f"{task}: {pattern} names no file")

    def test_the_quick_gates_are_tasks_and_every_drift_gate_is_one(self):
        drift = {name for name in mise_tasks() if name.startswith("lint:drift-")}
        self.assertTrue(drift, "no task is named lint:drift-*, so this holds nothing")
        self.assertLessEqual(drift, set(fast_gate.quick_gates()), "a drift gate is a quick gate: tag it fast")

    def test_a_gates_exclusions_are_those_its_tool_declares(self):
        """complexipy leaves out what complexipy.toml's exclude names; the structural bar drops test code."""

        declared = tomllib.loads((ROOT / "complexipy.toml").read_text(encoding="utf-8"))["exclude"]
        self.assertEqual(sorted(exclusions("lint:complexity")), sorted(declared))
        excluded = exclusions("lint:structural")
        disagree = [
            path
            for path in names("ls-files", root=ROOT)
            if any(PurePath(path).full_match(pattern) for pattern in excluded) != bool(TEST.search(path))
        ]
        self.assertTrue(excluded, "lint:structural takes nothing out, so this holds nothing")
        self.assertFalse(disagree, "lint:structural's ! sources take out what classify.TEST names, and only that")

    def test_a_docs_change_runs_only_the_prose_gate_and_the_gate_that_derives_it(self):
        self.assertEqual(selected("docs/Example.md"), {"lint:prose"})
        self.assertEqual(selected("docs/Connections.md"), {"lint:prose", "lint:drift-connections-reference"})

    def test_a_change_to_a_file_the_shell_gate_reads_runs_it_and_no_other_change_does(self):
        """A workflow's run blocks are read by the shell gate's contracts, as its scripts and suites are."""
        for changed in (
            ".github/workflows/deploy.yml",
            ".github/actions/admit-plugin/action.yml",
            "tools/lib/heavy-turn.sh",
            "tests/shell/test-heavy.sh",
            "mise-tasks/lint/shell/_default",
        ):
            with self.subTest(changed=changed):
                self.assertIn("lint:shell", selected(changed))
        for changed in ("docs/Example.md", "tools/gate/fast.py", "controller/host/verify.go"):
            with self.subTest(changed=changed):
                self.assertNotIn("lint:shell", selected(changed))

    def test_a_change_to_a_file_a_drift_gate_derives_from_runs_that_gate(self):
        """Each file the generator reads besides Python, which a change can move without regenerating."""
        for changed, gate in (
            ("hq/domains/control_plane/bridge_base.json", "lint:drift-bridge-contract"),
            ("hq/platform/api/openapi-examples.json", "lint:drift-api-document"),
            ("templates/base.html", "lint:drift-sdk-contract"),
            ("static/css/app/base.css", "lint:drift-sdk-contract"),
            ("static/js/app.js", "lint:drift-sdk-contract"),
            ("tools/lint/ast-grep/rules/hand-drawn-table.yml", "lint:drift-sdk-contract"),
        ):
            with self.subTest(changed=changed):
                self.assertIn(gate, selected(changed))

    def test_a_python_change_runs_the_python_gates_and_not_the_stylesheet_ones(self):
        picked = selected("hq/domains/docs_index/importer.py")
        self.assertLessEqual({"lint:ruff", "lint:mypy", "test:migrations", "lint:drift-api-document"}, picked)
        self.assertNotIn("lint:css", picked)
        self.assertNotIn("lint:drift-font", picked)

    def test_a_change_to_a_linters_suppressions_runs_that_linter(self):
        self.assertIn("lint:css", selected("tools/lint/stylelint-suppressions.json"))
        self.assertIn("lint:js", selected("tools/lint/eslint-suppressions.json"))

    def test_a_deleted_file_is_a_change_to_the_gates_that_read_it(self):
        self.assertLessEqual({"lint:ruff", "lint:mypy"}, selected(deleted=("hq/domains/docs_index/importer.py",)))

    def test_a_change_to_tests_alone_leaves_out_the_structural_bar_which_never_reads_them(self):
        self.assertNotIn("lint:structural", selected("hq/platform/core/tests/test_example.py"))
        self.assertIn("lint:structural", selected("hq/platform/core/example.py"))
        self.assertIn(
            "lint:structural", selected("hq/platform/core/tests/test_example.py", "hq/platform/core/example.py")
        )

    def test_a_change_to_tests_alone_runs_the_complexity_bound_and_a_migration_alone_does_not(self):
        self.assertIn("lint:complexity", selected("hq/platform/core/tests/test_example.py"))
        self.assertIn("lint:complexity", selected("tests/example/consumer.py"))
        self.assertNotIn("lint:complexity", selected("hq/domains/example/migrations/0001_initial.py"))

    def test_a_change_to_what_every_gate_reads_runs_them_all(self):
        for changed in ("mise-tasks/lint.toml", "mise-tasks/test.toml", "tools/pipeline/toolchain.env"):
            with self.subTest(changed=changed):
                self.assertEqual(selected(changed), set(fast_gate.quick_gates()))
        self.assertEqual(selected("uv.lock"), set(fast_gate.quick_gates()) - {GO})

    def test_a_change_the_go_tests_can_break_on_runs_them_and_no_other_change_does(self):
        """The controller's code and module, the contracts generated for it and the bridge base they are
        generated from, and the fixtures its tests read."""
        for changed in (
            "controller/runtime/contract_test.go",
            "controller/host/verify.go",
            "controller/go.mod",
            "controller/go.sum",
            "controller/api/hq-controller.openapi.json",
            "controller/api/hq-connections.openapi.json",
            "hq/domains/control_plane/bridge_base.json",
            "controller/providers/testdata/cloudflare-normalization.json",
            "controller/edge/testdata/fuzz/FuzzReadBundle/sparse-file-past-the-body",
        ):
            with self.subTest(changed=changed):
                self.assertIn(GO, selected(changed))
        for changed in ("docs/Example.md", "hq/domains/docs_index/importer.py", "templates/base.html"):
            with self.subTest(changed=changed):
                self.assertNotIn(GO, selected(changed))

    def test_a_change_to_mise_toml_or_a_workflow_runs_the_gates_that_read_them(self):
        self.assertLessEqual({"lint:mise-format", "lint:workflows"}, selected("mise.toml"))
        self.assertIn("lint:mise-format", selected("mise-tasks/lint/mise-format"))
        self.assertIn("lint:workflows", selected(".github/workflows/ci.yml"))
        self.assertIn("lint:workflows", selected(".github/actions/toolchain/action.yml"))
        self.assertNotIn("lint:workflows", selected("docs/Example.md"))


@tag("source")
class FastGateLintPlacementTests(GateTestCase):
    """Every lint gate is tagged a quick gate, selected by the sources it reads, or named exempt, with why,
    so a new gate is never skipped unseen."""

    def test_every_lint_task_is_a_quick_gate_or_named_exempt(self):
        lint = sorted(name for name in mise_tasks() if name.startswith("lint:"))
        self.assertTrue(lint, "mise lists no lint:* task, so this holds nothing")
        unplaced = [task for task in lint if task not in {*fast_gate.quick_gates(), *EXEMPT}]
        self.assertFalse(
            unplaced,
            f"{', '.join(unplaced)}: tag each fast with the sources it reads (mise-tasks/lint.toml says how),"
            " or name it in EXEMPT in tools/gate/tests/test_fast_selection.py with why fast leaves it out",
        )

    def test_each_exempt_gate_is_a_lint_task_fast_never_selects_with_its_reason(self):
        tasks = mise_tasks()
        quick = fast_gate.quick_gates()
        for task, reason in EXEMPT.items():
            with self.subTest(task=task):
                self.assertIn(task, tasks, f"{task} is no task; drop it from EXEMPT")
                self.assertTrue(task.startswith("lint:"))
                self.assertNotIn(task, quick, f"{task} is tagged fast and exempt")
                self.assertTrue(reason.strip(), f"{task} is left out with no reason")


@tag("source")
class ShellSourcesTests(GateTestCase):
    """The one definition of a shell file, asked by the shell gate and by fast, on a fixture repository."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        files = {
            "script": "#!/bin/sh\necho\n",
            "bash.sh": "#!/usr/bin/env bash\necho\n",
            "lib.sh": "# shellcheck shell=sh\nx=1\n",
            "tool.py": "#!/usr/bin/env python3\n",
            "notes.sh.md": "text\n",
            "gone.sh": "#!/bin/sh\n",
            "gone.txt": "text\n",
            "ignored.sh": "#!/bin/sh\n",
            "new.sh": "#!/bin/dash\n",
        }
        for name, text in files.items():
            (self.root / name).write_text(text)
        (self.root / ".gitignore").write_text("ignored.sh\nnew.sh\n")
        git = fixture_git(self.root)
        git("init", "--quiet")
        git("add", "-A")
        git("commit", "--quiet", "--no-verify", "-m", "base")
        self.base = git("rev-parse", "HEAD")
        (self.root / ".gitignore").write_text("ignored.sh\n")
        (self.root / "gone.sh").unlink()
        (self.root / "gone.txt").unlink()

    def lister(self, *paths: str) -> list[str]:
        done = subprocess.run(
            ["/bin/sh", str(SOURCES), *paths], cwd=self.root, check=True, capture_output=True, text=True
        )
        return done.stdout.splitlines()

    def test_without_paths_it_lists_every_shell_file_git_tracks_or_would_track(self):
        self.assertEqual(sorted(self.lister()), ["bash.sh", "lib.sh", "new.sh", "script"])

    def test_a_shell_file_whose_name_git_quotes_in_a_list_of_lines_is_listed_as_it_is(self):
        """systemd's escaping writes a backslash into a name, which git quotes unless asked for -z."""

        name = r"run\x2dname.sh"
        (self.root / name).write_text("#!/bin/sh\n")
        self.assertIn(name, self.lister())

    def test_given_paths_it_names_those_of_them_that_are_shell_files(self):
        self.assertEqual(self.lister("tool.py", "lib.sh", "missing.sh", "ignored.sh"), ["lib.sh", "ignored.sh"])

    def test_fast_reads_a_present_path_here_and_a_deleted_one_as_the_base_had_it(self):
        # mise's own selection is none, so the shell gate is fast's from the lister alone.
        with (
            patch.object(fast_gate, "ROOT", self.root),
            patch.object(fast_gate, "mise_json", return_value={"tasks": []}),
        ):
            listed = fast_gate.shell_files(["tool.py", "script", "gone.sh", "gone.txt"], self.base)
            self.assertEqual(sorted(listed), ["gone.sh", "script"])
            self.assertNotIn("lint:shell", fast_gate.selected_tasks(["gone.txt", "tool.py"], self.base))
            self.assertIn("lint:shell", fast_gate.selected_tasks(["gone.sh"], self.base))
