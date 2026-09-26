"""Fail-closed ASGI boundary for the tailnet-only MCP endpoint."""

from __future__ import annotations

import ipaddress
import logging
from collections.abc import Awaitable, Callable, Iterable

from starlette.datastructures import Headers
from starlette.responses import JSONResponse

from core.network import strict_host
from hq_mcp.identity import reset_principal, set_principal

logger = logging.getLogger("severino.mcp")


class MCPBoundary:
    def __init__(
        self,
        app,
        *,
        allowed_hosts: Iterable[str],
        allowed_networks: Iterable[str],
        allowed_origins: Iterable[str] = (),
        verifier: Callable[[str], object] | None = None,
        gate: Callable[[], Awaitable[bool]] | None = None,
        on_denied: Callable[..., Awaitable[None]] | None = None,
        observer: Callable[[object], Awaitable[None]] | None = None,
    ):
        self.app = app
        # Injected rather than imported so this module keeps knowing only about
        # ASGI and bytes. The adapter that owns token verification lives in
        # `hq_api`, and a boundary that imported it would make the network gate
        # depend on the identity provider being configured at all.
        self.verifier = verifier
        # Injected, like the verifier, so this module stays free of Django.
        self.gate = gate
        self.on_denied = on_denied
        self.observer = observer
        self.allowed_hosts = {
            normalized
            for host in allowed_hosts
            if (normalized := strict_host(host))
        }
        self.allowed_networks = tuple(
            ipaddress.ip_network(network) for network in allowed_networks
        )
        self.allowed_origins = set(allowed_origins)
        # One credential: an access token from the identity provider, naming the
        # agent that holds it. Without a verifier nothing can authenticate, so
        # the endpoint is off. The network gates are not optional either: they
        # make the endpoint unreachable rather than merely unauthorized.
        self.enabled = (
            verifier is not None
            and bool(self.allowed_hosts)
            and bool(self.allowed_networks)
        )

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return

        if scope["type"] != "http" or not self.enabled:
            await self._deny(scope, receive, send, 404, "not_found")
            return

        headers = Headers(scope=scope)
        if not self._tailnet_peer(scope):
            await self._deny(scope, receive, send, 404, "not_found")
            return

        host = strict_host(headers.get("host", ""))
        if host not in self.allowed_hosts:
            await self._deny(scope, receive, send, 400, "invalid_host")
            return

        origin = headers.get("origin")
        if origin and origin not in self.allowed_origins:
            await self._deny(scope, receive, send, 403, "invalid_origin")
            return

        source = (scope.get("client") or ("",))[0]
        scheme, separator, supplied = headers.get("authorization", "").partition(" ")
        if not (separator and scheme.lower() == "bearer" and supplied):
            await self._note_denial(reason="missing_credential", source=source, authenticated=False)
            await self._unauthorized(scope, receive, send)
            return

        authenticated, principal = self._authenticate(supplied)
        if not authenticated:
            await self._note_denial(reason="invalid_credential", source=source, authenticated=False)
            await self._unauthorized(scope, receive, send)
            return

        # Before the brake, so a paused agent is still registered.
        await self._observe(principal)

        # After authentication, so only a valid caller learns agents are
        # paused; before dispatch, so a paused agent cannot list tools.
        if not await self._gate_allows():
            await self._note_denial(
                reason="agents_paused",
                actor=principal.actor,
                source=source,
            )
            await self._deny(scope, receive, send, 403, "agents_paused")
            return

        # Bound to this request and unbound when it ends, so a task that
        # outlives the response cannot keep acting as whoever last called.
        reset = set_principal(principal)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_principal(reset)

    async def _observe(self, principal) -> None:
        """Report an authenticated identity if an observer is wired. Never raises."""

        if self.observer is None or principal is None:
            return
        try:
            await self.observer(principal)
        except Exception as exc:  # noqa: BLE001 - observation never blocks
            logger.warning(
                "An identity could not be observed: %s",
                exc,
                extra={"event": "mcp.identity.unobserved"},
            )

    async def _note_denial(self, **fields) -> None:
        """Record a refusal if a recorder is wired. Never raises."""

        if self.on_denied is None:
            return
        try:
            await self.on_denied(**fields)
        except Exception as exc:  # noqa: BLE001 - recording never blocks a refusal
            logger.warning(
                "A refusal could not be recorded: %s",
                exc,
                extra={"event": "mcp.denial.unrecorded"},
            )

    async def _gate_allows(self) -> bool:
        """Whether the operator currently allows agents. Fails closed."""

        if self.gate is None:
            return True
        try:
            return bool(await self.gate())
        except Exception as exc:  # noqa: BLE001 - a brake fails closed
            logger.warning(
                "Agent access could not be checked; refusing: %s",
                exc,
                extra={"event": "mcp.gate.unavailable"},
            )
            return False

    def _authenticate(self, supplied: str):
        """Authenticate one bearer: `(authenticated, principal)`.

        Only an access token the verifier accepts authenticates, and it always
        names its agent.
        """

        try:
            return True, self.verifier(supplied)
        except Exception as exc:  # noqa: BLE001 - a boundary fails closed
            # Deliberately broad. The verifier may raise anything its library
            # does, and any of it means "not authenticated" here. The reason is
            # logged, never returned: a rejected token's error text tells its
            # holder what to change about the next one.
            logger.warning(
                "Rejected an MCP access token: %s",
                exc,
                extra={"event": "mcp.token.rejected"},
            )
            return False, None

    @staticmethod
    async def _unauthorized(scope, receive, send):
        response = JSONResponse(
            {"error": "unauthorized"},
            status_code=401,
            headers={
                "WWW-Authenticate": 'Bearer realm="Severino HQ MCP"',
                "Cache-Control": "private, no-store",
            },
        )
        await response(scope, receive, send)

    def _tailnet_peer(self, scope) -> bool:
        client = scope.get("client")
        if not client:
            return False
        try:
            address = ipaddress.ip_address(client[0])
        except ValueError:
            return False
        return any(address in network for network in self.allowed_networks)

    @staticmethod
    async def _deny(scope, receive, send, status: int, error: str):
        response = JSONResponse(
            {"error": error},
            status_code=status,
            headers={"Cache-Control": "private, no-store"},
        )
        await response(scope, receive, send)
