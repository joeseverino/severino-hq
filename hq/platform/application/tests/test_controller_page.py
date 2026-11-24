"""The controller's own page: what HQ knows of it, from what it left behind."""

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from django.test import SimpleTestCase, TestCase, override_settings, tag
from django.urls import reverse
from django.utils import timezone

from hq.domains.control_plane.models import OperationRequest, ProviderInventory, ReadRequest
from hq.platform.application.markup_testing import select, texts
from hq.platform.core.templatetags.value_tags import dot
from hq.platform.core.testing import declare, operator

from ..cadence_testing import controller_arrived
from ..controller_page import controller_page
from .commands import commands_on

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
    def test_an_installation_with_no_controller_says_so_and_nothing_more(self):
        page = controller_page()

        self.assertFalse(page.standing.known)
        self.assertIsNone(page.swept_at)
        self.assertEqual(page.readings, ())
        self.assertEqual(page.queue, ())
        self.assertEqual((page.overdue_tone, page.queue_tone), ("",) * 2)

    def test_late_sweeps_are_serious(self):
        self.assertEqual(replace(controller_page(), overdue=True).overdue_tone, "serious")

    def test_a_controller_never_seen_is_given_its_start_steps_and_one_that_ran_is_not(self):
        from hq.platform.application import cadence

        operator(self.client, "controller-reader")
        with override_settings(SEVERINO_HQ_SOURCE="https://github.com/example/hq"):
            never = self.client.get(reverse("control_plane:controller"))
            cadence.note_controller()
            arrived = self.client.get(reverse("control_plane:controller"))

        self.assertContains(never, "Start the controller")
        self.assertIn("sudo systemctl start severino-hq-controller.service", commands_on(never))
        self.assertContains(never, "docs/Install.md#11-the-controllers-first-reading")
        self.assertNotContains(arrived, "Start the controller")

    def test_what_was_read_is_listed_with_what_failed_first(self):
        store("adguard.rewrite")
        store("tailscale.device", reachable=False, error="The provider refused the credential.")
        store("npm.proxy_host", connected=False)

        page = controller_page()

        self.assertEqual(next(reading.kind for reading in page.readings), "tailscale.device")
        results = {reading.kind: (reading.ok, reading.result) for reading in page.readings}
        self.assertEqual(results["adguard.rewrite"], (True, "Read"))
        self.assertEqual(results["npm.proxy_host"], (True, "Not connected"))
        self.assertEqual(results["tailscale.device"], (False, "The provider refused the credential."))
        self.assertEqual({reading.records for reading in page.readings}, {2})

    def test_a_kind_no_read_has_answered_was_tried_and_never_succeeded(self):
        store("tailscale.device", age=timedelta(minutes=5), records=[], reachable=False, observed_at=None)

        reading = next(reading for reading in controller_page().readings if reading.kind == "tailscale.device")

        self.assertEqual(reading.reading.state, "never")
        self.assertLess(reading.attempted_at, timezone.now() - timedelta(minutes=4))

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

        self.assertEqual([(work.resource, work.state) for work in page.queue], [("example-dns", "Waiting")])
        self.assertEqual(page.queue_tone, "attention")
        self.assertEqual([read.subject for read in page.asked], ["adguard.rewrite"])

    def test_the_page_shows_a_controller_that_has_gone_quiet(self):
        controller_arrived(ago=timedelta(hours=26))
        store("adguard.rewrite", age=timedelta(hours=26))
        operator(self.client, "someone")

        response = self.client.get(reverse("control_plane:controller"))

        self.assertContains(response, "Nothing has been read or changed since.")
        (named,) = select(response, "title", "adguard.rewrite")
        self.assertEqual(named.words, "Internal DNS record")
        self.assertNotIn("adguard.rewrite", texts(response, "code"))
        # A reading 26 hours old against a five-minute cadence is not "due": it is late, and says which.
        self.assertContains(response, "Overdue")
        self.assertContains(response, f"Internal DNS record{dot()}")

    def test_the_dashboard_links_to_it(self):
        operator(self.client, "someone")

        self.assertContains(self.client.get("/"), f'href="{reverse("control_plane:controller")}"')


@tag("source")
class HostLayoutTests(SimpleTestCase):
    """The unit names HQ tells an operator to inspect are the host's own."""

    def test_the_controller_unit_is_the_one_the_host_declares(self):
        from ..controller_findings import CONTROLLER_UNIT

        layout = (Path(__file__).resolve().parents[4] / "controller" / "host" / "layout.go").read_text()

        self.assertIn(f'ControllerUnit = "{CONTROLLER_UNIT}"', layout)
