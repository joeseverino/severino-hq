"""Each agent HQ has seen, as the Agents page shows it: its grant against its
person's role, the devices it called from, what it did and was refused lately,
and the listen streams it holds open now.

The audit log names an agent's client as each event's ``actor`` and its device
as ``device``, columns indexed with the time, so one windowed query over the
last ``RECENT`` reads every agent's latest events and its counts together, one
more each device's, and the page costs the same however long the log or how
many agents and devices it holds.
"""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from django.conf import settings
from django.db.models import Case, CharField, Count, F, IntegerField, Max, Min, Q, Sum, Value, When, Window
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Cast, Coalesce, RowNumber
from django.utils import timezone

from hq.platform.core.models import AgentDevice, AgentIdentity, AuditLog

from . import agent_devices, agent_limits, agent_registry, denials, roles, tailnet
from .agent_access import DEVICE_AWAITING_APPROVAL
from .agent_devices import DeviceStanding
from .agent_limits import RATE_LIMITED, TOO_MANY_CALLS
from .security import (
    MCP_INTERFACE,
    Capability,
    Principal,
    agent_grant,
    agent_grantable,
    could_ever_hold,
    web_principal,
)

# How far back the page reads, and how many of an agent's latest events it shows.
RECENT = timedelta(days=7)
SHOWN = 5

# How a provider's permission list is written from HQ's catalog, which the
# page's steps for adding an agent and a missing permission's fix point at.
CLIENT_SETUP = "https://github.com/joeseverino/severino-hq/blob/main/docs/API.md#setting-up-a-client"

# The refusals an agent earns by asking too much, rather than for what it asked.
LIMITED = (RATE_LIMITED, TOO_MANY_CALLS)

DENIED = Q(action=AuditLog.Action.DENIED)

# HQ noting it saw an agent or one of its devices, or a person changing either,
# is not something the agent did.
NOT_CALLS = (agent_registry.AUDIT_LABEL, agent_devices.AUDIT_LABEL)


def agent_name(identity: AgentIdentity) -> str:
    """What a person reads an agent as. The app's own name is the identity
    provider's, which HQ holds no key to read, so it is the client id."""

    return identity.client_id


# What each standing is called on the page, and its pill's tone.
_STANDING_WORDS = {
    DeviceStanding.APPROVED: ("Approved", "pill-good"),
    DeviceStanding.AWAITING: ("Awaiting approval", "pill-attention"),
    DeviceStanding.BLOCKED: ("Blocked", "pill-serious"),
    DeviceStanding.UNSEEN: ("Not seen yet", ""),
}


@dataclass(frozen=True, slots=True)
class DeviceRow:
    """One device an agent called from, as its row on the Agents page shows it.

    A device HQ has a record of (``record``) stands as that record says. A call
    from a tailnet address the last reading did not hold has none, so nothing
    can be approved yet: it is ``UNSEEN``, named by its ``address``.
    """

    standing: DeviceStanding
    name: str
    first_seen: datetime | None
    last_seen: datetime | None
    calls: int = 0
    refused: int = 0
    os: str = ""
    owner: str = ""
    record: AgentDevice | None = None
    address: str = ""
    # Whether the person reading may approve it: theirs, or a machine's.
    yours: bool = False

    @property
    def label(self) -> str:
        return _STANDING_WORDS[self.standing][0]

    @property
    def tone(self) -> str:
        """The pill it is drawn with."""

        return _STANDING_WORDS[self.standing][1]

    @property
    def waits_for(self) -> str:
        """Whom an approval waits on, when it is not the reader."""

        record = self.record
        if self.standing is not DeviceStanding.AWAITING or self.yours or record is None or record.person is None:
            return ""
        return record.person.get_username()

    @property
    def anchor(self) -> str:
        """The row's id on the page, which a refusal from the device points at."""

        if self.record is not None:
            return agent_devices.device_anchor(self.record.pk)
        return agent_devices.device_anchor(f"unseen-{self.address.replace(':', '-').replace('.', '-')}")


@dataclass(frozen=True, slots=True)
class DevicePrompt:
    """A device awaiting the approval of the person reading: what it asks, and the row it is on."""

    agent: str
    device: DeviceRow
    # Whom it acts for: the reader, or nobody for a machine's own token.
    for_you: bool

    @property
    def text(self) -> str:
        owner = f"owned by {self.device.owner}" if self.device.owner else "a tagged device"
        acts = "wants to act for you from" if self.for_you else "wants to act from"
        return f"{self.agent} {acts} {self.device.name}, {owner}"


@dataclass(frozen=True, slots=True)
class Activity:
    """What one agent did over the window: its latest events, newest first, and the counts of all of them."""

    latest: tuple[AuditLog, ...] = ()
    changes: int = 0
    refused: int = 0
    limited: int = 0


