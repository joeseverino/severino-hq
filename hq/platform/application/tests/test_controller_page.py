"""The controller's own page: what HQ knows of it, from what it left behind."""

import os
from datetime import timedelta
from pathlib import Path
import tempfile
import time

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from hq.domains.control_plane.models import ManagedResource, OperationRequest, ProviderInventory, ReadRequest

from ..controller_page import controller_page

DIRECTORY = Path(tempfile.mkdtemp())
MARKERS = override_settings(
    SEVERINO_CONTROLLER_HEARTBEAT=str(DIRECTORY / "heartbeat"),
    SEVERINO_ACTIVITY_MARKER=str(DIRECTORY / "activity"),
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


@MARKERS
class ControllerPageTests(TestCase):
    def setUp(self):
        (DIRECTORY / "heartbeat").unlink(missing_ok=True)

    def test_an_installation_with_no_controller_says_so_and_nothing_more(self):
        page = controller_page()

        self.assertFalse(page.standing.known)
        self.assertIsNone(page.swept_at)
        self.assertEqual(page.readings, ())
        self.assertEqual(page.queue, ())

    def test_what_was_read_is_listed_with_what_failed_first(self):
        store("adguard.rewrite")
        store("tailscale.device", reachable=False, error="The provider refused the credential.")
        store("npm.proxy_host", connected=False)

        page = controller_page()

        self.assertEqual([reading.kind for reading in page.readings][0], "tailscale.device")
        self.assertEqual(page.failing, 1)
        results = {reading.kind: (reading.ok, reading.result) for reading in page.readings}
        self.assertEqual(results["adguard.rewrite"], (True, "Read"))
        self.assertEqual(results["npm.proxy_host"], (True, "Not connected"))
        self.assertEqual(results["tailscale.device"], (False, "The provider refused the credential."))
        self.assertEqual({reading.records for reading in page.readings}, {2})

    def test_it_says_when_the_next_sweep_is_due(self):
        store("adguard.rewrite", age=timedelta(seconds=60))
        fresh = controller_page()
        store("adguard.rewrite", age=timedelta(seconds=900))
        stale = controller_page()

        self.assertFalse(fresh.sweep_due)
        self.assertAlmostEqual((fresh.next_sweep_at - timezone.now()).total_seconds(), 240, delta=5)
        self.assertTrue(stale.sweep_due)
        self.assertIsNone(stale.next_sweep_at)

    def test_work_and_reads_waiting_for_it_are_listed_and_finished_work_is_not(self):
        resource = ManagedResource.objects.create(key="example-dns", kind="adguard.rewrite", spec={})
        asked = {"resource": resource, "action": "reconcile", "requested_actor": "someone", "requested_interface": "web"}
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

        self.assertEqual([(work.resource, work.state) for work in page.queue], [("example-dns", "Waiting")])
        self.assertEqual([read.subject for read in page.asked], ["adguard.rewrite"])

    def test_the_page_shows_a_controller_that_has_gone_quiet(self):
        marker = DIRECTORY / "heartbeat"
        marker.touch()
        long_ago = time.time() - 26 * 3600
        os.utime(marker, (long_ago, long_ago))
        store("adguard.rewrite", age=timedelta(hours=26))
        user = get_user_model().objects.create_user(username="someone", password="not-used-here")
        self.client.force_login(user)

        response = self.client.get(reverse("control_plane:controller"))

        self.assertContains(response, "Not heard from for")
        self.assertContains(response, "Nothing has been read or changed since.")
        self.assertContains(response, 'title="adguard.rewrite">Internal DNS record')
        self.assertNotContains(response, "<code>adguard.rewrite</code>")
        # A reading 26 hours old against a five-minute cadence is not "due": it is late, and says which.
        self.assertContains(response, "Overdue")
        self.assertContains(response, "Internal DNS record was last read")

    def test_the_page_needs_a_session_and_the_dashboard_links_to_it(self):
        self.assertEqual(self.client.get(reverse("control_plane:controller")).status_code, 302)
        user = get_user_model().objects.create_user(username="someone", password="not-used-here")
        self.client.force_login(user)

        self.assertContains(self.client.get("/"), 'href="%s"' % reverse("control_plane:controller"))
