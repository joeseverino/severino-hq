"""A listen stream (``subscriptions/listen``) through the door: opened under
the brake a tool call is under, told when its agent's tools change, carrying
only what HQ publishes, holding no thread, and ended by its token's expiry and
its agent's limit. What the brakes hold back once it is open is
``test_listen_bus``'s.
"""

import asyncio
import contextlib
import json
import threading
from dataclasses import replace
from datetime import UTC, datetime
from unittest import mock

import jwt
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.test import TransactionTestCase, override_settings

from hq.platform.api.tests.tokens import _token
from hq.platform.application import agent_limits, agent_registry, linked_accounts, roles
from hq.platform.application.agent_access import AGENTS_PAUSED
from hq.platform.application.security import Capability, Principal
from hq.platform.core.outside_requests import connections_of_its_own

from ..security import MCPBoundary, TailnetGate
from ..server import mcp
from .test_mcp import admitting

PAUSED = "hq.platform.application.agent_access.paused_interfaces"
AGENT = Principal("example-agent", "mcp", frozenset({Capability.READ}), granted=frozenset({"read"}))
OTHER = Principal("example-other", "mcp", frozenset({Capability.READ}), granted=frozenset({"read"}))
# An agent a person signed in, whose token holds what that person's role holds.
SIGNED_IN = Principal(
    "example-assistant", "mcp", frozenset({Capability.READ}), granted=frozenset({"read"}), person_key="example-key"
)
AGENTS = {AGENT.actor: AGENT, OTHER.actor: OTHER, SIGNED_IN.actor: SIGNED_IN}
MODERN = "2026-07-28"
LISTEN = "subscriptions/listen"
LIST_CHANGED = "notifications/tools/list_changed"
ACKNOWLEDGED = "notifications/subscriptions/acknowledged"
SUBSCRIPTION_ID = "io.modelcontextprotocol/subscriptionId"


def _listen(ident, **notifications):
    meta = {"io.modelcontextprotocol/protocolVersion": MODERN, "io.modelcontextprotocol/clientCapabilities": {}}
    return {
        "jsonrpc": "2.0",
        "id": ident,
        "method": LISTEN,
        "params": {"notifications": notifications or {"toolsListChanged": True}, "_meta": meta},
    }


class Reply:
    """What the door sends one request, read a message at a time as it arrives."""

    def __init__(self, sent: asyncio.Queue) -> None:
        self._sent = sent
        self.status = 0
        self.headers: dict[bytes, bytes] = {}
        self._buffer = b""
        self._json = False
        self.ended = False

    async def message(self) -> dict | None:
        """The next JSON-RPC message, or None once the response has ended."""

        while True:
            frame = self._frame()
            if frame is not None:
                return frame
            if self.ended:
                return None
            sent = await self._sent.get()
            if sent["type"] == "http.response.start":
                self.status = sent["status"]
                self.headers = dict(sent["headers"])
                self._json = self.headers.get(b"content-type", b"").startswith(b"application/json")
                continue
            self._buffer += sent.get("body", b"")
            self.ended = not sent.get("more_body", False)

    async def rest(self) -> list[dict]:
        messages = []
        while (message := await self.message()) is not None:
            messages.append(message)
        return messages

    def sent_before_leaving(self) -> list[dict]:
        """Every message the door sent before its request ended, once it has."""

        while not self._sent.empty():
            sent = self._sent.get_nowait()
            if sent["type"] == "http.response.body":
                self._buffer += sent.get("body", b"")
        messages = []
        while (frame := self._frame()) is not None:
            messages.append(frame)
        return messages

    def _frame(self) -> dict | None:
        if self._json:
            if not self.ended or not self._buffer:
                return None
            body, self._buffer = self._buffer, b""
            return json.loads(body)
        while b"\r\n\r\n" in self._buffer:
            event, self._buffer = self._buffer.split(b"\r\n\r\n", 1)
            data = [line[len(b"data: ") :] for line in event.split(b"\r\n") if line.startswith(b"data: ")]
            if data:
                return json.loads(b"".join(data))
        return None


def _grant_changes(principal):
    """``principal``'s next token, granted one permission more, admitted on a thread of its own as the door admits one."""

    with connections_of_its_own():
        agent_registry.observe(replace(principal, granted=principal.granted | {"write_projects"}))


