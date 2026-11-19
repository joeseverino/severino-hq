"""The brakes on agents, on /mcp/ and on the machine API: the switch that pauses
every agent, a person's block on one client, and the approval and block of
each tailnet device an agent calls from (``agent_devices``). The Agents
domain's commands, a session's sign-out among them, are declared here."""

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
    Capability,
    Principal,
    person,
    subject_of,
)
from hq.platform.core.models import AgentAccess, AgentIdentity

from . import agent_devices
from .agent_devices import DeviceStanding, Unplaced
from .agent_registry import identity_of
from .capability_policy import SET_POLICY, PolicyCommand, apply_changes
from .command_fields import Describe
from .contracts import Effect
from .integration_specs import CapabilitySpec
from .own_devices import SET_OWN_DEVICE, OwnDeviceCommand, set_own_device
from .record_errors import PolicyError, RecordNotFound
from .refusal_status import Refusal
from .sessions import SIGN_OUT, SignOutCommand, sign_out

logger = logging.getLogger("severino.agents")


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
    held = Q(Exists(blocked)) & Q(requested_interface__in=TOKEN_INTERFACES)
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
        row = AgentAccess.objects.select_for_update().filter(pk=1).first() or AgentAccess(pk=1)
        if row.paused == paused:
            return {"ok": True, "paused": paused}
        row.paused = paused
        row.changed_by = user
        row.changed_at = timezone.now()
        row.audit_said = "Agents paused" if paused else "Agents resumed"
        row.save()
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
        row.audit_said = f"{'Blocked' if blocking else 'Allowed'} {row}"
        row.save(update_fields=["blocked_at", "blocked_by"])
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

    person(principal)
    with transaction.atomic():
        row = identity_of(current_key).select_for_update().first()
        if row is None:
            raise RecordNotFound("No agent with that client has called HQ.")
        if row.blocked_at is not None:
            raise PolicyError("Allow this agent before you remove it. A blocked agent stays listed so its block holds.")
        rules, _ = CapabilityRule.objects.filter(subject=subject_of(row.client_id, row.person_key)).delete()
        devices = row.devices.count()
        row.audit_gone = f"Removed {row}"
        row.delete()
    return {"ok": True, "client_id": row.client_id, "subject": current_key, "devices": devices, "rules": rules}


def capabilities() -> tuple[CapabilitySpec, ...]:
    return (
        # Who may act as an agent, decided by a person (security.HELD_ONLY_AT), so
        # no credential widens or frees itself.
        CapabilitySpec(
            SET_AGENT_ACCESS,
            "Pause every agent, on MCP and the machine API, or let them back in.",
            Effect.REMOTE_WRITE,
            Capability.GOVERN_AGENTS,
            AgentAccessCommand,
            set_agents_paused,
            execution_notes=("Set the state rather than toggle it, so asking twice changes nothing.",),
            label="Pause or resume agents",
            variants=(("on", "Pause all agents"), ("off", "Resume agents")),
            state=agents_paused,
        ),
        CapabilitySpec(
            SET_AGENT_BLOCK,
            "Block one agent's client on MCP and the machine API, or let it back in.",
            Effect.REMOTE_WRITE,
            Capability.GOVERN_AGENTS,
            AgentBlockCommand,
            set_agent_blocked,
            "key",
            target_label="Client",
            target_help="The client id of an agent HQ has seen.",
            execution_notes=(
                "Refuse a client HQ has never seen.",
                "Set the state rather than toggle it, so asking twice changes nothing.",
            ),
            label="Block or allow an agent",
            variants=(("on", "Block"), ("off", "Allow")),
            reached_from="agent_policy",
            state=blocked,
        ),
        CapabilitySpec(
            REMOVE_AGENT,
            "Remove one agent HQ has seen, with its devices and its own rules, so it is listed no more.",
            Effect.DESTRUCTIVE,
            Capability.GOVERN_AGENTS,
            AgentRemoveCommand,
            remove_agent,
            "key",
            target_label="Client",
            target_help="The client id of an agent HQ has seen.",
            execution_notes=(
                "Refuse a client HQ has never seen.",
                "Refuse a blocked agent, so removing it never lifts its block.",
                "Keep the audit log and what it asked for; a client that signs in again is a new agent.",
            ),
            label="Remove an agent",
            verb="Remove",
            reached_from="agent_policy",
        ),
        CapabilitySpec(
            agent_devices.APPROVE_AGENT_DEVICE,
            "Approve a tailnet device an agent called from, so its calls from there run, or withdraw the approval.",
            Effect.REMOTE_WRITE,
            Capability.GOVERN_AGENTS,
            agent_devices.AgentDeviceApprovalCommand,
            agent_devices.set_device_approved,
            "key",
            target_label="Device",
            target_help="The record of a device an agent has called HQ from.",
            execution_notes=(
                "Refuse a device no agent has called from.",
                "Only the user an agent acts for approves the device it called from.",
                "Set the state rather than toggle it, so asking twice changes nothing.",
            ),
            label="Approve an agent's device",
            variants=(("on", "Approve"), ("off", "Withdraw")),
            reached_from="agent_policy",
            state=agent_devices.device_approved,
        ),
        CapabilitySpec(
            agent_devices.BLOCK_AGENT_DEVICE,
            "Block one tailnet device an agent calls from, on MCP and the machine API, or let it back in.",
            Effect.REMOTE_WRITE,
            Capability.GOVERN_AGENTS,
            agent_devices.AgentDeviceBlockCommand,
            agent_devices.set_device_blocked,
            "key",
            target_label="Device",
            target_help="The record of a device an agent has called HQ from.",
            execution_notes=(
                "Refuse a device no agent has called from.",
                "Set the state rather than toggle it, so asking twice changes nothing.",
            ),
            label="Block or allow an agent's device",
            variants=(("on", "Block"), ("off", "Allow")),
            reached_from="agent_policy",
            state=agent_devices.device_blocked,
        ),
        CapabilitySpec(
            SET_OWN_DEVICE,
            "Register a client as one of your own devices, so its token acts as you, or remove it.",
            Effect.REMOTE_WRITE,
            Capability.GOVERN_AGENTS,
            OwnDeviceCommand,
            set_own_device,
            "client_id",
            target_label="Client",
            target_help="The client id Pocket ID registered for your device.",
            execution_notes=(
                "Register the device only as your own, never as another user's.",
                "Refuse a client id Pocket ID could not have registered.",
                "Set the state rather than toggle it, so asking twice changes nothing.",
            ),
            label="Register or remove your device",
            variants=(("on", "Register"), ("off", "Remove")),
            reached_from="agent_policy",
        ),
        CapabilitySpec(
            SET_POLICY,
            "Set whether one agent's call to one action runs, waits for approval, or is refused.",
            Effect.REMOTE_WRITE,
            Capability.GOVERN_AGENTS,
            PolicyCommand,
            apply_changes,
            execution_notes=(
                "Change only the rules that differ from the current ones, each recorded on its own.",
                "Narrow only what Pocket ID granted: refuse a rule for an action the agent's grant does not carry.",
                "Refuse a rule for an action taken only in the browser, and approval for a read.",
            ),
            label="Set the agent policy",
        ),
        CapabilitySpec(
            SIGN_OUT,
            "Sign a user out of every HQ session when Pocket ID signs them out.",
            Effect.REMOTE_WRITE,
            Capability.END_SESSIONS,
            SignOutCommand,
            sign_out,
            execution_notes=(
                "Find the user who signed in as Pocket ID's subject; if none did, there is nothing to do.",
                "End every live session of theirs, leaving the account and any local password alone.",
            ),
            label="Sign a user out",
        ),
    )
