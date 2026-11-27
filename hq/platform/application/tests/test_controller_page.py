"""The controller's own page: what HQ knows of it, from what it left behind."""

import tempfile
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from hq.domains.control_plane.models import OperationRequest, ProviderInventory, ReadRequest
from hq.platform.application.markup_testing import select, text, texts, with_class
from hq.platform.core.templatetags.value_tags import dot
from hq.platform.core.testing import declare, operator

from ..cadence import note_activity
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

    def test_the_summary_counts_the_readings_the_table_lists(self):
        # A machine's figures are gathered through several connections, so no
        # one provider takes them.
        store("machine.telemetry")
        store("adguard.rewrite")
        controller_arrived()
        operator(self.client, "someone")

        response = self.client.get(reverse("control_plane:controller"))

        (listed,) = (
            int(count.words) for count in with_class(response, "count") if "Readings" in count.ancestors[-1].words
        )
        self.assertIn(f"{listed} readings", text(response))
        self.assertIn("machine.telemetry", [named.attrs["title"] for named in select(response, "title")])

    def test_a_reading_row_says_its_state_its_last_read_and_its_cadence_once_each(self):
        from ..credential_sight import NEVER_SWEPT, READABLE
        from ..freshness import LAST_READ, OUT_OF_DATE, cadence, freshness

        # Read on a clock of its own, so its row says its cadence in every state.
        kind = "github.dependency_run"
        late = cadence(kind).stale_after + timedelta(hours=1)
        # A reader's words hold the cadence's non-breaking space as a space.
        every = " ".join(freshness(kind, None).cadence.split())
        store("github.repository")
        operator(self.client, "someone")
        for state, age in ((NEVER_SWEPT.label, None), (READABLE.label, timedelta(0)), (OUT_OF_DATE, late)):
            with self.subTest(state):
                if age is not None:
                    store(kind, age=age)

                (named,) = select(self.client.get(reverse("control_plane:controller")), "title", kind)
                row = next(ancestor for ancestor in named.ancestors if ancestor.tag == "tr").words.lower()

                self.assertEqual(row.count(state.lower()), 1)
                self.assertNotIn(LAST_READ.lower(), row)
                self.assertNotIn(freshness(kind, None).label.lower(), row)
                self.assertEqual(row.count(every), 1)

    def test_a_reading_still_current_is_never_overdue_however_short_the_live_interval(self):
        marker = Path(tempfile.mkdtemp()) / "activity"
        with override_settings(SEVERINO_ACTIVITY_MARKER=str(marker), SEVERINO_SWEEP_INTERVAL_IDLE_SECONDS=43200):
            note_activity()
            store("adguard.rewrite", age=timedelta(minutes=15))
            page = controller_page()

        self.assertFalse(page.overdue)

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
