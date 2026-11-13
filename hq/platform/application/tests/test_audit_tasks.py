"""The audit: tasks stay reads, since a read-only auditor may run any of them."""

import os
import shutil
import subprocess
import tempfile
import tomllib
from pathlib import Path
from typing import Any

from django.test import SimpleTestCase, tag

from tests.mise_tasks import ROOT, body, mise_tasks

# mise's task sandbox holds each read: the audit template denies writes but to
# its temporary and cache paths. These run outside it, each for its reason.
UNSANDBOXED = {
    "audit:image": "docker saves the image under var/audit/image, and it takes the heavy turn's template",
    "audit:mcp": "--base checks a ref out in a git worktree, a write to .git",
}
# What a sandboxed task may write beyond the template's paths, each a path it keeps its run in.
ALSO_WRITES = {"audit:memory": ["var/perf"]}


def declared(task: dict[str, Any]) -> dict[str, Any]:
    """The task's own declaration, before mise applies a template: its TOML
    table, or its file's #MISE header, which mise's listing does not report."""

    if task["file"]:
        header = [line.removeprefix("#MISE ") for line in body(task).splitlines() if line.startswith("#MISE ")]
        return tomllib.loads("\n".join(header))
    return tomllib.loads(Path(task["source"]).read_text())[task["name"]]


def audits() -> dict[str, dict[str, Any]]:
    return {name: task for name, task in mise_tasks().items() if name.startswith("audit:")}


@tag("source")
class AuditTaskTests(SimpleTestCase):
    def test_every_audit_task_runs_in_the_write_sandbox(self) -> None:
        self.assertTrue(audits(), "no audit: task found, so this test reads nothing")
        self.assertEqual(sorted(set(UNSANDBOXED) - set(audits())), [], "an exception names no audit task")
        for name, task in audits().items():
            with self.subTest(task=name):
                own = declared(task)
                if name not in UNSANDBOXED:
                    self.assertEqual(own.get("extends"), "audit")
                self.assertEqual(own.get("allow_write", []), ALSO_WRITES.get(name, []))
                # mise runs a dependency outside the task's sandbox.
                self.assertEqual([each for each in task["depends"] if not each.startswith("setup:")], [])
                self.assertTrue(task["hide"], "an audit task is left out of `mise tasks`")

    def test_every_path_a_sandboxed_audit_may_write_is_made_before_it_runs(self) -> None:
        """On Linux, mise drops the rule for an allowed path that does not exist
        yet, so setup:sandbox, which runs outside the sandbox, makes each one."""

        tasks = mise_tasks()
        template = tomllib.loads((ROOT / "mise.toml").read_text())["task_templates"]["audit"]
        allowed = template["allow_write"] + [path for name in sorted(ALSO_WRITES) for path in ALSO_WRITES[name]]
        made = declared(tasks["setup:sandbox"])["env"]["SANDBOX_WRITES"].split(":")
        self.assertEqual(sorted(made), sorted(allowed))
        for name, task in audits().items():
            if name not in UNSANDBOXED:
                with self.subTest(task=name):
                    self.assertIn("setup:sandbox", task["depends"])

    def test_an_audit_task_runs_before_the_environment_exists(self) -> None:
        """A fresh worktree has no .venv, which the sandbox refuses uv's write
        of; one in var/ is as unwritable to it, and as missing."""

        (ROOT / "var").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / "var") as kept:
            environment = Path(kept) / "venv"
            run = subprocess.run(
                ["mise", "run", "audit:layers"],
                cwd=ROOT,
                env=os.environ | {"UV_PROJECT_ENVIRONMENT": str(environment)},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(run.returncode, 0, run.stderr)

    def test_the_template_denies_writes_and_restates_django_s_env(self) -> None:
        templates = tomllib.loads((ROOT / "mise.toml").read_text())["task_templates"]
        self.assertTrue(templates["audit"]["deny_write"])
        django = templates["django"]["env"]
        self.assertEqual({key: templates["audit"]["env"].get(key) for key in django}, django)

    def test_a_write_an_audit_task_is_tricked_into_fails(self) -> None:
        """A git that writes into the checkout before it reads stands for any
        word or tool that would turn the read into a write."""

        (ROOT / "var").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory() as fake, tempfile.TemporaryDirectory(dir=ROOT / "var") as kept:
            target = Path(kept) / "written"
            git = shutil.which("git")
            self.assertIsNotNone(git)
            script = Path(fake) / "git"
            script.write_text(f'#!/bin/sh\nprintf written >"{target}"\nexec "{git}" "$@"\n')
            script.chmod(0o755)
            run = subprocess.run(
                ["mise", "run", "audit:churn", "2026-01-01"],
                cwd=ROOT,
                env=os.environ | {"PATH": f"{fake}{os.pathsep}{os.environ['PATH']}"},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertFalse(target.exists(), "the sandbox let audit:churn write into the checkout")


@tag("source")
class AdvisoriesTests(SimpleTestCase):
    def test_the_advisories_an_auditor_reads_are_the_gate_s(self) -> None:
        tasks = mise_tasks()
        audit = "tools/pipeline/audit.sh"
        self.assertIn(audit, body(tasks["lint:audit"]))
        self.assertIn(f"exec {audit}", body(tasks["audit:advisories"]))
