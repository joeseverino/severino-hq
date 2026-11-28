"""What HQ read of itself once a release of it went live, kept by the release's
commit: the readings it is taken from are replaced on every read, and GitHub
keeps the release itself (``releases``).

Taken from readings HQ already holds, never from the host: its own
container's ``portainer.runtime`` record, its machine's ``host.unit``
records, and the ``host.rehearsal`` record when it names the release, which
a sweep reads with the units. A repository read that shows production's
newest release live with no snapshot asks for them, once, and the first sweep
after which the container's and the units' are newer than the release's end
writes the snapshot.
"""

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict

from hq.domains.control_plane.observations.host import (
    CONTROLLER_UNIT,
    DRIFT_UNIT,
    GLANCE_UNIT,
    REHEARSAL_KIND,
    UNIT_KIND,
    RehearsedPage,
)
from hq.domains.control_plane.observations.portainer import CONTAINER_KIND, MEMORY_BASES, RUNTIME_KIND
from hq.platform.core.models import ReleaseSnapshot

from . import deploy_outcomes, readings
from .moments import moment
from .titled import Titled
lazy from . import hq_self
lazy from .cadence import ask
lazy from .facts import stored_snapshots
lazy from .hq_context.release_part import release
lazy from .unit_findings import unit_state

# The operation a snapshot's audit row names, written as HQ.
TAKE = Titled("hq.release.snapshot", "Kept what HQ read of a release")
# The units whose last run a release keeps.
TIMED = (CONTROLLER_UNIT, GLANCE_UNIT, DRIFT_UNIT)
# The release whose readings were last asked for, so they are asked once.
ASKED = "release-snapshot-asked"


class UnitRun(BaseModel):
    """One unit's last run as systemd last said it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    unit: str
    # systemd's word for how it ended ("success", "exit-code"); "" while it has not run.
    result: str = ""
    started_at: datetime | None = None
    ended_at: datetime | None = None

    @property
    def took(self) -> timedelta | None:
        if self.started_at is None or self.ended_at is None or self.ended_at < self.started_at:
            return None
        return self.ended_at - self.started_at


class ReleaseReadings(BaseModel):
    """HQ's own container and units, as read once its release went live, and
    what each page cost in its rehearsal on a copy of production's data."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Bytes, counted as ``memory_basis`` says (``MEMORY_BASES``' words); None unread.
    memory: int | None = None
    memory_basis: str = ""
    # Nanoseconds of CPU since the container started, and when it started.
    cpu_time: int | None = None
    started_at: datetime | None = None
    runtime_read_at: datetime | None = None
    # Every unit of HQ's that systemd held as failed.
    failed: tuple[str, ...] = ()
    units: tuple[UnitRun, ...] = ()
    units_read_at: datetime | None = None
    # Each page its rehearsal walked, or why the walk measured none; neither
    # where no rehearsal of this release was read.
    pages: tuple[RehearsedPage, ...] = ()
    pages_unread: str = ""

    @property
    def cpu_percent(self) -> float | None:
        """Its CPU since it started as a share of one core, to a tenth."""

        if self.cpu_time is None or self.started_at is None or self.runtime_read_at is None:
            return None
        seconds = (self.runtime_read_at - self.started_at).total_seconds()
        return round(self.cpu_time / (seconds * 1e9) * 100, 1) if seconds > 0 else None

    def run_of(self, unit: str) -> UnitRun | None:
        return next((run for run in self.units if run.unit == unit), None)


def loaded(snapshot: ReleaseSnapshot) -> ReleaseReadings:
    """A stored snapshot's readings, through their declared shape."""

    return ReleaseReadings.model_validate(snapshot.readings)


def keep(commit: str, taken: ReleaseReadings) -> ReleaseSnapshot:
    """Store what HQ read of the release ``commit``, once: a release already
    kept stays as it was first read. The caller binds who took it."""

    snapshot, _ = ReleaseSnapshot.objects.get_or_create(
        commit=commit, defaults={"readings": taken.model_dump(mode="json")}
    )
    return snapshot


def settle(*, repository_read: bool) -> ReleaseSnapshot | None:
    """Keep production's newest release's readings once HQ holds ones newer
    than the release, when it is live, runs here and has none kept; the
    snapshot kept, or None. ``repository_read`` is whether the sweep that
    calls this stored the repository's reading: only one asks for the readings."""

    container = hq_self.own_container()
    running = release(container)
    production = running.production
    commit = str(production.get("sha") or "")
    if not commit or production.get("state") != "success" or not deploy_outcomes.went_live(production):
        return None
    if ReleaseSnapshot.objects.filter(commit=commit).exists():
        return None
    since = moment(production.get("ended_at")) or moment(production.get("created_at"))
    units = next(iter(stored_snapshots().get(UNIT_KIND, ())), None)
    runtime = (container.runtime or {}) if container is not None else {}
    read_after = [moment(runtime.get("usage_read_at")), units.observed_at if units is not None else None]
    fresh = since is not None and all(at is not None and at > since for at in read_after)
    if fresh and running.runs_deployed:
        return keep(commit, _readings(runtime, units, _rehearsal(commit)))
    # Asked once a release: a container read older than the release still
    # names the image before it.
    if repository_read and _asked() != commit:
        ask(reads=(UNIT_KIND, RUNTIME_KIND, CONTAINER_KIND, REHEARSAL_KIND))
        readings.record(ASKED, commit)
    return None


def _asked() -> str:
    found = readings.stored(ASKED)
    return str(found.value) if found is not None else ""


def _rehearsal(commit: str) -> Mapping[str, Any]:
    """The rehearsal's record when it is the release ``commit``'s, else nothing."""

    for stored in stored_snapshots().get(REHEARSAL_KIND, ()):
        for record in stored.records or ():
            if isinstance(record, dict) and record.get("revision") == commit:
                return record
    return {}


def _readings(runtime: Mapping[str, Any], units: Any, rehearsal: Mapping[str, Any]) -> ReleaseReadings:
    states = [unit_state(record) for record in units.records or () if isinstance(record, dict)]
    basis = str(runtime.get("memory_usage_basis") or "")
    return ReleaseReadings(
        memory=int(runtime["memory_usage"]) if runtime.get("memory_usage") else None,
        memory_basis=MEMORY_BASES.get(basis, basis),
        cpu_time=int(runtime["cpu_time"]) if runtime.get("cpu_time") else None,
        started_at=moment(runtime.get("started_at")),
        runtime_read_at=moment(runtime.get("usage_read_at")),
        failed=tuple(sorted(str(state.unit) for state in states if state.failed and not state.foreign)),
        units=tuple(
            UnitRun(
                unit=str(state.unit),
                result=state.result,
                started_at=moment(state.started_at),
                ended_at=moment(state.ended_at),
            )
            for state in states
            if state.unit in TIMED
        ),
        units_read_at=units.observed_at,
        pages=tuple(RehearsedPage.model_validate(page) for page in rehearsal.get("pages") or ()),
        pages_unread=str(rehearsal.get("reason") or ""),
    )
