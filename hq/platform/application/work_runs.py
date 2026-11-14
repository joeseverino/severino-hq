"""All the work HQ started, as one list: its own jobs and the automation work
it queued (each procedure's runs and the source's scans), each said in the
same words and drawn in the same row shape.

A job stays a ``jobs.Job`` and a run an ``OperationRequest``, since the
runner's lock and the worker's claim and lease need each record as it is. The
list is a database union of the two, filtered, ordered and paginated there,
whose page of ids is then read back with one query per source, so a page
costs the same however much work has run. A held run is an
``ApprovalRequest``, not an operation, and is not listed here.
"""

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from django.db.models import Case, CharField, Exists, F, IntegerField, OuterRef, Q, QuerySet, Value, When
from django.db.models.fields.json import KT
from django.db.models.functions import Coalesce, Collate, Concat, NullIf
from django.db.models.lookups import Exact
from django.utils import timezone

from hq.domains.control_plane.models import OperationHost, OperationRequest, OperationState
from hq.domains.jobs.models import HEARTBEAT_GRACE, Job, JobState
lazy from hq.domains.jobs.runner import AUDIT_TYPE as JOB_AUDIT_TYPE

from .asks import DONE, FAILED, QUEUED, RUNNING, Standing
from .automation_runs import capability_name, resource_key, run_title
from .doors import NOBODY, Attribution
from .resource_operations import active_conditions, asked_by, problem_condition
from .routes import reverse
lazy from .automation_adoption import playbook_titles
lazy from .automation_sources import REFRESH, SOURCE_KEY, capability_specs
lazy from .cadence import automation_standing
lazy from .history import who_did
lazy from .scheduled_work import job_names

type Source = Literal["job", "run"]
JOB: Source = "job"
RUN: Source = "run"

# Each state's tone, the same for a job and a run on every page that says
# one: work waiting its turn is paused, work under way active.
TONES = {
    JobState.QUEUED: "paused",
    JobState.RUNNING: "active",
    JobState.SUCCEEDED: "good",
    JobState.FAILED: "serious",
    # Stopped early: a process or a worker that vanished, a restart to look at
    # rather than a fault to read.
    JobState.LOST: "attention",
}
# Each task outcome a run reports (the bridge's ``RunEvent``), in the same vocabulary.
OUTCOME_TONES = {
    "ok": "good",
    "changed": "active",
    "skipped": "paused",
    "failed": "serious",
    "unreachable": "serious",
}
# How work in each state stands for a control that follows it (``asks.Standing``).
STANDS = {
    JobState.QUEUED: QUEUED,
    JobState.RUNNING: RUNNING,
    JobState.SUCCEEDED: DONE,
    JobState.FAILED: FAILED,
    JobState.LOST: FAILED,
}
# What an operation that ended and found something wrong (a ``Degraded`` or
# ``Drifted`` condition) says, so it never reads as done.
PROBLEM_FOUND = "Problem found"
PROBLEM_TONE = "attention"
# What a claimed run whose lease ran out says until the next claim ends it.
LAPSED = "The run stopped reporting, so HQ can't tell whether it finished."
# What a run or scan that waits says while no worker will take it, in the
# tone of something to look at: it is neither under way nor waiting its turn.
NOT_STARTED = "Not started"
NOT_STARTED_TONE = "attention"
# The states of a run that has no outcome to say yet.
UNENDED = frozenset({OperationState.QUEUED, OperationState.CLAIMED})
# A kind of work whose latest run ended so is one to look at.
ENDED_BADLY = (JobState.FAILED, JobState.LOST)

# The orders the list can be read in, each with a stable tie-break.
NEWEST, OLDEST, BY_STATE, BY_TITLE, FAILED_FIRST = "-created_at", "created_at", "state", "title", "failed"
ORDERS = {
    NEWEST: ("-created_at", "-id"),
    OLDEST: ("created_at", "id"),
    BY_STATE: ("seen", "-created_at", "-id"),
    BY_TITLE: ("title", "-created_at", "-id"),
    # The latest run of each kind that ended badly leads, then the newest.
    FAILED_FIRST: ("lead", "-created_at", "-id"),
}
# How many runs a machine's or an operation's page shows.
RECENT = 10

