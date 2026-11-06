"""A person's block on one agent: asked on its every call at both doors, set only by a person."""

from unittest import mock

from django.contrib.auth import get_user_model
from django.db import DatabaseError
from django.test import TestCase, override_settings
from django.urls import reverse

from hq.platform.api.tests.tokens import ISSUER, RESOURCE, _serving, _token
from hq.platform.application.plugin_testing import form_keyed
from hq.platform.application.tests.writers import keyed
from hq.platform.core.models import AgentIdentity, AuditLog

from ..agent_access import AGENT_BLOCKED, AGENTS_PAUSED, SET_AGENT_BLOCK, blocked, refusal
from ..capabilities import execute_capability
from ..linked_accounts import bind_sign_in
from ..security import (
    OPERATOR_CAPABILITIES,
    Capability,
    Principal,
    agent_principal,
    cli_principal,
    device_principal,
    web_principal,
)

AGENT = "example-agent"
OTHER = "example-other-agent"
READ = frozenset({"read"})


def _seen(*clients: str) -> None:
    for client in clients:
        AgentIdentity.objects.create(client_id=client, interfaces=["mcp"], granted=["read"])


class RefusalTests(TestCase):
    def setUp(self):
        _seen(AGENT, OTHER)
        self.person = get_user_model().objects.create_user("op", password="x" * 20)
        answer = execute_capability(
            SET_AGENT_BLOCK, keyed({"blocked": True}), principal=web_principal(self.person), target=AGENT
        )
        self.assertTrue(answer["ok"], answer)

    def test_a_blocked_client_is_refused_on_both_surfaces_and_the_one_beside_it_acts(self):
        for interface in ("mcp", "api"):
            with self.subTest(interface=interface):
                self.assertEqual(refusal(agent_principal(AGENT, interface, READ)), AGENT_BLOCKED)
                self.assertIsNone(refusal(agent_principal(OTHER, interface, READ)))

    def test_a_blocked_device_stops_with_its_client(self):
        bind_sign_in(self.person, "example-key")
        device = device_principal(AGENT, READ, READ, person_key="example-key")

        self.assertEqual(refusal(device), AGENT_BLOCKED)

    def test_a_person_in_the_browser_is_never_blocked_by_a_client_of_the_same_name(self):
        AgentIdentity.objects.filter(client_id=AGENT).update(client_id=self.person.username)
        answer = execute_capability(
            SET_AGENT_BLOCK, keyed({"blocked": True}), principal=web_principal(self.person), target=self.person.username
        )
        self.assertTrue(answer["ok"], answer)

        self.assertIsNone(refusal(web_principal(self.person)))

    def test_the_block_says_its_own_code_while_agents_are_also_paused(self):
        with mock.patch("hq.platform.application.agent_access.agents_paused", return_value=True):
            self.assertEqual(refusal(agent_principal(AGENT, "mcp", READ)), AGENT_BLOCKED)
            self.assertEqual(refusal(agent_principal(OTHER, "mcp", READ)), AGENTS_PAUSED)

    def test_an_unreadable_block_refuses(self):
        with mock.patch.object(AgentIdentity.objects, "filter", side_effect=DatabaseError("locked")):
            self.assertTrue(blocked(OTHER))

    def test_the_refusal_is_recorded_by_the_agents_name(self):
        from ..agent_admission import refused

        refused(agent_principal(AGENT, "mcp", READ), source="192.0.2.10")

        denial = AuditLog.objects.get(action=AuditLog.Action.DENIED)
        self.assertEqual((denial.metadata["actor"], denial.metadata["reason"]), (AGENT, AGENT_BLOCKED.code))


