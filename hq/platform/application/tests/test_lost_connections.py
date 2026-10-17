"""A controller that reported connections and now reports none, and one that never had any."""

from django.test import TestCase

from hq.domains.control_plane.models import ProviderConnection

from ..connection_context import connections_context
from ..estate import cards
from ..projection import projection_scope
from ..report_testing import report_connections
from ..security import cli_principal
from .test_finding_fixes import steps_text
from .test_tailnet_posture import raised

RULE = "connections-lost"
CONTROLLER = "example-controller"


def report(*refs: str, controller_id: str = CONTROLLER) -> None:
    report_connections(
        [{"connection_ref": ref, "provider": "example"} for ref in refs],
        controller_id=controller_id,
    )


class ConnectionsLostTests(TestCase):
    def test_a_host_that_never_had_a_connection_raises_nothing(self):
        report()
        report()

        self.assertEqual(raised(RULE), [])
        self.assertFalse(ProviderConnection.objects.exists())

    def test_connections_that_vanish_in_one_report_are_serious(self):
        report("example-dns", "example-tunnel")
        report()

        (finding,) = raised(RULE)

        self.assertEqual(finding["severity"], "serious")
        self.assertEqual(finding["title"], f"The controller on {CONTROLLER} reports no connections")
        self.assertIn("2 connections", finding["explanation"])
        self.assertIn({"label": "Last reported", "value": "example-dns, example-tunnel"}, finding["evidence"])
        self.assertIn("systemctl start severino-hq-secrets.service", steps_text(finding))
        self.assertEqual(
            [remedy["capability"] for remedy in finding["remedies"]], ["infrastructure.controller.refresh"]
        )
        # Said once for the controller, not again for each connection.
        self.assertEqual(raised("connection-not-answering"), [])

    def test_the_rows_are_kept_and_dated_by_the_first_empty_report(self):
        report("example-dns")
        report()
        first = ProviderConnection.objects.get().unreported_at
        report()

        row = ProviderConnection.objects.get()
        self.assertIsNotNone(first)
        self.assertEqual(row.unreported_at, first)
        self.assertFalse(row.reachable)

    def test_it_stays_until_a_report_carries_a_connection_again(self):
        report("example-dns")
        report()
        report()

        (finding,) = raised(RULE)
        self.assertIn({"label": "Last reported", "value": "example-dns"}, finding["evidence"])

        report("example-dns")
        self.assertEqual(raised(RULE), [])
        row = ProviderConnection.objects.get()
        self.assertIsNone(row.unreported_at)
        self.assertTrue(row.reachable)

    def test_a_returning_report_replaces_what_was_kept(self):
        report("example-dns", "example-tunnel")
        report()
        report("example-dns")

        self.assertEqual(list(ProviderConnection.objects.values_list("connection_ref", flat=True)), ["example-dns"])
        self.assertEqual(raised(RULE), [])

    def test_one_connection_removed_of_several_is_not_a_loss(self):
        report("example-dns", "example-tunnel")
        report("example-dns")

        self.assertEqual(raised(RULE), [])
        self.assertFalse(ProviderConnection.objects.filter(connection_ref="example-tunnel").exists())

    def test_each_controller_answers_for_its_own(self):
        report("example-dns")
        report("example-edge", controller_id="example-other")
        report()
        report("example-edge", controller_id="example-other")

        (finding,) = raised(RULE)
        self.assertIn(CONTROLLER, finding["title"])


class UnreportedRowsTests(TestCase):
    def setUp(self):
        report("example-dns")
        report()

    def test_the_connections_page_says_no_longer_reported(self):
        context = connections_context(principal=cli_principal())

        (row,) = (row for row in context.rows if row.instance.connection_ref == "example-dns")
        self.assertEqual(row.lifecycle, "unreported")
        self.assertEqual(row.state_label, "No longer reported")
        self.assertTrue(row.state_line.startswith("Last reported "))
        self.assertIn((1, "no longer reported"), context.attention_states)

    def test_the_estate_counts_them_as_a_problem(self):
        with projection_scope():
            card = {card["id"]: card for card in cards()}["hq.estate.connections"]

        self.assertEqual(card["detail"], "example-dns no longer reported")
        self.assertEqual(card["status"], "attention")
