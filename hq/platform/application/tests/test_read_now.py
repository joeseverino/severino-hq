"""Read now: an operator asks for one connection, one kind or everything to be read.

The forced kinds are due on the next pull whatever the cadence says, and only
the kinds that connection's credential reads. The request goes through the one
gated capability, POST only.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
import tempfile

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from hq.domains.control_plane.models import ProviderConnection, ProviderInventory, ReadRequest
from hq.platform.core.models import AuditLog

from ..action_links import read_now_link, read_now_payload
from ..cadence import forced_reads, settle_read_requests, sweep_due
from ..capabilities import execute_capability
from ..credential_sight import fed_kinds
from ..security import Capability, Principal, cli_principal

READ_NOW = "infrastructure.controller.refresh"
DIRECTORY = Path(tempfile.mkdtemp())
MARKERS = override_settings(
    SEVERINO_CONTROLLER_DOORBELL=str(DIRECTORY / "doorbell"),
    SEVERINO_ACTIVITY_MARKER=str(DIRECTORY / "activity"),
    SEVERINO_SWEEP_INTERVAL_ACTIVE_SECONDS=3600,
    SEVERINO_SWEEP_INTERVAL_IDLE_SECONDS=43200,
)


def store(kind: str, *, at=None) -> None:
    ProviderInventory.objects.update_or_create(
        kind=kind,
        defaults={"records": [], "reachable": True, "observed_at": at or timezone.now()},
    )


def connect(ref: str, provider: str, *, at=None) -> None:
    ProviderConnection.objects.update_or_create(
        controller_id="example-controller",
        connection_ref=ref,
        defaults={"provider": provider, "observed_at": at or timezone.now()},
    )


@MARKERS
class ForcedKindsTests(TestCase):
    def setUp(self):
        connect("example-tailnet", "tailscale")
        connect("example-dns", "cloudflare_api")
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
        connect("example-tailnet", "tailscale", at=later)
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


@MARKERS
class RefusalTests(TestCase):
    def setUp(self):
        connect("example-tailnet", "tailscale")

    def test_an_unknown_connection_is_refused(self):
        result = execute_capability(
            READ_NOW, {"connection_ref": "no-such-connection"}, principal=cli_principal()
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "invalid_input")
        self.assertFalse(ReadRequest.objects.exists())

    def test_an_unknown_kind_is_refused(self):
        result = execute_capability(
            READ_NOW, {"kind": "no.such.kind"}, principal=cli_principal()
        )

        self.assertEqual(result["error"]["code"], "invalid_input")

    def test_two_subjects_at_once_are_refused(self):
        result = execute_capability(
            READ_NOW,
            {"connection_ref": "example-tailnet", "every_connection": True},
            principal=cli_principal(),
        )

        self.assertEqual(result["error"]["code"], "invalid_input")
        self.assertFalse(ReadRequest.objects.exists())

    def test_an_unknown_field_is_refused(self):
        result = execute_capability(READ_NOW, {"connection": "x"}, principal=cli_principal())

        self.assertEqual(result["error"]["code"], "invalid_input")

    def test_a_reader_may_not_ask(self):
        reader = Principal("reader", "test", frozenset({Capability.READ}))

        result = execute_capability(
            READ_NOW, {"connection_ref": "example-tailnet"}, principal=reader
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "forbidden")
        self.assertFalse(ReadRequest.objects.exists())
        self.assertIsNone(read_now_link(reader, connection_ref="example-tailnet"))


@MARKERS
class WebTests(TestCase):
    def setUp(self):
        connect("example-tailnet", "tailscale")
        self.client.force_login(
            get_user_model().objects.create_user("operator", password="pw", is_staff=True)
        )

    def test_get_has_no_side_effect(self):
        response = self.client.get(
            reverse("control_plane:read_now"), {"connection_ref": "example-tailnet"}
        )

        self.assertEqual(response.status_code, 405)
        self.assertFalse(ReadRequest.objects.exists())

    def test_the_link_posts_what_it_names_and_returns_with_the_result(self):
        link = read_now_link(cli_principal(), connection_ref="example-tailnet")

        response = self.client.post(
            link.url, {"next": reverse("control_plane:connections")}, follow=True
        )

        self.assertEqual(link.method, "POST")
        self.assertEqual(ReadRequest.objects.get().connection_ref, "example-tailnet")
        self.assertContains(
            response,
            "Asked the controller to read example-tailnet now; this page updates when "
            "it reports.",
        )

    def test_a_refusal_is_said_on_the_page_it_came_from(self):
        response = self.client.post(
            reverse("control_plane:read_now"), {"connection_ref": "no-such-connection"}, follow=True
        )

        self.assertContains(response, "connection_ref is not one of the allowed choices.")
        self.assertFalse(ReadRequest.objects.exists())

    def test_the_payload_is_the_inverse_of_the_link(self):
        self.assertEqual(
            read_now_payload({"connection_ref": " example-tailnet ", "kind": ""}),
            {"connection_ref": "example-tailnet"},
        )
        self.assertEqual(read_now_payload({"every_connection": "1"}), {"every_connection": True})
        self.assertEqual(read_now_payload({"every_connection": "yes"}), {})


class VerificationTests(TestCase):
    """A finding whose evidence is a reading is confirmed by reading it again."""

    def verify(self, finding, subject=None, principal=None):
        from ..findings import _verification

        return _verification(finding, principal or cli_principal(), subject)

    def finding(self, **fields):
        from ..finding_model import Finding

        return Finding(
            rule="example-rule", subject="", title="t", severity="neutral", explanation="e",
            **fields,
        )

    def test_a_kind_level_finding_reads_that_kind_now(self):
        action = self.verify(self.finding(scope="tailscale.device"))

        self.assertEqual(action.label, "Check again")
        self.assertEqual(action.method, "POST")
        self.assertIn("kind=tailscale.device", action.url)
        self.assertIn("next=%2Finfrastructure%2Ffindings%2F%3Frule%3Dexample-rule", action.url)

    def test_a_finding_no_reading_involves_is_checked_again(self):
        action = self.verify(self.finding())

        self.assertEqual(action.label, "Check again")
        self.assertEqual(action.method, "GET")

    def test_a_reader_is_offered_the_check_not_the_read(self):
        reader = Principal("reader", "test", frozenset({Capability.READ}))

        action = self.verify(self.finding(scope="tailscale.device"), principal=reader)

        self.assertEqual(action.label, "Check again")
