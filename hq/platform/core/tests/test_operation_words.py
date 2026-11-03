"""A request reads by what it changed, never by the route it resolved to."""

from uuid import uuid7

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from hq.platform.application.read_models import recent_activity
from hq.platform.application.security import cli_principal
from hq.platform.application.tests.markup import within
from hq.platform.core import append_only
from hq.platform.core.audit import record_event
from hq.platform.core.middleware import request_operation
from hq.platform.core.models import AuditLog

# A route whose words would show: its namespace and its name.
ROUTE = "core:audit_list"
ROUTE_WORDS = ("core", "audit_list", "audit list", ":")


class RequestWordsTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_superuser("owner", "owner@example.test", "x" * 14)
        self.client.force_login(self.owner)
        self.records = [get_user_model().objects.create_user(name) for name in ("example-a", "example-b")]
        with append_only.lifted():
            AuditLog.objects.all().delete()
        self.request_id = uuid7().hex
        with request_operation(reverse(ROUTE), operation_id=self.request_id):
            self.rows = [
                record_event(action=AuditLog.Action.UPDATED, obj=record, required=True) for record in self.records
            ]

    def assertNoRouteWords(self, words: str):
        for route_word in ROUTE_WORDS:
            self.assertNotIn(route_word, words.lower())

    def test_a_request_in_the_history_is_named_by_what_it_changed(self):
        (line,) = [line for line in self.client.get(reverse("core:audit_list")).context["events"] if not line.under]

        # The newest thing it changed names it.
        self.assertIn("example-b", line.title)
        self.assertNoRouteWords(line.title)

    def test_a_model_reads_a_request_by_what_it_changed(self):
        (item,) = recent_activity(principal=cli_principal())["items"]

        self.assertIn("example-b", item["title"])
        self.assertNoRouteWords(item["title"])

    def test_an_event_page_says_a_request_with_its_id_and_no_route(self):
        html = self.client.get(reverse("core:audit_detail", args=[self.rows[0].pk])).content.decode()
        elements = [element for element in within(html, "aria-label", "Event") if element.tag in {"dt", "dd"}]
        facts = {dt.words: dd.words for dt, dd in zip(elements[::2], elements[1::2], strict=True)}

        self.assertIn("request", facts["Operation"].lower())
        self.assertIn(self.request_id, facts["Operation"].replace(" ", ""))
        self.assertNoRouteWords(facts["Operation"])
