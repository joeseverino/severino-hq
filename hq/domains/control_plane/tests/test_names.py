"""How a registry's organization is said, and what counts as a host name."""

from django.test import SimpleTestCase

from hq.domains.control_plane.names import is_hostname, organization_name


class OrganizationNameTests(SimpleTestCase):
    def test_a_legal_suffix_is_dropped(self):
        for raw, said in (
            ("Example Registrar, Inc.", "Example Registrar"),
            ("Example Hosting LLC", "Example Hosting"),
            ("Example Networks Ltd", "Example Networks"),
            ("Example GmbH", "Example"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(organization_name(raw), said)

    def test_the_name_itself_is_kept(self):
        # "Co" inside a word is not a suffix, and a name that is only a suffix stays.
        self.assertEqual(organization_name("Comcast Cable"), "Comcast Cable")
        self.assertEqual(organization_name("Inc"), "Inc")
        self.assertEqual(organization_name(""), "")


class IsHostnameTests(SimpleTestCase):
    def test_an_address_is_not_a_hostname(self):
        for address in ("192.0.2.10", "198.51.100.7.", "2001:db8::1", "[2001:db8::1]", "::1"):
            with self.subTest(address=address):
                self.assertFalse(is_hostname(address))

    def test_a_name_is_a_hostname(self):
        for name in ("app.example.com", "Example.COM.", "localhost", "192-0-2-10.example.net", "10.example"):
            with self.subTest(name=name):
                self.assertTrue(is_hostname(name))
