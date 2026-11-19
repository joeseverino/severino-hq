"""Every record page draws the record its kind's API and MCP read answer
(``RecordPageMixin``), walked over the showcase estate, so a page that moves
onto it is held here without a test of its own."""

from django.test import TestCase
from django.urls import reverse

from hq.platform.core import seeds
from hq.platform.core.example_estate.testing import throwaway_media

from ..pages import RecordPageMixin
from ..resources import get_resource, list_resource, resource_registry
from ..routes import routed
from ..security import web_principal


class RecordPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        with throwaway_media(), seeds.built("showcase") as owner:
            cls.owner = owner

    def test_each_record_page_draws_the_record_its_api_read_answers(self):
        pages = [
            route
            for route in routed()
            if isinstance(route.view, type) and issubclass(route.view, RecordPageMixin) and route.name
        ]
        self.assertTrue(pages)
        principal = web_principal(self.owner)
        self.client.force_login(self.owner)
        for route in pages:
            view = route.view
            identifier = str(resource_registry()[view.resource].identifier)
            with self.subTest(view.__name__):
                listed = list_resource(view.resource, principal=principal, fields=[identifier])["items"]
                drawn = 0
                for item in listed:
                    response = self.client.get(reverse(route.name, kwargs={identifier: item[identifier]}))
                    # A record whose page lives elsewhere, such as a machine's declaration, is sent there.
                    if response.status_code != 200:
                        self.assertEqual(response.status_code, 302)
                        continue
                    read = get_resource(view.resource, item[identifier], principal=principal)
                    self.assertEqual(response.context[view.context_object_name], read)
                    drawn += 1
                self.assertTrue(drawn, f"The showcase holds no {view.resource} its page draws.")
