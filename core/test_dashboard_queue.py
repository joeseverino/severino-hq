"""An insight must keep its next step when it reaches a human or machine queue."""

import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from application.dashboard import work_queue
from application.plugins import gather_attention
from application.ui import Insight
from application.workflow_contracts import ActionLink
from application.workflows import claim_resolution_plan


class DashboardQueueTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(username="queue-review")

    def setUp(self):
        self.client.force_login(self.user)
        plan = claim_resolution_plan(
            namespace="example.utility",
            rule="changed",
            subject="account:one",
            scope="",
            investigations=(
                ActionLink("inspect", "Inspect evidence", "read", "/example/evidence/"),
            ),
            offers=(),
            remedies=(),
            verification=ActionLink(
                "verify", "Recheck facts", "write", "/dashboard/glance/", method="POST"
            ),
        )
        insights = (
            Insight(
                "attention",
                "Review",
                "Check the reading",
                "1",
                "Supporting facts.",
                action="Reconcile the bill",
                workflow=plan,
            ),
            Insight(
                "serious",
                "Review",
                "A service needs you",
                "1",
                "Inspect the dependency.",
                url="/infrastructure/services/",
            ),
            Insight(
                "neutral",
                "Context",
                "An interesting observation",
                "1",
                "No decision needed.",
            ),
        )
        entries = gather_attention((("example.utility", "Example", lambda: insights),))
        self.source = patch(
            "application.dashboard.domain_attention_items", return_value=entries
        )
        self.source.start()
        self.addCleanup(self.source.stop)

    def test_transport_preserves_action_workflow_and_domain_severity(self):
        queue = work_queue()
        json.dumps(queue)
        self.assertEqual([item["status"] for item in queue], ["serious", "attention"])
        self.assertIsNone(queue[0]["workflow"])
        self.assertEqual(queue[1]["action"], "Reconcile the bill")
        self.assertEqual(
            queue[1]["workflow"]["steps"][-1]["actions"][0]["method"], "POST"
        )

    def test_full_queue_renders_workflows_while_dashboard_stays_compact(self):
        from control_plane.models import DashboardRefreshRequest

        with patch("core.views.get_dashboard_state", return_value=([], 0)):
            for path in ("/", "/action-items/"):
                with self.subTest(path=path):
                    response = self.client.get(path)
                    if path == "/action-items/":
                        self.assertContains(response, "Reconcile the bill")
                        self.assertContains(response, "Inspect evidence")
                        self.assertContains(response, "Recheck facts")
                        self.assertContains(
                            response, 'action="/dashboard/glance/" method="post"'
                        )
                        self.assertContains(response, 'name="csrfmiddlewaretoken"')
                    else:
                        self.assertNotContains(response, "Resolution workflow")
                        self.assertNotContains(response, "Reconcile the bill")
                    self.assertContains(response, "Serious</span>")
                    self.assertNotContains(response, "An interesting observation")
                    self.assertNotContains(response, 'href=""')
                    self.assertTemplateUsed(response, "partials/_work_queue.html")
        self.assertFalse(DashboardRefreshRequest.objects.exists())

    def test_search_includes_the_recommended_action(self):
        response = self.client.get("/action-items/", {"q": "Reconcile the bill"})
        self.assertContains(response, "Check the reading")
        self.assertNotContains(response, "A service needs you")

    def test_dashboard_places_metrics_before_compact_queue(self):
        with (
            patch("core.views.get_dashboard_state", return_value=([], 0)),
            patch(
                "core.views.dashboard_highlights",
                return_value={
                    "highlights": [],
                    "compact": [{"label": "Example count", "value": 3, "url": "/"}],
                },
            ),
        ):
            html = self.client.get("/").content.decode()
        self.assertLess(
            html.index('aria-label="Across HQ"'), html.index("Needs attention")
        )

    def test_anonymous_reader_cannot_open_either_queue(self):
        self.client.logout()
        for path in ("/", "/action-items/"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 302)
            self.assertIn("/accounts/login/", response.url)

    def test_contributor_visuals_are_visible_and_calendars_have_distinct_ids(self):
        highlights = [
            {
                "id": f"example.section{number}",
                "label": f"Example {number}",
                "overview": {
                    "url": "/example/",
                    "charts": [{"title": "Example movement", "empty": True}],
                    "calendars": [{"title": "Example activity", "weeks": []}],
                },
            }
            for number in (1, 2)
        ]
        with patch(
            "core.views.dashboard_highlights",
            return_value={"highlights": highlights, "compact": []},
        ):
            response = self.client.get("/")
        self.assertContains(response, "Example movement", count=2)
        self.assertContains(response, 'class="dashboard-patterns"', count=2)
        self.assertNotContains(response, '<details class="highlight-patterns">')
        self.assertNotContains(response, 'aria-label="Across HQ"')
        for number in (1, 2):
            self.assertContains(
                response, f'id="dashboard-calendar-example.section{number}"', count=1
            )
