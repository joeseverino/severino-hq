"""A held request goes stale when what it would apply, or what its page showed,
moves; never when the controller only reads the same record again."""

from contextlib import suppress

from django.test import TestCase
from django.utils import timezone

from hq.domains.control_plane.models import ApprovalRequest
from hq.platform.core.models import AuditLog
from hq.platform.core.testing import declare

from ..approval_review import preview
from ..approvals import AUDIT_LABEL, ApprovalError, ApproveCommand, approve
from ..capabilities import execute_capability, run_capability
from ..security import Principal, mcp_principal
from .test_approvals import POLICY_KEY, an_operator, declare_policy, policy_document, reconcile_payload

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


class LiveComparisonStalenessTests(TestCase):
    """An apply is shown against the live record, so that record counts."""

    def setUp(self):
        from hq.platform.core.testing import managing_everything

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


class SettledThroughTheRunnerTests(TestCase):
    """A decision a person posts carries a retry key, so it runs inside the key's
    transaction; a refusal that settles the request still leaves it settled."""

    def setUp(self):
        from hq.platform.core.testing import managing_everything

        managing_everything()
        declare("photos", REWRITE, dict(RECORD))
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
        said = AuditLog.objects.filter(object_type=AUDIT_LABEL, object_id=str(self.held.id))
        self.assertTrue(said.exclude(action=AuditLog.Action.CREATED).exists(), "the settle is in the audit log")

    def lapsed(self, decision: str) -> None:
        ApprovalRequest.objects.filter(pk=self.held.pk).update(
            expires_at=timezone.now() - timezone.timedelta(minutes=1)
        )

        self.assertFalse(self.posted(decision)["ok"])
        self.settled_as(ApprovalRequest.State.EXPIRED)

    def test_approving_a_lapsed_request_is_refused_and_it_stays_lapsed(self):
        self.lapsed("approve")
