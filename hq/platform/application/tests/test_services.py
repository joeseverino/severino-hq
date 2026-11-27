"""What the service view has to get right to be worth trusting.

Every hostname here is under ``example.com`` or ``example.test``. This repository
is public, and a fixture is documentation whether or not it was meant to be."""

from django.test import TestCase


class ParkedNameTests(TestCase):
    """A record that exists is not a name that answers.

    Boards ask whether a record is declared and reconciled, and both are true of
    a name pointed at an address reserved for documentation, so a parked
    domain reads as a working service.
    """

    def test_a_documentation_or_unspecified_address_is_a_wiring_fault_and_a_real_one_is_not(self):
        from ..services import _points_nowhere
        from ..whereabouts import Origin

        for address, parked in (
            ("192.0.2.1", True),
            ("203.0.113.9:443", True),
            ("0.0.0.0", True),
            ("10.0.0.9:8000", False),
            ("example.pages.dev", False),
        ):
            with self.subTest(address):
                self.assertEqual(bool(_points_nowhere(Origin(address=address))), parked)
        self.assertEqual(_points_nowhere(None), "")


class WwwIsTheSameSiteTests(TestCase):
    """`www.example.com` and `example.com` as two address records are one site.

    A CNAME says "I am that name" and already folds. An address record says
    only where to go, so without this the pair reads as two services with two
    certificates and two verdicts about one website.

    Narrow on purpose. Every other subdomain sharing an address is a different
    service on one host: mail and a quiz on one cPanel are not each other, so
    the rule is the one prefix that conventionally means the same site.
    """

    def aliases(self, origins, declared=None):
        from ..service_declarations import _aliases

        return _aliases(declared or set(origins), origins)

    def test_only_www_at_the_apexs_own_address_folds_into_it(self):
        """Two addresses is two places, whatever the names suggest."""
        for origins, folded in (
            ({"example.com": "203.0.113.9", "www.example.com": "203.0.113.9"}, {"www.example.com": "example.com"}),
            ({"example.com": "203.0.113.9", "www.example.com": "203.0.113.10"}, {}),
            ({"example.com": "203.0.113.9", "mail.example.com": "203.0.113.9"}, {}),
            ({"www.example.com": "203.0.113.9"}, {}),
        ):
            with self.subTest(origins):
                self.assertEqual(self.aliases(origins), folded)
