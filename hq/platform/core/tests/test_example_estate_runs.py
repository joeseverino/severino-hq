"""The example estate holds an automation run in every state a run can be in,
each carried out through the paths a real run takes, of the example
procedures it adopted."""

from django.db.models import Q
from django.test import TestCase
from django.utils import timezone

from hq.domains.control_plane.models import OperationHost, OperationRequest
from hq.platform.core import seeds
from hq.platform.core.example_estate.names import LAPTOP_NAME, MACHINE, PHONE_NAME
from hq.platform.core.example_estate.testing import throwaway_media

State = OperationRequest.State


class EstateRunTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        with throwaway_media(), seeds.built("showcase"):
            pass

    def runs(self, *conditions, **lookups):
        return OperationRequest.objects.filter(*conditions, action=OperationRequest.Action.RUN, **lookups)

    def test_a_run_is_in_every_state(self):
        now = timezone.now()
        states = {
            "waiting": self.runs(state=State.QUEUED),
            "working": self.runs(state=State.CLAIMED, lease_expires_at__gt=now),
            "worker gone quiet": self.runs(state=State.CLAIMED, lease_expires_at__lte=now),
            "done": self.runs(state=State.SUCCEEDED),
            "failed a task": self.runs(state=State.FAILED, result__events__isnull=False),
            "outcome unknown": self.runs(state=State.FAILED, result__reason="OutcomeUnknown"),
            "called off": self.runs(state=State.CANCELLED),
            "approved by a person": self.runs(~Q(input__approved_by="")).filter(input__approved_by__isnull=False),
            "correlated": self.runs(~Q(input__correlation="")),
        }
        for state, found in states.items():
            with self.subTest(state=state):
                self.assertTrue(found.exists())

    def test_a_finished_run_reports_what_its_tasks_did(self):
        for run in self.runs(state__in=(State.SUCCEEDED, State.FAILED)).exclude(result__reason="OutcomeUnknown"):
            with self.subTest(run=run.pk):
                self.assertTrue(run.result["events"])
                self.assertEqual({event["host"] for event in run.result["events"]}, set(run.input["hosts"]))

    def test_each_run_names_its_machines_as_rows(self):
        for run in self.runs():
            with self.subTest(run=run.pk):
                self.assertEqual(sorted(run.hosts.values_list("host", flat=True)), sorted(run.input["hosts"]))
        self.assertEqual(set(OperationHost.objects.values_list("host", flat=True)), {MACHINE, LAPTOP_NAME, PHONE_NAME})

    def test_runs_are_dated_over_weeks_and_none_ahead_of_now(self):
        times = list(self.runs().values_list("created_at", flat=True))

        self.assertGreater(max(times) - min(times), timezone.timedelta(days=14))
        self.assertLessEqual(max(times), timezone.now())

    def test_its_procedures_are_adopted_from_what_its_worker_s_scan_proposed(self):
        from hq.domains.control_plane.models import AdoptedCatalog
        from hq.platform.application.automation_adoption import catch_up
        from hq.platform.application.automation_runs import operation_registry

        (catalog,) = AdoptedCatalog.objects.all()
        catch_up()
        commands = set(operation_registry())
        self.assertEqual(catalog.proposal.operation.state, State.SUCCEEDED)
        self.assertEqual(commands, {row["id"] for row in catalog.procedures})
        self.assertLessEqual(set(self.runs().values_list("input__operation", flat=True)), commands)
