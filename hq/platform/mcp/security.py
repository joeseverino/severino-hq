"""Fail-closed ASGI boundary for the tailnet-only MCP endpoint and its resource metadata.

The bearer is read and the 401 challenge written by the MCP SDK's own
middleware (``BearerAuthBackend``, ``RequireAuthMiddleware``); who a token is
comes from HQ's one admission, which the machine API asks too. What stays
HQ's own here, and why:

- The tailnet, Host and Origin gates. The SDK's ``TransportSecuritySettings``
  matches the raw Host header as written, so it refuses a case or port
  spelling HQ admits, and its ``host:*`` form admits a port that is not a
  number, which HQ refuses. It also runs inside the transport, after
  authentication, and never in front of the metadata route.
- The 403 for a token that verified but grants nothing HQ can use. The SDK's
  scope check asks for every scope it is given, where HQ asks for any grant,
  and its challenge carries no ``scope`` parameter.
- A body read as strict JSON (``strict_json``). The SDK's parsers keep the
  last of a repeated field and accept NaN and Infinity, and take no hook.
"""

import asyncio
import ipaddress
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from functools import cached_property
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from starlette.datastructures import Headers
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send
lazy import jwt
lazy from django.conf import settings
lazy from mcp import types
lazy from mcp.server.auth.handlers.metadata import ProtectedResourceMetadataHandler
lazy from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend, RequireAuthMiddleware
lazy from mcp.server.auth.provider import AccessToken
lazy from mcp.server.transport_security import DEFAULT_MAX_REQUEST_BODY_SIZE, RequestBodyLimitMiddleware
lazy from mcp.shared.auth import ProtectedResourceMetadata
lazy from pydantic import AnyHttpUrl
lazy from starlette.authentication import AuthCredentials, UnauthenticatedUser
lazy from starlette.requests import HTTPConnection
lazy from starlette.routing import request_response

from hq.platform.application import request_context
from hq.platform.core import strict_json
from hq.platform.core.network import strict_host
from hq.platform.core.outside_requests import thread_of_its_own
from hq.platform.mcp.identity import set_principal
lazy from hq.platform.application.agent_admission import Throttled, Unadmitted, carrying, challenge
lazy from hq.platform.application.security import MCP_INTERFACE, Principal, agent_grantable
lazy from hq.platform.core.audit import audit_operation
lazy from hq.platform.mcp import listen

logger = logging.getLogger("severino.mcp")

# RFC 9728 section 3.1: a resource's metadata is at this prefix, inserted
# between the resource identifier's origin and its path.
WELL_KNOWN = "/.well-known/oauth-protected-resource"

# A bearer and the peer that sent it, to the principal it names or why none.
Admit = Callable[[str, str], Awaitable[Any]]

# The routing header the 2026-07-28 transport holds equal to the body's method
# before any handler runs, so the door knows a listen before reading the body.
METHOD_HEADER = "mcp-method"
LISTEN = "subscriptions/listen"


class TailnetGate:
    """The network gates every MCP route stands behind: a tailnet peer, an allowed Host, no foreign Origin.

    They make the endpoint unreachable rather than merely unauthorized, so
    without an allowed host or network it is off.
    """

    def __init__(
        self,
        *,
        allowed_hosts: Iterable[str],
        allowed_networks: Iterable[str],
        allowed_origins: Iterable[str] = (),
    ) -> None:
        self.allowed_hosts = {normalized for host in allowed_hosts if (normalized := strict_host(host))}
        self.allowed_networks = tuple(ipaddress.ip_network(network) for network in allowed_networks)
        self.allowed_origins = set(allowed_origins)
        self.enabled = bool(self.allowed_hosts) and bool(self.allowed_networks)

    async def refused(self, scope: Scope, receive: Receive, send: Send) -> bool:
        """Answer a request the gates refuse, and say whether it was refused."""

        headers = Headers(scope=scope)
        if not self._tailnet_peer(scope):
            await _deny(scope, receive, send, 404, "not_found")
        elif strict_host(headers.get("host", "")) not in self.allowed_hosts:
            await _deny(scope, receive, send, 400, "invalid_host")
        elif (origin := headers.get("origin")) and origin not in self.allowed_origins:
            await _deny(scope, receive, send, 403, "invalid_origin")
        else:
            return False
        return True

    def _tailnet_peer(self, scope: Scope) -> bool:
        client = scope.get("client")
        if not client:
            return False
        try:
            address = ipaddress.ip_address(client[0])
        except ValueError:
            return False
        return any(address in network for network in self.allowed_networks)


