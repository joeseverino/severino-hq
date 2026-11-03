"""A nav group an extension adds says what it is for, once, in the manifest
whose items are in it."""

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

from ..domains import NAVIGATION_GROUPS
from ..plugin_testing import installed, sibling
from ..plugins import DeclarationRefused, NavigationGroup, NavigationItem, installed_plugins

ITEM = NavigationItem("Alpha", "alpha:home", "alpha", 40, "Outdoors")
GROUP = NavigationGroup("Outdoors", "Time spent outside, and what it took.")


def _refused_fields(**manifest_fields):
    try:
        sibling(**manifest_fields)
    except DeclarationRefused as refused:
        return {field for field, _ in refused.violations}
    return set()


class NavigationGroupTests(SimpleTestCase):
    def test_a_group_says_its_name_and_what_it_is_for(self):
        for name, description in (("", "Something."), ("Outdoors", " ")):
            with self.subTest(name=name, description=description), self.assertRaises(DeclarationRefused):
                NavigationGroup(name, description)

    def test_a_manifest_says_what_a_group_is_for_only_where_its_items_are(self):
        self.assertEqual(_refused_fields(navigation=(ITEM,), navigation_groups=(GROUP,)), set())
        self.assertEqual(_refused_fields(navigation_groups=(GROUP,)), {"navigation_groups"})
        self.assertEqual(_refused_fields(navigation=(ITEM,), navigation_groups=(GROUP, GROUP)), {"navigation_groups"})

    def test_a_group_says_what_it_is_for_once_in_the_installed_set(self):
        host_group = next(iter(NAVIGATION_GROUPS))
        host_item = NavigationItem("Alpha", "alpha:home", "alpha", 40, host_group)
        for case, siblings in (
            (
                "two extensions",
                (
                    sibling(navigation=(ITEM,), navigation_groups=(GROUP,)),
                    sibling(identifier="example.beta", navigation=(ITEM,), navigation_groups=(GROUP,)),
                ),
            ),
            (
                "the host and an extension",
                (
                    sibling(
                        navigation=(host_item,), navigation_groups=(NavigationGroup(host_group, "Another purpose."),)
                    ),
                ),
            ),
        ):
            with self.subTest(case), installed(*siblings), self.assertRaises(ImproperlyConfigured):
                installed_plugins()
