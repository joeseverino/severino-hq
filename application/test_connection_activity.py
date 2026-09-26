"""Each connection's last activity: stored on the event, read in bounded queries."""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.db import connection as db_connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from control_plane.models import ManagedResource, OperationRequest, ProviderConnection
from core.audit import CONNECTION_AUDIT_TYPE
from core.models import AuditLog

from .inventory import record_connections
from .security import cli_principal


class ConnectionEventTests(TestCase):
    def test_a_probe_is_a_routine_event_on_its_connection(self):
        record_connections(
            [{"connection_ref": "example-dns", "provider": "cloudflare_dns", "ok": True}],
            principal=cli_principal(),
            controller_id="example-controller",
        )

        event = AuditLog.objects.get(connection="example-dns")
        self.assertEqual(event.action, AuditLog.Action.OBSERVED)
        self.assertEqual(event.object_type, CONNECTION_AUDIT_TYPE)
        self.assertIsNone(event.user)
        self.assertEqual(event.summary, "Probed, reachable")

    def test_an_unchanged_probe_writes_nothing_and_a_change_is_recorded(self):
        def sweep(ok):
            record_connections(
                [{"connection_ref": "example-dns", "provider": "cloudflare_dns", "ok": ok}],
                principal=cli_principal(),
                controller_id="example-controller",
            )

        sweep(True)
        sweep(True)
        sweep(True)
        self.assertEqual(AuditLog.objects.filter(connection="example-dns").count(), 1)

        sweep(False)
        latest = AuditLog.objects.filter(connection="example-dns").first()
        self.assertEqual(AuditLog.objects.filter(connection="example-dns").count(), 2)
        self.assertEqual(latest.summary, "Probed, unreachable")

    def test_starting_to_manage_is_a_kept_settings_change(self):
        def sweep(manages):
            record_connections(
                [{"connection_ref": "example-dns", "provider": "cloudflare_dns",
                  "ok": True, "manages": manages}],
                principal=cli_principal(),
                controller_id="example-controller",
            )

        sweep(False)
        self.assertFalse(
            AuditLog.objects.filter(action=AuditLog.Action.SETTINGS_CHANGED).exists()
        )
        sweep(True)
        sweep(True)
        changes = AuditLog.objects.filter(
            connection="example-dns", action=AuditLog.Action.SETTINGS_CHANGED
        )
        self.assertEqual([event.summary for event in changes], ["Manages"])

    def test_a_carried_connection_was_not_asked_and_writes_nothing(self):
        record_connections(
            [{"connection_ref": "example-ssh", "provider": "ssh", "ok": True}],
            principal=cli_principal(),
            controller_id="example-controller",
        )
        AuditLog.objects.all().delete()

        record_connections(
            [{"connection_ref": "example-ssh", "provider": "ssh", "carried": True}],
            principal=cli_principal(),
            controller_id="example-controller",
        )

        self.assertFalse(AuditLog.objects.filter(connection="example-ssh").exists())

    def test_a_resource_and_its_operations_name_their_connection(self):
        resource = ManagedResource.objects.create(
            key="example-zone",
            kind="cloudflare.zone",
            spec={"zone": "example.com", "connection_ref": "example-dns"},
        )
        OperationRequest.objects.create(
            resource=resource,
            action=OperationRequest.Action.RECONCILE,
            requested_actor="example-controller",
            requested_interface="controller",
            idempotency_key="example-key",
        )

        events = AuditLog.objects.filter(connection="example-dns")
        self.assertEqual(
            set(events.values_list("object_type", flat=True)),
            {"Managed resource", "Infrastructure operation"},
        )


class LastActivityPageTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user(
            username="operator", password="not-a-real-password"
        )
        self.client.force_login(user)

    def _connection(self, ref, provider="cloudflare_dns"):
        return ProviderConnection.objects.create(
            connection_ref=ref,
            controller_id="example-controller",
            provider=provider,
            observed_at=timezone.now(),
        )

    def test_a_row_links_its_last_activity(self):
        self._connection("example-dns")
        self._connection("example-quiet", provider="ssh")
        event = AuditLog.objects.create(
            action=AuditLog.Action.UPDATED,
            connection="example-dns",
            message="Reconciled example.com",
        )

        response = self.client.get(reverse("control_plane:connections"))

        self.assertContains(response, reverse("core:audit_detail", args=[event.pk]))
        self.assertContains(response, "Reconciled example.com · ")
        self.assertContains(response, 'class="connection-last-activity"', count=1)

    def test_last_activity_does_not_cost_a_query_per_row(self):
        def queries_with(count):
            ProviderConnection.objects.all().delete()
            for index in range(count):
                ref = f"example-{index}"
                self._connection(ref)
                AuditLog.objects.create(
                    action=AuditLog.Action.UPDATED, connection=ref, message="x"
                )
            with CaptureQueriesContext(db_connection) as captured:
                self.client.get(reverse("control_plane:connections"))
            return len(captured)

        self.assertEqual(queries_with(1), queries_with(4))