class MCPBoundary:
    """/api/mcp/: the gates, then a token HQ's admission names, then the server."""

    def __init__(self, app: ASGIApp, *, gate: TailnetGate, resource: str = "", admit: Admit | None = None) -> None:
        self.app = app
        self.gate = gate
        self.resource = resource
        # Injected rather than imported so this module keeps knowing only
        # about ASGI and bytes, and the network gate does not depend on the
        # identity provider being configured at all. Without it nothing can
        # authenticate, so the endpoint is off.
        self.admit = admit
        self.enabled = admit is not None and gate.enabled

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http" or not self.enabled:
            await _deny(scope, receive, send, 404, "not_found")
            return
        if await self.gate.refused(scope, receive, send):
            return
        # This mount is beside Django, so its synchronous work (the
        # admission, the brake, the tool) gets a thread of this request's own
        # here, and one agent's slow call never queues another's. A listen
        # stream waits on events for as long as its token lasts, so it gives
        # its thread and connections back once admitted, and its own brakes
        # each take a pooled thread for the moment they are asked (listen.py).
        if Headers(scope=scope).get(METHOD_HEADER) == LISTEN:
            async with thread_of_its_own():
                presented = await self._presented(scope)
            await self._serve(scope, receive, send, presented, listens=True)
            return
        async with thread_of_its_own():
            await self._serve(scope, receive, send, await self._presented(scope))

    async def _presented(self, scope: Scope) -> _Presented:
        presented = _Presented(self._admitted, (scope.get("client") or ("",))[0])
        presented.authenticated = await BearerAuthBackend(presented).authenticate(HTTPConnection(scope))
        if presented.outcome is None:
            # No bearer reached the verifier; the admission still records it.
            await presented.verify_token("")
        return presented

    async def _serve(
        self, scope: Scope, receive: Receive, send: Send, presented: _Presented, *, listens: bool = False
    ) -> None:
        authenticated = presented.authenticated
        outcome = presented.outcome
        if authenticated is None and isinstance(outcome, Unadmitted) and outcome.verified:
            await _insufficient_scope(scope, receive, send, metadata_url(self.resource), outcome)
            return

        scope["auth"], scope["user"] = authenticated or (AuthCredentials(), UnauthenticatedUser())

        async def bound(scope: Scope, receive: Receive, send: Send) -> None:
            # Bound to this request and unbound when it ends, so a task that
            # outlives the response cannot keep acting as whoever last called.
            # The caller's own request is bound beside it, for the read that
            # describes how it arrived. A brake on the caller, and a rate it
            # is past, are answered at each tool call (server.py), so a paused
            # or throttled agent keeps its connection.
            arrived = request_context.from_scope(scope, allowed_hosts=self.gate.allowed_hosts)
            # Admitted, so each row from here names the agent, a refusal
            # included, under the door's operation id.
            with (
                carrying(outcome) as caller,
                set_principal(caller),
                request_context.bind(arrived),
                audit_operation(operation=MCP_INTERFACE, principal=caller),
                listen.ending_at(self._stream_ends(presented.token) if listens else None),
            ):
                await self._strict(scope, receive, send)

        await RequireAuthMiddleware(bound, [], self._metadata)(scope, receive, send)

    def _stream_ends(self, bearer: str) -> float:
        """When a listen stream this bearer opens ends, on the loop's clock: when
        its admission would refuse the token. One that cannot be read ends at once."""

        try:
            remaining = token_expiry(bearer) - time.time()
        except Exception:  # noqa: BLE001 - a stream whose end is unknown is not kept open
            remaining = 0
        return asyncio.get_running_loop().time() + remaining

    @cached_property
    def _strict(self) -> ASGIApp:
        # The SDK's own limit buffers the body, so it is read here whole and
        # never past the size the SDK would accept.
        return RequestBodyLimitMiddleware(_StrictBody(self.app), DEFAULT_MAX_REQUEST_BODY_SIZE)

    @cached_property
    def _metadata(self) -> AnyHttpUrl | None:
        return AnyHttpUrl(metadata_url(self.resource)) if self.resource else None

    async def _admitted(self, bearer: str, source: str) -> Any:
        """HQ's admission of one bearer. Fails closed: an admission that
        cannot answer is an unauthenticated request."""

        assert self.admit is not None
        try:
            return await self.admit(bearer, source)
        except Exception as exc:  # noqa: BLE001 - a boundary fails closed
            logger.warning("A token could not be admitted; refusing: %s", exc, extra={"event": "mcp.token.unadmitted"})
            return None


class _StrictBody:
    """A POST whose body is not strict JSON, answered as the SDK answers one that is not JSON at all."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["method"] != "POST":
            await self.app(scope, receive, send)
            return
        first = await receive()
        # A body that is empty, or that the client stopped sending, is left
        # to the SDK, which refuses either.
        body = first.get("body", b"") if first["type"] == "http.request" and not first.get("more_body") else b""
        if body:
            try:
                strict_json.loads(body)
            except ValueError, RecursionError:
                await _parse_error(scope, receive, send)
                return
        replayed: deque[Message] = deque([first])

        async def replay() -> Message:
            return replayed.popleft() if replayed else await receive()

        await self.app(scope, replay, send)


async def _parse_error(scope: Scope, receive: Receive, send: Send) -> None:
    """JSON-RPC's parse error, framed as the SDK's transport writes one: no id, and no parser's words."""

    error = types.JSONRPCError(
        jsonrpc="2.0", id=None, error=types.ErrorData(code=types.PARSE_ERROR, message="Parse error")
    )
    response = JSONResponse(
        error.model_dump(mode="json", by_alias=True, exclude_unset=True),
        status_code=400,
        headers={"Cache-Control": "private, no-store"},
    )
    await response(scope, receive, send)