@dataclass(frozen=True, slots=True)
class Streams:
    """The listen streams an agent holds open now, against how many it may.

    Counted by this process's limits, which hold every stream, since HQ serves
    from one process.
    """

    open: int = 0
    cap: int = 0

    @property
    def full(self) -> bool:
        """Every one it may hold is open, so its next is refused ``too_many_calls``."""

        return bool(self.cap) and self.open >= self.cap


@dataclass(frozen=True, slots=True)
class AgentStanding:
    """One agent: who it is and acts for, what of its grant is in force here, and what it did lately."""

    identity: AgentIdentity
    in_force: tuple[str, ...]
    activity: Activity
    # Granted, but beyond what its person's role holds.
    beyond_role: tuple[str, ...] = ()
    # What HQ offers an agent within its person's role that their last sign-in
    # did not carry: what a refusal names as ``insufficient_scope``, and what a
    # sign-in that asks for every permission is refused for until the identity
    # provider grants it. None for a machine's own token, which asks only for
    # what its automation needs.
    missing: tuple[str, ...] = ()
    # Its person's role ("" for none) and when their last sign-in confirmed it;
    # both empty for a machine's own token, which acts for nobody.
    role: str = ""
    role_confirmed: datetime | None = None
    devices: tuple[DeviceRow, ...] = ()
    streams: Streams = Streams()

    @property
    def role_label(self) -> str:
        return roles.Role(self.role).label if self.role else ""

    @property
    def acts_for_nobody(self) -> bool:
        """Signed in by a person who holds no role, so every call it makes is refused."""

        return self.identity.person_id is not None and not self.role


def standings(viewer=None) -> list[AgentStanding]:
    """Every agent HQ has seen, in client order, with its person, their role and who blocked it read in the same query.

    Each carries its devices, marked ``yours`` where ``viewer`` is the one to
    approve them, and the listen streams it holds open.
    """

    identities = list(AgentIdentity.objects.select_related("person__person_role", "blocked_by"))
    clients = [identity.client_id for identity in identities]
    activity = recent_activity(clients)
    devices = device_rows(identities, viewer)
    cap = settings.SEVERINO_AGENT_STREAMS_AT_ONCE
    return [
        replace(
            _standing(identity, activity.get(identity.client_id, Activity())),
            devices=devices.get(identity.client_id, ()),
            streams=Streams(agent_limits.LIMITS.listening(identity.client_id), cap),
        )
        for identity in identities
    ]


def prompts(found: list[AgentStanding]) -> list[DevicePrompt]:
    """Each device in ``found`` awaiting the approval of the person they were read for."""

    return [
        DevicePrompt(agent_name(standing.identity), device, for_you=device.record.person_id is not None)
        for standing in found
        for device in standing.devices
        if device.standing is DeviceStanding.AWAITING and device.yours and device.record is not None
    ]


def device_rows(identities: list[AgentIdentity], viewer=None) -> dict[str, tuple[DeviceRow, ...]]:
    """Each of ``identities``' devices, by client id: its records, then the tailnet
    addresses it was refused from as unseen, each with its calls over the window."""

    if not identities:
        return {}
    clients = {identity.pk: identity.client_id for identity in identities}
    governs = viewer is not None and web_principal(viewer).permits(Capability.GOVERN_AGENTS)
    calls = _device_calls(list(clients.values()))
    found: dict[str, list[DeviceRow]] = {}
    for record in AgentDevice.objects.filter(identity_id__in=clients).select_related(
        "person", "approved_by", "blocked_by"
    ):
        client = clients[record.identity_id]
        counted = calls.get((client, record.node_id), (0, 0))
        yours = governs and (record.person_id is None or (viewer is not None and record.person_id == viewer.pk))
        found.setdefault(client, []).append(
            DeviceRow(
                _record_standing(record),
                record.name or record.node_id,
                record.first_seen,
                record.last_seen,
                *counted,
                os=record.os,
                owner=record.owner,
                record=record,
                yours=yours,
            )
        )
    for client, rows in _unseen(list(clients.values())).items():
        found.setdefault(client, []).extend(rows)
    return {client: tuple(rows) for client, rows in found.items()}


def _record_standing(record: AgentDevice) -> DeviceStanding:
    # As ``agent_devices.standing`` reads the same record for a call.
    if record.blocked_at is not None:
        return DeviceStanding.BLOCKED
    if record.approved_at is None:
        return DeviceStanding.AWAITING
    return DeviceStanding.APPROVED


