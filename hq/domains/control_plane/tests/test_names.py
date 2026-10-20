"""How a registry's organization is said."""

from django.test import SimpleTestCase

from hq.domains.control_plane.names import organization_name


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
