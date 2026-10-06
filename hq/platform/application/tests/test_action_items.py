"""Which action items a person has set aside, and the queue under its domains."""

from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from hq.platform.core.models import ActionItemRead

from ..action_items import FORGET_AFTER, by_source, named_rows, row_value, item_key, item_revision, set_aside, waiting_count, with_aside_state
from ..security import cli_principal
from ..ui import Insight

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

    def test_a_notice_comes_back_only_when_it_reports_something_else(self):
        told = self.insight(key="k", notice=True, body="Rose 19 bpm on the Sep 21 run", value="1 day ago")

        # Nothing about a notice gets worse, and a counter that moves on its
        # own is not news.
        self.assertEqual(
            item_revision(told),
            item_revision(self.insight(key="k", notice=True, status="serious", magnitude=4,
                                       body="Rose 19 bpm on the Sep 21 run", value="2 days ago")),
        )
        self.assertNotEqual(
            item_revision(told),
            item_revision(self.insight(key="k", notice=True, body="Rose 12 bpm on the Sep 28 run")),
        )

    def test_without_a_key_the_eyebrow_and_title_stand_in(self):
        self.assertEqual(item_key("ext", self.insight(title="Plan a run")), "ext:Finding:Plan a run")


class HostItemsNameTheirSubjectTests(TestCase):
    def test_every_host_attention_item_carries_a_key(self):
        """Read state follows the key, so a host item without one would fall
        back to its title, and several titles carry counts and days-left."""

        import ast
        from pathlib import Path

        source = Path(__file__).parent.with_name("attention.py").read_text()
        missing = [
            node.lineno
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call)
            and getattr(node.func, "id", "") in {"Insight", "_backlog"}
            and "key" not in {keyword.arg for keyword in node.keywords}
        ]

        self.assertEqual(missing, [], f"attention.py builds items without a key at lines {missing}")


class AsideStateTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("op", password="x" * 20)
        self.someone = get_user_model().objects.create_user("other", password="x" * 20)

    def test_read_items_leave_the_count_for_that_person_only(self):
        set_aside(self.user, [ITEM["key"]], aside=True, current=[ITEM, OTHER])

        self.assertEqual(waiting_count([ITEM, OTHER], self.user), 1)
        # Two items, whatever each carries: ITEM stands for two things, and
        # says so on itself.
        self.assertEqual(waiting_count([ITEM, OTHER], self.someone), 2)

    def test_a_reworded_item_stays_read_and_a_new_revision_does_not(self):
        set_aside(self.user, [ITEM["key"]], aside=True, current=[ITEM])

        reworded = {**ITEM, "label": "Two widgets still need a look", "detail": "changed"}
        revised = {**ITEM, "revision": "r2"}

        self.assertTrue(with_aside_state([reworded], self.user)[0]["aside"])
        self.assertFalse(with_aside_state([revised], self.user)[0]["aside"])

    def test_marking_one_item_leaves_the_others_alone(self):
        set_aside(self.user, [ITEM["key"]], aside=True, current=[ITEM, OTHER])
        set_aside(self.user, [OTHER["key"]], aside=True, current=[OTHER])

        self.assertEqual(
            sorted(ActionItemRead.objects.values_list("key", flat=True)),
            sorted([ITEM["key"], OTHER["key"]]),
        )

    def test_only_current_items_can_be_marked(self):
        set_aside(self.user, [ITEM["key"], "example:nothing"], aside=True, current=[ITEM])

        self.assertEqual(list(ActionItemRead.objects.values_list("key", flat=True)), [ITEM["key"]])

    def test_a_mark_for_a_gone_item_is_forgotten_after_a_while(self):
        set_aside(self.user, [ITEM["key"]], aside=True, current=[ITEM])
        ActionItemRead.objects.update(read_at=timezone.now() - FORGET_AFTER - timedelta(days=1))

        set_aside(self.user, [OTHER["key"]], aside=True, current=[OTHER])

        self.assertEqual(list(ActionItemRead.objects.values_list("key", flat=True)), [OTHER["key"]])

    def test_bringing_back_undoes_setting_aside(self):
        set_aside(self.user, [ITEM["key"]], aside=True, current=[ITEM])
        set_aside(self.user, [ITEM["key"]], aside=False, current=[ITEM])

        self.assertEqual(waiting_count([ITEM], self.user), 1)

    def test_the_queue_is_grouped_under_the_domain_each_item_names(self):
        elsewhere = {**ITEM, "key": "other:thing", "source_id": "other", "source": "Other"}

        groups = by_source([ITEM, elsewhere, OTHER])

        self.assertEqual([(group["id"], group["label"]) for group in groups], [("example", "Example"), ("other", "Other")])
        self.assertEqual([item["key"] for item in groups[0]["items"]], [ITEM["key"], OTHER["key"]])


class PageTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("op", password="x" * 20)
        self.client.force_login(self.user)
        # The page asks for the queue; the header's count derives from it.
        for name in ("hq.platform.core.action_item_views", "hq.platform.application.dashboard"):
            patcher = mock.patch(f"{name}.work_queue", return_value=[ITEM, OTHER])
            patcher.start()
            self.addCleanup(patcher.stop)

    def shown(self, page, name="action_groups"):
        return [item["label"] for group in page.context[name] for item in group["items"]]

    def test_setting_one_aside_moves_it_below_and_lowers_the_count(self):
        self.client.post(reverse("action_items_set_aside"), {"key": row_value(ITEM)})

        page = self.client.get(reverse("action_items"))

        self.assertEqual(self.shown(page), [OTHER["label"]])
        self.assertEqual(self.shown(page, "aside_groups"), [ITEM["label"]])
        self.assertEqual(page.context["profile_action_count"], 1)
        self.assertContains(page, "Dismissed")
        self.assertEqual(self.client.get(reverse("action_item_count")).json(), {"count": 1})

    def test_the_dashboard_counts_what_waits_and_items_not_the_numbers_they_carry(self):
        # ITEM stands for two widgets and still waits: one thing needs you.
        self.client.post(reverse("action_items_set_aside"), {"key": row_value(OTHER)})

        with mock.patch("hq.platform.application.dashboard.work_queue", return_value=[ITEM, OTHER]):
            page = self.client.get(reverse("dashboard"))

        expected = self.client.get(reverse("action_item_count")).json()["count"]
        self.assertEqual(expected, 1)
        self.assertEqual(page.context["action_queue_count"], expected)
        self.assertEqual(page.context["profile_action_count"], expected)
        self.assertContains(page, "1 thing needs you")

    def test_a_row_button_posts_its_key_to_a_route_that_names_the_state(self):
        page = self.client.get(reverse("action_items"))
        self.assertContains(page, f'formaction="{reverse("action_items_set_aside")}"')

        self.client.post(reverse("action_items_set_aside"), {"key": row_value(ITEM)})
        self.assertEqual(waiting_count([ITEM, OTHER], self.user), 1)
        self.assertContains(
            self.client.get(reverse("action_items")), f'formaction="{reverse("action_items_bring_back")}"'
        )

        self.client.post(reverse("action_items_bring_back"), {"key": row_value(ITEM)})
        self.assertEqual(waiting_count([ITEM, OTHER], self.user), 2)

    def test_one_row_is_dismissed_without_composing_the_queue(self):
        with mock.patch("hq.platform.core.action_item_views.work_queue", side_effect=AssertionError("composed")):
            reply = self.client.post(
                reverse("action_items_set_aside"),
                {"key": row_value(ITEM)},
                headers={"x-requested-with": "XMLHttpRequest"},
            )
            self.assertEqual(reply.status_code, 204)
            self.client.post(reverse("action_items_bring_back"), {"key": row_value(ITEM)})
            self.client.post(reverse("action_items_set_aside"), {"key": row_value(OTHER)})

        self.assertEqual(
            [item["key"] for item in with_aside_state([ITEM, OTHER], self.user) if item["aside"]],
            [OTHER["key"]],
        )

    def test_a_row_dismissed_at_an_old_revision_does_not_hide_the_item(self):
        self.client.post(reverse("action_items_set_aside"), {"key": f"old0 {ITEM['key']}"})

        self.assertEqual(waiting_count([ITEM, OTHER], self.user), 2)

    def test_a_malformed_row_names_nothing(self):
        self.assertEqual(named_rows(["", "nospace", " key", "bad!rev key", "x" * 17 + " key", "r1 " + "k" * 201]), {})
        self.assertEqual(named_rows(["r1 example:a b"]), {"example:a b": "r1"})

    def test_a_family_folds_and_can_be_dismissed_whole(self):
        family = [
            {**ITEM, "key": f"example:advisory:{n}", "label": f"Image {n} has advisories", "family": "Image advisories"}
            for n in range(3)
        ]
        with mock.patch("hq.platform.core.action_item_views.work_queue", return_value=[*family, OTHER]):
            page = self.client.get(reverse("action_items"))
            (group,) = page.context["action_groups"]
            self.assertEqual(
                [(entry["family"], entry["folded"], len(entry["items"])) for entry in group["entries"]],
                [("Image advisories", True, 3), ("", False, 1)],
            )
            self.assertContains(page, "data-queue-family", count=1)
            self.assertContains(page, "<strong>Image advisories</strong>")

            self.client.post(f'{page.context["aside_all_url"]}&family={group["entries"][0]["id"]}')
            page = self.client.get(reverse("action_items"))

        self.assertEqual(self.shown(page), [OTHER["label"]])
        self.assertEqual(page.context["aside_total"], 3)

    def test_restoring_names_what_it_restores(self):
        self.client.post(reverse("action_items_set_aside"), {"key": [row_value(ITEM), row_value(OTHER)]})

        self.client.post(reverse("action_items_bring_back"))

        self.assertEqual(waiting_count([ITEM, OTHER], self.user), 0)

    def test_setting_all_aside_means_all_the_filters_show(self):
        page = self.client.get(reverse("action_items"), {"q": "gadget"})
        self.assertFalse(page.context["page"].actions)
        url = page.context["aside_all_url"]
        self.assertEqual(url, f'{reverse("action_items_set_aside")}?q=gadget&part=doing')

        self.client.post(url)

        self.assertEqual(self.shown(self.client.get(reverse("action_items"))), [ITEM["label"]])
        self.assertContains(self.client.get(reverse("action_items"), {"q": "gadget"}), "Nothing matches these filters.")

    def test_a_notice_is_kept_apart_from_what_needs_doing_and_out_of_that_count(self):
        told = {**ITEM, "key": "example:run", "label": "A run drifted", "notice": True}
        with mock.patch("hq.platform.core.action_item_views.work_queue", return_value=[ITEM, told]):
            page = self.client.get(reverse("action_items"))
            self.assertEqual(self.shown(page), [ITEM["label"]])
            self.assertEqual(self.shown(page, "notice_groups"), [told["label"]])
            self.assertContains(page, '<span class="pill pill-idea">Notice</span>', count=1)
            # The header counts everything waiting; dismissing the notices
            # leaves what needs doing where it was.
            self.assertEqual(page.context["profile_action_count"], 2)
            self.client.post(page.context["notices_aside_url"])
            page = self.client.get(reverse("action_items"))
            self.assertEqual(self.shown(page), [ITEM["label"]])
            self.assertEqual(page.context["notice_total"], 0)
            self.assertEqual(self.shown(page, "aside_groups"), [told["label"]])
            self.client.post(reverse("action_items_bring_back"), {"key": row_value(told)})

        with mock.patch("hq.platform.application.dashboard.work_queue", return_value=[ITEM, told]):
            dashboard = self.client.get(reverse("dashboard"))
        self.assertContains(dashboard, "1 thing needs you")
        self.assertContains(dashboard, "1 notice")

    def test_the_page_is_sections_by_domain_and_a_row_does_not_repeat_its_domain(self):
        page = self.client.get(reverse("action_items"))

        self.assertContains(page, '<section class="queue-group"', count=1)
        self.assertContains(page, "Example", count=1 + 1)  # the heading and the source filter


