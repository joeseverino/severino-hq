"""An agent refused a view on MCP, outside a tool call, leaves the Denied row a refused call does."""

from asgiref.sync import async_to_sync
from django.test import TestCase
from mcp import Client
from mcp.shared.exceptions import MCPError

from hq.platform.core.models import AuditLog
from hq.platform.core.testing import MCP_READER

from ..identity import set_principal
from .registry import KIND, example_kind, served, with_kind


def _denials():
    return [
        {key: row.metadata[key] for key in ("interface", "actor", "reason", "authenticated")}
        for row in AuditLog.objects.filter(action=AuditLog.Action.DENIED).order_by("id")
    ]


def _on_mcp(steps):
    async def run(server):
        async with Client(server.server) as client:
            return await steps(client)

    with set_principal(MCP_READER), served(with_kind(example_kind())) as server:
        return async_to_sync(run)(server)


class RecordedRefusalTests(TestCase):
    def test_a_view_refused_on_mcp_is_recorded_as_a_refused_call_is(self):
        async def steps(client):
            with self.assertRaises(MCPError) as raised:
                await client.read_resource(f"hq://{KIND}/one")
            return raised.exception.error.data

        self.assertEqual(_on_mcp(steps), {"code": "forbidden"})
        self.assertEqual(
            _denials(), [{"interface": "mcp", "actor": MCP_READER.actor, "reason": "forbidden", "authenticated": True}]
        )
