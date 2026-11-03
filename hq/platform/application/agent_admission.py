"""How a bearer token is let through an agent door, the same on /mcp/ and the machine API.

Each door parses its own transport and answers in its own protocol; what it
asks is answered here, once. ``admit`` says who a token is, ``refused``
whether that caller may act right now, and ``at_once`` holds one of its calls
at once. They are apart because the doors ask them at different moments: the
machine API at the door, MCP at each tool call, so a paused or throttled agent
keeps its connection and its next call after that runs on it.
"""

import contextlib
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import dataclass

from . import agent_access, agent_limits
from .agent_access import AgentRefusal
from .agent_limits import Limited
from .agent_registry import observe
from .denials import record_denial, record_limited, settle
from .security import AuthorizationError, Principal
from .tokens import TokenError, token_principal, verify


@dataclass(frozen=True, slots=True)
class Unadmitted:
    """A bearer no principal came of: ``verified`` when the token itself was
    good but grants nothing HQ can use, which is a 403 rather than a 401."""

    code: str
    reason: str
    verified: bool


@dataclass(frozen=True, slots=True)
class Throttled:
    """A good token whose agent is calling faster than its rate: who it is, and its refusal."""

    principal: Principal
    refusal: Limited


MISSING = Unadmitted(TokenError.code, "An access token is required.", verified=False)

# The rate refusal an MCP request carries from its door to each tool call in it.
_carried: ContextVar[Limited | None] = ContextVar("hq_agent_throttled", default=None)


def admit(bearer: str, *, interface: str, source: str) -> Principal | Throttled | Unadmitted:
    """The principal ``bearer`` names on ``interface``, or why there is none.

    A refused token is recorded against ``source``, the peer that sent it,
    since no identity was established. An admitted one spends one call of its
    rate before anything is written, so a flood costs no write past its
    refusal's; within its rate it is observed, before any brake, so a paused
    agent is still registered.
    """

    if not bearer:
        unadmitted = MISSING
    else:
        try:
            principal = token_principal(verify(bearer), interface)
        except TokenError as exc:
            unadmitted = Unadmitted(exc.code, exc.reason, verified=False)
        except AuthorizationError as exc:
            # RFC 6750's word for a good token that grants too little, on both doors.
            unadmitted = Unadmitted("insufficient_scope", exc.reason, verified=True)
        else:
            limited = agent_limits.LIMITS.take(principal.actor)
            if limited is not None:
                return Throttled(principal, limited)
            settle(principal.actor)
            observe(principal)
            return principal
    record_denial(interface=interface, reason=unadmitted.code, source=source, authenticated=False)
    return unadmitted


@contextlib.contextmanager
def carrying(admitted: Principal | Throttled) -> Iterator[Principal]:
    """The caller ``admitted`` names, with its rate refusal carried for the block to ``refused``."""

    if not isinstance(admitted, Throttled):
        yield admitted
        return
    with _carried.set(admitted.refusal):
        yield admitted.principal


def refused(caller: Principal | Throttled, *, source: str = "") -> AgentRefusal | Limited | None:
    """The refusal that applies to ``caller`` now, recorded by its name; None lets it act.

    Asked only after authentication, so only a valid caller learns of it. A
    rate refusal comes first, from ``caller`` or the request carrying it, and
    is counted rather than written each time.
    """

    principal = caller.principal if isinstance(caller, Throttled) else caller
    limited = caller.refusal if isinstance(caller, Throttled) else _carried.get()
    if limited is not None:
        record_limited(interface=principal.interface, reason=limited.code, actor=principal.actor, source=source)
        return limited
    applies = agent_access.refusal(principal)
    if applies is not None:
        record_denial(interface=principal.interface, reason=applies.code, actor=principal.actor, source=source)
    return applies


@contextlib.contextmanager
def at_once(principal: Principal, *, source: str = "") -> Iterator[Limited | None]:
    """One of ``principal``'s calls at once, held for the block; the block is given the
    refusal instead when the agent, or every agent together, has all of them in flight."""

    held = agent_limits.LIMITS.occupy(principal.actor)
    if isinstance(held, Limited):
        record_limited(interface=principal.interface, reason=held.code, actor=principal.actor, source=source)
        yield held
        return
    with held:
        yield None
