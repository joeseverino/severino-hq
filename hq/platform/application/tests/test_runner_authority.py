"""A command whose use case trusts the capability runner is refused there,
before anything is written, to a caller without the capability it requires."""

from django.test import TestCase

from hq.platform.core.models import AuditLog, Revision
from hq.platform.search_index.models import SearchDocument

from ..capabilities import execute_capability
from ..domains import records_of
from ..security import OPERATOR_CAPABILITIES, Capability, Principal

# Each capability whose handler, or a use case only it reaches, leaves its
# authority to the runner; the target where it takes one; and the capability
# the runner refuses it without.
TRUSTING = (
    ("infrastructure.reconcile", "example-target", Capability.MANAGE_INFRASTRUCTURE),
    ("infrastructure.restart", "example-target", Capability.MANAGE_INFRASTRUCTURE),
    ("infrastructure.start", "example-target", Capability.MANAGE_INFRASTRUCTURE),
    ("infrastructure.stop", "example-target", Capability.MANAGE_INFRASTRUCTURE),
    ("infrastructure.resource.accept_observed", "example-target", Capability.MANAGE_INFRASTRUCTURE),
    ("infrastructure.resource.remove", "example-target", Capability.MANAGE_INFRASTRUCTURE),
    ("tailnet.routes.approve", "example-target", Capability.MANAGE_INFRASTRUCTURE),
    ("tailnet.reach.allow", "example-target", Capability.MANAGE_INFRASTRUCTURE),
    ("tailnet.policy.remove_empty_groups", "example-target", Capability.MANAGE_INFRASTRUCTURE),
    ("certificate.renew", "example-target", Capability.REQUEST_CERTIFICATE_RENEWAL),
    ("infrastructure.controller.refresh", None, Capability.MANAGE_INFRASTRUCTURE),
    ("visit.refresh", None, Capability.MANAGE_INFRASTRUCTURE),
    ("dashboard.refresh.request", None, Capability.MANAGE_INFRASTRUCTURE),
    ("dashboard.refresh.stale", None, Capability.MANAGE_INFRASTRUCTURE),
    ("dashboard.machine.select", "example-target", Capability.SET_PREFERENCES),
    ("dashboard.settings.update", None, Capability.SET_PREFERENCES),
    ("appearance.theme.set", None, Capability.SET_PREFERENCES),
    ("agents.access.set", None, Capability.GOVERN_AGENTS),
    ("agents.policy.set", None, Capability.GOVERN_AGENTS),
    ("github.profile.read", None, Capability.LOOK_UP_PUBLIC_RECORDS),
    ("lookup.name", None, Capability.LOOK_UP_PUBLIC_RECORDS),
    ("contact.submissions.list", None, Capability.MANAGE_CONTACTS),
    ("contact.submission.review", 1, Capability.MANAGE_CONTACTS),
    ("contact.submission.delete", 1, Capability.MANAGE_CONTACTS),
    ("project.dependency_updates", "example-target", records_of("projects").write),
    ("project.dependency_updates.run", None, records_of("projects").write),
    ("receipt.update", 1, records_of("receipts").write),
    ("project.refresh", "example-target", records_of("projects").write),
)


def _written() -> dict[str, int]:
    """How many times each table has been written, as the database counts it."""

    return dict(Revision.objects.values_list("name", "value"))


class RefusedAtTheRunnerTests(TestCase):
    def test_a_caller_without_the_capability_is_refused_and_nothing_is_written(self):
        for name, target, capability in TRUSTING:
            with self.subTest(capability=name):
                caller = Principal("example-operator", "web", OPERATOR_CAPABILITIES - {capability})
                before = _written()
                audited = AuditLog.objects.count()

                result = execute_capability(name, {}, principal=caller, target=target)

                self.assertEqual(result["error"]["code"], "forbidden", result)
                self.assertIn(str(capability), result["error"]["message"])
                after = _written()
                moved = {table for table in after.keys() | before.keys() if after.get(table) != before.get(table)}
                # The refusal itself is recorded, and indexed for search, and nothing else.
                self.assertLessEqual(moved, {AuditLog._meta.db_table, SearchDocument._meta.db_table})
                self.assertEqual(
                    set(AuditLog.objects.order_by("pk")[audited:].values_list("action", flat=True)),
                    {AuditLog.Action.DENIED},
                )
