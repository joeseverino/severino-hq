"""An agent refused on MCP leaves the Denied row the same refusal leaves on the API."""

from asgiref.sync import async_to_sync
from django.test import TestCase, override_settings
from mcp import Client
from mcp.shared.exceptions import MCPError

from hq.platform.api.tests.tokens import ISSUER, RESOURCE, _serving, _token
from hq.platform.application.security import Capability, Principal
from hq.platform.core.models import AuditLog

from ..identity import set_principal
from .registry import KIND, example_kind, registry_patched, served, with_kind

READER = Principal("example-reader", "mcp", frozenset({Capability.READ}))


def _denials():
    return [
        {key: row.metadata[key] for key in ("interface", "actor", "reason", "authenticated")}
        for row in AuditLog.objects.filter(action=AuditLog.Action.DENIED).order_by("id")
    ]


def _on_mcp(steps):
    async def run(server):
        async with Client(server.server) as client:
            return await steps(client)

    with set_principal(READER), served(with_kind(example_kind())) as server:
        return async_to_sync(run)(server)


@override_settings(OIDC_ISSUER=ISSUER, SEVERINO_API_RESOURCE=RESOURCE, OIDC_RP_SIGN_ALGO="RS256")
class RecordedRefusalTests(TestCase):
    def test_the_api_records_the_refusal(self):
        with _serving(), registry_patched(with_kind(example_kind())):
            response = self.client.get(
                f"/api/v2/resources/{KIND}/", HTTP_AUTHORIZATION=f"Bearer {_token(scope='read')}"
            )

        self.assertEqual(response.json()["error"]["code"], "forbidden")
        self.assertEqual(
            _denials(),
            [{"interface": "api", "actor": "example-automation", "reason": "forbidden", "authenticated": True}],
        )

    def test_a_tool_call_refused_on_mcp_is_recorded_as_the_api_records_it(self):
        async def steps(client):
            return await client.call_tool("list_resource", {"name": KIND})

        answered = _on_mcp(steps)

        self.assertTrue(answered.is_error)
        self.assertEqual(answered.structured_content["error"]["code"], "forbidden")
        self.assertEqual(
            _denials(), [{"interface": "mcp", "actor": READER.actor, "reason": "forbidden", "authenticated": True}]
        )

    def test_a_read_refused_on_mcp_is_recorded_as_the_api_records_it(self):
        async def steps(client):
            with self.assertRaises(MCPError) as raised:
                await client.read_resource(f"hq://{KIND}/one")
            return raised.exception.error.data

        self.assertEqual(_on_mcp(steps), {"code": "forbidden"})
        self.assertEqual(
            _denials(), [{"interface": "mcp", "actor": READER.actor, "reason": "forbidden", "authenticated": True}]
        )
