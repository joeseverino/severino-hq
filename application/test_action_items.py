"""Marking action items read."""

from __future__ import annotations

from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.models import ActionItemRead

from .action_items import fingerprint, mark, unread_count, with_read_state

ITEM = {
    "source_id": "example",
    "source": "Example",
    "label": "Two widgets need a look",
    "detail": "",
    "count": 2,
    "status": "attention",
    "url": "",
    "action": "",
    "workflow": None,
}
OTHER = {**ITEM, "label": "A gadget is stale", "count": 1}


class ReadStateTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("op", password="x" * 20)
        self.someone = get_user_model().objects.create_user("other", password="x" * 20)

    def test_read_items_leave_the_count_for_that_person_only(self):
        mark(self.user, [fingerprint(ITEM)], read=True, current=[ITEM, OTHER])

        self.assertEqual(unread_count([ITEM, OTHER], self.user), 1)
        self.assertEqual(unread_count([ITEM, OTHER], self.someone), 3)

    def test_an_item_that_changes_is_unread_again(self):
        mark(self.user, [fingerprint(ITEM)], read=True, current=[ITEM])

        grown = {**ITEM, "count": 3}

        self.assertFalse(with_read_state([grown], self.user)[0]["read"])

    def test_only_current_items_can_be_marked_and_gone_ones_are_forgotten(self):
        mark(self.user, [fingerprint(ITEM), "0" * 64], read=True, current=[ITEM])
        mark(self.user, [fingerprint(OTHER)], read=True, current=[OTHER])

        self.assertEqual(
            list(ActionItemRead.objects.values_list("key", flat=True)), [fingerprint(OTHER)]
        )

    def test_unread_undoes_read(self):
        mark(self.user, [fingerprint(ITEM)], read=True, current=[ITEM])
        mark(self.user, [fingerprint(ITEM)], read=False, current=[ITEM])

        self.assertEqual(unread_count([ITEM], self.user), 2)


class PageTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("op", password="x" * 20)
        self.client.force_login(self.user)
        patcher = mock.patch("core.views.work_queue", return_value=[ITEM, OTHER])
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_marking_read_moves_it_below_and_lowers_the_count(self):
        self.client.post(
            reverse("action_items_read"), {"key": fingerprint(ITEM), "read": "1"}
        )

        page = self.client.get(reverse("action_items"))

        self.assertEqual([item["label"] for item in page.context["action_items"]], [OTHER["label"]])
        self.assertEqual(page.context["profile_action_count"], 1)
        self.assertContains(page, "Read · 1")
        self.assertEqual(self.client.get(reverse("action_item_count")).json(), {"count": 1})

    def test_mark_all_read_empties_the_unread_list(self):
        self.client.post(
            reverse("action_items_read"),
            {"key": [fingerprint(ITEM), fingerprint(OTHER)], "read": "1"},
        )

        self.assertContains(self.client.get(reverse("action_items")), "Nothing unread.")

    def test_a_malformed_request_changes_nothing(self):
        response = self.client.post(reverse("action_items_read"), {"key": fingerprint(ITEM)})

        self.assertEqual(response.status_code, 400)
        self.assertFalse(ActionItemRead.objects.exists())


class ActivityActorTests(TestCase):
    def test_an_agent_row_names_the_agent_not_system(self):
        from core.models import AuditLog

        from .read_models import recent_activity

        AuditLog.objects.create(
            action=AuditLog.Action.CREATED, object_type="Approval", metadata={"actor": "example-agent"}
        )

        self.assertEqual(recent_activity(limit=1)["items"][0]["actor"], "example-agent")


class GlanceSettingsLinkTests(TestCase):
    def test_following_the_settings_link_lands_on_the_dashboard(self):
        self.client.force_login(get_user_model().objects.create_user("op", password="x" * 20))

        response = self.client.get(reverse("dashboard_glance_settings"))

        self.assertRedirects(response, reverse("dashboard"), fetch_redirect_response=False)


class ReadableValueTests(TestCase):
    def test_a_timestamp_reads_as_a_date_and_anything_else_is_left_alone(self):
        from core.templatetags.value_tags import readable

        self.assertEqual(readable("2026-09-20T22:08:26.284763+00:00"), "Sep 20, 2026, 5:08 p.m.")
        self.assertEqual(readable("2d"), "2d")
        self.assertEqual(readable("Tailnet"), "Tailnet")
