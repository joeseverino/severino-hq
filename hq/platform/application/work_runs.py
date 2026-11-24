"""All the work HQ started, as one list: its jobs and its queued automation work, in the same words and row shape.

Each stays its own record, since the runner's lock and the worker's lease need
it as it is. The list is a database union, so a page costs the same however
much work has run.
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

from hq.domains.control_plane.models import OPERATION_LAPSE, OperationHost, OperationRequest, OperationState
from hq.domains.jobs.models import LAPSE as JOB_LAPSE, Job, JobState
lazy from hq.domains.jobs.runner import AUDIT_TYPE as JOB_AUDIT_TYPE

from .asks import DONE, FAILED, QUEUED, RUNNING, Standing
from .automation_runs import capability_name, run_title
from .conditions import PROBLEMS, holding
from .doors import NOBODY, Attribution
from .projection import Combined, read_once
from .tone import Tone, Toned
lazy from .automation_adoption import playbook_titles
lazy from .automation_sources import REFRESH, capability_specs
lazy from .cadence import automation_standing
lazy from .entity_links import entity_link
lazy from .history import Acts, asked_by, asking, who_did_each
lazy from .outbound_work import job_names
lazy from .references import Kind

type Source = Literal["job", "run"]
JOB: Source = "job"
RUN: Source = "run"


class TaskOutcome(Toned):
    """What a run's task did on one host (the bridge's ``RunEvent``), in the
    tone a run's state takes for the same news."""

    OK = "ok", Tone.GOOD
    CHANGED = "changed", Tone.GOOD
    SKIPPED = "skipped", Tone.NEUTRAL
    FAILED = "failed", Tone.SERIOUS
    UNREACHABLE = "unreachable", Tone.SERIOUS


# The outcomes a run's audit trail records one row each: what changed a
# machine or went wrong. Every outcome stays in the run's result.
AUDITED_OUTCOMES = frozenset({TaskOutcome.CHANGED, TaskOutcome.FAILED, TaskOutcome.UNREACHABLE})
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
PROBLEM_TONE = Tone.ATTENTION
# What a run says whose worker stopped reporting before it ended, so what it
# changed is unknown: while its lease has run out, and once HQ ends it
# (``controller.OUTCOME_UNKNOWN``).
STOPPED_REPORTING = "Stopped reporting"
# What a run or scan that waits says while no worker will take it, in the
# tone of something to look at: it is neither under way nor waiting its turn.
NOT_STARTED = "Not started"
NOT_STARTED_TONE = Tone.ATTENTION
# The states of a run that has no outcome to say yet.
UNENDED = frozenset({OperationState.QUEUED, OperationState.CLAIMED})
# A kind of work whose latest run ended so is one to look at.
ENDED_BADLY = (JobState.FAILED, JobState.LOST)

# Newest first, with a stable tie-break.
NEWEST = ("-created_at", "-id")
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
    tone: Tone
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

    return Case(When(JOB_LAPSE.q(now), then=Value(JobState.LOST)), default=F("state"), output_field=CharField())


def run_seen_state(now: datetime) -> Case:
    """A run's state as a job's is said (``OperationState.as_job``), where a
    claimed run whose lease has run out has stopped early whether or not the
    next claim has ended it yet, as a job nothing has heard from has."""

    return Case(
        When(OPERATION_LAPSE.q(now), then=Value(JobState.LOST)),
        *(When(state=state, then=Value(state.as_job.value)) for state in OperationState),
        output_field=CharField(),
    )


def operation_of(kind: str) -> str:
    """The procedure a run kind names, or "" when it names none."""

    start, end = _RUN_KIND
    return kind[len(start) : -len(end)] if kind.startswith(start) and kind.endswith(end) else ""


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
    run = Case(
        *titled,
        default=unadopted,
        output_field=CharField(),
    )
    acted = (
        When(action=action, then=Value(action.label)) for action in OperationRequest.Action if action not in _LISTED
    )
    return Collate(_scanned(scan_title(), Case(*acted, default=run, output_field=CharField())), "NOCASE")


