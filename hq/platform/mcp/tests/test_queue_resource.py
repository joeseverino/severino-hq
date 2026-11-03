"""The attachable action queue reads the queue's own list declaration, so it
sends what the queue's list read sends."""

from django.test import TestCase

from hq.platform.application.resources import list_resource, resource_registry
from hq.platform.application.security import Capability, Principal
from hq.platform.core import seeds
from hq.platform.core.example_estate.testing import throwaway_media

from .. import resources

OPERATOR = Principal("example-operator", "mcp", frozenset(Capability))


class QueueResourceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        with throwaway_media(), seeds.built("standard"):
            pass

    def test_the_queue_resource_reads_the_kinds_declaration(self):
        attached = resources.read("hq://action_items", OPERATOR)

        self.assertEqual(attached, list_resource("action_items", None, principal=OPERATOR))
        self.assertTrue(attached["items"])
        self.assertEqual(
            {key for item in attached["items"] for key in item}, set(resource_registry()["action_items"].list_fields)
        )