# A run's procedure is named on Jobs by the command it is run through: what
# ``capability_name`` puts before and after the procedure's id. A scan is
# named by the command that asks for one, Refresh.
_RUN_KIND = tuple(capability_name("\0").split("\0"))
# The automation work Jobs lists: a procedure's runs and the source's scans.
_LISTED = (OperationRequest.Action.RUN, OperationRequest.Action.SCAN)


@dataclass(frozen=True, slots=True)
class RunEvidence:
    """What a piece of work shows for itself (``partials/_run_evidence.html``):
    its machines, its asker's correlation id, the worker holding it and until
    when while it is claimed, how many tries it took, its tasks' outcomes
    (each a ``RunEvent`` with its ``tone``), the kinds of reading its playbook
    stated, what each name served of the certificate it checked, and the
    result of an operation with no page of its own."""

    hosts: tuple[str, ...] = ()
    correlation: str = ""
    worker: str = ""
    lease_until: datetime | None = None
    attempts: int = 0
    events: tuple[dict[str, Any], ...] = ()
    stated: tuple[str, ...] = ()
    # The certificate's fingerprint, and each name's answer (``matches_expected``).
    expected: str = ""
    consumers: tuple[dict[str, Any], ...] = ()
    raw: str = ""

    @property
    def affected(self) -> tuple[dict[str, Any], ...]:
        return tuple(item for item in self.consumers if item.get("matches_expected") is False)

    @property
    def matched(self) -> int:
        return sum(1 for item in self.consumers if item.get("matches_expected") is True)

    def __bool__(self) -> bool:
        return bool(
            self.hosts
            or self.correlation
            or self.worker
            or self.attempts > 1
            or self.events
            or self.stated
            or self.affected
            or self.matched
            or self.raw
        )


@dataclass(frozen=True, slots=True)
class WorkRun:
    """One piece of work in the row every list of work draws."""

    source: Source
    id: str
    # What it is in words, and the name the list filters it by: a job's kind,
    # or the command a run's procedure is run through.
    title: str
    kind: str
    # Its state as seen (``JobState``'s value), in words and as a tone.
    state: str
    word: str
    tone: str
    # What it is doing or how it ended, in a sentence.
    note: str
    # Who asked for it, through which door, and who approved it, from its audit row.
    by: Attribution
    created_at: datetime
    started: datetime | None
    finished: datetime | None
    duration: float | None
    # What a run shows for itself; empty for a job.
    evidence: RunEvidence = RunEvidence()
    # How far along a job is, where it knows.
    percent: int | None = None
    # A run's effect as its playbook's record pins it, which asking for one,
    # and cancelling one, takes the authority of; empty for a job.
    effect: str = ""
    # What its reader may do to it where it is listed (``automation_runs.listed_with_actions``).
    actions: tuple[Any, ...] = ()

    @property
    def url(self) -> str:
        """Its own page, for a run or a scan; "" for a job, which has none."""

        return run_url(self.id) if self.source == RUN else ""


@dataclass(frozen=True, slots=True)
class WorkKind:
    """A kind of work by its latest run: the name the list filters it by, what
    it is called, and how that run is seen (``JobState``'s value)."""

    kind: str
    title: str
    state: str


def job_seen_state(now: datetime) -> Case:
    """``Job.seen_state`` as SQL, so a list filters and orders by what it shows."""

    stale = now - HEARTBEAT_GRACE
    return Case(
        When(Q(state=JobState.RUNNING, heartbeat_at__isnull=False, heartbeat_at__lt=stale), then=Value(JobState.LOST)),
        When(Q(state=JobState.QUEUED, created_at__lt=stale), then=Value(JobState.LOST)),
        default=F("state"),
        output_field=CharField(),
    )


def run_seen_state(now: datetime) -> Case:
    """A run's state as a job's is said (``OperationState.as_job``), where a
    claimed run whose lease has run out has stopped early whether or not the
    next claim has ended it yet, as a job nothing has heard from has."""

    lapsed = Q(state=OperationState.CLAIMED) & (Q(lease_expires_at__isnull=True) | Q(lease_expires_at__lte=now))
    return Case(
        When(lapsed, then=Value(JobState.LOST)),
        *(When(state=state, then=Value(state.as_job.value)) for state in OperationState),
        output_field=CharField(),
    )


def operation_of(kind: str) -> str:
    """The procedure a run kind names, or "" when it names none."""

    start, end = _RUN_KIND
    return kind[len(start) : -len(end)] if kind.startswith(start) and kind.endswith(end) else ""


