"""HQ's own work: the units it ships and its credentials renderers, as the
``host.unit`` and ``host.render_status`` readings of its machine say."""

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any

from django.utils import timezone
lazy from django.db.models import Max, Q

from hq.domains.control_plane.observations.host import RENDER_STATUS_KIND, UNIT_KIND, unescaped
lazy from hq.domains.jobs.models import Job as JobRow
lazy from hq.platform.core.included_parts import DOTS

from ..automation_workers import ProfileWorker, profile_workers, reached_by_profile, workers_table
from ..backup_findings import OffSiteCopy, off_site_copies
from ..drawings import Dot, Dots
from ..moments import moment
from ..projection import iso, read_once
from ..tables import Cell, DataTable, Piece, TableColumn
from ..tone import Tone
from ..ui import counted
lazy from .. import outbound_work
lazy from ..facts import stored_snapshots
lazy from ..render_status_findings import FAILURE_CLASSES, UNKNOWN_FAILURE, UNREAD_REASONS, rendering
lazy from ..routes import reverse
lazy from ..unit_findings import unit_state
lazy from ..work_runs import job_run


@dataclass(frozen=True, slots=True)
class WorkUnit:
    """One unit HQ ships, as its own work: what it runs, when and how it last
    ran, and when it runs next."""

    # The unit's name as systemd addresses it, escapes and all.
    unit: str
    runs: str
    last_run: datetime | None
    result: str
    tone: Tone
    next_run: datetime | None = None
    # The unit's name as a person reads it: ``unit`` with its escapes decoded.
    name: str = ""
    # Whether ``runs`` is a unit's name, an identifier, rather than words.
    runs_unit: bool = False
    # What the unit is, in its unit file's words; "" when systemd gave none.
    title: str = ""


@dataclass(frozen=True, slots=True)
class Renderer:
    """One credentials renderer's account of its own runs."""

    name: str
    result: str
    tone: Tone
    attempted: datetime | None = None
    succeeded: datetime | None = None


@dataclass(frozen=True, slots=True)
class Scheduled:
    """One piece of work done on a cadence, the host's or an extension's, as
    its declaration and its jobs say."""

    name: str
    label: str
    every: str
    last_run: datetime | None
    result: str
    tone: Tone
    # When it is next due; None while it is due now.
    next_due: datetime | None
    # Its recent runs, oldest first: filled for each that succeeded.
    runs: Dots | None = None


@dataclass(frozen=True, slots=True)
class Work:
    """The ``host.unit`` and ``host.render_status`` readings, or why not, and
    the work done on a cadence."""

    jobs: tuple[WorkUnit, ...] = ()
    renderers: tuple[Renderer, ...] = ()
    scheduled: tuple[Scheduled, ...] = ()
    read_at: datetime | None = None
    # Why the units could not be read, in a sentence; "" when they were.
    unread: str = ""
    renderers_unread: str = ""
    # HQ's playbook workers, one per privilege profile.
    workers: tuple[ProfileWorker, ...] = ()
    # The newest off-site copy of HQ's backups, in each backup connection.
    off_site: tuple[OffSiteCopy, ...] = ()

    @property
    def services(self) -> tuple[WorkUnit, ...]:
        """What keeps running rather than running on a schedule: the unit that
        serves HQ is one of these."""

        return tuple(job for job in self.jobs if job.next_run is None and job.result == "Running")

    @property
    def jobs_table(self) -> DataTable:
        return DataTable.of("Units", _JOB_COLUMNS, self.jobs)

    @property
    def scheduled_table(self) -> DataTable:
        return DataTable.of("Scheduled work", _SCHEDULED_COLUMNS, self.scheduled)

    @property
    def renderers_table(self) -> DataTable:
        return DataTable.of("Credentials refresh", _RENDERER_COLUMNS, self.renderers)

    @property
    def workers_table(self) -> DataTable:
        return workers_table(self.workers)


def _unit(job: WorkUnit) -> Cell:
    """A systemd unit as a person reads it: its unit file's title over its
    name, its escapes decoded; the name alone when it has no title."""

    name = Cell(job.name, readout=True)
    return Cell(lines=(Cell(job.title), name)) if job.title else name


