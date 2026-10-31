"""A gate's closing line names what failed and ends on the command that reruns
just that, and a gate run under another leaves its failed tests to the outer
verdict too."""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from django.test import tag

from tools.gate import verdict as gate_verdict
from tools.gate.tests.base import UNITTEST, GateTestCase
from tools.gate.tests.test_fast import RUNNER_FAILURES, VERDICT_IN

ROOT = Path(__file__).resolve().parents[3]

# A command that prints the lines it is given, as mise and the runner print
# theirs, records the tests it is given as the runner records its failures,
# and fails.
SAYS = """
import json, os, sys
from pathlib import Path
lines, *tests = sys.argv[1:]
if tests:
    records = [{"test": test, "exception": "", "traceback": ""} for test in tests]
    Path(os.environ["HQ_TEST_REPORTS"], "1.json").write_text(json.dumps(records))
for line in json.loads(lines):
    print(line)
sys.exit(1)
"""


@tag("source")
class NestedVerdictTests(GateTestCase):
    def test_the_outer_verdict_names_the_tests_an_inner_gate_saw_fail(self):
        """As `check` runs `test --composed`, which keeps a verdict of its own."""

        kept = str(gate_verdict.VERDICTS)
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "test_example.py").write_text(RUNNER_FAILURES)
            unittest = [sys.executable, "-c", UNITTEST, "discover", "-s", folder, "-t", folder]
            inner = [sys.executable, "-c", VERDICT_IN, kept, "inner", *unittest]
            result = subprocess.run(
                [sys.executable, "-c", VERDICT_IN, kept, "outer", *inner],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(result.returncode, 1, result.stdout)
        inner_line, outer_line = (line for line in result.stdout.splitlines() if " FAILED in " in line)
        for line in (inner_line, outer_line):
            with self.subTest(line=line):
                self.assertIn("tests: test_example.Case.test_compares, test_example.Case.test_raises", line)
        outer = Path(kept, "outer.txt").read_text().splitlines()
        self.assertIn("test_example.Case.test_raises: LookupError: no example.invalid record", outer)


@tag("source")
class RerunTests(GateTestCase):
    def gate(self, name: str, *cmd: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-c", VERDICT_IN, str(gate_verdict.VERDICTS), name, *cmd],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    def closing(self, name: str, lines: list[str], *tests: str) -> str:
        """The closing line of a gate whose command printed ``lines`` and failed ``tests``."""

        result = self.gate(name, sys.executable, "-c", SAYS, json.dumps(lines), *tests)
        self.assertEqual(result.returncode, 1, result.stdout)
        return result.stdout.splitlines()[-1]

    def test_an_aggregate_is_named_by_the_leaf_it_failed_under_alone(self):
        line = self.closing(
            "check",
            [
                "[lint] [lint:mise-format] exited with status 1",
                "[browser] [browser:navigation] exited with status 1",
                "[lint] exited with status 1",
                "[browser] exited with status 1",
            ],
        )
        failed = line.split(": ", 1)[1].split("; rerun: ")[0].split("; ")
        self.assertEqual(failed, ["lint:mise-format", "browser:navigation"])
        self.assertTrue(line.endswith("; rerun: mise run -c lint:mise-format ::: browser:navigation"), line)

    def test_a_line_a_tool_quotes_from_a_file_names_no_task(self):
        """As a formatter's diff of a test that holds mise's lines."""

        line = self.closing(
            "fast",
            [
                '[lint:ruff]     -     ["[lint:ruff] exited with status 1", "[lint:source] exited with status 1"],',
                '[lint:ruff] 101 +    "[lint:source] FAIL: test_x (example.tests.Case.test_x)",',
                "[lint:ruff] exited with status 1",
            ],
        )
        self.assertTrue(line.endswith(": lint:ruff; rerun: mise run lint:ruff"), line)

    def test_a_task_that_failed_alone_is_named_and_rerun(self):
        line = self.closing("test", ["[test:controller] ERROR task failed"])
        self.assertTrue(line.endswith(": test:controller; rerun: mise run test:controller"), line)

    def test_failed_tests_rerun_by_label_rather_than_the_task_that_ran_them(self):
        test = "example.tests.test_a.Case.test_x"
        line = self.closing(
            "lint",
            [
                "[lint:ruff] exited with status 1",
                f"[lint:source] FAIL: test_x ({test})",
                "[lint:source] exited with status 1",
            ],
            test,
        )
        self.assertTrue(line.endswith(f"; rerun: mise run -c lint:ruff ::: test {test}"), line)

    def test_a_class_fixture_and_a_module_that_did_not_import_rerun_by_what_they_name(self):
        line = self.closing(
            "test", [], "setUpClass (example.tests.test_a.Case)", "unittest.loader._FailedTest.example.tests.test_b"
        )
        self.assertTrue(line.endswith("; rerun: mise run test example.tests.test_a.Case example.tests.test_b"), line)

    def test_a_test_of_the_composed_suite_reruns_composed(self):
        test = "example.tests.Case.test_x"
        for name, lines in (
            ("check", [f"[test:composed] FAIL: test_x ({test})", "[test:composed] exited with status 1"]),
            ("test:composed", []),
        ):
            with self.subTest(name=name):
                line = self.closing(name, lines, test)
                self.assertTrue(line.endswith(f"; rerun: mise run test --composed {test}"), line)

    def test_many_failed_tests_rerun_by_their_classes(self):
        tests = [f"example.tests.Case{n % 2}.test_{n}" for n in range(gate_verdict.SHOWN + 2)]
        line = self.closing("test", [], *tests)
        self.assertTrue(line.endswith("; rerun: mise run test example.tests.Case0 example.tests.Case1"), line)

    def test_a_step_that_names_nothing_reruns_by_its_own_command(self):
        result = self.gate("check", "--step", "deploy host", sys.executable, "-c", "raise SystemExit(3)")
        line = result.stdout.splitlines()[-1]
        self.assertTrue(line.endswith(": deploy host; rerun: uv run python -c 'raise SystemExit(3)'"), line)

    def test_each_failed_leaf_s_last_lines_are_kept_and_its_last_few_shown_above_the_verdict(self):
        """A failed test's traceback says why it failed, so its task's lines are not kept."""

        test = "example.tests.test_a.Case.test_x"
        said = [f"[lint] [lint:shell] example.sh: finding {n}" for n in range(gate_verdict.TAIL_KEPT + 5)]
        lines = [
            *said,
            "[lint] [lint:ruff] All checks passed!",
            f"[lint] [lint:source] FAIL: test_x ({test})",
            "[lint] [lint:shell] exited with status 1",
            "[lint] [lint:source] exited with status 1",
            "[lint] exited with status 1",
        ]
        result = self.gate("tails", sys.executable, "-c", SAYS, json.dumps(lines), test)
        printed = result.stdout.splitlines()
        self.assertTrue(printed[-1].endswith(f"; rerun: mise run -c lint:shell ::: test {test}"), printed[-1])
        shown = [f"example.sh: finding {n}" for n in range(len(said) - gate_verdict.TAIL_SHOWN, len(said))]
        heading = printed.index("[tails] lint:shell ended on:")
        self.assertEqual([line.split("   ", 1)[1] for line in printed[heading + 1 : -2]], shown)
        kept = Path(gate_verdict.VERDICTS, "tails.txt").read_text().splitlines()
        at = kept.index("lint:shell ended on:")
        tail = [f"example.sh: finding {n}" for n in range(len(said) - gate_verdict.TAIL_KEPT, len(said))]
        self.assertEqual(kept[at + 1 : at + 1 + gate_verdict.TAIL_KEPT], tail)
        self.assertEqual([line for line in kept if line.endswith(" ended on:")], ["lint:shell ended on:"])

    def test_a_pass_names_no_rerun_and_the_run_before_is_kept_beside_it(self):
        self.closing("kept", ["[lint:ruff] exited with status 1"])
        passed = self.gate("kept", sys.executable, "-c", "pass")
        self.assertNotIn("rerun", passed.stdout)
        latest = Path(gate_verdict.VERDICTS, "kept.txt").read_text()
        before = Path(gate_verdict.VERDICTS, "kept.prev.txt").read_text()
        self.assertTrue(latest.startswith("[kept] passed in"), latest)
        self.assertTrue(before.splitlines()[0].endswith("; rerun: mise run lint:ruff"), before)
