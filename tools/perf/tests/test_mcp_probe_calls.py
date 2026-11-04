"""The calls audit:mcp's probe makes are answered by the server as it is served now.

The probe builds its tool calls and its slow tool by hand, so a change to a
tool's arguments or to ``Tool`` would otherwise first fail at audit:mcp. Here
the probe's own code makes each call of the served list, and its slow tool
runs on the server, so that change fails in the fast gate.
"""

from asgiref.sync import async_to_sync
from django.test import TestCase
from mcp import Client

from hq.platform.application.security import mcp_principal
from hq.platform.mcp.identity import set_principal
from hq.platform.mcp.server import ToolServer, mcp
from tools.perf.mcp_probe import CATALOG, LIST, QUEUE, SLOW, Slow, answered, slow_tool


class ProbeCallTests(TestCase):
    def test_each_call_the_probe_makes_of_the_served_list_is_answered(self) -> None:
        # The probe stops with SystemExit on a refused call, naming it.
        with set_principal(mcp_principal()):
            listed, counted = async_to_sync(answered)(mcp, lambda: None)

        self.assertTrue(listed["tools"], LIST)
        for name in (CATALOG, QUEUE):
            self.assertGreater(counted[name], 0, name)

    def test_the_slow_tool_is_a_tool_the_server_runs(self) -> None:
        slow = Slow()
        # Released from the start, so the call answers at once.
        slow.release.set()

        async def call():
            async with Client(ToolServer({SLOW: slow_tool(slow)}).server) as client:
                return await client.call_tool(SLOW, {})

        with set_principal(mcp_principal()):
            answer = async_to_sync(call)()

        self.assertFalse(answer.is_error, answer.content)
        self.assertEqual(slow.most, 1)
