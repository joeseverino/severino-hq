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
from control_plane.models import ManagedResource

from .machine_context import SECTIONS, sections_for


def a_machine(*hostnames, resources=(), name="probe", declaration="", device=""):
    """The attributes the resolvers read, and nothing else.

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


def declare(key, kind, **spec):
    return ManagedResource.objects.create(key=key, kind=kind, spec=spec, enabled=True)


def by_id(machine):
    return {section.id: section for section in sections_for(machine)}


def row_for(machine, hostname):
    rows = by_id(machine)["names"].records
    return next(row for row in rows if row[0].text == hostname)


class NamesTests(TestCase):
    """One row per name, and what supplies it read from the service model."""

    def test_what_supplies_a_name_is_read_rather_than_re_derived(self):
        """The claims come from the service catalog, so the two cannot disagree."""

        declare("site-dns", "adguard.rewrite", domain="site.test", answer="10.9.9.9")

        row = row_for(a_machine("site.test"), "site.test")

        self.assertEqual(row[0].text, "site.test")
        self.assertEqual(row[2].text, "site-dns")  # the DNS column

    def test_a_name_nothing_supplies_says_so_per_column(self):
        """Blank is "nothing supplies this" — not unhealthy, not unmeasured."""

        row = row_for(a_machine("bare.test"), "bare.test")

        for cell in row[1:5]:
            self.assertEqual(cell.text, "—")
            self.assertTrue(cell.muted)

    def test_traffic_rides_along_and_names_absence_as_absence(self):
        measure("busy.test", pageviews=900)
        machine = a_machine("busy.test", "dark.test")

        self.assertEqual(row_for(machine, "busy.test")[-1].text, "900")
        self.assertEqual(row_for(machine, "dark.test")[-1].text, "unmeasured")
        self.assertTrue(row_for(machine, "dark.test")[-1].muted)

    def test_the_busiest_name_is_first(self):
        """The question a machine page raises is which names carry the traffic."""

        measure("busy.test", pageviews=900)
        measure("quiet.test", pageviews=5)

        rows = by_id(a_machine("quiet.test", "dark.test", "busy.test"))["names"].records

        self.assertEqual([row[0].text for row in rows], ["busy.test", "quiet.test", "dark.test"])

    def test_a_machine_with_no_names_grows_no_band(self):
        self.assertNotIn("names", by_id(a_machine()))

    def test_the_band_points_at_the_graph_rather_than_listing_more_rows(self):
        """Every other relationship is an edge, and edges live in the topology."""

        actions = by_id(a_machine("site.test"))["names"].actions

        self.assertEqual(len(actions), 1)
        self.assertIn("topology", actions[0][1])

    def test_another_machines_traffic_is_not_borrowed(self):
        measure("elsewhere.test", pageviews=9999)

        self.assertEqual(row_for(a_machine("mine.test"), "mine.test")[-1].text, "unmeasured")


class IdentityTests(TestCase):
    """One machine is declared twice, and the page says so rather than hiding it."""

    def test_both_declarations_of_one_machine_are_named(self):
        declare("box", "machine", name="box")
        declare("box-2", "tailscale.device", name="box")

        rows = by_id(a_machine(name="box", declaration="box", device="box-2"))["identity"].records

        self.assertEqual([row[0].text for row in rows], ["box", "box-2"])
        self.assertEqual([row[1].text for row in rows], ["machine", "tailscale.device"])

    def test_a_suffixed_key_says_why_it_is_suffixed(self):
        """The question this band exists to stop anyone asking twice."""

        declare("box", "machine", name="box")
        declare("box-2", "tailscale.device", name="box")

        rows = by_id(a_machine(name="box", declaration="box", device="box-2"))["identity"].records

        self.assertEqual(rows[0][2].text, "")
        self.assertIn("the plain one was taken", rows[1][2].text)

    def test_a_machine_with_no_declaration_grows_no_band(self):
        self.assertNotIn("identity", by_id(a_machine(name="box")))


class RegistryTests(TestCase):
    def test_the_registry_is_the_only_list_of_sections(self):
        """Adding one is a function and an entry, never an edit to the page."""

        self.assertEqual(
            [resolve.__name__ for resolve in SECTIONS],
            ["_identity", "_names", "_activity"],
        )

    def test_a_section_with_nothing_to_say_does_not_appear(self):
        self.assertEqual(sections_for(a_machine()), ())
