"""When the controller sweeps, and how it hears that there is work.

Two properties. Applying queued work must not wait for a polling interval, and
sweeping must not cost a provider call a minute for records that change monthly.
"""

from datetime import timedelta
from pathlib import Path
import tempfile

from django.conf import settings
from django.contrib.auth import get_user_model
from django.http import HttpResponse
from django.test import RequestFactory
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from hq.domains.control_plane.models import ManagedResource, OperationRequest, ProviderInventory

from .. import cadence
from ..cadence import (
    carried_connections,
    sweep_interval,
    ControllerSweepCommand,
    note_activity,
    recently_used,
    request_controller_sweep,
    ring_doorbell,
    sweep_due,
)
from ..resource_operations import OperationCommand, request_reconcile
from ..security import cli_principal


def markers():
    directory = Path(tempfile.mkdtemp())
    return override_settings(
        SEVERINO_CONTROLLER_DOORBELL=str(directory / "doorbell"),
        SEVERINO_ACTIVITY_MARKER=str(directory / "activity"),
    ), directory


def swept(age_seconds, kind="adguard.rewrite", tried_seconds=None):
    """A kind last read ``age_seconds`` ago, and last tried then or since."""

    at = timezone.now() - timedelta(seconds=age_seconds)
    ProviderInventory.objects.update_or_create(
        kind=kind, defaults={"records": [], "reachable": True, "observed_at": at}
    )
    tried = at if tried_seconds is None else timezone.now() - timedelta(seconds=tried_seconds)
    ProviderInventory.objects.filter(kind=kind).update(updated_at=tried)


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

    @override_settings(SEVERINO_SWEEP_INTERVAL_ACTIVE_SECONDS=300)
    def test_a_kind_hq_reads_itself_does_not_make_the_sweep_due(self):
        """A public registry's reading is days old by design and is not the sweep's."""

        swept(age_seconds=30)
        swept(age_seconds=500_000, kind="registry.address")
        note_activity()

        verdict = sweep_due()
        self.assertFalse(verdict["due"])
        self.assertLess(verdict["age_seconds"], 300)

    @override_settings(SEVERINO_SWEEP_INTERVAL_ACTIVE_SECONDS=300)
    def test_a_provider_that_is_down_is_tried_each_interval_not_each_run(self):
        swept(age_seconds=90_000, tried_seconds=30)
        note_activity()

        self.assertFalse(sweep_due()["due"])

    @override_settings(SEVERINO_SWEEP_INTERVAL_ACTIVE_SECONDS=300)
    def test_the_reason_reads_as_lengths_of_time(self):
        swept(age_seconds=4000)
        note_activity()

        reason = sweep_due()["reason"]
        self.assertIn("1\xa0hour", reason)
        self.assertIn("every 5\xa0minutes", reason)
        self.assertNotRegex(reason, r"\d+s\b")

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
        from hq.platform.application.adoption_testing import managing_everything

        managing_everything()
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

    @override_settings(
        SEVERINO_SWEEP_INTERVAL_ACTIVE_SECONDS=60,
        SEVERINO_SWEEP_INTERVAL_IDLE_SECONDS=43200,
    )
    def test_an_operator_can_request_a_due_sweep_without_provider_authority(self):
        swept(age_seconds=600)

        result = request_controller_sweep(
            ControllerSweepCommand(), principal=cli_principal()
        )

        self.assertTrue(result["requested"])
        self.assertTrue(result["due"])
        self.assertTrue((self.directory / "doorbell").exists())

    @override_settings(SEVERINO_CONTROLLER_DOORBELL="/proc/nonexistent/doorbell")
    def test_an_explicit_sweep_request_reports_an_unreachable_doorbell(self):
        with self.assertRaisesRegex(ValueError, "could not reach the controller"):
            request_controller_sweep(
                ControllerSweepCommand(), principal=cli_principal()
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


class ProbesAreNotPresenceTests(TestCase):
    """A health probe must not count as somebody using HQ.

    The container polls readiness every thirty seconds. If that refreshes the
    activity marker the short interval never lapses, so the long one is
    unreachable and the controller sweeps continuously for as long as the
    container is healthy.
    """

    def setUp(self):
        self.marker = Path(tempfile.mkdtemp()) / "hq-activity"

    def _get(self, path):
        from hq.platform.core.middleware import RequestContextMiddleware

        request = RequestFactory().get(path)
        RequestContextMiddleware(lambda _r: HttpResponse("ok"))(request)

    def test_a_health_probe_does_not_mark_hq_as_used(self):
        with self.settings(SEVERINO_ACTIVITY_MARKER=str(self.marker)):
            self._get("/health/ready/")
            self.assertFalse(self.marker.exists())
            self.assertFalse(recently_used())

    def test_an_ordinary_request_still_does(self):
        with self.settings(SEVERINO_ACTIVITY_MARKER=str(self.marker)):
            self._get("/")
            self.assertTrue(self.marker.exists())
            self.assertTrue(recently_used())

    def test_repeated_probes_never_reach_the_short_interval(self):
        with self.settings(SEVERINO_ACTIVITY_MARKER=str(self.marker)):
            for _ in range(5):
                self._get("/health/live/")
            self.assertEqual(
                sweep_interval(),
                timedelta(seconds=settings.SEVERINO_SWEEP_INTERVAL_IDLE_SECONDS),
            )


class CarriedConnectionPolicyTests(TestCase):
    """Which SSH connections a sweep may report without logging in again."""

    def _connection(self, ref, *, controller="here", provider="ssh", age_minutes=5,
                    reachable=True, probed=True):
        from hq.domains.control_plane.models import ProviderConnection

        return ProviderConnection.objects.create(
            controller_id=controller,
            connection_ref=ref,
            provider=provider,
            reachable=reachable,
            probed=probed,
            observed_at=timezone.now() - timedelta(minutes=age_minutes),
        )

    @override_settings(SEVERINO_SSH_PROBE_INTERVAL_SECONDS=3600)
    def test_only_recent_good_ssh_answers_are_carried(self):
        self._connection("fresh")
        self._connection("old", age_minutes=61)
        self._connection("failing", reachable=False)
        self._connection("never-asked", probed=False)
        self._connection("an-api", provider="cloudflare_dns")
        self._connection("fresh", controller="elsewhere")

        self.assertEqual(carried_connections("here"), ["fresh"])
        self.assertEqual(sweep_due("here")["carry"], ["fresh"])

    def test_without_a_controller_nothing_is_carried(self):
        self._connection("fresh")

        self.assertEqual(sweep_due()["carry"], [])


class CarriedConnectionRecordTests(TestCase):
    """A carried report keeps the last real answer, and a new one cannot be faked."""

    def test_a_carried_connection_keeps_its_last_answer_and_its_time(self):
        from hq.domains.control_plane.models import ProviderConnection

        from ..report_testing import report_connections

        taken = timezone.now() - timedelta(minutes=20)
        ProviderConnection.objects.create(
            controller_id="here",
            connection_ref="shared-hosting",
            provider="ssh",
            reachable=True,
            probed=True,
            detail="user@host:22",
            observed_at=taken,
        )

        report_connections(
            [
                {
                    "connection_ref": "shared-hosting",
                    "provider": "ssh",
                    "endpoint": "192.0.2.30:22",
                    "carried": True,
                    "probed": False,
                    "detail": "Not asked again this sweep.",
                }
            ],
            principal=cli_principal(),
            controller_id="here",
        )

        row = ProviderConnection.objects.get(connection_ref="shared-hosting")
        self.assertTrue(row.probed)
        self.assertEqual(row.detail, "user@host:22")
        self.assertEqual(row.observed_at, taken)
        self.assertEqual(row.endpoint, "192.0.2.30:22")

    def test_a_carried_connection_hq_has_never_seen_is_recorded_as_unprobed(self):
        from hq.domains.control_plane.models import ProviderConnection

        from ..report_testing import report_connections

        report_connections(
            [
                {
                    "connection_ref": "new-host",
                    "provider": "ssh",
                    "carried": True,
                    "probed": False,
                    "detail": "Not asked again this sweep.",
                }
            ],
            principal=cli_principal(),
            controller_id="here",
        )

        row = ProviderConnection.objects.get(connection_ref="new-host")
        self.assertFalse(row.probed)


class BootTests(TestCase):
    """A new image asks for delivery to be read; nothing polls for it."""

    def setUp(self):
        self.settings_override, self.directory = markers()
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)

    def test_a_boot_where_delivery_is_read_asks_for_it_and_rings(self):
        from hq.domains.control_plane.models import ReadRequest

        from ..cadence import request_delivery_read

        ProviderInventory.objects.create(kind="github.delivery", records=[], reachable=True, observed_at=timezone.now())

        self.assertTrue(request_delivery_read())
        self.assertTrue(ReadRequest.objects.filter(kind="github.delivery").exists())
        self.assertTrue((self.directory / "doorbell").exists())
        # A boot is not a person using HQ: the idle cadence stands.
        self.assertFalse(recently_used())

    def test_a_boot_where_delivery_is_not_read_asks_for_nothing(self):
        from hq.domains.control_plane.models import ReadRequest

        from ..cadence import request_delivery_read

        self.assertFalse(request_delivery_read())
        self.assertFalse(ReadRequest.objects.exists())
        self.assertFalse((self.directory / "doorbell").exists())


