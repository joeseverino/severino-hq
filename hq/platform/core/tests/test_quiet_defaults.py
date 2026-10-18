"""A page opens on what differs, and the rest is one press away."""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from hq.platform.application import history
from hq.platform.application.capability_policy import Cell, Column, Group, Row, Scope, split_by_default
from hq.platform.application.tests.markup import elements, with_class
from hq.platform.core.models import AuditLog


def _row(name: str, rule: str | None) -> Row:
    column = Column(Scope.SURFACE, "mcp", "All MCP agents", "MCP")
    cell = Cell(
        field=f"rule:{column.scope}:{column.subject}:{name}",
        scope=column.scope,
        column=column.label,
        rule=rule,
        options=(("", "Default"), ("deny", "Refuse")),
    )
    return Row(name, f"Do {name}", "write", "Write", "Allow", (cell,))


GROUPS = (
    Group("Example records", (_row("example.create", None), _row("example.delete", "deny"))),
    Group("Example notes", (_row("example.note", None),)),
)
COLUMNS = (Column(Scope.SURFACE, "mcp", "All MCP agents", "MCP"),)


class AgentRulesTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser("owner", "owner@example.test", "pw"))

    def page(self, groups=GROUPS, **params):
        with patch("hq.platform.application.capability_policy.matrix", return_value=(COLUMNS, groups)):
            return self.client.get(reverse("agent_policy"), params)

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

    def test_the_page_opens_on_what_differs_and_folds_the_rest(self):
        page = self.page()
        (rules,) = with_class(page, "policy")
        (folded,) = with_class(page, "section-fold")

        self.assertIn("Do example.delete", rules.words)
        self.assertNotIn("Do example.delete", folded.words)
        self.assertIn("Do example.create", folded.words)
        self.assertIn("Example notes", folded.words)

    def test_with_nothing_changed_every_action_is_folded_and_none_is_lost(self):
        page = self.page(groups=(Group("Example notes", (_row("example.note", None),)),))
        (folded,) = with_class(page, "section-fold")

        self.assertIn("Do example.note", folded.words)
        self.assertEqual(len(elements(page, "table")), 1)


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
