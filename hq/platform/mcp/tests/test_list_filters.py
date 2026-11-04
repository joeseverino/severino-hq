"""A model learns every list filter from the schema: each enumerated filter
lists the values it takes, each limit its most, and a value outside them is
refused naming the field, through the MCP server (the API is
hq.platform.api.tests.test_resource_filters).
describe_resources lists every kind by its filters and gives one kind's whole
schema when asked by name."""

from typing import get_args

from asgiref.sync import async_to_sync
from django.test import TestCase
from mcp import Client

from hq.domains.analytics.models import RumDaily
from hq.domains.assets.models import Asset
from hq.domains.content.models import ContentItem
from hq.domains.control_plane.observations import OBSERVATIONS
from hq.domains.control_plane.provider_adapters.cloudflare import ContactStatus
from hq.domains.control_plane.providers import PROVIDERS
from hq.domains.docs_index.models import DocStatus
from hq.domains.expenses.models import EXPENSE_CATEGORY_CHOICES
from hq.domains.projects.models import Project
from hq.platform.application.domains import all_domains
from hq.platform.application.json_schemas import fields
from hq.platform.application.plugins import ATTENTION_ORDER
from hq.platform.application.projection import MAX_PAGE_SIZE
from hq.platform.application.security import Capability, Principal
from hq.platform.application.tests.host_only import host_only

from ..contract import served_resources
from ..identity import set_principal
from ..server import mcp
from .registry import KIND, example_kind, registry_patched, with_kind

READER = Principal("example-reader", "mcp", frozenset(Capability))
OUTSIDE = "example-outside"


def declared_choices():
    """Each enumerated filter and the values where they are declared."""

    return (
        ("projects", "status", Project.Status.values),
        ("content", "status", ContentItem.Status.values),
        ("assets", "status", Asset.Status.values),
        ("documentation", "status", DocStatus.values),
        ("contact.submissions", "status", get_args(ContactStatus)),
        ("expenses", "category", [value for value, _ in EXPENSE_CATEGORY_CHOICES]),
        ("analytics", "dimension", RumDaily.Dimension.values),
        ("action_items", "status", ATTENTION_ORDER),
        (
            "action_items",
            "source",
            [domain.id for domain in all_domains() if domain.integration.attention is not None],
        ),
        ("infrastructure", "kind", list(PROVIDERS)),
        ("readings", "provider", {spec.provider for spec in OBSERVATIONS.values()}),
    )


def calls(*asked):
    """Each call's result through the server, as a client receives it."""

    async def run():
        async with Client(mcp.server) as client:
            return [await client.call_tool(name, arguments) for name, arguments in asked]

    with set_principal(READER):
        return async_to_sync(run)()


def enum_of(schema):
    """The values a property's schema admits, whether or not it is nullable."""

    for option in (schema, *schema.get("anyOf", ())):
        if "enum" in option:
            return option["enum"]
    return None


def query_schema(kind):
    (answer,) = calls(("describe_resources", {"name": kind}))
    assert not answer.is_error, f"describe_resources refused {kind!r}: {answer.structured_content}"
    (entry,) = answer.structured_content["resources"]
    return entry["operations"]["list"]["query_schema"]


class FiltersInTheSchemaTests(TestCase):
    def test_each_enumerated_filter_lists_the_values_where_they_are_declared(self):
        for kind, field, values in declared_choices():
            with self.subTest(kind=kind, field=field):
                listed = enum_of(query_schema(kind)["properties"][field])
                self.assertEqual(set(listed or ()), set(values))

    def test_every_listing_with_a_limit_says_the_most_it_may_ask_for(self):
        for resource in served_resources().listable:
            properties = fields(query_schema(resource.name))
            if "limit" in properties:
                with self.subTest(kind=resource.name):
                    self.assertIn("maximum", properties["limit"])
        self.assertEqual(fields(query_schema("projects"))["limit"]["maximum"], MAX_PAGE_SIZE)

    def test_a_value_outside_a_filters_choices_is_refused_naming_the_field(self):
        asked = [
            *((kind, {field: OUTSIDE}, field) for kind, field, _ in declared_choices()),
        ]
        answers = calls(
            *(("list_resource", {"name": kind, "filters": filters}) for kind, filters, _ in asked),
            ("list_resource", {"name": "projects", "limit": MAX_PAGE_SIZE + 1}),
        )
        asked.append(("projects", {}, "limit"))
        for (kind, _, field), answer in zip(asked, answers, strict=True):
            with self.subTest(kind=kind, field=field):
                self.assertTrue(answer.is_error)
                error = answer.structured_content["error"]
                self.assertEqual(error["code"], "invalid_input")
                self.assertIn(field, [detail["loc"][0] for detail in error["details"]])

    def test_a_value_among_the_choices_lists(self):
        Project.objects.create(name="Chosen", slug="chosen", status=Project.Status.PAUSED)
        Project.objects.create(name="Other", slug="other", status=Project.Status.ACTIVE)

        (answer,) = calls(("list_resource", {"name": "projects", "filters": {"status": "paused"}}))

        self.assertFalse(answer.is_error)
        self.assertEqual([item["slug"] for item in answer.structured_content["items"]], ["chosen"])


class DescribeResourcesNarrowsTests(TestCase):
    def test_every_kind_is_listed_by_its_filters_without_a_schema(self):
        (answer,) = calls(("describe_resources", {}))
        catalog = answer.structured_content

        self.assertEqual(
            {entry["name"] for entry in catalog["resources"]}, {r.name for r in served_resources().described}
        )
        for entry in catalog["resources"]:
            listing = entry["operations"]["list"]
            if listing is not None:
                with self.subTest(kind=entry["name"]):
                    self.assertNotIn("query_schema", listing)
                    self.assertIsInstance(listing["filters"], list)
        # The catalog names the call that answers one kind's whole schema.
        self.assertIn("describe_resources", catalog["schemas"])
        self.assertIn("name", catalog["schemas"])

    def test_a_name_answers_that_kind_with_its_whole_schema(self):
        (answer,) = calls(("describe_resources", {"name": "projects"}))
        (entry,) = answer.structured_content["resources"]

        self.assertEqual(entry["name"], "projects")
        # Its filters are what narrows it; limit and cursor page it, beside them.
        listing = entry["operations"]["list"]
        self.assertEqual(set(listing["query_schema"]["properties"]), {*listing["filters"], "limit", "cursor"})

    def test_a_name_is_one_of_the_kinds_composed_at_the_call(self):
        # The server is reached first with no extension installed, as an
        # earlier test in the same process may have reached it.
        with host_only():
            calls(("describe_resources", {"name": "projects"}))

        with registry_patched(with_kind(example_kind())):
            (answer,) = calls(("describe_resources", {"name": KIND}))

        self.assertFalse(answer.is_error, answer.structured_content)
        self.assertEqual([entry["name"] for entry in answer.structured_content["resources"]], [KIND])

    def test_a_name_the_registry_lacks_is_refused(self):
        (answer,) = calls(("describe_resources", {"name": OUTSIDE}))

        self.assertTrue(answer.is_error)
        self.assertEqual(answer.structured_content["error"]["code"], "invalid_input")