@override_settings(
    OIDC_ISSUER=ISSUER, SEVERINO_API_RESOURCE=RESOURCE, SEVERINO_API_LEEWAY_SECONDS=30, OIDC_RP_SIGN_ALGO="RS256"
)
class MachineApiTests(TestCase):
    def test_a_blocked_client_is_refused_at_the_machine_api_and_another_reads(self):
        _seen(AGENT)
        AgentIdentity.objects.filter(client_id=AGENT).update(blocked_at="2026-11-01T12:00:00Z")
        with _serving():
            refused = self.client.get(
                "/api/v2/", HTTP_AUTHORIZATION=f"Bearer {_token(AGENT, sub=f'client-{AGENT}', scope='read')}"
            )
            allowed = self.client.get(
                "/api/v2/", HTTP_AUTHORIZATION=f"Bearer {_token(OTHER, sub=f'client-{OTHER}', scope='read')}"
            )

        self.assertEqual(refused.status_code, 403)
        self.assertEqual(refused.json()["error"]["code"], AGENT_BLOCKED.code)
        self.assertEqual(allowed.status_code, 200, allowed.content)


class WhoMayBlockTests(TestCase):
    def setUp(self):
        _seen(AGENT)
        self.person = get_user_model().objects.create_user("op", password="x" * 20)

    def test_no_credential_blocks_or_frees_a_client(self):
        granted = frozenset(Capability)
        for credential in (
            Principal(AGENT, "mcp", granted, granted=granted),
            Principal("example-client", "api", granted, granted=granted),
            cli_principal(),
        ):
            for state in (True, False):
                with self.subTest(interface=credential.interface, blocked=state):
                    result = execute_capability(
                        SET_AGENT_BLOCK, keyed({"blocked": state}), principal=credential, target=AGENT
                    )

                    self.assertEqual(result["error"]["code"], "forbidden")
        self.assertFalse(blocked(AGENT))

    def test_a_client_hq_has_never_seen_is_not_found(self):
        result = execute_capability(
            SET_AGENT_BLOCK, keyed({"blocked": True}), principal=web_principal(self.person), target="example-unseen"
        )

        self.assertEqual(result["error"]["code"], "not_found")
        self.assertFalse(AgentIdentity.objects.filter(client_id="example-unseen").exists())


class BlockControlTests(TestCase):
    def setUp(self):
        _seen(AGENT)
        self.person = get_user_model().objects.create_user("op", password="x" * 20)
        self.client.force_login(self.person)

    def _set(self, state: str, client_id: str = AGENT):
        return self.client.post(reverse("agent_block"), form_keyed({"client_id": client_id, "blocked": state}))

    def _said(self) -> list[str]:
        return list(
            AuditLog.objects.filter(metadata__operation=SET_AGENT_BLOCK)
            .order_by("id")
            .values_list("message", flat=True)
        )

    def test_blocking_holds_and_names_who_and_when(self):
        self._set("1")

        row = AgentIdentity.objects.get(client_id=AGENT)
        self.assertEqual(row.blocked_by, self.person)
        self.assertIsNotNone(row.blocked_at)
        event = AuditLog.objects.get(metadata__operation=SET_AGENT_BLOCK)
        self.assertEqual(event.user, self.person)

    def test_a_double_click_stays_blocked_and_both_directions_are_recorded(self):
        self._set("1")
        self._set("1")
        self._set("0")

        self.assertFalse(blocked(AGENT))
        self.assertEqual(len(self._said()), 2)
        self.assertIsNone(AgentIdentity.objects.get(client_id=AGENT).blocked_by)

    def test_an_operator_without_the_grant_is_refused(self):
        lacking = Principal("op", "web", OPERATOR_CAPABILITIES - {Capability.GOVERN_AGENTS})
        with mock.patch("hq.platform.application.web_writes.web_principal", return_value=lacking):
            response = self._set("1")

        self.assertEqual(response.status_code, 403)
        self.assertFalse(blocked(AGENT))

    def test_an_unrecognized_state_or_client_changes_nothing(self):
        self.assertEqual(self._set("yes").status_code, 400)
        self.assertEqual(self._set("1", client_id="").status_code, 400)
        self.assertEqual(self._set("1", client_id="example-unseen").status_code, 404)
        self.assertFalse(blocked(AGENT))

    def test_a_get_cannot_block(self):
        response = self.client.get(reverse("agent_block"), {"client_id": AGENT, "blocked": "1"})

        self.assertEqual(response.status_code, 405)
        self.assertFalse(blocked(AGENT))