def _shown_title(kind: str, title: str) -> str:
    """The work ``kind`` names as a page draws it: its playbook's title as the
    author wrote it (``quoted.Quoted``), which SQL's copy of it
    (``_run_title``) is not; else ``title``."""

    return playbook_titles().get(operation_of(kind)) or title


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


def _jobs_latest() -> Exists:
    """Where a job is its kind's latest run."""

    return ~Exists(Job.objects.filter(kind=OuterRef("kind"), created_at__gt=OuterRef("created_at")))


def _runs_latest() -> Exists:
    """Where a run is its procedure's latest, or a scan its source's."""

    later = OperationRequest.objects.filter(
        resource=OuterRef("resource"), action=OuterRef("action"), created_at__gt=OuterRef("created_at")
    )
    return ~Exists(later)


def _lead(latest: Exists) -> Case:
    """0 where the row is its kind's latest run and it ended badly, else 1."""

    return Case(When(latest & Q(seen__in=ENDED_BADLY), then=Value(0)), default=Value(1), output_field=IntegerField())


def _rows(rows: QuerySet, source: Source) -> QuerySet:
    """One source's side of the union, unordered as a compound statement's parts must be."""

    return rows.annotate(source=Value(source, output_field=CharField())).values_list(*_COLUMNS).order_by()


def work(search: str = "") -> Combined:
    """Every job and run as ``_COLUMNS`` rows, those whose words hold
    ``search``, each part narrowed by ``seen`` (a ``JobState`` value as seen)
    and ``kind`` (a job's kind or a run's command name) and cut in the
    database; ``hydrated`` turns a page of it into ``WorkRun`` rows."""

    now = timezone.now()
    jobs = Job.objects.annotate(seen=job_seen_state(now), title=_job_title())
    runs = seen_as_runs(OperationRequest.objects.filter(action__in=_LISTED), now).annotate(kind=_run_kind())
    if search:
        jobs = jobs.filter(Q(label__icontains=search) | Q(note__icontains=search))
        runs = runs.annotate(procedure=KT("input__operation"), said=KT("result__message")).filter(
            Q(procedure__icontains=search) | Q(title__icontains=search) | Q(said__icontains=search)
        )
    return Combined(
        (_rows(jobs.annotate(lead=_lead(_jobs_latest())), JOB), _rows(runs.annotate(lead=_lead(_runs_latest())), RUN))
    )


def on_hosts(hosts: Iterable[str]) -> Q:
    """The work that acted on any of the machines ``hosts`` names, by their
    canonical names: by subquery, so a run on two of them is one row. A job
    acts on none, and its id is never a run's."""

    return Q(pk__in=OperationHost.objects.filter(host__in=hosts).values("operation"))


def work_kinds() -> list[WorkKind]:
    """Each kind of work that has run, by its latest run, A to Z by what it is
    called: what the list is filtered by, and what failed on its last run,
    read once for a page that asks both."""

    return read_once("work_runs.kinds", _kinds)


