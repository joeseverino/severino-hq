"""One act reads alike wherever HQ says who did it, and every table of audit
events draws its rows as the audit log does.

The act is written once, as its audit row; Jobs, the run's page, its audit
entry, the audit log and the run read all say who asked and who approved it
from that row. Every procedure and name here is a synthetic ``example.*`` one.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from hq.domains.control_plane.models import ManagedResource
from hq.platform.application.resources import get_resource
from hq.platform.application.run_reads import RUNS
from hq.platform.application.security import Capability, Principal
from hq.platform.application.tests.markup import all_elements, within
from hq.platform.application.tests.procedures import APPROVER, Supply, approved_run, manifest, run_payload
from hq.platform.core.attribution import operation_context
from hq.platform.core.models import AuditLog

AGENT = Principal("example-agent", "mcp", frozenset({Capability.READ, Capability.MANAGE_INFRASTRUCTURE}))
READER = Principal("example-reader", "api", frozenset({Capability.READ}))


def _words(text: str) -> str:
    """Its words, however the page spaces them."""

    return " ".join(text.split())


def _said(html: str) -> str:
    return _words(" ".join(element.words for element in all_elements(html)))


class OneRunReadsAlikeTests(TestCase):
    def setUp(self):
        Supply(self, manifest())
        answer = approved_run(self, run_payload(), principal=AGENT, key="example-attributed")
        self.operation_id = answer["operation"]["id"]
        self.queued = AuditLog.objects.get(object_id=self.operation_id, action=AuditLog.Action.CREATED)
        self.assertEqual(self.queued.interface, "mcp")
        self.client.force_login(get_user_model().objects.create_superuser("example-admin", "a@example.test", "x" * 14))

    def page(self, url: str) -> str:
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_jobs_its_run_its_audit_entry_and_the_run_read_say_who_asked_alike(self):
        # Written out, so the test reads at a base without the one attribution.
        who = f"example-agent, over MCP · approved by {APPROVER}"

        jobs = self.page(reverse("jobs:list"))
        run = self.page(reverse("jobs:run", kwargs={"pk": self.operation_id}))
        entry = self.page(reverse("core:audit_detail", args=[self.queued.pk]))
        log = self.page(reverse("core:audit_list"))
        read = get_resource(RUNS, self.operation_id, principal=READER)

        for where, html in (("jobs", jobs), ("run", run), ("audit entry", entry), ("audit log", log)):
            with self.subTest(page=where):
                self.assertIn(_words(who), _said(html))
        self.assertEqual((_words(read["by"]), read["approved_by"]), (_words(who), APPROVER))


class OneEventRowTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser("example-admin", "a@example.test", "x" * 14))

    def test_an_entry_s_other_events_say_who_as_the_audit_log_does(self):
        with operation_context(
            interface="mcp", actor="example-agent", operation="example.change", approved_by="example-owner"
        ):
            first = ManagedResource.objects.create(key="example-first", kind="example.record", spec={})
            ManagedResource.objects.create(key="example-second", kind="example.record", spec={})
        row = AuditLog.objects.filter(object_id=str(first.pk)).first()

        html = self.client.get(reverse("core:audit_detail", args=[row.pk])).content.decode()

        others = _words(" ".join(element.words for element in within(html, "id", "rest-of-operation")))
        self.assertIn("example-agent, over MCP · approved by example-owner", others)
