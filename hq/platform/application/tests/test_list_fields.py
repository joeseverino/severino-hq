"""A list read sends each item as its kind lists it: the fields declared once
on its ResourceSpec, the whole record a detail read or ``fields`` away."""

from unittest import mock

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, TestCase

from hq.platform.core import seeds
from hq.platform.core.example_estate.testing import throwaway_media

from ..integrations import integration_graph
from ..plugins import plugin_resource_specs
from ..resources import EmptyQuery, ResourceSpec, describe_resources, get_resource, list_resource, resource_registry
from ..security import Capability, Principal

OPERATOR = Principal("example-operator", "test", frozenset(Capability))


def _declaring():
    return {name: spec for name, spec in resource_registry().items() if spec.list_fields}


class ListFieldTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        # The example estate, so each kind's list has items whose fields are read.
        with throwaway_media(), seeds.built("standard"):
            pass

    def test_each_item_keeps_only_its_kinds_list_fields_and_holds_them(self):
        declaring = _declaring()
        self.assertTrue(declaring, "a kind declares list_fields")
        for name, spec in declaring.items():
            with self.subTest(name=name):
                page = list_resource(name, {}, principal=OPERATOR)
                held = {key for item in page["items"] for key in item}

                self.assertLessEqual(held, set(spec.list_fields))
                # A row's NotRequired field may be absent from every item.
                required = spec.row_type.__required_keys__ if spec.row_type else set(spec.list_fields)
                if page["items"]:
                    self.assertLessEqual(required, held, "a required field no item holds")

    def test_a_star_keeps_every_field(self):
        for name, spec in _declaring().items():
            page = list_resource(name, {}, principal=OPERATOR)
            whole = list_resource(name, {}, principal=OPERATOR, fields=["*"])
            with self.subTest(name=name):
                self.assertEqual(
                    [{key: item[key] for key in item if key in spec.list_fields} for item in whole["items"]],
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
        described = {entry["name"]: entry for entry in describe_resources()["resources"]}

        for name, spec in _declaring().items():
            with self.subTest(name=name):
                self.assertEqual(described[name]["operations"]["list"]["fields"], list(spec.list_fields))

    def test_the_catalog_names_no_fields_for_a_kind_that_declares_none(self):
        whole = ResourceSpec("example.records", "Records", "Example records.", "example.read", _rows, EmptyQuery)
        installed = plugin_resource_specs()
        with mock.patch("hq.platform.application.plugins.plugin_resource_specs", return_value=(*installed, whole)):
            described = {entry["name"]: entry for entry in describe_resources()["resources"]}

        self.assertNotIn("fields", described["example.records"]["operations"]["list"])


def _rows(**_):
    return {"items": [], "count": 0}


class ListFieldRegistrationTests(SimpleTestCase):
    def test_list_fields_without_a_list_or_repeated_or_dotted_are_refused(self):
        for list_fields in (("name", "name"), ("parts.name",), ("",)):
            invalid = ResourceSpec(
                "example.records",
                "Records",
                "Example records.",
                "example.read",
                _rows,
                EmptyQuery,
                list_fields=list_fields,
            )
            with (
                self.subTest(list_fields=list_fields),
                mock.patch("hq.platform.application.plugins.plugin_resource_specs", return_value=(invalid,)),
                self.assertRaisesRegex(ImproperlyConfigured, "list_fields"),
            ):
                integration_graph()
        unlisted = ResourceSpec(
            "example.records",
            "Records",
            "Example records.",
            "example.read",
            detail_handler=lambda identifier: {},
            identifier="slug",
            list_fields=("slug",),
        )
        with (
            mock.patch("hq.platform.application.plugins.plugin_resource_specs", return_value=(unlisted,)),
            self.assertRaisesRegex(ImproperlyConfigured, "list_fields"),
        ):
            integration_graph()