def _role_changes(user, role):
    """``user``'s role resolved afresh at a sign-in, on a thread of its own as a request runs."""

    with connections_of_its_own():
        roles.assign(user, role)


def _caller(bearer):
    """The agent a test's token names, as the admission would find it."""

    return AGENTS[jwt.decode(bearer, options={"verify_signature": False})["client_id"]]


def _door():
    from starlette.applications import Starlette
    from starlette.routing import Mount

    served = mcp.served()
    gate = TailnetGate(allowed_hosts=("testserver",), allowed_networks=("100.64.0.0/10",))
    door = MCPBoundary(served.application, gate=gate, admit=admitting(_caller))
    return served, Starlette(routes=[Mount("/api/mcp", app=door)])


@contextlib.asynccontextmanager
async def listening(application, principal, body, *, protocol=MODERN, **claims):
    """One listen request to ``application`` with a token for ``principal``, its reply read as it streams; the client leaves on exit."""

    raw = json.dumps(body).encode()
    sent: asyncio.Queue = asyncio.Queue()
    gone = asyncio.Event()
    delivered = False

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": raw, "more_body": False}
        await gone.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        await sent.put(message)

    headers = {
        "host": "testserver",
        "authorization": f"Bearer {_token(principal.actor, **claims)}",
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
        "mcp-protocol-version": protocol,
        "mcp-method": body["method"],
    }
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/mcp/",
        "raw_path": b"/api/mcp/",
        "root_path": "",
        "query_string": b"",
        "headers": [(name.encode(), value.encode()) for name, value in headers.items()],
        "client": ("100.64.0.10", 50000),
        "server": ("testserver", 80),
    }
    task = asyncio.create_task(application(scope, receive, send))
    try:
        yield Reply(sent)
    finally:
        gone.set()
        await task


def _run(steps):
    served, application = _door()

    async def run():
        async with served.sessions.run():
            return await steps(application)

    return async_to_sync(run)()