def _kinds() -> list[WorkKind]:
    now = timezone.now()
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
    latest = {
        named: WorkKind(named, _shown_title(named, title), seen)
        for named, title, seen in jobs.union(runs, all=True).order_by()
    }
    return sorted(latest.values(), key=lambda found: found.title.casefold())


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
    who = asked_by(OperationRequest, found)
    now = now or timezone.now()
    # Only a run or a scan waits on the playbook worker; the controller takes the rest.
    said = _untaken_of(JobState(operation.seen) for operation in found if operation.action in _LISTED)
    return [
        _run_row(
            operation,
            JobState(operation.seen),
            operation.title,
            now,
            who.get(str(operation.pk), NOBODY),
            untaken=said if operation.action in _LISTED else "",
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
    who = asked_by(OperationRequest, (found,)).get(str(found.pk), NOBODY)
    seen = JobState(found.seen)
    return _run_row(found, seen, found.title, now, who, untaken=_untaken_of((seen,))), found


def run_url(operation_id: str) -> str:
    """The page of the run or scan ``operation_id``."""

    return entity_link(Kind.RUN, operation_id).url


def run_hosts() -> list[str]:
    """Each machine a run has acted on, by its canonical name, A to Z: a scan acts on none."""

    return list(OperationHost.objects.order_by("host").values_list("host", flat=True).distinct())


def asked_with(correlations: Iterable[str]) -> Q:
    """The runs asked for with any of ``correlations``, by subquery as
    ``on_hosts`` reads: a job carries none."""

    return Q(pk__in=OperationRequest.objects.filter(input__correlation__in=list(correlations)).values("pk"))


def run_correlations() -> list[str]:
    """Each correlation id a run was asked with, A to Z."""

    said = OperationRequest.objects.filter(action__in=_LISTED).values_list("input__correlation", flat=True)
    return sorted({str(each) for each in said.distinct() if each})


def hydrated(rows: Iterable[Sequence[Any]]) -> list[WorkRun]:
    """A page of ``work_rows`` as ``WorkRun`` rows, in its order, read with one query per source."""

    page = list(rows)
    ids: dict[str, list[Any]] = {JOB: [], RUN: []}
    for source, pk, *_rest in page:
        ids[source].append(pk)
    jobs = Job.objects.in_bulk(ids[JOB]) if ids[JOB] else {}
    runs = OperationRequest.objects.select_related("resource").in_bulk(ids[RUN]) if ids[RUN] else {}
    # Who started each, from the audit row each wrote first, both kinds in one read.
    who = dict(
        zip(
            (JOB, RUN),
            who_did_each(Acts((JOB_AUDIT_TYPE,), ids[JOB]), asking(OperationRequest, runs.values())),
            strict=True,
        )
    )
    now = timezone.now()
    said = _untaken_of(JobState(seen) for source, _pk, seen, *_rest in page if source == RUN)
    built: list[WorkRun] = []
    for source, pk, seen, _created, title, _lead in page:
        by = who[source].get(str(pk), NOBODY)
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
        tone=seen.tone,
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
    return seen.tone if seen else ""


def outcome_tone(outcome: str) -> str:
    """A task outcome's tone; "" for one the bridge names that HQ does not."""

    return next((each.tone for each in TaskOutcome if each == outcome), "")


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
    problem = next(iter(holding(result.get("conditions"), *PROBLEMS)), None) if seen == JobState.SUCCEEDED else None
    word, tone = (
        (NOT_STARTED, NOT_STARTED_TONE)
        if stuck
        else (PROBLEM_FOUND, PROBLEM_TONE)
        if problem
        else (seen.label, seen.tone)
    )
    started, finished = operation.claimed_at, operation.completed_at
    return WorkRun(
        source=RUN,
        id=str(operation.pk),
        title=_shown_title(kind := _kind_of(operation.action, str(asked.get("operation") or "")), title),
        kind=kind,
        state=seen.value,
        word=word,
        tone=tone,
        # A run not yet ended says only its state, in the State column, unless
        # it will not start.
        note=untaken
        if stuck
        else STOPPED_REPORTING
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
                {**event, "tone": outcome_tone(event.get("outcome", ""))} for event in result.get("events") or ()
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
        active = holding(result.get("conditions"))
        return (active[0].get("message") if active else "") or said
    return (problem.get("message") if problem else "") or said


def stopped_at(operation: OperationRequest) -> tuple[str, str]:
    """The task a run that ended badly stopped on and what it said, the task
    "" where it stopped before any ran."""

    events = (operation.result or {}).get("events") or ()
    failed = next(
        (event for event in events if event.get("outcome") in (TaskOutcome.FAILED, TaskOutcome.UNREACHABLE)), None
    )
    if failed is not None:
        return str(failed.get("task") or ""), str(failed.get("message") or "")
    return "", _said(operation, None)


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

    return hydrated(work().kept(on_hosts((host,))).order_by(*NEWEST)[:limit])


def recent_runs(operation_id: str, limit: int = RECENT) -> list[WorkRun]:
    """The latest runs of the procedure ``operation_id``, newest first."""

    return hydrated(work().kept(Q(kind=capability_name(operation_id))).order_by(*NEWEST)[:limit])
