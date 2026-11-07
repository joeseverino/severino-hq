"""Every agent call is placed on the tailnet device tailscaled says sent it,
and does nothing from a device until the person it acts for approves it there.

Held at both doors, through HQ's own admission: an unapproved device is
refused ``device_awaiting_approval``, a blocked one ``device_blocked`` while
the same client's other devices work, an approved one runs, and every event
the call records names the device. Where the deployment has a tailnet, a call
it cannot place is refused, never admitted as the client alone. The codes and
capability names are written out, so the module imports at a base that has
none of them and fails there for what it lacks.
"""

import json
import tempfile
from pathlib import Path

from asgiref.sync import async_to_sync
from django.apps import apps
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from hq.domains.control_plane.models import ApprovalRequest, ProviderInventory
from hq.domains.projects.models import Project
from hq.platform.api.tests.tokens import ISSUER, RESOURCE, _serving, _token
from hq.platform.application.tests.writers import keyed
from hq.platform.core.models import AgentIdentity, AuditLog, OwnDevice

from ..capabilities import execute_capability
from ..linked_accounts import bind_sign_in, sign_in_subject
from ..security import Principal, subject_of, web_principal

AWAITING = "device_awaiting_approval"
BLOCKED = "device_blocked"
UNKNOWN = "device_unknown"
UNAVAILABLE = "tailnet_unavailable"
APPROVE = "agent.device_approval.set"
BLOCK = "agent.device_block.set"

CLIENT = "example-agent"
PERSON = "3f1c2b7e-5a4d-4e8b-9c1a-0d2e3f4a5b6c"
# Two of the person's devices, each at an address only Tailscale issues.
LAPTOP, LAPTOP_AT, LAPTOP_ID = "example-laptop", "100.64.0.7", "nLAPTOP1CNTRL"
DESKTOP, DESKTOP_AT, DESKTOP_ID = "example-desktop", "100.64.0.8", "nDESKTOP1CNTRL"
# A tailnet address no peer has, and HQ's own loopback, which the trusted networks let in:
# nowhere tailscaled places a call. The LAN is refused before admission, as a network HQ does not trust.
NOWHERE_AT = ("100.64.0.99", "127.0.0.1")
SCOPE = "read write_projects"

API = override_settings(
    OIDC_ISSUER=ISSUER, SEVERINO_API_RESOURCE=RESOURCE, SEVERINO_API_LEEWAY_SECONDS=30, OIDC_RP_SIGN_ALGO="RS256"
)


def AgentDevice():
    """The model, looked up so this module imports at a base that has none."""

    return apps.get_model("core", "AgentDevice")


def a_tailnet(*devices, **how):
    """tailscaled on its socket, placing a call from each device's address on it: name, address, stable id."""

    from .tailscaled import answering, peer

    return answering({address: peer(name, node_id) for name, address, node_id in devices}, **how)


def _token_for(scope=SCOPE, *, person=True):
    return _token(CLIENT, sub=PERSON if person else f"client-{CLIENT}", scope=scope)


