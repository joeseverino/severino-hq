"""An insight must keep its next step when it reaches a human or machine queue."""

import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from hq.platform.application.dashboard import work_queue
from hq.platform.application.derivations import uncached
from hq.platform.application.plugins import gather_attention
from hq.platform.application.tests.markup import hrefs, select, with_class
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
            remedies=(ActionLink("remedy", "Inspect evidence", "read", "/example/evidence/", recommended=True),),
            verification=ActionLink("verify", "Recheck facts", "write", "/dashboard/glance/", method="POST"),
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
        self.source = patch("hq.platform.application.dashboard.domain_attention_items", return_value=entries)
        self.source.start()
        self.addCleanup(self.source.stop)

    def test_transport_preserves_action_workflow_and_domain_severity(self):
        queue = work_queue()
        json.dumps(queue)
        self.assertEqual([item["status"] for item in queue], ["serious", "attention"])
        self.assertIsNone(queue[0]["workflow"])
        self.assertEqual(queue[1]["action"], "Reconcile the bill")
        self.assertEqual(queue[1]["workflow"]["steps"][-1]["actions"][0]["method"], "POST")

    def test_queue_has_safe_actions_and_dashboard_keeps_domain_overviews_first(self):
        from hq.domains.control_plane.models import DashboardRefreshRequest

        for path in ("/", "/action-items/"):
            with self.subTest(path=path):
                response = self.client.get(path)
                if path == "/action-items/":
                    self.assertContains(response, "Reconcile the bill")
                    self.assertEqual(hrefs(response).count("/example/evidence/"), 1)
                    # The verification is a write: posted with the token, never a link.
                    self.assertTrue(select(response, "formaction", "/dashboard/glance/"))
                    self.assertNotIn("/dashboard/glance/", hrefs(response))
                    self.assertTrue(select(response, "name", "csrfmiddlewaretoken"))
                else:
                    # What is serious is read on the dashboard itself; the rest is a count.
                    serious = [item for item in work_queue() if item["status"] == "serious"]
                    self.assertContains(response, "data-attention-item", count=len(serious))
                    self.assertIn("/action-items/", hrefs(response))
                self.assertNotContains(response, "An interesting observation")
        self.assertFalse(DashboardRefreshRequest.objects.exists())

    def test_a_queue_with_something_urgent_in_it_is_triaged_on_the_dashboard(self):
        self.assertContains(self.client.get("/"), "data-triage-routes", count=1)

    def test_a_queue_with_nothing_urgent_is_only_the_count(self):
        calm = [item for item in work_queue() if item["status"] != "serious"]
        with patch("hq.platform.application.dashboard.work_queue", return_value=calm):
            response = self.client.get("/")

        self.assertNotContains(response, "data-triage-routes")
        self.assertNotContains(response, "data-attention-item")
        self.assertEqual(response.context["action_queue_count"], len(calm))
        self.assertIn("/action-items/", hrefs(response))

    def test_a_card_on_the_dashboard_opens_its_whole_card_and_shows_how_old_it_is(self):
        from django.utils import timezone

        from hq.platform.application.item_help import commands

        urgent = Insight(
            "serious",
            "Review",
            "A run is failing",
            "1",
            "Two checks fail.",
            key="example-failing",
            workflow=commands("example-failing", (("See the failing runs", "example-tool runs --failing"),)),
            observed_at=timezone.now(),
        )
        entries = gather_attention((("example.utility", "Example", lambda: (urgent,)),))
        with patch("hq.platform.application.dashboard.domain_attention_items", return_value=entries):
            dashboard = self.client.get("/")
            (page,) = [title.attrs["href"] for title in with_class(dashboard, "attention-title")]
            whole = self.client.get(page)

        self.assertContains(dashboard, "data-reading-age")
        self.assertNotContains(dashboard, "runs --failing")
        path, _, anchor = page.partition("#")
        self.assertEqual(path, "/action-items/")
        self.assertTrue(select(whole, "id", anchor))
        self.assertContains(whole, "runs --failing")

    def test_the_dashboard_asks_for_what_its_queue_is_drawn_from_only_when_it_is_due(self):
        from datetime import timedelta

        from django.urls import reverse
        from django.utils import timezone

        from hq.domains.control_plane.models import ProviderInventory

        ask = f'action="{reverse("control_plane:visit")}"'
        for age, asks in ((timedelta(0), False), (timedelta(hours=3), True)):
            with self.subTest(age=age):
                ProviderInventory.objects.update_or_create(
                    kind="github.repository",
                    defaults={"records": [], "reachable": True, "observed_at": timezone.now() - age},
                )
                ProviderInventory.objects.filter(kind="github.repository").update(updated_at=timezone.now() - age)

                self.assertEqual(ask in self.client.get("/").content.decode(), asks)

    def test_a_long_queue_stays_a_count_on_the_dashboard(self):
        items = [{**work_queue()[0], "key": f"example:{i}", "label": f"Decision {i}"} for i in range(4)]
        with patch("hq.platform.application.dashboard.work_queue", return_value=items):
            response = self.client.get("/")

        self.assertNotContains(response, "data-attention-item")
        self.assertEqual(response.context["action_queue_count"], 4)

    def test_search_includes_the_recommended_action(self):
        response = self.client.get("/action-items/", {"q": "Reconcile the bill"})
        self.assertContains(response, "Check the reading")
        self.assertNotContains(response, "A service needs you")

    def test_dashboard_counts_share_one_read_state_query_at_any_queue_size(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from hq.platform.application.action_items import set_aside

        for size in (4, 40):
            items = [{**work_queue()[0], "key": f"example:{i}", "label": f"Decision {i}"} for i in range(size)]
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
        self.assertEqual((response.context["action_queue_count"], response.context["aside_count"]), (0, 2))
        self.assertContains(response, "2 dismissed")

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
        with (
            patch(
                "hq.platform.core.dashboard_views.dashboard_highlights",
                return_value={"highlights": [], "compact": []},
            ),
            patch("hq.platform.application.calendar.calendar_sources", return_value=()),
        ):
            response = self.client.get("/")
        self.assertFalse(select(response, "id", "dashboard-calendar"))

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
        # A domain's own calendar stays on its pages; the dashboard shows the composed month.
        self.assertNotContains(response, "Example activity")
        self.assertEqual(len(select(response, "id", "dashboard-calendar")), 1)


class EmptyQueueClaimTests(TestCase):
    """An empty queue is "Nothing needs you" only once something has been read."""

    def setUp(self):
        self.client.force_login(get_user_model().objects.create_user(username="fresh-install"))
        empty = patch("hq.platform.application.dashboard.domain_attention_items", return_value=())
        empty.start()
        self.addCleanup(empty.stop)

    def status(self, seen_at):
        from hq.platform.application.cadence import ControllerStanding

        with patch(
            "hq.platform.core.dashboard_views.controller_standing",
            return_value=ControllerStanding(seen_at=seen_at, silent=False),
        ):
            return self.client.get("/")

    def test_a_controller_that_never_ran_has_cleared_nothing(self):
        response = self.status(None)

        self.assertContains(response, "HQ has read nothing yet.")
        self.assertNotContains(response, "Nothing needs you.")

    def test_once_read_an_empty_queue_says_so(self):
        from django.utils import timezone

        response = self.status(timezone.now())

        self.assertContains(response, "Nothing needs you.")
