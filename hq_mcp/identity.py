"""Who is calling the MCP endpoint, and what that caller may do.

Two credentials reach `/mcp/`, and they differ in what they can say about
themselves. A Pocket ID client-credentials token *names* the agent holding it,
so every tool call it makes is attributable to that agent and revocable at the
identity provider. The legacy shared bearer names nobody: anyone holding it is
the same deployment-configured service account, which is exactly why it is
being replaced rather than extended.

The principal is carried in a context variable rather than threaded through
every adapter signature. The boundary knows who called; the tool functions are
several frames below it inside FastMCP and never see the request. A context
variable set before the downstream `await` is visible to that call and to
nothing else, which is the same shape `core.middleware` already uses to carry
the current web user.
"""

from __future__ import annotations

import contextvars

from application.security import Principal, mcp_principal

# Not "api". Both are non-interactive, so the approval gate holds either way,
# but the audit log distinguishes adapters and an MCP call should not arrive
# claiming to have come through the machine API.
INTERFACE = "mcp"

_current_principal: contextvars.ContextVar[Principal | None] = contextvars.ContextVar(
    "hq_mcp_principal", default=None
)


def set_principal(principal: Principal | None):
    """Bind the caller for the duration of one request. Returns a reset token."""

    return _current_principal.set(principal)


def reset_principal(token) -> None:
    _current_principal.reset(token)


def current_principal() -> Principal:
    """The authenticated caller, or the shared service account.

    A missing principal means the legacy bearer authenticated this request:
    that credential carries no identity, so it gets the deployment-configured
    one it has always had. It is never an anonymous caller -- the boundary
    rejects those before any of this runs.
    """

    return _current_principal.get() or mcp_principal()


def token_principal(claims: dict) -> Principal:
    """An agent identity, holding exactly what its token was granted.

    `api_principal` is reused rather than reimplemented: it already refuses a
    token that names no client and already declines to widen a grant, and a
    second copy of those two rules is a second place for them to rot. Only the
    interface differs, because the call arrived here and not at `/api/`.
    """

    from hq_api.security import api_principal

    verified = api_principal(claims)
    return Principal(verified.actor, INTERFACE, verified.capabilities)
