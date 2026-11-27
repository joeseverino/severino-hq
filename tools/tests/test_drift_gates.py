"""Each lint:drift-* gate is a declaration of the files it writes, which
fix:derived runs and .gitattributes marks, or a gate that records itself and
names the task it records with."""

import os
import re
import subprocess
from typing import Any

from django.test import SimpleTestCase, tag

from hq.platform.core.tests.task import changed_paths, overlay, trusted_mise, worktree
from tests.mise_tasks import ROOT, body, mise_tasks, writes

DRIFT = "lint:drift-"
# The tag that makes a gate its files' declaration, the one fix:derived runs.
DERIVED = "derived"
# How a failure names its rewrite: "...; run `<command>` and ...".
NAMED = re.compile(r"\brun `([^`]+)`")
# A task a gate's run starts, and the message it echoes when it fails.
RUNS_TASK = re.compile(r"\bmise run ([\w:-]+)")
ECHOED = re.compile(r"""\becho (["'])(.*?)\1""")
EVERY_FILE = "mise run fix"
DECLARE = "Tag it derived and name each file its fix writes in its vars.writes (docs/Development.md, Derived files)."


def drift_gates() -> dict[str, str]:
    """Each drift gate by name, with what it runs."""

    return {name: body(task) for name, task in mise_tasks().items() if name.startswith(DRIFT)}


def derived() -> dict[str, dict[str, Any]]:
    """Each gate tagged derived, by name."""

    return {name: task for name, task in mise_tasks().items() if DERIVED in task["tags"]}


def records_itself(run: str) -> bool:
    """A gate whose run is its own rewrite, into a file beside the one it holds,
    so its failure is the message it echoes."""

    return bool(RUNS_TASK.search(ECHOED.sub("", run)))


# Each rewrite runs every derived file's writer over a worktree of the whole
# checkout, so whatever selects it, it is CI's lint:source, never fast's.
@tag("source", "tree")
class DriftGateTests(SimpleTestCase):
    def test_every_drift_gate_is_a_declaration_or_records_itself(self):
        gates = drift_gates()
        declarations = derived()
        self.assertTrue(declarations)
        for name, run in gates.items():
            with self.subTest(name):
                self.assertNotEqual(name in declarations, records_itself(run), f"{name}: {DECLARE}")
                self.assertTrue(writes(mise_tasks()[name]), f"{name} declares no file it writes. {DECLARE}")
        self.assertLessEqual(declarations.keys(), gates.keys(), f"a gate tagged {DERIVED} is named {DRIFT}*")
        for name, task in declarations.items():
            for path in writes(task):
                with self.subTest(name, path=path):
                    self.assertTrue((ROOT / path).is_file(), f"{name} declares it writes {path}, which is not here")

    def test_a_gate_that_records_itself_names_the_task_it_records_with(self):
        for name, run in drift_gates().items():
            if not records_itself(run):
                continue
            with self.subTest(name):
                ran = set(RUNS_TASK.findall(ECHOED.sub("", run)))
                named = {task for _, said in ECHOED.findall(run) for task in RUNS_TASK.findall(said)}
                self.assertTrue(named, f"{name}'s failure names no task")
                self.assertLessEqual(named, ran)
                self.assertNotIn("fix", named)

    def test_a_new_declaration_is_written_and_marked_with_nothing_else_edited(self):
        """A gate tagged derived, added alone, is a file `mise run fix` writes and .gitattributes marks."""

        declared = "docs/Example-derived.md"
        gate = "\n".join(
            (
                "",
                '["lint:drift-example"]',
                'description = "An example derived file"',
                "hide = true",
                'tags = ["derived"]',
                """usage = 'flag "--fix" help="Rewrite the file"'""",
                f'vars = {{ writes = "{declared}" }}',
                f"""run = 'if [ "${{usage_fix:-}}" = true ]; then echo example >{declared}; else test -f {declared}; fi'""",
                "",
            )
        )
        present, deleted = changed_paths("HEAD", ROOT)
        env = {key: value for key, value in os.environ.items() if key != "VIRTUAL_ENV"}
        with worktree("HEAD", ROOT) as tree:
            overlay(tree, present, deleted, ROOT)
            env |= trusted_mise(tree, tree.parent / "mise-state")
            with (tree / "mise-tasks" / "lint.toml").open("a") as tasks:
                tasks.write(gate)

            def run(*command: str) -> subprocess.CompletedProcess[str]:
                return subprocess.run(command, cwd=tree, env=env, capture_output=True, text=True, check=False)

            behind = run("mise", "run", "lint:drift-gitattributes")
            self.assertNotEqual(behind.returncode, 0, "the attributes did not see the new declaration")
            self.assertIn("mise run lint:drift-gitattributes --fix", behind.stderr)

            fixed = run("mise", "run", "fix:derived")
            self.assertEqual(fixed.returncode, 0, fixed.stdout + fixed.stderr)
            self.assertTrue((tree / declared).is_file(), "fix:derived did not run the new gate's fix")
            marked = run("git", "check-attr", "linguist-generated", "--", declared)
            self.assertEqual(marked.stdout.strip(), f"{declared}: linguist-generated: set")
            current = run("mise", "run", "lint:drift-gitattributes")
            self.assertEqual(current.returncode, 0, current.stdout + current.stderr)
