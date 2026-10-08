"""One history across what HQ did and what its readings saw change.

Each connection dates its own events in its own place: HQ's audit log holds
what anyone did through HQ and when a reading's records changed between two
sweeps (``inventory._record_change``), GitHub its deployments, Docker when
each container last started. The audit log page lays them on one line, and a
drift finding asks the same history what happened near the moment it was
first seen.

Read only from what is already stored; nothing here polls.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from django.db.models import Case, CharField, Q, Value, When
from django.utils import timezone

from hq.domains.control_plane.observations.github import REPOSITORY_KIND
from hq.domains.control_plane.observations.portainer import RUNTIME_KIND

from .entity_links import container_link
from .facts import inventory_records
from .labels import lower_first
from .timestamps import moment
from .ui import counted

# How far either side of a moment counts as near it.
NEAR = timedelta(hours=6)
# Consecutive events of one verb by one actor on one type of thing, each within
# this of the last, read as one piece of work: a sweep adopting fourteen
# containers is one line, not fourteen.
GROUP_GAP = timedelta(minutes=5)
# The actions a person or HQ took that change something. Views, sign-ins and
# routine connection probes are left out of "near": a drift explained by a
# page view is noise.
_CHANGES = ("created", "updated", "deleted", "settings_changed", "failed", "imported", "uploaded")

# Where an entry was recorded, as the audit page filters by it.
THROUGH_HQ = "hq"
OUTSIDE_HQ = "outside"
DEPLOYS = "deploy"
CONTAINERS = "container"
SOURCES = (
    (THROUGH_HQ, "Done in HQ"),
    (OUTSIDE_HQ, "Changed outside HQ"),
    (DEPLOYS, "Deploys"),
    (CONTAINERS, "Containers"),
)
# The sources that live in a connection's reading rather than in the audit log.
EXTERNAL = frozenset({DEPLOYS, CONTAINERS})


def read_request_type() -> str:
    """What the log calls an ask for a fresh reading.

    HQ records one each time a page asks the controller to read, and removes
    it when the reading arrives. Neither is a change to anything he runs, so
    the log leaves them out until asked.
    """

    from hq.domains.control_plane.models import ReadRequest
    from hq.platform.core.audit import audited_labels

    return audited_labels()[ReadRequest]


def source_of_event():
    """The source an audit row belongs to, as a query expression.

    A reading that found its records changed is the one thing in the log that
    happened outside HQ; everything else was done through it.
    """

    from hq.platform.core.models import AuditLog

    from .inventory import READING_AUDIT_TYPE

    return Case(
        When(
            Q(action=AuditLog.Action.OBSERVED, object_type=READING_AUDIT_TYPE),
            then=Value(OUTSIDE_HQ),
        ),
        default=Value(THROUGH_HQ),
        output_field=CharField(),
    )


@dataclass(frozen=True, slots=True)
class Moment:
    at: datetime
    # Where it was recorded: "HQ", "Reading", "Deploy", "Container".
    source: str
    title: str
    detail: str = ""
    url: str = ""
    # Who dated it, when it is not HQ's own log.
    actor: str = ""

    @property
    def external(self) -> bool:
        return self.url.startswith("https://")


@dataclass(frozen=True, slots=True)
class Entry:
    """One line of the history: an audit event, a run of them, or a moment."""

    at: datetime
    events: tuple[Any, ...] = ()
    moment: Moment | None = None

    @property
    def event(self) -> Any:
        return self.events[0] if len(self.events) == 1 else None

    @property
    def grouped(self) -> bool:
        return len(self.events) > 1

    @property
    def pk(self) -> int | None:
        """The row's id for selection; a run or a moment has none of its own."""

        return self.event.pk if self.event is not None else None

    @property
    def title(self) -> str:
        """A run as its object cell: "14 containers", or "operator · 4 times".

        The row's own columns already say the action and who did it, so the
        run names only what it happened to.
        """

        first = self.events[0]
        objects = {event.object_repr for event in self.events}
        if len(objects) == 1 and first.object_repr:
            # One thing, again and again: "operator · 4 times", not "4 users".
            return f"{first.object_repr} · {len(self.events)} times"
        if not first.type_label:
            return counted(len(objects), "event")
        return counted(len(objects), lower_first(first.type_label), lower_first(first.type_plural))


