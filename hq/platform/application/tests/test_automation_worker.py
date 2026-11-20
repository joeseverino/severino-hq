"""HQ's playbook worker is known by its claims: one never seen, or not seen
within its timer's interval, takes no scan or run, so each is refused with
the reason. Every manifest here is a synthetic ``example.*`` one."""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from hq.domains.control_plane.models import ManagedResource, OperationRequest
from hq.domains.control_plane.tests import bridge_client
from hq.platform.core.models import UpstreamReading
from hq.platform.core.testing import declare

from ..capabilities import run_capability
from ..security import Capability, Principal
from .procedures import APP, RESTART_RUN, Supply, manifest, run_payload

OPERATOR = Principal("example-operator", "web", frozenset({Capability.READ, Capability.MANAGE_INFRASTRUCTURE}))
HEARTBEAT = "automation-heartbeat"
RUNS = ["automation.operation:run", "automation.source:scan"]


def seen(ago: timedelta | None) -> None:
    """The worker last arrived ``ago``, or never when None."""

    readings = UpstreamReading.objects.filter(key=HEARTBEAT)
    if ago is None:
        readings.delete()
    else:
        readings.update(observed_at=timezone.now() - ago)


class ArrivalTests(TestCase):
    """A claim of the worker's own work is the worker arriving, and the
    controller's claim is the controller's alone."""

    def test_a_claim_of_scans_and_runs_notes_the_worker_and_not_the_controller(self):
        from ..cadence import automation_standing, controller_standing

        self.assertIsNone(bridge_client.call("claim", controller_id="example-worker", capability=RUNS)["operation"])

        self.assertIsNotNone(automation_standing().seen_at)
        self.assertFalse(controller_standing().known)

    def test_the_controllers_claim_notes_the_controller_and_not_the_worker(self):
        from ..cadence import automation_standing, controller_standing

        bridge_client.call("claim", controller_id="example-controller")

        self.assertTrue(controller_standing().known)
        self.assertIsNone(automation_standing().seen_at)


class LeaseTests(TestCase):
    """A worker past its silence bound that still holds a run's lease is not
    silent, and its standing reads the work it was given in one query."""

    def setUp(self):
        def resource(key: str) -> ManagedResource:
            return declare(key, "automation.source")

        self.claim = OperationRequest.objects.create(
            resource=resource("example-app"),
            action=OperationRequest.Action.RUN,
            idempotency_key="example-claim",
            state=OperationRequest.State.CLAIMED,
            claimed_by="example-worker",
            claimed_at=timezone.now() - timedelta(days=1),
            lease_expires_at=timezone.now() + timedelta(minutes=5),
        )
        OperationRequest.objects.create(
            resource=resource("example-other-app"),
            action=OperationRequest.Action.RUN,
            idempotency_key="example-earlier-claim",
            state=OperationRequest.State.CLAIMED,
            claimed_by="example-worker",
            claimed_at=timezone.now() - timedelta(days=2),
            lease_expires_at=timezone.now() - timedelta(days=2),
        )
        seen(None)

    def test_a_live_lease_keeps_it_heard_and_its_end_leaves_it_silent(self):
        from ..cadence import automation_standing

        self.assertFalse(automation_standing().silent)

        OperationRequest.objects.filter(pk=self.claim.pk).update(lease_expires_at=timezone.now() - timedelta(seconds=1))
        self.assertTrue(automation_standing().silent)

    def test_its_standing_reads_the_work_once(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from ..cadence import automation_standing

        with CaptureQueriesContext(connection) as asked:
            automation_standing()

        work = [query for query in asked if "control_plane_operationrequest" in query["sql"]]
        self.assertEqual(len(work), 1)


class AbsentWorkerTests(TestCase):
    def setUp(self):
        self.supply = Supply(self, manifest())

    def run_it(self) -> dict:
        return run_capability(RESTART_RUN, run_payload(APP), principal=OPERATOR, idempotency_key="example-run").answer

    def test_never_seen_a_run_is_refused_with_the_reason(self):
        seen(None)

        answer = self.run_it()
        self.assertEqual(answer["error"]["code"], "precondition", answer)
        self.assertEqual(answer["error"]["message"], "Cannot run: no playbook worker reports to this HQ.")
        self.assertFalse(OperationRequest.objects.exists())

    def test_not_seen_within_its_interval_a_run_is_refused_with_how_long(self):
        seen(timedelta(hours=3))

        answer = self.run_it()
        # An amount and its unit are kept on one line.
        said = answer["error"]["message"].replace("\N{NO-BREAK SPACE}", " ")
        self.assertEqual(said, "Cannot run: 3 hours since a playbook worker last reported.")

    def test_seen_within_its_interval_a_run_is_queued_and_nothing_is_raised(self):
        seen(timedelta(minutes=20))

        self.assertTrue(self.run_it()["queued"])

    def test_a_retried_run_queued_before_the_worker_went_is_answered_as_it_was(self):
        first = self.run_it()
        seen(None)

        again = self.run_it()
        self.assertTrue(again["ok"], again)
        self.assertEqual(again["operation"]["id"], first["operation"]["id"])
        self.assertEqual(OperationRequest.objects.count(), 1)
