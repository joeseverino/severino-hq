"""Every audit row written inside one operation carries that operation's id,
so the log reads as events: one operation, with the items it changed."""

from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from hq.domains.control_plane.models import ApprovalRequest
from hq.domains.jobs.runner import run
from hq.platform.core.audit import audit_operation, operation_context, record_event
from hq.platform.core.models import AuditLog
from hq_sdk.capabilities import StrictCommand

from ..approvals import APPROVE, ApprovalError, ApproveCommand, RejectCommand, approve, lapse_expired, reject
from ..capabilities import CapabilitySpec, execute_capability, run_capability
from ..infrastructure import ManagedResourceCommand, save_managed_resource
from ..security import Capability, cli_principal, mcp_principal
from .test_approvals import POLICY_KEY, an_operator, declare_policy, policy_document, update_payload
from .writers import keyed

UNGATED_KIND = "adguard.rewrite"
NAME = "example.rewrites.pair"


class _Pair(StrictCommand):
    prefix: str


def _declare_pair(command: _Pair, *, principal) -> dict:
    """A capability whose service writes several rows, each through a binding of its own."""

    for key in (f"{command.prefix}-one", f"{command.prefix}-two"):
        save_managed_resource(
            ManagedResourceCommand(
                key=key, kind=UNGATED_KIND, spec={"domain": f"{key}.example", "answer": "192.0.2.10"}
            ),
            principal=principal,
            copied_from_live=True,
        )
    return {"ok": True}


PAIR = CapabilitySpec(
    NAME,
    "Declare two example rewrites.",
    "remote_write",
    Capability.MANAGE_INFRASTRUCTURE,
    _Pair,
    _declare_pair,
)


def rows_after(mark: int):
    return AuditLog.objects.filter(pk__gt=mark).order_by("pk")


def last_row() -> int:
    return AuditLog.objects.order_by("-pk").values_list("pk", flat=True).first() or 0


class CapabilityRunTests(TestCase):
    def run_pair(self, prefix: str) -> list[AuditLog]:
        mark = last_row()
        with mock.patch("hq.platform.application.capabilities.capability_registry", return_value={NAME: PAIR}):
            result = execute_capability(NAME, {"prefix": prefix}, principal=cli_principal())
        self.assertTrue(result["ok"], result)
        return list(rows_after(mark))

    def test_every_row_one_run_writes_carries_one_id_and_another_run_another(self):
        first = self.run_pair("example-first")
        second = self.run_pair("example-second")

        for rows in (first, second):
            self.assertGreaterEqual(len(rows), 2)
            # Written by the service's own binding, nested in the run.
            self.assertEqual({row.metadata["operation"] for row in rows}, {"infrastructure.resource.create"})
        first_ids = {row.operation_id for row in first}
        second_ids = {row.operation_id for row in second}
        self.assertEqual(len(first_ids), 1)
        self.assertEqual(len(second_ids), 1)
        self.assertNotIn("", first_ids | second_ids)
        self.assertNotEqual(first_ids, second_ids)


class BindingTests(TestCase):
    def test_a_nested_binding_keeps_the_outer_id_and_an_explicit_one_wins(self):
        mark = last_row()
        with audit_operation(operation="example.outer", principal=cli_principal()):
            record_event(action=AuditLog.Action.UPDATED, type_label="Example", message="outer")
            with audit_operation(operation="example.inner", principal=mcp_principal()):
                record_event(action=AuditLog.Action.UPDATED, type_label="Example", message="inner")
                with operation_context(
                    interface="cli", actor="example", operation="example.own", operation_id="example-own"
                ):
                    record_event(action=AuditLog.Action.UPDATED, type_label="Example", message="own")

        outer, inner, own = rows_after(mark)
        self.assertTrue(outer.operation_id)
        self.assertEqual(inner.operation_id, outer.operation_id)
        # The inner binding's own name and caller stand.
        self.assertEqual((inner.metadata["operation"], inner.metadata["interface"]), ("example.inner", "mcp"))
        self.assertEqual(own.operation_id, "example-own")