class ControllerStandingTests(TestCase):
    """Whether the controller is still arriving, which no record of a sweep can say."""

    def setUp(self):
        self.marker = Path(tempfile.mkdtemp()) / "controller-heartbeat"
        self.enterContext(override_settings(SEVERINO_CONTROLLER_HEARTBEAT=str(self.marker)))

    def test_an_installation_the_controller_never_reached_is_unknown_not_silent(self):
        standing = cadence.controller_standing()

        self.assertFalse(standing.known)
        self.assertFalse(standing.silent)

    def test_a_controller_that_just_arrived_is_not_silent(self):
        cadence.note_controller()
        standing = cadence.controller_standing()

        self.assertTrue(standing.known)
        self.assertFalse(standing.silent)

    def test_one_that_stopped_arriving_is_silent(self):
        cadence.note_controller()
        later = timezone.now() + cadence.CONTROLLER_SILENT_AFTER + timedelta(minutes=1)

        self.assertTrue(cadence.controller_standing(later).silent)

    def test_the_arrival_is_noted_on_the_first_call_every_applying_run_makes(self):
        from hq.domains.control_plane.tests import bridge_client

        bridge_client.call("glance-plan", controller_id="test")

        self.assertTrue(cadence.controller_standing().known)

    def test_a_marker_that_cannot_be_written_does_not_fail_the_controller(self):
        with override_settings(SEVERINO_CONTROLLER_HEARTBEAT="/nonexistent/dir/heartbeat"):
            cadence.note_controller()
            self.assertFalse(cadence.controller_standing().known)
