"""The authenticated MCP caller, carried per request in a context variable."""

import contextvars

from hq.platform.application import request_context
from hq.platform.application.security import AuthorizationError, Principal
from hq.platform.core.network import client_ip

_current_principal: contextvars.ContextVar[Principal | None] = contextvars.ContextVar("hq_mcp_principal", default=None)


def set_principal(principal: Principal | None) -> contextvars.Token[Principal | None]:
    """Carry ``principal`` as the caller for the length of a ``with`` block."""

    return _current_principal.set(principal)


def current_principal() -> Principal:
    """The authenticated caller of this request.

    There is no default: every authenticated request names its agent, and a tool
    reached without one is refused rather than run at the deployment ceiling.
    """

    principal = _current_principal.get()
    if principal is None:
        raise AuthorizationError("No authenticated MCP caller for this request.")
    return principal


def authenticated() -> Principal | None:
    """The authenticated caller, or None, so a refusal is recorded against whoever made it."""

    return _current_principal.get()


def caller_address() -> str:
    """The address this request's caller is judged and recorded by, or "" outside a request."""

    arrived = request_context.current()
    return "" if arrived is None else client_ip(arrived)