def _device_calls(clients: list[str]) -> dict[tuple[str, str], tuple[int, int]]:
    """Each client's calls and refusals from each device since ``RECENT`` ago, in one query."""

    rows = (
        _calls_of(clients).exclude(device="").values("actor", "device").annotate(calls=_counted(), refused=_refusals())
    )
    return {(row["actor"], row["device"]): (row["calls"] or 0, row["refused"] or 0) for row in rows}


def _unseen(clients: list[str]) -> dict[str, list[DeviceRow]]:
    """The tailnet addresses each client was refused from as unseen, while the
    reading still holds no device there; a device it holds since has its own record."""

    rows = list(
        _calls_of(clients)
        .filter(DENIED, device="")
        .annotate(reason=_text("reason"), address=_text("source"))
        .filter(reason=DEVICE_AWAITING_APPROVAL.code)
        .exclude(address="")
        .values("actor", "address")
        .annotate(calls=_counted(), first=Min("created_at"), last=Max("created_at"))
        .order_by("actor", "address")
    )
    if not rows:
        return {}
    known = tailnet.devices()
    found: dict[str, list[DeviceRow]] = {}
    for row in rows:
        held = tailnet.device_at(row["address"], known)
        if held is not None and held.node_id:
            continue
        found.setdefault(row["actor"], []).append(
            DeviceRow(
                DeviceStanding.UNSEEN,
                row["address"],
                row["first"],
                row["last"],
                row["calls"] or 0,
                row["calls"] or 0,
                address=row["address"],
            )
        )
    return found


def _calls_of(clients: list[str]):
    return AuditLog.objects.filter(actor__in=clients, created_at__gte=timezone.now() - RECENT).exclude(
        object_type__in=NOT_CALLS
    )


def _standing(identity: AgentIdentity, activity: Activity) -> AgentStanding:
    granted = agent_grant(identity.granted)
    person = identity.person
    if person is None:
        # A machine's own token has no person, so what keeps a person's is
        # refused it whatever its grant, by the question ``require`` asks.
        nobody = Principal(identity.client_id, MCP_INTERFACE, granted)
        in_force = (capability for capability in granted if could_ever_hold(nobody, (capability,)))
        return AgentStanding(identity, tuple(sorted(in_force)), activity)
    role = roles.role_of(person)
    # A person with no role holds nothing, so nothing is in force: the token
    # itself is refused (``security.agent_principal``), and the page says why.
    held = roles.grants(role)
    in_force = agent_grant(granted, held)
    return AgentStanding(
        identity,
        tuple(sorted(in_force)),
        activity,
        beyond_role=tuple(sorted(granted - in_force)),
        missing=tuple(sorted(agent_grant(agent_grantable(), held) - granted)),
        role=role,
        role_confirmed=person.person_role.resolved_at if role else None,
    )


def recent_activity(clients: list[str]) -> dict[str, Activity]:
    """Each of ``clients``' events since ``RECENT`` ago, read in one query."""

    if not clients:
        return {}
    by_agent = {"partition_by": [F("actor")]}
    events = (
        _calls_of(clients)
        .annotate(reason=_text("reason"))
        .annotate(
            rank=Window(RowNumber(), order_by=[F("created_at").desc(), F("id").desc()], **by_agent),
            changes=Window(Count("id", filter=~DENIED), **by_agent),
            refused=Window(_refusals(), **by_agent),
            limited=Window(_refusals(Q(reason__in=LIMITED)), **by_agent),
        )
        .filter(rank__lte=SHOWN)
        .order_by("actor", "rank")
    )
    found: dict[str, list[AuditLog]] = {}
    for event in events:
        found.setdefault(event.actor, []).append(event)
    return {
        agent: Activity(tuple(latest), latest[0].changes, latest[0].refused or 0, latest[0].limited or 0)
        for agent, latest in found.items()
    }


def refused_since(since: datetime) -> int:
    """Every call refused since ``since``, by any actor, counted as the Agents page's rows count them."""

    return AuditLog.objects.filter(DENIED, created_at__gte=since).aggregate(refused=_refusals())["refused"] or 0


def _refusals(among: Q | None = None) -> Sum:
    return Sum(Case(When(DENIED & among if among else DENIED, then=_each()), default=Value(0)))


def _counted() -> Sum:
    """Every call the rows stand for."""

    return Sum(_each())


def _each() -> Coalesce:
    # A coalesced refusal stands for every call it counted.
    return Coalesce(Cast(KeyTextTransform(denials.COUNT, "metadata"), IntegerField()), Value(1))


def _text(key: str) -> Cast:
    # A key read as plain text. Compared as a key transform, SQLite reads each
    # value it is compared with as JSON, which a client id is not.
    return Cast(KeyTextTransform(key, "metadata"), CharField())
