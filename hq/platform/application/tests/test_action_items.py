"""Which action items a person has set aside, and the queue under its domains."""

from datetime import timedelta
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from hq.platform.application.markup_testing import select
from hq.platform.application.plugin_testing import form_keyed
from hq.platform.application.tests.writers import keyed
from hq.platform.core.models import ActionItemRead, AuditLog
from hq_sdk.testing import plain

from ..action_items import (
    FORGET_AFTER,
    RESTORE,
    SET_ASIDE,
    by_source,
    item_key,
    item_revision,
    named_rows,
    row_value,
    set_aside,
    waiting_count,
    with_aside_state,
)
from ..capabilities import execute_capability
from ..finding_model import Finding
from ..findings import finding_key
from ..security import OPERATOR_CAPABILITIES, Capability, Principal, cli_principal, internal_principal

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
    "steps": [],
    "no_help_reason": "",
    "check": None,
}
OTHER = {**ITEM, "key": "example:gadget", "label": "A gadget is stale", "count": 1}


class IdentityTests(TestCase):
    def finding(self, **fields):
        return Finding(
            **{"rule": "rule", "subject": "a", "severity": "attention", "title": "t", "explanation": "", **fields}
        )

    def test_an_item_is_what_it_is_about_not_what_it_says(self):
        first = self.finding(title="Expires in 5 days", explanation="x")
        later = self.finding(title="Expires in 4 days", explanation="y")

        self.assertEqual(item_key("infra", finding_key(first)), item_key("infra", finding_key(later)))
        self.assertEqual(item_revision(first), item_revision(later))

    def test_getting_worse_or_growing_is_a_new_revision(self):
        base = self.finding(count=2)

        self.assertNotEqual(item_revision(base), item_revision(self.finding(count=3)))
        self.assertNotEqual(item_revision(base), item_revision(self.finding(count=2, severity="serious")))

    def test_a_notice_comes_back_only_when_it_reports_something_else(self):
        told = self.finding(notice=True, explanation="Rose 19 bpm on the Sep 21 run")

        # Nothing about a notice gets worse, and a counter that moves on its
        # own is not news.
        self.assertEqual(
            item_revision(told),
            item_revision(
                self.finding(notice=True, severity="serious", count=4, explanation="Rose 19 bpm on the Sep 21 run")
            ),
        )
        self.assertNotEqual(
            item_revision(told),
            item_revision(self.finding(notice=True, explanation="Rose 12 bpm on the Sep 28 run")),
        )


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

    def test_only_current_items_are_marked_and_marking_one_leaves_the_others(self):
        set_aside(self.user, [ITEM["key"], "example:nothing"], aside=True, current=[ITEM, OTHER])
        set_aside(self.user, [OTHER["key"]], aside=True, current=[OTHER])

        self.assertEqual(
            sorted(ActionItemRead.objects.values_list("key", flat=True)),
            sorted([ITEM["key"], OTHER["key"]]),
        )

    def test_a_mark_for_a_gone_item_is_forgotten_after_a_while(self):
        set_aside(self.user, [ITEM["key"]], aside=True, current=[ITEM])
        ActionItemRead.objects.update(read_at=timezone.now() - FORGET_AFTER - timedelta(days=1))

        set_aside(self.user, [OTHER["key"]], aside=True, current=[OTHER])

        self.assertEqual(list(ActionItemRead.objects.values_list("key", flat=True)), [OTHER["key"]])

    def test_a_held_request_is_never_set_aside_and_anything_else_is(self):
        held = {**OTHER, "held": True}

        set_aside(self.user, [ITEM["key"], held["key"]], aside=True, current=[ITEM, held])

        # Decided by its Approve or Reject, it waits until then: a mark,
        # however it was made, does not hide it.
        self.assertEqual(
            [item["key"] for item in with_aside_state([ITEM, held], self.user) if not item["aside"]], [held["key"]]
        )
        self.assertEqual(waiting_count([ITEM, held], self.user), 1)

    def test_an_item_raised_again_after_it_cleared_is_back_and_one_still_open_stays_dismissed(self):
        now = timezone.now()
        still_open = {**ITEM, "since": (now - timedelta(days=3)).isoformat()}
        set_aside(self.user, [ITEM["key"], OTHER["key"]], aside=True, current=[still_open, OTHER])
        ActionItemRead.objects.update(read_at=now - timedelta(days=2))

        # OTHER cleared after it was dismissed and is raised again, as it was.
        returned = {**OTHER, "since": (now - timedelta(days=1)).isoformat()}

        self.assertEqual(
            [(item["key"], item["aside"]) for item in with_aside_state([still_open, returned], self.user)],
            [(ITEM["key"], True), (OTHER["key"], False)],
        )

    def test_bringing_back_undoes_setting_aside(self):
        set_aside(self.user, [ITEM["key"]], aside=True, current=[ITEM])
        set_aside(self.user, [ITEM["key"]], aside=False, current=[ITEM])

        self.assertEqual(waiting_count([ITEM], self.user), 1)

    def test_the_queue_is_grouped_under_the_domain_each_item_names(self):
        elsewhere = {**ITEM, "key": "other:thing", "source_id": "other", "source": "Other"}

        groups = by_source([ITEM, elsewhere, OTHER])

        self.assertEqual(
            [(group["id"], group["label"]) for group in groups], [("example", "Example"), ("other", "Other")]
        )
        self.assertEqual([item["key"] for item in groups[0]["items"]], [ITEM["key"], OTHER["key"]])

    def test_only_a_named_family_folds(self):
        # Items that share no family each stand on their own row, whatever
        # else they share; a fold is drawn under its family's name.
        twin = {**ITEM, "label": "Another widget"}
        named = [{**ITEM, "key": f"example:{n}", "family": "Widgets"} for n in range(2)]

        (group,) = by_source([ITEM, twin, *named])

        self.assertEqual(
            [(entry["folded"], entry["family"], len(entry["items"])) for entry in group["entries"]],
            [(False, "", 1), (False, "", 1), (True, "Widgets", 2)],
        )


class PageTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("op", password="x" * 20)
        self.client.force_login(self.user)
        # The page and the header's count both read the queue through it.
        patcher = mock.patch("hq.platform.application.dashboard.work_queue", return_value=[ITEM, OTHER])
        patcher.start()
        self.addCleanup(patcher.stop)

    def shown(self, page, name="action_groups"):
        return [item["label"] for group in page.context[name] for item in group["items"]]

    def test_setting_one_aside_moves_it_below_and_lowers_the_count(self):
        self.client.post(reverse("action_items_set_aside"), form_keyed({"key": row_value(ITEM)}))

        page = self.client.get(reverse("action_items"))

        self.assertEqual(self.shown(page), [OTHER["label"]])
        self.assertEqual(self.shown(page, "aside_groups"), [ITEM["label"]])
        self.assertEqual(page.context["profile_action_count"], 1)
        self.assertEqual(self.client.get(reverse("action_item_count")).context["profile_action_count"], 1)

    def test_the_dashboard_counts_what_waits_and_items_not_the_numbers_they_carry(self):
        # ITEM stands for two widgets and still waits: one thing needs you.
        self.client.post(reverse("action_items_set_aside"), form_keyed({"key": row_value(OTHER)}))

        with mock.patch("hq.platform.application.dashboard.work_queue", return_value=[ITEM, OTHER]):
            page = self.client.get(reverse("dashboard"))

        expected = self.client.get(reverse("action_item_count")).context["profile_action_count"]
        self.assertEqual(expected, 1)
        self.assertEqual(page.context["action_queue_count"], expected)
        self.assertEqual(page.context["profile_action_count"], expected)
        self.assertIn("1 thing needs you", plain(page.content))

    def test_a_row_button_posts_its_key_to_a_route_that_names_the_state(self):
        page = self.client.get(reverse("action_items"))
        self.assertContains(page, f'formaction="{reverse("action_items_set_aside")}"')

        self.client.post(reverse("action_items_set_aside"), form_keyed({"key": row_value(ITEM)}))
        self.assertEqual(waiting_count([ITEM, OTHER], self.user), 1)
        self.assertContains(
            self.client.get(reverse("action_items")), f'formaction="{reverse("action_items_bring_back")}"'
        )

        self.client.post(reverse("action_items_bring_back"), form_keyed({"key": row_value(ITEM)}))
        self.assertEqual(waiting_count([ITEM, OTHER], self.user), 2)

    def test_a_held_requests_row_offers_no_dismiss_and_any_other_row_does(self):
        held = {**OTHER, "held": True}
        with mock.patch("hq.platform.application.dashboard.work_queue", return_value=[ITEM, held]):
            page = self.client.get(reverse("action_items"))

        dismissed = [
            button.attrs.get("value") for button in select(page, "formaction", reverse("action_items_set_aside"))
        ]
        self.assertIn(row_value(ITEM), dismissed)
        self.assertNotIn(row_value(held), dismissed)

    def test_one_row_is_dismissed_without_composing_the_queue(self):
        with mock.patch("hq.platform.application.dashboard.work_queue", side_effect=AssertionError("composed")):
            reply = self.client.post(
                reverse("action_items_set_aside"),
                form_keyed({"key": row_value(ITEM)}),
                headers={"x-requested-with": "XMLHttpRequest"},
            )
            self.assertEqual(reply.status_code, 204)
            self.client.post(reverse("action_items_bring_back"), form_keyed({"key": row_value(ITEM)}))
            self.client.post(reverse("action_items_set_aside"), form_keyed({"key": row_value(OTHER)}))

        self.assertEqual(
            [item["key"] for item in with_aside_state([ITEM, OTHER], self.user) if item["aside"]],
            [OTHER["key"]],
        )

    def test_a_row_dismissed_at_an_old_revision_does_not_hide_the_item(self):
        self.client.post(reverse("action_items_set_aside"), form_keyed({"key": f"old0 {ITEM['key']}"}))

        self.assertEqual(waiting_count([ITEM, OTHER], self.user), 2)

    def test_a_malformed_row_names_nothing(self):
        self.assertEqual(named_rows(["", "nospace", " key", "bad!rev key", "x" * 17 + " key", "r1 " + "k" * 201]), {})
        self.assertEqual(named_rows(["r1 example:a b"]), {"example:a b": "r1"})

    def test_a_family_folds_and_can_be_dismissed_whole(self):
        family = [
            {**ITEM, "key": f"example:advisory:{n}", "label": f"Image {n} has advisories", "family": "Image advisories"}
            for n in range(3)
        ]
        with (
            mock.patch("hq.platform.application.dashboard.work_queue", return_value=[*family, OTHER]),
        ):
            page = self.client.get(reverse("action_items"))
            (group,) = page.context["action_groups"]
            self.assertEqual(
                [(entry["family"], entry["folded"], len(entry["items"])) for entry in group["entries"]],
                [("Image advisories", True, 3), ("", False, 1)],
            )
            self.assertContains(page, "data-queue-family", count=1)

            self.client.post(f"{page.context['dismiss_all'].url}&family={group['entries'][0]['id']}", form_keyed())
            page = self.client.get(reverse("action_items"))

        self.assertEqual(self.shown(page), [OTHER["label"]])
        self.assertEqual(page.context["aside_total"], 3)

    def test_restoring_names_what_it_restores(self):
        self.client.post(reverse("action_items_set_aside"), form_keyed({"key": [row_value(ITEM), row_value(OTHER)]}))

        self.client.post(reverse("action_items_bring_back"), form_keyed())

        self.assertEqual(waiting_count([ITEM, OTHER], self.user), 0)

    def test_setting_all_aside_means_all_the_filters_show(self):
        page = self.client.get(reverse("action_items"), {"q": "gadget"})
        self.assertFalse(page.context["page"].actions)
        url = urlsplit(page.context["dismiss_all"].url)
        self.assertEqual(url.path, reverse("action_items_set_aside"))
        self.assertEqual(parse_qs(url.query)["q"], ["gadget"])

        self.client.post(page.context["dismiss_all"].url, form_keyed())

        self.assertEqual(self.shown(self.client.get(reverse("action_items"))), [ITEM["label"]])
        filtered = self.client.get(reverse("action_items"), {"q": "gadget"})
        self.assertEqual(self.shown(filtered), [])
        self.assertContains(filtered, "data-empty-state")

    def test_a_notice_is_kept_apart_from_what_needs_doing_and_out_of_that_count(self):
        told = {**ITEM, "key": "example:run", "label": "A run drifted", "notice": True}
        with (
            mock.patch("hq.platform.application.dashboard.work_queue", return_value=[ITEM, told]),
        ):
            page = self.client.get(reverse("action_items"))
            self.assertEqual(self.shown(page), [ITEM["label"]])
            self.assertEqual(self.shown(page, "notice_groups"), [told["label"]])
            # The header counts everything waiting; dismissing the notices
            # leaves what needs doing where it was.
            self.assertEqual(page.context["profile_action_count"], 2)
            self.client.post(page.context["dismiss_notices"].url, form_keyed())
            page = self.client.get(reverse("action_items"))
            self.assertEqual(self.shown(page), [ITEM["label"]])
            self.assertEqual(page.context["notice_total"], 0)
            self.assertEqual(self.shown(page, "aside_groups"), [told["label"]])
            self.client.post(reverse("action_items_bring_back"), form_keyed({"key": row_value(told)}))

        with mock.patch("hq.platform.application.dashboard.work_queue", return_value=[ITEM, told]):
            dashboard = self.client.get(reverse("dashboard"))
        self.assertIn("1 thing needs you", plain(dashboard.content))
        self.assertIn("1 notice", plain(dashboard.content))

    def test_the_page_is_one_section_per_domain(self):
        page = self.client.get(reverse("action_items"))

        self.assertEqual([group["id"] for group in page.context["action_groups"]], ["example"])
        self.assertEqual(len(select(page, "data-queue")), 1)


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
            recent_activity(limit=1)["items"][0]["actor"],
            "example-agent",
        )

    def test_the_audit_trail_needs_its_own_capability(self):
        from hq.platform.core.models import AuditLog

        from ..dashboard import operating_snapshot
        from ..security import Capability, Principal

        AuditLog.objects.create(action=AuditLog.Action.CREATED, object_type="Approval")
        reader = Principal("example-agent", "mcp", frozenset({Capability.READ}))

        self.assertEqual(operating_snapshot(principal=reader)["recent_activity"], [])
        self.assertTrue(operating_snapshot(principal=cli_principal())["recent_activity"])

    def test_a_withheld_trail_reads_differently_from_an_empty_one(self):
        from ..dashboard import operating_snapshot
        from ..security import Capability, Principal

        reader = Principal("example-agent", "mcp", frozenset({Capability.READ}))
        auditor = Principal("example-auditor", "mcp", frozenset({Capability.READ, Capability.READ_AUDIT_LOG}))

        withheld = operating_snapshot(principal=reader)
        empty = operating_snapshot(principal=auditor)

        self.assertEqual(empty["recent_activity"], [])
        self.assertEqual(empty["unread"], [])
        self.assertEqual(withheld["recent_activity"], [])
        self.assertTrue(any("recent_activity" in reason for reason in withheld["unread"]))