_JOB_COLUMNS = (
    TableColumn("Unit", kind="identifier", cell=_unit),
    TableColumn("Runs", kind="identifier", drop=3, cell=lambda job: Cell(job.runs, readout=job.runs_unit)),
    TableColumn("Last run", kind="status", drop=2, cell=lambda job: Cell(moment=job.last_run)),
    TableColumn("Result", kind="status", css="key-col", cell=lambda job: Cell(job.result, tone=str(job.tone))),
    TableColumn("Next run", kind="status", css="optional-col", drop=1, cell=lambda job: Cell(moment=job.next_run)),
)


def _runs(item: Scheduled) -> Cell:
    """Its recent runs as dots, each that succeeded filled."""

    if not item.runs:
        return Cell()
    return Cell(piece=Piece(DOTS, {"dots": item.runs}, words="•" * len(item.runs.shown)))


_SCHEDULED_COLUMNS = (
    TableColumn("Scheduled work", cell=lambda item: Cell(item.label)),
    TableColumn("Schedule", kind="status", css="optional-col", drop=2, cell=lambda item: Cell(item.every)),
    TableColumn("Recent runs", kind="status", drop=3, cell=_runs),
    TableColumn("Last result", kind="status", css="key-col", cell=lambda item: Cell(item.result, tone=str(item.tone))),
    TableColumn(
        "Next due",
        kind="status",
        css="optional-col",
        drop=1,
        cell=lambda item: Cell(moment=item.next_due) if item.next_due else Cell("Now"),
    ),
)
_RENDERER_COLUMNS = (
    TableColumn("Credentials refresh", kind="identifier", cell=lambda item: Cell(item.name, readout=True)),
    TableColumn("Result", kind="status", css="key-col", cell=lambda item: Cell(item.result, tone=str(item.tone))),
    TableColumn("Tried", kind="status", drop=2, cell=lambda item: Cell(moment=item.attempted)),
    TableColumn("Last confirmed current", kind="status", drop=1, cell=lambda item: Cell(moment=item.succeeded)),
)


def work() -> Work:
    """The units HQ ships and its credentials renderers, as last read."""

    return read_once("hq_context.work", _work)


def _work() -> Work:
    stored = stored_snapshots()
    units = next(iter(stored.get(UNIT_KIND, ())), None)
    renders = next(iter(stored.get(RENDER_STATUS_KIND, ())), None)
    jobs, unread = _jobs(units)
    renderers, renderers_unread = _renderers(renders)
    return Work(
        jobs=jobs,
        renderers=renderers,
        read_at=units.observed_at if units is not None and units.connected else None,
        unread=unread,
        renderers_unread=renderers_unread,
        scheduled=_on_a_cadence(),
        workers=profile_workers(),
        off_site=off_site_copies(),
    )


def _unread(snapshot: Any, noun: str) -> str:
    """Why a reading of HQ's own machine says nothing, or ""."""

    if snapshot is None:
        return f"No controller has reported {noun} yet."
    if not snapshot.connected:
        return f"The controller on HQ's machine is not given {noun} to read."
    if not snapshot.reachable:
        reason = f": {snapshot.error}" if snapshot.error else "."
        return f"HQ could not read {noun}{reason}"
    if snapshot.error and not snapshot.records:
        return f"The controller's answer about {noun} could not be understood."
    return ""


def _jobs(snapshot: Any) -> tuple[tuple[WorkUnit, ...], str]:
    unread = _unread(snapshot, "HQ's units")
    if unread:
        return (), unread
    # A service another package installs is in the reading for its restarts
    # alone; it is not one of HQ's jobs.
    units = [
        unit
        for record in snapshot.records or ()
        if isinstance(record, dict) and not (unit := unit_state(record)).foreign
    ]
    by_name = {unit.unit: unit for unit in units}
    # A socket that starts a new instance per connection names no one unit it
    # starts, so it stands as itself.
    starters = [unit for unit in units if unit.starter and unit.activates]
    started = {unit.activates for unit in starters}
    jobs = [_scheduled(unit, by_name.get(unit.activates)) for unit in starters]
    jobs.extend(_standing(unit) for unit in units if unit not in starters and unit.unit not in started)
    return tuple(sorted(jobs, key=lambda job: job.unit)), ""


