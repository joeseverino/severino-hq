"""How old a reading may be before it is out of date, per kind.

One answer for every surface. A reading is taken on a cadence its kind
declares: a controller sweep, a daily public registry lookup, a dashboard
refresh on view, an hourly SSH probe. It is *due* once one cadence has passed,
so a surface may ask for it again, and *stale* once the tolerance has passed
with no newer reading, so a surface says it is out of date. Nothing else
decides either.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from django.utils import timezone

CURRENT = "current"
DUE = "due"
STALE = "stale"
NEVER = "never"

# Cadence names for readings that are not observation kinds.
DASHBOARD_GLANCE = "dashboard.glance"
SSH_PROBE = "connection.ssh_probe"

# A dashboard reading costs a trip to a machine, so it is re-read when the
# dashboard is opened and older than this.
GLANCE_EVERY = timedelta(minutes=5)


@dataclass(frozen=True)
class Cadence:
    """How often a kind is read, and how many of those may pass unread."""

    every: timedelta
    missed: int = 1

    @property
    def stale_after(self) -> timedelta:
        return self.every * self.missed


def cadence(kind: str = "") -> Cadence:
    """The cadence one kind of reading is taken on."""

    from control_plane.observations import OBSERVATIONS

    from .cadence import slowest_sweep_interval, ssh_probe_interval

    if kind == DASHBOARD_GLANCE:
        return Cadence(GLANCE_EVERY, missed=3)
    if kind == SSH_PROBE:
        return Cadence(ssh_probe_interval(), missed=2)
    spec = OBSERVATIONS.get(kind)
    if spec is not None and spec.read_by == "hq":
        from .public_registry import read_every

        return Cadence(read_every(kind), missed=2)
    # A controller reads on the sweep, whose slowest interval is the idle one.
    return Cadence(slowest_sweep_interval())


def stale_after(kind: str = "") -> timedelta:
    return cadence(kind).stale_after


@dataclass(frozen=True)
class Freshness:
    state: str
    observed_at: datetime | None
    stale_after: timedelta

    @property
    def due(self) -> bool:
        return self.state in (DUE, STALE)

    @property
    def stale(self) -> bool:
        return self.state == STALE

    @property
    def label(self) -> str:
        return {
            CURRENT: "Current",
            DUE: "Current",
            STALE: "Out of date",
            NEVER: "No reading yet",
        }[self.state]


def freshness(kind: str, observed_at: datetime | None, now: datetime | None = None) -> Freshness:
    """Whether a reading of ``kind`` taken at ``observed_at`` is current."""

    found = cadence(kind)
    if observed_at is None:
        return Freshness(NEVER, None, found.stale_after)
    age = (now or timezone.now()) - observed_at
    if age >= found.stale_after:
        state = STALE
    elif age >= found.every:
        state = DUE
    else:
        state = CURRENT
    return Freshness(state, observed_at, found.stale_after)
