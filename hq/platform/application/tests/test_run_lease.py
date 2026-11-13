"""A claimed run holds its lease only while its worker renews it, stops when HQ
says stop, is never run again blindly once the lease lapses, and lands its
report as bounded evidence on the run, never as its record's status. Every
procedure here is a synthetic ``example.*`` one."""

import json
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from hq.domains.control_plane.models import ApprovalRequest, ManagedResource, OperationRequest
from hq.domains.control_plane.tests import bridge_client
from hq.platform.core.audit import audited_labels
from hq.platform.core.models import AuditLog

from ..capabilities import run_capability
from ..controller import ControllerReport, claim_next_operation, report_operation
from ..security import Capability, Principal
from .procedures import APP, DB, RESTART_KEY, RESTART_RUN, Supply, manifest, run_payload

AGENT = Principal("example-agent", "mcp", frozenset({Capability.READ, Capability.MANAGE_INFRASTRUCTURE}))
WORKER = "example-worker"
RUNS = (("automation.operation", "run"),)
PAUSED = "hq.platform.application.agent_access.paused_interfaces"


def renew(operation_id: str, *, controller_id: str = WORKER, lease_seconds: int = 300):
    from ..controller import renew_operation

    return renew_operation(operation_id, controller_id=controller_id, lease_seconds=lease_seconds)


def event(outcome: str = "changed", *, host: str = APP, message: str = "Restarted.") -> dict:
    return {"host": host, "task": "Restart the service", "outcome": outcome, "message": message}


class LeasedRunTestCase(TestCase):
    retry = "never"

    def setUp(self):
        self.supply = Supply(self, manifest(retry=self.retry))
        answer = run_capability(RESTART_RUN, run_payload(), principal=AGENT, idempotency_key="example-run").answer
        self.assertTrue(answer["queued"], answer)
        self.operation_id = answer["operation"]["id"]

    def claim(self, **options):
        return claim_next_operation(WORKER, capabilities=RUNS, **options)["operation"]

    def operation(self) -> OperationRequest:
        return OperationRequest.objects.get(pk=self.operation_id)

    def lapse(self) -> None:
        OperationRequest.objects.filter(pk=self.operation_id).update(
            lease_expires_at=timezone.now() - timedelta(seconds=1)
        )

    def report(self, *, success: bool = True, **fields):
        operation = self.operation()
        return report_operation(
            self.operation_id,
            ControllerReport(success=success, observed_generation=operation.input["generation"], **fields),
            controller_id=WORKER,
        )