class RouteTests(TestCase):
    def test_no_route_marks_one_item_read(self):
        from django.urls import NoReverseMatch

        with self.assertRaises(NoReverseMatch):
            reverse("action_items_mark_read")


class ActivityActorTests(TestCase):
    def test_an_agent_row_names_the_agent_not_system(self):
        from hq.platform.core.models import AuditLog

        from ..read_models import recent_activity

        AuditLog.objects.create(
            action=AuditLog.Action.CREATED, object_type="Approval", metadata={"actor": "example-agent"}
        )

        self.assertEqual(
            recent_activity(principal=cli_principal(), limit=1)["items"][0]["actor"],
            "example-agent",
        )

    def test_the_audit_trail_needs_its_own_capability(self):
        from hq.platform.core.models import AuditLog

        from ..dashboard import operating_snapshot
        from ..read_models import recent_activity
        from ..security import AuthorizationError, Capability, Principal

        AuditLog.objects.create(action=AuditLog.Action.CREATED, object_type="Approval")
        reader = Principal("example-agent", "mcp", frozenset({Capability.READ}))

        with self.assertRaises(AuthorizationError):
            recent_activity(principal=reader)
        self.assertEqual(operating_snapshot(principal=reader)["recent_activity"], [])
        self.assertTrue(operating_snapshot(principal=cli_principal())["recent_activity"])


class GlanceSettingsLinkTests(TestCase):
    def test_following_the_settings_link_opens_the_settings(self):
        self.client.force_login(get_user_model().objects.create_user("op", password="x" * 20))

        response = self.client.get(reverse("dashboard_glance_settings"))

        self.assertContains(response, 'name="weather_point"')


class ReadableValueTests(TestCase):
    def test_a_timestamp_reads_as_a_date_and_anything_else_is_left_alone(self):
        from hq.platform.core.templatetags.value_tags import readable, when

        self.assertEqual(
            readable("2026-09-20T22:08:26.284763+00:00"),
            when("2026-09-20T22:08:26.284763+00:00"),
        )
        self.assertIn("Sep 20", readable("2026-09-20T22:08:26.284763+00:00"))
        self.assertEqual(readable("2d"), "2d")
        self.assertEqual(readable("Tailnet"), "Tailnet")
