"""The tailnet devices an agent calls HQ from (docs/Security.md#agents).

A token proves the client and, for a delegated grant, the person; the tailnet
proves the device, since its address is one only that device's WireGuard key
answers for. So an agent's call is placed on the device tailscaled says sent
it, asked per request (``tailscaled.whois``), and does nothing from a device
until it is approved for that client and person. The approval is the person's
own act in HQ, and the link between their HQ person and the device's tailnet
owner: HQ never infers one namespace from the other. Where the deployment has
a tailnet, a call it cannot place is refused; only a deployment with none
answers for the client alone.
"""

import logging
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Annotated, Literal, TypedDict

from django.db import DatabaseError, IntegrityError, transaction
from django.db.models import Exists, OuterRef, Q, QuerySet
from django.utils import timezone

from hq.platform.core.audit import record_event
from hq.platform.core.models import AgentDevice, AgentIdentity, AuditLog

from . import tailnet, tailscaled
from .command_fields import Describe
from .record_errors import RecordNotFound
from .security import AGENT_SURFACES, AuthorizationError, Principal, person
lazy from .hq_self import site_url
lazy from .routes import reverse

logger = logging.getLogger("severino.agents")

AUDIT_LABEL = "Agent device"

APPROVE_AGENT_DEVICE = "agent.device_approval.set"
BLOCK_AGENT_DEVICE = "agent.device_block.set"

# A device's name, os and owner are kept to this precision, as an identity's last seen is.
TOUCH_INTERVAL = timedelta(minutes=10)


class Unplaced(StrEnum):
    """Why the tailnet could not place a call: the refusal code each door answers."""

    # tailscaled holds no device at the address: loopback, the LAN, or an address no peer has.
    NO_DEVICE = "device_unknown"
    # tailscaled did not answer, so HQ cannot say who sent the call.
    UNANSWERED = "tailnet_unavailable"


@dataclass(frozen=True, slots=True)
class Placement:
    """Where HQ places one call: on a device; nowhere, in a deployment with no
    tailnet; or refused, where the tailnet could not place it (``refused``
    says why, ``detail`` the daemon's own words)."""

    device: tailnet.Device | None = None
    refused: Unplaced | None = None
    detail: str = ""

    @property
    def node_id(self) -> str:
        return self.device.node_id if self.device is not None else ""


NOWHERE = Placement()


def place(address: str) -> Placement:
    """The device the call from ``address`` came from, as tailscaled knows it now.

    ``address`` is the caller as ``ForwardedCaller`` resolved it: through a
    proxy HQ trusts it is the caller's own, and through any other it is the
    peer that opened the socket, the one hop HQ can prove. So a forwarded
    header never moves a call off the device that sent it. The deployment has
    a tailnet when the host named tailscaled's socket (``tailscaled.socket_path``); then
    every call is placed by the daemon, and one it holds no device for, or
    cannot answer for, is refused, never let through as the client alone.
    Without the socket nothing is placed, and the client is the finest unit.
    """

    if not tailscaled.socket_path():
        return NOWHERE
    try:
        found = tailscaled.whois(address)
    except tailscaled.Unavailable as exc:
        logger.warning("tailscaled did not place a call: %s", exc, extra={"event": "agents.device.unanswered"})
        return Placement(refused=Unplaced.UNANSWERED, detail=str(exc))
    if found is None or not found.node_id:
        return Placement(refused=Unplaced.NO_DEVICE)
    return Placement(found)


