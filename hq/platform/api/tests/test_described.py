"""The API document says what each thing a client sends or reads is."""

import re

from django.test import SimpleTestCase

from hq.platform.application.contracts import EFFECTS
from hq.platform.application.plugin_testing import installed, sibling
from hq.platform.application.plugins import NavigationGroup, NavigationItem

from .. import openapi


class DescribedTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.document = openapi.document()

    def _parameters(self):
        shared = self.document["components"]["parameters"]
        for path, item in self.document["paths"].items():
            for method, operation in item.items():
                for written in operation.get("parameters", []):
                    parameter = shared[written["$ref"].rsplit("/", 1)[-1]] if "$ref" in written else written
                    yield f"{method.upper()} {path} {parameter['name']}", parameter

    def test_every_parameter_says_what_it_is(self):
        # Itself, or through the described component its schema refers to.
        schemas = self.document["components"]["schemas"]
        for where, parameter in self._parameters():
            referred = parameter["schema"].get("$ref", "").removeprefix(openapi.SCHEMAS)
            said = parameter.get("description") or schemas.get(referred, {}).get("description", "")
            with self.subTest(where):
                self.assertTrue(said.strip())

    def test_every_tag_says_what_it_holds(self):
        for tag in self.document["tags"]:
            with self.subTest(tag["name"]):
                self.assertTrue(tag.get("description", "").strip())

    def test_a_group_says_what_it_is_for_rather_than_listing_its_domains(self):
        tags = self.document["tags"]
        for group in tags:
            held = [tag["summary"] for tag in tags if tag.get("parent") == group["name"]]
            if held:
                with self.subTest(group["name"]):
                    self.assertNotEqual(set(held) - set(re.split(r", | and |\.", group["description"])), set())

    def test_the_document_says_what_each_effect_means(self):
        said = self.document["info"]["description"]
        for effect in EFFECTS:
            with self.subTest(effect):
                self.assertIn(f"`{effect}`", said)

    def test_a_parameter_of_a_fixed_set_lists_it(self):
        # The generic routes name what they serve, so a client picks a name rather than guessing one.
        for where, parameter in self._parameters():
            if parameter["name"] == "name" and parameter["in"] == "path":
                with self.subTest(where):
                    self.assertTrue(parameter["schema"].get("enum"))


class AnExtensionSaysWhatItHoldsTests(SimpleTestCase):
    def test_its_domain_and_the_group_it_adds_say_what_its_manifest_declares(self):
        group = NavigationGroup("Outdoors", "Time spent outside, and what it took.")
        extension = sibling(
            description="Walks and climbs.",
            navigation=(NavigationItem("Alpha", "alpha:home", "alpha", 40, group.name),),
            navigation_groups=(group,),
        )
        with installed(extension, alone=True):
            tags = openapi._Tags({}, {})
            tags.use("example.alpha")
            rendered = {tag["name"]: tag for tag in tags.render()}
        domain = rendered["example.alpha"]
        self.assertEqual(domain["description"], "Walks and climbs.")
        self.assertEqual(rendered[domain["parent"]]["description"], group.description)
