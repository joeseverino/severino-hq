from django.test import SimpleTestCase
from django.urls import reverse

from hq.platform.application.tables import ranked
from hq.platform.application.tests.markup import elements
from hq.platform.core.audit_views import AuditLogListView
from hq.platform.core.models import AuditLog

from .test_web import _AuthedTestCase


class AuditColumnTests(SimpleTestCase):
    def test_a_narrow_audit_log_keeps_what_and_when_and_lets_who_go_first(self):
        columns = ranked(AuditLogListView.table_columns)
        narrow = AuditLogListView.table_drop_narrow
        kept = [column.label for column in columns if not column.drop or column.drop > narrow]

        self.assertEqual(kept, ["What", "When"])
        self.assertEqual(next(column.label for column in columns if column.drop == 1), "Who")


class AuditWhatTests(_AuthedTestCase):
    """On a phone a row is What and When, so there What says what happened
    too, in the word the Action column says it in; wider, that column says it
    and What does not repeat it."""

    def what(self, record: str) -> tuple[str, str]:
        """The cell that names ``record``'s row: all it says, and what it says
        only in a narrow box."""

        page = self.client.get(reverse("core:audit_list")).content.decode()
        (row,) = [row for row in elements(page, "tr") if record in row.text]
        cell = next(cell for cell in elements(page, "td") if row in cell.ancestors and record in cell.text)
        narrow = [
            part.words for part in elements(page, "span") if "narrow-only" in part.classes and cell in part.ancestors
        ]
        return cell.words, " ".join(narrow)

    def test_an_event_says_its_action_only_where_the_action_column_has_left(self):
        AuditLog.objects.create(action=AuditLog.Action.DELETED, object_type="auth.user", object_repr="example-record")

        self.assertIn(AuditLog.Action.DELETED.label, self.what("example-record")[1])

    def test_an_event_with_a_message_says_its_action_and_leaves_the_message_to_its_column(self):
        AuditLog.objects.create(
            action=AuditLog.Action.LOGIN, object_type="auth.user", object_repr="example-record", message="Signed in"
        )

        words, narrow = self.what("example-record")
        self.assertIn(AuditLog.Action.LOGIN.label, narrow)
        self.assertNotIn("Signed in", words)

    def test_a_run_of_events_says_its_action_in_the_same_word(self):
        for _ in range(3):
            AuditLog.objects.create(
                action=AuditLog.Action.LOGIN, object_type="auth.user", object_repr="example-record", message="Signed in"
            )

        words, narrow = self.what("example-record")
        self.assertIn("3 times", words)
        self.assertIn(AuditLog.Action.LOGIN.label, narrow)
