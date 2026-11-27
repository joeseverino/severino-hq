"""A controller that reported connections and now reports none, and one that never had any."""

from django.test import TestCase

from ..connection_context import connections_context
from ..report_testing import report_connections
from ..security import cli_principal

CONTROLLER = "example-controller"


def report(*refs: str, controller_id: str = CONTROLLER) -> None:
    report_connections(
        [{"connection_ref": ref, "provider": "example"} for ref in refs],
        controller_id=controller_id,
    )


class UnreportedRowsTests(TestCase):
    def setUp(self):
        report("example-dns")
        report()

    def test_the_connections_page_says_no_longer_reported(self):
        context = connections_context(principal=cli_principal())

        (row,) = (row for row in context.rows if row.instance.connection_ref == "example-dns")
        self.assertTrue(row.connection.state_line)
        self.assertIn((1, "no longer reported"), context.attention_states)
