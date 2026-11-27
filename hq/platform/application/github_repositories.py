"""Which GitHub repositories the controller may carry, and which are due a full read.

The controller asks GitHub once a sweep whether each covered repository
changed (``controller/providers/github_probe.go``), and reads in full only one
whose answer moved from the fingerprint HQ holds, or whose full read HQ's clock
or an alert's event makes due. HQ owns that state: the fingerprint rides in each record, and the
clock is ``observations.github.FULL_READ_EVERY``.
"""

import hashlib
from datetime import datetime, timedelta
from typing import Any

from django.db.models import Max
from django.utils import timezone

from hq.domains.control_plane.models import InboundEvent, ProviderInventory
from hq.domains.control_plane.observations import OBSERVATIONS
from hq.domains.control_plane.observations.github import FULL_READ_EVERY, PROVIDER, REPOSITORY_KIND, UNPROBED_EVENTS

lazy from .moments import moment


def _phase(repository: str, period: int) -> int:
    """Where in each clock period, in seconds, a repository falls due: the
    same every sweep, so the covered repositories come due across different
    sweeps rather than all in one."""

    return int.from_bytes(hashlib.sha256(repository.encode()).digest()[:8]) % period


def due(repository: str, read_at: datetime | None, now: datetime, every: timedelta = FULL_READ_EVERY) -> bool:
    """Whether a repository read in full at ``read_at`` is due again: once its
    own point in the clock period has passed since then, or never read."""

    if read_at is None:
        return True
    period = int(every.total_seconds())
    phase = _phase(repository, period)
    return (int(now.timestamp()) - phase) // period != (int(read_at.timestamp()) - phase) // period


def plan(now: datetime | None = None) -> dict[str, Any]:
    """Each repository HQ holds a record of, with its fingerprint and whether
    its full read is due, read longest ago first, the order the controller
    reads due ones in.

    A held record the reading's schema refuses was stored under an older
    contract, so it is due at once: carried, it would stand as current. So is
    one GitHub told HQ of a change to since its read that the change query
    cannot see (``UNPROBED_EVENTS``), an alert.
    """

    now = now or timezone.now()
    reading = OBSERVATIONS[REPOSITORY_KIND]
    snapshot = ProviderInventory.objects.filter(kind=REPOSITORY_KIND).values_list("records", flat=True).first()
    # Each event by its name, with or without the action after a dot.
    unprobed = rf"^({'|'.join(UNPROBED_EVENTS)})(\.|$)"
    told = dict(
        InboundEvent.objects.filter(provider=PROVIDER, event__regex=unprobed)
        .values("subject")
        .annotate(last=Max("applied_at"))
        .values_list("subject", "last")
    )
    held = []
    for record in snapshot or ():
        name = str(record.get("repository", "") or "")
        if not name:
            continue
        read_at = moment(record.get("read_at"))
        told_since = read_at is not None and told.get(name, read_at) > read_at
        held.append(
            {
                "repository": name,
                "fingerprint": str(record.get("fingerprint", "") or ""),
                "due": due(name, read_at, now) or bool(reading.clean([record])[1]) or told_since,
                "read_at": read_at or datetime.min.replace(tzinfo=now.tzinfo),
            }
        )
    held.sort(key=lambda entry: entry.pop("read_at"))
    return {"held": held}