@dataclass(frozen=True, slots=True)
class _Asked:
    state: tuple[str, ...]
    kind: tuple[str, ...]
    host: tuple[str, ...]
    search: str
    leads: bool


# What each side of the union selects, in its order.
_COLUMNS = ("source", "id", "seen", "created_at", "title", "lead")


def _job_title() -> Case:
    """What a job is called: its declaration's name, else what its run stored,
    else its kind. Case-blind, so the list sorts by it as it reads."""

    declared = (When(kind=kind, then=Value(name)) for kind, name in job_names().items())
    return Collate(
        Case(*declared, default=Coalesce(NullIf(F("label"), Value("")), F("kind")), output_field=CharField()),
        "NOCASE",
    )


def _scanned(then: str, default: Any) -> Case:
    return Case(When(action=OperationRequest.Action.SCAN, then=Value(then)), default=default, output_field=CharField())


def _run_title() -> Collate:
    """The run's playbook by its title, ``run_title`` of a procedure no longer
    adopted, Refresh's title for a scan, or any other operation's action in
    words, as SQL. Case-blind, so the list sorts by it as it reads."""

    operation = KT("input__operation")
    titled = (
        When(Exact(operation, Value(playbook)), then=Value(title))
        for playbook, title in playbook_titles().items()
        if title
    )
    unadopted = Concat(Value(run_title("", {})), operation, output_field=CharField())
    run = Case(*titled, default=unadopted, output_field=CharField())
    acted = (
        When(action=action, then=Value(action.label)) for action in OperationRequest.Action if action not in _LISTED
    )
    return Collate(_scanned(scan_title(), Case(*acted, default=run, output_field=CharField())), "NOCASE")


def scan_title() -> str:
    """A scan in words: the title of Refresh, the command that asks for one.

    Read from its declaration rather than the registry, whose commands are
    built from the adopted catalog, so listing work never builds them.
    """

    return next(spec.title for spec in capability_specs() if spec.name == REFRESH)


def _run_kind() -> Case:
    """``capability_name`` of the run's procedure, or Refresh for a scan, as SQL."""

    start, end = _RUN_KIND
    return _scanned(REFRESH, Concat(Value(start), KT("input__operation"), Value(end), output_field=CharField()))


def _kind_key(kind: str) -> str:
    """The key of the record the work of ``kind`` is queued on, or "" for a kind no run is."""

    if kind == REFRESH:
        return SOURCE_KEY
    procedure = operation_of(kind)
    return resource_key(procedure) if procedure else ""


def _jobs_latest() -> Exists:
    """Where a job is its kind's latest run."""

    return ~Exists(Job.objects.filter(kind=OuterRef("kind"), created_at__gt=OuterRef("created_at")))


def _runs_latest() -> Exists:
    """Where a run is its procedure's latest, or a scan its source's."""

    later = OperationRequest.objects.filter(
        resource=OuterRef("resource"), action=OuterRef("action"), created_at__gt=OuterRef("created_at")
    )
    return ~Exists(later)


def _lead(latest: Exists, *, leads: bool) -> Case | Value:
    """0 where the row is its kind's latest run and it ended badly, else 1;
    1 for every row of a list in another order."""

    if not leads:
        return Value(1, output_field=IntegerField())
    return Case(When(latest & Q(seen__in=ENDED_BADLY), then=Value(0)), default=Value(1), output_field=IntegerField())


def _rows(rows: QuerySet, source: Source) -> QuerySet:
    """One source's side of the union, unordered as a compound statement's parts must be."""

    return rows.annotate(source=Value(source, output_field=CharField())).values_list(*_COLUMNS).order_by()


def _jobs(now: datetime, asked: _Asked) -> QuerySet:
    jobs = Job.objects.annotate(seen=job_seen_state(now), title=_job_title())
    if asked.host:
        # A job acts on no machine.
        return _rows(jobs.annotate(lead=Value(1)).none(), JOB)
    if asked.state:
        jobs = jobs.filter(seen__in=asked.state)
    if asked.kind:
        jobs = jobs.filter(kind__in=asked.kind)
    if asked.search:
        jobs = jobs.filter(Q(label__icontains=asked.search) | Q(note__icontains=asked.search))
    return _rows(jobs.annotate(lead=_lead(_jobs_latest(), leads=asked.leads)), JOB)


