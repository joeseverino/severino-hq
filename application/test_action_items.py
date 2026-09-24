"""Marking action items read."""

from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import ActionItemRead

from .action_items import FORGET_AFTER, item_key, item_revision, mark, unread_count, with_read_state
from .ui import Insight

ITEM = {
    "key": "example:widgets",
    "revision": "r1",
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
OTHER = {**ITEM, "key": "example:gadget", "label": "A gadget is stale", "count": 1}


class IdentityTests(TestCase):
    def insight(self, **fields):
        return Insight(**{"status": "attention", "eyebrow": "Finding", "title": "t",
                          "value": "", "body": "", **fields})

    def test_an_item_is_what_it_is_about_not_what_it_says(self):
        first = self.insight(key="finding:rule:a", title="Expires in 5 days", body="x")
        later = self.insight(key="finding:rule:a", title="Expires in 4 days", body="y")

        self.assertEqual(item_key("infra", first), item_key("infra", later))
        self.assertEqual(item_revision(first), item_revision(later))

    def test_getting_worse_or_growing_is_a_new_revision(self):
        base = self.insight(key="k", magnitude=2)

        self.assertNotEqual(item_revision(base), item_revision(self.insight(key="k", magnitude=3)))
        self.assertNotEqual(
            item_revision(base), item_revision(self.insight(key="k", magnitude=2, status="serious"))
        )

    def test_without_a_key_the_eyebrow_and_title_stand_in(self):
        self.assertEqual(item_key("ext", self.insight(title="Plan a run")), "ext:Finding:Plan a run")


class HostItemsNameTheirSubjectTests(TestCase):
    def test_every_host_attention_item_carries_a_key(self):
        """Read state follows the key, so a host item without one would fall
        back to its title -- and several titles carry counts and days-left."""

        import ast
        from pathlib import Path

        source = Path(__file__).with_name("attention.py").read_text()
        missing = [
            node.lineno
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call)
            and getattr(node.func, "id", "") in {"Insight", "_backlog"}
            and "key" not in {keyword.arg for keyword in node.keywords}
        ]

        self.assertEqual(missing, [], f"attention.py builds items without a key at lines {missing}")


class ReadStateTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("op", password="x" * 20)
        self.someone = get_user_model().objects.create_user("other", password="x" * 20)

    def test_read_items_leave_the_count_for_that_person_only(self):
        mark(self.user, [ITEM["key"]], read=True, current=[ITEM, OTHER])

        self.assertEqual(unread_count([ITEM, OTHER], self.user), 1)
        self.assertEqual(unread_count([ITEM, OTHER], self.someone), 3)

    def test_a_reworded_item_stays_read_and_a_new_revision_does_not(self):
        mark(self.user, [ITEM["key"]], read=True, current=[ITEM])

        reworded = {**ITEM, "label": "Two widgets still need a look", "detail": "changed"}
        revised = {**ITEM, "revision": "r2"}

        self.assertTrue(with_read_state([reworded], self.user)[0]["read"])
        self.assertFalse(with_read_state([revised], self.user)[0]["read"])

    def test_marking_one_item_leaves_the_others_alone(self):
        mark(self.user, [ITEM["key"]], read=True, current=[ITEM, OTHER])
        mark(self.user, [OTHER["key"]], read=True, current=[OTHER])

        self.assertEqual(
            sorted(ActionItemRead.objects.values_list("key", flat=True)),
            sorted([ITEM["key"], OTHER["key"]]),
        )

    def test_only_current_items_can_be_marked(self):
        mark(self.user, [ITEM["key"], "example:nothing"], read=True, current=[ITEM])

        self.assertEqual(list(ActionItemRead.objects.values_list("key", flat=True)), [ITEM["key"]])

    def test_a_mark_for_a_gone_item_is_forgotten_after_a_while(self):
        mark(self.user, [ITEM["key"]], read=True, current=[ITEM])
        ActionItemRead.objects.update(read_at=timezone.now() - FORGET_AFTER - timedelta(days=1))

        mark(self.user, [OTHER["key"]], read=True, current=[OTHER])

        self.assertEqual(list(ActionItemRead.objects.values_list("key", flat=True)), [OTHER["key"]])

    def test_unread_undoes_read(self):
        mark(self.user, [ITEM["key"]], read=True, current=[ITEM])
        mark(self.user, [ITEM["key"]], read=False, current=[ITEM])

        self.assertEqual(unread_count([ITEM], self.user), 2)


class PageTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("op", password="x" * 20)
        self.client.force_login(self.user)
        patcher = mock.patch("core.views.work_queue", return_value=[ITEM, OTHER])
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_marking_read_moves_it_below_and_lowers_the_count(self):
        self.client.post(reverse("action_items_read"), {"key": ITEM["key"], "read": "1"})

        page = self.client.get(reverse("action_items"))

        self.assertEqual([item["label"] for item in page.context["action_items"]], [OTHER["label"]])
        self.assertEqual(page.context["profile_action_count"], 1)
        self.assertContains(page, "Read · 1")
        self.assertEqual(self.client.get(reverse("action_item_count")).json(), {"count": 1})

    def test_the_dashboard_counts_and_lists_only_unread(self):
        self.client.post(reverse("action_items_read"), {"key": ITEM["key"], "read": "1"})

        with mock.patch("application.dashboard.work_queue", return_value=[ITEM, OTHER]):
            page = self.client.get(reverse("dashboard"))

        self.assertEqual(page.context["profile_action_count"], 1)
        self.assertEqual(page.context["action_queue_count"], 1)
        self.assertEqual([item["label"] for item in page.context["action_queue"]], [OTHER["label"]])

    def test_mark_all_read_empties_the_unread_list(self):
        self.client.post(
            reverse("action_items_read"), {"key": [ITEM["key"], OTHER["key"]], "read": "1"}
        )

        self.assertContains(self.client.get(reverse("action_items")), "Nothing unread.")

    def test_a_malformed_request_changes_nothing(self):
        response = self.client.post(reverse("action_items_read"), {"key": ITEM["key"]})

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
