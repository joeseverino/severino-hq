"""The showcase and standard sets show a set-up HQ acting through its
connections: every action their records offer is drawn enabled, and nothing
asks for setup a set-up HQ has done. Only a fact of a record's own kind holds
one back: removing a kind HQ cannot remove at its provider."""

from django.test import TestCase, override_settings

from hq.domains.contacts import inbox
from hq.domains.control_plane.models import ManagedResource
from hq.domains.control_plane.providers import PROVIDERS
from hq.platform.application import adoption
from hq.platform.application.connections import connection_rows
from hq.platform.application.page_reads import pages_reading, readers
from hq.platform.application.resource_capabilities import resource_capabilities
from hq.platform.core import seeds
from hq.platform.core.example_estate.testing import throwaway_media


class _Acts:
    seed = ""

    @classmethod
    def setUpTestData(cls):
        with throwaway_media(), seeds.built(cls.seed):
            pass

    def test_every_action_on_every_declaration_is_offered(self):
        resources = list(ManagedResource.objects.order_by("key"))
        self.assertTrue(resources)
        manages = adoption.manages_through()
        for resource in resources:
            found = resource_capabilities(resource, manages=manages)
            for verb, allowed in found.actions.items():
                with self.subTest(resource=resource.key, verb=verb):
                    self.assertTrue(allowed.enabled, allowed.reason)
            if not PROVIDERS[resource.kind].removal_gap:
                with self.subTest(resource=resource.key, removal=found.removal):
                    self.assertNotEqual(found.removal, "unavailable", found.removal_reason)

    def test_every_record_hq_found_may_be_adopted(self):
        found = adoption.unmanaged()
        self.assertTrue(found)
        for item in found:
            with self.subTest(kind=item.kind, record=item.identity):
                self.assertFalse(item.observed_only)

    def test_the_contact_inbox_may_be_changed(self):
        database = inbox.current().database
        self.assertIsNotNone(database)

        self.assertFalse(adoption.observes_only(inbox.DECLARATION, {"connection_ref": database.connection_ref}))

    def test_every_connection_a_page_reads_through_is_connected(self):
        connected = {row.provider for row in connection_rows()}
        for view in pages_reading():
            with self.subTest(page=view.__name__):
                self.assertLessEqual(set(readers(view.page_reads)), connected)


@override_settings(**seeds.get("showcase").settings)
class ShowcaseActsTests(_Acts, TestCase):
    seed = "showcase"


# The same homelab, so it acts the same.
@override_settings(**seeds.get("standard").settings)
class StandardActsTests(_Acts, TestCase):
    seed = "standard"
