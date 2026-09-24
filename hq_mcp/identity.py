"""The authenticated MCP caller, carried per request in a context variable."""

from __future__ import annotations

import contextvars
import logging

from application.security import AuthorizationError, Principal, mcp_principal

# Never "web": an interactive interface would waive the approval hold.
INTERFACE = "mcp"

logger = logging.getLogger("severino.mcp")

_current_principal: contextvars.ContextVar[Principal | None] = contextvars.ContextVar(
    "hq_mcp_principal", default=None
)


def set_principal(principal: Principal | None):
    return _current_principal.set(principal)


def reset_principal(token) -> None:
    _current_principal.reset(token)


def current_principal() -> Principal:
    """The authenticated caller of this request.

    There is no default: every authenticated request names its agent, and a tool
    reached without one is refused rather than run at the deployment ceiling.
    """

    principal = _current_principal.get()
    if principal is None:
        raise AuthorizationError("No authenticated MCP caller for this request.")
    return principal


def token_principal(claims: dict) -> Principal:
    """An agent holding its grant, capped by what this deployment allows MCP."""

    from hq_api.security import api_principal

    verified = api_principal(claims)
    ceiling = {str(capability) for capability in mcp_principal().capabilities}
    granted = {str(capability) for capability in verified.capabilities}
    withheld = granted - ceiling
    if withheld:
        logger.debug(
            "Agent %s holds grants this deployment withholds from MCP: %s",
            verified.actor,
            ", ".join(sorted(withheld)),
            extra={"event": "mcp.grant.capped"},
        )
    return Principal(
        verified.actor,
        INTERFACE,
        frozenset(granted & ceiling),
        granted=frozenset(granted),
    )
