"""One history across what HQ did and what its readings saw change.

Each source dates its own events in its own place: the audit log what anyone
did through HQ and when a reading's records changed (``inventory._record_change``),
GitHub its deployments, Docker when each container last started. The audit log
page lays them on one line, and a drift finding asks it what happened near when
it was first seen. Read only from what is already stored; nothing here polls.
"""

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from hashlib import blake2s
from typing import Any
from uuid import UUID

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
    Subquery,
    Value,
    When,
    Window,
)
from django.db.models.functions import RowNumber
from django.utils import timezone

from hq.domains.control_plane.observations.github import REPOSITORY_KIND
from hq.domains.control_plane.observations.portainer import RUNTIME_KIND
lazy from hq.domains.control_plane.models import ApprovalRequest, OperationRequest, ReadRequest
lazy from hq.platform.core.audit import ROUTINE_EVENTS, audited_labels, subject_kind
lazy from hq.platform.core.audit_words import words_of
lazy from hq.platform.core.models import AuditLog, object_type_label, object_type_plural

from .doors import Attribution
from .entity_links import container_link, entity_link, web_url
from .facts import inventory_records
from .figured import Figure
from .labels import lower_first, plural
from .moments import duration, epoch, moment
from .projection import read_once
from .quoted import Quoted
from .routes import reverse
from .tables import Cell, DataTable, Group, Row, TableColumn
from .ui import counted
from .url_query import with_query
lazy from .inventory import READING_AUDIT_TYPE
lazy from .work_runs import run_url

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

# The audit list's filter to every row of one chain, by its id, and the ids it takes.
CHAIN = "chain"
CHAIN_PATTERN = r"^[\w.:-]{0,128}$"
# What every page that draws a chain heads it: its rows are the audit log's events.
EVENTS = "Events"

# How far a run of like moments reaches back from its newest: a host's boot
# or a `compose up` starts every container it holds within a few minutes, so
# a quarter of an hour holds a slow host's whole start, while starts an hour
# apart (two deploys, a crash and its restart) stay two events.
RUN = timedelta(minutes=15)

# The columns of every table of history (``partials/_history_table.html``),
# the audit log's among them: each heading and its column's css. ``WHO`` is
# the column a reader who may not read the audit log is not shown. Action
# stands only in a wide box, since a narrow row says it in its name.
COLUMNS = (
    ("Type", ""),
    ("Time", "date-col key-col"),
    ("User", "optional-col"),
    ("Action", "wide-only"),
    ("Message", ""),
)
WHO = "User"

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
        return bool(web_url(self.url))

    @property
    def page(self) -> str:
        """The page in HQ the moment is shown on, without the place on it, or nothing."""

        return "" if self.external else self.url.partition("#")[0]


