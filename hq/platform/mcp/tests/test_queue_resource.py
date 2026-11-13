"""The attachable action queue reads the queue's own list declaration, so it
sends what the queue's list read sends, every page of it; the machine API's
page of the queue is its own, larger than the one the MCP tools answer."""

from django.test import TestCase

from hq.platform.application.card_items import Insight
from hq.platform.application.plugin_testing import installed, sibling
from hq.platform.application.resources import BoundedQuery, list_resource, resource_registry
from hq.platform.application.security import Capability, Principal
from hq.platform.core import seeds
from hq.platform.core.example_estate.testing import throwaway_media

from .. import resources
from ..dashboard_answer import ACTION_ITEMS_PAGE

OPERATOR = Principal("example-operator", "mcp", frozenset(Capability))
# The page a list read answers when no limit is sent, as the machine API reads it.
LIST_PAGE = BoundedQuery.model_fields["limit"].default
# More than a page of the queue, as either door reads it, raised by an extension.
RAISED = tuple(
    Insight("serious", "Alpha", f"Example {n} needs you", "1", "Body.")
    for n in range(max(LIST_PAGE, ACTION_ITEMS_PAGE) + 2)
)


class QueueResourceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        with throwaway_media(), seeds.built("standard"):
            pass

    def test_the_queue_resource_reads_the_kinds_declaration(self):
        attached = resources.read("hq://action_items", OPERATOR)

        first = list_resource("action_items", None, principal=OPERATOR)
        self.assertEqual(attached["items"][: first["count"]], first["items"])
        self.assertTrue(attached["items"])
        self.assertEqual(
            {key for item in attached["items"] for key in item}, set(resource_registry()["action_items"].list_fields)
        )

    def test_a_queue_longer_than_a_page_is_attached_whole(self):
        with installed(sibling(attention=RAISED)):
            attached = resources.read("hq://action_items", OPERATOR)
            first = list_resource("action_items", None, principal=OPERATOR)

        self.assertTrue(first["truncated"])
        self.assertEqual((attached["count"], attached["truncated"]), (first["total"], False))

    def test_the_machine_apis_page_of_the_queue_is_more_than_the_mcp_tools(self):
        with installed(sibling(attention=RAISED)):
            first = list_resource("action_items", None, principal=OPERATOR)

        self.assertEqual(first["count"], LIST_PAGE)
        self.assertGreater(first["count"], ACTION_ITEMS_PAGE)
