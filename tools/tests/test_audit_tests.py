"""audit:tests: the tests whose lines other tests near them cover, read from a recording's contexts."""

import tempfile
from pathlib import Path
from unittest import mock

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


def start():
    return 6


def deep():
    return 7


def only():
    return 8
"""

# The source of app.tests.test_y, whose tags the audit reads.
TAGGED = """\
from django.test import SimpleTestCase, tag


@tag("source")
class SourceTests(SimpleTestCase):
    def test_reads(self):
        pass


class InheritedTests(SourceTests):
    pass


class YTests(SimpleTestCase):
    def test_twin(self):
        pass

    @tag("tree")
    def test_walk(self):
        pass
"""

NARROW = "app.tests.test_x:XTests.test_narrow"
WIDE = "app.tests.test_x:OtherTests.test_wide"
ALONE = "app.tests.test_x:XTests.test_alone"
TWIN = "app.tests.test_y:YTests.test_twin"
SOURCE = "app.tests.test_y:SourceTests.test_reads"
INHERITED = "app.tests.test_y:InheritedTests.test_reads"
WALK = "app.tests.test_y:YTests.test_walk"
DEEP = "app.tests.test_w:WTests.test_deep"
DEEPER = "app.tests.test_w:WTests.test_deeper"
WIDEST = "other.tests.test_z:ZTests.test_widest"
# Of a family the recording measures no file of.
TOOL = "tool.tests.test_t:TTests.test_tool"
# Each context's lines of app/code.py: return 1 is line 2, return 2 line 6, and so on; line 22 is the
# setup every test of the app runs.
RECORDED = {
    NARROW: {2, 22},
    WIDE: {2, 6, 22, 30},
    ALONE: {14, 22},
    TWIN: {2, 22},
    SOURCE: {2, 6, 10, 14, 22},
    INHERITED: {2, 6, 10, 14, 22},
    WALK: {2, 6, 22},
    DEEP: {2, 6, 22, 26},
    DEEPER: {2, 6, 22, 26},
    WIDEST: {2, 6, 10, 22, 30},
    TOOL: {14, 22},
    # A module's class fixtures, which are no test's.
    "app.tests.test_x": {18},
}


def label(context: str) -> str:
    return context.replace(":", ".")


def audited() -> dict:
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder).resolve()
        code = root / "app" / "code.py"
        code.parent.mkdir()
        code.write_text(CODE)
        (root / "app" / "tests").mkdir()
        (root / "app" / "tests" / "test_y.py").write_text(TAGGED)
        other = root / "other" / "code.py"
        other.parent.mkdir()
        other.write_text(CODE)
        data = CoverageData(basename=str(root / "data"))
        for context, lines in RECORDED.items():
            data.set_context(context)
            data.add_lines({str(code): lines})
        data.set_context(WIDEST)
        data.add_lines({str(other): {2}})
        data.write()
        return audit_tests.audit(audit_tests.measure(data, root))


class AuditTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.report = audited()
        cls.rows = {row["test"]: row for row in cls.report["candidates"]}

    def test_a_test_whose_lines_a_test_of_its_module_covers_names_that_test(self):
        self.assertEqual(self.rows[label(NARROW)]["wider"], [label(WIDE)])

    def test_a_wider_test_of_another_module_is_not_named(self):
        self.assertEqual(self.rows[label(TWIN)]["wider"], [])

    def test_tests_of_a_family_that_run_the_same_lines_name_each_other(self):
        self.assertEqual(self.rows[label(NARROW)]["equal"], [label(TWIN)])
        self.assertEqual(self.rows[label(TWIN)]["equal"], [label(NARROW)])

    def test_a_wider_test_of_another_family_makes_no_candidate(self):
        self.assertNotIn(label(WIDE), self.rows)

    def test_a_test_with_a_line_of_its_own_is_no_candidate(self):
        self.assertNotIn(label(ALONE), self.rows)
        self.assertNotIn(label(ALONE), self.report["without a line of their own"])

    def test_a_line_another_family_s_test_runs_is_not_a_test_s_own(self):
        self.assertIn(label(WIDE), self.report["without a line of their own"])

    def test_a_module_s_fixtures_are_no_test(self):
        self.assertEqual(self.report["counts"]["tests"], 7)

    def test_a_test_tagged_source_or_tree_is_left_out_as_covering_and_covered(self):
        self.assertEqual(self.report["left out"][audit_tests.TAGGED], [label(INHERITED), label(SOURCE), label(WALK)])
        for test in (SOURCE, INHERITED, WALK):
            self.assertNotIn(label(test), self.rows)
            self.assertNotIn(label(test), self.rows[label(TWIN)]["wider"])

    def test_a_test_whose_family_s_code_is_unmeasured_is_left_out_as_covering_and_covered(self):
        self.assertEqual(self.report["left out"][audit_tests.UNMEASURED], [label(TOOL)])
        self.assertNotIn(label(TOOL), self.report["without a line of their own"])

    def test_a_line_only_left_out_tests_run_beside_it_is_a_test_s_own(self):
        self.assertNotIn(label(ALONE), self.report["without a line of their own"])

    def test_a_test_s_size_leaves_out_the_lines_all_its_peers_run(self):
        self.assertEqual(self.rows[label(NARROW)]["lines"], 2)
        self.assertEqual(self.rows[label(NARROW)]["past setup"], 1)
        # Peers that all run the same lines are measured against their family's setup.
        self.assertEqual(self.rows[label(DEEP)]["past setup"], 3)

    def test_equal_tests_are_grouped_the_most_lines_past_setup_first(self):
        self.assertEqual(
            [sorted(group["tests"]) for group in self.report["equal"]],
            [sorted(map(label, [DEEP, DEEPER])), sorted(map(label, [NARROW, TWIN]))],
        )

    def test_a_test_is_grouped_under_the_function_the_fewest_tests_run(self):
        self.assertEqual(self.rows[label(NARROW)]["reaches"], "app/code.py:shared")
        grouped = {row["reaches"]: row["candidates"] for row in self.report["reaches"]}
        self.assertEqual(sorted(grouped["app/code.py:shared"]), sorted([label(NARROW), label(TWIN)]))

    def test_the_report_says_a_candidate_is_no_verdict(self):
        self.assertIn("not proof", self.report["caveat"])


class LargeModuleTests(SimpleTestCase):
    def test_a_test_of_a_large_module_is_compared_with_its_class_alone(self):
        with mock.patch.object(audit_tests, "LARGE", 2):
            rows = {row["test"]: row for row in audited()["candidates"]}
        self.assertEqual(rows[label(NARROW)]["wider"], [])


@tag("source")
class AuditArtifactTests(SimpleTestCase):
    def test_ci_keeps_the_report_the_audit_writes(self):
        uploads = {
            step["with"]["name"]: step["with"]["path"]
            for step in steps(jobs(workflow("map.yml"))["map"])
            if step.get("uses", "").startswith("actions/upload-artifact@")
        }
        self.assertEqual(uploads.get("test-audit"), audit_tests.WRITTEN.as_posix())
