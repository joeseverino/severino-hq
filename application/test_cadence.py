"""When the controller sweeps, and how it hears that there is work.

Two properties. Applying queued work must not wait for a polling interval, and
sweeping must not cost a provider call a minute for records that change monthly.
Both used to ride one timer, where only one of them could be right.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
import tempfile

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from control_plane.models import ManagedResource, OperationRequest, ProviderInventory

from .cadence import note_activity, recently_used, ring_doorbell, sweep_due
from .infrastructure import OperationCommand, request_reconcile
from .security import cli_principal


def markers():
    directory = Path(tempfile.mkdtemp())
    return override_settings(
        SEVERINO_CONTROLLER_DOORBELL=str(directory / "doorbell"),
        SEVERINO_ACTIVITY_MARKER=str(directory / "activity"),
    ), directory


def swept(age_seconds):
    ProviderInventory.objects.update_or_create(
        kind="adguard.rewrite",
        defaults={
            "records": [],
            "reachable": True,
            "observed_at": timezone.now() - timedelta(seconds=age_seconds),
        },
    )


class SweepPolicyTests(TestCase):
    def setUp(self):
        self.settings_override, self.directory = markers()
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)

    def test_a_first_sweep_is_always_due(self):
        self.assertTrue(sweep_due()["due"])

    @override_settings(
        SEVERINO_SWEEP_INTERVAL_ACTIVE_SECONDS=60,
        SEVERINO_SWEEP_INTERVAL_IDLE_SECONDS=43200,
    )
    def test_idle_hq_sweeps_on_the_long_interval(self):
        """Nothing reads the answer until somebody opens a page.

        This is the whole saving: a minute-old view of the estate is the point
        while you are looking at it and worth nothing while you are not.
        """

        swept(age_seconds=600)

        verdict = sweep_due()
        self.assertFalse(verdict["due"])
        self.assertEqual(verdict["interval_seconds"], 43200)

    @override_settings(
        SEVERINO_SWEEP_INTERVAL_ACTIVE_SECONDS=60,
        SEVERINO_SWEEP_INTERVAL_IDLE_SECONDS=43200,
    )
    def test_hq_in_use_sweeps_on_the_short_one(self):
        swept(age_seconds=600)
        note_activity()

        verdict = sweep_due()
        self.assertTrue(verdict["due"])
        self.assertEqual(verdict["interval_seconds"], 60)

    @override_settings(SEVERINO_ACTIVE_WINDOW_SECONDS=0)
    def test_use_stops_counting_once_the_window_passes(self):
        note_activity()

        self.assertFalse(recently_used(now=timezone.now().timestamp() + 5))

    def test_nobody_having_used_it_is_not_an_error(self):
        self.assertFalse(recently_used())

    @override_settings(SEVERINO_SWEEP_INTERVAL_IDLE_SECONDS=60)
    def test_the_oldest_sweep_decides(self):
        """A provider swept an hour ago is due even if another was swept now.

        Taking the newest, one cheap provider refreshing often would report the
        whole estate as current while an expensive one went stale for a day.
        """

        swept(age_seconds=3600)
        ProviderInventory.objects.update_or_create(
            kind="npm.proxy_host",
            defaults={
                "records": [],
                "reachable": True,
                "observed_at": timezone.now(),
            },
        )

        self.assertTrue(sweep_due()["due"])


class DoorbellTests(TestCase):
    def setUp(self):
        self.settings_override, self.directory = markers()
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.resource = ManagedResource.objects.create(
            key="a-rewrite",
            kind="adguard.rewrite",
            spec={"domain": "app.example.com", "answer": "10.0.0.1"},
        )

    def test_queueing_work_rings_it(self):
        # Rung on commit, so the host is never told about an operation a
        # rollback then took away.
        with self.captureOnCommitCallbacks(execute=True):
            request_reconcile(
                OperationCommand(idempotency_key="test-1", reason=""),
                principal=cli_principal(),
                current_key="a-rewrite",
            )

        self.assertTrue((self.directory / "doorbell").exists())

    def test_it_carries_nothing(self):
        """No authority, no credentials, no data.

        The host may watch this and start the controller, which pulls the work
        through the path it always used. Forged or replayed, the worst it can
        cause is a controller run that finds nothing to do.
        """

        ring_doorbell()

        self.assertEqual((self.directory / "doorbell").read_bytes(), b"")

    def test_ringing_twice_leaves_one_doorbell(self):
        ring_doorbell()
        ring_doorbell()

        self.assertEqual(
            [path.name for path in self.directory.iterdir()], ["doorbell"]
        )

    @override_settings(SEVERINO_CONTROLLER_DOORBELL="/proc/nonexistent/doorbell")
    def test_a_doorbell_it_cannot_write_does_not_fail_the_write(self):
        """Queueing must not depend on the host filesystem.

        A doorbell able to fail the operation it announces would be worse than
        no doorbell: the wait it removes is an inconvenience, and refusing the
        change is the thing the operator actually asked for.
        """

        with self.captureOnCommitCallbacks(execute=True):
            result = request_reconcile(
                OperationCommand(idempotency_key="test-2", reason=""),
                principal=cli_principal(),
                current_key="a-rewrite",
            )

        self.assertTrue(result["queued"])
        self.assertEqual(OperationRequest.objects.count(), 1)


class ActivityTests(TestCase):
    def setUp(self):
        self.settings_override, self.directory = markers()
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.user = get_user_model().objects.create_user(
            username="operator", password="not-a-real-password"
        )
        self.client.force_login(self.user)

    def test_opening_a_page_counts_as_using_hq(self):
        self.client.get(reverse("control_plane:services"))

        self.assertTrue(recently_used())

    @override_settings(SEVERINO_ACTIVITY_THROTTLE_SECONDS=3600)
    def test_it_is_not_rewritten_on_every_request(self):
        """Every request checks the marker; only the first in each interval
        writes one, because this runs on the path that serves every page."""

        self.client.get(reverse("control_plane:services"))
        first = (self.directory / "activity").stat().st_mtime_ns
        self.client.get(reverse("control_plane:services"))

        self.assertEqual((self.directory / "activity").stat().st_mtime_ns, first)
