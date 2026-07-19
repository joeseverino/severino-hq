"""Fail-closed ASGI boundary for the tailnet-only MCP endpoint."""

from __future__ import annotations

import ipaddress
import secrets
from collections.abc import Iterable

from starlette.datastructures import Headers
from starlette.responses import JSONResponse

MIN_TOKEN_LENGTH = 32


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
    ):
        self.app = app
        self.token = token
        self.allowed_hosts = {
            normalized
            for host in allowed_hosts
            if (normalized := _normalize_host(host))
        }
        self.allowed_networks = tuple(
            ipaddress.ip_network(network) for network in allowed_networks
        )
        self.allowed_origins = set(allowed_origins)
        self.enabled = (
            len(token) >= MIN_TOKEN_LENGTH
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
        valid_token = (
            bool(separator)
            and scheme.lower() == "bearer"
            and bool(supplied)
            and secrets.compare_digest(supplied, self.token)
        )
        if not valid_token:
            response = JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={
                    "WWW-Authenticate": 'Bearer realm="Severino HQ MCP"',
                    "Cache-Control": "private, no-store",
                },
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)

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
