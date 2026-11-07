"""How a bearer token is let through an agent door, the same on /mcp/ and the machine API.

Each door parses its own transport and answers in its own protocol; what it
asks is answered here, once. ``admit`` says who a token is, ``refused``
whether that caller may act right now, ``at_once`` holds one of its calls at
once, and ``stream_opened`` one of its listen streams. They are apart because
the doors ask them at different moments: the machine API at the door, MCP at
each tool call, so a paused or throttled agent keeps its connection and its
next call after that runs on it.
"""

import contextlib
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import dataclass, replace

from . import agent_access, agent_devices, agent_limits
from .agent_access import AgentRefusal
from .agent_limits import Limited, Slot
from .agent_registry import observe
from .denials import record_denial, record_limited, settle
from .security import AuthorizationError, Capability, Principal, agent_subject
from .tokens import TokenError, token_principal, verify


@dataclass(frozen=True, slots=True)
class Unadmitted:
    """A bearer no principal came of: ``verified`` when the token itself was
    good but grants nothing HQ can use, which is a 403 rather than a 401."""

    code: str
    reason: str
    verified: bool

    @property
    def scope(self) -> str:
        """The permission a verified token's 403 names: the least that gets in."""

        return Capability.READ.value if self.verified else ""


@dataclass(frozen=True, slots=True)
class Stopped:
    """A good token whose call is stopped before any brake is read: who it is,
    and its refusal, for calling faster than its rate or from nowhere the
    tailnet could place it."""

    principal: Principal
    refusal: Limited | AgentRefusal


MISSING = Unadmitted(TokenError.code, "An access token is required.", verified=False)

# The refusal an MCP request carries from its door to each tool call in it.
_carried: ContextVar[Limited | AgentRefusal | None] = ContextVar("hq_agent_stopped", default=None)


def admit(bearer: str, *, interface: str, source: str) -> Principal | Stopped | Unadmitted:
    """The principal ``bearer`` names on ``interface``, or why there is none.

    A refused token is recorded against ``source``, the peer that sent it,
    since no identity was established. An admitted one spends one call of its
    rate before anything is written, so a flood costs no write past its
    refusal's; within its rate it is placed on the tailnet device at
    ``source`` and observed, before any brake, so a paused agent and a device
    awaiting approval are still registered. A call the tailnet could not
    place is stopped here, where the two doors meet, so neither door can let
    it through as the client alone.
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
            limited = agent_limits.LIMITS.take(agent_subject(principal))
            if limited is not None:
                return Stopped(principal, limited)
            settle(principal.actor)
            placed = agent_devices.place(source)
            principal = replace(principal, device=placed.node_id)
            observe(principal, placed.device)
            if placed.refused is not None:
                return Stopped(principal, agent_access.unplaced(placed.refused, placed.detail))
            return principal
    record_denial(interface=interface, reason=unadmitted.code, source=source, authenticated=False)
    return unadmitted


@contextlib.contextmanager
def carrying(admitted: Principal | Stopped) -> Iterator[Principal]:
    """The caller ``admitted`` names, with the refusal that stopped it carried for the block to ``refused``."""

    if not isinstance(admitted, Stopped):
        yield admitted
        return
    with _carried.set(admitted.refusal):
        yield admitted.principal


def refused(caller: Principal | Stopped, *, source: str = "") -> AgentRefusal | Limited | None:
    """The refusal that applies to ``caller`` now, recorded by its name; None lets it act.

    Asked only after authentication, so only a valid caller learns of it. What
    stopped the call at admission comes first, from ``caller`` or the request
    carrying it: a rate refusal is counted rather than written each time, and
    an unplaced call is recorded against the address it came from, which is
    all HQ knows of where it was.
    """

    principal = caller.principal if isinstance(caller, Stopped) else caller
    stopped = caller.refusal if isinstance(caller, Stopped) else _carried.get()
    if isinstance(stopped, Limited):
        record_limited(
            interface=principal.interface,
            reason=stopped.code,
            actor=principal.actor,
            source=source,
            device=principal.device,
        )
        return stopped
    applies = stopped or agent_access.refusal(principal)
    if applies is not None:
        record_denial(
            interface=principal.interface,
            reason=applies.code,
            actor=principal.actor,
            source=source,
            device=principal.device,
        )
    return applies


@contextlib.contextmanager
def at_once(principal: Principal, *, source: str = "") -> Iterator[Limited | None]:
    """One of ``principal``'s calls at once, held for the block; the block is given the
    refusal instead when the agent, or every agent together, has all of them in flight."""

    held = agent_limits.LIMITS.occupy(agent_subject(principal))
    if isinstance(held, Limited):
        record_limited(
            interface=principal.interface,
            reason=held.code,
            actor=principal.actor,
            source=source,
            device=principal.device,
        )
        yield held
        return
    with held:
        yield None


def stream_opened(principal: Principal, *, source: str = "") -> Slot | Limited:
    """One of ``principal``'s listen streams, held until the slot is given back,
    or the refusal, recorded, when the agent has all of them open."""

    held = agent_limits.LIMITS.listen(agent_subject(principal))
    if isinstance(held, Limited):
        record_limited(
            interface=principal.interface,
            reason=held.code,
            actor=principal.actor,
            source=source,
            device=principal.device,
        )
    return held


def challenge(*params: tuple[str, str]) -> str:
    """A ``WWW-Authenticate: Bearer`` value of ``params``, as either door writes one."""

    return "Bearer " + ", ".join(f'{name}="{_quoted(value)}"' for name, value in params)


def _quoted(value: str) -> str:
    """``value`` as the inside of an HTTP quoted-string (RFC 9110 section 5.6.4)."""

    return value.replace("\\", "\\\\").replace('"', '\\"')