class GlanceSettingsLinkTests(TestCase):
    def test_following_the_settings_link_opens_the_settings(self):
        self.client.force_login(get_user_model().objects.create_user("op", password="x" * 20))

        response = self.client.get(reverse("dashboard_glance_settings"))

        self.assertTrue(select(response, "name", "weather_point"))


class ReadableValueTests(TestCase):
    def test_a_timestamp_reads_as_a_date_and_anything_else_is_left_alone(self):
        from hq.platform.core.templatetags.value_tags import readable, when

        self.assertEqual(
            readable("2026-09-20T22:08:26.284763+00:00"),
            when("2026-09-20T22:08:26.284763+00:00"),
        )
        self.assertIn("Sep\xa020", readable("2026-09-20T22:08:26.284763+00:00"))
        self.assertEqual(readable("2d"), "2d")
        self.assertEqual(readable("Tailnet"), "Tailnet")


class AsideCapabilityTests(TestCase):
    """Setting aside is the person's own: through the chokepoint, audited, and refused to any token."""

    def setUp(self):
        self.user = get_user_model().objects.create_user("op", password="x" * 20)
        self.client.force_login(self.user)
        patcher = mock.patch("hq.platform.application.dashboard.work_queue", return_value=[ITEM, OTHER])
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_setting_aside_and_bringing_back_are_audited_as_the_person(self):
        self.client.post(reverse("action_items_set_aside"), form_keyed({"key": row_value(ITEM)}))
        self.client.post(reverse("action_items_bring_back"), form_keyed({"key": row_value(ITEM)}))

        for operation in (SET_ASIDE, RESTORE):
            with self.subTest(operation=operation):
                self.assertTrue(
                    AuditLog.objects.filter(
                        user=self.user, metadata__operation=operation, metadata__interface="web", metadata__actor="op"
                    ).exists()
                )

    def test_a_principal_with_no_person_is_refused_whatever_it_was_granted(self):
        granted = frozenset({Capability.SET_ASIDE_ACTION_ITEMS})
        rows = {ITEM["key"]: ITEM["revision"]}
        for principal in (
            Principal("op", "api", granted, granted=granted),
            Principal("op", "mcp", granted, granted=granted),
            cli_principal(),
            internal_principal("example-purpose"),
        ):
            with self.subTest(interface=principal.interface):
                result = execute_capability(SET_ASIDE, keyed({"rows": rows}), principal=principal)

                self.assertEqual(result["error"]["code"], "forbidden")
        self.assertFalse(ActionItemRead.objects.exists())
        self.assertEqual(
            AuditLog.objects.filter(action=AuditLog.Action.DENIED, metadata__capability=SET_ASIDE).count(), 4
        )

    def test_the_page_says_when_it_is_refused_and_keeps_nothing(self):
        lacking = Principal("op", "web", frozenset({Capability.READ}))
        with mock.patch("hq.platform.application.web_writes.web_principal", return_value=lacking):
            response = self.client.post(
                reverse("action_items_set_aside"), form_keyed({"key": row_value(ITEM)}), follow=True
            )

        self.assertContains(response, Capability.SET_ASIDE_ACTION_ITEMS.value)
        self.assertFalse(ActionItemRead.objects.exists())
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.Action.DENIED, metadata__capability=SET_ASIDE).exists())

    def test_a_malformed_request_is_invalid_input(self):
        operator = Principal("op", "web", OPERATOR_CAPABILITIES)
        for name, payload in (
            (SET_ASIDE, {"rows": {ITEM["key"]: "bad!rev"}}),
            (SET_ASIDE, {"keys": ITEM["key"]}),
            (RESTORE, {}),
        ):
            with self.subTest(payload=payload):
                result = execute_capability(name, keyed(payload), principal=operator)

                self.assertEqual(result["error"]["code"], "invalid_input")
        self.assertFalse(ActionItemRead.objects.exists())