@override_settings(SEVERINO_AGENT_ENABLE_INFRASTRUCTURE=True)
class ApprovalLifeTests(TestCase):
    """A request, its decision, the change it applies and its lapse are one operation."""

    def setUp(self):
        from hq.platform.application.adoption_testing import managing_everything

        managing_everything()
        self.resource = declare_policy()
        mark = last_row()
        answer = execute_capability(
            "infrastructure.resource.update",
            keyed(update_payload(policy_document("group:elsewhere"))),
            principal=mcp_principal(),
            target=POLICY_KEY,
        )
        self.assertTrue(answer["ok"], answer)
        self.held = ApprovalRequest.objects.get()
        self.asked = list(rows_after(mark))

    def assert_one_operation(self, rows):
        self.assertEqual({row.operation_id for row in rows}, {str(self.held.id)})

    def test_the_request_is_the_approvals_own_operation(self):
        self.assertTrue(self.asked)
        self.assert_one_operation(self.asked)

    def test_approving_through_the_capability_writes_every_row_under_the_approvals_id(self):
        mark = last_row()
        answer = run_capability(
            "approval.approve",
            {"approval_id": str(self.held.id)},
            principal=an_operator(),
            idempotency_key="example-approve",
        ).answer
        self.assertTrue(answer["ok"], answer)

        decided = list(rows_after(mark))
        self.assertTrue(
            any(row.object_type == "Approval" and row.metadata["operation"] == APPROVE for row in decided),
            "the decision is recorded",
        )
        self.assertTrue(any(row.object_type == "Managed resource" for row in decided), "the change is recorded")
        self.assert_one_operation(self.asked + decided)

    def test_rejecting_is_part_of_the_approval(self):
        mark = last_row()
        reject(RejectCommand(str(self.held.id), note="Not wanted."), principal=an_operator())

        decided = list(rows_after(mark))
        self.assertTrue(decided)
        self.assert_one_operation(self.asked + decided)

    def lapse(self):
        self.held.expires_at = timezone.now() - timezone.timedelta(minutes=1)
        # Written past the signal, so the only rows after the mark are the lapse's.
        ApprovalRequest.objects.filter(pk=self.held.pk).update(expires_at=self.held.expires_at)

    def test_the_sweeps_lapse_is_part_of_the_approval_whatever_binds_the_sweep(self):
        self.lapse()
        mark = last_row()
        with operation_context(interface="job", actor="example", operation="example.due", operation_id="example-job"):
            self.assertEqual(lapse_expired(), 1)

        lapsed = list(rows_after(mark))
        self.assertTrue(lapsed, "a lapse is audited")
        self.assert_one_operation(self.asked + lapsed)

    def test_a_lapse_found_on_deciding_is_part_of_the_approval(self):
        self.lapse()
        mark = last_row()
        with self.assertRaises(ApprovalError):
            approve(ApproveCommand(str(self.held.id)), principal=an_operator())

        lapsed = list(rows_after(mark))
        self.assertTrue(lapsed)
        self.assert_one_operation(self.asked + lapsed)


class JobTests(TestCase):
    def test_a_capability_run_inside_a_job_writes_under_the_jobs_id(self):
        def work(progress):
            with mock.patch("hq.platform.application.capabilities.capability_registry", return_value={NAME: PAIR}):
                return execute_capability(NAME, {"prefix": "example-job"}, principal=cli_principal())

        mark = last_row()
        job = run("example.pair", "Pair", work, actor="example-timer")

        written = [
            row for row in rows_after(mark) if not (row.object_type == "Background job" and row.action == "created")
        ]
        self.assertGreaterEqual(len(written), 3, "the capability's rows and the job's ending")
        self.assertEqual({row.operation_id for row in written}, {str(job.pk)})