@dataclass(frozen=True, slots=True)
class HistoryLine:
    """One line of the history: an event, or a moment another source dated.

    An event is the rows one chain of operations wrote that were read, newest
    first (``AuditLog.trace``), or a row that belongs to none. Its own rows
    are the ones about an operation itself (a job's ending, an approval's
    request and decision: the row whose object is the operation's id); the
    rest are the items it changed. A chain of one operation reads the newest
    ``SHOWN`` items beneath it and counts the others in ``kinds``; a chain of
    several reads each operation beneath it as a step, in the order they ran.
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
    # Each operation of a chain of several, oldest first, as a line of its own.
    steps: tuple[HistoryLine, ...] = ()
    # Where a step's title, and each row beneath it, opens (``_step_pages``).
    opens: str = ""
    # A later step's: when its chain's first step was, so its time says how long after.
    began: datetime | None = None

    @property
    def own(self) -> tuple[Any, ...]:
        return tuple(event for event in self.events if event.operation_id and event.object_id == event.operation_id)

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

        if len(self.events) < 2 or self.steps:
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
        return tuple(Counter(item.object_type for item in self.items).items())

    @property
    def total(self) -> int:
        """How many items it changed, shown or not; a run, how many moments it holds."""

        if self.burst:
            return len(self.burst)
        if self.steps:
            return len(self.steps)
        return sum(count for _kind, count in self.counts)

    @property
    def shown(self) -> tuple[HistoryLine, ...]:
        """The lines read beneath it: its newest ``SHOWN`` items, or moments of a run."""

        fold = self.fold
        if self.burst:
            return tuple(HistoryLine(item.at, moment=item, under=fold) for item in self.burst[:SHOWN])
        if self.steps:
            return tuple(replace(step, under=fold) for step in self.steps)
        return tuple(HistoryLine(item.created_at, events=(item,), under=fold, opens=self.opens) for item in self.items)

    @property
    def more(self) -> int:
        """How many of its items its line leaves to its page: none of a chain's steps."""

        if self.steps:
            return 0
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
        """Whether lines fold beneath this one: never beneath a line it is folded under."""

        return bool(self.burst or self.items or self.steps) and not self.under

    @property
    def url(self) -> str:
        """Where the line's title opens, or nothing when it has nowhere to go.

        An event opens its own row's page, else its newest row's, which reads
        its whole chain; a step of a chain its run or its record. The row
        counting what a fold leaves out opens the log of every row of its
        operation. A moment opens what it names; a run of them the one page
        all its moments are shown on, when they share one.
        """

        if self.rest is not None:
            return self.rest.rest_url
        if self.burst:
            pages = {item.page for item in self.burst}
            return pages.pop() if len(pages) == 1 else ""
        if self.moment is not None:
            return self.moment.url
        if self.opens:
            return self.opens
        return reverse("core:audit_detail", args=[self.page.pk]) if self.events else ""

    @property
    def rest_url(self) -> str:
        """The list of everything this line's fold leaves out, or nothing when no page lists it."""

        if self.burst:
            return self.url
        return chain_url(self.page.trace)

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

        # A run whose moments share no page has no list of the rest to open,
        # so its title's count says how many it holds and no row counts them.
        rest = (HistoryLine(self.at, under=self.fold, rest=self),) if self.more and self.rest_url else ()
        return (self, *self.shown, *rest)

    @property
    def title(self) -> str:
        """What the event was: its own row's name, else its operation's words, else what it changed."""

        name, after = self.named
        return f"{name}{after}"

    @property
    def named(self) -> tuple[str, str]:
        """``title`` as what it names and the words after that name, so a page
        draws the name as one (a key cut at its cell's edge) and the words as text."""

        if self.burst:
            noun, happened = self.burst[0].like
            return f"{counted(self.total, noun)} {happened}", ""
        head = self.head
        if head is not None and head.object_repr:
            return head.object_repr, ""
        newest = head or self.events[0]
        named = words_of(newest).title
        if named:
            return named, ""
        if head is not None:
            return head.type_label, ""
        # Nothing declared its words, so the event is named by what it changed.
        return newest.object_repr or newest.type_label, f" and {counted(self.total - 1, 'more', 'more')}"

    @property
    def type_label(self) -> str:
        head = self.head
        return head.type_label if head is not None and head.object_repr else ""

    @property
    def who(self) -> Attribution:
        """Who did what its line says: the row its action comes from (``page``).
        Who started a chain and who approved it are said by its record's page."""

        return self.page.who

    @property
    def action(self) -> str:
        """How it came out: its own row's action, else its newest row's."""

        return self.page.action

    @property
    def state(self) -> AuditLog.Action | str:
        """Its action as the member its pill is drawn from; an action HQ no
        longer names stays its word, drawn in no tone."""

        return AuditLog.Action(self.action) if self.action in AuditLog.Action.values else self.action

    @property
    def action_label(self) -> str:
        state = self.state
        return state.label if isinstance(state, AuditLog.Action) else state

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