def _scheduled_label(unit: str) -> str:
    """The host's work an instance of the job template runs, or ""."""

    instance = unit.partition("@")[2].rpartition(".")[0]
    return next((work.label for work in outbound_work.host_work() if instance and work.name == instance), "")


def _runs(unit: str, title: str = "", shown: str = "") -> tuple[str, bool]:
    """What a unit a trigger starts is, in the words HQ has for it: the
    scheduled work an instance of the job template names, else its unit file's
    title unless that repeats the ``shown`` words already beside it, else the
    unit's own name; and whether it is that name."""

    label = _scheduled_label(unit) or (title if title != shown else "")
    return (label, False) if label else (unescaped(unit), True)


def _scheduled(starter: Any, target: Any) -> WorkUnit:
    """A timer, path or socket, as the work it starts."""

    result, tone = _result(starter) if target is None or starter.absent else _result(target)
    runs, runs_unit = (
        _runs(starter.activates, target.title if target is not None else "", starter.title)
        if starter.activates
        else ("Nothing", False)
    )
    return WorkUnit(
        unit=starter.unit,
        name=unescaped(starter.unit),
        title=starter.title,
        runs=runs,
        runs_unit=runs_unit,
        last_run=moment(starter.last_trigger_at) or (moment(target.started_at) if target is not None else None),
        result=result,
        tone=tone,
        next_run=moment(starter.next_elapse_at),
    )


def _standing(unit: Any) -> WorkUnit:
    """A service, mount or other unit that is the work itself: it starts no
    other unit, so its Runs is empty beyond the scheduled work its name says."""

    result, tone = _result(unit)
    return WorkUnit(
        unit=unit.unit,
        name=unescaped(unit.unit),
        title=unit.title,
        runs=_scheduled_label(unit.unit),
        last_run=moment(unit.started_at),
        result=result,
        tone=tone,
    )


def _result(unit: Any) -> tuple[str, Tone]:
    """How a unit's last run went, in words, and its tone."""

    if unit.absent:
        return unit.absent, Tone.SERIOUS
    if unit.failed:
        return f"Failed: {unit.result or 'unknown'}", Tone.SERIOUS
    if unit.active in ("activating", "reloading"):
        return JobRow.State.RUNNING.label, Tone.NEUTRAL
    if unit.active == "active" and unit.sub == "running":
        return JobRow.State.RUNNING.label, Tone.GOOD
    if unit.active == "active" and unit.sub in ("listening", "mounted"):
        # A socket or a mount doing its work, in systemd's word for it.
        return unit.sub.capitalize(), Tone.GOOD
    if unit.condition == "no":
        return "Skipped", Tone.NEUTRAL
    if unit.result == "success":
        return JobRow.State.SUCCEEDED.label, Tone.GOOD
    if unit.result:
        return JobRow.State.LOST.label, Tone.ATTENTION
    return ("Waiting", Tone.GOOD) if unit.starter else ("Not run since the machine started", Tone.NEUTRAL)


def _renderers(snapshot: Any) -> tuple[tuple[Renderer, ...], str]:
    unread = _unread(snapshot, "how credentials were refreshed")
    if unread:
        return (), unread
    found = []
    for record in snapshot.records or ():
        if not isinstance(record, dict):
            continue
        run = rendering(record, snapshot.observed_at)
        if run.reason:
            result, tone = UNREAD_REASONS.get(run.reason, run.reason), Tone.ATTENTION
        elif run.failed:
            result, tone = f"Failed: {FAILURE_CLASSES.get(run.failure, UNKNOWN_FAILURE)}", Tone.SERIOUS
        else:
            result, tone = ("Refreshed" if run.outcome == "rendered" else "Already current"), Tone.GOOD
        found.append(Renderer(run.name, result, tone, moment(run.attempted_at), moment(run.confirmed_at)))
    return tuple(found), ""


def _plain(row: WorkUnit | Renderer) -> dict[str, Any]:
    """A row of words and moments as JSON: every field, each moment in ISO 8601."""

    return {name: iso(value) if isinstance(value, datetime) else value for name, value in asdict(row).items()}


