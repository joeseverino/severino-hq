"""An insight must keep its next step when it reaches a human or machine queue."""

import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from hq.platform.application.dashboard import work_queue
from hq.platform.application.derivations import uncached
from hq.platform.application.plugins import gather_attention
from hq.platform.application.ui import DomainOverview, Insight
from hq.platform.application.workflow_contracts import ActionLink
from hq.platform.application.workflows import claim_resolution_plan


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
            remedies=(
                ActionLink("remedy", "Inspect evidence", "read", "/example/evidence/", recommended=True),
            ),
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
            "hq.platform.application.dashboard.domain_attention_items", return_value=entries
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

    def test_queue_has_safe_actions_and_dashboard_keeps_domain_overviews_first(self):
        from hq.domains.control_plane.models import DashboardRefreshRequest

        with patch("hq.domains.contacts.d1.query", side_effect=AssertionError("a page render called D1")):
            for path in ("/", "/action-items/"):
                with self.subTest(path=path):
                    response = self.client.get(path)
                    if path == "/action-items/":
                        self.assertContains(response, "Reconcile the bill")
                        self.assertContains(response, "Inspect evidence", count=1)
                        self.assertContains(response, "Recheck facts")
                        self.assertContains(response, 'formaction="/dashboard/glance/"')
                        self.assertContains(response, 'form="hq-post"')
                        self.assertContains(response, 'name="csrfmiddlewaretoken"')
                        self.assertContains(response, "Urgent</span>")
                    else:
                        # A queue this short is read on the dashboard itself.
                        self.assertContains(response, "data-attention-item", count=2)
                        self.assertContains(response, "Inspect evidence", count=1)
                        self.assertContains(response, 'href="/action-items/"')
                    self.assertTemplateUsed(response, "partials/_work_queue.html")
                    self.assertNotContains(response, "An interesting observation")
                    self.assertNotContains(response, 'href=""')
        self.assertFalse(DashboardRefreshRequest.objects.exists())

    def test_a_long_queue_stays_a_count_on_the_dashboard(self):
        items = [{**work_queue()[0], "key": f"example:{i}", "label": f"Decision {i}"} for i in range(4)]
        with patch("hq.platform.application.dashboard.work_queue", return_value=items):
            response = self.client.get("/")

        self.assertNotContains(response, "data-attention-item")
        self.assertContains(response, "4 things need you")

    def test_search_includes_the_recommended_action(self):
        response = self.client.get("/action-items/", {"q": "Reconcile the bill"})
        self.assertContains(response, "Check the reading")
        self.assertNotContains(response, "A service needs you")

    def test_dashboard_counts_share_one_read_state_query_at_any_queue_size(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from hq.platform.application.action_items import set_aside

        for size in (4, 40):
            items = [{**work_queue()[0], "key": f"example:{i}", "label": f"Decision {i}"}
                     for i in range(size)]
            set_aside(self.user, [items[0]["key"]], aside=True, current=items)
            # The queue is replaced, not written: derive with the store bypassed.
            with (
                patch("hq.platform.application.dashboard.work_queue", return_value=items),
                uncached(),
                CaptureQueriesContext(connection) as queries,
            ):
                response = self.client.get("/")
            self.assertEqual(response.context["action_queue_count"], size - 1)
            self.assertEqual(response.context["profile_action_count"], size - 1)
            self.assertEqual(response.context["aside_count"], 1)
            self.assertEqual(sum("core_actionitemread" in query["sql"] for query in queries), 1)

    def test_set_aside_does_not_claim_resolved(self):
        from hq.platform.application.action_items import set_aside

        items = work_queue()
        set_aside(self.user, [item["key"] for item in items], aside=True, current=items)
        response = self.client.get("/")
        self.assertContains(response, "2 dismissed")
        self.assertNotContains(response, "All clear")

    def test_empty_filtered_queue_offers_a_way_back(self):
        response = self.client.get("/action-items/", {"q": "not an existing item"})
        self.assertContains(response, "Nothing matches these filters.")
        self.assertContains(response, "Clear filters")
        self.assertNotContains(response, "Nothing needs you.")

    def test_anonymous_reader_cannot_open_either_queue(self):
        self.client.logout()
        for path in ("/", "/action-items/"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 302)
            self.assertIn("/accounts/login/", response.url)

    def test_an_empty_month_takes_no_space(self):
        # Nothing contributes, the calendar's sources included, so the month is
        # empty whatever is installed.
        with patch(
            "hq.platform.core.dashboard_views.dashboard_highlights",
            return_value={"highlights": [], "compact": []},
        ), patch("hq.platform.application.calendar.calendar_sources", return_value=()):
            response = self.client.get("/")
        self.assertNotContains(response, 'id="dashboard-calendar"')
        self.assertNotContains(response, 'class="dashboard-patterns"')

    def test_each_domain_leads_with_its_chart_and_the_month_is_composed_once(self):
        from django.utils import timezone

        from hq.domains.calendars.models import Entry

        Entry.objects.create(title="Example visit", starts_on=timezone.localdate())
        highlights = [
            {
                "id": f"example.section{number}",
                "label": f"Example {number}",
                "overview": DomainOverview(
                    "Example",
                    "/example/",
                    (),
                    charts=({"title": "Example movement", "empty": True},),
                    calendars=({"title": "Example activity", "weeks": []},),
                ),
            }
            for number in (1, 2)
        ]
        with patch(
            "hq.platform.core.dashboard_views.dashboard_highlights",
            return_value={"highlights": highlights, "compact": []},
        ):
            response = self.client.get("/")
        self.assertContains(response, "Example movement", count=2)
        self.assertContains(response, 'class="dashboard-patterns"', count=1)
        self.assertNotContains(response, '<details class="highlight-patterns">')
        self.assertNotContains(response, 'aria-label="Across HQ"')
        # A domain's own calendar stays on its pages; the dashboard shows the composed month.
        self.assertNotContains(response, "Example activity")
        self.assertContains(response, 'id="dashboard-calendar"', count=1)
