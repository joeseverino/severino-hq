"""The controller as HQ sees it: whether it is arriving, what it read, what waits for it.

Every reading on every page is the controller's, so "is it running, and what
did it last do" is a question HQ has to be able to answer in one place. All of
it is what HQ already holds: the heartbeat its bridge calls leave, the
readings it stored and when each was last attempted, the sweep policy, and
the work queued for it. Nothing here asks the controller anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from django.utils import timezone

from hq.domains.control_plane.models import OperationRequest, ProviderInventory

from .cadence import (
    ControllerStanding,
    ForcedRead,
    controller_standing,
    every_sweep,
    forced_reads,
    sweep_due,
)
from .entity_links import kind_label
from .moments import duration
from .resource_operations import ACTION_LABELS


@dataclass(frozen=True)
class KindReading:
    """One kind the sweep reads: when it was last tried, and what came of it."""

    kind: str
    label: str
    attempted_at: datetime
    observed_at: datetime
    records: int
    # "read", "not connected", or why the last attempt stored nothing new.
    result: str
    ok: bool


@dataclass(frozen=True)
class QueuedWork:
    resource: str
    action: str
    state: str
    created_at: datetime


@dataclass(frozen=True)
class ControllerPage:
    standing: ControllerStanding
    swept_at: datetime | None
    sweep_due: bool
    next_sweep_at: datetime | None
    # How often everything is read, in words.
    sweep_every: str
    # Of what every sweep reads, the reading tried longest ago: the one that
    # decides when the next sweep is due, and the one to look at when it is late.
    oldest: KindReading | None
    # The oldest reading is more than two sweeps old: sweeps are not landing.
    overdue: bool
    readings: tuple[KindReading, ...]
    failing: int
    queue: tuple[QueuedWork, ...]
    asked: tuple[ForcedRead, ...]


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
        attempted_at=max(row.observed_at, row.updated_at),
        observed_at=row.observed_at,
        records=len(row.records) if isinstance(row.records, list) else 0,
        result=result,
        ok=ok,
    )


def controller_page(now: datetime | None = None) -> ControllerPage:
    now = now or timezone.now()
    verdict: dict[str, Any] = sweep_due()
    readings = tuple(
        sorted(
            (_reading(row) for row in ProviderInventory.objects.all()),
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
            action=ACTION_LABELS.get(operation.action, operation.get_action_display()),
            state="Running" if operation.state == OperationRequest.State.CLAIMED else "Waiting",
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
        next_sweep_at=(
            None if due or age is None else now + timedelta(seconds=max(0, interval - int(age)))
        ),
        sweep_every=duration(every),
        oldest=oldest,
        overdue=oldest is not None and interval > 0 and now - oldest.attempted_at > 2 * every,
        readings=readings,
        failing=sum(1 for reading in readings if not reading.ok),
        queue=queue,
        asked=forced_reads(),
    )
