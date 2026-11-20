"""Each lint:drift-* gate's failure names the command that rewrites its own files,
and that command, run as named, brings the gate back to passing: one gate in
seconds, where `mise run fix` rewrites every derived file. The gate's own fix
mode, which fix:derived runs for every gate tagged derived,
writes the same files, and exactly the files its declaration names."""

import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any

from django.test import SimpleTestCase, tag

from hq.platform.core.tests.task import changed_paths, names, overlay, trusted_mise, worktree
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

    def test_each_failure_names_a_command_that_rewrites_its_files_alone(self):
        declarations = derived()
        present, deleted = changed_paths("HEAD", ROOT)
        env = {key: value for key, value in os.environ.items() if key != "VIRTUAL_ENV"}
        with worktree("HEAD", ROOT) as tree:
            overlay(tree, present, deleted, ROOT)
            env |= trusted_mise(tree, tree.parent / "mise-state")
            files = [tree / path for path in names("ls-files", "--cached", "--others", "--exclude-standard", root=tree)]

            def run(command: list[str]) -> subprocess.CompletedProcess[str]:
                return subprocess.run(command, cwd=tree, env=env, capture_output=True, text=True, check=False)

            def written(step: list[str]) -> set[str]:
                """The checkout's files the step writes, read from their change times."""

                before = {path: path.stat().st_mtime_ns for path in files if path.exists()}
                done = run(step)
                self.assertEqual(done.returncode, 0, f"{shlex.join(step)}:\n{done.stdout}{done.stderr}")
                return {
                    str(path.relative_to(tree))
                    for path in files
                    if path.exists() and before.get(path) != path.stat().st_mtime_ns
                }

            for name, task in declarations.items():
                with self.subTest(name):
                    held = writes(task)
                    for path in held:
                        (tree / path).unlink()
                    failed = run(["mise", "run", name])
                    said = failed.stdout + failed.stderr
                    self.assertNotEqual(failed.returncode, 0, f"{name} passed with {', '.join(held)} gone:\n{said}")
                    commands = NAMED.findall(said)
                    self.assertTrue(commands, f"{name}'s failure names no command:\n{said}")
                    command = commands[-1]
                    self.assertNotEqual(command, EVERY_FILE, f"{name} names the command that rewrites every file")

                    written(shlex.split(command))
                    for path in held:
                        self.assertEqual((tree / path).read_bytes(), Path(ROOT, path).read_bytes(), command)
                    passed = run(["mise", "run", name])
                    self.assertEqual(passed.returncode, 0, f"{name} after {command}:\n{passed.stdout}{passed.stderr}")

                    # Its fix mode writes the same files, and none its declaration leaves out.
                    for path in held:
                        (tree / path).unlink()
                    self.assertEqual(
                        written(["mise", "run", name, "--fix"]),
                        set(held),
                        f"{name} --fix writes these. {DECLARE}",
                    )
                    for path in held:
                        self.assertEqual((tree / path).read_bytes(), Path(ROOT, path).read_bytes(), f"{name} --fix")