def observe(identity: AgentIdentity, person, device: tailnet.Device, *, interface: str) -> None:
    """Record ``identity``'s call from ``device`` for ``person``; its first is audited.

    A device first seen waits for approval: its row is what the person approves.
    Its first sighting names the agent and its door itself, since admission
    records it before the door binds the caller.
    """

    now = timezone.now()
    row = AgentDevice.objects.filter(identity=identity, person=person, node_id=device.node_id).first()
    if row is None:
        try:
            with transaction.atomic():
                row = AgentDevice.objects.create(
                    identity=identity,
                    person=person,
                    node_id=device.node_id,
                    name=device.label,
                    os=device.os,
                    owner=device.user,
                    last_seen=now,
                )
        except IntegrityError:
            return
        record_event(
            action=AuditLog.Action.CREATED,
            obj=row,
            type_label=AUDIT_LABEL,
            message=f"First seen: {identity.client_id} from {device.label}, awaiting approval",
            metadata={
                "actor": identity.client_id,
                "interface": interface,
                "client_id": identity.client_id,
                "device": device.node_id,
                "owner": device.user,
                "person": "" if person is None else person.get_username(),
            },
        )
        return
    seen = {"name": device.label, "os": device.os, "owner": device.user}
    changed = [field for field, value in seen.items() if getattr(row, field) != value]
    if changed or now - row.last_seen >= TOUCH_INTERVAL:
        for field in changed:
            setattr(row, field, seen[field])
        row.last_seen = now
        row.save(update_fields=[*changed, "last_seen"])


class DeviceStanding(StrEnum):
    """What a device's record says of an agent's call from it."""

    # Not placed on a device, in a deployment with no tailnet: the client's own rules alone apply.
    UNPLACED = "unplaced"
    APPROVED = "approved"
    AWAITING = "awaiting"
    # Refused at admission: the tailnet could not place the call (``Unplaced``).
    UNPLACEABLE = "unplaceable"
    BLOCKED = "blocked"


def standing(principal: Principal) -> DeviceStanding:
    """Whether ``principal``'s call may run from the device it came from. Fails closed.

    Asked of an agent's call alone: a person's own client is their own act in
    HQ already (``OwnDevice``). An admitted call carries the device its
    admission placed it on; one that carries none was admitted where there is
    no tailnet, since a call the tailnet could not place is stopped at
    admission (``agent_admission.Stopped``) and never asked here. A read:
    nothing here asks tailscaled or writes, since a GET is refused here too.
    """

    if principal.interface not in AGENT_SURFACES or not principal.device:
        return DeviceStanding.UNPLACED
    try:
        row = _rows_of(principal).only("approved_at", "blocked_at").first()
    except DatabaseError:
        logger.warning(
            "Agent devices unreadable; refusing %s.", principal.actor, extra={"event": "agents.device.unreadable"}
        )
        return DeviceStanding.BLOCKED
    if row is not None and row.blocked_at is not None:
        return DeviceStanding.BLOCKED
    if row is None or row.approved_at is None:
        return DeviceStanding.AWAITING
    return DeviceStanding.APPROVED


def record_of(principal: Principal) -> AgentDevice | None:
    """The record of ``principal``'s device, with who approved and blocked it, or None."""

    if not principal.device:
        return None
    return _rows_of(principal).select_related("approved_by", "blocked_by").first()


def _rows_of(principal: Principal) -> QuerySet[AgentDevice]:
    """The record of ``principal``'s device, for its client and the person its token names."""

    return AgentDevice.objects.filter(
        identity__client_id=principal.actor, identity__person_key=principal.person_key, node_id=principal.device
    )


def device_anchor(key: int | str) -> str:
    """The id of a device's row on the Agents page."""

    return f"device-{key}"


def where(principal: Principal) -> str:
    """The address of ``principal``'s device's row on HQ's Agents page, or "" with no record of it."""

    try:
        key = _rows_of(principal).values_list("pk", flat=True).first()
    except DatabaseError:
        return ""
    if key is None:
        return ""
    return site_url(f"{reverse('agent_policy')}#{device_anchor(key)}")


def placement_posture() -> str:
    """Where the deployment places agent calls, as the Agents page states it: the socket's path, or "" for no tailnet."""

    return tailscaled.socket_path()


def blocked_askers() -> Q:
    """Rows asked for from a blocked device, over ``requested_actor``, ``requested_interface`` and ``requested_device``.

    Matched by client and device whoever's sign-in it was, so a block withholds
    no less than it names.
    """

    return Q(
        Exists(
            AgentDevice.objects.filter(
                blocked_at__isnull=False,
                identity__client_id=OuterRef("requested_actor"),
                node_id=OuterRef("requested_device"),
            )
        ),
        requested_interface__in=AGENT_SURFACES,
    )


