"""The brakes on agents, on /mcp/ and on the machine API: the switch that pauses
every agent, a person's block on one client, and the approval and block of
each tailnet device an agent calls from (``agent_devices``)."""

import logging
from dataclasses import dataclass
from typing import Annotated, Literal, TypedDict

from django.db import DatabaseError, transaction
from django.db.models import Exists, OuterRef, Q, QuerySet
from django.utils import timezone

from hq.domains.control_plane.models import CapabilityRule
from hq.platform.application.security import (
    AGENT_SURFACES,
    DEVICE_INTERFACE,
    Principal,
    person,
    subject_of,
)
from hq.platform.core.audit import record_event
from hq.platform.core.models import AgentAccess, AgentIdentity, AuditLog

from . import agent_devices
from .agent_devices import DeviceStanding, Unplaced
from .agent_registry import AUDIT_LABEL as IDENTITY_LABEL, identity_of
from .command_fields import Describe
from .record_errors import PolicyError, RecordNotFound
from .refusal_status import Refusal

logger = logging.getLogger("severino.agents")

AUDIT_LABEL = "Agent access"

SET_AGENT_ACCESS = "agent.access.set"
SET_AGENT_BLOCK = "agent.block.set"
REMOVE_AGENT = "agent.remove"

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

    blocked = _blocked_clients().filter(
        client_id=OuterRef("requested_actor"), person_key=OuterRef("requested_person_key")
    )
    held = Q(Exists(blocked)) | Q(
        # An operation queued before person attribution cannot safely be
        # assigned to one of a shared client's people. Keep its old brake.
        requested_person_key__isnull=True,
        requested_actor__in=_blocked_clients().values("client_id"),
    )
    held &= Q(requested_interface__in=TOKEN_INTERFACES)
    held |= agent_devices.blocked_askers()
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


AGENTS_PAUSED = AgentRefusal("agents_paused", "An operator paused all agents in HQ. Try again later.")
AGENT_BLOCKED = AgentRefusal("agent_blocked", "An operator blocked this client in HQ.")
DEVICE_AWAITING_APPROVAL = AgentRefusal(
    "device_awaiting_approval",
    "This client is not approved on this device yet. Once it is approved on HQ's Agents page, the next call runs.",
)
DEVICE_BLOCKED = AgentRefusal(
    "device_blocked",
    "An operator blocked this client on this device. It can be allowed again on HQ's Agents page.",
)
DEVICE_UNKNOWN = AgentRefusal(
    Unplaced.NO_DEVICE,
    "No tailnet device sent this call, so HQ refused it. Call HQ over the tailnet from the agent's device, "
    "then approve that device on HQ's Agents page.",
)
TAILNET_UNAVAILABLE = AgentRefusal(
    Unplaced.UNANSWERED,
    "HQ could not ask tailscaled which device sent this call, so HQ refused it. "
    "Try again. If it keeps failing, check tailscaled on HQ's host.",
)
_DEVICE_REFUSALS = {
    DeviceStanding.AWAITING: DEVICE_AWAITING_APPROVAL,
    DeviceStanding.BLOCKED: DEVICE_BLOCKED,
}
_UNPLACED_REFUSALS = {Unplaced.NO_DEVICE: DEVICE_UNKNOWN, Unplaced.UNANSWERED: TAILNET_UNAVAILABLE}
# The codes a call the tailnet could not place is refused with, which the
# Agents page reads back as the addresses an agent was refused from.
UNPLACED_CODES = frozenset(str(code) for code in Unplaced)


def unplaced(refused: Unplaced, detail: str = "") -> AgentRefusal:
    """The refusal of a call the tailnet could not place, with the daemon's own words where it has any."""

    refusal = _UNPLACED_REFUSALS[refused]
    if not detail:
        return refusal
    return AgentRefusal(refusal.code, f"{refusal.reason} tailscaled said: {detail}")


def refusal(principal: Principal) -> AgentRefusal | None:
    """The refusal that applies to this caller now, or None when it may act.

    The one question both doors ask of the principal they authenticated,
    never of the surface they serve, so a person's own device runs while every
    agent beside it is refused. Every brake on a caller answers here, so a new
    one changes neither door.
    """

    if principal.interface in TOKEN_INTERFACES and blocked(principal):
        return AGENT_BLOCKED
    standing = agent_devices.standing(principal)
    if (device := _DEVICE_REFUSALS.get(standing)) is not None:
        row = agent_devices.where(principal)
        return AgentRefusal(device.code, f"{device.reason} Its row there: {row}") if row else device
    if principal.interface in paused_interfaces():
        return AGENTS_PAUSED
    return None


