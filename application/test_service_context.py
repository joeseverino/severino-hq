"""The name is the join, and nothing stores the tie twice.

HQ's infrastructure half relates to almost nothing and its knowledge half
relates to everything. What connects them is already on both sides: a project
says where it is published, a document says which system it is about, an audit
entry says which object it changed. These prove the page reads those rather than
asking for a column that points at infrastructure.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from control_plane.models import ManagedResource
from core.models import AuditLog
from projects.models import Project

from .service_context import ServiceSection, sections_for
from .services import service_or_prospect


def a_service(hostname="probe.example.com"):
    ManagedResource.objects.create(
        key="probe-dns",
        kind="adguard.rewrite",
        spec={"domain": hostname, "answer": "10.0.0.1"},
        generation=1,
        observed_generation=1,
    )
    return service_or_prospect(hostname)


def a_project(**fields):
    return Project.objects.create(
        **{
            "name": "A Project",
            "slug": "a-project",
            "category": "homelab",
            "status": "active",
            "public_url": "https://probe.example.com",
            **fields,
        }
    )


def by_id(service):
    return {section.id: section for section in sections_for(service)}


class DeliveryTests(TestCase):
    def test_a_project_publishing_this_name_is_the_project_for_it(self):
        """No column points a service at a project. One says where it is
        published, which is the same statement read the other way."""

        a_project()

        self.assertIn("delivery", by_id(a_service()))

    def test_a_project_publishing_a_different_name_is_not(self):
        a_project(public_url="https://elsewhere.example.com")

        self.assertNotIn("delivery", by_id(a_service()))

    def test_the_project_and_the_repository_are_both_links(self):
        a_project(repository_url="https://github.com/example/a-project")

        project, repository, _ = by_id(a_service())["delivery"].records[0]

        self.assertTrue(project.url)
        self.assertEqual(repository.text, "example/a-project")
        self.assertEqual(repository.url, "https://github.com/example/a-project")
        self.assertTrue(repository.external)

    def test_a_project_without_a_repository_says_so_rather_than_linking(self):
        a_project(repository_url="")

        _, repository, _ = by_id(a_service())["delivery"].records[0]

        self.assertEqual(repository.url, "")
        self.assertTrue(repository.muted)


class ActivityTests(TestCase):
    def test_changes_to_the_resources_behind_the_name_are_shown(self):
        service = a_service()
        AuditLog.objects.create(
            action="update", object_type="ManagedResource",
            object_id="probe-dns", object_repr="probe-dns",
        )

        self.assertIn("activity", by_id(service))

    def test_changes_to_something_else_are_not(self):
        """This answers what changed here, not what changed."""

        service = a_service()
        AuditLog.objects.create(
            action="update", object_type="ManagedResource",
            object_id="unrelated", object_repr="unrelated",
        )

        self.assertNotIn("activity", by_id(service))


class PageTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="operator", password="not-a-real-password"
        )
        self.client.force_login(self.user)

    def test_the_page_renders_what_the_registry_produced(self):
        a_project(name="A Project", repository_url="https://github.com/example/a")
        a_service()

        response = self.client.get(
            reverse("control_plane:service",
                    kwargs={"hostname": "probe.example.com"})
        )

        self.assertContains(response, "Delivery")
        self.assertContains(response, "example/a")
        self.assertContains(response, 'aria-label="On this page"')
        self.assertContains(response, 'href="#delivery"')
        self.assertContains(response, 'id="delivery"')
        self.assertContains(response, 'class="page-head-status ', count=1)

    def test_a_service_nothing_else_knows_about_grows_no_bands(self):
        a_service()

        response = self.client.get(
            reverse("control_plane:service",
                    kwargs={"hostname": "probe.example.com"})
        )

        self.assertNotContains(response, "Delivery")
        self.assertNotContains(response, "Recent changes")

    def test_the_registry_is_the_only_list_of_sections(self):
        """A band appears because a resolver produced it, so adding one is a
        function and an entry rather than an edit to the page."""

        from .service_context import SECTIONS

        self.assertEqual(
            [resolve.__name__ for resolve in SECTIONS],
            ["_delivery", "_activity", "_traffic"],
        )

    def test_service_section_ids_share_the_page_navigation_contract(self):
        with self.assertRaisesRegex(ValueError, "valid page section id"):
            ServiceSection("Not valid", "Broken", (), ())


class TrafficSectionTests(TestCase):
    """The join is the hostname, and an unmeasured host is not a dead one."""

    def _measure(self, host, *, pageviews=120, visits=90, sample_interval=1, days_ago=1):
        from datetime import timedelta

        from django.utils import timezone

        from analytics.models import AnalyticsSite, RumDaily

        site = AnalyticsSite.objects.create(site_tag=f"tag-{host}", host=host)
        RumDaily.objects.create(
            site=site,
            date=timezone.now().date() - timedelta(days=days_ago),
            dimension=RumDaily.Dimension.PATH,
            value="/",
            pageviews=pageviews,
            visits=visits,
            sample_interval=sample_interval,
        )
        return site

    def test_a_measured_host_gets_its_traffic_without_a_foreign_key(self):
        self._measure("probe.example.com")
        section = next(s for s in sections_for(a_service()) if s.id == "traffic")
        self.assertEqual(section.records[0][0].text, "120")
        self.assertEqual(section.records[0][1].text, "90")
        self.assertEqual(section.records[0][2].text, "Counted")

    def test_a_host_nothing_measures_renders_no_band(self):
        # Not an empty table: that would read as a dead site rather than an
        # unmeasured one, and those are opposite conclusions.
        self.assertFalse([s for s in sections_for(a_service()) if s.id == "traffic"])

    def test_sampling_is_carried_rather_than_presented_as_a_count(self):
        self._measure("probe.example.com", sample_interval=10)
        section = next(s for s in sections_for(a_service()) if s.id == "traffic")
        self.assertEqual(section.records[0][2].text, "Sampled 1 in 10")

    def test_another_hosts_traffic_is_not_borrowed(self):
        self._measure("someone-else.example.com", pageviews=9999)
        self.assertFalse([s for s in sections_for(a_service()) if s.id == "traffic"])

    def test_the_host_join_is_case_and_trailing_dot_insensitive(self):
        # Ingest stores it normalised; the lookup is what must tolerate mess.
        self._measure("probe.example.com")
        from .analytics import traffic_for_hosts

        self.assertEqual(
            traffic_for_hosts({"  PROBE.example.com. "}, days=7)["probe.example.com"]["pageviews"],
            120,
        )

    def test_traffic_for_many_hosts_costs_one_query(self):
        for index in range(5):
            self._measure(f"h{index}.example.com")
        from .analytics import traffic_for_hosts

        hosts = {f"h{index}.example.com" for index in range(5)}
        with self.assertNumQueries(1):
            self.assertEqual(len(traffic_for_hosts(hosts, days=7)), 5)


class OneWindowTests(TestCase):
    """The page, the graph and the query must mean the same week.

    Two sevens in two modules agree only until someone changes one, and then a
    service page and the topology node for the same host quietly disagree with
    nothing on screen to show for it.
    """

    def test_the_page_and_the_graph_share_one_window(self):
        from . import service_context, topology
        from .analytics import HOST_TRAFFIC_DAYS

        self.assertIs(service_context.HOST_TRAFFIC_DAYS, HOST_TRAFFIC_DAYS)
        self.assertIs(topology.HOST_TRAFFIC_DAYS, HOST_TRAFFIC_DAYS)

    def test_no_module_declares_a_host_window_of_its_own(self):
        import pathlib
        import re

        root = pathlib.Path(__file__).resolve().parent
        owner = root / "analytics.py"
        declares = re.compile(r"^[A-Z_]*HOST_TRAFFIC_DAYS\s*=", re.MULTILINE)
        offenders = [
            path.name
            for path in sorted(root.glob("*.py"))
            if path != owner and declares.search(path.read_text())
        ]
        self.assertEqual(offenders, [])

    def test_the_template_does_not_restate_the_window(self):
        import pathlib

        template = (
            pathlib.Path(__file__).resolve().parents[1]
            / "templates/control_plane/topology.html"
        ).read_text()
        self.assertIn("{{ traffic_window_days }}", template)
        self.assertNotIn("Traffic · 7 days", template)
