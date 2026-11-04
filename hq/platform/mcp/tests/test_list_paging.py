"""list_resource pages by arguments of its own, beside its filters, as every tool
that pages does, and its description names each listable kind's filters, read
from the kind's own query model."""

from asgiref.sync import async_to_sync
from django.test import TestCase
from mcp import Client
from pydantic import Field, create_model

from hq.domains.projects.models import Project
from hq.platform.application.integration_specs import ResourceSpec
from hq.platform.application.resources import BoundedQuery
from hq.platform.application.security import Capability, Principal

from ..identity import set_principal
from .registry import KIND, served, with_kind

READER = Principal("example-reader", "mcp", frozenset(Capability))


def _calls(server, *asked):
    async def run():
        async with Client(server) as client:
            return [(await client.call_tool("list_resource", arguments)) for arguments in asked]

    with set_principal(READER):
        return async_to_sync(run)()


def _description(server):
    async def run():
        async with Client(server) as client:
            return {tool.name: tool for tool in (await client.list_tools()).tools}["list_resource"]

    with set_principal(READER):
        return async_to_sync(run)()


def _example_kind():
    """A listable kind the host does not name, narrowed by a filter of its own."""

    query = create_model("ExampleQuery", __base__=BoundedQuery, shade=(str | None, Field(default=None)))
    return ResourceSpec(KIND, "Example records", "Records an extension declares.", "read", lambda **_: {}, query)


class PagingArgumentTests(TestCase):
    def setUp(self):
        for index in range(3):
            Project.objects.create(name=f"Paged {index}", slug=f"paged-{index}")

    def test_limit_and_cursor_beside_the_filters_read_the_list_page_by_page(self):
        with served(with_kind(_example_kind())) as server:
            (first,) = _calls(server.server, {"name": "projects", "limit": 2})
            page = first.structured_content
            (second,) = _calls(server.server, {"name": "projects", "limit": 2, "cursor": page["next"]["cursor"]})

        self.assertFalse(first.is_error, page)
        self.assertEqual(page["count"], 2)
        rest = second.structured_content
        self.assertFalse(second.is_error, rest)
        slugs = [item["slug"] for item in (*page["items"], *rest["items"])]
        self.assertEqual(sorted(slugs), ["paged-0", "paged-1", "paged-2"])

    def test_paging_sent_among_the_filters_is_refused_naming_it(self):
        with served(with_kind(_example_kind())) as server:
            answers = _calls(server.server, {"name": "projects", "filters": {"limit": 2}})

        (answer,) = answers
        self.assertTrue(answer.is_error)
        error = answer.structured_content["error"]
        self.assertEqual(error["code"], "invalid_input")
        self.assertIn("limit", error["message"])


class FiltersInTheDescriptionTests(TestCase):
    def test_each_listable_kind_is_described_by_the_filters_its_query_declares(self):
        registry = with_kind(_example_kind())
        with served(registry) as server:
            description = _description(server.server).description

        named = dict(part.split(" by ", 1) for part in description.split("Filters: ", 1)[1].split("; "))
        for spec in registry.values():
            if spec.list_query_type is None:
                continue
            with self.subTest(kind=spec.name):
                said = named[f"`{spec.name}`"]
                for field in spec.list_query_type.model_fields:
                    # Paging is the tool's own arguments, never a filter a kind is named by.
                    (self.assertNotIn if field in {"limit", "cursor"} else self.assertIn)(f"`{field}`", said)
        self.assertIn("`shade`", named[f"`{KIND}`"])
