"""All the work HQ started, as one list: its jobs and its queued automation work, in the same words and row shape.

Each stays its own record, since the runner's lock and the worker's lease need
it as it is. The list is a database union, so a page costs the same however
much work has run.
"""

import json
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from django.db.models import Case, CharField, Exists, F, IntegerField, OuterRef, Q, QuerySet, Value, When
from django.db.models.fields.json import KT
from django.db.models.functions import Coalesce, Collate, Concat, NullIf
from django.db.models.lookups import Exact
from django.utils import timezone
from django.utils.text import get_text_list

from hq.domains.control_plane.models import OPERATION_LAPSE, OperationHost, OperationRequest, OperationState
from hq.domains.jobs.models import LAPSE as JOB_LAPSE, Job, JobState
lazy from hq.domains.control_plane.models import InboundEvent, InboundSource
lazy from hq.domains.control_plane.observations import OBSERVATIONS, ObservationSpec
lazy from hq.domains.control_plane.observations.host import DOCKER_CONTAINER
lazy from hq.domains.jobs.runner import AUDIT_TYPE as JOB_AUDIT_TYPE

from .asks import DONE, FAILED, QUEUED, RUNNING, Standing
from .automation_runs import CHECK, PREVIEW, capability_name, run_title
from .conditions import PROBLEMS, holding
from .doors import NOBODY, Attribution
from .projection import Combined, read_once
from .quoted import Quoted
from .tone import Tone, Toned
from .ui import counted
lazy from . import automation_adoption
lazy from .app_update_approval import PULLED, PULLED_KIND, RECEIPT, RECEIPT_KIND
lazy from .automation_runs import run_profile
lazy from .cadence import automation_standing
lazy from .containers import stacks
lazy from .entity_links import entity_link
lazy from .history import Acts, asked_by, asking, who_did_each
lazy from .hq_self import own_machine
lazy from .labels import dotted
lazy from .outbound_work import job_names
lazy from .references import Kind
lazy from .resource_operations import HANDOFF

type Source = Literal["job", "run"]
JOB: Source = "job"
RUN: Source = "run"


class TaskOutcome(Toned):
    """What a run's task did on one host (the bridge's ``RunEvent``), in the
    tone a run's state takes for the same news: started while it is under way."""

    STARTED = "started", JobState.RUNNING.tone
    OK = "ok", Tone.GOOD
    CHANGED = "changed", Tone.GOOD
    # Ended well and warned, its message carrying the warnings.
    WARNED = "warned", Tone.ATTENTION
    SKIPPED = "skipped", Tone.NEUTRAL
    FAILED = "failed", Tone.SERIOUS
    UNREACHABLE = "unreachable", Tone.SERIOUS


# Where a run's result holds its tasks so far, each a ``RunEvent``: written
# by its worker's renewals alone (``controller.renew_operation``) and read by
# ``run_events`` alone.
RUN_EVENTS = "events"
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
# What a run or scan waiting its turn says it waits on, where it is followed.
WORKER_DUE = "HQ's playbook worker has not taken it yet."
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
# named by its action.
_RUN_KIND = tuple(capability_name("\0").split("\0"))
# The automation work Jobs lists: a procedure's runs and the source's scans.
_LISTED = (OperationRequest.Action.RUN, OperationRequest.Action.SCAN)


@dataclass(frozen=True, slots=True)
class StatedKind:
    """What the records of a reading only a playbook states mean, in the
    shape a swept reading's ``ObservationSpec`` declares it: what the reading
    is called, each record's name, what it says and the tone of what it
    says."""

    label: str
    title: Callable[[Mapping[str, Any]], str]
    describe: Callable[[Mapping[str, Any]], str]
    tone: Callable[[Mapping[str, Any]], Tone]


@dataclass(frozen=True, slots=True)
class Reading:
    """One reading a run stated, in words: its kind and what it is called
    ("" where HQ declares nothing of it), its machine, each record's name,
    what it says and its tone ("" where its kind declares none), and how many
    records it holds."""

    kind: str
    label: str
    host: str
    records: tuple[tuple[str, str, str], ...]
    count: int


def _stated_kinds() -> dict[str, StatedKind]:
    """The readings only a playbook states, by kind."""

    return {PULLED: PULLED_KIND, RECEIPT: RECEIPT_KIND}


def wording(kind: str) -> ObservationSpec | StatedKind | None:
    """What HQ declares a reading of ``kind`` means: the swept reading's
    spec, or a playbook's own (``_stated_kinds``)."""

    return OBSERVATIONS.get(kind) or _stated_kinds().get(kind)


