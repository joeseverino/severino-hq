"""The controller as HQ sees it: whether it is arriving, what it read, what waits for it.

Every reading on every page is the controller's, so "is it running, and what
did it last do" is a question HQ has to be able to answer in one place. All of
it is what HQ already holds: the heartbeat its bridge calls leave, the
readings it stored and when each was last attempted, the sweep policy, and
the work queued for it. Nothing here asks the controller anything.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from django.utils import timezone
from django.utils.text import get_text_list

from hq.domains.control_plane.models import OperationRequest, ProviderInventory, attempted_at

from .cadence import WorkerStanding, controller_standing, every_sweep, sweep_due, swept
from .entity_links import kind_label
from .freshness import Freshness, freshness
from .moments import duration
from .read_requests import ForcedRead, forced_reads
from .tone import Tone
lazy from .facts import every_snapshot
lazy from .inbound_events import covers


@dataclass(frozen=True, slots=True)
class KindReading:
    """One kind the sweep reads: when it was last tried, and what came of it."""

    kind: str
    label: str
    attempted_at: datetime
    # When a read last answered, and whether that is still current.
    reading: Freshness
    records: int
    # "read", "not connected", or why the last attempt stored nothing new.
    result: str
    ok: bool


@dataclass(frozen=True, slots=True)
class QueuedWork:
    resource: str
    action: str
    state: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ControllerPage:
    standing: WorkerStanding
    swept_at: datetime | None
    sweep_due: bool
    next_sweep_at: datetime | None
    # How often everything is read, in words, then each reading on a clock of
    # its own with its clock.
    sweep_every: str
    # Of what every sweep reads, the reading tried longest ago: the one that
    # decides when the next sweep is due, and the one to look at when it is late.
    oldest: KindReading | None
    # The oldest reading is more than two sweeps old: sweeps are not landing.
    overdue: bool
    readings: tuple[KindReading, ...]
    queue: tuple[QueuedWork, ...]
    asked: tuple[ForcedRead, ...]

    @property
    def overdue_tone(self) -> str:
        return Tone.SERIOUS if self.overdue else ""

    @property
    def queue_tone(self) -> str:
        return Tone.ATTENTION if self.queue else ""


def _reading(row: ProviderInventory) -> KindReading:
    if not row.connected:
        result, ok = "Not connected", True
    elif row.reachable and not row.error:
        result, ok = "Read", True
    else:
        result, ok = (row.error or "Could not be read")[:200], False
    return KindReading(
        kind=row.kind,
        label=kind_label(row.kind),
        attempted_at=attempted_at(row.observed_at, row.updated_at),
        reading=freshness(row.kind, row.observed_at),
        records=len(row.records) if isinstance(row.records, list) else 0,
        result=result,
        ok=ok,
    )


def _own_clocks(readings: tuple[KindReading, ...]) -> str:
    """Each reading the sweep reads on a clock of its own, in its freshness's
    words ("GitHub profile read every 6 hours"), and "and on each event" for
    one events keep current."""

    return get_text_list(
        [
            f"{reading.label}{reading.reading.cadence.removeprefix(';')}{' and on each event' * covers(reading.kind)}"
            for reading in readings
            if swept(reading.kind) and not every_sweep(reading.kind)
        ],
        "and",
    )


def controller_page(now: datetime | None = None) -> ControllerPage:
    now = now or timezone.now()
    verdict: dict[str, Any] = sweep_due()
    readings = tuple(
        sorted(
            (_reading(row) for row in every_snapshot()),
            # What failed first, then the kinds in a stable order.
            key=lambda reading: (reading.ok, reading.label.lower()),
        )
    )
    age = verdict.get("age_seconds")
    interval = int(verdict.get("interval_seconds") or 0)
    due = bool(verdict.get("due"))
    queue = tuple(
        QueuedWork(
            resource=operation.resource.key,
            action=operation.get_action_display(),
            state=operation.get_state_display(),
            created_at=operation.created_at,
        )
        for operation in OperationRequest.objects.filter(
            state__in=(OperationRequest.State.QUEUED, OperationRequest.State.CLAIMED)
        )
        .select_related("resource")
        .order_by("created_at")[:50]
    )
    every = timedelta(seconds=interval)
    oldest = min(
        (reading for reading in readings if every_sweep(reading.kind)),
        key=lambda reading: reading.attempted_at,
        default=None,
    )
    return ControllerPage(
        standing=controller_standing(now),
        swept_at=max((reading.attempted_at for reading in readings), default=None),
        sweep_due=due,
        next_sweep_at=(None if due or age is None else now + timedelta(seconds=max(0, interval - int(age)))),
        sweep_every="; ".join(filter(None, (duration(every), _own_clocks(readings)))),
        oldest=oldest,
        overdue=oldest is not None and interval > 0 and now - oldest.attempted_at > 2 * every,
        readings=readings,
        queue=queue,
        asked=forced_reads(),
    )
