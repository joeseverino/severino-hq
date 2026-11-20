"""Where a request may leave the process, and the only places that enter it.

A protected contract in pyproject.toml lets only those places import this
module (``lint:imports``), so a new way to wait is a decision made there.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

lazy from . import speculation

# Where a request may wait on something outside the process, and why.
ALLOWED = frozendict(
    {
        "oidc": (
            "Signing in waits on the sign-in service for the token and the "
            "user's claims and picture. Each call carries the service's own "
            "short-lived token and is bounded by OIDC_TIMEOUT or the "
            "picture's timeout."
        ),
        "lookup": (
            "A lookup asks a public resolver or registry one question that an "
            "operator or a program waits on. It carries no credential and is "
            "bounded by SEVERINO_LOOKUP_TIMEOUT_SECONDS."
        ),
        "tailscaled": (
            "To name the tailnet device behind an agent's call, HQ asks the "
            "local tailscaled who holds its address now. It asks over a Unix "
            "socket, with no network, name lookup or credential, bounded by "
            "tailscaled.TIMEOUT. A stored answer would be the stale reading "
            "the question replaces."
        ),
    }
)

# The request this context is serving ("POST /watching/refresh/"), or "".
serving: ContextVar[str] = ContextVar("hq_outbound_serving", default="")
# The entry of ``ALLOWED`` this context is inside, or "".
entered: ContextVar[str] = ContextVar("hq_outbound_allowed", default="")


@contextmanager
def allowed(name: str) -> Iterator[None]:
    """Enter one of the exceptions ``ALLOWED`` names."""

    if name not in ALLOWED:
        raise ValueError(f"{name!r} is not an outbound exception HQ allows.")
    # A page fetched on a guess asks nothing outside the process.
    speculation.refuse(f"it reaches outside the process ({name})")
    with entered.set(name):
        yield