def blocked(principal: Principal) -> bool:
    """Whether a person blocked this one client as this person, read on every call. Fails closed.

    Asked of every token, a person's own device included, so a lost phone
    stops with its client while the rest go on, and a client one person
    blocked still runs for another who signed it in.
    """

    try:
        return _blocked_clients().filter(client_id=principal.actor, person_key=principal.person_key).exists()
    except DatabaseError:
        logger.warning(
            "Agent block unreadable; refusing %s.", principal.actor, extra={"event": "agents.block.unreadable"}
        )
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
    """Whether this one agent is blocked."""

    blocked: Annotated[bool, Describe("Whether the agent is blocked.")]


class AgentBlocked(TypedDict):
    """One agent's block, as the call left it."""

    ok: Literal[True]
    client_id: Annotated[str, Describe("The client the call blocked or allowed, by its client id.")]
    subject: Annotated[str, Describe("The agent the call blocked or allowed: its client and the account it acts for.")]
    blocked: Annotated[bool, Describe("Whether the agent is blocked now.")]


def set_agent_blocked(command: AgentBlockCommand, *, principal: Principal, current_key: str) -> AgentBlocked:
    """Block or allow one agent HQ has seen, by its subject (``security.agent_subject``). Set, not toggled. People only."""

    user = person(principal)
    blocking = command.blocked
    with transaction.atomic():
        row = identity_of(current_key).select_for_update().first()
        if row is None:
            raise RecordNotFound("No agent with that client has called HQ.")
        answer: AgentBlocked = {"ok": True, "client_id": row.client_id, "subject": current_key, "blocked": blocking}
        if (row.blocked_at is not None) == blocking:
            return answer
        row.blocked_at = timezone.now() if blocking else None
        row.blocked_by = user if blocking else None
        row.save(update_fields=["blocked_at", "blocked_by"])
        record_event(
            action=AuditLog.Action.UPDATED,
            obj=row,
            type_label=IDENTITY_LABEL,
            message=f"{'Blocked' if blocking else 'Allowed'} {row}",
            metadata={"client_id": row.client_id, "subject": current_key, "blocked": blocking},
            user=user,
            required=True,
        )
    return answer


@dataclass(frozen=True, slots=True)
class AgentRemoveCommand:
    """Removing one agent names nothing but the agent."""


class AgentRemoved(TypedDict):
    """One agent's removal, and what went with it."""

    ok: Literal[True]
    client_id: Annotated[str, Describe("The client the call removed, by its client id.")]
    subject: Annotated[str, Describe("The agent the call removed: its client and the account it acts for.")]
    devices: Annotated[int, Describe("How many of its tailnet devices were removed with it.")]
    rules: Annotated[int, Describe("How many of its own rules were removed with it.")]


def remove_agent(_command: AgentRemoveCommand, *, principal: Principal, current_key: str) -> AgentRemoved:
    """Remove one agent HQ has seen, by its subject, with its devices and its own rules. People only.

    Removal grants nothing. A client that signs in again afterwards is a new
    identity, as one HQ has never seen: its first call from each device waits
    for approval again, and its grant is whatever its next token carries.
    Removal never lifts a block, since the block lives on the row removed:
    a blocked agent is refused until a person allows it, which is the one
    way a block ends. What the agent did and asked for stays as history: the
    audit log, which the Agents page counts its activity from, and the
    changes it asked for, which a person still decides.
    """

    user = person(principal)
    with transaction.atomic():
        row = identity_of(current_key).select_for_update().first()
        if row is None:
            raise RecordNotFound("No agent with that client has called HQ.")
        if row.blocked_at is not None:
            raise PolicyError("Allow this agent before you remove it. A blocked agent stays listed so its block holds.")
        subject = subject_of(row.client_id, row.person_key)
        named = str(row)
        rules, _ = CapabilityRule.objects.filter(subject=subject).delete()
        devices = row.devices.count()
        record_event(
            action=AuditLog.Action.DELETED,
            obj=row,
            type_label=IDENTITY_LABEL,
            message=f"Removed {named}",
            metadata={"client_id": row.client_id, "subject": subject, "devices": devices, "rules": rules},
            user=user,
            required=True,
        )
        row.delete()
    return {"ok": True, "client_id": row.client_id, "subject": current_key, "devices": devices, "rules": rules}
