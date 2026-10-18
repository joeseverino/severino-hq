"""The request being answered, for a read that describes its own caller.

Bound for one request by the web middleware and by the MCP boundary; nothing
else sets it. Outside a request it is None, and a read that needs it refuses
rather than describing some other request.
"""

from collections.abc import Collection, MutableMapping
from contextvars import ContextVar, Token
from io import BytesIO
from typing import Any, override

from django.core.exceptions import DisallowedHost
from django.core.handlers.asgi import ASGIRequest

from hq.platform.core.network import strict_host

_current: ContextVar[Any] = ContextVar("hq_current_request", default=None)


def bind(request: Any) -> Token:
    return _current.set(request)


def unbind(token: Token) -> None:
    _current.reset(token)


def current() -> Any:
    return _current.get()


class AdmittedRequest(ASGIRequest):
    """A request a boundary other than Django's admitted, by its own hosts.

    ``get_host`` holds it to the hosts that boundary serves. Django's own would
    hold it to the web's ``ALLOWED_HOSTS``, which name none of them, so every
    read that asks how the caller arrived would refuse a caller already let in.
    """

    def __init__(self, scope: MutableMapping[str, Any], allowed_hosts: Collection[str]) -> None:
        super().__init__(scope, BytesIO())
        self.admitted_hosts = frozenset(allowed_hosts)

    @override
    def get_host(self) -> str:
        host = self._get_raw_host()
        if strict_host(host) in self.admitted_hosts:
            return host
        raise DisallowedHost(f"Invalid HTTP_HOST header for this boundary: {host!r}.")


def from_scope(scope: MutableMapping[str, Any], *, allowed_hosts: Collection[str]) -> AdmittedRequest:
    """A Django request over an ASGI scope that does not pass through Django."""

    return AdmittedRequest(scope, allowed_hosts)
