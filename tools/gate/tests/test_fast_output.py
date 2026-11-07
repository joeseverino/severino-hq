"""fast's one mise run: the source checks that read the browser gate run ahead
of the tests, and the summary output is the run's config environment's."""

import subprocess
from pathlib import Path

from django.test import tag
from django.test.runner import DiscoverRunner

from hq.platform.core.test_runner import drives_a_browser, tests_in
from tests.mise_tasks import body, mise_tasks
from tools.gate import fast as fast_gate
from tools.gate.tests.base import GateTestCase

ROOT = Path(__file__).resolve().parents[3]
SELECTORS = "hq.platform.core.tests.test_browser_selectors"


@tag("source", "tree")
class SourceFirstTests(GateTestCase):
    """A selected test that reads each browser module runs in fast:source, which the tests wait on."""

    def test_the_selector_guard_runs_first_and_the_rest_with_the_tests(self):
        other = "hq.platform.core.tests.test_part_tags"
        first, rest = fast_gate.source_first(
            [SELECTORS, f"{SELECTORS}.BrowserGateSelectorTests", other], fast_gate.python_files()
        )
        self.assertEqual(first, [SELECTORS, f"{SELECTORS}.BrowserGateSelectorTests"])
        self.assertEqual(rest, [other])

    def test_each_test_run_first_starts_no_browser(self):
        readers = sorted(fast_gate.importers(fast_gate.python_files())[fast_gate.BROWSER_MODULES])
        self.assertIn(SELECTORS, readers)
        suite = DiscoverRunner(verbosity=0).build_suite(readers)
        self.assertFalse([test.id() for test in tests_in(suite) if drives_a_browser(test)])

    def test_the_tests_start_once_the_source_checks_they_name_have_passed(self):
        # The run as fast makes it, less its gates and its config environment.
        command = fast_gate.run_command((), [SELECTORS], ["example.tests.test_x"], "auto")
        run = command.index("run")
        planned = subprocess.run(
            [*command[: command.index("--env")], "run", "--dry-run", *command[run + 1 :]],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        order = [line for line in (planned.stdout + planned.stderr).splitlines() if " $ " in line]
        self.assertEqual(len(order), 2, order)
        self.assertTrue(order[0].startswith("[fast:source] ") and order[0].endswith(f" {SELECTORS}"), order)
        self.assertTrue(order[1].startswith(f"[{fast_gate.TESTS}] "), order)

    def test_the_run_is_one_mise_run_in_fast_s_environment_with_no_output_flag(self):
        # An --output or MISE_TASK_OUTPUT would outrank the tests' own output setting.
        command = fast_gate.run_command(("lint:ruff", "lint:mypy"), [SELECTORS], ["example.tests.test_x"], "auto")
        mise = command.index("mise")
        self.assertEqual(command[mise : mise + 4], ["mise", "--env", fast_gate.ENVIRONMENT, "run"])
        self.assertIn(f"{fast_gate.SOURCE}={SELECTORS}", command[:mise])
        self.assertNotIn("--output", command)
        self.assertNotIn("-c", command)
        tests = command.index(fast_gate.TESTS)
        self.assertEqual(command[tests - 1], ":::")
        self.assertEqual(command[tests + 1 :], ["--parallel", "auto", "example.tests.test_x"])
        config = (ROOT / f"mise.{fast_gate.ENVIRONMENT}.toml").read_text()
        self.assertRegex(config, r'(?m)^output = "summary"$')
        # mise's listing leaves a task's output out, so it is read from the task's own header.
        self.assertRegex(body(mise_tasks()[fast_gate.TESTS]), r'(?m)^#MISE output="prefix"$')
