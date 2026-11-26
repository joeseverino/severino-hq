"""Refresh: an operator asks for one connection, one kind or everything to be read.

The forced kinds are due on the next pull whatever the cadence says, and only
the kinds that connection's credential reads. The request goes through the one
gated capability, POST only.
"""

from datetime import timedelta

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from hq.domains.control_plane.models import ProviderInventory, ReadRequest
from hq.platform.application.markup_testing import text
from hq.platform.application.plugin_testing import form_keyed
from hq.platform.core.models import AuditLog
from hq.platform.core.testing import READER, connection, operator

from ..action_links import read_now_link, read_now_payload
from ..cadence import sweep_due
from ..capabilities import command_spec, execute_capability
from ..credential_sight import fed_kinds
from ..read_requests import forced_reads, settle_read_requests
from ..security import cli_principal

READ_NOW = "infrastructure.controller.refresh"
SWEEPS = override_settings(
    SEVERINO_SWEEP_INTERVAL_ACTIVE_SECONDS=3600,
    SEVERINO_SWEEP_INTERVAL_IDLE_SECONDS=43200,
)


def store(kind: str, *, at=None) -> None:
    ProviderInventory.objects.update_or_create(
        kind=kind,
        defaults={"records": [], "reachable": True, "observed_at": at or timezone.now()},
    )


@SWEEPS
class ForcedKindsTests(TestCase):
    def setUp(self):
        connection("tailscale", "example-tailnet")
        connection("cloudflare_api", "example-dns")
        # Everything fresh: the cadence alone says nothing is due.
        for kind in (*fed_kinds("tailscale"), *fed_kinds("cloudflare_api")):
            store(kind)

    def ask(self, **payload):
        return execute_capability(READ_NOW, payload, principal=cli_principal())

    def test_nothing_is_due_until_somebody_asks(self):
        verdict = sweep_due("example-controller")

        self.assertFalse(verdict["due"])
        self.assertEqual(verdict["only_kinds"], [])

    def test_a_connection_asked_for_is_due_now_and_only_its_kinds(self):
        result = self.ask(connection_ref="example-tailnet")

        self.assertTrue(result["ok"], result)
        self.assertIn("example-tailnet", result["message"])
        verdict = sweep_due("example-controller")
        self.assertTrue(verdict["due"])
        self.assertEqual(verdict["only_kinds"], sorted(fed_kinds("tailscale")))
        self.assertFalse(set(verdict["only_kinds"]) & set(fed_kinds("cloudflare_api")))
        self.assertNotIn("example-tailnet", verdict["carry"])

    def test_a_kind_asked_for_is_the_only_kind_read(self):
        kind = fed_kinds("cloudflare_api")[0]

        self.assertTrue(self.ask(kind=kind)["ok"])

        self.assertEqual(sweep_due()["only_kinds"], [kind])

    def test_every_connection_reads_the_whole_sweep(self):
        self.assertTrue(self.ask(every_connection=True)["ok"])

        verdict = sweep_due()
        self.assertTrue(verdict["due"])
        # Empty is every kind.
        self.assertEqual(verdict["only_kinds"], [])

    def test_a_read_is_answered_by_what_the_controller_stores_after_it(self):
        self.ask(connection_ref="example-tailnet")
        self.assertEqual(len(forced_reads()), 1)

        later = timezone.now() + timedelta(seconds=5)
        connection("tailscale", "example-tailnet", observed_at=later)
        for kind in fed_kinds("tailscale"):
            store(kind, at=later)

        self.assertEqual(forced_reads(), ())
        self.assertEqual(settle_read_requests(), 1)
        self.assertFalse(ReadRequest.objects.exists())

    @override_settings(SEVERINO_READ_REQUEST_SECONDS=60)
    def test_a_read_nothing_answers_stops_forcing_sweeps(self):
        self.ask(connection_ref="example-tailnet")
        ReadRequest.objects.update(requested_at=timezone.now() - timedelta(minutes=5))

        self.assertEqual(forced_reads(), ())
        self.assertFalse(sweep_due()["due"])

    def test_the_request_is_audited_against_its_connection(self):
        self.ask(connection_ref="example-tailnet")

        event = AuditLog.objects.filter(object_type="Read request").latest("pk")
        self.assertEqual(event.connection, "example-tailnet")