class _Presented:
    """The SDK's ``TokenVerifier`` for one request: the token it read, named by HQ's admission.

    Made per request because the admission records a refused token against
    the peer that sent it, which the SDK's verifier is never told.
    """

    def __init__(self, admitted: Callable[[str, str], Awaitable[Any]], source: str) -> None:
        self._admitted = admitted
        self.source = source
        self.outcome: Any = None
        self.token = ""
        self.authenticated: tuple[AuthCredentials, Any] | None = None

    async def verify_token(self, token: str) -> AccessToken | None:
        self.outcome = await self._admitted(token, self.source)
        self.token = token
        # A throttled agent's token is good: the rate is answered in the protocol.
        principal = self.outcome.principal if isinstance(self.outcome, Throttled) else self.outcome
        if not isinstance(principal, Principal):
            return None
        # No expiry: the admission checked it, with the clock skew the
        # machine API allows, and the SDK's own check would allow none.
        return AccessToken(token=token, client_id=principal.actor, scopes=sorted(principal.granted))


class ResourceMetadata:
    """RFC 9728 metadata for the resource both agent doors serve, behind the gates of /api/mcp/.

    It names the identity provider as the one authorization server and the
    API resource as what a token is asked for, which is the audience the
    admission checks on either door. It is served at ``metadata_url``'s path.
    """

    def __init__(self, *, gate: TailnetGate, resource: str, authorization_server: str) -> None:
        self.gate = gate
        self.resource = resource
        self.authorization_server = authorization_server
        self.enabled = gate.enabled and bool(resource) and bool(authorization_server)

    @cached_property
    def _answer(self) -> ASGIApp:
        # Validated from text, so the model keeps an empty path empty: the
        # resource it names is compared with each token's `aud` as a string.
        metadata = ProtectedResourceMetadata.model_validate(
            {
                "resource": self.resource,
                "authorization_servers": [self.authorization_server],
                "resource_name": "Severino HQ",
                # A native sign-in asks for what is listed here, and the
                # provider grants only what was asked for.
                "scopes_supported": token_scopes(),
            }
        )
        return request_response(ProtectedResourceMetadataHandler(metadata).handle)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self.enabled:
            await _deny(scope, receive, send, 404, "not_found")
            return
        if await self.gate.refused(scope, receive, send):
            return
        if scope["method"] not in {"GET", "HEAD"}:
            await Response(status_code=405, headers={"Allow": "GET, HEAD"})(scope, receive, send)
            return
        await self._answer(scope, receive, send)


def token_expiry(bearer: str) -> float:
    """When the admission would refuse ``bearer``: its ``exp``, with the clock skew the admission allows.

    Read only from a bearer the admission verified, so its signature is not checked again.
    """

    claims = jwt.decode(bearer, options={"verify_signature": False})
    return float(claims["exp"]) + settings.SEVERINO_API_LEEWAY_SECONDS


def token_scopes() -> list[str]:
    """The permission keys an agent's token could carry: every capability HQ
    and its extensions declare, less those held only by a person."""

    return sorted(agent_grantable())


def metadata_url(resource: str) -> str:
    """Where the metadata of ``resource`` is, as the SDK's ``build_resource_metadata_url`` places it."""

    parts = urlsplit(resource)
    path = "" if parts.path == "/" else parts.path
    return urlunsplit((parts.scheme, parts.netloc, f"{WELL_KNOWN}{path}", "", ""))


async def _insufficient_scope(scope: Scope, receive: Receive, send: Send, location: str, refused: Unadmitted) -> None:
    """A verified token with no grant HQ can use: the spec's step-up challenge, naming the least scope."""

    header = challenge(
        ("error", "insufficient_scope"),
        ("scope", refused.scope),
        ("resource_metadata", location),
        ("error_description", refused.reason),
    )
    response = JSONResponse(
        {"error": "insufficient_scope", "error_description": refused.reason},
        status_code=403,
        headers={"WWW-Authenticate": header, "Cache-Control": "private, no-store"},
    )
    await response(scope, receive, send)


async def _deny(scope: Scope, receive: Receive, send: Send, status: int, error: str) -> None:
    response = JSONResponse(
        {"error": error},
        status_code=status,
        headers={"Cache-Control": "private, no-store"},
    )
    await response(scope, receive, send)
