"""When the controller should sweep, and how it hears that there is work.

Applying a queued operation and sweeping what the providers hold run on
different clocks. The first should happen the moment it is asked for; the second
describes records that change monthly, and asking more often buys nothing and
costs a provider call each time. Driven by one timer, only one of them can be
right.

Both halves keep the trust direction that makes HQ safe. The web process holds
no provider credential, so a compromise of it cannot reach a provider or open a
shell anywhere. Trust runs one way: the privileged controller reaches into HQ
and pulls work, and HQ never reaches out.

Cadence is policy, and policy belongs where the observations are. HQ records
when each provider was last swept, so HQ answers "is one due?" and the controller
executes -- the same split as claim, schedule and report.

The doorbell is a file HQ touches when it queues something. It carries no
authority, no credentials and no data: it cannot say what to do, only that
something changed. A unit on the host watches it and starts the controller,
which pulls the work through the path it always used. Forged, deleted or
replayed, the worst it can cause is a controller run that finds nothing to do.
"""


from __future__ import annotations

from datetime import timedelta
from pathlib import Path
import os
import tempfile
import time

from django.conf import settings
from django.utils import timezone

from control_plane.models import ProviderInventory


def _seconds(name: str, fallback: int) -> int:
    try:
        return max(0, int(getattr(settings, name, fallback)))
    except (TypeError, ValueError):
        return fallback


def _path(name: str, filename: str) -> Path:
    configured = str(getattr(settings, name, "") or "").strip()
    if configured:
        return Path(configured)
    # Beside the database, which is the volume both sides share. Anywhere else
    # is visible in the container and invisible from the host, which is the one
    # place the watcher runs.
    return Path(settings.DATABASES["default"]["NAME"]).parent / filename


def note_activity() -> None:
    """Record that somebody is using HQ, cheaply enough to do on every request.

    A file's mtime rather than a column: this runs on every page load, and the
    fact is too coarse to be worth a database write. Rewritten at most once per
    interval, so the common case is a stat and nothing else.

    Best effort. Nothing an operator asked for may fail because a hint about
    scheduling could not be written.
    """

    marker = _path("SEVERINO_ACTIVITY_MARKER", "hq-activity")
    throttle = _seconds("SEVERINO_ACTIVITY_THROTTLE_SECONDS", 60)
    try:
        if marker.exists() and time.time() - marker.stat().st_mtime < throttle:
            return
        _touch(marker)
    except OSError:
        return


def recently_used(now: float | None = None) -> bool:
    """Whether HQ has been used inside the active window."""

    marker = _path("SEVERINO_ACTIVITY_MARKER", "hq-activity")
    window = _seconds("SEVERINO_ACTIVE_WINDOW_SECONDS", 900)
    try:
        age = (time.time() if now is None else now) - marker.stat().st_mtime
    except OSError:
        return False
    return age <= window


def sweep_interval() -> timedelta:
    """How stale a sweep may be before another is worth the calls.

    Adaptive, because the answer depends on whether anybody is looking. In use,
    a minute-old view of the estate is the point of having one. Idle, twelve
    hours of staleness costs nothing and saves the calls.
    """

    active = _seconds("SEVERINO_SWEEP_INTERVAL_ACTIVE_SECONDS", 60)
    idle = _seconds("SEVERINO_SWEEP_INTERVAL_IDLE_SECONDS", 12 * 60 * 60)
    return timedelta(seconds=active if recently_used() else idle)


def sweep_due() -> dict[str, object]:
    """Whether the controller should sweep now, and why.

    The oldest sweep decides. The reason rides along because a controller that
    stopped sweeping and one that was told not to look identical from outside,
    and only one of them is a fault.
    """

    interval = sweep_interval()
    oldest = (
        ProviderInventory.objects.order_by("observed_at")
        .values_list("observed_at", flat=True)
        .first()
    )
    if oldest is None:
        return {
            "ok": True,
            "due": True,
            "reason": "Nothing has been swept yet.",
            "interval_seconds": int(interval.total_seconds()),
        }
    age = timezone.now() - oldest
    due = age >= interval
    return {
        "ok": True,
        "due": due,
        "reason": (
            f"Oldest sweep is {int(age.total_seconds())}s old; "
            f"{'due' if due else 'not due'} at "
            f"{int(interval.total_seconds())}s."
        ),
        "interval_seconds": int(interval.total_seconds()),
        "age_seconds": int(age.total_seconds()),
    }


def ring_doorbell() -> None:
    """Tell the host something is queued, without telling it anything else.

    Best effort, and rung only after the operation is stored. A doorbell able to
    fail the write it announces would make queueing depend on the host
    filesystem, which is the opposite of what it is for.
    """

    try:
        _touch(_path("SEVERINO_CONTROLLER_DOORBELL", "controller-doorbell"))
    except OSError:
        return


def _touch(path: Path) -> None:
    """Replace a marker, so a watcher sees an event it cannot coalesce away.

    `Path.touch` on an existing file is a bare utime, which inotify may fold
    into nothing. A replacement is a create, and `os.replace` is atomic, so a
    reader never catches the file absent.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=".marker-")
    os.close(handle)
    os.replace(temporary, path)
