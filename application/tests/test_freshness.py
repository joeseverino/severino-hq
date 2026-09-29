"""One freshness rule per reading kind, used by every surface."""

from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from control_plane.models import ProviderConnection

from ..freshness import (
    CURRENT,
    DASHBOARD_GLANCE,
    DUE,
    NEVER,
    SSH_PROBE,
    STALE,
    cadence,
    freshness,
    stale_after,
)
from ..inventory import record_connections
from ..security import cli_principal


class FreshnessTests(TestCase):
    def test_states_follow_the_kinds_cadence(self):
        now = timezone.now()
        every = cadence(DASHBOARD_GLANCE).every

        self.assertEqual(freshness(DASHBOARD_GLANCE, now, now).state, CURRENT)
        self.assertEqual(freshness(DASHBOARD_GLANCE, now - every, now).state, DUE)
        self.assertEqual(
            freshness(DASHBOARD_GLANCE, now - stale_after(DASHBOARD_GLANCE), now).state,
            STALE,
        )
        self.assertEqual(freshness(DASHBOARD_GLANCE, None, now).state, NEVER)

    def test_a_reading_minutes_old_is_never_out_of_date(self):
        now = timezone.now()
        for kind in (DASHBOARD_GLANCE, SSH_PROBE, "host.perimeter", "registry.domain"):
            with self.subTest(kind=kind):
                found = freshness(kind, now - timedelta(minutes=7), now)
                self.assertFalse(found.stale)
                self.assertNotEqual(found.label, "Out of date")

    def test_a_public_registry_reading_stands_as_long_as_what_it_reads_is_slow_to_change(self):
        self.assertEqual(stale_after("registry.image"), timedelta(days=2))
        self.assertEqual(stale_after("registry.vulnerabilities"), timedelta(days=2))
        self.assertEqual(stale_after("registry.domain"), timedelta(days=14))
        # A digest never changes; its reading is the daily check that every one is held.
        self.assertEqual(stale_after("registry.digest"), timedelta(days=2))

    @override_settings(SEVERINO_SWEEP_INTERVAL_IDLE_SECONDS=3600)
    def test_a_swept_kind_is_stale_after_the_slowest_sweep(self):
        self.assertEqual(stale_after("host.perimeter"), timedelta(hours=1))
        self.assertEqual(stale_after(), timedelta(hours=1))

    def test_the_join_engine_uses_the_same_rule(self):
        from .. import facts

        self.assertIs(facts.stale_after, stale_after)


def ssh_report(**extra):
    return {
        "connection_ref": "example-edge",
        "provider": "ssh",
        "endpoint": "192.0.2.9:22",
        "ok": True,
        "probed": True,
        **extra,
    }


class CarriedConnectionTests(TestCase):
    """An SSH connection carried between probes is still reported each pass."""

    def test_a_carried_connection_is_reported_now_and_probed_earlier(self):
        record_connections([ssh_report()], principal=cli_principal(), controller_id="c")
        earlier = timezone.now() - timedelta(minutes=48)
        ProviderConnection.objects.update(observed_at=earlier, reported_at=earlier)

        record_connections(
            [ssh_report(carried=True, probed=False)],
            principal=cli_principal(),
            controller_id="c",
        )

        row = ProviderConnection.objects.get()
        self.assertEqual(row.observed_at, earlier)
        self.assertGreater(row.reported_at, earlier + timedelta(minutes=47))
        self.assertTrue(row.probed)

        from ..connections import connection_readings

        (reading,) = connection_readings()
        self.assertEqual(reading.observed_at, row.reported_at)
        self.assertEqual(reading.probed_at, earlier)

    def test_a_probed_connection_has_no_separate_probe_time(self):
        record_connections([ssh_report()], principal=cli_principal(), controller_id="c")

        from ..connections import connection_readings

        (reading,) = connection_readings()
        self.assertIsNone(reading.probed_at)


class OldestReadingTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user(
            username="operator", password="not-a-real-password"
        )
        self.client.force_login(user)
        now = timezone.now()
        for ref, age in (("example-fresh", 1), ("example-old", 300)):
            ProviderConnection.objects.create(
                connection_ref=ref,
                controller_id="example-controller",
                provider="ssh",
                endpoint="",
                observed_at=now - timedelta(minutes=age),
                reported_at=now - timedelta(minutes=age),
            )

    def test_the_oldest_reading_is_named_and_linked(self):
        response = self.client.get(reverse("control_plane:connections"))

        self.assertContains(response, "Oldest reading <a href=\"#")
        self.assertContains(response, ">example-old</a>, 5")
