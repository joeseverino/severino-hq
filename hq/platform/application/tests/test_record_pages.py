"""Every record page draws the record its kind's API and MCP read answer
(``RecordPageMixin``), and every record list page the items its list read
answers (``RecordListMixin``), walked over the showcase estate, so a page that
moves onto either is held here without a test of its own."""

from unittest import mock

from django.test import TestCase
from django.urls import reverse
from django.views.generic.base import TemplateResponseMixin

from hq.platform.core import seeds
from hq.platform.core.example_estate.testing import throwaway_media

from ..pages import RecordListMixin, RecordPageMixin
from ..resources import every_item, get_resource, list_resource, resource_registry
from ..routes import routed
from ..security import web_principal


class RecordPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        with throwaway_media(), seeds.built("showcase") as owner:
            cls.owner = owner

    def routes(self, mixin):
        found = [
            route for route in routed() if isinstance(route.view, type) and issubclass(route.view, mixin) and route.name
        ]
        self.assertTrue(found)
        return found

    def test_each_record_list_page_draws_the_items_its_list_read_answers(self):
        principal = web_principal(self.owner)
        self.client.force_login(self.owner)
        for route in self.routes(RecordListMixin):
            view = route.view
            with self.subTest(view.__name__):
                response = self.client.get(reverse(route.name))
                listed = list_resource(view.resource, principal=principal, fields=list(view.fields) or None)
                self.assertTrue(listed["items"])
                # A row may carry what only the page draws (its actions), beside every field the read answers.
                drawn = response.context[view.context_object_name]
                kept = [{key: row[key] for key in item} for row, item in zip(drawn, listed["items"], strict=True)]
                self.assertEqual(kept, listed["items"])

    def test_a_whole_list_is_read_page_after_page(self):
        principal = web_principal(self.owner)
        whole = list_resource("machines", principal=principal)["items"]
        self.assertGreater(len(whole), 1)

        with mock.patch("hq.platform.application.resources.MAX_PAGE_SIZE", 1):
            self.assertEqual(every_item("machines", principal=principal), whole)

    def test_each_record_page_draws_the_record_its_api_read_answers(self):
        # A route that only sends a record to its page elsewhere draws nothing.
        pages = [route for route in self.routes(RecordPageMixin) if issubclass(route.view, TemplateResponseMixin)]
        principal = web_principal(self.owner)
        self.client.force_login(self.owner)
        for route in pages:
            view = route.view
            spec = resource_registry()[view.resource]
            identifier = str(spec.identifier)
            with self.subTest(view.__name__):
                if spec.listing:
                    listed = list_resource(view.resource, principal=principal, fields=[identifier])["items"]
                else:
                    # A run lists among the work, by the id a read of one takes.
                    work = list_resource("work", principal=principal, fields=["id", "source"])["items"]
                    listed = [{identifier: item["id"]} for item in work if item["source"] == "run"]
                drawn = 0
                for item in listed:
                    asked = {view.url_kwarg or identifier: item[identifier]}
                    response = self.client.get(reverse(route.name, kwargs=asked))
                    # A record whose page lives elsewhere, such as a machine's declaration, is sent there.
                    if response.status_code != 200:
                        self.assertEqual(response.status_code, 302)
                        continue
                    read = get_resource(view.resource, item[identifier], principal=principal)
                    self.assertEqual(response.context[view.context_object_name], read)
                    drawn += 1
                self.assertTrue(drawn, f"The showcase holds no {view.resource} its page draws.")
