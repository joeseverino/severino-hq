"""fast's one mise run: the source checks that read the browser gate run ahead
of the tests, the summary output is the run's config environment's, and the
run goes on past a failure to name every one."""

import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import tag
from django.test.runner import DiscoverRunner

from hq.platform.core.test_runner import drives_a_browser, tests_in
from hq.platform.core.tests.task import trusted_mise
from tests.mise_tasks import body, mise_tasks
from tools.gate import fast as fast_gate, verdict
from tools.gate.tests.base import GateTestCase
from tools.gate.verdict import ESCAPE

ROOT = Path(__file__).resolve().parents[3]
PROBES = "hq.platform.core.tests.test_browser_probes"


@tag("source", "tree")
class SourceFirstTests(GateTestCase):
    """A selected test that reads each browser module runs in fast:source, which the tests wait on."""

    def test_the_probe_check_runs_first_and_the_rest_with_the_tests(self):
        other = "hq.platform.core.tests.test_part_tags"
        first, rest = fast_gate.source_first([PROBES, f"{PROBES}.ProbeTests", other], fast_gate.python_files())
        self.assertEqual(first, [PROBES, f"{PROBES}.ProbeTests"])
        self.assertEqual(rest, [other])

    def test_each_test_run_first_starts_no_browser(self):
        readers = sorted(fast_gate.importers(fast_gate.python_files())[fast_gate.BROWSER_MODULES])
        self.assertIn(PROBES, readers)
        suite = DiscoverRunner(verbosity=0).build_suite(readers)
        self.assertFalse([test.id() for test in tests_in(suite) if drives_a_browser(test)])

    def test_the_tests_start_once_the_source_checks_they_name_have_passed(self):
        # The run as fast makes it, less its gates and its config environment.
        command = fast_gate.run_command((), [PROBES], ["example.tests.test_x"], "auto")
        run = command.index("run")
        planned = subprocess.run(
            [*command[: command.index("--env")], "run", "--dry-run", *command[run + 1 :]],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        # Read without the color mise gives its lines on a CI runner.
        plain = ESCAPE.sub("", planned.stdout + planned.stderr)
        order = [line for line in plain.splitlines() if " $ " in line]
        self.assertEqual(len(order), 2, order)
        self.assertTrue(order[0].startswith("[fast:source] ") and order[0].endswith(f" {PROBES}"), order)
        self.assertTrue(order[1].startswith(f"[{fast_gate.TESTS}] "), order)

    def test_the_run_is_one_mise_run_in_fast_s_environment_with_no_output_flag(self):
        # An --output or MISE_TASK_OUTPUT would outrank the tests' own output setting.
        command = fast_gate.run_command(("lint:ruff", "lint:mypy"), [PROBES], ["example.tests.test_x"], "auto")
        mise = command.index("mise")
        self.assertEqual(command[mise : mise + 4], ["mise", "--env", fast_gate.ENVIRONMENT, "run"])
        self.assertIn(f"{fast_gate.SOURCE}={PROBES}", command[:mise])
        self.assertNotIn("--output", command)
        tests = command.index(fast_gate.TESTS)
        self.assertEqual(command[tests - 1], ":::")
        self.assertEqual(command[tests + 1 :], ["--parallel", "auto", "example.tests.test_x"])
        config = (ROOT / f"mise.{fast_gate.ENVIRONMENT}.toml").read_text()
        self.assertRegex(config, r'(?m)^output = "summary"$')
        # mise's listing leaves a task's output out, so it is read from the task's own header.
        self.assertRegex(body(mise_tasks()[fast_gate.TESTS]), r'(?m)^#MISE output="prefix"$')


# Two quick gates that fail and the tests' task, as a project of the test's own declares them.
FAILING_GATES = f"""
[tasks."lint:ruff"]
run = "echo ruff refused example.py; exit 1"

[tasks."lint:prose"]
run = "echo prose refused example.md; exit 3"

[tasks."{fast_gate.TESTS}"]
run = "true"
"""


class EveryFailureTests(GateTestCase):
    """One run of fast's names every quick gate that failed, and keeps why each did."""

    def test_a_run_with_two_failing_gates_names_both_and_keeps_each_one_s_output(self):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (folder / "mise.toml").write_text(FAILING_GATES)
        environment = f"mise.{fast_gate.ENVIRONMENT}.toml"
        (folder / environment).write_text((ROOT / environment).read_text())
        none = folder / "none.toml"
        none.touch()
        # Nothing of the run this test is in: an inherited MISE_TASK_LOCKS would read as held.
        env = {name: value for name, value in os.environ.items() if not name.startswith("MISE_")}
        env |= trusted_mise(folder, folder / "state")
        env |= {
            "MISE_CEILING_PATHS": str(folder.parent),
            "MISE_GLOBAL_CONFIG_FILE": str(none),
            "MISE_SYSTEM_CONFIG_FILE": str(none),
        }
        # One job, so the second gate starts only after the first has failed.
        command = fast_gate.run_command(("lint:ruff", "lint:prose"), [], ["example.tests.test_x"], "1")
        with patch.dict(os.environ, env, clear=True), patch.object(verdict, "outlets", return_value=[]):
            step = verdict.run("quick gates and tests", command, cwd=folder)
        self.assertFalse(step.ok)
        self.assertNotEqual(step.code, 0)
        self.assertLessEqual({"lint:ruff", "lint:prose"}, set(step.failures()))
        kept = step.tails()
        self.assertIn("ruff refused example.py", kept.get("lint:ruff", []))
        self.assertIn("prose refused example.md", kept.get("lint:prose", []))