class Doors:
    """One agent call through each door, from ``address``, through HQ's own admission."""

    def api_write(self, token, address, slug):
        return self.client.post(
            "/api/v2/capabilities/project.create/",
            data=json.dumps({"payload": {"name": slug, "slug": slug, "status": "active"}}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
            HTTP_IDEMPOTENCY_KEY=slug,
            REMOTE_ADDR=address,
        )

    def api_code(self, token, address, slug):
        answer = self.api_write(token, address, slug)
        return None if answer.status_code == 200 else answer.json()["error"]["code"]

    def mcp_write(self, token, address, slug):
        """The tool result of an ``execute_capability`` call over /api/mcp/ from ``address``."""

        import httpx2
        from starlette.applications import Starlette
        from starlette.routing import Mount

        from hq.config import asgi
        from hq.platform.mcp.security import MCPBoundary, TailnetGate
        from hq.platform.mcp.server import mcp

        served = mcp.served()
        # The networks production trusts: the tailnet, and loopback for the proxy and the health probe.
        gate = TailnetGate(allowed_hosts=("testserver",), allowed_networks=("100.64.0.0/10", "127.0.0.0/8"))
        door = MCPBoundary(served.application, gate=gate, admit=asgi.admit_agent)
        application = Starlette(routes=[Mount("/api/mcp", app=door)])
        call = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "execute_capability",
                "arguments": {
                    "name": "project.create",
                    "payload": {"name": slug, "slug": slug, "status": "active", "idempotency_key": slug},
                },
            },
        }

        async def run():
            transport = httpx2.ASGITransport(app=application, client=(address, 50000))
            async with (
                served.sessions.run(),
                httpx2.AsyncClient(transport=transport, base_url="http://testserver") as client,
            ):
                return await client.post(
                    "/api/mcp/",
                    headers={"Accept": "application/json, text/event-stream", "Authorization": f"Bearer {token}"},
                    json=call,
                )

        answer = async_to_sync(run)().json()["result"]
        return answer["structuredContent"]

    def mcp_code(self, token, address, slug):
        answer = self.mcp_write(token, address, slug)
        return (answer.get("error") or {}).get("code")

    def codes(self, token, address, slug):
        """The refusal code each door answers, or None where the call ran: ``(api, mcp)``."""

        with _serving():
            return self.api_code(token, address, f"{slug}-api"), self.mcp_code(token, address, f"{slug}-mcp")


@API
class PlacementTests(TestCase):
    def test_a_call_is_placed_on_the_device_tailscaled_names_by_its_stable_id(self):
        from ..agent_devices import place

        with a_tailnet((LAPTOP, LAPTOP_AT, LAPTOP_ID)):
            placed = place(LAPTOP_AT)

        self.assertEqual(placed.node_id, LAPTOP_ID)
        self.assertEqual(
            (placed.device.label, placed.device.os, placed.device.user), (LAPTOP, "macOS", "owner@example.com")
        )
        self.assertIsNone(placed.refused)

    def test_with_a_tailnet_a_call_no_device_sent_is_refused_never_the_client_alone(self):
        from ..agent_devices import place

        with a_tailnet((LAPTOP, LAPTOP_AT, LAPTOP_ID)):
            for address in (*NOWHERE_AT, "192.0.2.20"):
                with self.subTest(address=address):
                    placed = place(address)
                    self.assertEqual((placed.refused, placed.node_id), (UNKNOWN, ""))

    def test_a_tailnet_that_does_not_answer_refuses_the_call_and_says_so(self):
        from ..agent_devices import place

        with a_tailnet(failing="tailscaled.sock: connection refused"):
            placed = place(LAPTOP_AT)

        self.assertEqual(placed.refused, UNAVAILABLE)
        self.assertIn("connection refused", placed.detail)

    def test_on_a_tailnet_host_whose_daemon_is_away_the_call_waits_rather_than_running_unplaced(self):
        """The real client against the path the host named, with no socket
        there: a daemon restarting leaves none for a moment."""

        from ..agent_devices import place

        with override_settings(SEVERINO_TAILSCALED_SOCKET=str(Path(tempfile.mkdtemp()) / "tailscaled.sock")):
            placed = place(LAPTOP_AT)

        self.assertEqual(placed.refused, UNAVAILABLE)
        self.assertIn("No such file", placed.detail)

    def test_without_a_tailnet_nothing_is_placed_and_the_client_is_the_finest_unit(self):
        from ..agent_devices import place

        for address in (LAPTOP_AT, *NOWHERE_AT, ""):
            with self.subTest(address=address):
                placed = place(address)
                self.assertEqual((placed.refused, placed.node_id), (None, ""))


