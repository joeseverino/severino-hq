"""HQ's playbook worker is known by its claims: one never seen, or not seen
within its timer's interval, takes no scan or run, so each is refused with
the reason, and one finding says how to set it up. Every manifest here is a
synthetic ``example.*`` one."""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from hq.domains.control_plane.models import ManagedResource, OperationRequest
from hq.domains.control_plane.tests import bridge_client
from hq.platform.core.models import UpstreamReading

from ..capabilities import run_capability
from ..security import Capability, Principal, cli_principal
from .procedures import APP, RESTART_RUN, Supply, manifest, run_payload

OPERATOR = Principal("example-operator", "web", frozenset({Capability.READ, Capability.MANAGE_INFRASTRUCTURE}))
HEARTBEAT = "automation-heartbeat"
RULE = "automation-worker-absent"
RUNS = ["automation.operation:run", "automation.source:scan"]


def seen(ago: timedelta | None) -> None:
    """The worker last arrived ``ago``, or never when None."""

    readings = UpstreamReading.objects.filter(key=HEARTBEAT)
    if ago is None:
        readings.delete()
    else:
        readings.update(observed_at=timezone.now() - ago)


def playbook_source() -> None:
    """The playbook source a Refresh records, as an HQ with automation set up holds."""

    ManagedResource.objects.create(key="automation-source", kind="automation.source", spec={"named_by": "host"})


def absent_findings() -> list:
    from ..findings import estate_findings

    return [finding for finding in estate_findings(principal=cli_principal()) if finding.rule == RULE]


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


class AbsentWorkerTests(TestCase):
    def setUp(self):
        self.supply = Supply(self, manifest())

    def run_it(self) -> dict:
        return run_capability(RESTART_RUN, run_payload(APP), principal=OPERATOR, idempotency_key="example-run").answer

    def test_never_seen_a_run_is_refused_with_the_reason_and_one_finding_says_how_to_set_it_up(self):
        playbook_source()
        seen(None)

        answer = self.run_it()
        self.assertEqual(answer["error"]["code"], "precondition", answer)
        self.assertEqual(answer["error"]["message"], "Cannot run: no playbook worker reports to this HQ.")
        self.assertFalse(OperationRequest.objects.exists())

        (finding,) = absent_findings()
        commands = [step.command for step in finding.steps if step.command]
        self.assertIn("sudo /usr/local/lib/severino-hq/deploy/bin/hq-host automation", commands)
        self.assertIn("sudoedit /etc/severino-hq/host.json", commands)
        self.assertTrue(any('"automation"' in step.label for step in finding.steps))

    def test_where_automation_was_never_set_up_a_run_is_refused_and_nothing_is_raised(self):
        seen(None)

        answer = self.run_it()
        self.assertEqual(answer["error"]["message"], "Cannot run: no playbook worker reports to this HQ.")
        self.assertEqual(absent_findings(), [])

    def test_not_seen_within_its_interval_a_run_is_refused_and_the_finding_is_urgent(self):
        seen(timedelta(hours=3))

        answer = self.run_it()
        # An amount and its unit are kept on one line.
        said = answer["error"]["message"].replace("\N{NO-BREAK SPACE}", " ")
        self.assertEqual(said, "Cannot run: 3 hours since a playbook worker last reported.")
        (finding,) = absent_findings()
        self.assertEqual(finding.severity, "serious")
        self.assertIn(
            ("Last seen", "3 hours ago"),
            [(label, value.replace("\N{NO-BREAK SPACE}", " ")) for label, value in finding.evidence],
        )

    def test_seen_within_its_interval_a_run_is_queued_and_nothing_is_raised(self):
        seen(timedelta(minutes=20))

        self.assertTrue(self.run_it()["queued"])
        self.assertEqual(absent_findings(), [])

    def test_a_worker_that_arrives_clears_what_its_absence_raised(self):
        playbook_source()
        seen(None)
        self.assertTrue(absent_findings())

        bridge_client.call("claim", controller_id="example-worker", capability=RUNS)

        self.assertEqual(absent_findings(), [])
        self.assertTrue(self.run_it()["queued"])

    def test_a_retried_run_queued_before_the_worker_went_is_answered_as_it_was(self):
        first = self.run_it()
        seen(None)

        again = self.run_it()
        self.assertTrue(again["ok"], again)
        self.assertEqual(again["operation"]["id"], first["operation"]["id"])
        self.assertEqual(OperationRequest.objects.count(), 1)
