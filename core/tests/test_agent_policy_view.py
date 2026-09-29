"""The agents page marks a column off by the column, not by its label."""

from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from application.capability_policy import Cell, Column, Group, Row, Scope


def _cell(column: Column, *, dormant: bool) -> Cell:
    return Cell(
        field=f"rule:{column.scope}:{column.subject}:example.capability",
        scope=column.scope,
        column=column.label,
        rule=None,
        options=(),
        dormant=dormant,
    )


class DormantColumnTests(TestCase):
    def test_a_label_shared_by_two_columns_marks_only_the_dormant_one(self):
        surface = Column(Scope.SURFACE, "mcp", "Example", "MCP")
        agent = Column(Scope.AGENT, "example-agent", "Example", "API")
        groups = (
            Group(
                "Example group",
                (
                    Row(
                        "example.capability",
                        "Example capability",
                        "write",
                        "Write",
                        "Allow",
                        (_cell(surface, dormant=True), _cell(agent, dormant=False)),
                    ),
                ),
            ),
        )
        self.client.force_login(
            get_user_model().objects.create_user("op", password="x" * 20)
        )
        with patch(
            "application.capability_policy.matrix", return_value=((surface, agent), groups)
        ):
            response = self.client.get(reverse("agent_policy"))

        self.assertEqual(
            [head["dormant"] for head in response.context["column_heads"]], [True, False]
        )
        self.assertContains(response, '<span class="policy-off">off on this server</span>', count=1)
