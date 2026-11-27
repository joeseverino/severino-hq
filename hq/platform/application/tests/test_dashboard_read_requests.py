"""The dashboard's ask for a fresh reading: a read request for each panel, naming it."""

from django.test import TestCase

from hq.domains.control_plane.models import (
    DashboardConfiguration,
    DashboardMachine,
)
from hq.platform.core.bindings import audit_operation
from hq.platform.core.models import AuditLog
from hq.platform.core.testing import connection, declare

from ..glance_refresh import DashboardRefreshCommand, request_dashboard_refresh
from ..security import cli_principal

# Written out, so the module imports at a ref that files the ask elsewhere.
READ_REQUEST = "Read request"


class DashboardAskTests(TestCase):
    def setUp(self):
        self.machine = declare(
            "app-server", "machine", {"name": "app", "addresses": ["192.0.2.10"]}, status={"kept": True}
        )
        connection("ssh", "app-ssh", controller_id="app-server", endpoint="192.0.2.10", reaches=["app"])
        DashboardConfiguration.objects.create(weather_point="41.0000,-87.0000", weather_label="Example town")
        DashboardMachine.objects.create(machine=self.machine)

    def ask(self) -> list[AuditLog]:
        principal = cli_principal()
        with audit_operation(operation="dashboard.panels.refresh", principal=principal):
            request_dashboard_refresh(DashboardRefreshCommand(), principal=principal)
        return list(AuditLog.objects.filter(metadata__operation="dashboard.panels.refresh"))

    def test_it_is_filed_as_a_read_request_naming_each_panel(self):
        rows = self.ask()

        self.assertEqual({row.object_type for row in rows}, {READ_REQUEST})
        # The row's message and the record it names both call a panel by its head's name.
        said = " ".join(f"{row.object_repr} {row.message}" for row in rows)
        self.assertIn("app", said)
        self.assertIn("Example town", said)
        self.assertNotIn(f"machine-{self.machine.pk}", said)
