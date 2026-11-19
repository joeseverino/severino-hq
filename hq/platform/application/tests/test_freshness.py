"""One freshness rule per reading kind, used by every surface."""

from dataclasses import replace
from datetime import timedelta

from django.core.exceptions import ImproperlyConfigured
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from hq.domains.control_plane.models import ProviderConnection
from hq.platform.application.markup_testing import elements, links, select, text
from hq.platform.core.testing import operator

from ..entity_links import entity_link
from ..freshness import (
    CURRENT,
    DASHBOARD_GLANCE,
    DUE,
    NEVER,
    SSH_PROBE,
    STALE,
    ReadCadence,
    cadence,
    freshness,
    stale_after,
)
from ..plugin_testing import installed, sibling
from ..report_testing import report_connections

# A kind the controller sweeps, read through a provider's API.
SWEPT = "adguard.rewrite"


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
    def test_a_swept_kind_is_stale_once_it_misses_a_whole_sweep(self):
        self.assertEqual(stale_after("host.perimeter"), timedelta(hours=2))
        self.assertEqual(stale_after(), timedelta(hours=2))

    def test_a_record_is_left_behind_once_its_kind_is_read_over_an_hour_after_it(self):
        from ..freshness import behind

        newest = timezone.now()
        self.assertFalse(behind(newest - timedelta(hours=1), newest))
        self.assertTrue(behind(newest - timedelta(hours=1, seconds=1), newest))
        self.assertFalse(behind(None, newest))
        self.assertFalse(behind(newest, None))

    def test_the_join_engine_uses_the_same_rule(self):
        from .. import facts

        self.assertIs(facts.freshness, freshness)

    def test_a_cadence_is_two_positive_spans(self):
        with self.assertRaises(ValueError):
            ReadCadence(timedelta(0), timedelta(days=1))
        with self.assertRaises(ValueError):
            ReadCadence(timedelta(days=1), -timedelta(days=1))


# An extension's own kind, read when somebody asks.
EXAMPLE_KIND = "example.readings"


def declaring(identifier, declared):
    """An example extension whose ``cadences`` answers ``declared``."""

    manifest, integration = sibling(identifier=identifier)
    return manifest, replace(integration, cadences=lambda: declared)


def every(days, stale_days):
    found = ReadCadence(timedelta(days=days), timedelta(days=stale_days))
    return lambda: found


class ExtensionCadenceTests(TestCase):
    """An extension's kind is judged by the cadence it declares, on every surface."""

    def test_its_readings_are_due_and_stale_on_its_own_cadence(self):
        now = timezone.now()
        with installed(declaring("example.alpha", {EXAMPLE_KIND: every(1, 4)})):
            self.assertEqual(freshness(EXAMPLE_KIND, now - timedelta(hours=1), now).state, CURRENT)
            self.assertEqual(freshness(EXAMPLE_KIND, now - timedelta(days=2), now).state, DUE)
            self.assertEqual(freshness(EXAMPLE_KIND, now - timedelta(days=4), now).state, STALE)
            self.assertEqual(stale_after(EXAMPLE_KIND), timedelta(days=4))

    def test_its_connections_go_stale_on_its_own_cadence(self):
        from ..connection_catalog import connection_lifecycle
        from ..connections import ConnectionInstance

        now = timezone.now()

        def lifecycle(age):
            instance = ConnectionInstance(
                "one", "One", EXAMPLE_KIND, "good", "ok", credential_model="none", observed_at=now - age
            )
            return connection_lifecycle(instance, "proven", now=now)

        with installed(declaring("example.alpha", {EXAMPLE_KIND: every(30, 30)})):
            self.assertEqual(lifecycle(timedelta(days=20)), "ready")
            self.assertEqual(lifecycle(timedelta(days=31)), "stale")

    def test_a_kind_the_host_declares_or_two_extensions_claim_is_refused(self):
        weekly = every(7, 7)
        cases = {
            "the host's": (declaring("example.alpha", {DASHBOARD_GLANCE: weekly}),),
            "twice": (
                declaring("example.alpha", {EXAMPLE_KIND: weekly}),
                declaring("example.beta", {EXAMPLE_KIND: weekly}),
            ),
            "not a callable": (declaring("example.alpha", {EXAMPLE_KIND: weekly()}),),
        }
        for case, siblings in cases.items():
            with self.subTest(case), installed(*siblings), self.assertRaises(ImproperlyConfigured):
                freshness("example.other", timezone.now())

    def test_a_kinds_cadence_is_asked_only_as_that_kind_is_judged(self):
        asked = []

        def weekly():
            asked.append(EXAMPLE_KIND)
            return ReadCadence(timedelta(days=7), timedelta(days=7))

        with installed(declaring("example.alpha", {EXAMPLE_KIND: weekly})):
            freshness("example.other", timezone.now())
            self.assertEqual(asked, [])
            self.assertEqual(stale_after(EXAMPLE_KIND), timedelta(days=7))
            self.assertEqual(asked, [EXAMPLE_KIND])

    def test_a_kind_answered_with_no_cadence_is_refused(self):
        with (
            installed(declaring("example.alpha", {EXAMPLE_KIND: lambda: timedelta(days=7)})),
            self.assertRaises(ImproperlyConfigured),
        ):
            freshness(EXAMPLE_KIND, timezone.now())


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
        report_connections([ssh_report()], controller_id="c")
        earlier = timezone.now() - timedelta(minutes=48)
        ProviderConnection.objects.update(observed_at=earlier, reported_at=earlier)

        report_connections([ssh_report(carried=True, probed=False)], controller_id="c")

        row = ProviderConnection.objects.get()
        self.assertEqual(row.observed_at, earlier)
        self.assertGreater(row.reported_at, earlier + timedelta(minutes=47))
        self.assertTrue(row.probed)

        from ..connections import connection_readings

        (reading,) = connection_readings()
        self.assertEqual(reading.observed_at, row.reported_at)
        self.assertEqual(reading.probed_at, earlier)

    def test_a_probed_connection_has_no_separate_probe_time(self):
        report_connections([ssh_report()], controller_id="c")

        from ..connections import connection_readings

        (reading,) = connection_readings()
        self.assertIsNone(reading.probed_at)


