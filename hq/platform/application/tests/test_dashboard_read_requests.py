"""The dashboard's ask for a fresh reading: a read request, naming its panels."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from hq.domains.control_plane.models import (
    DashboardConfiguration,
    DashboardMachine,
    ManagedResource,
    ProviderConnection,
)
from hq.platform.core.audit import audit_operation
from hq.platform.core.models import AuditLog

from ..glance import DashboardRefreshCommand, request_dashboard_refresh
from ..security import cli_principal

# Written out, so the module imports at a ref that files the ask elsewhere.
READ_REQUEST = "Read request"


class DashboardAskTests(TestCase):
    def setUp(self):
        self.machine = ManagedResource.objects.create(
            key="app-server", kind="machine", spec={"name": "app", "addresses": ["192.0.2.10"]}, status={"kept": True}
        )
        ProviderConnection.objects.create(
            connection_ref="app-ssh",
            controller_id="app-server",
            provider="ssh",
            endpoint="192.0.2.10",
            reaches=["app"],
            observed_at=timezone.now(),
        )
        DashboardConfiguration.objects.create(weather_point="41.0000,-87.0000", weather_label="Example town")
        DashboardMachine.objects.create(machine=self.machine)

    def ask(self) -> AuditLog:
        principal = cli_principal()
        with audit_operation(operation="dashboard.panels.refresh", principal=principal):
            request_dashboard_refresh(DashboardRefreshCommand(), principal=principal)
        return AuditLog.objects.get(metadata__has_key="panels")

    def test_it_is_filed_as_a_read_request_naming_each_panel(self):
        row = self.ask()

        self.assertEqual(row.object_type, READ_REQUEST)
        self.assertIn("app", row.message)
        self.assertIn("Example town", row.message)
        self.assertNotIn(f"machine-{self.machine.pk}", row.message)

    def test_the_log_leaves_it_out_until_read_requests_are_shown(self):
        self.client.force_login(get_user_model().objects.create_superuser("owner", "owner@example.test", "x" * 14))
        row = self.ask()
        # An ask the dashboard filed under its own name before, as older rows are.
        older = AuditLog.objects.create(
            action=AuditLog.Action.CREATED, object_type="Dashboard", message="Asked", metadata={"panels": ["weather"]}
        )

        def shown(**params):
            page = self.client.get(reverse("core:audit_list"), params)
            return {event.pk for line in page.context["events"] for event in line.events}

        self.assertFalse({row.pk, older.pk} & shown())
        self.assertLessEqual({row.pk, older.pk}, shown(reads=1))
