"""The request being answered, for a read that describes its own caller.

Bound for one request by the web middleware and by the MCP boundary; nothing
else sets it. Outside a request it is None, and a read that needs it refuses
rather than describing some other request.
"""

from collections.abc import MutableMapping
from contextvars import ContextVar, Token
from typing import Any

_current: ContextVar[Any] = ContextVar("hq_current_request", default=None)


def bind(request: Any) -> Token:
    return _current.set(request)


def unbind(token: Token) -> None:
    _current.reset(token)


def current() -> Any:
    return _current.get()


def from_scope(scope: MutableMapping[str, Any]) -> Any:
    """A Django request over an ASGI scope that does not pass through Django."""

    from io import BytesIO

    from django.core.handlers.asgi import ASGIRequest

    return ASGIRequest(scope, BytesIO())