def moments(*, since: datetime, until: datetime | None = None, limit: int = 200) -> tuple[Moment, ...]:
    """Every change dated between ``since`` and ``until``, newest first."""

    until = until or timezone.now()
    found = [*_recorded(since, until), *external(EXTERNAL, since=since, until=until)]
    return tuple(sorted(found, key=lambda item: item.at, reverse=True)[:limit])


def near(at: datetime, *, window: timedelta = NEAR, limit: int = 5) -> tuple[Moment, ...]:
    """What happened close to ``at``, closest first."""

    around = moments(since=at - window, until=at + window)
    return tuple(sorted(around, key=lambda item: abs(item.at - at))[:limit])


def external(sources: Iterable[str], *, since: datetime | None = None, until: datetime | None = None) -> list[Moment]:
    """The deploys and container starts HQ's readings hold, within the bounds."""

    wanted = set(sources)
    found = []
    if DEPLOYS in wanted:
        found += _deploys(since, until)
    if CONTAINERS in wanted:
        found += _starts(since, until)
    return found


def entries(events: Sequence[Any], found: Iterable[Moment] = (), *, group: bool = True) -> list[Entry]:
    """Events and moments on one line, newest first, runs of like events as one.

    ``events`` must already be newest first. A run is consecutive events of one
    verb by one actor on one type of thing, each within ``GROUP_GAP`` of the
    last; a moment from another source between them ends it.
    """

    lines: list[Entry] = [Entry(event.created_at, events=(event,)) for event in events]
    lines += [Entry(item.at, moment=item) for item in found]
    lines.sort(key=lambda line: line.at, reverse=True)
    return _grouped(lines) if group else lines


def _grouped(lines: list[Entry]) -> list[Entry]:
    merged: list[Entry] = []
    for line in lines:
        last = merged[-1] if merged else None
        if last is not None and _joins(last, line):
            merged[-1] = Entry(last.at, events=(*last.events, *line.events))
        else:
            merged.append(line)
    return merged


def _key(event: Any) -> tuple[str, str, str]:
    return (event.action, event.actor_label, event.object_type)


def _joins(run: Entry, line: Entry) -> bool:
    if not run.events or not line.events:
        return False
    previous, event = run.events[-1], line.events[0]
    return _key(previous) == _key(event) and previous.created_at - event.created_at <= GROUP_GAP


def changes(since: datetime, until: datetime):
    """The audit rows that changed something, as the history counts them."""

    from hq.platform.core.models import AuditLog

    return (
        AuditLog.objects.filter(created_at__gte=since, created_at__lte=until)
        .annotate(source=source_of_event())
        .filter(Q(action__in=_CHANGES) | Q(source=OUTSIDE_HQ))
    )


def _recorded(since: datetime, until: datetime) -> list[Moment]:
    events = changes(since, until).select_related("user").order_by("-created_at")[:500]
    return [
        Moment(event.created_at, "Reading", event.message)
        if event.source == OUTSIDE_HQ
        else Moment(
            event.created_at,
            "HQ",
            f"{event.get_action_display()} {event.object_repr or event.type_label}".strip(),
            detail=event.actor_label,
        )
        for event in events
    ]


def _between(stamp: object, since: datetime | None, until: datetime | None) -> datetime | None:
    at = moment(stamp)
    if at is None or (since is not None and at < since) or (until is not None and at > until):
        return None
    return at


def _deploys(since: datetime | None, until: datetime | None) -> list[Moment]:
    found = []
    for _snapshot, record in inventory_records(REPOSITORY_KIND):
        for deployment in record.get("deployments") or ():
            at = _between(deployment.get("created_at"), since, until)
            if at is None:
                continue
            sha = str(deployment.get("sha") or "")[:7]
            found.append(
                Moment(
                    at,
                    "Deploy",
                    f"Deployed {sha} to {deployment.get('environment') or 'an environment'}".replace("  ", " "),
                    detail=str(record.get("repository") or ""),
                    url=str(deployment.get("url") or ""),
                    actor="GitHub",
                )
            )
    return found


def _starts(since: datetime | None, until: datetime | None) -> list[Moment]:
    found = []
    for _snapshot, record in inventory_records(RUNTIME_KIND):
        at = _between(record.get("started_at"), since, until)
        if at is not None:
            found.append(
                Moment(
                    at,
                    "Container",
                    f"{record.get('container')} started on {record.get('host')}",
                    detail="created, recreated or restarted",
                    # Its row on its machine's page: a start is dated for every
                    # container, tracked or not.
                    url=container_link(str(record.get("host") or ""), str(record.get("container") or "")).url,
                    actor="Docker",
                )
            )
    return found
