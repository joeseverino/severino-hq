"""A page opens on what differs, and the rest is one press away: the Agents
page's rules left at the default are drawn when opened, and a save posts only
the rules drawn."""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from hq.domains.control_plane.models import CapabilityRule
from hq.platform.application import history
from hq.platform.application.capability_policy import (
    Column,
    Group,
    PolicyCell,
    Row,
    Rule,
    field_name,
    split_by_default,
    submitted_rules,
)
from hq.platform.application.tests.markup import all_elements, elements, hrefs, select, text, with_class, within
from hq.platform.core.models import AgentIdentity, AuditLog

# The name the page's placeholder asks for the every-rule part by, written out:
# the test holds what a fetch asks for, whatever the view calls it.
EVERY_PART = "every_rule"


AGENT = AgentIdentity(client_id="example-agent", interfaces=["mcp"])
COLUMNS = (Column(AGENT.client_id, AGENT.client_id, "MCP", AGENT),)


def _row(name: str, rule: str | None) -> Row:
    (column,) = COLUMNS
    cell = PolicyCell(
        field=field_name(column.subject, name),
        column=column.label,
        rule=rule,
        options=(("", "Default"), ("deny", "Refuse")),
    )
    return Row(name, f"Do {name}", "write", "Write", "Allow", (cell,))


GROUPS = (
    Group("Example records", (_row("example.create", None), _row("example.delete", "deny"))),
    Group("Example notes", (_row("example.note", None),)),
)


class AgentRulesTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser("owner", "owner@example.test", "pw"))

    def page(self, groups=GROUPS, *, part="", **params):
        headers = {"HTTP_X_FRAGMENT": part} if part else {}
        with patch("hq.platform.application.capability_policy.matrix", return_value=(COLUMNS, groups)):
            return self.client.get(reverse("agent_policy"), params, **headers)

    def test_every_row_is_on_exactly_one_side(self):
        changed, at_default = split_by_default(GROUPS)

        self.assertEqual(
            [(group.label, [row.name for row in group.rows]) for group in changed],
            [("Example records", ["example.delete"])],
        )
        self.assertEqual(
            [(group.label, [row.name for row in group.rows]) for group in at_default],
            [("Example records", ["example.create"]), ("Example notes", ["example.note"])],
        )

    def test_the_page_opens_on_what_differs_and_draws_the_rest_when_opened(self):
        page = self.page()
        (rules,) = with_class(page, "policy")
        (folded,) = [fold for fold in within(page, "class", "policy") if "section-fold" in fold.classes]
        (placeholder,) = select(page, "data-fragment-name", EVERY_PART)

        self.assertIn("Do example.delete", rules.words)
        self.assertNotIn("open", folded.attrs)
        self.assertNotIn("Do example.create", folded.words)
        self.assertIn("data-fragment-load", placeholder.attrs)
        self.assertIn(placeholder.attrs["data-fragment-fallback"], hrefs(page))

        part = self.page(part=EVERY_PART)
        self.assertIn("Do example.create", text(part))
        self.assertIn("Example notes", text(part))
        self.assertNotIn("Do example.delete", text(part))

    def test_the_part_reads_nothing_but_the_rules(self):
        with patch("hq.platform.application.agent_activity.standings") as standings:
            self.page(part=EVERY_PART)

        standings.assert_not_called()

    def test_without_script_the_address_draws_every_action_and_none_is_lost(self):
        page = self.page(groups=(Group("Example notes", (_row("example.note", None),)),), all=1)
        (folded,) = [fold for fold in within(page, "class", "policy") if "section-fold" in fold.classes]

        self.assertIn("open", folded.attrs)
        self.assertIn("Do example.note", folded.words)
        self.assertEqual(len(elements(page, "table")), 1)


def _drawn(*answers) -> dict[str, str]:
    """What the policy form posts once ``answers`` are drawn into it: each
    field's value, a select's the option it opens on."""

    fields = {}
    for answer in answers:
        for element in all_elements(answer):
            name = element.attrs.get("name", "")
            if element.tag == "input" and name and any("policy" in held.classes for held in element.ancestors):
                fields[name] = element.attrs.get("value", "")
            elif element.tag == "option" and "selected" in element.attrs:
                fields[element.ancestors[-1].attrs["name"]] = element.attrs["value"]
    return fields


class FoldedSaveTests(TestCase):
    """A rule not drawn is not posted, and a rule not posted is left as it holds."""

    def setUp(self):
        self.owner = get_user_model().objects.create_superuser("owner", "owner@example.test", "pw")
        self.client.force_login(self.owner)
        AgentIdentity.objects.create(client_id="example-agent", granted=["read", "write_projects", "delete_projects"])
        self.changed = field_name("example-agent", "project.delete")
        CapabilityRule.objects.create(
            subject="example-agent", capability="project.delete", rule=Rule.DENY, changed_by=self.owner
        )

    def held(self) -> set[tuple[str, str, str]]:
        return set(CapabilityRule.objects.values_list("subject", "capability", "rule"))

    def test_a_save_with_the_fold_never_opened_changes_no_rule(self):
        before = self.held()
        posted = _drawn(self.client.get(reverse("agent_policy")))

        self.assertIn(self.changed, posted)
        self.client.post(reverse("agent_policy"), posted)

        self.assertEqual(self.held(), before)

    def test_a_save_after_it_opened_posts_the_rules_it_drew(self):
        page = self.client.get(reverse("agent_policy"))
        part = self.client.get(reverse("agent_policy"), HTTP_X_FRAGMENT=EVERY_PART)
        posted = _drawn(page, part)
        (first, *_) = [name for name in _drawn(part) if name != self.changed]
        posted[first] = Rule.DENY

        self.client.post(reverse("agent_policy"), posted)

        (opened,) = submitted_rules({first: Rule.DENY})
        self.assertIn((opened["subject"], opened["capability"], Rule.DENY), self.held())
        self.assertIn(("example-agent", "project.delete", Rule.DENY), self.held())


class AuditLogReadRequestTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser("owner", "owner@example.test", "pw"))
        asked = history.read_request_type()
        AuditLog.objects.create(action=AuditLog.Action.CREATED, object_type=asked, object_repr="example-dns")
        AuditLog.objects.create(action=AuditLog.Action.DELETED, object_type=asked, object_repr="example-dns")
        AuditLog.objects.create(action=AuditLog.Action.UPDATED, object_type="Expense", object_repr="Example Hosting")

    def shown(self, **params):
        page = self.client.get(reverse("core:audit_list"), params)
        return [event.object_type for entry in page.context["events"] for event in entry.events]

    def test_requests_for_a_reading_are_left_out_of_the_log(self):
        shown = self.shown()

        self.assertIn("Expense", shown)
        self.assertNotIn(history.read_request_type(), shown)

    def test_they_are_one_toggle_away(self):
        self.assertEqual(self.shown(reads=1).count(history.read_request_type()), 2)
