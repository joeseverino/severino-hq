"""Each agent HQ has seen, as the Agents page shows it: its grant against the
deployment's ceiling and its person's role, and what it did and was refused lately.

The audit log names an agent's client as each event's ``actor``, a column
indexed with the time, so one windowed query over the last ``RECENT`` reads
every agent's latest events and its counts together, and the page costs the
same however long the log or how many agents it holds.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from django.db.models import Case, CharField, Count, F, IntegerField, Q, Sum, Value, When, Window
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Cast, Coalesce, RowNumber
from django.utils import timezone

from hq.platform.core.models import AgentIdentity, AuditLog

from . import agent_registry, denials, roles
from .agent_limits import RATE_LIMITED, TOO_MANY_CALLS
from .security import agent_ceiling, agent_grant

# How far back the page reads, and how many of an agent's latest events it shows.
RECENT = timedelta(days=7)
SHOWN = 5

# The refusals an agent earns by asking too much, rather than for what it asked.
LIMITED = (RATE_LIMITED, TOO_MANY_CALLS)

DENIED = Q(action=AuditLog.Action.DENIED)


@dataclass(frozen=True, slots=True)
class Activity:
    """What one agent did over the window: its latest events, newest first, and the counts of all of them."""

    latest: tuple[AuditLog, ...] = ()
    changes: int = 0
    refused: int = 0
    limited: int = 0


@dataclass(frozen=True, slots=True)
class AgentStanding:
    """One agent: who it is and acts for, what of its grant is in force here, and what it did lately."""

    identity: AgentIdentity
    in_force: tuple[str, ...]
    # Granted by the identity provider, held back by this deployment's settings.
    held_back: tuple[str, ...]
    activity: Activity
    # Within the settings, but beyond what its person's role holds.
    beyond_role: tuple[str, ...] = ()
    # Its person's role ("" for none) and when their last sign-in confirmed it;
    # both empty for a machine's own token, which acts for nobody.
    role: str = ""
    role_confirmed: datetime | None = None

    @property
    def role_label(self) -> str:
        return roles.Role(self.role).label if self.role else ""

    @property
    def acts_for_nobody(self) -> bool:
        """Signed in by a person who holds no role, so every call it makes is refused."""

        return self.identity.person_id is not None and not self.role


def standings() -> list[AgentStanding]:
    """Every agent HQ has seen, in client order, with its person and their role read in the same query."""

    identities = list(AgentIdentity.objects.select_related("person__person_role"))
    ceiling = agent_ceiling()
    activity = recent_activity([identity.client_id for identity in identities])
    return [_standing(identity, ceiling, activity.get(identity.client_id, Activity())) for identity in identities]


def _standing(identity: AgentIdentity, ceiling: frozenset[str], activity: Activity) -> AgentStanding:
    granted = set(identity.granted)
    person = identity.person
    if person is None:
        return AgentStanding(identity, tuple(sorted(agent_grant(granted))), tuple(sorted(granted - ceiling)), activity)
    role = roles.role_of(person)
    # A person with no role holds nothing, so nothing is in force: the token
    # itself is refused (``security.agent_principal``), and the page says why.
    in_force = agent_grant(granted, roles.grants(role))
    return AgentStanding(
        identity,
        tuple(sorted(in_force)),
        tuple(sorted(granted - ceiling)),
        activity,
        beyond_role=tuple(sorted((granted & ceiling) - in_force)),
        role=role,
        role_confirmed=person.person_role.resolved_at if role else None,
    )


def recent_activity(clients: list[str]) -> dict[str, Activity]:
    """Each of ``clients``' events since ``RECENT`` ago, read in one query."""

    if not clients:
        return {}
    by_agent = {"partition_by": [F("actor")]}
    events = (
        AuditLog.objects.filter(actor__in=clients, created_at__gte=timezone.now() - RECENT)
        # HQ noting it saw the agent, or its grant change, is not something the agent did.
        .exclude(object_type=agent_registry.AUDIT_LABEL)
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
    # A coalesced refusal stands for every call it counted.
    counted = Coalesce(Cast(KeyTextTransform(denials.COUNT, "metadata"), IntegerField()), Value(1))
    return Sum(Case(When(DENIED & among if among else DENIED, then=counted), default=Value(0)))


def _text(key: str) -> Cast:
    # A key read as plain text. Compared as a key transform, SQLite reads each
    # value it is compared with as JSON, which a client id is not.
    return Cast(KeyTextTransform(key, "metadata"), CharField())