@dataclass(frozen=True, slots=True)
class HistoryGroup:
    """Lines of history under one heading of a table's rows; ``folds`` is the
    row they start folded away under. A group with no label is a table's only one."""

    lines: tuple[HistoryLine, ...]
    label: str = ""
    folds: str = ""

    @property
    def count(self) -> int:
        """How many rows of history its table draws: each line and those folded
        beneath it, never the row counting what a fold leaves out."""

        return sum(line.rest is None for event in self.lines for line in event.lines)


def columns(*, hide_who: bool = False) -> tuple[TableColumn, ...]:
    """The headings of a table of history, without who did each where ``hide_who``."""

    return tuple(TableColumn(heading, css=css) for heading, css in COLUMNS if not (hide_who and heading == WHO))


def table(
    label: str,
    groups: Iterable[HistoryGroup],
    headings: tuple[TableColumn, ...],
    *,
    selectable: bool = False,
    sorted_by: str | None = None,
    here: str = "",
) -> DataTable:
    """The table of history ``{% data_table %}`` draws: each event's line, then
    the lines folded beneath it, under ``headings`` (``COLUMNS``, the Who column
    left out where the reader may not read the audit log). A group that starts
    folded away folds each of its rows under its heading. A title that opens
    ``here``, the page the table is drawn on, is no link."""

    who = any(column.label == WHO for column in headings)
    return DataTable(
        label,
        headings,
        groups=tuple(
            Group(
                group.label,
                tuple(
                    _row(
                        line,
                        who=who,
                        folded=group.folds,
                        key=line.pk if selectable and not line.rest else None,
                        here=here,
                    )
                    for event in group.lines
                    for line in event.lines
                ),
                count=group.count or None,
                folds=group.folds,
            )
            for group in groups
        ),
        selectable=selectable,
        sorted_by=sorted_by,
    )


def _row(line: HistoryLine, *, who: bool, folded: str, key: Any, here: str) -> Row:
    """One line as its row: what it was, when, who, how it came out and what it
    said; on a phone only What and When stay, so What's second line says how it
    came out, in the action's own word, which the Action column says wider."""

    folding = {"folded": folded} if folded else {"folds": line.fold if line.grouped else "", "folded": line.under}
    if line.rest is not None:
        return Row((Cell(line.rest.more_words, line.url),), across=True, **folding)
    if line.burst or line.moment is not None:
        moment = line.burst[0] if line.burst else line.moment
        title = (
            Cell(line.title, line.url, role="data", fold=True)
            if line.burst
            else Cell(moment.title, line.url, external=moment.external, role="data")
        )
        person, outcome = Cell(moment.actor), Cell(moment.source, tone="neutral")
        said = Cell(moment.detail, muted=True, role="data")
    else:
        action = Cell(line.action_label, muted=True, dot=bool(line.type_label), shows="narrow")
        name, after = line.named
        title = Cell(
            name,
            "" if here and line.url.partition("#")[0] == here else line.url,
            readout=True,
            after=after,
            name=True,
            fold=line.grouped,
            count=line.total if line.grouped and line.head is not None else None,
            lines=(Cell(line.type_label, muted=True, parts=(action,)) if line.type_label else action,),
        )
        person = line.who.cell
        state = line.state
        outcome = Cell.state(state) if isinstance(state, AuditLog.Action) else Cell(state, tone="neutral")
        # The row's own text as stored, which an older release may have worded.
        said = Cell(Quoted(line.message), muted=True, role="data")
    when = (
        Cell(Figure(f"+{duration(line.at - line.began, short=True)}", epoch(line.at)), muted=True)
        if line.began is not None
        else Cell(moment=line.at, muted=True)
    )
    return Row((title, when, *((person,) if who else ()), outcome, said), key=key, **folding)


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
    """One row per event in ``rows``: the newest of each chain's, and every
    row that belongs to none.

    A row is a chain's newest unless one of its siblings in ``rows`` came
    later, so a page of these is a page of events, read from the indexes on
    ``created_at`` and ``trace`` with nothing counted.
    """

    later = rows.order_by().filter(
        Q(created_at__gt=OuterRef("created_at")) | Q(created_at=OuterRef("created_at"), pk__gt=OuterRef("pk")),
        trace=OuterRef("trace"),
    )
    return rows.filter(Q(trace="") | ~Exists(later))