class OldestReadingTests(TestCase):
    def setUp(self):
        operator(self.client)
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

        oldest = entity_link("connection", "example-old").label
        self.assertIn(f"Oldest reading {oldest}", text(response))
        self.assertTrue(any(name == oldest and href.startswith("#") for name, href in links(response)))
        self.assertTrue(select(response, "data-reading-age", "read"))


class OneStaleLineTests(TestCase):
    """A page saying a swept kind is out of date and the finding saying it
    has not been read draw the line in the same place."""

    def claimed(self, age: timedelta) -> bool:
        from ..controller_findings import _kind_never_swept
        from ..finding_model import FindingEstate
        from ..topology_model import Topology

        now = timezone.now()
        estate = FindingEstate(
            topology=Topology(nodes=(), edges=()),
            now=now,
            observed={},
            latest_by_kind={SWEPT: now - age},
            declared_kinds=frozenset({SWEPT}),
            declared_counts={},
            governed={},
            controllers_by_kind={},
        )
        return bool(_kind_never_swept(estate))

    def test_a_kind_is_claimed_unread_exactly_when_its_reading_is_out_of_date(self):
        line = stale_after(SWEPT)
        for age in (line - timedelta(minutes=1), line + timedelta(minutes=1)):
            with self.subTest(age=age):
                self.assertEqual(self.claimed(age), freshness(SWEPT, timezone.now() - age).stale)
        self.assertTrue(self.claimed(line + timedelta(minutes=1)))


class ReadingAgeTests(TestCase):
    """One primitive says how old a reading is, in one wording."""

    def render(self, **context) -> str:
        from django.template.loader import render_to_string

        return render_to_string("partials/_reading_age.html", context)

    def test_a_current_reading_gives_its_age(self):
        html = self.render(reading=freshness(SWEPT, timezone.now() - timedelta(minutes=5)))

        self.assertTrue(select(html, "data-reading-age", CURRENT))
        self.assertTrue(elements(html, "time"))

    def test_an_out_of_date_reading_says_so_beside_its_age(self):
        reading = freshness(SWEPT, timezone.now() - stale_after(SWEPT) - timedelta(hours=1))

        html = self.render(reading=reading)

        self.assertIn(f'data-reading-age="{STALE}"', html)
        self.assertIn(reading.label.lower(), html)

    def test_no_reading_says_there_is_none(self):
        html = self.render(reading=freshness(SWEPT, None))

        self.assertTrue(select(html, "data-reading-age", NEVER))
        self.assertEqual(elements(html, "time"), [])

    def test_a_bare_moment_gives_its_age(self):
        self.assertTrue(elements(self.render(at=timezone.now()), "time"))
