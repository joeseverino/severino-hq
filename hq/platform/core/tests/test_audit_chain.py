"""One record's whole audit story is one address."""

from django.test import TestCase
from django.urls import reverse

from hq.platform.application.markup_testing import text
from hq.platform.core import append_only
from hq.platform.core.bindings import operation_context
from hq.platform.core.models import AuditLog
from hq.platform.core.testing import declare, operator


class RecordStoryTests(TestCase):
    def setUp(self):
        operator(self.client, "owner")
        with append_only.lifted():
            AuditLog.objects.all().delete()

    def test_about_a_record_shows_its_rows_and_no_other_records(self):
        with operation_context(interface="cli", actor="op", operation="example.one"):
            declare("example-mine", "example.record")
            declare("example-other", "example.record")

        page = self.client.get(reverse("core:audit_list"), {"about": "example-mine"})
        shown = text(page)

        self.assertIn("example-mine", shown)
        self.assertNotIn("example-other", shown)
