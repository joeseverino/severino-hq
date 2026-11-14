"""One approval of a change to a record applies it live, whatever its kind.

A kind applied only by hand has HQ's whole copy written over what is live, so
the apply is held to the change approved: it runs only over a live record read
since the approval as the copy the change replaced.
"""

from django.test import TestCase

from hq.domains.control_plane.models import ApprovalRequest, ManagedResource, OperationRequest

from ..approvals import ApproveCommand, RejectCommand, approve, reject
from ..capabilities import execute_capability
from ..controller import ControllerReport, claim_next_operation, report_operation
from ..infrastructure import ManagedResourceCommand, is_drifted, save_managed_resource
from ..security import cli_principal, mcp_principal
from ..sweep import record_sweep
from .test_approvals import (
    GATED_KIND,
    POLICY_KEY,
    UNGATED_KIND,
    an_operator,
    declare_policy,
    policy_document,
    update_payload,
)
from .writers import keyed

CONTROLLER = "controller-example"


class ApprovedChangeAppliesTests(TestCase):
    def setUp(self):
        from hq.platform.application.adoption_testing import managing_everything

        managing_everything()
        self.resource = declare_policy()
        self.live(policy_document())

    def live(self, document):
        """A sweep that reads ``document`` as the tailnet's policy."""

        record_sweep(
            {GATED_KIND: {"ok": True, "records": [{"record": "policy", "document": document}]}},
            principal=cli_principal(),
        )

    def ask(self, document):
        answer = execute_capability(
            "infrastructure.resource.update",
            keyed(update_payload(document)),
            principal=mcp_principal(),
            target=POLICY_KEY,
        )
        self.assertEqual(answer.get("status"), "awaiting_approval", answer)
        return ApprovalRequest.objects.get()

    def approved(self, document):
        held = self.ask(document)
        approve(ApproveCommand(str(held.id)), principal=an_operator())
        return held

    def claim(self):
        return claim_next_operation(CONTROLLER, capabilities=((GATED_KIND, "reconcile"),))["operation"]

    def test_an_approved_change_is_applied_live_without_a_second_request(self):
        wanted = policy_document("group:elsewhere")
        self.approved(wanted)

        operation = OperationRequest.objects.get()
        self.assertEqual(operation.action, OperationRequest.Action.RECONCILE)
        # The apply is the approved call's own: who asked, and who agreed.
        self.assertEqual(operation.requested_interface, "mcp")
        self.assertEqual(operation.input["approved_by"], "operator")
        # Nobody is asked a second time.
        self.assertEqual(ApprovalRequest.objects.count(), 1)
        # Held for a read made after the approval, which the controller's pass makes.
        self.assertIsNone(self.claim())
        self.live(policy_document())
        claimed = self.claim()
        self.assertEqual(claimed["id"], str(operation.id))

        report_operation(
            claimed["id"],
            ControllerReport(
                success=True,
                observed_generation=operation.input["generation"],
                status={"applied": True, "document": wanted},
                conditions=[{"type": "Ready", "status": True, "reason": "Reconciled", "message": ""}],
            ),
            controller_id=CONTROLLER,
        )
        self.live(wanted)

        self.resource.refresh_from_db()
        self.assertEqual(self.resource.spec["document"], wanted)
        self.assertFalse(is_drifted(self.resource))
        operation.refresh_from_db()
        self.assertEqual(operation.state, OperationRequest.State.SUCCEEDED)

    def test_a_live_change_after_the_approval_refuses_the_apply(self):
        self.approved(policy_document("group:elsewhere"))
        # Changed at the provider between the approval and the apply.
        self.live(policy_document("group:changed-live"))

        self.assertIsNone(self.claim())

        operation = OperationRequest.objects.get()
        self.assertEqual(operation.state, OperationRequest.State.FAILED)
        self.assertEqual(operation.result["reason"], "Drifted")

    def test_a_live_record_already_unlike_hqs_copy_is_not_overwritten(self):
        # The approval compared HQ's copy before and after, not the live policy.
        self.live(policy_document("group:changed-live"))
        self.approved(policy_document("group:elsewhere"))
        self.live(policy_document("group:changed-live"))

        self.assertIsNone(self.claim())
        self.assertEqual(OperationRequest.objects.get().state, OperationRequest.State.FAILED)

    def test_a_rejected_change_applies_nothing(self):
        held = self.ask(policy_document("group:elsewhere"))

        reject(RejectCommand(str(held.id)), principal=an_operator())

        self.assertFalse(OperationRequest.objects.exists())
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.spec["document"], policy_document())

    def test_a_kind_the_controller_converges_is_applied_at_once(self):
        ManagedResource.objects.create(
            key="example-rewrite",
            kind=UNGATED_KIND,
            spec={"domain": "example.test", "answer": "192.0.2.10"},
        )

        save_managed_resource(
            ManagedResourceCommand(
                key="example-rewrite",
                kind=UNGATED_KIND,
                spec={"domain": "example.test", "answer": "192.0.2.11"},
            ),
            principal=an_operator(),
            current_key="example-rewrite",
        )

        operation = OperationRequest.objects.get()
        claimed = claim_next_operation(CONTROLLER, capabilities=((UNGATED_KIND, "reconcile"),))
        self.assertEqual(claimed["operation"]["id"], str(operation.id))