def _every(span: timedelta) -> str:
    """A cadence in words: "Hourly", "Daily", "Every 6 hours"."""

    hours = int(span.total_seconds() // 3600)
    if hours == 1:
        return "Hourly"
    if hours == 24:
        return "Daily"
    if hours and hours % 24 == 0:
        return f"Every {counted(hours // 24, 'day')}"
    return f"Every {counted(hours, 'hour')}" if hours else f"Every {counted(int(span.total_seconds() // 60), 'minute')}"


# Recent runs shown per piece of work: as many dots as Dots draws.
RECENT = Dots.LIMIT


def _run_dot(job: Any) -> Dot:
    return Dot(
        filled=job.state == JobRow.State.SUCCEEDED,
        tip=f"{job_run(job).word}, {timezone.localtime(job.created_at):%b %-d %H:%M}",
        url=reverse("jobs:list"),
    )


def _on_a_cadence() -> tuple[Scheduled, ...]:
    """Every piece of work done on a cadence, with its recent runs and when it
    is next due. Three queries, however many jobs there are."""

    declared = [
        (work.name, work.label, work.every) for work in outbound_work.declared_work().values() if work.every is not None
    ]
    if not declared:
        return ()
    now = timezone.now()
    # Only the window a history is drawn from: twelve cadences of each.
    window = Q()
    for name, _label, every in declared:
        window |= Q(kind=name, created_at__gte=now - every * RECENT)
    recent: dict[str, list[Any]] = {}
    for job in JobRow.objects.filter(window).order_by("-created_at"):
        recent.setdefault(job.kind, []).append(job)
    names = [name for name, _label, _every in declared]
    last_seen = dict(
        JobRow.objects.filter(kind__in=names).values("kind").annotate(at=Max("created_at")).values_list("kind", "at")
    )
    succeeded = dict(
        JobRow.objects.filter(kind__in=names, state=JobRow.State.SUCCEEDED)
        .values("kind")
        .annotate(at=Max("finished_at"))
        .values_list("kind", "at")
    )
    found = []
    for name, label, every in declared:
        runs = recent.get(name, [])[:RECENT]
        if runs:
            latest = job_run(runs[0])
            result, tone = latest.word, latest.tone
        else:
            result, tone = ("Not run lately", Tone.ATTENTION) if name in last_seen else ("Not done yet", Tone.NEUTRAL)
        last = succeeded.get(name)
        found.append(
            Scheduled(
                name=name,
                label=label,
                every=_every(every),
                last_run=(runs[0].started_at or runs[0].created_at) if runs else last_seen.get(name),
                result=result,
                tone=tone,
                next_due=last + every if last is not None else None,
                runs=Dots(tuple(_run_dot(job) for job in reversed(runs)), ordered=True) if runs else None,
            )
        )
    return tuple(sorted(found, key=lambda item: item.label))


def work_json(work: Work) -> dict[str, Any]:
    reached = reached_by_profile() if work.workers else {}
    return {
        "read_at": iso(work.read_at),
        "unread": work.unread,
        "jobs": [_plain(job) for job in work.jobs],
        "credentials_unread": work.renderers_unread,
        "credentials": [_plain(item) for item in work.renderers],
        "scheduled": [_scheduled_json(item) for item in work.scheduled],
        "playbook_workers": [_worker_json(worker, reached) for worker in work.workers],
        "off_site_backups": [_off_site_json(copy) for copy in work.off_site],
    }


def _scheduled_json(item: Scheduled) -> dict[str, Any]:
    return {
        "name": item.name,
        "label": item.label,
        "every": item.every,
        "last_run": iso(item.last_run),
        "result": item.result,
        "next_due": iso(item.next_due),
        "recent": [dot.tip for dot in item.runs.dots] if item.runs else [],
    }


def _worker_json(worker: ProfileWorker, reached: dict[str, tuple[str, ...]]) -> dict[str, Any]:
    return {
        "profile": worker.profile,
        "state": worker.standing.state.label,
        "seen_at": iso(worker.standing.seen_at),
        "claimed_at": iso(worker.standing.claimed_at),
        "machines": list(reached.get(worker.profile, ())),
    }


def _off_site_json(copy: OffSiteCopy) -> dict[str, Any]:
    return {
        "connection_ref": copy.connection_ref,
        "key": copy.key,
        "uploaded_at": iso(copy.uploaded_at),
        "copies": copy.copies,
        "late": copy.late(timezone.now()),
    }