def _runs(now: datetime, asked: _Asked) -> QuerySet:
    runs = seen_as_runs(OperationRequest.objects.filter(action__in=_LISTED), now)
    if asked.state:
        runs = runs.filter(seen__in=asked.state)
    if asked.kind:
        runs = runs.filter(resource__key__in=[key for kind in asked.kind if (key := _kind_key(kind))])
    if asked.host:
        # By subquery, so a run on two of the machines asked for is one row,
        # and a scan, which acts on no machine, is none.
        runs = runs.filter(pk__in=OperationHost.objects.filter(host__in=asked.host).values("operation"))
    if asked.search:
        runs = runs.annotate(operation=KT("input__operation"), said=KT("result__message")).filter(
            Q(operation__icontains=asked.search) | Q(title__icontains=asked.search) | Q(said__icontains=asked.search)
        )
    return _rows(runs.annotate(lead=_lead(_runs_latest(), leads=asked.leads)), RUN)


def work_rows(
    *,
    state: tuple[str, ...] = (),
    kind: tuple[str, ...] = (),
    host: tuple[str, ...] = (),
    search: str = "",
    order: str = NEWEST,
    now: datetime | None = None,
) -> QuerySet:
    """Every job and run that matches, as ``_COLUMNS`` rows in ``order``: a
    queryset a paginator counts and slices in the database.

    Each filter keeps what matches any of its values: ``state`` a ``JobState``
    value as seen, ``kind`` a job's kind or a run's command name, ``host`` a
    machine's canonical name (a job acts on none). ``hydrated`` turns a page
    of it into ``WorkRun`` rows.
    """

    now = now or timezone.now()
    asked = _Asked(state, kind, host, search, leads=order == FAILED_FIRST)
    return _jobs(now, asked).union(_runs(now, asked), all=True).order_by(*ORDERS[order])


def work_kinds(*, now: datetime | None = None) -> list[WorkKind]:
    """Each kind of work that has run, by its latest run, A to Z by what it is
    called: what the list is filtered by, and what failed on its last run."""

    now = now or timezone.now()
    jobs = (
        Job.objects.annotate(seen=job_seen_state(now), title=_job_title(), named=F("kind"))
        .filter(_jobs_latest())
        .values_list("named", "title", "seen")
        .order_by()
    )
    runs = (
        OperationRequest.objects.filter(action__in=_LISTED)
        .annotate(seen=run_seen_state(now), title=_run_title(), named=_run_kind())
        .filter(_runs_latest())
        .values_list("named", "title", "seen")
        .order_by()
    )
    # Two runs of a kind made in the same instant are both its latest; one names it.
    latest = {named: WorkKind(named, title, seen) for named, title, seen in jobs.union(runs, all=True).order_by()}
    return sorted(latest.values(), key=lambda found: found.title.casefold())


def playbook_runs(
    *,
    playbook: str = "",
    host: str = "",
    correlation: str = "",
    state: tuple[str, ...] = (),
    now: datetime | None = None,
) -> QuerySet:
    """The runs of a playbook that match, never a scan, each annotated with
    how it is seen and what it is called, for ``run_rows``.

    ``playbook`` is a procedure's id, ``host`` a machine's canonical name,
    ``correlation`` the id its asker sent, and ``state`` a ``JobState`` value
    as seen; each left empty keeps every run.
    """

    found = seen_as_runs(
        OperationRequest.objects.filter(action=OperationRequest.Action.RUN).select_related("resource"), now
    )
    if playbook:
        found = found.filter(resource__key=resource_key(playbook))
    if host:
        found = found.filter(pk__in=OperationHost.objects.filter(host=host).values("operation"))
    if correlation:
        found = found.filter(input__correlation=correlation)
    if state:
        found = found.filter(seen__in=state)
    return found


def seen_as_runs(operations: QuerySet, now: datetime | None = None) -> QuerySet:
    """``operations`` with the state each is seen in (``run_seen_state``) and
    what each is called, as ``run_rows`` reads them."""

    return operations.annotate(seen=run_seen_state(now or timezone.now()), title=_run_title())


def untaken() -> str:
    """What a run or scan that waits is waiting for, in a sentence, while no
    playbook worker reports to take it; "" while one does."""

    return automation_standing().cannot("", now=timezone.now())


def _untaken_of(seen: Iterable[JobState]) -> str:
    """``untaken``, asked only where a run waits."""

    return untaken() if JobState.QUEUED in seen else ""


