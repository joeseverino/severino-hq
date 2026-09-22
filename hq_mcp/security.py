"""Fail-closed ASGI boundary for the tailnet-only MCP endpoint."""

from __future__ import annotations

import ipaddress
import logging
import secrets
from collections.abc import Callable, Iterable

from starlette.datastructures import Headers
from starlette.responses import JSONResponse

from hq_mcp.identity import reset_principal, set_principal

MIN_TOKEN_LENGTH = 32

logger = logging.getLogger("severino.mcp")


def _looks_like_jwt(value: str) -> bool:
    """Whether to try the verifier rather than the shared-secret compare.

    A shape test, never a trust decision: a value that passes still has to
    verify. It exists so that presenting the legacy bearer does not spend a
    signature check, and so that a rejected JWT is not also compared against
    the shared secret.
    """

    return value.count(".") == 2


def _normalize_host(value: str) -> str:
    """Normalize hostname, IPv4, or bracketed IPv6 Host header values."""

    value = value.strip().lower()
    if value.startswith("["):
        closing = value.find("]")
        if closing == -1:
            return ""
        suffix = value[closing + 1 :]
        if suffix and not (suffix.startswith(":") and suffix[1:].isdigit()):
            return ""
        return value[1:closing]
    if value.count(":") == 1:
        host, port = value.rsplit(":", 1)
        if not port.isdigit():
            return ""
        value = host
    return value.rstrip(".")


class MCPBoundary:
    def __init__(
        self,
        app,
        *,
        token: str,
        allowed_hosts: Iterable[str],
        allowed_networks: Iterable[str],
        allowed_origins: Iterable[str] = (),
        verifier: Callable[[str], object] | None = None,
    ):
        self.app = app
        self.token = token
        # Injected rather than imported so this module keeps knowing only about
        # ASGI and bytes. The adapter that owns token verification lives in
        # `hq_api`, and a boundary that imported it would make the network gate
        # depend on the identity provider being configured at all.
        self.verifier = verifier
        self.allowed_hosts = {
            normalized
            for host in allowed_hosts
            if (normalized := _normalize_host(host))
        }
        self.allowed_networks = tuple(
            ipaddress.ip_network(network) for network in allowed_networks
        )
        self.allowed_origins = set(allowed_origins)
        # A deployment may present either credential, so either one being
        # usable is enough to serve. The network gates are not optional in
        # either case: they are what makes this endpoint unreachable rather
        # than merely unauthorized.
        self.enabled = (
            (len(token) >= MIN_TOKEN_LENGTH or verifier is not None)
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

        host = _normalize_host(headers.get("host", ""))
        if host not in self.allowed_hosts:
            await self._deny(scope, receive, send, 400, "invalid_host")
            return

        origin = headers.get("origin")
        if origin and origin not in self.allowed_origins:
            await self._deny(scope, receive, send, 403, "invalid_origin")
            return

        scheme, separator, supplied = headers.get("authorization", "").partition(" ")
        if not (separator and scheme.lower() == "bearer" and supplied):
            await self._unauthorized(scope, receive, send)
            return

        authenticated, principal = self._authenticate(supplied)
        if not authenticated:
            await self._unauthorized(scope, receive, send)
            return

        # Bound to this request and unbound when it ends, so a task that
        # outlives the response cannot keep acting as whoever last called.
        reset = set_principal(principal)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_principal(reset)

    def _authenticate(self, supplied: str):
        """Authenticate one bearer.

        Returns `(authenticated, principal)`. A `None` principal on success
        means the shared bearer was presented: it names no one, so the caller
        is the deployment-configured service account it has always been.

        The two credentials are tried exclusively rather than in sequence. A
        value shaped like a JWT that fails verification is rejected outright
        instead of also being compared against the shared secret, so a rejected
        token cannot be used to probe it.
        """

        if self.verifier is not None and _looks_like_jwt(supplied):
            try:
                return True, self.verifier(supplied)
            except Exception as exc:  # noqa: BLE001 - a boundary fails closed
                # Deliberately broad. The verifier may raise anything its
                # library does, and any of it means "not authenticated" here.
                # The reason is logged, never returned: a rejected token's
                # error text tells its holder what to change about the next
                # one.
                logger.warning(
                    "Rejected an MCP access token: %s",
                    exc,
                    extra={"event": "mcp.token.rejected"},
                )
                return False, None

        if len(self.token) >= MIN_TOKEN_LENGTH and secrets.compare_digest(
            supplied, self.token
        ):
            return True, None
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
