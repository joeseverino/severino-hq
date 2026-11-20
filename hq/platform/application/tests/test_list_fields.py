"""A list read sends each item as its kind lists it: the fields declared once
on its ResourceSpec, the whole record a detail read or ``fields`` away."""

from typing import TypedDict

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, TestCase

from hq.platform.core import seeds
from hq.platform.core.example_estate.testing import throwaway_media

from ..integration_specs import ListRead
from ..integrations import integration_graph
from ..plugin_testing import offering
from ..resources import (
    EmptyQuery,
    ResourceSpec,
    describe_resources,
    get_resource,
    list_resource,
    resource_registry,
)
from ..security import Principal, operator_capabilities

OPERATOR = Principal("example-operator", "test", operator_capabilities())


def _declaring():
    return {name: spec for name, spec in resource_registry().items() if spec.listing}


class ListFieldTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        # The example estate, so each kind's list has items whose fields are read.
        with throwaway_media(), seeds.built("standard"):
            pass

    def test_each_item_keeps_only_its_kinds_list_fields_and_holds_them(self):
        declaring = _declaring()
        for name, spec in declaring.items():
            with self.subTest(name=name):
                page = list_resource(name, {}, principal=OPERATOR)
                held = {key for item in page["items"] for key in item}

                self.assertLessEqual(held, set(spec.listing.fields))
                # An item's NotRequired field may be absent from every item.
                required = spec.listing.item.__required_keys__ & set(spec.listing.fields)
                if page["items"]:
                    self.assertLessEqual(required, held, "a required field no item holds")

    def test_a_star_keeps_every_field(self):
        for name, spec in _declaring().items():
            page = list_resource(name, {}, principal=OPERATOR)
            whole = list_resource(name, {}, principal=OPERATOR, fields=["*"])
            with self.subTest(name=name):
                self.assertEqual(
                    [{key: item[key] for key in item if key in spec.listing.fields} for item in whole["items"]],
                    page["items"],
                )
                # A kind whose rows are not its records may list none by its identifier.
                if spec.detail_handler and whole["items"] and spec.identifier in whole["items"][0]:
                    identifier = whole["items"][0][spec.identifier]
                    self.assertEqual(
                        get_resource(name, identifier, principal=OPERATOR, fields=["*"]),
                        get_resource(name, identifier, principal=OPERATOR),
                    )

    def test_named_fields_reach_past_the_kinds_list_fields(self):
        listed = list_resource("machines", {}, principal=OPERATOR)
        page = list_resource("machines", {}, principal=OPERATOR, fields=["name", "containers"])

        self.assertTrue(page["items"])
        self.assertFalse(any("containers" in item for item in listed["items"]))
        self.assertEqual({key for item in page["items"] for key in item}, {"name", "containers"})

    def test_the_catalog_names_each_kinds_list_fields(self):
        for name, spec in _declaring().items():
            with self.subTest(name=name):
                (described,) = describe_resources(name=name)["resources"]
                self.assertEqual(described["operations"]["list"]["fields"], list(spec.listing.fields))


class _ExampleRecord(TypedDict):
    name: str


def _rows(**_):
    return {"items": [], "count": 0}


class ListFieldRegistrationTests(SimpleTestCase):
    def test_fields_repeated_or_dotted_are_refused(self):
        for fields in (("name", "name"), ("parts.name",), ("",)):
            listing = ListRead(_rows, EmptyQuery, _ExampleRecord, fields=fields)
            invalid = ResourceSpec("example.records", "Records", "Example records.", "example.read", listing)
            with (
                self.subTest(fields=fields),
                offering(resources=(invalid,)),
                self.assertRaisesRegex(ImproperlyConfigured, "fields"),
            ):
                integration_graph()
