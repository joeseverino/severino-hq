"""audit:hand-rolled prints the page-class check's count, read from the check's own reader."""

from django.test import SimpleTestCase, tag

from tests.mise_tasks import body, mise_tasks
from tools import audit_hand_rolled


@tag("source")
class HandRolledAuditTests(SimpleTestCase):
    def test_the_task_reads_the_checks_own_reader(self) -> None:
        """One reader: the task recounts nothing, it prints what the check reads."""

        self.assertIn("tools.audit_hand_rolled", body(mise_tasks()["audit:hand-rolled"]))
        lines, agrees = audit_hand_rolled.report(
            {("a.html", "x"), ("a.html", "y"), ("b.html", "x")}, {"a.html": "x y", "b.html": "x"}
        )
        self.assertTrue(agrees)
        self.assertEqual(lines[0], "2 classes, 2 templates, 3 template-and-class pairs")

    def test_a_difference_from_hand_rolled_is_named_and_fails(self) -> None:
        lines, agrees = audit_hand_rolled.report({("a.html", "new")}, {"a.html": "gone"})
        self.assertFalse(agrees)
        self.assertIn("hand-rolled, not in HAND_ROLLED: templates/a.html: .new", lines)
        self.assertIn("no longer hand-rolled: delete it from HAND_ROLLED: templates/a.html: .gone", lines)