class RenewTests(LeasedRunTestCase):
    def test_renewing_moves_the_lease_and_says_go_on(self):
        self.claim(lease_seconds=60)
        before = self.operation().lease_expires_at

        answer = renew(self.operation_id, lease_seconds=3600)

        after = self.operation().lease_expires_at
        self.assertTrue(answer["continue"], answer)
        self.assertGreater(after, before)
        self.assertEqual(answer["lease_expires_at"], after.isoformat())
        self.assertEqual(self.operation().state, OperationRequest.State.CLAIMED)

    def test_another_controller_cannot_renew_it(self):
        self.claim()
        before = self.operation().lease_expires_at

        with self.assertRaisesMessage(ValueError, "different controller"):
            renew(self.operation_id, controller_id="example-other")

        self.assertEqual(self.operation().lease_expires_at, before)

    def test_a_lease_that_ran_out_is_not_renewed(self):
        self.claim()
        self.lapse()

        with self.assertRaisesMessage(ValueError, "expired"):
            renew(self.operation_id)

    def test_work_nobody_claimed_is_not_renewed(self):
        with self.assertRaisesMessage(ValueError, "not claimed"):
            renew(self.operation_id)
        self.assertEqual(self.operation().state, OperationRequest.State.QUEUED)

    def test_invalid_parameters_are_refused(self):
        self.claim()
        for asked in ({"lease_seconds": 29}, {"lease_seconds": 3601}, {"controller_id": ""}):
            with self.subTest(asked=asked), self.assertRaises(ValueError):
                renew(self.operation_id, **asked)
        for operation_id in ("not-an-id", "00000000-0000-0000-0000-000000000000"):
            with self.subTest(operation_id=operation_id), self.assertRaisesMessage(ValueError, "not found"):
                renew(operation_id)

    def test_the_bridge_refuses_a_renewal_without_its_operation_or_with_a_lease_out_of_range(self):
        missing = bridge_client.request("/renew", query={"controller-id": WORKER})
        self.assertEqual(missing.status, 400, missing.body)
        self.claim()
        too_long = bridge_client.request(
            "/renew", query={"controller-id": WORKER, "operation": self.operation_id, "lease-seconds": 3601}
        )
        self.assertEqual(too_long.status, 400, too_long.body)

        answer = bridge_client.call("renew", controller_id=WORKER, operation=self.operation_id)

        self.assertTrue(answer["continue"], answer)

    def test_under_the_pause_it_is_told_to_stop_and_keeps_time_to_report(self):
        self.claim(lease_seconds=60)
        before = self.operation().lease_expires_at

        with patch(PAUSED, return_value=frozenset({"mcp"})):
            answer = renew(self.operation_id)

        self.assertFalse(answer["continue"], answer)
        self.assertEqual(answer["reason"], "Held")
        self.assertGreater(self.operation().lease_expires_at, before)
        self.report(success=False, message="Stopped for the pause.")
        self.assertEqual(self.operation().state, OperationRequest.State.FAILED)

    def test_a_cancelled_run_is_told_to_stop(self):
        self.claim()
        OperationRequest.objects.filter(pk=self.operation_id).update(
            state=OperationRequest.State.CANCELLED, completed_at=timezone.now(), lease_expires_at=None
        )

        answer = renew(self.operation_id)

        self.assertFalse(answer["continue"], answer)
        self.assertEqual(answer["reason"], "Cancelled")


class ClaimTests(LeasedRunTestCase):
    def test_a_claim_naming_nothing_claims_nothing(self):
        from ..controller import peek_next_operation

        self.assertIsNone(claim_next_operation(WORKER, capabilities=())["operation"])
        self.assertIsNone(peek_next_operation(capabilities=())["operation"])
        self.assertIsNone(bridge_client.call("claim", controller_id=WORKER)["operation"])
        self.assertEqual(self.operation().state, OperationRequest.State.QUEUED)

    def test_a_claim_retried_under_its_attempt_is_the_same_lease(self):
        first = self.claim(attempt="example-attempt-1")

        again = self.claim(attempt="example-attempt-1")

        self.assertEqual(again["id"], first["id"])
        self.assertEqual(self.operation().attempt_count, 1)
        self.assertIsNone(self.claim(attempt="example-attempt-2"))
        self.assertIsNone(
            claim_next_operation("example-other", capabilities=RUNS, attempt="example-attempt-1")["operation"]
        )

    def test_a_duplicate_report_is_refused(self):
        self.claim()
        self.report(events=[event()])

        with self.assertRaisesMessage(ValueError, "not claimed"):
            self.report(events=[event()])


class HeldApprovalTests(TestCase):
    def test_a_run_waiting_for_a_person_is_not_claimable(self):
        from ..security import mcp_principal

        Supply(self, manifest(effect="destructive"))
        answer = run_capability(
            RESTART_RUN, run_payload(), principal=mcp_principal(), idempotency_key="example-run"
        ).answer
        self.assertEqual(answer.get("status"), "awaiting_approval", answer)

        self.assertIsNone(claim_next_operation(WORKER, capabilities=RUNS)["operation"])
        self.assertEqual(ApprovalRequest.objects.get().state, ApprovalRequest.State.PENDING)


class LapsedRunTests(LeasedRunTestCase):
    def test_a_lapsed_run_is_failed_as_its_outcome_unknown(self):
        self.claim()
        self.lapse()

        self.assertIsNone(self.claim())

        operation = self.operation()
        self.assertEqual(operation.state, OperationRequest.State.FAILED)
        self.assertEqual(operation.result["reason"], "OutcomeUnknown")
        self.assertEqual(operation.attempt_count, 1)
        ended = AuditLog.objects.filter(
            object_type=audited_labels()[OperationRequest], object_id=self.operation_id, operation_id=self.operation_id
        ).latest("pk")
        self.assertIn("unknown", ended.message)