class ListenStreamTests(TransactionTestCase):
    """TransactionTestCase because a stream's brakes are asked on a pooled thread, as in production."""

    def setUp(self):
        # Each agent seen once, so its next token's grant is a change.
        for principal in AGENTS.values():
            agent_registry.observe(principal)

    def test_a_stream_opens_with_its_ack_and_hears_its_tools_change(self):
        async def steps(application):
            async with listening(application, AGENT, _listen(7)) as reply:
                ack = await reply.message()
                await asyncio.to_thread(_grant_changes, AGENT)
                return reply, ack, await reply.message()

        reply, ack, changed = _run(steps)

        self.assertEqual(reply.status, 200)
        self.assertEqual(reply.headers.get(b"x-accel-buffering"), b"no", "a proxy passes the stream on as it comes")
        self.assertEqual(ack["method"], ACKNOWLEDGED)
        self.assertEqual(ack["params"]["_meta"][SUBSCRIPTION_ID], 7)
        self.assertEqual(changed["method"], LIST_CHANGED)
        self.assertEqual(changed["params"]["_meta"][SUBSCRIPTION_ID], 7)

    def test_a_grant_change_reaches_only_that_agents_stream(self):
        async def steps(application):
            async with (
                listening(application, AGENT, _listen(1)) as aimed,
                listening(application, OTHER, _listen(2)) as other,
            ):
                await aimed.message(), await other.message()
                await asyncio.to_thread(_grant_changes, AGENT)
                changed = await aimed.message()
            return changed, other.sent_before_leaving()

        changed, other = _run(steps)

        self.assertEqual(changed["method"], LIST_CHANGED)
        self.assertNotIn(LIST_CHANGED, [message.get("method") for message in other])

    def test_a_role_change_reaches_only_the_streams_of_that_persons_agents(self):
        person = get_user_model().objects.create_user("example-person")
        linked_accounts.bind_sign_in(person, SIGNED_IN.person_key)
        roles.assign(person, roles.Role.VIEWER)

        async def steps(application):
            async with (
                listening(application, SIGNED_IN, _listen(1)) as theirs,
                listening(application, AGENT, _listen(2)) as other,
            ):
                await theirs.message(), await other.message()
                await asyncio.to_thread(_role_changes, person, roles.Role.OPERATOR)
                changed = await theirs.message()
            return changed, other.sent_before_leaving()

        changed, other = _run(steps)

        self.assertEqual(changed["method"], LIST_CHANGED)
        self.assertNotIn(LIST_CHANGED, [message.get("method") for message in other])

    def test_the_ack_honors_only_what_hq_publishes(self):
        asked = {
            "toolsListChanged": True,
            "promptsListChanged": True,
            "resourcesListChanged": True,
            "resourceSubscriptions": ["hq://approvals"],
        }

        async def steps(application):
            async with listening(application, AGENT, _listen(1, **asked)) as reply:
                return await reply.message()

        ack = _run(steps)

        self.assertEqual(ack["params"]["notifications"], {"toolsListChanged": True})

    def test_a_paused_agent_is_refused_at_open_as_a_tool_call_is(self):
        async def steps(application):
            async with listening(application, AGENT, _listen(1)) as reply:
                return reply, await reply.message()

        with mock.patch(PAUSED, return_value=frozenset({"mcp", "api"})):
            reply, refused = _run(steps)

        self.assertEqual(refused["error"]["data"]["code"], AGENTS_PAUSED.code)
        self.assertEqual(refused["error"]["message"], AGENTS_PAUSED.reason)
        self.assertFalse(reply.headers.get(b"content-type", b"").startswith(b"text/event-stream"), "no stream opened")

    @override_settings(SEVERINO_API_LEEWAY_SECONDS=0)
    def test_a_stream_ends_when_its_token_expires(self):
        async def steps(application):
            # A token that expires as the stream opens: the stream ends on its own, with no client leaving.
            async with listening(application, AGENT, _listen(3), exp=datetime.now(tz=UTC)) as reply:
                return await reply.rest()

        messages = _run(steps)

        closing = messages[-1]
        self.assertEqual(closing["id"], 3)
        self.assertEqual(closing["result"]["_meta"][SUBSCRIPTION_ID], 3)

    def test_an_open_stream_holds_no_thread(self):
        async def threads_with(application, count):
            async with contextlib.AsyncExitStack() as streams:
                for ident in range(count):
                    reply = await streams.enter_async_context(listening(application, AGENT, _listen(ident)))
                    await reply.message()
                return threading.active_count()

        async def steps(application):
            # A first stream warms the pool its brakes are asked on, which every stream shares.
            await threads_with(application, 1)
            return await threads_with(application, 1), await threads_with(application, 4)

        served, application = _door()

        async def run():
            async with served.sessions.run():
                return await steps(application)

        # Served as production serves it: an event loop with no synchronous caller above it.
        one, four = asyncio.run(run())

        self.assertEqual(four, one, "three more open streams hold no thread more")

    def test_a_listen_at_an_earlier_protocol_opens_no_stream(self):
        async def steps(application):
            async with listening(application, AGENT, _listen(1), protocol="2025-06-18") as reply:
                return reply, await reply.message()

        reply, answer = _run(steps)

        self.assertIn("error", answer)
        self.assertFalse(reply.headers.get(b"content-type", b"").startswith(b"text/event-stream"))

    @override_settings(SEVERINO_AGENT_STREAMS_AT_ONCE=1)
    def test_an_agent_past_its_streams_is_refused_and_another_agent_is_not(self):
        async def steps(application):
            async with listening(application, AGENT, _listen(1)) as first:
                await first.message()
                async with (
                    listening(application, AGENT, _listen(2)) as second,
                    listening(application, OTHER, _listen(3)) as other,
                ):
                    refused, opened = await second.message(), await other.message()
                    held = agent_limits.LIMITS.listening(AGENT.actor)
            return refused, opened, held

        refused, opened, held = _run(steps)

        self.assertEqual(refused["error"]["data"]["code"], "too_many_calls")
        self.assertGreater(refused["error"]["data"]["retry_after"], 0)
        self.assertEqual(opened["method"], ACKNOWLEDGED)
        self.assertEqual(held, 1, "the refused stream holds none of its agent's streams")
        self.assertEqual(agent_limits.LIMITS.held(), 0, "a closed stream gives its slot back")
