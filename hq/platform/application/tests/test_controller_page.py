"""The controller's own page: what HQ knows of it, from what it left behind."""

from datetime import timedelta

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from hq.domains.control_plane.models import OperationRequest, ProviderInventory, ReadRequest
from hq.platform.core.testing import declare, operator

from ..cadence_testing import controller_arrived
from ..controller_page import controller_page

SWEEPS = override_settings(
    SEVERINO_SWEEP_INTERVAL_ACTIVE_SECONDS=300,
    SEVERINO_SWEEP_INTERVAL_IDLE_SECONDS=300,
)


def store(kind: str, *, age: timedelta = timedelta(0), **fields) -> None:
    ProviderInventory.objects.update_or_create(
        kind=kind,
        defaults={"records": [{}, {}], "reachable": True, "observed_at": timezone.now() - age, **fields},
    )
    # Tried when it was read: storing it just now is not an attempt.
    ProviderInventory.objects.filter(kind=kind).update(updated_at=timezone.now() - age)


@SWEEPS
class ControllerPageTests(TestCase):
    def test_work_and_reads_waiting_for_it_are_listed_and_finished_work_is_not(self):
        resource = declare("example-dns", "adguard.rewrite")
        asked = {
            "resource": resource,
            "action": "reconcile",
            "requested_actor": "someone",
            "requested_interface": "web",
        }
        OperationRequest.objects.create(**asked, idempotency_key="waiting")
        OperationRequest.objects.create(
            **asked,
            idempotency_key="finished",
            state=OperationRequest.State.SUCCEEDED,
            completed_at=timezone.now(),
        )
        store("adguard.rewrite")
        ReadRequest.objects.create(kind="adguard.rewrite")

        page = controller_page()

        self.assertEqual([(work.record.identity, work.state) for work in page.queue], [("example-dns", "Waiting")])
        self.assertEqual(page.queue_tone, "attention")
        self.assertEqual([read.record.kind for read in page.asked], ["adguard.rewrite"])
        self.assertEqual(page.waiting_said, "1 change and 1 refresh")

    def test_the_page_shows_a_controller_that_has_gone_quiet(self):
        controller_arrived(ago=timedelta(hours=26))
        store("adguard.rewrite", age=timedelta(hours=26))
        operator(self.client, "someone")

        response = self.client.get(reverse("control_plane:controller"))

        # A reading 26 hours old against a five-minute cadence is not "due": it is late, and says which.
        self.assertContains(response, "Overdue")