def _worded(kind: str, host: str, records: Sequence[Mapping[str, Any]]) -> Reading:
    declared = wording(kind)
    if declared is None:
        return Reading(kind, "", host, (), len(records))
    toned = declared.tone if isinstance(declared, StatedKind) else lambda _record: ""
    worded = tuple((declared.title(record), declared.describe(record), toned(record)) for record in records)
    return Reading(kind, declared.label, host, worded, len(records))


@dataclass(frozen=True, slots=True)
class RunEvidence:
    """What a piece of work shows for itself (``partials/_run_evidence.html``):
    its machines, its asker's correlation id, the worker holding it and until
    when while it is claimed, how many tries it took, its tasks' outcomes
    (each a ``RunEvent`` with its ``tone``), each reading its playbook
    stated, what each name served of the certificate it checked, and the
    result of an operation with no page of its own.

    ``stated`` is each reading its playbook stated, a ``{host, kind,
    records}`` per machine and kind (``stated_readings``)."""

    hosts: tuple[str, ...] = ()
    correlation: str = ""
    worker: str = ""
    lease_until: datetime | None = None
    attempts: int = 0
    events: tuple[dict[str, Any], ...] = ()
    stated: tuple[dict[str, Any], ...] = ()
    # The certificate's fingerprint, and each name's answer (``matches_expected``).
    expected: str = ""
    consumers: tuple[dict[str, Any], ...] = ()
    raw: str = ""

    @property
    def under_way(self) -> tuple[dict[str, Any], ...]:
        """The tasks started and not yet ended while a worker holds the run."""

        return (
            tuple(event for event in self.events if event.get("outcome") == TaskOutcome.STARTED) if self.worker else ()
        )

    @property
    def readings(self) -> tuple[Reading, ...]:
        """Each reading in the words its kind declares (``wording``); one of a
        kind HQ declares none for is named and counted, its records left to
        ``raw_readings``."""

        return tuple(_worded(str(each["kind"]), str(each["host"]), each["records"]) for each in self.stated)

    def _toned(self, *tones: Tone) -> tuple[tuple[str, Tone], ...]:
        return tuple(
            (dotted(title, said), Tone(tone))
            for reading in self.readings
            for title, said, tone in reading.records
            if tone in tones
        )

    @property
    def amiss(self) -> tuple[tuple[str, Tone], ...]:
        """Each record of its readings whose kind says it in a tone to act on,
        said in a line, with that tone."""

        return self._toned(Tone.ATTENTION, Tone.SERIOUS)

    @property
    def told(self) -> str:
        """What its readings say it did, in a line: each record to act on,
        else each its kind says in a good tone; "" where none says either."""

        return "; ".join(line for line, _tone in self.amiss or self._toned(Tone.GOOD))

    @property
    def tallied(self) -> str:
        """How its tasks ended, counted by outcome ("4 tasks: 2 ok and 2
        skipped"); "" with none ended."""

        counts = Counter(str(event.get("outcome") or "") for event in self.events)
        ended = [(each, counts[each]) for each in TaskOutcome if each != TaskOutcome.STARTED and counts[each]]
        if len(ended) == 1:
            ((outcome, count),) = ended
            return f"{counted(count, 'task')} {outcome}"
        said = get_text_list([f"{count} {outcome}" for outcome, count in ended], "and")
        return f"{counted(sum(count for _outcome, count in ended), 'task')}: {said}" if ended else ""

    @property
    def raw_readings(self) -> str:
        return json.dumps(list(self.stated), indent=2, sort_keys=True) if self.stated else ""

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
    def stopped(self) -> tuple[str, str]:
        """The task it stopped on, once it ended badly, and what that said; the
        task "" where it stopped before any ran, said by its note."""

        failed = next(
            (
                event
                for event in self.evidence.events
                if event.get("outcome") in (TaskOutcome.FAILED, TaskOutcome.UNREACHABLE)
            ),
            None,
        )
        if failed is None:
            return "", self.note
        return str(failed.get("task") or ""), Quoted(str(failed.get("message") or ""))

    @property
    def url(self) -> str:
        """Its own page, for a run or a scan; "" for a job or any other
        operation (a merge, a reconcile), which has none."""

        return (
            run_url(self.id)
            if self.source == RUN and (operation_of(self.kind) or self.kind == OperationRequest.Action.SCAN)
            else ""
        )


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


def _run_title() -> Collate:
    """The run's playbook by its title, ``run_title`` of a procedure no longer
    adopted, or any other operation's action in words (a scan's among them),
    as SQL. Case-blind, so the list sorts by it as it reads."""

    operation = KT("input__operation")
    titled = (
        When(Exact(operation, Value(playbook)), then=Value(title))
        for playbook, title in automation_adoption.playbook_titles().items()
        if title
    )
    unadopted = Concat(Value(run_title("", {})), operation, output_field=CharField())
    run = Case(
        *titled,
        default=unadopted,
        output_field=CharField(),
    )
    acted = (
        When(action=action, then=Value(action.label))
        for action in OperationRequest.Action
        if action != OperationRequest.Action.RUN
    )
    return Collate(Case(*acted, default=run, output_field=CharField()), "NOCASE")


