"""MCP names a connection's reads and changes as the connections page draws them."""

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from hq.platform.application.security import mcp_principal
from hq.platform.application.tests.test_page_projections import page_lists, populate, served_lists

from .. import services
from ..identity import reset_principal, set_principal


@override_settings(SEVERINO_SITE_HOST="hq.example.com")
class ConnectionStandingTests(TestCase):
    def setUp(self):
        populate()
        bound = set_principal(mcp_principal())
        self.addCleanup(reset_principal, bound)

    def test_a_connections_reads_and_changes_are_the_pages(self):
        item = services.get_resource("connection.standing", "example-tailnet")
        self.client.force_login(get_user_model().objects.create_user("operator", password="pw", is_staff=True))
        reads, changes = page_lists(self.client, "example-tailnet")

        self.assertTrue(reads)
        self.assertEqual(served_lists(item), (reads, changes))
