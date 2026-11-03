"""Both brakes, the Agents pause and a person's block on one client, hold what
an agent already asked for: its pending hold cannot be approved and its queued
work is not claimed, while it stays declinable and every other caller's work
runs. Lifting the brake releases both as they stand."""

from contextlib import contextmanager

from django.test import TestCase

from hq.domains.control_plane.models import ApprovalRequest, OperationRequest
from hq.platform.core.models import AgentAccess, AgentIdentity

from ..approvals import ApprovalError, ApproveCommand, RejectCommand, approve, reject
from ..capabilities import execute_capability
from ..controller import claim_next_operation
from ..security import Capability, Principal
from .test_approvals import GATED_KIND, POLICY_KEY, an_operator, declare_policy, reconcile_payload

AGENT = "example-agent"
OTHER = "example-other-agent"
GRANT = frozenset({Capability.READ, Capability.MANAGE_INFRASTRUCTURE})


@contextmanager
def _paused():
    AgentAccess.objects.update_or_create(pk=1, defaults={"paused": True})
    yield
    AgentAccess.objects.filter(pk=1).update(paused=False)


@contextmanager
def _blocked():
    AgentIdentity.objects.filter(client_id=AGENT).update(blocked_at="2026-11-01T12:00:00Z")
    yield
    AgentIdentity.objects.filter(client_id=AGENT).update(blocked_at=None)


BRAKES = {"pause": _paused, "block": _blocked}


class BrakesHoldAskedWorkTests(TestCase):
    def setUp(self):
        from hq.platform.application.adoption_testing import managing_everything

        managing_everything()
        declare_policy()
        self.operator = an_operator()
        for client in (AGENT, OTHER):
            AgentIdentity.objects.create(client_id=client, interfaces=["mcp"], granted=["read"])

    def _held(self, key: str, actor: str = AGENT) -> ApprovalRequest:
        agent = Principal(actor, "mcp", GRANT)
        answer = execute_capability(
            "infrastructure.reconcile", reconcile_payload(key), principal=agent, target=POLICY_KEY
        )
        self.assertTrue(answer["ok"], answer)
        return ApprovalRequest.objects.get(state=ApprovalRequest.State.PENDING, requested_actor=actor)

    def _claim(self):
        return claim_next_operation("controller-example", capabilities=((GATED_KIND, "reconcile"),))["operation"]

    def test_a_held_request_cannot_be_approved_until_the_brake_lifts(self):
        for name, brake in BRAKES.items():
            with self.subTest(brake=name):
                held = self._held(f"approve-{name}")
                with brake(), self.assertRaises(ApprovalError):
                    approve(ApproveCommand(str(held.id)), principal=self.operator)
                held.refresh_from_db()
                self.assertEqual(held.state, ApprovalRequest.State.PENDING)

                approve(ApproveCommand(str(held.id)), principal=self.operator)
                held.refresh_from_db()
                self.assertEqual(held.state, ApprovalRequest.State.APPROVED)
                OperationRequest.objects.all().delete()

    def test_a_held_request_stays_declinable(self):
        for name, brake in BRAKES.items():
            with self.subTest(brake=name):
                held = self._held(f"decline-{name}")
                with brake():
                    reject(RejectCommand(str(held.id)), principal=self.operator)
                held.refresh_from_db()
                self.assertEqual(held.state, ApprovalRequest.State.REJECTED)

    def test_queued_work_is_not_claimed_until_the_brake_lifts(self):
        for name, brake in BRAKES.items():
            with self.subTest(brake=name):
                approve(ApproveCommand(str(self._held(f"claim-{name}").id)), principal=self.operator)
                with brake():
                    self.assertIsNone(self._claim())
                operation = OperationRequest.objects.get(requested_actor=AGENT)
                self.assertEqual(operation.state, OperationRequest.State.QUEUED)

                self.assertEqual(self._claim()["id"], str(operation.id))
                OperationRequest.objects.all().delete()

    def test_a_block_holds_only_its_own_client(self):
        approve(ApproveCommand(str(self._held("other", actor=OTHER).id)), principal=self.operator)
        mine = self._held("mine")
        with _blocked():
            approve(ApproveCommand(str(self._held("other-again", actor=OTHER).id)), principal=self.operator)
            with self.assertRaises(ApprovalError):
                approve(ApproveCommand(str(mine.id)), principal=self.operator)
            self.assertIsNotNone(self._claim())

    def test_a_block_does_not_hold_another_client_that_asked_the_same_change(self):
        self._held("same")
        theirs = self._held("same", actor=OTHER)
        with _blocked():
            approve(ApproveCommand(str(theirs.id)), principal=self.operator)
        self.assertEqual(OperationRequest.objects.get().requested_actor, OTHER)

    def test_a_person_named_like_a_blocked_client_is_not_held(self):
        person = an_operator(AGENT)
        answer = execute_capability(
            "infrastructure.reconcile", reconcile_payload("person"), principal=person, target=POLICY_KEY
        )
        self.assertTrue(answer["ok"], answer)
        with _blocked():
            self.assertIsNotNone(self._claim())