def entries(
    events: Sequence[Any], found: Iterable[Moment] = (), *, within: QuerySet | None = None, steps: bool = False
) -> list[HistoryLine]:
    """Events and moments on one line, newest first.

    ``events`` is one row per event (``heads``), newest first; each chain's
    other rows are read from ``within``, in one query, so it lists what the
    event changed. Without ``within`` each row is a line of its own. A chain
    read as ``steps`` opens each step where a chain of several does, even
    a chain of one.
    """

    chains: dict[str, dict[str, list[Any]]] = {}
    kinds: dict[str, dict[str, int]] = {}
    traces = {event.trace for event in events if event.trace}
    if within is not None and traces:
        for row in _newest_of_each_kind(within, traces):
            chains.setdefault(row.trace, {}).setdefault(row.operation_id, []).append(row)
            if not row.own_row:
                kinds.setdefault(row.operation_id, {}).setdefault(row.object_type, row.of_kind)
    opens = _step_pages(chains.values(), fewest=1 if steps else 2)
    lines: list[HistoryLine] = [
        _chain(event, chains.get(event.trace) or {event.operation_id: [event]}, kinds, opens, steps_alone=steps)
        for event in events
    ]
    lines += runs(found)
    lines.sort(key=lambda line: line.at, reverse=True)
    return lines


def _chain(
    event: Any,
    steps: dict[str, list[Any]],
    kinds: dict[str, dict[str, int]],
    opens: dict[str, str],
    *,
    steps_alone: bool = False,
) -> HistoryLine:
    """The line of ``event``'s chain: its one operation's, else each
    operation a step beneath it, oldest first."""

    lines = [
        HistoryLine(rows[0].created_at, events=tuple(rows), kinds=tuple(kinds.get(operation, {}).items()))
        for operation, rows in steps.items()
    ]
    if len(lines) == 1 and not steps_alone:
        return lines[0]
    ordered = tuple(
        replace(line, opens=opens.get(line.page.operation_id) or _record_page(line))
        for line in sorted(lines, key=lambda line: (line.events[-1].created_at, line.events[-1].pk))
    )
    ordered = (ordered[0], *(replace(line, began=ordered[0].at) for line in ordered[1:]))
    if len(ordered) == 1:
        return ordered[0]
    every = tuple(sorted((row for line in lines for row in line.events), key=_newest))
    return HistoryLine(event.created_at, events=every, steps=ordered)


def chain(trace: str) -> tuple[HistoryLine, ...]:
    """The operations of the chain ``trace``, oldest first, each a line with
    what it changed beneath it."""

    return _chain_of(AuditLog.objects.filter(trace=trace))


def chain_made(model: type, record_id: object) -> tuple[HistoryLine, ...]:
    """``chain`` of the chain the record ``record_id`` of ``model`` was made
    in: a run's page draws the press, the approval and the merge that set it
    going, and what it set going itself. Empty for a record no row made."""

    made = AuditLog.objects.filter(
        object_type=audited_labels()[model], object_id=str(record_id), action=AuditLog.Action.CREATED
    ).order_by("created_at", "pk")
    return _chain_of(AuditLog.objects.filter(trace=Subquery(made.values("trace")[:1])))


def _chain_of(rows: QuerySet) -> tuple[HistoryLine, ...]:
    rows = rows.select_related("user")
    head = list(heads(rows)[:1])
    if not head:
        return ()
    (line,) = entries(head, within=rows, steps=True)
    return line.steps or (line,)


