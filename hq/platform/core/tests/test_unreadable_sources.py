"""One source of a composed page that raises is unreadable in its own place, and the rest still renders."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from hq.platform.application import roles
from hq.platform.application.dashboard import dashboard_highlights, operating_snapshot, work_queue
from hq.platform.application.domains import Domain, host_domains
from hq.platform.application.plugin_testing import installed, sibling
from hq.platform.application.plugins import NavigationItem, PluginIntegration
from hq.platform.application.projection import projection_scope
from hq.platform.application.security import MCP_INTERFACE, agent_principal, cli_principal
from hq.platform.application.ui import DomainOverview, Insight, Kpi

LOGGER = "severino.composition"
EVENT = "composition.source.unreadable"


def broken():
    raise RuntimeError("stored readings no longer parse")


def contributor(name, *, origin="extension", attention=None, dashboard=None, overview=None):
    return Domain(
        id=f"example.{name}",
        label=f"Example {name}",
        origin=origin,
        navigation=(NavigationItem(f"Example {name}", "dashboard", "", 900),),
        integration=PluginIntegration(attention=attention, dashboard=dashboard, overview=overview),
    )


def urgent(title):
    return Insight("serious", "Example", title, "1", "Look into it.", key=title)


def cards(name, count=3):
    return tuple(
        {"id": f"example-{name}-{n}", "label": f"{name} reading {n}", "value": n, "url": "/"} for n in range(count)
    )


class UnreadableSourceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(username="unreadable-sources")

    def setUp(self):
        self.client.force_login(self.user)

    def composing(self, *domains):
        every = (*host_domains(), *domains)
        for module in ("domains", "dashboard"):
            patcher = patch(f"hq.platform.application.{module}.all_domains", return_value=every)
            patcher.start()
            self.addCleanup(patcher.stop)

    def assertLogsTheFailure(self, logs, source_id):
        (record,) = [record for record in logs.records if getattr(record, "event", None) == EVENT]
        self.assertIn(source_id, record.getMessage())
        self.assertIsNotNone(record.exc_info)

    def test_a_raising_host_attention_source_is_one_item_and_the_rest_of_the_queue_renders(self):
        self.assertTheQueueRendersBeside("host")

    def test_a_raising_extension_attention_source_is_one_item_and_the_rest_of_the_queue_renders(self):
        self.assertTheQueueRendersBeside("extension")

    def assertTheQueueRendersBeside(self, origin):
        self.composing(
            contributor("good", origin=origin, attention=lambda: (urgent("Good source item"),)),
            contributor("broken", origin=origin, attention=broken),
        )
        with self.assertLogs(LOGGER, "ERROR") as logs:
            home = self.client.get("/")
        self.assertLogsTheFailure(logs, "example.broken")
        items = self.client.get("/action-items/")
        snapshot = operating_snapshot(principal=cli_principal())

        self.assertEqual(home.status_code, 200)
        self.assertContains(home, "Good source item")
        self.assertContains(items, "Good source item")
        self.assertContains(items, "Example broken could not be read")
        labels = {item["label"]: item["status"] for item in snapshot["priority"]}
        self.assertEqual(labels["Good source item"], "serious")
        self.assertEqual(labels["Example broken could not be read"], "attention")

    def test_a_source_that_reads_again_is_asked_again_and_its_neighbors_are_not(self):
        good = Mock(return_value=(urgent("Good source item"),))
        failing = Mock(side_effect=RuntimeError("stored readings no longer parse"))
        self.composing(
            contributor("good", origin="host", attention=good),
            contributor("flaky", attention=failing),
        )

        for _ in range(2):
            with projection_scope(), self.assertLogs(LOGGER, "ERROR"):
                queued = [item["label"] for item in work_queue()]
            self.assertIn("Example flaky could not be read", queued)
        failing.side_effect = None
        failing.return_value = (urgent("Flaky source item"),)
        with projection_scope():
            queued = [item["label"] for item in work_queue()]

        self.assertIn("Flaky source item", queued)
        self.assertNotIn("Example flaky could not be read", queued)
        self.assertEqual(failing.call_count, 3)
        # The host's part did not compose the failure, so it stood throughout.
        good.assert_called_once()

    def test_a_raising_dashboard_source_is_an_unreadable_card_beside_the_good_ones(self):
        self.composing(
            contributor("good", dashboard=lambda: cards("good")),
            contributor("broken", dashboard=broken),
        )
        with self.assertLogs(LOGGER, "ERROR") as logs:
            home = self.client.get("/")
        self.assertLogsTheFailure(logs, "example.broken")
        snapshot = operating_snapshot(principal=cli_principal())

        self.assertEqual(home.status_code, 200)
        self.assertContains(home, "good reading 1")
        self.assertContains(home, "Unreadable")
        shown = {card["label"]: card["value"] for card in snapshot["cards"]}
        self.assertEqual(shown["good reading 1"], 1)
        self.assertEqual(shown["Example broken"], "Unreadable")

    def test_a_raising_overview_leaves_its_section_to_its_cards(self):
        overview = DomainOverview(
            "Example", "/example/", (Kpi("Good overview reading", 4), Kpi("Second", 5), Kpi("Third", 6))
        )
        self.composing(
            contributor("good", dashboard=lambda: cards("good"), overview=lambda: overview),
            contributor("broken", dashboard=lambda: cards("broken"), overview=broken),
        )
        with self.assertLogs(LOGGER, "ERROR") as logs:
            home = self.client.get("/")
        self.assertLogsTheFailure(logs, "example.broken")

        self.assertEqual(home.status_code, 200)
        self.assertContains(home, "Good overview reading")
        self.assertContains(home, "broken reading 1")
        sections = {section["id"]: section for section in dashboard_highlights(cli_principal())["highlights"]}
        self.assertIsNone(sections["example.broken"]["overview"])

    def test_a_malformed_attention_item_is_its_sources_failure_and_the_rest_renders(self):
        self.composing(
            contributor("good", attention=lambda: (urgent("Good source item"),)),
            contributor("broken", attention=lambda: (SimpleNamespace(status="urgent"),)),
        )
        with self.assertLogs(LOGGER, "ERROR") as logs:
            home = self.client.get("/")
        self.assertLogsTheFailure(logs, "example.broken")
        items = self.client.get("/action-items/")

        self.assertContains(home, "Good source item")
        self.assertContains(items, "Good source item")
        self.assertContains(items, "Example broken could not be read")

    def test_a_malformed_card_is_its_sources_failure_and_the_rest_renders(self):
        for name, malformed in (
            ("unknown", lambda: ({**cards("unknown")[0], "tint": "red"},)),
            ("twice", lambda: (cards("twice")[0], cards("twice")[0])),
        ):
            with self.subTest(name=name):
                self.composing(
                    contributor("good", dashboard=lambda: cards("good")), contributor(name, dashboard=malformed)
                )
                with self.assertLogs(LOGGER, "ERROR") as logs:
                    home = self.client.get("/")
                self.assertLogsTheFailure(logs, f"example.{name}")

                self.assertContains(home, "good reading 1")
                shown = {
                    card["label"]: card["value"] for card in operating_snapshot(principal=cli_principal())["cards"]
                }
                self.assertEqual(shown[f"Example {name}"], "Unreadable")


class StandInGateTests(TestCase):
    """A stand-in tells no reader more than its source would."""

    GATE = "example.gated.read"

    def setUp(self):
        manifest, contribution = sibling(
            identifier="example.gated",
            name="Gated",
            operator_capabilities=(self.GATE,),
            mcp_read_capabilities=(self.GATE,),
        )
        self.enterContext(installed((manifest, replace(contribution, attention=broken))))

    def shown_to(self, principal):
        with self.assertLogs(LOGGER, "ERROR"):
            return [item["label"] for item in operating_snapshot(principal=principal)["priority"]]

    def test_a_viewer_who_holds_the_extensions_reads_sees_it(self):
        viewer = get_user_model().objects.create_user(username="gated-viewer")
        roles.assign(viewer, roles.Role.VIEWER)
        self.client.force_login(viewer)

        with self.assertLogs(LOGGER, "ERROR"):
            self.assertContains(self.client.get("/action-items/"), "Gated could not be read")

    def test_an_agent_sees_it_only_if_its_grant_holds_the_extensions_reads(self):
        holding = agent_principal("example-agent", MCP_INTERFACE, frozenset({"read", self.GATE}))
        lacking = agent_principal("example-agent", MCP_INTERFACE, frozenset({"read"}))

        self.assertIn("Gated could not be read", self.shown_to(holding))
        self.assertNotIn("Gated could not be read", self.shown_to(lacking))
