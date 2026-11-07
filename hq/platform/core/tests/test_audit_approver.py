"""A change someone agreed to says, on each of its rows, who agreed."""

from django.test import TestCase

from hq.domains.control_plane.models import ApprovalRequest
from hq.platform.application.approvals import ApproveCommand, approve
from hq.platform.application.capabilities import execute_capability
from hq.platform.application.security import Principal, cli_principal, mcp_principal
from hq.platform.application.tests.test_approvals import POLICY_KEY, an_operator, declare_policy, reconcile_payload
from hq.platform.core.audit import audit_operation, audited_labels, record_event
from hq.platform.core.models import AuditLog


class ApproverTests(TestCase):
    def test_each_row_an_approved_change_writes_names_who_approved_it(self):
        from hq.platform.application.adoption_testing import managing_everything

        managing_everything()
        declare_policy()
        answer = execute_capability(
            "infrastructure.resource.reconcile", reconcile_payload(), principal=mcp_principal(), target=POLICY_KEY
        )
        self.assertTrue(answer["ok"], answer)
        held = ApprovalRequest.objects.get()

        approve(ApproveCommand(str(held.id)), principal=an_operator())

        # The request's own rows were written before anyone agreed.
        effects = AuditLog.objects.filter(operation_id=str(held.id), actor=held.requested_actor).exclude(
            object_type=audited_labels()[ApprovalRequest]
        )
        self.assertTrue(effects.exists(), "the approved change wrote no row as its requester")
        for row in effects:
            with self.subTest(row=row.object_repr):
                self.assertEqual(row.metadata.get("approved_by"), "operator")

    def test_work_nested_in_an_approved_operation_keeps_its_approver(self):
        consented = Principal("example-agent", "mcp", frozenset(), approved_by="example-person")

        with (
            audit_operation(operation="example.outer", principal=consented),
            audit_operation(operation="example.inner"),
        ):
            row = record_event(action=AuditLog.Action.UPDATED, type_label="Example", required=True)

        self.assertEqual(row.metadata["approved_by"], "example-person")

    def test_a_row_nobody_had_to_approve_names_no_approver(self):
        with audit_operation(operation="example.edit", principal=cli_principal()):
            row = record_event(action=AuditLog.Action.UPDATED, type_label="Example", required=True)

        self.assertNotIn("approved_by", row.metadata)
