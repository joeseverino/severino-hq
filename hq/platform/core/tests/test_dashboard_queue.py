"""An insight must keep its next step when it reaches a human or machine queue."""

import json
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from hq.platform.application import cadence
from hq.platform.application.action_links import read_link
from hq.platform.application.asks import RUNNING, Standing
from hq.platform.application.cadence import ControllerSweepCommand
from hq.platform.application.dashboard import work_queue
from hq.platform.application.derivation_testing import uncached
from hq.platform.application.finding_model import Finding, opens
from hq.platform.application.finding_steps import ByHand, Do, Press, Why
from hq.platform.application.markup_testing import hrefs, select, with_class, within
from hq.platform.application.plugins import gather_attention
from hq.platform.application.tests.test_decisions import row_hrefs
from hq.platform.application.ui import DomainOverview
from hq.platform.core.testing import SignedInTestCase, operator
from hq_sdk.testing import plain


class DashboardQueueTests(SignedInTestCase):
    def setUp(self):
        super().setUp()
        insights = (
            Finding(
                rule="example-reading",
                subject="one",
                severity="attention",
                title="Check the reading",
                explanation="Supporting facts.",
                primary=opens("Reconcile the bill", ""),
                steps=(Press(read_link("remedy", "Inspect evidence", "/example/evidence/", recommended=True)),),
                read_now=ControllerSweepCommand(kinds=("github.repository",)),
            ),
            Finding(
                rule="example-service",
                subject="",
                severity="serious",
                title="A service needs you",
                explanation="Inspect the dependency.",
                primary=opens("", "/infrastructure/services/"),
            ),
            Finding(
                rule="example-context",
                subject="",
                severity="neutral",
                title="An interesting observation",
                explanation="No decision needed.",
            ),
        )
        entries = gather_attention((("example.utility", "Example", lambda: insights, ()),))
        self.source = patch("hq.platform.application.dashboard.domain_attention_items", return_value=entries)
        self.source.start()
        self.addCleanup(self.source.stop)

    def test_transport_preserves_action_steps_and_domain_severity(self):
        queue = work_queue()
        json.dumps(queue)
        self.assertEqual([item["status"] for item in queue], ["serious", "attention"])
        self.assertEqual(queue[0]["steps"], [])
        self.assertEqual(queue[1]["action"], "Reconcile the bill")
        self.assertEqual(queue[1]["steps"][0]["link"]["url"], "/example/evidence/")
        self.assertEqual(queue[1]["read_now"], {"kinds": ("github.repository",)})

    def test_queue_has_safe_actions_and_dashboard_keeps_domain_overviews_first(self):
        from hq.domains.control_plane.models import DashboardRefreshRequest

        for path in ("/", "/action-items/"):
            with self.subTest(path=path):
                response = self.client.get(path)
                if path == "/action-items/":
                    self.assertContains(response, "Reconcile the bill")
                    self.assertEqual(row_hrefs(response).count("/example/evidence/"), 1)
                    # The queue's one Refresh is a write: posted with the token, never a link.
                    read = reverse("control_plane:read_now")
                    (refresh,) = [
                        each for each in select(response, "formaction") if each.attrs["formaction"].startswith(read)
                    ]
                    self.assertIn("kind=github.repository", refresh.attrs["formaction"])
                    self.assertFalse([href for href in hrefs(response) if href.startswith(read)])
                    self.assertTrue(select(response, "name", "csrfmiddlewaretoken"))
                else:
                    # What is serious is read on the dashboard itself; the rest is a count.
                    serious = [item for item in work_queue() if item["status"] == "serious"]
                    self.assertContains(response, "data-attention-item", count=len(serious))
                    self.assertIn("/action-items/", hrefs(response))
                self.assertNotContains(response, "An interesting observation")
        self.assertFalse(DashboardRefreshRequest.objects.exists())

    def test_the_urgent_cards_are_named_without_a_heading_row(self):
        response = self.client.get("/")

        (card,) = select(response, "data-triage-routes")
        self.assertEqual(card.attrs.get("aria-label"), "Needs you")
        inside = within(response, "data-triage-routes")
        # A row's urgency opens what raised it in a popover, titled as a dialog is; that is no heading row.
        headings = [
            element
            for element in inside
            if element.tag in {"h1", "h2", "h3", "h4", "h5", "h6"}
            and not any("popover" in ancestor.attrs for ancestor in element.ancestors)
        ]
        self.assertEqual(headings, [])

    def test_the_urgent_card_names_a_rows_urgency_to_a_screen_reader_alone(self):
        # The card is in the urgent tone, so a row's word for it is no second mark; the queue page shows it.
        for path, shown in (("/", False), ("/action-items/", True)):
            with self.subTest(path=path):
                marks = [
                    mark for mark in with_class(self.client.get(path), "is-mark") if "tone-serious" in mark.classes
                ]
                self.assertTrue(marks)
                self.assertEqual(any("visually-hidden" not in mark.classes for mark in marks), shown)

    def test_a_queue_with_nothing_urgent_is_only_the_count(self):
        calm = [item for item in work_queue() if item["status"] != "serious"]
        with patch("hq.platform.application.dashboard.work_queue", return_value=calm):
            response = self.client.get("/")

        self.assertNotContains(response, "data-triage-routes")
        self.assertNotContains(response, "data-attention-item")
        self.assertEqual(response.context["action_queue_count"], len(calm))
        self.assertIn("/action-items/", hrefs(response))

    def test_a_card_on_the_dashboard_opens_its_whole_card_and_shows_how_old_it_is(self):
        from datetime import timedelta

        from django.utils import timezone

        urgent = Finding(
            rule="example-failing",
            subject="",
            severity="serious",
            title="A run is failing",
            explanation="Two checks fail.",
            steps=(ByHand("See the failing runs", "example-tool runs --failing", Why.NOT_GRANTED),),
            observed_at=timezone.now() - timedelta(days=30),
            reads="github.repository",
        )
        entries = gather_attention((("example.utility", "Example", lambda: (urgent,), ()),))
        with patch("hq.platform.application.dashboard.domain_attention_items", return_value=entries):
            dashboard = self.client.get("/")
            (page,) = [title.attrs["href"] for title in select(dashboard, "data-attention-title")]
            whole = self.client.get(page)

        self.assertContains(dashboard, "data-reading-age")
        self.assertContains(dashboard, "read every")
        self.assertContains(dashboard, "runs --failing")
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
        # A controller that reached this HQ a moment ago, to take what is asked.
        cadence.note_controller()
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

    def test_a_held_request_is_read_in_place_whatever_else_is_open(self):
        from django.urls import reverse

        from hq.domains.control_plane.models import CapabilityRule
        from hq.platform.application.attention import waiting_for_approval
        from hq.platform.application.capabilities import execute_capability
        from hq.platform.application.security import Capability, Principal
        from hq.platform.application.tests.writers import keyed, runbook

        CapabilityRule.objects.create(subject="example-agent", capability="documentation.sync", rule="approve")
        agent = Principal("example-agent", "mcp", frozenset({Capability.READ, Capability.SYNC_DOCUMENTATION}))
        asked = execute_capability("documentation.sync", keyed({"manifest": [runbook("rb-example")]}), principal=agent)
        held = asked["approval"]["id"]
        approve = reverse("control_plane:approval_decide", kwargs={"approval_id": held, "decision": "approve"})

        def dashboard(findings, reader=None):
            serious = tuple(
                Finding(
                    rule="example",
                    subject=str(n),
                    severity="serious",
                    title=f"Example finding {n}",
                    explanation="Inspect it.",
                    steps=(Do("Look into it."),),
                )
                for n in range(findings)
            )
            entries = gather_attention(
                (
                    ("example.utility", "Example", lambda: serious, ()),
                    ("example.approvals", "Waiting for approval", waiting_for_approval, ()),
                )
            )
            # The queue is replaced, not written: derive with the store bypassed.
            with patch("hq.platform.application.dashboard.domain_attention_items", return_value=entries), uncached():
                if reader is None:
                    return self.client.get("/")
                with patch("hq.platform.core.dashboard_views.web_principal", return_value=reader):
                    return self.client.get("/")

        # Beside more findings than fit, the held request is read in place with its buttons;
        # the findings stay behind the count.
        crowded = dashboard(5)
        self.assertEqual(len(select(crowded, "data-attention-item")), 1)
        self.assertTrue(select(crowded, "formaction", approve))
        self.assertNotContains(crowded, "Example finding")
        self.assertIn("/action-items/", hrefs(crowded))

        # A reader who may not see another caller's held request is not shown it in place.
        stranger = Principal("example-other", "mcp", frozenset({Capability.READ}))
        hidden = dashboard(5, reader=stranger)
        self.assertFalse(select(hidden, "formaction", approve))
        self.assertNotContains(hidden, "data-attention-item")

        # A short queue is read whole, the held request first.
        short = dashboard(2)
        rows = select(short, "data-attention-item")
        self.assertEqual(len(rows), 3)
        (button,) = select(short, "formaction", approve)
        self.assertTrue(button.inside(rows[0]))
        self.assertContains(short, "Example finding 1")

    def test_a_row_hq_is_acting_on_is_asked_again_until_it_ends(self):
        moving = Finding(
            rule="example-moving",
            subject="",
            severity="attention",
            title="An example update under way",
            explanation="An example update is ready.",
            primary=opens("Open the update", "/example/update/"),
            under_way=Standing(RUNNING, "Previewing: Check the example", url="/jobs/runs/example/"),
        )
        entries = gather_attention((("example.utility", "Example", lambda: (moving,), ()),))
        with patch("hq.platform.application.dashboard.domain_attention_items", return_value=entries), uncached():
            response = self.client.get("/action-items/")

        (row,) = select(response, "data-fragment-poll")
        self.assertIn("data-attention-item", row.attrs)
        # It says where its run stands in the run's own words, its state linked to the run.
        (run,) = select(response, "href", "/jobs/runs/example/")
        self.assertTrue(run.inside(row))
        self.assertEqual(run.words, "Running")
        self.assertIn("Previewing: Check the example", row.words)
        # Asked again at its own address: the row is answered from the page by its id.
        self.assertTrue(row.attrs.get("id"))

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
        self.assertIn("2 dismissed", plain(response.content))

    def test_empty_filtered_queue_offers_a_way_back(self):
        response = self.client.get("/action-items/", {"q": "not an existing item"})
        self.assertContains(response, "Nothing matches.")
        self.assertContains(response, "Clear filters")
        self.assertNotContains(response, "Nothing needs you.")

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
        operator(self.client)
        empty = patch("hq.platform.application.dashboard.domain_attention_items", return_value=())
        empty.start()
        self.addCleanup(empty.stop)

    def status(self, seen_at):
        from hq.platform.application.cadence import WorkerStanding

        with patch(
            "hq.platform.core.dashboard_views.controller_standing",
            return_value=WorkerStanding(seen_at=seen_at, silent=False),
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
