"""Which records a set of connections lets HQ adopt, asked of the test itself."""

from django.test import SimpleTestCase

from hq.domains.control_plane.models import ProviderConnection

from ..adoption import manages_through


def reported(ref: str, *, manages: bool, provider: str = "cloudflare_dns") -> ProviderConnection:
    return ProviderConnection(provider=provider, connection_ref=ref, manages=manages)


class ManagesThroughTests(SimpleTestCase):
    def test_a_kind_no_provider_declares_manages_nothing(self):
        self.assertFalse(manages_through([reported("example-a", manages=True)])("example.unknown"))
