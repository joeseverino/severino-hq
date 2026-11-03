"""One history across what HQ did and what its readings saw change.

Each source dates its own events in its own place: the audit log what anyone
did through HQ and when a reading's records changed (``inventory._record_change``),
GitHub its deployments, Docker when each container last started. The audit log
page lays them on one line, and a drift finding asks it what happened near when
it was first seen. Read only from what is already stored; nothing here polls.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import blake2s
from typing import Any

from django.db.models import (
    BooleanField,
    Case,
    CharField,
    Count,
    Exists,
    ExpressionWrapper,
    F,
    OuterRef,
    Q,
    QuerySet,
    Value,
    When,
    Window,
)
from django.db.models.functions import RowNumber
from django.utils import timezone

from hq.domains.control_plane.observations.github import REPOSITORY_KIND
from hq.domains.control_plane.observations.portainer import RUNTIME_KIND
lazy from hq.domains.control_plane.models import ReadRequest
lazy from hq.platform.core.audit import ROUTINE_EVENTS, audited_labels
lazy from hq.platform.core.models import AuditLog, object_type_label, object_type_plural

from .entity_links import container_link
from .facts import inventory_records
from .labels import lower_first, plural
from .timestamps import moment
from .ui import counted
lazy from .capabilities import capability_registry, capability_title
lazy from .glance import AUDIT_LABEL as DASHBOARD_AUDIT_TYPE
lazy from .inventory import READING_AUDIT_TYPE
lazy from .security import API_INTERFACE, WEB_INTERFACE

# How far either side of a moment counts as near it.
NEAR = timedelta(hours=6)
# The actions a person or HQ took that change something. Views, sign-ins and
# routine connection probes are left out of "near": a drift explained by a
# page view is noise.
_CHANGES = ("created", "updated", "deleted", "settings_changed", "failed", "imported", "uploaded")

# The most of what one event changed that a line of the history carries. The
# rest are counted beside them and listed on the event's own page, so what a
# page reads and draws grows with its events, never with one event's size.
SHOWN = 5

# How far a run of like moments reaches back from its newest: a host's boot
# or a `compose up` starts every container it holds within a few minutes, so
# a quarter of an hour holds a slow host's whole start, while starts an hour
# apart (two deploys, a crash and its restart) stay two events.
RUN = timedelta(minutes=15)

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

    return audited_labels()[ReadRequest]


def source_of_event():
    """The source an audit row belongs to, as a query expression.

    A reading that found its records changed is the one thing in the log that
    happened outside HQ; everything else was done through it.
    """

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
    # What a run of moments like it counts, and what happened to them all:
    # ("container", "started on docker-host-01"). Moments that share it, from
    # one source, within ``RUN``, read as one event. Empty: always its own.
    like: tuple[str, ...] = ()

    @property
    def external(self) -> bool:
        return self.url.startswith("https://")


@dataclass(frozen=True, slots=True)
class HistoryLine:
    """One line of the history: an event, or a moment another source dated.

    An event is the rows one operation wrote that were read, newest first, or
    a row that belongs to none. Its own rows are the ones about the operation
    itself (a job's ending, an approval's request and decision: the row whose
    object is the operation's id); the rest are the items it changed, the
    newest ``SHOWN`` read beneath it and the others counted in ``kinds``.
    """

    at: datetime
    events: tuple[Any, ...] = ()
    moment: Moment | None = None
    # The event a row is read beneath, by its fold: set on an item's own line.
    under: str = ""
    # How many items of each object type the event changed, every one, newest type first.
    kinds: tuple[tuple[str, int], ...] = ()
    # The event whose items past those shown this line counts.
    rest: HistoryLine | None = None
    # A run of like moments, newest first, read as one event (``RUN``).
    burst: tuple[Moment, ...] = ()

    @property
    def own(self) -> tuple[Any, ...]:
        first = self.events[0] if self.events else None
        if first is None or not first.operation_id:
            return ()
        return tuple(event for event in self.events if event.object_id == first.operation_id)

    @property
    def head(self) -> Any:
        """The row the event is named by and opens to, if one row stands for it."""

        own = self.own
        if own:
            return own[0]
        return self.events[0] if len(self.events) == 1 else None

    @property
    def event(self) -> Any:
        return self.head

    @property
    def page(self) -> Any:
        """The row whose page reads the whole event: its head, else its newest row."""

        head = self.head
        return head if head is not None else self.events[0]

    @property
    def items(self) -> tuple[Any, ...]:
        """What the event changed, newest first, as many as a line carries."""

        if len(self.events) < 2:
            return ()
        own = set(map(id, self.own))
        return tuple(event for event in self.events if id(event) not in own)[:SHOWN]

    @property
    def counts(self) -> tuple[tuple[str, int], ...]:
        """How many items of each object type it changed, newest type first."""

        # A row alone is the event itself, whatever operation wrote it.
        if len(self.events) < 2:
            return ()
        if self.kinds:
            return self.kinds
        found: dict[str, int] = {}
        for item in self.items:
            found[item.object_type] = found.get(item.object_type, 0) + 1
        return tuple(found.items())

    @property
    def total(self) -> int:
        """How many items it changed, shown or not; a run, how many moments it holds."""

        if self.burst:
            return len(self.burst)
        return sum(count for _kind, count in self.counts)

    @property
    def shown(self) -> tuple[HistoryLine, ...]:
        """The lines read beneath it: its newest ``SHOWN`` items, or moments of a run."""

        fold = self.fold
        if self.burst:
            return tuple(HistoryLine(item.at, moment=item, under=fold) for item in self.burst[:SHOWN])
        return tuple(HistoryLine(item.created_at, events=(item,), under=fold) for item in self.items)

    @property
    def more(self) -> int:
        """How many of its items its line leaves to its page."""

        return self.total - min(self.total, SHOWN) if self.burst else self.total - len(self.items)

    @property
    def more_words(self) -> str:
        """The count of what its line leaves out, in what it counts."""

        if self.burst:
            noun = self.burst[0].like[0]
            return counted(self.more, f"more {noun}", f"more {plural(noun)}")
        return counted(self.more, "more change", "more changes")

    @property
    def grouped(self) -> bool:
        return bool(self.burst or self.items)

    @property
    def pk(self) -> int | None:
        """The row's id for selection; an event no one row stands for, or a moment, has none."""

        head = self.head
        return head.pk if head is not None else None

    @property
    def fold(self) -> str:
        """The id its items name, to be read beneath it."""

        if self.burst:
            first = self.burst[0]
            return f"run-{blake2s(repr((first.source, first.like, first.at)).encode(), digest_size=6).hexdigest()}"
        return f"event-{self.events[-1].pk}" if self.grouped else ""

    @property
    def lines(self) -> tuple[HistoryLine, ...]:
        """This line, then a line of each item beneath it, and one counting the rest."""

        rest = (HistoryLine(self.at, under=self.fold, rest=self),) if self.more else ()
        return (self, *self.shown, *rest)

    @property
    def title(self) -> str:
        """What the event was: its own row's name, else its operation's words, else what it changed."""

        if self.burst:
            noun, happened = self.burst[0].like
            return f"{counted(self.total, noun)} {happened}"
        head = self.head
        if head is not None:
            return head.object_repr or head.type_label
        newest = self.events[0]
        named = operation_title(str((newest.metadata or {}).get("operation") or ""), newest.interface)
        if named:
            return named
        # Nothing declared its words, so the event is named by what it changed.
        name = newest.object_repr or newest.type_label
        return f"{name} and {counted(self.total - 1, 'more', 'more')}"

    @property
    def type_label(self) -> str:
        head = self.head
        return head.type_label if head is not None and head.object_repr else ""

    @property
    def actor(self) -> str:
        """Whoever started it: the actor of its first row."""

        return self.events[-1].actor_label

    @property
    def action(self) -> str:
        """How it came out: its own row's action, else the one its items share, else the latest."""

        head = self.head
        if head is not None:
            return head.action
        actions = {event.action for event in self.events}
        return actions.pop() if len(actions) == 1 else self.events[0].action

    @property
    def action_label(self) -> str:
        return AuditLog.Action(self.action).label if self.action in AuditLog.Action.values else self.action

    @property
    def message(self) -> str:
        """What its own row said, else what it changed, by type."""

        head = self.head
        if head is not None:
            return head.message
        return ", ".join(
            counted(count, lower_first(object_type_label(kind)), lower_first(object_type_plural(kind)))
            if kind
            else counted(count, "event")
            for kind, count in self.counts
        )


def operation_title(operation: str, interface: str) -> str:
    """What a person calls ``operation``, in the words of whatever declared it,
    or nothing when nothing did.

    A capability declares its title. A request through the web or the API is
    named by its route, which is a name for code and declares no words, so a
    request is told by what it changed instead.
    """

    if not operation or (interface in {WEB_INTERFACE, API_INTERFACE} and operation not in capability_registry()):
        return ""
    return capability_title(operation)


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


def heads(rows: QuerySet) -> QuerySet:
    """One row per event in ``rows``: the newest of each operation's, and every
    row that belongs to none.

    A row is an operation's newest unless one of its siblings in ``rows`` came
    later, so a page of these is a page of events, read from the indexes on
    ``created_at`` and ``operation_id`` with nothing counted.
    """

    later = rows.order_by().filter(
        Q(created_at__gt=OuterRef("created_at")) | Q(created_at=OuterRef("created_at"), pk__gt=OuterRef("pk")),
        operation_id=OuterRef("operation_id"),
    )
    return rows.filter(Q(operation_id="") | ~Exists(later))


def entries(
    events: Sequence[Any], found: Iterable[Moment] = (), *, within: QuerySet | None = None
) -> list[HistoryLine]:
    """Events and moments on one line, newest first.

    ``events`` is one row per event (``heads``), newest first; each operation's
    other rows are read from ``within``, in one query, so it lists what the
    event changed. Without ``within`` each row is a line of its own.
    """

    siblings: dict[str, list[Any]] = {}
    kinds: dict[str, dict[str, int]] = {}
    operations = {event.operation_id for event in events if event.operation_id}
    if within is not None and operations:
        for row in _newest_of_each_kind(within, operations):
            siblings.setdefault(row.operation_id, []).append(row)
            if not row.own_row:
                kinds.setdefault(row.operation_id, {}).setdefault(row.object_type, row.of_kind)
    lines: list[HistoryLine] = [
        HistoryLine(
            event.created_at,
            events=tuple(siblings.get(event.operation_id) or (event,)),
            kinds=tuple(kinds.get(event.operation_id, {}).items()),
        )
        for event in events
    ]
    lines += runs(found)
    lines.sort(key=lambda line: line.at, reverse=True)
    return lines


def runs(found: Iterable[Moment]) -> list[HistoryLine]:
    """Moments as lines, newest first: each run of like ones (``Moment.like``,
    from one source, within ``RUN`` of the run's newest) as one line, every
    other moment a line of its own."""

    gathered: list[list[Moment]] = []
    open_runs: dict[tuple[str, tuple[str, ...]], list[Moment]] = {}
    for item in sorted(found, key=lambda each: each.at, reverse=True):
        key = (item.source, item.like)
        run = open_runs.get(key) if item.like else None
        if run is not None and run[0].at - item.at <= RUN:
            run.append(item)
            continue
        run = [item]
        gathered.append(run)
        if item.like:
            open_runs[key] = run
    return [
        HistoryLine(run[0].at, burst=tuple(run)) if len(run) > 1 else HistoryLine(run[0].at, moment=run[0])
        for run in gathered
    ]


def _newest_of_each_kind(within: QuerySet, operations: Iterable[str]) -> QuerySet:
    """The operations' rows a line can carry, newest first, each counted with its kind.

    The newest ``SHOWN`` of each operation's own rows and of each object type
    it changed, which hold the newest ``SHOWN`` items of all: the rows read
    grow with the page and the types of record, never with one operation.
    """

    own = ExpressionWrapper(Q(object_id=F("operation_id")), output_field=BooleanField())
    kind = (F("operation_id"), own, F("object_type"))
    return (
        within.filter(operation_id__in=operations)
        .annotate(
            own_row=own,
            place=Window(RowNumber(), partition_by=kind, order_by=(F("created_at").desc(), F("pk").desc())),
            of_kind=Window(Count("pk"), partition_by=kind),
        )
        .filter(place__lte=SHOWN)
        .order_by("-created_at", "-pk")
    )


def read_requests() -> Q:
    """The rows that are an ask for a fresh reading, as a filter.

    Asks the dashboard made before it filed them as read requests carry the
    panels they named.
    """

    return Q(object_type=read_request_type()) | Q(object_type=DASHBOARD_AUDIT_TYPE, metadata__has_key="panels")


def routine() -> Q:
    """HQ looking with nobody asking: the rows ``prune_routine`` removes in time."""

    kinds = Q(pk__in=[])
    for action, object_type in ROUTINE_EVENTS:
        kinds |= Q(action=action, object_type=object_type)
    return kinds & Q(user__isnull=True)


def recent(*, limit: int, reads: bool = False, quiet: bool = False) -> list[HistoryLine]:
    """The newest ``limit`` events, read requests and routine work left out unless asked for."""

    rows = AuditLog.objects.select_related("user")
    if not reads:
        rows = rows.exclude(read_requests())
    if not quiet:
        rows = rows.exclude(routine())
    page = list(heads(rows).order_by("-created_at", "-pk")[:limit])
    return entries(page, within=rows)


def changes(since: datetime, until: datetime):
    """The audit rows that changed something, as the history counts them."""

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
                    like=(
                        "deploy",
                        (
                            f"of {record.get('repository') or 'a repository'} to "
                            f"{deployment.get('environment') or 'an environment'}"
                        ),
                    ),
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
                    like=("container", f"started on {record.get('host')}"),
                )
            )
    return found