def chain_url(trace: str) -> str:
    """The audit log narrowed to every row of the chain ``trace``, or nothing for none."""

    return with_query(reverse("core:audit_list"), {CHAIN: trace}) if trace else ""


def _newest(row: Any) -> tuple[float, int]:
    return (-row.created_at.timestamp(), -row.pk)


def _step_pages(chains: Iterable[dict[str, list[Any]]], *, fewest: int = 2) -> dict[str, str]:
    """The one page that holds the whole of each step of a chain of ``fewest``
    steps or more, by its operation: a run's or a scan's page, read in one query, or the
    entry of the approval its rows ask for or decide. Any other step opens
    its record (``_record_page``)."""

    several = [steps for steps in chains if len(steps) >= fewest]
    approval = audited_labels()[ApprovalRequest]
    # A step that asked for or decided a held change: its rows about it name it.
    pages = {
        operation: reverse("core:approval_entry", args=[held])
        for steps in several
        for operation, rows in steps.items()
        if (held := next((row.object_id for row in rows if row.object_type == approval), ""))
    }
    steps = {operation for steps in several for operation in steps if operation and operation not in pages}
    listed = (OperationRequest.Action.RUN, OperationRequest.Action.SCAN)
    found = (
        OperationRequest.objects.filter(pk__in=_uuids(steps), action__in=listed).values_list("pk", flat=True)
        if steps
        else ()
    )
    return {**pages, **{str(pk): run_url(str(pk)) for pk in found}}


def _uuids(operations: Iterable[str]) -> list[UUID]:
    """The operations a queued request can be: those named by a uuid."""

    return [named for named in map(_uuid, operations) if named is not None]


def _uuid(operation: str) -> UUID | None:
    try:
        return UUID(operation)
    except ValueError:
        return None


def _record_page(line: HistoryLine) -> str:
    """The page of the record a step is about (its own rows' ``subject``, else
    the rest's: a merge's, never the run it queued), of the kind its model
    declares it (``Subject.kind``), else none."""

    return next(
        (
            entity_link(kind, row.subject).url
            for row in (*line.own, *line.events)
            if row.subject and (kind := subject_kind(row.object_type))
        ),
        "",
    )


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


def _newest_of_each_kind(within: QuerySet, traces: Iterable[str]) -> QuerySet:
    """The chains' rows a line can carry, newest first, each counted with its kind.

    The newest ``SHOWN`` of each operation's own rows and of each object type
    it changed, which hold the newest ``SHOWN`` items of all: the rows read
    grow with the page, the steps of a chain and the types of record, never
    with what one operation changed.
    """

    own = ExpressionWrapper(Q(object_id=F("operation_id")), output_field=BooleanField())
    kind = (F("trace"), F("operation_id"), own, F("object_type"))
    return (
        within.filter(trace__in=traces)
        .annotate(
            own_row=own,
            place=Window(RowNumber(), partition_by=kind, order_by=(F("created_at").desc(), F("pk").desc())),
            of_kind=Window(Count("pk"), partition_by=kind),
        )
        .filter(place__lte=SHOWN)
        .order_by("-created_at", "-pk")
    )


def read_requests() -> Q:
    """The rows that are an ask for a fresh reading, as a filter."""

    return Q(object_type=read_request_type())


def routine() -> Q:
    """HQ looking with nobody asking: the rows ``prune_routine`` removes in time."""

    kinds = Q(pk__in=[])
    for action, object_type in ROUTINE_EVENTS:
        kinds |= Q(action=action, object_type=object_type)
    return kinds & Q(user__isnull=True)


