"""The audit pages: the log, a change, one entry, and the registry command."""

from django.urls import reverse

from hq.domains.control_plane.models import ManagedResource
from hq.domains.projects.models import Project
from hq.platform.application.markup_testing import hrefs
from hq.platform.core.audit import audited_labels
from hq.platform.core.bindings import operation_context
from hq.platform.core.models import AuditLog
from hq.platform.core.testing import SignedInTestCase


class AuditDetailPageTests(SignedInTestCase):
    """Every row leads somewhere, and what it leads to answers the question."""

    def test_an_event_links_to_the_rest_of_its_operation(self):
        # The point of operation_id: one action touching several rows is one
        # thing that happened, not four events that share a timestamp.
        with operation_context(interface="cli", actor="op", operation="test.bulk", operation_id="op-1"):
            Project.objects.create(name="One", status=Project.Status.IDEA)
            Project.objects.create(name="Two", status=Project.Status.IDEA)
        event, other = AuditLog.objects.filter(operation_id="op-1")

        page = self.client.get(reverse("core:audit_detail", args=[event.pk]))

        self.assertIn(reverse("core:audit_detail", args=[other.pk]), hrefs(page))

    def test_a_row_whose_record_is_gone_or_unreadable_names_none(self):
        label = audited_labels()[ManagedResource]
        for object_id in ("00000000-0000-7000-8000-000000000000", "not-a-key"):
            with self.subTest(object_id=object_id):
                row = AuditLog.objects.create(action=AuditLog.Action.DELETED, object_type=label, object_id=object_id)

                page = self.client.get(reverse("core:audit_detail", args=[row.pk]))

                self.assertEqual(page.status_code, 200)
                self.assertNotIn("about", page.context)
