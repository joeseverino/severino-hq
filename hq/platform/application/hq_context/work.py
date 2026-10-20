"""HQ's own work: the units it ships and its credentials renderers, as the
``host.unit`` and ``host.render_status`` readings of its machine say."""

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any

from django.utils import timezone
lazy from django.db.models import Max, Q

from hq.domains.control_plane.observations.host import RENDER_STATUS_KIND, UNIT_KIND
lazy from hq.domains.jobs.models import Job as JobRow

from ..drawings import Dot, Dots
from ..projection import read_once
from ..timestamps import moment
from ..ui import counted
from .tones import ATTENTION, GOOD, NEUTRAL, SERIOUS, iso
lazy from .. import outbound_work, scheduled_work
lazy from ..facts import stored_snapshots
lazy from ..render_status_findings import FAILURE_CLASSES, UNKNOWN_FAILURE, UNREAD_REASONS, rendering
lazy from ..routes import reverse
lazy from ..unit_findings import unit_state


@dataclass(frozen=True, slots=True)
class Job:
    """One unit HQ ships, as its own work: what it runs, when and how it last
    ran, and when it runs next."""

    unit: str
    runs: str
    last_run: datetime | None
    result: str
    tone: str
    next_run: datetime | None = None


@dataclass(frozen=True, slots=True)
class Renderer:
    """One credentials renderer's account of its own runs."""

    name: str
    result: str
    tone: str
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
    tone: str
    # When it is next due; None while it is due now.
    next_due: datetime | None
    # Its recent runs, oldest first: filled for each that succeeded.
    runs: Dots | None = None


@dataclass(frozen=True, slots=True)
class Work:
    """The ``host.unit`` and ``host.render_status`` readings, or why not, and
    the work done on a cadence."""

    jobs: tuple[Job, ...] = ()
    renderers: tuple[Renderer, ...] = ()
    scheduled: tuple[Scheduled, ...] = ()
    read_at: datetime | None = None
    # Why the units could not be read, in a sentence; "" when they were.
    unread: str = ""
    renderers_unread: str = ""

    @property
    def services(self) -> tuple[Job, ...]:
        """What keeps running rather than running on a schedule: the unit that
        serves HQ is one of these."""

        return tuple(job for job in self.jobs if job.next_run is None and job.result == "Running")


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
        scheduled=_scheduled_work(),
    )


def _unread(snapshot: Any, noun: str) -> str:
    """Why a reading of HQ's own machine says nothing, or ""."""

    if snapshot is None:
        return f"No controller has reported {noun} yet."
    if not snapshot.connected:
        return f"The controller on HQ's machine is not given {noun} to read."
    if not snapshot.reachable:
        reason = f": {snapshot.error}" if snapshot.error else "."
        return f"The controller could not read {noun}{reason}"
    if snapshot.error and not snapshot.records:
        return f"The controller's answer about {noun} could not be understood."
    return ""


def _jobs(snapshot: Any) -> tuple[tuple[Job, ...], str]:
    unread = _unread(snapshot, "HQ's background jobs")
    if unread:
        return (), unread
    units = [unit_state(record) for record in snapshot.records or () if isinstance(record, dict)]
    by_name = {unit.unit: unit for unit in units}
    started = {unit.activates for unit in units if unit.starter}
    jobs = [_scheduled(unit, by_name.get(unit.activates)) for unit in units if unit.starter]
    jobs.extend(_standing(unit) for unit in units if not unit.starter and unit.unit not in started)
    return tuple(sorted(jobs, key=lambda job: job.unit)), ""


def _runs(unit: str) -> str:
    """What a unit runs, in the words HQ has for it: the scheduled work an
    instance of the job template names, else the unit's own name."""

    instance = unit.partition("@")[2].rpartition(".")[0]
    return next((work.label for work in scheduled_work.SCHEDULED if instance and work.name == instance), unit)


def _scheduled(starter: Any, target: Any) -> Job:
    """A timer or path, as the work it starts."""

    result, tone = _result(starter) if target is None or starter.absent else _result(target)
    return Job(
        unit=starter.unit,
        runs=_runs(starter.activates) if starter.activates else "Nothing",
        last_run=moment(starter.last_trigger_at) or (moment(target.started_at) if target is not None else None),
        result=result,
        tone=tone,
        next_run=moment(starter.next_elapse_at),
    )


def _standing(unit: Any) -> Job:
    result, tone = _result(unit)
    return Job(unit=unit.unit, runs=_runs(unit.unit), last_run=moment(unit.started_at), result=result, tone=tone)


def _result(unit: Any) -> tuple[str, str]:
    """How a unit's last run went, in words, and its tone."""

    if unit.absent:
        return unit.absent, SERIOUS
    if unit.failed:
        return f"Failed: {unit.result or 'unknown'}", SERIOUS
    if unit.active in ("activating", "reloading"):
        return "Running now", NEUTRAL
    if unit.active == "active" and unit.sub == "running":
        return "Running", GOOD
    if unit.condition == "no":
        return "Skipped: its condition did not hold", NEUTRAL
    if unit.result == "success":
        return "Succeeded", GOOD
    if unit.result:
        return f"Ended: {unit.result}", ATTENTION
    return ("Waiting", GOOD) if unit.starter else ("Not run since the machine started", NEUTRAL)


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
            result, tone = UNREAD_REASONS.get(run.reason, run.reason), ATTENTION
        elif run.failed:
            result, tone = f"Failed: {FAILURE_CLASSES.get(run.failure, UNKNOWN_FAILURE)}", SERIOUS
        else:
            result, tone = ("Refreshed" if run.outcome == "rendered" else "Already current"), GOOD
        found.append(Renderer(run.name, result, tone, moment(run.attempted_at), moment(run.confirmed_at)))
    return tuple(found), ""


def _plain(row: Job | Renderer) -> dict[str, Any]:
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


# How a job of a piece of scheduled work ended, in words, with its tone.
_ENDED = {
    "succeeded": ("Succeeded", GOOD),
    "failed": ("Failed", SERIOUS),
    "queued": ("Running now", NEUTRAL),
    "running": ("Running now", NEUTRAL),
}
# Recent runs shown per piece of work: as many dots as Dots draws.
RECENT = Dots.LIMIT


def _run_dot(job: Any) -> Dot:
    result, _tone = _ENDED.get(job.state, ("Did not finish", ATTENTION))
    return Dot(
        filled=job.state == "succeeded",
        tip=f"{result}, {timezone.localtime(job.created_at):%b %-d %H:%M}",
        url=reverse("jobs:status", args=[job.pk]),
    )


def _scheduled_work() -> tuple[Scheduled, ...]:
    """Every piece of work done on a cadence, with its recent runs and when it
    is next due. Three queries, however many jobs there are."""

    declared = [(work.name, work.label, work.every) for work in scheduled_work.SCHEDULED if work.every is not None]
    declared += [
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
            result, tone = _ENDED.get(runs[0].state, ("Did not finish", ATTENTION))
        else:
            result, tone = ("Not run lately", ATTENTION) if name in last_seen else ("Not done yet", NEUTRAL)
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
    return {
        "read_at": iso(work.read_at),
        "unread": work.unread,
        "jobs": [_plain(job) for job in work.jobs],
        "credentials_unread": work.renderers_unread,
        "credentials": [_plain(item) for item in work.renderers],
        "scheduled": [_scheduled_json(item) for item in work.scheduled],
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
