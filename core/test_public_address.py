"""The public-address fragment: a GET reads what HQ holds, a POST looks up."""

from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from application.test_lookup import GLOBAL as ADDRESS
from control_plane.models import AddressReading

STORED = {
    "ok": True,
    "address": ADDRESS,
    "version": 4,
    "hostnames": ["host.example.net"],
    "note": "",
    "allocation": {
        "organisation": "Example Networks",
        "name": "EXAMPLE-NET",
        "country": "",
        "prefixes": [],
    },
}


class PublicAddressViewTests(TestCase):
    def setUp(self):
        self.client.force_login(
            get_user_model().objects.create_user("op", password="x" * 20)
        )
        self.url = reverse("tool_public_address")

    def test_a_get_serves_the_stored_reading_and_asks_nobody(self):
        AddressReading.objects.create(
            address=ADDRESS, reading=STORED, observed_at=timezone.now()
        )
        with patch(
            "application.lookup.look_up_address", side_effect=AssertionError("a GET looked up")
        ):
            response = self.client.get(self.url, {"address": ADDRESS})

        self.assertContains(response, "host.example.net")
        self.assertContains(response, "Look up again")
        self.assertContains(response, "csrfmiddlewaretoken")

    def test_a_get_with_nothing_stored_writes_nothing_and_offers_the_lookup(self):
        with patch(
            "application.lookup.look_up_address", side_effect=AssertionError("a GET looked up")
        ):
            response = self.client.get(self.url, {"address": ADDRESS})

        self.assertContains(response, "Not looked up yet")
        self.assertContains(response, 'data-public-address-lookup')
        self.assertFalse(AddressReading.objects.exists())

    def test_a_non_routable_address_is_answered_locally_with_no_lookup_offered(self):
        response = self.client.get(self.url, {"address": "10.9.9.9"})

        self.assertContains(response, "not routable")
        self.assertNotContains(response, "data-public-address-lookup")

    def test_a_malformed_address_is_refused(self):
        response = self.client.get(self.url, {"address": "not-an-address"})

        self.assertContains(response, "That is not an IP address.")

    def test_a_post_looks_the_address_up_again(self):
        with patch("application.lookup.look_up_address", return_value=STORED) as look:
            response = self.client.post(
                self.url,
                {"address": ADDRESS},
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )

        self.assertContains(response, "Example Networks")
        command = look.call_args.args[0]
        self.assertEqual((command.address, command.refresh), (ADDRESS, True))

    def test_a_form_post_without_script_returns_to_the_connection_page(self):
        with patch("application.lookup.look_up_address") as look:
            response = self.client.post(self.url, {"address": ADDRESS})

        self.assertRedirects(response, reverse("connection"), fetch_redirect_response=False)
        look.assert_not_called()

    def test_the_connection_panel_loads_the_fragment_through_the_deferred_loader(self):
        from django.template.loader import get_template

        source = get_template("core/_connection_panel.html").template.source
        self.assertIn('data-deferred="{% url \'tool_public_address\' %}?address=', source)
        self.assertIn("data-deferred-failure=", source)
