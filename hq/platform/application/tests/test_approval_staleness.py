"""A held request goes stale when what it would apply, or what its page showed,
moves; never when the controller only reads the same record again."""

from contextlib import suppress

from django.test import TestCase
from django.utils import timezone

from hq.domains.control_plane.models import ApprovalRequest, CapabilityRule, ManagedResource, OperationRequest
from hq.domains.projects.models import Project
from hq.platform.core.models import AuditLog

from ..approvals import AUDIT_LABELS, ApprovalError, ApproveCommand, approve, preview
from ..capabilities import execute_capability, run_capability
from ..domains import records_of
from ..projects import DEPENDENCY_UPDATES
from ..security import Capability, Principal, cli_principal, mcp_principal
from ..sweep import record_sweep
from .test_approvals import POLICY_KEY, an_operator, declare_policy, policy_document, reconcile_payload
from .writers import keyed

REWRITE = "adguard.rewrite"
RECORD = {"domain": "photos.example.com", "answer": "192.0.2.10"}


def held_for(capability: str, payload: dict, target: str, principal: Principal = None) -> ApprovalRequest:
    answer = execute_capability(capability, payload, principal=principal or mcp_principal(), target=target)
    assert answer.get("status") == "awaiting_approval", answer
    return ApprovalRequest.objects.get(pk=answer["approval"]["id"])


def decided(held: ApprovalRequest) -> str:
    """The state a person's approval leaves it in."""

    with suppress(ApprovalError):
        approve(ApproveCommand(str(held.id)), principal=an_operator())
    held.refresh_from_db()
    return held.state


class DeclarationStalenessTests(TestCase):
    def setUp(self):
        from hq.platform.application.adoption_testing import managing_everything

        managing_everything()
        # As the controller's last reconcile reported it, enabled flag and all.
        self.resource = ManagedResource.objects.create(
            key="photos", kind=REWRITE, spec=dict(RECORD), status={**RECORD, "enabled": True}
        )

    def removal(self) -> ApprovalRequest:
        return held_for("infrastructure.resource.remove", reconcile_payload(), "photos")

    def test_a_sweep_that_reads_the_record_again_leaves_the_removal_approvable(self):
        held = self.removal()

        record_sweep({REWRITE: {"ok": True, "records": [{**RECORD, "enabled": True}]}}, principal=cli_principal())

        self.resource.refresh_from_db()
        self.assertNotEqual(self.resource.status, {**RECORD, "enabled": True}, "the sweep rewrote what it read")
        self.assertEqual(decided(held), ApprovalRequest.State.APPROVED)
        self.assertEqual(OperationRequest.objects.get().action, OperationRequest.Action.DELETE)

    def stales_when(self, **change) -> None:
        held = self.removal()

        ManagedResource.objects.filter(pk=self.resource.pk).update(**change)

        self.assertEqual(decided(held), ApprovalRequest.State.STALE)
        self.assertFalse(OperationRequest.objects.exists())

    def test_a_spec_that_moved_stales_it(self):
        self.stales_when(spec={**RECORD, "answer": "192.0.2.20"})

    def test_a_declaration_switched_off_stales_it(self):
        self.stales_when(enabled=False)

    def test_a_new_generation_stales_it(self):
        self.stales_when(generation=2)


class LiveComparisonStalenessTests(TestCase):
    """An apply is shown against the live record, so that record counts."""

    def setUp(self):
        from hq.platform.application.adoption_testing import managing_everything

        managing_everything()
        self.resource = declare_policy()
        self.resource.status = {"document": policy_document("group:live")}
        self.resource.save(update_fields=("status",))
        self.held = held_for("infrastructure.resource.reconcile", reconcile_payload(), POLICY_KEY)

    def test_the_live_record_it_was_compared_against_moving_stales_it(self):
        self.assertTrue(preview(self.held).rows, "the page shows the live record")
        self.resource.status = {"document": policy_document("group:elsewhere")}
        self.resource.save(update_fields=("status",))

        self.assertEqual(decided(self.held), ApprovalRequest.State.STALE)


class RecordStalenessTests(TestCase):
    """A record's change shows the fields it sends and the record's name."""

    def setUp(self):
        self.project = Project.objects.create(
            name="Example App",
            slug="example-app",
            repository_url="https://github.com/example/app",
            dependency_updates=True,
        )
        CapabilityRule.objects.create(subject="example-agent", capability=DEPENDENCY_UPDATES, rule="approve")
        agent = Principal("example-agent", "mcp", frozenset({Capability.READ, records_of("projects").write}))
        self.held = held_for(DEPENDENCY_UPDATES, keyed({"dependency_updates": False}), "example-app", agent)

    def test_a_refresh_of_what_github_says_leaves_it_approvable(self):
        self.project.last_push_at = timezone.now()
        self.project.save()

        self.assertEqual(decided(self.held), ApprovalRequest.State.APPROVED)
        self.project.refresh_from_db()
        self.assertFalse(self.project.dependency_updates)

    def test_a_change_to_the_value_it_showed_stales_it(self):
        Project.objects.filter(pk=self.project.pk).update(dependency_updates=False)

        self.assertEqual(decided(self.held), ApprovalRequest.State.STALE)

    def test_a_change_to_the_name_it_showed_stales_it(self):
        Project.objects.filter(pk=self.project.pk).update(name="Renamed App")

        self.assertEqual(decided(self.held), ApprovalRequest.State.STALE)


class SettledThroughTheRunnerTests(TestCase):
    """A decision a person posts carries a retry key, so it runs inside the key's
    transaction; a refusal that settles the request still leaves it settled."""

    def setUp(self):
        from hq.platform.application.adoption_testing import managing_everything

        managing_everything()
        ManagedResource.objects.create(key="photos", kind=REWRITE, spec=dict(RECORD))
        self.held = held_for("infrastructure.resource.remove", reconcile_payload(), "photos")

    def posted(self, decision: str) -> dict:
        return run_capability(
            f"approval.{decision}",
            {"approval_id": str(self.held.id)},
            principal=an_operator(),
            idempotency_key=f"example-{decision}-key",
        ).answer

    def settled_as(self, state: str) -> None:
        self.held.refresh_from_db()
        self.assertEqual(self.held.state, state)
        said = AuditLog.objects.filter(object_type__in=AUDIT_LABELS, object_id=str(self.held.id))
        self.assertTrue(said.exclude(action=AuditLog.Action.CREATED).exists(), "the settle is in the audit log")

    def test_a_moved_baseline_is_refused_and_stays_stale(self):
        ManagedResource.objects.filter(key="photos").update(spec={**RECORD, "answer": "192.0.2.20"})

        self.assertFalse(self.posted("approve")["ok"])
        self.settled_as(ApprovalRequest.State.STALE)

    def lapsed(self, decision: str) -> None:
        ApprovalRequest.objects.filter(pk=self.held.pk).update(
            expires_at=timezone.now() - timezone.timedelta(minutes=1)
        )

        self.assertFalse(self.posted(decision)["ok"])
        self.settled_as(ApprovalRequest.State.EXPIRED)

    def test_approving_a_lapsed_request_is_refused_and_it_stays_lapsed(self):
        self.lapsed("approve")

    def test_rejecting_a_lapsed_request_is_refused_and_it_stays_lapsed(self):
        self.lapsed("reject")