@API
class ApprovalAtTheDoorsTests(Doors, TestCase):
    def setUp(self):
        self.person = get_user_model().objects.create_user(username="operator", password="test-only-password")
        bind_sign_in(self.person, sign_in_subject(ISSUER, PERSON))
        self.enterContext(a_tailnet((LAPTOP, LAPTOP_AT, LAPTOP_ID), (DESKTOP, DESKTOP_AT, DESKTOP_ID)))

    def _device(self, node_id):
        return AgentDevice().objects.get(identity__client_id=CLIENT, node_id=node_id)

    def _set(self, capability, node_id, *, state=True, principal=None):
        field = "approved" if capability == APPROVE else "blocked"
        return execute_capability(
            capability,
            keyed({field: state}),
            principal=principal or web_principal(self.person),
            target=str(self._device(node_id).pk),
        )

    def test_a_device_nobody_approved_does_nothing_until_its_person_approves_it(self):
        token = _token_for()

        self.assertEqual(self.codes(token, LAPTOP_AT, "first"), (AWAITING, AWAITING))
        self.assertFalse(Project.objects.exists(), "an unapproved device's call ran")
        waiting = self._device(LAPTOP_ID)
        self.assertEqual((waiting.person, waiting.name, waiting.approved_at), (self.person, LAPTOP, None))

        self.assertTrue(self._set(APPROVE, LAPTOP_ID)["ok"])

        self.assertEqual(self.codes(token, LAPTOP_AT, "approved"), (None, None))
        self.assertEqual(set(Project.objects.values_list("slug", flat=True)), {"approved-api", "approved-mcp"})

    def test_a_blocked_device_is_refused_while_the_same_clients_other_device_runs(self):
        token = _token_for()
        self.codes(token, LAPTOP_AT, "seen")
        self.codes(token, DESKTOP_AT, "seen-too")
        for node_id in (LAPTOP_ID, DESKTOP_ID):
            self._set(APPROVE, node_id)

        self.assertTrue(self._set(BLOCK, LAPTOP_ID)["ok"])

        self.assertEqual(self.codes(token, LAPTOP_AT, "blocked"), (BLOCKED, BLOCKED))
        self.assertEqual(self.codes(token, DESKTOP_AT, "other"), (None, None))
        self.assertFalse(Project.objects.filter(slug__startswith="blocked").exists())

    def test_before_any_tailnet_reading_a_device_still_waits_for_its_approval(self):
        self.assertFalse(ProviderInventory.objects.exists(), "a fresh database, before the first reading")

        self.assertEqual(self.codes(_token_for(), LAPTOP_AT, "fresh"), (AWAITING, AWAITING))

        self.assertFalse(Project.objects.exists())
        self.assertEqual(AgentDevice().objects.get().node_id, LAPTOP_ID)

    def test_a_token_presented_from_nowhere_the_tailnet_places_is_refused_at_both_doors(self):
        token = _token_for()
        for address in NOWHERE_AT:
            with self.subTest(address=address):
                self.assertEqual(self.codes(token, address, f"from-{address}"), (UNKNOWN, UNKNOWN))
        with _serving():
            answer = self.api_write(token, "127.0.0.1", "loopback")

        self.assertEqual(answer.status_code, 403)
        self.assertIn("Agents page", answer.json()["error"]["message"])
        self.assertFalse(Project.objects.exists(), "a call from nowhere ran")
        self.assertFalse(AgentDevice().objects.exists(), "nothing is there to approve")
        refused = AuditLog.objects.filter(action=AuditLog.Action.DENIED, metadata__reason=UNKNOWN)
        self.assertEqual(set(refused.values_list("metadata__source", flat=True)), set(NOWHERE_AT))

    def test_a_tailnet_that_does_not_answer_refuses_the_call_at_both_doors(self):
        token = _token_for()
        with a_tailnet(failing="connection refused"):
            codes = self.codes(token, LAPTOP_AT, "unanswered")
            with _serving():
                answer = self.api_write(token, LAPTOP_AT, "unanswered-api")

        self.assertEqual(codes, (UNAVAILABLE, UNAVAILABLE))
        self.assertEqual(answer.status_code, 503)
        self.assertIn("connection refused", answer.json()["error"]["message"])
        self.assertFalse(Project.objects.exists())

    def test_a_forwarded_header_does_not_move_a_call_off_the_device_that_sent_it(self):
        with _serving():
            answer = self.client.post(
                "/api/v2/capabilities/project.create/",
                data=json.dumps({"payload": {"name": "x", "slug": "forwarded", "status": "active"}}),
                content_type="application/json",
                HTTP_AUTHORIZATION=f"Bearer {_token_for()}",
                HTTP_IDEMPOTENCY_KEY="forwarded",
                HTTP_X_FORWARDED_FOR="127.0.0.1",
                REMOTE_ADDR=LAPTOP_AT,
            )

        self.assertEqual(answer.json()["error"]["code"], AWAITING)

    def test_without_a_tailnet_the_clients_own_rules_are_the_finest_unit(self):
        with override_settings(SEVERINO_TAILSCALED_SOCKET=""), _serving():
            answer = self.api_write(_token_for(), "127.0.0.1", "loopback")

        self.assertEqual(answer.status_code, 200, answer.content)
        self.assertFalse(AgentDevice().objects.exists())

    def test_a_page_never_asks_tailscaled(self):
        self.person.is_superuser = self.person.is_staff = True
        self.person.save()
        self.codes(_token_for(), LAPTOP_AT, "seen")
        self.client.force_login(self.person)

        with a_tailnet((LAPTOP, LAPTOP_AT, LAPTOP_ID)) as asked:
            for route in ("dashboard", "agent_policy", "connection", "core:audit_list"):
                with self.subTest(route=route):
                    self.assertEqual(self.client.get(reverse(route), REMOTE_ADDR=LAPTOP_AT).status_code, 200)

        self.assertEqual(asked.call_count, 0, "a page load placed a call")

    def test_a_persons_own_client_needs_no_device_approval(self):
        OwnDevice.objects.create(user=self.person, client_id=CLIENT)

        with _serving():
            answer = self.api_write(_token_for(f"{SCOPE} own_device"), LAPTOP_AT, "own")

        self.assertEqual(answer.status_code, 200, answer.content)
        self.assertFalse(AgentDevice().objects.exists())

    def test_every_event_the_call_records_names_its_device(self):
        token = _token_for()
        self.codes(token, LAPTOP_AT, "seen")
        self._set(APPROVE, LAPTOP_ID)
        before = AuditLog.objects.values_list("pk", flat=True)
        before = set(before)

        self.assertEqual(self.codes(token, LAPTOP_AT, "named"), (None, None))

        written = AuditLog.objects.exclude(pk__in=before).exclude(object_type="Agent device")
        self.assertTrue(written.filter(object_type="Project").exists())
        self.assertEqual(set(written.values_list("device", flat=True)), {LAPTOP_ID})

    def test_a_held_request_records_the_device_that_asked(self):
        from hq.domains.control_plane.models import CapabilityRule

        CapabilityRule.objects.create(
            subject=subject_of(CLIENT, sign_in_subject(ISSUER, PERSON)),
            capability="project.create",
            rule=CapabilityRule.Rule.APPROVE,
        )
        token = _token_for()
        self.codes(token, LAPTOP_AT, "seen")
        self._set(APPROVE, LAPTOP_ID)

        with _serving():
            answer = self.api_write(token, LAPTOP_AT, "held")

        self.assertEqual(answer.json()["data"]["status"], "awaiting_approval", answer.content)
        self.assertEqual(ApprovalRequest.objects.get().requested_device, LAPTOP_ID)

    def test_a_refusal_names_the_device_it_refused(self):
        self.codes(_token_for(), LAPTOP_AT, "refused")

        denials = AuditLog.objects.filter(action=AuditLog.Action.DENIED, metadata__reason=AWAITING)
        self.assertTrue(denials.exists())
        self.assertEqual(set(denials.values_list("device", flat=True)), {LAPTOP_ID})


