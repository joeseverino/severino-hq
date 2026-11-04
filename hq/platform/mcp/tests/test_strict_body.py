"""/api/mcp/ reads a request body as the machine API does: a repeated field or a
bare NaN or Infinity is refused as JSON-RPC's parse error, before the SDK's own
parser could keep the last copy or a value no other parser agrees on."""

from itertools import pairwise
from unittest import mock

from asgiref.sync import async_to_sync
from django.test import TestCase

from hq.platform.application.security import Capability, Principal

from ..security import MCPBoundary, TailnetGate
from ..server import mcp
from .test_mcp import admitting

AGENT = Principal("example-agent", "mcp", frozenset({Capability.READ}))
HEADERS = {
    "Authorization": "Bearer header.payload.signature",
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}
# JSON-RPC 2.0's code for a body that is not JSON.
PARSE_ERROR = -32700
TOOL = "hq.platform.mcp.services.list_application_resource"


def _call(arguments: str) -> bytes:
    return (
        '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"list_resource","arguments":' + arguments + "}}"
    ).encode()


def _cut(body: bytes, *at: int) -> list[bytes]:
    """``body`` in pieces, cut at each of the offsets ``at``."""

    edges = (0, *at, len(body))
    return [body[start:end] for start, end in pairwise(edges)]


class StrictBodyTests(TestCase):
    def _post(self, *bodies):
        """Each of ``bodies`` posted raw to a served MCP application of this test's own.

        A body given as a list of pieces is sent one ASGI message a piece, as a
        client streaming its request is read.
        """

        import httpx2
        from starlette.applications import Starlette
        from starlette.routing import Mount

        served = mcp.served()
        gate = TailnetGate(allowed_hosts=("testserver",), allowed_networks=("100.64.0.0/10",))
        application = Starlette(
            routes=[Mount("/api/mcp", app=MCPBoundary(served.application, gate=gate, admit=admitting(lambda _: AGENT)))]
        )

        async def streamed(pieces):
            for piece in pieces:
                yield piece

        async def run():
            transport = httpx2.ASGITransport(app=application, client=("100.64.0.10", 50000))
            async with (
                served.sessions.run(),
                httpx2.AsyncClient(transport=transport, base_url="http://testserver") as client,
            ):
                return [
                    await client.post(
                        "/api/mcp/", headers=HEADERS, content=streamed(body) if isinstance(body, list) else body
                    )
                    for body in bodies
                ]

        return async_to_sync(run)()

    def test_a_repeated_field_or_a_constant_is_a_parse_error_and_reaches_no_tool(self):
        reached = []
        bodies = (
            _call('{"name":"projects","name":"assets"}'),
            _call('{"name":"projects","limit":NaN}'),
            _call('{"name":"projects","limit":-Infinity}'),
            b'{"jsonrpc":"2.0","id":1,"id":2,"method":"tools/list"}',
        )
        with mock.patch(TOOL, side_effect=lambda *a, **k: reached.append(a) or {"items": []}):
            responses = self._post(*bodies)

        for body, response in zip(bodies, responses, strict=True):
            with self.subTest(body=body):
                self.assertEqual(response.status_code, 400, response.text)
                answer = response.json()
                self.assertEqual(answer["jsonrpc"], "2.0")
                self.assertIsNone(answer["id"])
                self.assertEqual(answer["error"]["code"], PARSE_ERROR)
        self.assertEqual(reached, [])

    def test_a_body_sent_in_pieces_is_judged_whole(self):
        # Each refused body is cut inside the repeated field or the constant, so
        # no single piece shows the fault and no piece is JSON on its own.
        repeated = _call('{"name":"projects","name":"assets"}')
        constant = _call('{"name":"projects","limit":NaN}')
        refused = (
            _cut(repeated, repeated.index(b"proj") + 2, repeated.rindex(b'"name"') + 3),
            _cut(constant, constant.index(b"NaN") + 2),
        )
        accepted = _call('{"name":"projects"}')
        reached = []
        with mock.patch(TOOL, side_effect=lambda name, *a, **k: reached.append(name) or {"items": []}):
            responses = self._post(*refused, _cut(accepted, accepted.index(b"projects") + 3))

        for pieces, response in zip(refused, responses[:-1], strict=True):
            with self.subTest(body=b"".join(pieces)):
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(response.json()["error"]["code"], PARSE_ERROR)
        self.assertEqual(responses[-1].status_code, 200, responses[-1].text)
        self.assertEqual(reached, ["projects"])

    def test_a_body_read_once_each_still_reaches_its_tool(self):
        reached = []
        with mock.patch(TOOL, side_effect=lambda name, *a, **k: reached.append(name) or {"items": []}):
            (response,) = self._post(_call('{"name":"projects"}'))

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(reached, ["projects"])

    def test_a_body_past_the_sdks_limit_is_refused_before_it_is_read_whole(self):
        from mcp.server.transport_security import DEFAULT_MAX_REQUEST_BODY_SIZE

        padding = " " * DEFAULT_MAX_REQUEST_BODY_SIZE
        (response,) = self._post(_call('{"name":"projects"}' + padding))

        self.assertEqual(response.status_code, 413)
