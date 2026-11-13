"""A held request is its asker's to take back while it waits: withdrawn, it
applies nothing, nobody is asked to decide it, it frees its place among the
requests its asker may hold, and its asker reads it as withdrawn. Nobody
else may withdraw it."""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from hq.domains.control_plane.models import ApprovalRequest, OperationRequest
from hq.platform.core.models import AuditLog

from ..approvals import MAX_PENDING_PER_ACTOR, ApproveCommand, approve, pending
from ..capabilities import run_capability
from ..held_reads import get_held
from ..security import Capability, Principal, web_principal
from .procedures import RESTART_RUN, Supply, manifest, run_payload

WITHDRAW = "approval.withdraw"
AGENT = Principal("example-agent", "mcp", frozenset({Capability.READ, Capability.MANAGE_INFRASTRUCTURE}))
OTHER_AGENT = Principal("example-other-agent", "mcp", frozenset({Capability.READ, Capability.MANAGE_INFRASTRUCTURE}))


class WithdrawTestCase(TestCase):
    def setUp(self):
        self.supply = Supply(self, manifest())
        self.keys = iter(range(1000))

    def hold(self, *, principal=AGENT, service="web"):
        answer = run_capability(
            RESTART_RUN, run_payload(service=service), principal=principal, idempotency_key=f"ask-{next(self.keys)}"
        ).answer
        self.assertEqual(answer.get("status"), "awaiting_approval", answer)
        return answer["approval"]["id"]

    def withdraw(self, approval_id, *, principal=AGENT):
        return run_capability(
            WITHDRAW, {"approval_id": approval_id}, principal=principal, idempotency_key=f"back-{next(self.keys)}"
        ).answer

    def refused(self, approval_id, code, **asked):
        answer = self.withdraw(approval_id, **asked)
        self.assertFalse(answer["ok"], answer)
        self.assertEqual(answer["error"]["code"], code, answer)

    def state(self, approval_id):
        return ApprovalRequest.objects.get(pk=approval_id).state


class AskerWithdrawsTests(WithdrawTestCase):
    def test_the_asker_withdraws_its_request_and_nothing_is_applied(self):
        held = self.hold()

        answer = self.withdraw(held)

        self.assertEqual(answer, {"ok": True, "withdrawn": held}, answer)
        self.assertEqual(self.state(held), "withdrawn")
        self.assertFalse(OperationRequest.objects.exists())
        self.assertNotIn(held, [str(waiting.pk) for waiting in pending(principal=AGENT)])

    def test_its_asker_reads_it_as_withdrawn(self):
        held = self.hold()
        self.withdraw(held)

        read = get_held(held, principal=AGENT)

        self.assertTrue(read["done"])
        self.assertEqual(read["answer"]["error"]["code"], "withdrawn")

    def test_the_withdrawal_is_audited_under_the_request_by_its_asker(self):
        held = self.hold()

        self.withdraw(held)

        row = AuditLog.objects.filter(object_id=held, metadata__operation=WITHDRAW).get()
        self.assertEqual(row.metadata["actor"], AGENT.actor)

    def test_a_withdrawn_request_frees_its_place_among_those_its_asker_may_hold(self):
        held = [self.hold(service=f"web-{index}") for index in range(MAX_PENDING_PER_ACTOR)]
        full = run_capability(
            RESTART_RUN, run_payload(service="web-over"), principal=AGENT, idempotency_key="ask-over"
        ).answer
        self.assertEqual(full["error"]["code"], "too_many_pending_approvals", full)

        self.withdraw(held[0])

        self.hold(service="web-over")


class RefusedWithdrawTests(WithdrawTestCase):
    def test_another_caller_s_request_is_no_request(self):
        held = self.hold()
        person = web_principal(get_user_model().objects.create_superuser("owner", "owner@example.test", "pw"))

        for other in (OTHER_AGENT, person):
            with self.subTest(other.actor):
                self.refused(held, "not_found", principal=other)
        self.assertEqual(self.state(held), ApprovalRequest.State.PENDING)

    def test_a_decided_request_cannot_be_withdrawn(self):
        held = self.hold()
        person = get_user_model().objects.create_superuser("owner", "owner@example.test", "pw")
        approve(ApproveCommand(held), principal=web_principal(person))

        self.refused(held, "precondition")
        self.assertEqual(self.state(held), ApprovalRequest.State.APPROVED)

    def test_a_withdrawn_request_cannot_be_approved(self):
        held = self.hold()
        self.withdraw(held)
        person = get_user_model().objects.create_superuser("owner", "owner@example.test", "pw")

        answer = run_capability(
            "approval.approve", {"approval_id": held}, principal=web_principal(person), idempotency_key="approve"
        ).answer

        self.assertFalse(answer["ok"], answer)
        self.assertFalse(OperationRequest.objects.exists())

    def test_an_expired_request_lapses_instead(self):
        held = self.hold()
        ApprovalRequest.objects.filter(pk=held).update(expires_at=timezone.now() - timedelta(minutes=1))

        self.refused(held, "precondition")
        self.assertEqual(self.state(held), ApprovalRequest.State.EXPIRED)
