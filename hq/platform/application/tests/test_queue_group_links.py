"""A queue group's heading links where its domain's items are decided together,
for a domain that declares the page (``PluginIntegration.attention_url``)."""

from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from ..action_items import by_source
from .markup import elements, links

HELD = {
    "key": "hq.approvals:approval:example",
    "revision": "r1",
    "source_id": "hq.approvals",
    "source": "Waiting for approval",
    "label": "An agent asked to change example.test",
    "detail": "",
    "count": 1,
    "status": "serious",
    "url": "",
    "action": "",
    "workflow": None,
}
OTHER = {**HELD, "key": "example:widgets", "source_id": "example", "source": "Example", "label": "Two widgets"}


def approval_queue() -> str:
    return f"{reverse('core:audit_list')}?awaiting=1"


class GroupTests(TestCase):
    def test_held_changes_are_grouped_under_the_approval_queue_and_other_sources_under_no_page(self):
        groups = {group["id"]: group["url"] for group in by_source([HELD, OTHER])}

        self.assertEqual(groups, {"hq.approvals": approval_queue(), "example": ""})


class PageTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_superuser("op", password="x" * 20)
        self.client.force_login(user)

    def page(self, *items):
        with (
            mock.patch("hq.platform.core.action_item_views.work_queue", return_value=list(items)),
            mock.patch("hq.platform.application.dashboard.work_queue", return_value=list(items)),
        ):
            return self.client.get(reverse("action_items"))

    def test_the_held_changes_heading_opens_the_approval_queue(self):
        shown = links(self.page(HELD, OTHER).content.decode())

        self.assertIn((HELD["source"], approval_queue()), shown)
        self.assertNotIn(OTHER["source"], [words for words, _href in shown])

    def test_held_changes_alone_link_the_approval_queue_beside_their_heading(self):
        beside = [
            link.attrs.get("href")
            for link in elements(self.page(HELD).content.decode(), "a")
            if any("section-aside" in ancestor.classes for ancestor in link.ancestors)
        ]

        self.assertIn(approval_queue(), beside)

    def test_the_approval_queue_is_the_log_narrowed_to_what_waits(self):
        page = self.client.get(approval_queue())

        self.assertEqual(page.status_code, 200)
        self.assertTrue(page.context["awaiting"])
