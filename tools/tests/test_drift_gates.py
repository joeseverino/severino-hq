"""Each lint:drift-* gate's failure names the command that rewrites its own file,
and that command, run as named, brings the gate back to passing: one file in
seconds, where `mise run fix` rewrites every derived file."""

import os
import re
import shlex
import subprocess
from pathlib import Path

from django.test import SimpleTestCase, tag

from hq.platform.core.tests.task import changed_paths, overlay, trusted_mise, worktree
from tests.mise_tasks import ROOT, body, mise_tasks

DRIFT = "lint:drift-"
# A file each gate holds against its source, which the test deletes to drive
# the gate into failure.
HELD = {
    "lint:drift-sdk-contract": "hq_sdk/contract.json",
    "lint:drift-api-document": "hq/platform/api/hq-api.openapi.json",
    "lint:drift-bridge-contract": "controller/api/hq-controller.openapi.json",
    "lint:drift-connections-reference": "docs/Connections.md",
    "lint:drift-procedure-schema": "hq/platform/application/hq-procedure.schema.json",
    "lint:drift-font": "static/fonts/inter/inter-variable-latin.woff2",
}
# How a failure names its rewrite: "...; run `<command>` and ...".
NAMED = re.compile(r"\brun `([^`]+)`")
# A task a gate's run starts, and the message it echoes when it fails.
RUNS_TASK = re.compile(r"\bmise run ([\w:-]+)")
ECHOED = re.compile(r"""\becho (["'])(.*?)\1""")
EVERY_FILE = "mise run fix"


def drift_gates() -> dict[str, str]:
    """Each drift gate by name, with what it runs."""

    return {name: body(task) for name, task in mise_tasks().items() if name.startswith(DRIFT)}


def records_itself(run: str) -> bool:
    """A gate whose run is its own rewrite, into a file beside the one it holds,
    so its failure is the message it echoes."""

    return bool(RUNS_TASK.search(ECHOED.sub("", run)))


@tag("source", "tree")
class DriftGateTests(SimpleTestCase):
    def test_every_drift_gate_is_driven_into_failure_here(self):
        driven = {name for name, run in drift_gates().items() if not records_itself(run)}
        self.assertTrue(driven)
        self.assertEqual(
            driven - HELD.keys(),
            set(),
            "Name a file each of these holds in HELD, so this test drives it into failure and runs what it names.",
        )
        self.assertEqual(HELD.keys() - driven, set(), "HELD names a gate that is gone or records itself")
        for name, path in HELD.items():
            with self.subTest(name):
                self.assertTrue((ROOT / path).is_file(), f"{name} holds {path}, which is not here")

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

    def test_each_failure_names_a_command_that_rewrites_its_file_alone(self):
        gates = {name: run for name, run in drift_gates().items() if name in HELD}
        present, deleted = changed_paths("HEAD", ROOT)
        env = {key: value for key, value in os.environ.items() if key != "VIRTUAL_ENV"}
        with worktree("HEAD", ROOT) as tree:
            overlay(tree, present, deleted, ROOT)
            env |= trusted_mise(tree, tree.parent / "mise-state")

            def run(command: list[str]) -> subprocess.CompletedProcess[str]:
                return subprocess.run(command, cwd=tree, env=env, capture_output=True, text=True, check=False)

            for name in gates:
                with self.subTest(name):
                    held = tree / HELD[name]
                    held.unlink()
                    failed = run(["mise", "run", name])
                    said = failed.stdout + failed.stderr
                    self.assertNotEqual(failed.returncode, 0, f"{name} passed with {HELD[name]} gone:\n{said}")
                    commands = NAMED.findall(said)
                    self.assertTrue(commands, f"{name}'s failure names no command:\n{said}")
                    command = commands[-1]
                    self.assertNotEqual(command, EVERY_FILE, f"{name} names the command that rewrites every file")

                    rewrote = run(shlex.split(command))
                    self.assertEqual(rewrote.returncode, 0, f"{command}:\n{rewrote.stdout}{rewrote.stderr}")
                    self.assertEqual(held.read_bytes(), Path(ROOT, HELD[name]).read_bytes(), command)
                    passed = run(["mise", "run", name])
                    self.assertEqual(passed.returncode, 0, f"{name} after {command}:\n{passed.stdout}{passed.stderr}")