def run_rows(operations: Iterable[OperationRequest], now: datetime | None = None) -> list[WorkRun]:
    """Operations read through ``seen_as_runs`` as the row every list of work
    draws, who asked for each read in one query."""

    found = list(operations)
    who = asked_by(found)
    now = now or timezone.now()
    said = _untaken_of(JobState(operation.seen) for operation in found)
    return [
        _run_row(
            operation, JobState(operation.seen), operation.title, now, who.get(str(operation.pk), NOBODY), untaken=said
        )
        for operation in found
    ]


def run_of(operation_id: str, now: datetime | None = None) -> tuple[WorkRun, OperationRequest] | None:
    """The run or scan ``operation_id`` names as its page shows it, with its
    record; None for an id no listed run has."""

    now = now or timezone.now()
    listed = OperationRequest.objects.filter(pk=operation_id, action__in=_LISTED).select_related("resource")
    found = seen_as_runs(listed, now).first()
    if found is None:
        return None
    who = asked_by((found,)).get(str(found.pk), NOBODY)
    seen = JobState(found.seen)
    return _run_row(found, seen, found.title, now, who, untaken=_untaken_of((seen,))), found


def run_url(operation_id: str) -> str:
    """The page of the run or scan ``operation_id``."""

    return reverse("jobs:run", kwargs={"pk": operation_id})


def run_hosts() -> list[str]:
    """Each machine a run has acted on, by its canonical name, A to Z: a scan acts on none."""

    return list(OperationHost.objects.order_by("host").values_list("host", flat=True).distinct())


def hydrated(rows: Iterable[Sequence[Any]]) -> list[WorkRun]:
    """A page of ``work_rows`` as ``WorkRun`` rows, in its order, read with one query per source."""

    page = list(rows)
    ids: dict[str, list[Any]] = {JOB: [], RUN: []}
    for source, pk, *_rest in page:
        ids[source].append(pk)
    jobs = Job.objects.in_bulk(ids[JOB]) if ids[JOB] else {}
    runs = OperationRequest.objects.select_related("resource").in_bulk(ids[RUN]) if ids[RUN] else {}
    # Who started each, from the audit row each wrote first.
    who = {**who_did((JOB_AUDIT_TYPE,), ids[JOB]), **asked_by(runs.values())}
    now = timezone.now()
    said = _untaken_of(JobState(seen) for source, _pk, seen, *_rest in page if source == RUN)
    built: list[WorkRun] = []
    for source, pk, seen, _created, title, _lead in page:
        by = who.get(str(pk), NOBODY)
        if source == JOB:
            built.append(job_run(jobs[pk], title, by))
        else:
            built.append(_run_row(runs[pk], JobState(seen), title, now, by, untaken=said))
    return built


def job_run(job: Job, title: str = "", by: Attribution = NOBODY) -> WorkRun:
    """A job as the row every list of work draws, called ``title`` (its kind
    when none is given) and started by ``by``."""

    seen = JobState(job.seen_state)
    return WorkRun(
        source=JOB,
        id=str(job.pk),
        title=title or job.kind,
        kind=job.kind,
        state=seen.value,
        word=seen.label,
        tone=TONES[seen],
        note=job.seen_error if seen in ENDED_BADLY else job.note,
        by=by,
        created_at=job.created_at,
        started=job.started_at,
        finished=job.finished_at,
        duration=job.duration_seconds,
        percent=100 if seen == JobState.SUCCEEDED else job.percent,
    )


def standing_of(run: WorkRun) -> Standing:
    """How a job or a run stands, for a control that follows it. A run that
    will not start while no worker takes it has ended, so no page follows it."""

    state = FAILED if run.word == NOT_STARTED else STANDS[JobState(run.state)]
    return Standing(state, run.note, since=run.created_at, percent=run.percent, word=run.word)


def standing_tone(state: str, word: str) -> str:
    """The tone of the job state ``word`` names, else of the one ``state`` stands for; "" for idle."""

    seen = next((seen for seen in JobState if seen.label == word), None)
    seen = seen or next((seen for seen, stands in STANDS.items() if stands == state), None)
    return TONES[seen] if seen else ""


def standing_word(state: str) -> str:
    """A control's state (``asks.Standing``) in the word its job state is said in; "" for idle."""

    return next((seen.label for seen, stands in STANDS.items() if stands == state), "")