class ObserveFirstLapsedRunTests(LeasedRunTestCase):
    retry = "observe_first"

    def test_it_fails_naming_observe_first_and_is_not_run_again(self):
        self.claim()
        self.lapse()

        self.assertIsNone(self.claim())

        operation = self.operation()
        self.assertEqual(
            (operation.state, operation.result["reason"]), (OperationRequest.State.FAILED, "OutcomeUnknown")
        )
        self.assertEqual(operation.result["retry"], "observe_first")
        self.assertIn("observes", operation.result["message"])


class SafeLapsedRunTests(LeasedRunTestCase):
    retry = "safe"

    def test_a_run_safe_to_repeat_is_queued_again(self):
        self.claim()
        self.lapse()

        again = self.claim()

        self.assertEqual(again["id"], self.operation_id)
        self.assertEqual(self.operation().attempt_count, 2)


class RunReportTests(LeasedRunTestCase):
    def setUp(self):
        super().setUp()
        self.claim()
        self.record = ManagedResource.objects.get(key=RESTART_KEY)

    def test_a_run_s_report_lands_on_the_run_and_leaves_its_record_alone(self):
        events = [event(), event("ok", host=DB, message="Already running.")]

        self.report(events=events, message="Restarted on one machine.")

        operation = self.operation()
        self.assertEqual(operation.state, OperationRequest.State.SUCCEEDED)
        self.assertEqual(operation.result["events"], events)
        record = ManagedResource.objects.get(key=RESTART_KEY)
        self.assertEqual(
            (record.status, record.conditions, record.observed_generation, record.last_observed_at),
            (self.record.status, self.record.conditions, self.record.observed_generation, self.record.last_observed_at),
        )

    def test_a_status_for_the_record_is_refused(self):
        with self.assertRaisesMessage(ValueError, "events"):
            self.report(status={"service": "web"})
        self.assertEqual(self.operation().state, OperationRequest.State.CLAIMED)

    def test_task_outcomes_are_audited_as_the_run_s_own_and_lead_back_to_the_ask(self):
        asked = AuditLog.objects.get(
            object_type=audited_labels()[OperationRequest], object_id=self.operation_id, action=AuditLog.Action.CREATED
        )

        self.report(success=False, events=[event(), event("failed", host=DB), event("ok", host=DB)])

        outcomes = AuditLog.objects.filter(
            object_id=self.operation_id, action__in=(AuditLog.Action.DONE, AuditLog.Action.FAILED)
        )
        self.assertEqual(
            sorted((row.action, row.metadata["event"]["host"]) for row in outcomes),
            [(AuditLog.Action.DONE, APP), (AuditLog.Action.FAILED, DB)],
        )
        for row in outcomes:
            self.assertEqual(row.operation_id, self.operation_id)
            self.assertEqual(row.metadata["cause"], asked.operation_id)

    def test_a_report_over_the_contract_s_bound_is_refused_at_the_bridge(self):
        from hq.domains.control_plane.bridge_contract import limit

        bound = limit("ControllerReport", "properties", "events", "maxItems")
        body = {
            "success": True,
            "observed_generation": self.operation().input["generation"],
            "events": [event()] * (bound + 1),
        }
        answer = bridge_client.request(
            "/report",
            query={"controller-id": WORKER, "operation": self.operation_id},
            body=json.dumps(body).encode(),
        )

        self.assertEqual(answer.status, 422, answer.body)
        self.assertEqual(self.operation().state, OperationRequest.State.CLAIMED)

    def test_events_from_work_other_than_a_run_are_refused(self):
        OperationRequest.objects.filter(pk=self.operation_id).update(action=OperationRequest.Action.RECONCILE)

        with self.assertRaisesMessage(ValueError, "Only a run"):
            self.report(events=[event()])
