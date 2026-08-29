"""A machine page grows a band because a resolver produced one.

The point of the registry is that the page never learns what a band means, so
these prove the resolvers rather than the template: what a machine can say about
its own names, and what it correctly refuses to say.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from django.test import TestCase
from django.utils import timezone

from analytics.models import AnalyticsSite, RumDaily

from .machine_context import SECTIONS, sections_for


def a_machine(*hostnames, resources=(), name="probe", declaration="", device=""):
    """The two attributes the resolvers read, and nothing else.

    Deliberately not a real Machine: a section must depend on what a machine
    *declares*, not on how the catalog happens to build one.
    """

    return SimpleNamespace(
        name=name,
        hostnames=tuple(hostnames),
        resources=tuple(resources),
        declaration=declaration,
        route_approval_key=device,
    )


def measure(host, *, pageviews=100, visits=60, sample_interval=1):
    site = AnalyticsSite.objects.create(site_tag=f"tag-{host}", host=host)
    RumDaily.objects.create(
        site=site,
        date=timezone.now().date() - timedelta(days=1),
        dimension=RumDaily.Dimension.PATH,
        value="/",
        pageviews=pageviews,
        visits=visits,
        sample_interval=sample_interval,
    )


def by_id(machine):
    return {section.id: section for section in sections_for(machine)}


class TrafficByNameTests(TestCase):
    def test_a_machine_says_which_of_its_names_are_visited(self):
        measure("busy.example.com", pageviews=900, visits=500)
        measure("quiet.example.com", pageviews=10, visits=5)

        rows = by_id(a_machine("quiet.example.com", "busy.example.com"))["traffic"].records

        self.assertEqual([cell[0].text for cell in rows], ["busy.example.com", "quiet.example.com"])

    def test_an_unmeasured_name_is_listed_as_unmeasured_not_as_zero(self):
        """Opposite conclusions: one is a gap in HQ, the other a fact about use."""

        measure("busy.example.com")

        rows = by_id(a_machine("busy.example.com", "dark.example.com"))["traffic"].records
        dark = next(row for row in rows if row[0].text == "dark.example.com")

        self.assertEqual(dark[1].text, "—")
        self.assertIn("nothing measures", dark[3].text)
        self.assertTrue(dark[1].muted)

    def test_a_machine_nothing_measures_grows_no_band(self):
        self.assertNotIn("traffic", by_id(a_machine("dark.example.com")))

    def test_a_machine_with_no_names_grows_no_band(self):
        self.assertNotIn("traffic", by_id(a_machine()))

    def test_sampling_is_carried_rather_than_presented_as_a_count(self):
        measure("busy.example.com", sample_interval=10)

        row = by_id(a_machine("busy.example.com"))["traffic"].records[0]

        self.assertEqual(row[3].text, "Sampled 1 in 10")
        self.assertTrue(row[3].muted)

    def test_another_machines_traffic_is_not_borrowed(self):
        measure("elsewhere.example.com", pageviews=9999)

        self.assertNotIn("traffic", by_id(a_machine("mine.example.com")))


class RegistryTests(TestCase):
    def test_the_registry_is_the_only_list_of_sections(self):
        """Adding one is a function and an entry, never an edit to the page."""

        self.assertEqual(
            [resolve.__name__ for resolve in SECTIONS],
            ["_identity", "_traffic", "_points_here", "_activity"],
        )

    def test_a_section_with_nothing_to_say_does_not_appear(self):
        self.assertEqual(sections_for(a_machine()), ())


class IdentityTests(TestCase):
    """One machine is declared twice, and the page says so rather than hiding it."""

    def _declare(self, key, kind, **spec):
        from control_plane.models import ManagedResource

        return ManagedResource.objects.create(key=key, kind=kind, spec=spec, enabled=True)

    def test_both_declarations_of_one_machine_are_named(self):
        self._declare("box", "machine", name="box")
        self._declare("box-2", "tailscale.device", name="box")

        rows = by_id(a_machine(name="box", declaration="box", device="box-2"))["identity"].records

        self.assertEqual([row[0].text for row in rows], ["box", "box-2"])
        self.assertEqual([row[1].text for row in rows], ["machine", "tailscale.device"])

    def test_a_suffixed_key_says_why_it_is_suffixed(self):
        """The question this whole band exists to stop anyone asking twice."""

        self._declare("box", "machine", name="box")
        self._declare("box-2", "tailscale.device", name="box")

        rows = by_id(a_machine(name="box", declaration="box", device="box-2"))["identity"].records

        self.assertEqual(rows[0][2].text, "")
        self.assertIn("the plain one was taken", rows[1][2].text)

    def test_a_machine_with_no_declaration_grows_no_band(self):
        self.assertNotIn("identity", by_id(a_machine(name="box")))


class PointsHereTests(TestCase):
    def _declare(self, key, kind, **spec):
        from control_plane.models import ManagedResource

        return ManagedResource.objects.create(key=key, kind=kind, spec=spec, enabled=True)

    def test_a_declaration_answering_this_machines_address_is_related(self):
        """The join nothing made: it relates by address, and has no host field."""

        self._declare("box", "machine", name="box", addresses=["10.9.9.9"])
        self._declare("site-dns", "adguard.rewrite", domain="site.test", answer="10.9.9.9")

        rows = by_id(a_machine(name="box", declaration="box"))["points-here"].records

        self.assertEqual([row[0].text for row in rows], ["site-dns"])
        self.assertIn("10.9.9.9", rows[0][2].text)

    def test_a_machines_own_declarations_are_not_things_that_reach_it(self):
        self._declare("box", "machine", name="box", addresses=["10.9.9.9"])
        self._declare("box-2", "tailscale.device", name="box")

        found = by_id(a_machine(name="box", declaration="box", device="box-2"))

        self.assertNotIn("points-here", found)

    def test_a_declaration_pointing_elsewhere_is_not_borrowed(self):
        self._declare("box", "machine", name="box", addresses=["10.9.9.9"])
        self._declare("other-dns", "adguard.rewrite", domain="other.test", answer="10.1.1.1")

        self.assertNotIn("points-here", by_id(a_machine(name="box", declaration="box")))