def _shown_title(kind: str, title: str, *, check: bool = False) -> str:
    """The work ``kind`` names as a page draws it: its playbook's title as the
    author wrote it (``quoted.Quoted``), which SQL's copy of it
    (``_run_title``) is not, else ``title``; a run in check mode
    (``check``) as a preview of it."""

    shown = automation_adoption.playbook_titles().get(operation_of(kind)) or title
    return f"{PREVIEW.title}: {shown}" if check else shown


def _run_kind() -> Case:
    """``capability_name`` of the run's procedure, or the action of a scan, as SQL."""

    start, end = _RUN_KIND
    return Case(
        When(
            action=OperationRequest.Action.RUN,
            then=Concat(Value(start), KT("input__operation"), Value(end), output_field=CharField()),
        ),
        default=F("action"),
        output_field=CharField(),
    )


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


def _untaken(operation: OperationRequest, seen: JobState, said: dict[str, str]) -> str:
    """What a run or scan that waits is waiting for, in a sentence, while the
    playbook worker that takes it does not report; "" while it does, or
    for anything else. ``said`` keeps each profile's, so a list asks once."""

    if seen != JobState.QUEUED or operation.action not in _LISTED:
        return ""
    profile = run_profile(operation)
    if profile not in said:
        said[profile] = automation_standing(profile).cannot("", now=timezone.now())
    return said[profile]


