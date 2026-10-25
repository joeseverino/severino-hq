"""The API names a connection's reads and changes as the connections page draws them."""

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from hq.platform.application.tests.test_page_projections import page_lists, populate, served_lists

from ..testing import ContractClient
from .test_api import ISSUER, RESOURCE, _serving, _token


@override_settings(OIDC_ISSUER=ISSUER, SEVERINO_API_RESOURCE=RESOURCE, SEVERINO_SITE_HOST="hq.example.com")
class ConnectionStandingTests(TestCase):
    client_class = ContractClient

    def setUp(self):
        populate()

    def test_a_connections_reads_and_changes_are_the_pages(self):
        with _serving():
            response = self.client.get(
                "/api/v2/resources/connection.standing/example-tailnet/",
                HTTP_AUTHORIZATION=f"Bearer {_token(scope='read')}",
            )
        self.assertEqual(response.status_code, 200)
        self.client.force_login(get_user_model().objects.create_user("operator", password="pw", is_staff=True))
        reads, changes = page_lists(self.client, "example-tailnet")

        self.assertTrue(reads)
        self.assertEqual(served_lists(response.json()["data"]), (reads, changes))
