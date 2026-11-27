"""The public-address fragment: a GET reads what HQ holds, a POST looks up."""

from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from hq.domains.control_plane.observations.public_registry import REVERSE_DNS_KIND
from hq.platform.application.facts import kept_by
from hq.platform.application.lookup import LOOK_UP_ADDRESS
from hq.platform.application.plugin_testing import form_keyed
from hq.platform.application.security import OPERATOR_CAPABILITIES, Capability, Principal
from hq.platform.application.tests.test_lookup import GLOBAL as ADDRESS
from hq.platform.core.models import AuditLog
from hq.platform.core.testing import operator


def held():
    return kept_by(REVERSE_DNS_KIND, "address").get(ADDRESS)


def nobody_asked():
    """Both registries, refusing to be asked."""

    asked = AssertionError("a registry was asked")
    return (
        patch("hq.platform.application.lookup.resolve", side_effect=asked),
        patch("hq.platform.application.lookup.registry", side_effect=asked),
    )


class PublicAddressViewTests(TestCase):
    def setUp(self):
        operator(self.client, "op")
        self.url = reverse("tool_public_address")

    def look_up(self, address):
        return self.client.post(self.url, form_keyed({"address": address}), HTTP_X_REQUESTED_WITH="XMLHttpRequest")

    def test_an_operator_without_the_grant_is_refused_in_the_fragment(self):
        lacking = Principal("op", "web", OPERATOR_CAPABILITIES - {Capability.LOOK_UP_PUBLIC_RECORDS})
        resolver, registry = nobody_asked()
        with resolver, registry, patch("hq.platform.application.web_writes.web_principal", return_value=lacking):
            self.look_up(ADDRESS)

        self.assertIsNone(held())
        denied = AuditLog.objects.filter(action=AuditLog.Action.DENIED, metadata__capability=LOOK_UP_ADDRESS)
        self.assertEqual(denied.count(), 1)

    def test_a_form_post_without_script_returns_to_the_connection_page(self):
        resolver, registry = nobody_asked()
        with resolver, registry:
            response = self.client.post(self.url, form_keyed({"address": ADDRESS}))

        self.assertRedirects(response, reverse("connection"), fetch_redirect_response=False)
        self.assertIsNone(held())
