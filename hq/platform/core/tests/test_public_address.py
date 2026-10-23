"""The public-address fragment: a GET reads what HQ holds, a POST looks up."""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.template.loader import render_to_string
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from hq.domains.control_plane.models import AddressReading
from hq.platform.application.request_connection import connection
from hq.platform.application.tests.markup import select, text
from hq.platform.application.tests.test_lookup import GLOBAL as ADDRESS
from hq.platform.application.tests.test_request_connection import A_TAILNET_ADDRESS, a_device, a_request, a_tailnet

STORED = {
    "ok": True,
    "address": ADDRESS,
    "version": 4,
    "hostnames": ["host.example.net"],
    "note": "",
    "allocation": {
        "organization": "Example Networks",
        "name": "EXAMPLE-NET",
        "country": "",
        "prefixes": [],
    },
}


class PublicAddressViewTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_user("op", password="x" * 20))
        self.url = reverse("tool_public_address")

    def test_a_get_serves_the_stored_reading_and_asks_nobody(self):
        AddressReading.objects.create(address=ADDRESS, reading=STORED, observed_at=timezone.now())
        with patch("hq.platform.application.lookup.look_up_address", side_effect=AssertionError("a GET looked up")):
            response = self.client.get(self.url, {"address": ADDRESS})

        self.assertIn("host.example.net", text(response))
        self.assertContains(response, "Look up again")
        self.assertContains(response, "csrfmiddlewaretoken")

    def test_a_get_with_nothing_stored_writes_nothing_and_offers_the_lookup(self):
        with patch("hq.platform.application.lookup.look_up_address", side_effect=AssertionError("a GET looked up")):
            response = self.client.get(self.url, {"address": ADDRESS})

        self.assertContains(response, "Not looked up yet")
        self.assertContains(response, "data-public-address-lookup")
        self.assertFalse(AddressReading.objects.exists())

    def test_a_non_routable_address_is_answered_locally_with_no_lookup_offered(self):
        response = self.client.get(self.url, {"address": "10.9.9.9"})

        self.assertContains(response, "private address")
        self.assertNotContains(response, "data-public-address-lookup")

    def test_a_malformed_address_is_refused(self):
        response = self.client.get(self.url, {"address": "not-an-address"})

        self.assertContains(response, "That is not an IP address.")

    def test_a_post_looks_the_address_up_again(self):
        with patch("hq.platform.application.lookup.look_up_address", return_value=STORED) as look:
            response = self.client.post(
                self.url,
                {"address": ADDRESS},
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )

        self.assertContains(response, "Example Networks")
        command = look.call_args.args[0]
        self.assertEqual((command.address, command.refresh), (ADDRESS, True))

    def test_a_form_post_without_script_returns_to_the_connection_page(self):
        with patch("hq.platform.application.lookup.look_up_address") as look:
            response = self.client.post(self.url, {"address": ADDRESS})

        self.assertRedirects(response, reverse("connection"), fetch_redirect_response=False)
        look.assert_not_called()

    @override_settings(ALLOWED_HOSTS=["hq.example.test", "testserver"])
    def test_the_connection_panel_loads_this_fragment_for_the_peers_public_address(self):
        a_tailnet(
            a_device("a-laptop", A_TAILNET_ADDRESS, direct_endpoint=f"{ADDRESS}:41641"),
            a_device("hq-host", "100.64.0.9", observer=True),
        )
        panel = render_to_string("core/_connection_panel.html", {"connection": connection(a_request())})
        (region,) = select(panel, "data-fragment-load")
        AddressReading.objects.create(address=ADDRESS, reading=STORED, observed_at=timezone.now())

        self.assertTrue(region.attrs.get("data-fragment-failure"))
        self.assertIn("host.example.net", text(self.client.get(region.attrs["data-fragment"])))
