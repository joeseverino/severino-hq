"""The name is the join, and nothing stores the tie twice.

HQ's infrastructure half relates to almost nothing and its knowledge half
relates to everything. What connects them is already on both sides: a project
says where it is published, a document says which system it is about, an audit
entry says which object it changed. These prove the page reads those rather than
asking for a column that points at infrastructure.
"""

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from hq.domains.control_plane.models import ManagedResource
from hq.domains.projects.models import Project
from hq.platform.core.models import AuditLog

from ..service_context import Cell, ServiceSection, sections_for
from ..services import service_or_prospect
from ..ui import MISSING


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


def project_tile(service):
    from ..service_context import service_summary

    return next((item for item in service_summary(service) if item.label == "Project"), None)


class ProjectTileTests(TestCase):
    def test_a_project_publishing_this_name_is_the_project_for_it(self):
        """No column points a service at a project. One says where it is
        published, which is the same statement read the other way."""

        a_project()

        self.assertEqual(project_tile(a_service()).value, "A Project")

    def test_a_project_publishing_a_different_name_is_not(self):
        a_project(public_url="https://elsewhere.example.com")

        self.assertIsNone(project_tile(a_service()))

    def test_the_project_links_and_names_its_repository(self):
        a_project(repository_url="https://github.com/example/a-project")

        tile = project_tile(a_service())

        self.assertTrue(tile.link.url)
        self.assertIn("example/a-project", tile.detail)

    def test_a_project_without_a_repository_names_none(self):
        a_project(repository_url="")

        self.assertEqual(project_tile(a_service()).detail, "")


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

        # The project is a tile of the band.
        self.assertContains(response, ">Project<")
        self.assertContains(response, "example/a")
        self.assertContains(response, 'aria-label="On this page"')
        self.assertNotContains(response, 'id="delivery"')
        service = response.context["service"]
        # Health once, first in the band, its tile in its tone.
        self.assertContains(response, f'<div class="band-fact tone-{service.status}">', count=1)
        self.assertContains(response, f"<strong>{service.status_label}</strong>")

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

        from ..service_context import SECTIONS

        self.assertEqual(
            [resolve.__name__ for resolve in SECTIONS],
            ["_access", "_activity"],
        )

    def test_service_section_ids_share_the_page_navigation_contract(self):
        with self.assertRaisesRegex(ValueError, "valid page section id"):
            ServiceSection("Not valid", "Broken", (), ())


class TrafficSectionTests(TestCase):
    """The join is the hostname, and an unmeasured host is not a dead one."""

    def _measure(self, host, *, pageviews=120, visits=90, sample_interval=1, days_ago=1):
        from datetime import timedelta

        from django.utils import timezone

        from hq.domains.analytics.models import AnalyticsSite, RumDaily

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

    def traffic(self):
        from ..service_context import service_summary

        return next((item for item in service_summary(a_service()) if item.label.startswith("Traffic")), None)

    def test_a_measured_host_gets_its_traffic_without_a_foreign_key(self):
        self._measure("probe.example.com")
        found = self.traffic()
        self.assertEqual((found.value, found.detail), ("120 pageviews", "90 visits · counted"))

    def test_a_host_nothing_measures_says_nothing(self):
        # Not "0 pageviews": that would read as a dead site rather than an
        # unmeasured one, and those are opposite conclusions.
        self.assertIsNone(self.traffic())

    def test_sampling_is_carried_rather_than_presented_as_a_count(self):
        self._measure("probe.example.com", sample_interval=10)
        self.assertEqual(self.traffic().detail, "90 visits · sampled 1 in 10")

    def test_another_hosts_traffic_is_not_borrowed(self):
        self._measure("someone-else.example.com", pageviews=9999)
        self.assertIsNone(self.traffic())

    def test_the_host_join_is_case_and_trailing_dot_insensitive(self):
        # Ingest stores it normalised; the lookup is what must tolerate mess.
        self._measure("probe.example.com")
        from ..analytics import traffic_for_hosts

        self.assertEqual(
            traffic_for_hosts({"  PROBE.example.com. "}, days=7)["probe.example.com"]["pageviews"],
            120,
        )

    def test_traffic_for_many_hosts_costs_one_query(self):
        for index in range(5):
            self._measure(f"h{index}.example.com")
        from ..analytics import traffic_for_hosts

        hosts = {f"h{index}.example.com" for index in range(5)}
        with self.assertNumQueries(1):
            self.assertEqual(len(traffic_for_hosts(hosts, days=7)), 5)


class OneWindowTests(TestCase):
    """The page, the graph and the query must mean the same week. Two sevens in
    two modules agree only until one changes, and then a service page and the
    topology node for the same host quietly disagree with nothing on screen to
    show for it.
    """

    def test_the_page_and_the_graph_share_one_window(self):
        from .. import service_context, topology
        from ..analytics import HOST_TRAFFIC_DAYS

        self.assertIs(service_context.HOST_TRAFFIC_DAYS, HOST_TRAFFIC_DAYS)
        self.assertIs(topology.HOST_TRAFFIC_DAYS, HOST_TRAFFIC_DAYS)

    def test_no_module_declares_a_host_window_of_its_own(self):
        import pathlib
        import re

        root = pathlib.Path(__file__).parent.resolve().parent
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
            pathlib.Path(__file__).resolve().parents[4]
            / "templates/control_plane/_topology_node_body.html"
        ).read_text()
        self.assertIn("{{ traffic_window_days|counted", template)
        self.assertNotIn("Traffic · 7 days", template)


class PartRowTests(TestCase):
    def test_every_step_on_a_path_says_what_changing_it_would_do(self):
        from ..path_dependencies import consequence_of
        from ..path_model import Hop

        for step in ("dns", "network", "machine", "ingress", "upstream", "container"):
            self.assertTrue(consequence_of(Hop(step, "Part", "name")), step)

    def test_declared_and_read_health_share_one_set_of_tones(self):
        from types import SimpleNamespace

        from ..service_context import PartRow

        declared = PartRow(claim=SimpleNamespace(health={"state": "healthy", "label": "Healthy"}, kind="npm.proxy_host"))
        broken = PartRow(claim=SimpleNamespace(health={"state": "degraded", "label": "Needs attention"}, kind="npm.proxy_host"))
        read = PartRow(observed_health=("Online", "pill-reachable"))

        self.assertEqual(declared.health, ("Healthy", "pill-reachable"))
        self.assertEqual(broken.health, ("Needs attention", "pill-unreachable"))
        self.assertEqual(read.health, ("Online", "pill-reachable"))


class EmptyColumnTests(SimpleTestCase):
    def test_a_column_no_row_fills_is_not_drawn(self):
        section = ServiceSection(
            id="example",
            label="Example",
            columns=("Name", "Note", "State"),
            records=((Cell("one"), Cell(""), Cell("ok")), (Cell("two"), Cell(""), Cell(MISSING))),
        )

        self.assertEqual(section.columns, ("Name", "State"))
        self.assertEqual([[c.text for c in row] for row in section.records], [["one", "ok"], ["two", MISSING]])

    def test_an_unknown_value_keeps_its_column(self):
        section = ServiceSection(
            id="example",
            label="Example",
            columns=("Name", "Note"),
            records=((Cell("one"), Cell(MISSING)),),
        )

        self.assertEqual(section.columns, ("Name", "Note"))
