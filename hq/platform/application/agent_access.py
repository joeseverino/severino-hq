"""The brakes on agents, on /mcp/ and on the machine API: the switch that pauses
every agent, and a person's block on one client."""

import logging
from dataclasses import dataclass
from typing import Annotated, Literal, TypedDict

from django.db import DatabaseError, transaction
from django.db.models import Q, QuerySet
from django.utils import timezone

from hq.platform.application.security import AGENT_SURFACES, DEVICE_INTERFACE, Principal, person
from hq.platform.core.audit import record_event
from hq.platform.core.models import AgentAccess, AgentIdentity, AuditLog

from .agent_registry import AUDIT_LABEL as IDENTITY_LABEL
from .command_fields import Describe
from .record_errors import RecordNotFound
from .refusal_status import Refusal

logger = logging.getLogger("severino.agents")

AUDIT_LABEL = "Agent access"

SET_AGENT_ACCESS = "agent.access.set"
SET_AGENT_BLOCK = "agent.block.set"

# Every interface a token reaches HQ through, where a client can be blocked.
TOKEN_INTERFACES = AGENT_SURFACES | {DEVICE_INTERFACE}


def paused_interfaces() -> frozenset[str]:
    """The interfaces the Agents pause refuses right now: every agent's, or none.

    The one reading of the pause. The doors ask it through ``refusal``, and
    approving what an agent asked for and the controller's claim of what it
    queued through ``held_askers``, so pausing agents stops every agent on
    every surface at once, and a person's interface is never in it.
    """

    return AGENT_SURFACES if agents_paused() else frozenset()


def held_askers() -> Q:
    """The rows asked for by a caller the brakes hold now, over ``requested_actor`` and ``requested_interface``.

    What an agent already asked for stops with it, a pending hold and queued
    work alike: approving a hold and the controller's claim exclude these. A
    query rather than a check per row, so the claim stays one statement and the
    block is read in the same one. Lifting the brake releases the rows as they
    stand.
    """

    held = Q(requested_interface__in=TOKEN_INTERFACES, requested_actor__in=_blocked_clients().values("client_id"))
    if paused := paused_interfaces():
        held |= Q(requested_interface__in=paused)
    return held


@dataclass(frozen=True, slots=True)
class AgentRefusal:
    """Why a caller may do nothing right now: a code a client keys on, and a sentence for it."""

    code: str
    reason: str

    @property
    def refusal(self) -> Refusal:
        """What every door answers."""

        return Refusal(self.code, self.reason)


AGENTS_PAUSED = AgentRefusal("agents_paused", "Agents are paused by the operator.")
AGENT_BLOCKED = AgentRefusal("agent_blocked", "This client is blocked by the operator.")


def refusal(principal: Principal) -> AgentRefusal | None:
    """The refusal that applies to this caller now, or None when it may act.

    The one question both doors ask of the principal they authenticated,
    never of the surface they serve, so a person's own device runs while every
    agent beside it is refused. Every brake on a caller answers here, so a new
    one changes neither door.
    """

    if principal.interface in TOKEN_INTERFACES and blocked(principal.actor):
        return AGENT_BLOCKED
    if principal.interface in paused_interfaces():
        return AGENTS_PAUSED
    return None


def blocked(client_id: str) -> bool:
    """Whether a person blocked this one client, read on every call. Fails closed.

    Asked of every token's client, a person's own device included, so a lost
    phone stops with its client while the rest go on.
    """

    try:
        return _blocked_clients().filter(client_id=client_id).exists()
    except DatabaseError:
        logger.warning("Agent block unreadable; refusing %s.", client_id, extra={"event": "agents.block.unreadable"})
        return True


def _blocked_clients() -> QuerySet[AgentIdentity]:
    return AgentIdentity.objects.filter(blocked_at__isnull=False)


def agents_paused() -> bool:
    """The switch itself, read on every call, never cached. Fails closed."""

    try:
        row = AgentAccess.objects.filter(pk=1).only("paused").first()
    except DatabaseError:
        logger.warning("Agent access unreadable; refusing agents.", extra={"event": "agents.access.unreadable"})
        return True
    return bool(row and row.paused)


@dataclass(frozen=True, slots=True)
class AgentAccessCommand:
    """Whether every agent is paused."""

    paused: Annotated[bool, Describe("Whether every agent is paused.")]


class AgentsPaused(TypedDict):
    """Whether agents are paused, as the call left it."""

    ok: Literal[True]
    paused: Annotated[bool, Describe("Whether every agent is paused now.")]


def set_agents_paused(command: AgentAccessCommand, *, principal: Principal) -> AgentsPaused:
    """Set, not toggle, so a repeated request cannot undo itself. People only."""

    user = person(principal)
    paused = command.paused
    with transaction.atomic():
        row, _ = AgentAccess.objects.select_for_update().get_or_create(pk=1)
        if row.paused == paused:
            return {"ok": True, "paused": paused}
        row.paused = paused
        row.changed_by = user
        row.changed_at = timezone.now()
        row.save(update_fields=["paused", "changed_by", "changed_at"])
        record_event(
            action=AuditLog.Action.UPDATED,
            obj=row,
            type_label=AUDIT_LABEL,
            message="Agents paused" if paused else "Agents resumed",
            user=user,
            required=True,
        )
    return {"ok": True, "paused": paused}


@dataclass(frozen=True, slots=True)
class AgentBlockCommand:
    """Whether this one client is blocked."""

    blocked: Annotated[bool, Describe("Whether the client is blocked.")]


class AgentBlocked(TypedDict):
    """One client's block, as the call left it."""

    ok: Literal[True]
    client_id: Annotated[str, Describe("The client the call blocked or allowed, by its client id.")]
    blocked: Annotated[bool, Describe("Whether the client is blocked now.")]


def set_agent_blocked(command: AgentBlockCommand, *, principal: Principal, current_key: str) -> AgentBlocked:
    """Block or allow one client HQ has seen, by its client id. Set, not toggled. People only."""

    user = person(principal)
    blocking = command.blocked
    with transaction.atomic():
        row = AgentIdentity.objects.select_for_update().filter(client_id=current_key).first()
        if row is None:
            raise RecordNotFound("No agent with that client has called HQ.")
        if (row.blocked_at is not None) == blocking:
            return {"ok": True, "client_id": row.client_id, "blocked": blocking}
        row.blocked_at = timezone.now() if blocking else None
        row.blocked_by = user if blocking else None
        row.save(update_fields=["blocked_at", "blocked_by"])
        record_event(
            action=AuditLog.Action.UPDATED,
            obj=row,
            type_label=IDENTITY_LABEL,
            message=f"{'Blocked' if blocking else 'Allowed'} {row.client_id}",
            metadata={"client_id": row.client_id, "blocked": blocking},
            user=user,
            required=True,
        )
    return {"ok": True, "client_id": row.client_id, "blocked": blocking}
