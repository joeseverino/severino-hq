"""A person's block on one agent: asked on its every call at both doors, set only by a person."""

from unittest import mock

from django.db import DatabaseError
from django.test import TestCase, override_settings
from django.urls import reverse

from hq.platform.api.tests.tokens import ISSUER, RESOURCE, _serving, _token
from hq.platform.application.plugin_testing import form_keyed
from hq.platform.core.models import AgentIdentity, AuditLog
from hq.platform.core.testing import operator

from ..agent_access import AGENT_BLOCKED, SET_AGENT_BLOCK, blocked
from ..capabilities import CAPABILITY_AUDIT_TYPE
from ..security import (
    agent_principal,
)

AGENT = "example-agent"
OTHER = "example-other-agent"
READ = frozenset({"read"})


def _seen(*clients: str, person_key: str = "") -> None:
    for client in clients:
        AgentIdentity.objects.create(client_id=client, person_key=person_key, interfaces=["mcp"], granted=["read"])


def _blocked(client: str) -> bool:
    return blocked(agent_principal(client, "mcp", READ))


class RefusalTests(TestCase):
    def test_an_unreadable_block_refuses(self):
        with mock.patch.object(AgentIdentity.objects, "filter", side_effect=DatabaseError("locked")):
            self.assertTrue(_blocked(OTHER))


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


class BlockControlTests(TestCase):
    def setUp(self):
        _seen(AGENT)
        self.person = operator(self.client, "op")

    def _set(self, state: str, client_id: str = AGENT):
        return self.client.post(reverse("agent_block"), form_keyed({"client_id": client_id, "blocked": state}))

    def _said(self) -> list[str]:
        return list(
            # The changes, apart from the act of a click that changed nothing.
            AuditLog.objects.filter(metadata__operation=SET_AGENT_BLOCK)
            .exclude(object_type=CAPABILITY_AUDIT_TYPE)
            .order_by("id")
            .values_list("message", flat=True)
        )

    def test_a_double_click_stays_blocked_and_both_directions_are_recorded(self):
        self._set("1")
        self._set("1")
        self._set("0")

        self.assertFalse(_blocked(AGENT))
        self.assertEqual(len(self._said()), 2)
        self.assertIsNone(AgentIdentity.objects.get(client_id=AGENT).blocked_by)

    def test_an_unrecognized_state_or_client_changes_nothing(self):
        self.assertEqual(self._set("yes").status_code, 400)
        self.assertEqual(self._set("1", client_id="").status_code, 400)
        self.assertEqual(self._set("1", client_id="example-unseen").status_code, 404)
        self.assertFalse(_blocked(AGENT))