def _run_row(
    operation: OperationRequest, seen: JobState, title: str, now: datetime, by: Attribution, *, untaken: str = ""
) -> WorkRun:
    """One operation as the row every list of work draws; one that waits
    while no worker will take it (``untaken``) says it has not started, and why."""

    asked = operation.input or {}
    result = operation.result or {}
    status = result.get("status") or {}
    claimed = operation.state == OperationState.CLAIMED
    lapsed = seen == JobState.LOST and claimed
    stuck = seen == JobState.QUEUED and bool(untaken)
    problem = problem_condition(result) if seen == JobState.SUCCEEDED else None
    word, tone = (
        (NOT_STARTED, NOT_STARTED_TONE)
        if stuck
        else (PROBLEM_FOUND, PROBLEM_TONE)
        if problem
        else (seen.label, TONES[seen])
    )
    started, finished = operation.claimed_at, operation.completed_at
    return WorkRun(
        source=RUN,
        id=str(operation.pk),
        title=title,
        kind=_kind_of(operation.action, str(asked.get("operation") or "")),
        state=seen.value,
        word=word,
        tone=tone,
        # A run not yet ended says only its state, in the State column, unless
        # it will not start.
        note=untaken
        if stuck
        else LAPSED
        if lapsed
        else ""
        if operation.state in UNENDED
        else _said(operation, problem),
        by=by,
        created_at=operation.created_at,
        started=started,
        finished=finished,
        # A lapsed run was last heard from when its lease ran out.
        duration=_seconds(started, finished or (operation.lease_expires_at if lapsed else now)),
        evidence=RunEvidence(
            hosts=tuple(asked.get("hosts") or ()),
            correlation=str(asked.get("correlation") or ""),
            worker=operation.claimed_by if claimed else "",
            lease_until=operation.lease_expires_at if claimed else None,
            attempts=operation.attempt_count,
            events=tuple(
                {**event, "tone": OUTCOME_TONES.get(event.get("outcome", ""), "")}
                for event in result.get("events") or ()
            ),
            stated=stated_kinds(result),
            expected=str(status.get("expected_fingerprint_sha256") or ""),
            consumers=tuple(status.get("consumers") or ()),
            # A run or a scan has a page of its own, where its record is.
            raw=""
            if operation.action in _LISTED or not result
            else json.dumps(shown_result(result), indent=2, sort_keys=True, default=str),
        ),
        effect=str((operation.resource.spec or {}).get("effect", "")),
    )


def _kind_of(action: str, procedure: str) -> str:
    """What the list filters an operation by: the command a run's procedure
    is run through, Refresh for a scan, else its action."""

    if action == OperationRequest.Action.RUN:
        return capability_name(procedure)
    return REFRESH if action == OperationRequest.Action.SCAN else action


def _said(operation: OperationRequest, problem: dict[str, Any] | None) -> str:
    """How an ended operation went, in a sentence: the condition that says
    what went wrong before its own message."""

    result = operation.result or {}
    said = result.get("message") or operation.reason
    if operation.state == OperationState.FAILED:
        active = active_conditions(result)
        return (active[0].get("message") if active else "") or said
    return (problem.get("message") if problem else "") or said


def shown_result(result: dict[str, Any]) -> dict[str, Any]:
    """An operation's result less the certificate, which is on its record's
    page: a row of history is not a second copy."""

    status = result.get("status") or {}
    return {**result, "status": {k: v for k, v in status.items() if k != "certificate_pem"}} if status else result


def stated_kinds(result: dict[str, Any]) -> tuple[str, ...]:
    """The kinds of reading a run's playbook stated on any of its machines, each once."""

    readings = (result.get("stated") or {}).get("readings") or {}
    return tuple(sorted({kind for kinds in readings.values() for kind in kinds}))


def _seconds(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    return max(timedelta(0), end - start).total_seconds()


def runs_on(host: str, limit: int = RECENT) -> list[WorkRun]:
    """The latest runs that acted on the machine ``host``, newest first."""

    return hydrated(work_rows(host=(host,))[:limit])


def recent_runs(operation_id: str, limit: int = RECENT) -> list[WorkRun]:
    """The latest runs of the procedure ``operation_id``, newest first."""

    return hydrated(work_rows(kind=(capability_name(operation_id),))[:limit])
