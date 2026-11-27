"""One source of a composed page that raises is unreadable in its own place, and the rest still renders."""

from unittest.mock import patch

from hq.platform.application.card_items import Counted
from hq.platform.application.dashboard import operating_snapshot
from hq.platform.application.domains import Domain, host_domains
from hq.platform.application.plugins import NavigationItem, PluginIntegration
from hq.platform.application.security import cli_principal
from hq.platform.core.testing import SignedInTestCase

LOGGER = "severino.composition"
EVENT = "composition.source.unreadable"


def broken():
    raise RuntimeError("stored readings no longer parse")


def contributor(name, *, dashboard):
    return Domain(
        id=f"example.{name}",
        label=f"Example {name}",
        origin="extension",
        navigation=(NavigationItem(f"Example {name}", "dashboard", "", 900),),
        integration=PluginIntegration(dashboard=dashboard),
    )


def cards(name, count=3):
    return tuple(
        {
            "id": f"example-{name}-{n}",
            "label": Counted(n, f"{name} reading {n}", f"{name} readings {n}"),
            "url": "/",
        }
        for n in range(count)
    )


class UnreadableSourceTests(SignedInTestCase):
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
        shown = {card["label"]: card["value"] for card in snapshot["cards"]}
        self.assertEqual(shown["good reading 1"], 1)
        self.assertEqual(shown["Example broken"], "Unreadable")
