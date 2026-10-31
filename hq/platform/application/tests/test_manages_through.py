"""Which records a set of connections lets HQ adopt, asked of the test itself."""

from django.test import SimpleTestCase, TestCase

from hq.domains.control_plane.models import ProviderConnection

from ..adoption import manages_through
from ..adoption_testing import connection

RECORD_KIND = "cloudflare.dns_record"


def reported(ref: str, *, manages: bool, provider: str = "cloudflare_dns") -> ProviderConnection:
    return ProviderConnection(provider=provider, connection_ref=ref, manages=manages)


class ManagesThroughTests(SimpleTestCase):
    def test_a_named_connection_decides_alone(self):
        manages = manages_through([reported("example-a", manages=True), reported("example-b", manages=False)])

        self.assertTrue(manages(RECORD_KIND, "example-a"))
        self.assertFalse(manages(RECORD_KIND, "example-b"))
        self.assertFalse(manages(RECORD_KIND, "example-missing"))

    def test_naming_none_needs_every_connection_of_the_kind_to_manage(self):
        every = manages_through([reported("example-a", manages=True), reported("example-b", manages=True)])
        some = manages_through([reported("example-a", manages=True), reported("example-b", manages=False)])

        self.assertTrue(every(RECORD_KIND))
        self.assertFalse(some(RECORD_KIND))

    def test_naming_none_with_no_connection_of_the_kind_manages_nothing(self):
        self.assertFalse(manages_through([])(RECORD_KIND))
        self.assertFalse(manages_through([reported("example-a", manages=True, provider="example")])(RECORD_KIND))

    def test_a_kind_no_provider_declares_manages_nothing(self):
        self.assertFalse(manages_through([reported("example-a", manages=True)])("example.unknown"))


class ManagesThroughReadsTests(TestCase):
    def test_it_reads_the_connections_on_the_first_ask_only(self):
        connection("cloudflare_dns", "example-dns")

        with self.assertNumQueries(0):
            manages = manages_through()
        with self.assertNumQueries(1):
            self.assertTrue(manages(RECORD_KIND))
        with self.assertNumQueries(0):
            self.assertTrue(manages(RECORD_KIND, "example-dns"))
