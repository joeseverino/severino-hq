"""A client completes a record template's identifier from what the caller may read of its kind, a bounded page of it."""

from asgiref.sync import async_to_sync
from django.test import TestCase
from mcp import Client, types

from hq.platform.application.integration_specs import ResourceSpec
from hq.platform.application.resources import EmptyQuery
from hq.platform.application.security import Capability, Principal

from ..identity import set_principal
from .registry import KIND, served, with_kind

HOLDER = Principal("example-holder", "mcp", frozenset({Capability.READ, "example_read"}))
READER = Principal("example-reader", "mcp", frozenset({Capability.READ}))
# More records than one completion offers.
NAMES = [f"example-{number:03}" for number in range(150)]


def _listing():
    return {"items": [{"name": name, "state": "ok"} for name in NAMES], "count": len(NAMES)}


def _kind():
    return ResourceSpec(
        KIND,
        "Example records",
        "Records an extension declares.",
        "example_read",
        _listing,
        EmptyQuery,
        lambda name: {"name": name},
        "name",
    )


def _completed(principal, typed):
    async def run(server):
        async with Client(server.server) as client:
            templates = (await client.list_resource_templates()).resource_templates
            ref = types.ResourceTemplateReference(uri=f"hq://{KIND}/{{name}}")
            completion = (await client.complete(ref, {"name": "name", "value": typed})).completion
            return templates, completion

    with set_principal(principal), served(with_kind(_kind())) as server:
        return async_to_sync(run)(server)


class CompletionTests(TestCase):
    def test_an_identifier_completes_from_the_kinds_listing_by_what_was_typed(self):
        _, completion = _completed(HOLDER, "example-01")

        self.assertEqual(completion.values, [name for name in NAMES if name.startswith("example-01")])

    def test_a_completion_is_bounded_and_says_more_match(self):
        _, completion = _completed(HOLDER, "")

        self.assertLessEqual(len(completion.values), 100)
        self.assertLessEqual(set(completion.values), set(NAMES))
        self.assertTrue(completion.has_more)

    def test_a_reader_without_the_kinds_read_gate_is_offered_nothing_of_it(self):
        _, completion = _completed(READER, "")

        self.assertEqual(completion.values, [])

    def test_each_template_says_what_it_reads(self):
        templates, _ = _completed(HOLDER, "")

        for template in templates:
            with self.subTest(template=template.name):
                self.assertTrue(template.description)
