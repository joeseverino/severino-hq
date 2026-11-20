"""An extension's domain says in the API document what its manifest declares it holds."""

from django.test import SimpleTestCase

from hq.platform.application.plugin_testing import installed, sibling
from hq.platform.application.plugins import NavigationGroup, NavigationItem

from ..tags import Tags


class AnExtensionSaysWhatItHoldsTests(SimpleTestCase):
    def test_its_domain_and_the_group_it_adds_say_what_its_manifest_declares(self):
        group = NavigationGroup("Outdoors", "Time spent outside, and what it took.")
        extension = sibling(
            description="Walks and climbs.",
            navigation=(NavigationItem("Alpha", "alpha:home", "alpha", 40, group.name),),
            navigation_groups=(group,),
        )
        with installed(extension, alone=True):
            tags = Tags({}, {})
            tags.use("example.alpha")
            rendered = {tag["name"]: tag for tag in tags.render()}
        domain = rendered["example.alpha"]
        self.assertEqual(domain["description"], "Walks and climbs.")
        said = rendered[domain["parent"]]["description"]
        self.assertTrue(said.startswith(group.description), said)
        # A group with no operations of its own indexes the domains it holds.
        self.assertIn("(#tag/examplealpha)", said)
