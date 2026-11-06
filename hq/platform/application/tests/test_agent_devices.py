"""Every agent call is placed on the tailnet device it came from, and does
nothing from a device until the person it acts for approves it there.

Held at both doors, through HQ's own admission: an unapproved device is
refused ``device_awaiting_approval``, a blocked one ``device_blocked`` while
the same client's other devices work, an approved one runs, and every event
the call records names the device. The codes and capability names are written
out, so the module imports at a base that has none of them and fails there for
what it lacks.
"""

import json

from asgiref.sync import async_to_sync
from django.apps import apps
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from hq.domains.control_plane.models import ApprovalRequest, ProviderInventory, ReadRequest
from hq.domains.projects.models import Project
from hq.platform.api.tests.tokens import ISSUER, RESOURCE, _serving, _token
from hq.platform.application.tests.writers import keyed
from hq.platform.core.models import AgentIdentity, AuditLog, OwnDevice

from ..capabilities import execute_capability
from ..linked_accounts import bind_sign_in, sign_in_subject
from ..security import Principal, web_principal

AWAITING = "device_awaiting_approval"
BLOCKED = "device_blocked"
APPROVE = "agent.device_approval.set"
BLOCK = "agent.device_block.set"

CLIENT = "example-agent"
PERSON = "3f1c2b7e-5a4d-4e8b-9c1a-0d2e3f4a5b6c"
# Two of the person's devices, each at an address only Tailscale issues.
LAPTOP, LAPTOP_AT, LAPTOP_ID = "example-laptop", "100.64.0.7", "nLAPTOP1CNTRL"
DESKTOP, DESKTOP_AT, DESKTOP_ID = "example-desktop", "100.64.0.8", "nDESKTOP1CNTRL"
# On the tailnet, and in no reading yet.
UNSEEN_AT = "100.64.0.99"
SCOPE = "read write_projects"

API = override_settings(
    OIDC_ISSUER=ISSUER, SEVERINO_API_RESOURCE=RESOURCE, SEVERINO_API_LEEWAY_SECONDS=30, OIDC_RP_SIGN_ALGO="RS256"
)


def AgentDevice():
    """The model, looked up so this module imports at a base that has none."""

    return apps.get_model("core", "AgentDevice")


def a_tailnet(*devices):
    """A device reading as the controller writes one: name, stable id, addresses, owner."""

    ProviderInventory.objects.update_or_create(
        kind="tailscale.device",
        defaults={
            "records": [
                {"name": name, "node_id": node_id, "addresses": [address], "user": "owner@example.com", "os": "macOS"}
                for name, address, node_id in devices
            ],
            "observed_at": timezone.now(),
        },
    )


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
        gate = TailnetGate(allowed_hosts=("testserver",), allowed_networks=("100.64.0.0/10",))
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
    def test_a_call_is_placed_on_the_device_at_its_address_by_its_stable_id(self):
        from ..agent_devices import place

        a_tailnet((LAPTOP, LAPTOP_AT, LAPTOP_ID))

        self.assertEqual(place(LAPTOP_AT).node_id, LAPTOP_ID)

    def test_a_tailnet_address_the_reading_does_not_hold_is_unseen_never_a_device(self):
        from ..agent_devices import place

        a_tailnet((LAPTOP, LAPTOP_AT, LAPTOP_ID), (DESKTOP, DESKTOP_AT, ""))

        for address in (UNSEEN_AT, DESKTOP_AT):
            with self.subTest(address=address):
                placed = place(address)
                self.assertTrue(placed.unseen)
                self.assertEqual(placed.node_id, "")

    def test_where_hq_cannot_place_a_call_the_client_is_the_finest_unit(self):
        from ..agent_devices import place

        self.assertFalse(place(LAPTOP_AT).unseen, "no tailnet reading at all")
        a_tailnet((LAPTOP, LAPTOP_AT, LAPTOP_ID))
        for address in ("127.0.0.1", "198.51.100.20", ""):
            with self.subTest(address=address):
                self.assertEqual((place(address).unseen, place(address).node_id), (False, ""))


@API
class ApprovalAtTheDoorsTests(Doors, TestCase):
    def setUp(self):
        self.person = get_user_model().objects.create_user(username="operator", password="test-only-password")
        bind_sign_in(self.person, sign_in_subject(ISSUER, PERSON))
        a_tailnet((LAPTOP, LAPTOP_AT, LAPTOP_ID), (DESKTOP, DESKTOP_AT, DESKTOP_ID))

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

    def test_a_device_the_tailnet_reading_does_not_hold_is_awaiting_and_told_how_to_read_it_now(self):
        with _serving():
            answer = self.api_write(_token_for(), UNSEEN_AT, "unseen")

        self.assertEqual((answer.status_code, answer.json()["error"]["code"]), (403, AWAITING))
        self.assertIn("infrastructure.controller.refresh", answer.json()["error"]["message"])
        self.assertFalse(ReadRequest.objects.exists(), "a refusal never writes")
        self.assertFalse(AgentDevice().objects.exists())

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

    def test_a_call_hq_cannot_place_keeps_the_clients_own_rules(self):
        with _serving():
            answer = self.api_write(_token_for(), "127.0.0.1", "loopback")

        self.assertEqual(answer.status_code, 200, answer.content)

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

        CapabilityRule.objects.create(subject=CLIENT, capability="project.create", rule=CapabilityRule.Rule.APPROVE)
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
        a_tailnet((LAPTOP, LAPTOP_AT, LAPTOP_ID))

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
