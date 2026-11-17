"""audit:tests: the tests whose lines other tests of their family cover, read from a recording's contexts."""

import tempfile
from pathlib import Path

from coverage import CoverageData
from django.test import SimpleTestCase, tag

from hq.platform.application.tests.test_pipeline import jobs, steps, workflow
from tools import audit_tests

CODE = """\
def shared():
    return 1


def wider():
    return 2


def elsewhere():
    return 3


def alone():
    return 4


def fixture():
    return 5
"""

NARROW = "app.tests.test_x:XTests.test_narrow"
TWIN = "app.tests.test_y:YTests.test_twin"
WIDE = "app.tests.test_x:XTests.test_wide"
ALONE = "app.tests.test_x:XTests.test_alone"
WIDEST = "other.tests.test_z:ZTests.test_widest"
# Each context's lines of app/code.py: return 1 is line 2, return 2 line 6, and so on.
RECORDED = {
    NARROW: {2},
    TWIN: {2},
    WIDE: {2, 6},
    ALONE: {14},
    WIDEST: {2, 6, 10},
    # A module's class fixtures, which are no test's.
    "app.tests.test_x": {18},
}


def label(context: str) -> str:
    return context.replace(":", ".")


class AuditTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            code = root / "app" / "code.py"
            code.parent.mkdir()
            code.write_text(CODE)
            data = CoverageData(basename=str(root / "data"))
            for context, lines in RECORDED.items():
                data.set_context(context)
                data.add_lines({str(code): lines})
            data.write()
            cls.report = audit_tests.audit(audit_tests.measure(data, root))
        cls.rows = {row["test"]: row for row in cls.report["candidates"]}

    def test_a_test_whose_lines_a_test_of_its_family_covers_names_that_test(self):
        self.assertEqual(self.rows[label(NARROW)]["wider"], [label(WIDE)])
        self.assertEqual(self.rows[label(TWIN)]["wider"], [label(WIDE)])

    def test_tests_that_run_the_same_lines_name_each_other(self):
        self.assertEqual(self.rows[label(NARROW)]["equal"], [label(TWIN)])
        self.assertEqual(self.rows[label(TWIN)]["equal"], [label(NARROW)])

    def test_a_wider_test_of_another_family_makes_no_candidate(self):
        self.assertNotIn(label(WIDE), self.rows)

    def test_a_test_with_a_line_of_its_own_is_no_candidate(self):
        self.assertNotIn(label(ALONE), self.rows)
        self.assertNotIn(label(ALONE), self.report["without a line of their own"])

    def test_a_line_another_family_s_test_runs_is_not_a_test_s_own(self):
        self.assertEqual(self.report["without a line of their own"], sorted([label(NARROW), label(TWIN), label(WIDE)]))

    def test_a_module_s_fixtures_are_no_test(self):
        self.assertEqual(self.report["counts"]["tests"], 5)

    def test_a_test_is_grouped_under_the_function_the_fewest_tests_run(self):
        self.assertEqual(self.rows[label(NARROW)]["reaches"], "app/code.py:shared")
        grouped = {row["reaches"]: row["candidates"] for row in self.report["reaches"]}
        self.assertEqual(sorted(grouped["app/code.py:shared"]), sorted([label(NARROW), label(TWIN)]))

    def test_the_report_says_a_candidate_is_no_verdict(self):
        self.assertIn("not proof", self.report["caveat"])


@tag("source")
class AuditArtifactTests(SimpleTestCase):
    def test_ci_keeps_the_report_the_audit_writes(self):
        uploads = {
            step["with"]["name"]: step["with"]["path"]
            for step in steps(jobs(workflow("ci.yml"))["map"])
            if step.get("uses", "").startswith("actions/upload-artifact@")
        }
        self.assertEqual(uploads.get("test-audit"), audit_tests.WRITTEN.as_posix())
