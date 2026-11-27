"""An insight must keep its next step when it reaches a human or machine queue."""

import json
from unittest.mock import patch

from django.urls import reverse

from hq.platform.application import cadence
from hq.platform.application.action_links import read_link
from hq.platform.application.asks import RUNNING, Standing
from hq.platform.application.cadence import ControllerSweepCommand
from hq.platform.application.dashboard import work_queue
from hq.platform.application.derivation_testing import uncached
from hq.platform.application.finding_model import Finding, opens
from hq.platform.application.finding_steps import ByHand, Do, Press, Why
from hq.platform.application.markup_testing import hrefs, select
from hq.platform.application.plugins import gather_attention
from hq.platform.core.testing import SignedInTestCase


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
        self.assertFalse(DashboardRefreshRequest.objects.exists())

    def test_a_queue_with_nothing_urgent_is_only_the_count(self):
        calm = [item for item in work_queue() if item["status"] != "serious"]
        with patch("hq.platform.application.dashboard.work_queue", return_value=calm):
            response = self.client.get("/")

        self.assertNotContains(response, "data-triage-routes")
        self.assertNotContains(response, "data-attention-item")
        self.assertEqual(response.context["action_queue_count"], len(calm))
        self.assertIn("/action-items/", hrefs(response))

    def test_a_card_on_the_dashboard_opens_its_whole_card(self):
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

        path, _, anchor = page.partition("#")
        self.assertEqual(path, "/action-items/")
        self.assertTrue(select(whole, "id", anchor))

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