@dataclass(frozen=True, slots=True)
class AgentDeviceApprovalCommand:
    """Whether this device may run this agent's calls."""

    approved: Annotated[bool, Describe("Whether the agent's calls from this device run.")]


class AgentDeviceApproved(TypedDict):
    """One device's approval, as the call left it."""

    ok: Literal[True]
    device: Annotated[str, Describe("The agent's device the call approved or withdrew, by its record's key.")]
    approved: Annotated[bool, Describe("Whether the agent's calls from this device run now.")]


@dataclass(frozen=True, slots=True)
class AgentDeviceBlockCommand:
    """Whether this device is blocked for this agent."""

    blocked: Annotated[bool, Describe("Whether the agent's calls from this device are refused.")]


class AgentDeviceBlocked(TypedDict):
    """One device's block, as the call left it."""

    ok: Literal[True]
    device: Annotated[str, Describe("The agent's device the call blocked or allowed, by its record's key.")]
    blocked: Annotated[bool, Describe("Whether the agent's calls from this device are refused now.")]


def set_device_approved(
    command: AgentDeviceApprovalCommand, *, principal: Principal, current_key: str
) -> AgentDeviceApproved:
    """Approve an agent's device, or withdraw the approval. Set, not toggled.

    A device an agent called from for a person is theirs to approve, since the
    approval is what says that device is the one they act from; a machine
    agent's is any person's who governs agents.
    """

    user = person(principal)
    approving = command.approved
    with transaction.atomic():
        row = _locked(current_key)
        if row.person_id is not None and row.person_id != user.pk:
            raise AuthorizationError(
                "This agent acts for another account, so only that account can approve its devices."
            )
        if (row.approved_at is not None) == approving:
            return {"ok": True, "device": str(row.pk), "approved": approving}
        row.approved_at = timezone.now() if approving else None
        row.approved_by = user if approving else None
        row.save(update_fields=["approved_at", "approved_by"])
        _record(row, f"{'Approved' if approving else 'Withdrew approval of'} {row}", user, approved=approving)
    return {"ok": True, "device": str(row.pk), "approved": approving}


def set_device_blocked(
    command: AgentDeviceBlockCommand, *, principal: Principal, current_key: str
) -> AgentDeviceBlocked:
    """Block an agent's device, or let it back in. Set, not toggled. People only."""

    user = person(principal)
    blocking = command.blocked
    with transaction.atomic():
        row = _locked(current_key)
        if (row.blocked_at is not None) == blocking:
            return {"ok": True, "device": str(row.pk), "blocked": blocking}
        row.blocked_at = timezone.now() if blocking else None
        row.blocked_by = user if blocking else None
        row.save(update_fields=["blocked_at", "blocked_by"])
        _record(row, f"{'Blocked' if blocking else 'Allowed'} {row}", user, blocked=blocking)
    return {"ok": True, "device": str(row.pk), "blocked": blocking}


def device_approved(key: str) -> bool:
    return AgentDevice.objects.filter(pk=_pk(key), approved_at__isnull=False).exists()


def device_blocked(key: str) -> bool:
    return AgentDevice.objects.filter(pk=_pk(key), blocked_at__isnull=False).exists()


def _pk(key: str) -> int:
    text = str(key).strip()
    return int(text) if text.isdigit() else 0


def _locked(key: str) -> AgentDevice:
    row = AgentDevice.objects.select_for_update().select_related("identity").filter(pk=_pk(key)).first()
    if row is None:
        raise RecordNotFound("No agent has called HQ from that device.")
    return row


def _record(row: AgentDevice, message: str, user, **state: bool) -> None:
    record_event(
        action=AuditLog.Action.UPDATED,
        obj=row,
        type_label=AUDIT_LABEL,
        message=message,
        metadata={"client_id": row.identity.client_id, "device": row.node_id, **state},
        user=user,
        required=True,
    )