@SWEEPS
class RefusalTests(TestCase):
    def setUp(self):
        connection("tailscale", "example-tailnet")

    def test_an_unknown_connection_is_not_found(self):
        result = execute_capability(READ_NOW, {"connection_ref": "no-such-connection"}, principal=cli_principal())

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "not_found")
        self.assertFalse(ReadRequest.objects.exists())

    def test_an_unknown_kind_is_refused(self):
        result = execute_capability(READ_NOW, {"kind": "no.such.kind"}, principal=cli_principal())

        self.assertEqual(result["error"]["code"], "invalid_input")

    def test_two_subjects_at_once_are_refused(self):
        result = execute_capability(
            READ_NOW,
            {"connection_ref": "example-tailnet", "every_connection": True},
            principal=cli_principal(),
        )

        self.assertEqual(result["error"]["code"], "invalid_input")
        self.assertFalse(ReadRequest.objects.exists())

    def test_a_reader_may_not_ask(self):
        reader = READER

        result = execute_capability(READ_NOW, {"connection_ref": "example-tailnet"}, principal=reader)

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "forbidden")
        self.assertFalse(ReadRequest.objects.exists())
        self.assertIsNone(read_now_link(reader, connection_ref="example-tailnet"))


@SWEEPS
class WebTests(TestCase):
    def setUp(self):
        connection("tailscale", "example-tailnet")
        operator(self.client)

    def test_the_link_posts_what_it_names_and_returns_with_the_result(self):
        link = read_now_link(cli_principal(), connection_ref="example-tailnet")

        response = self.client.post(link.url, form_keyed({"next": reverse("control_plane:connections")}), follow=True)

        self.assertEqual(link.method, "POST")
        self.assertEqual(ReadRequest.objects.get().connection_ref, "example-tailnet")
        self.assertIn(command_spec(link.capability).requested("example-tailnet"), text(response))

    def test_a_refusal_is_said_on_the_page_it_came_from(self):
        response = self.client.post(
            reverse("control_plane:read_now"), form_keyed({"connection_ref": "no-such-connection"}), follow=True
        )

        self.assertIn("connection_ref names nothing HQ has.", text(response.content.decode()))
        self.assertFalse(ReadRequest.objects.exists())

    def test_the_payload_is_the_inverse_of_the_link(self):
        self.assertEqual(
            read_now_payload({"connection_ref": " example-tailnet ", "kind": ""}),
            {"connection_ref": "example-tailnet"},
        )
        self.assertEqual(read_now_payload({"every_connection": "1"}), {"every_connection": True})
        self.assertEqual(read_now_payload({"every_connection": "yes"}), {})


class QueueRefreshTests(TestCase):
    """A queue's one Refresh reads again what its shown rows rest on."""

    def rows(self, *scopes, principal=None):
        from ..dashboard import finding_rows
        from ..finding_model import Finding
        from ..findings import _resolved

        found = (
            _resolved(
                Finding(
                    rule="example-rule",
                    subject="",
                    title=scope or "t",
                    severity="neutral",
                    explanation="e",
                    scope=scope,
                ),
                principal or cli_principal(),
                None,
            )
            for scope in scopes
        )
        return finding_rows("example", "Example", tuple(found))

    def refresh(self, rows, principal=None):
        from hq.platform.core.queue_table import refresh

        return refresh(rows, principal or cli_principal(), "/example/page/")

    def test_rows_on_one_reading_ask_for_it_and_come_back(self):
        action = self.refresh(self.rows("tailscale.device", "tailscale.device"))

        self.assertEqual(action.label, command_spec(READ_NOW).words())
        self.assertIn("kind=tailscale.device", action.url)
        self.assertIn("next=%2Fexample%2Fpage%2F", action.url)

    def test_rows_on_several_readings_ask_for_every_connection(self):
        action = self.refresh(self.rows("tailscale.device", "portainer.container"))

        self.assertIn("every_connection=1", action.url)

    def test_nothing_is_offered_where_no_row_rests_on_a_reading(self):
        self.assertIsNone(self.refresh(self.rows("")))

    def test_a_reader_who_may_not_ask_is_offered_nothing(self):
        self.assertIsNone(self.refresh(self.rows("tailscale.device", principal=READER), principal=READER))
