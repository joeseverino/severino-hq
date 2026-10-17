from django.test import SimpleTestCase

from hq.platform.core.audit_views import AuditLogListView


class AuditColumnTests(SimpleTestCase):
    def test_a_narrow_audit_log_keeps_what_changed_and_lets_who_go_first(self):
        """On a phone the table keeps the columns marked key: a row that reads
        "system / Created" says nothing, so What stays and Who leaves first."""

        css = {column.label: column.css.split() for column in AuditLogListView.table_columns}

        self.assertIn("key-col", css["What"])
        self.assertIn("optional-col", css["Who"])