def run_rows(operations: Iterable[OperationRequest], now: datetime | None = None) -> list[WorkRun]:
    """Operations read through ``seen_as_runs`` as the row every list of work
    draws, who asked for each read in one query."""

    found = list(operations)
    who = asked_by(OperationRequest, found)
    now = now or timezone.now()
    said: dict[str, str] = {}
    return [
        _run_row(
            operation,
            JobState(operation.seen),
            operation.title,
            now,
            who.get(str(operation.pk), NOBODY),
            untaken=_untaken(operation, JobState(operation.seen), said),
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
    return _run_row(found, seen, found.title, now, who, untaken=_untaken(found, seen, {})), found


def run_url(operation_id: str) -> str:
    """The page of the run or scan ``operation_id``."""

    return entity_link(Kind.RUN, operation_id).url


def run_row(operation: OperationRequest) -> WorkRun:
    """One operation, read again, as the row every list of work draws."""

    return run_rows(seen_as_runs(OperationRequest.objects.filter(pk=operation.pk).select_related("resource")))[0]


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
    said: dict[str, str] = {}
    built: list[WorkRun] = []
    for source, pk, seen, _created, title, _lead in page:
        by = who[source].get(str(pk), NOBODY)
        if source == JOB:
            built.append(job_run(jobs[pk], title, by))
        else:
            built.append(
                _run_row(runs[pk], JobState(seen), title, now, by, untaken=_untaken(runs[pk], JobState(seen), said))
            )
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
        note=Quoted(job.seen_error if seen in ENDED_BADLY else job.note),
        by=by,
        created_at=job.created_at,
        started=job.started_at,
        finished=job.finished_at,
        duration=job.duration_seconds,
        percent=100 if seen == JobState.SUCCEEDED else job.percent,
    )


def standing_of(run: WorkRun) -> Standing:
    """How a job or a run stands, for a control that follows it and every
    card that names it: waiting its turn, the worker it waits on; under way,
    the task its worker runs now and how far it has got. A run that will not
    start while no worker takes it has ended, so no page follows it."""

    state = FAILED if run.word == NOT_STARTED else STANDS[JobState(run.state)]
    now = get_text_list(
        [dotted(str(event.get("task") or ""), event["message"]) for event in run.evidence.under_way], "and"
    )
    waits = WORKER_DUE if state == QUEUED and run.source == RUN else ""
    note = run.note or now or waits
    return Standing(state, note, since=run.created_at, percent=run.percent, word=run.word, url=run.url)


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


# Each change to a container the host's watcher asks Docker for
# (``DockerFilters`` in controller/host/hostevents), by Docker's action, as a
# page says it.
_CONTAINER_CHANGES = {
    "start": "started",
    "die": "stopped",
    "destroy": "removed",
    "oom": "out of memory",
    "health_status: healthy": "healthy",
    "health_status: unhealthy": "unhealthy",
    "health_status: starting": "starting",
}


def held_note(handoff: Mapping[str, str], since: datetime | None) -> str:
    """What a held run waits on: each unit it handed off to, on its machine,
    then what each container of a Compose project on HQ's own machine did
    since ``since``, from the events HQ stored. Only HQ's machine tells HQ of
    its containers, so an app elsewhere shows nothing. A unit is named
    without its instance, which names the revision the card already shows
    and, as a commit's full hash, cannot wrap."""

    named = {
        machine: unit.partition("@")[0] if "@" in unit else unit.rpartition(".")[0] for machine, unit in handoff.items()
    }
    waits = get_text_list([f"{name} on {machine}" for machine, name in named.items()], "and")
    here = own_machine()
    if here is None or since is None:
        return f"Waiting on {waits}"
    prefix = f"{DOCKER_CONTAINER}."
    heard = InboundEvent.objects.filter(
        source=InboundSource.HOST, event__startswith=prefix, applied_at__gte=since
    ).order_by("applied_at")
    stack = stacks()
    done: dict[str, list[str]] = {}
    for subject, event in heard.values_list("subject", "event"):
        if (here.name, subject) in stack:
            action = event.removeprefix(prefix)
            done.setdefault(subject, []).append(_CONTAINER_CHANGES.get(action, action))
    return dotted(
        f"Waiting on {waits}", *(f"{name} so far: {get_text_list(said, 'and')}" for name, said in done.items())
    )


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
    held = claimed and HANDOFF in result
    evidence = RunEvidence(
        hosts=tuple(asked.get("hosts") or ()),
        correlation=str(asked.get("correlation") or ""),
        worker=operation.claimed_by if claimed else "",
        lease_until=operation.lease_expires_at if claimed else None,
        attempts=operation.attempt_count,
        events=run_events(result),
        stated=stated_readings(result),
        expected=str(status.get("expected_fingerprint_sha256") or ""),
        consumers=tuple(status.get("consumers") or ()),
        # A run or a scan has a page of its own, where its record is.
        raw=""
        if operation.action in _LISTED or not result
        else json.dumps(shown_result(result), indent=2, sort_keys=True, default=str),
    )
    return WorkRun(
        source=RUN,
        id=str(operation.pk),
        title=_shown_title(
            kind := _kind_of(operation.action, str(asked.get("operation") or "")),
            title,
            check=asked.get(CHECK) is True,
        ),
        kind=kind,
        state=seen.value,
        word=word,
        tone=tone,
        # A run not yet ended says only its state, in the State column, unless
        # it will not start or waits on a unit it handed off to.
        note=untaken
        if stuck
        else held_note(result[HANDOFF], operation.claimed_at)
        if held
        else STOPPED_REPORTING
        if lapsed
        else ""
        if operation.state in UNENDED
        else _said(operation, problem, evidence),
        by=by,
        created_at=operation.created_at,
        started=started,
        finished=finished,
        # A lapsed run was last heard from when its lease ran out.
        duration=_seconds(started, finished or (operation.lease_expires_at if lapsed else now)),
        evidence=evidence,
        effect=str((operation.resource.spec or {}).get("effect", "")),
    )


def _kind_of(action: str, procedure: str) -> str:
    """What the list filters an operation by: the command a run's procedure
    is run through, else its action."""

    return capability_name(procedure) if action == OperationRequest.Action.RUN else action


def _said(operation: OperationRequest, problem: dict[str, Any] | None, evidence: RunEvidence) -> str:
    """How an ended operation went, in a sentence: the condition that says
    what went wrong before its own message; one that ended well says what
    its readings say it did, else the result its playbook stated, else how
    its tasks ended."""

    result = operation.result or {}
    said = result.get("message") or ""
    if operation.state == OperationState.FAILED:
        active = holding(result.get("conditions"))
        return Quoted((active[0].get("message") if active else "") or said or operation.reason)
    if problem:
        return Quoted(problem.get("message") or said or operation.reason)
    return evidence.told or Quoted(said) or evidence.tallied or Quoted(operation.reason)


def shown_result(result: dict[str, Any]) -> dict[str, Any]:
    """An operation's result less the certificate, which is on its record's
    page: a row of history is not a second copy."""

    status = result.get("status") or {}
    return {**result, "status": {k: v for k, v in status.items() if k != "certificate_pem"}} if status else result


def run_events(result: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    """A run's tasks so far from its result, in the order they started, each in its outcome's tone."""

    return tuple({**event, "tone": outcome_tone(event.get("outcome", ""))} for event in result.get(RUN_EVENTS) or ())


def stated_readings(result: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    """Each reading a run's playbook stated, by machine then kind, A to Z, with its records."""

    readings = (result.get("stated") or {}).get("readings") or {}
    return tuple(
        {"host": host, "kind": kind, "records": list(records)}
        for host, kinds in sorted(readings.items())
        for kind, records in sorted(kinds.items())
    )


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