def recent(
    *, limit: int, reads: bool = False, quiet: bool = False, of: QuerySet[AuditLog] | None = None
) -> list[HistoryLine]:
    """The newest ``limit`` events of ``of`` (the whole log by default), read
    requests and routine work left out unless asked for."""

    rows = (AuditLog.objects.all() if of is None else of).select_related("user")
    if not reads:
        rows = rows.exclude(read_requests())
    if not quiet:
        rows = rows.exclude(routine())
    page = list(heads(rows).order_by("-created_at", "-pk")[:limit])
    return entries(page, within=rows)


def who_did(object_types: Iterable[str], ids: Iterable[object], *, action: str = "") -> dict[str, Attribution]:
    """Who did each act on the records ``ids`` name, of any of ``object_types``,
    by id: the attribution of each record's first audit row (of ``action``,
    given one), read in one query. A record no row is about is left out.

    The act is written once, as its audit row; every page and read that says
    who made a record says it from here rather than from a copy the record
    keeps of its own.
    """

    (found,) = who_did_each(Acts(tuple(object_types), ids, action))
    return found


@dataclass(frozen=True)
class Acts:
    """The records ``ids`` names, of any of ``object_types``, whose first audit
    row (of ``action``, and changing the field ``changed``, given either) says
    who did each (``who_did``)."""

    object_types: tuple[str, ...]
    ids: Iterable[object]
    action: str = ""
    changed: str = ""


def who_did_each(*asked: Acts) -> list[dict[str, Attribution]]:
    """``who_did`` for each of ``asked``, in its order, read in one query: a
    page naming records of several kinds asks once, and a record of one kind
    is never taken for another's of the same id. Two parts of one page asking
    the same are answered by that one query (``read_once``)."""

    wanted = [frozenset(str(one) for one in acts.ids) for acts in asked]
    key = repr(
        tuple(
            (acts.object_types, sorted(ids), acts.action, acts.changed) for acts, ids in zip(asked, wanted, strict=True)
        )
    )
    found = read_once(f"history.who_did_each:{key}", lambda: _who_did_each(asked, wanted))
    return [dict(each) for each in found]


def _who_did_each(asked: Sequence[Acts], ids: Sequence[frozenset[str]]) -> list[dict[str, Attribution]]:
    each = []
    for place, (acts, wanted) in enumerate(zip(asked, ids, strict=True)):
        # Asked about no record it still asks, so a page costs the same
        # whether it names none or many.
        condition = Q(object_type__in=acts.object_types) & (Q(object_id__in=wanted) if wanted else Q(pk__lt=0))
        if acts.action:
            condition &= Q(action=acts.action)
        if acts.changed:
            condition &= Q(metadata__changes__has_key=acts.changed)
        each.append((place, condition))
    found: list[dict[str, Attribution]] = [{} for _ in asked]
    rows = AuditLog.objects.filter(Q(*(condition for _place, condition in each), _connector=Q.OR))
    asks = Case(*(When(condition, then=Value(place)) for place, condition in each), default=Value(-1))
    first = rows.annotate(
        asked=asks,
        place=Window(RowNumber(), partition_by=(asks, F("object_id")), order_by=(F("created_at").asc(), F("pk").asc())),
    ).filter(place=1)
    for row in first.select_related("user"):
        found[row.asked][row.object_id] = row.who
    return found


def asking(model: type, records: Iterable[Any], *, changed: str = "") -> Acts:
    """The audit rows that created each of ``records`` (or first changed its
    field ``changed``: a decision's), of ``model``, under its audit label,
    read with others' by ``who_did_each``."""

    action = AuditLog.Action.UPDATED if changed else AuditLog.Action.CREATED
    return Acts((audited_labels()[model],), [record.pk for record in records], action, changed)


def asked_by(model: type, records: Iterable[Any]) -> dict[str, Attribution]:
    """Who asked for each of ``records`` (a held change, a queued operation),
    by id, from the audit row that created it; a record no row records is
    left out."""

    (found,) = who_did_each(asking(model, records))
    return found


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
            detail=event.who.words,
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
                    url=web_url(deployment.get("url")),
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
