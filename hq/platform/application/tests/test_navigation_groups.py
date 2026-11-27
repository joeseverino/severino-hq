"""A nav group an extension adds says what it is for, once, in the manifest
whose items are in it."""

from django.test import SimpleTestCase

from ..plugins import DeclarationRefused, NavigationGroup


class NavigationGroupTests(SimpleTestCase):
    def test_a_group_says_its_name_and_what_it_is_for(self):
        for name, description in (("", "Something."), ("Outdoors", " ")):
            with self.subTest(name=name, description=description), self.assertRaises(DeclarationRefused):
                NavigationGroup(name, description)
