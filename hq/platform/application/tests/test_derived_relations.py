"""Relations a page reads from the other end: a certificate's services, a
connection's problems and purpose, a backlog's own things, a list under its types."""

from django.test import TestCase
from django.urls import reverse

from hq.domains.control_plane.models import ProviderConnection
from hq.platform.application.markup_testing import hrefs, with_class
from hq.platform.core.testing import connection, operator

from ..connection_context import connections_context
from ..entity_links import entity_link
from ..security import cli_principal
from .host_only import host_only
from .test_relationships import estate


class ConnectionRowsTests(TestCase):
    def setUp(self):
        estate(1)
        connection("cloudflare_api", "example-broken")
        ProviderConnection.objects.filter(connection_ref="example-broken").update(reachable=False)

    def context(self):
        with host_only():
            return connections_context(principal=cli_principal())

    def test_a_connection_with_an_open_problem_counts_it_and_links_to_it(self):
        found = self.context()

        row = next(row for row in found.rows if row.instance.connection_ref == "example-broken")
        about = entity_link("connection", row.instance.label).url
        self.assertGreaterEqual(row.problems, 1)
        operator(self.client, "owner", email="owner@example.test")
        with host_only():
            queue = self.client.get(row.problems_url)
            page = self.client.get(reverse("control_plane:connections"))
        self.assertEqual(
            {item["subject"]["url"] for group in queue.context["action_groups"] for item in group["items"]},
            {about},
        )
        self.assertIn(row.problems_url, hrefs(page))


class RecordListTests(TestCase):
    def test_the_page_draws_a_group_for_each_type_and_filters_by_the_url(self):
        estate(2)
        operator(self.client, "owner", email="owner@example.test")

        page = self.client.get(reverse("control_plane:list"))
        one = self.client.get(reverse("control_plane:list"), {"q": "s0.example.com"})

        self.assertTrue(with_class(page, "row-group"))
        self.assertGreater(page.context["records"].total, one.context["records"].shown)
        self.assertEqual(one.context["records"].shown, 1)