@API
class WhoMayApproveTests(Doors, TestCase):
    def setUp(self):
        self.person = get_user_model().objects.create_user(username="operator", password="test-only-password")
        bind_sign_in(self.person, sign_in_subject(ISSUER, PERSON))
        self.other = get_user_model().objects.create_user(username="someone-else", password="test-only-password")
        self.enterContext(a_tailnet((LAPTOP, LAPTOP_AT, LAPTOP_ID)))

    def _key(self, *, person=True):
        with _serving():
            self.api_write(_token_for(person=person), LAPTOP_AT, f"seen-{person}")
        return str(AgentDevice().objects.get(person=self.person if person else None).pk)

    def test_no_token_approves_or_blocks_a_device(self):
        key = self._key()
        granted = frozenset({"read", "govern_agents"})
        for capability, field in ((APPROVE, "approved"), (BLOCK, "blocked")):
            for interface in ("mcp", "api"):
                with self.subTest(capability=capability, interface=interface):
                    answer = execute_capability(
                        capability,
                        keyed({field: True}),
                        principal=Principal(CLIENT, interface, granted, granted=granted),
                        target=key,
                    )
                    self.assertEqual(answer["error"]["code"], "forbidden")
        self.assertFalse(AgentDevice().objects.filter(approved_at__isnull=False).exists())

    def test_only_the_person_an_agent_acts_for_approves_its_device(self):
        key = self._key()

        refused = execute_capability(
            APPROVE, keyed({"approved": True}), principal=web_principal(self.other), target=key
        )
        approved = execute_capability(
            APPROVE, keyed({"approved": True}), principal=web_principal(self.person), target=key
        )

        self.assertEqual(refused["error"]["code"], "forbidden")
        self.assertTrue(approved["ok"], approved)
        self.assertEqual(AgentDevice().objects.get(pk=key).approved_by, self.person)

    def test_a_machine_agents_device_is_approved_by_whoever_governs_agents(self):
        key = self._key(person=False)

        answer = execute_capability(APPROVE, keyed({"approved": True}), principal=web_principal(self.other), target=key)

        self.assertTrue(answer["ok"], answer)

    def test_a_device_no_agent_called_from_is_not_found(self):
        answer = execute_capability(
            APPROVE, keyed({"approved": True}), principal=web_principal(self.person), target="999"
        )

        self.assertEqual(answer["error"]["code"], "not_found")


@API
class HeldByABlockedDeviceTests(TestCase):
    def setUp(self):
        self.person = get_user_model().objects.create_user(username="operator", password="test-only-password")
        identity = AgentIdentity.objects.create(client_id=CLIENT, interfaces=["mcp"], granted=["read"])
        for node_id, blocked in ((LAPTOP_ID, True), (DESKTOP_ID, False)):
            AgentDevice().objects.create(
                identity=identity, node_id=node_id, blocked_at=timezone.now() if blocked else None
            )

    def _held(self, node_id):
        return ApprovalRequest.objects.create(
            capability="example.change",
            content_fingerprint=node_id,
            requested_actor=CLIENT,
            requested_interface="mcp",
            requested_device=node_id,
            expires_at=timezone.now() + timezone.timedelta(hours=1),
        )

    def test_what_a_blocked_device_asked_for_is_held_and_its_other_device_is_not(self):
        from ..agent_access import held_askers

        blocked, other = self._held(LAPTOP_ID), self._held(DESKTOP_ID)

        withheld = set(ApprovalRequest.objects.filter(held_askers()).values_list("pk", flat=True))
        self.assertEqual(withheld, {blocked.pk})
        self.assertNotIn(other.pk, withheld)
