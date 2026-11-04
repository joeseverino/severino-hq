"""Which GitHub repositories the controller may carry, and which are due a full read.

The controller asks GitHub once a sweep whether each covered repository
changed (``controller/providers/github_probe.go``), and reads in full only one
whose answer moved from the fingerprint HQ holds, or whose full read HQ's clock
makes due. HQ owns that state: the fingerprint rides in each record, and the
clock is ``observations.github.FULL_READ_EVERY``.
"""

import hashlib
from datetime import datetime, timedelta
from typing import Any

from django.utils import timezone

from hq.domains.control_plane.models import ProviderInventory
from hq.domains.control_plane.observations.github import FULL_READ_EVERY, REPOSITORY_KIND

lazy from .timestamps import moment


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
    its full read is due."""

    now = now or timezone.now()
    snapshot = ProviderInventory.objects.filter(kind=REPOSITORY_KIND).values_list("records", flat=True).first()
    held = []
    for record in snapshot or ():
        name = str(record.get("repository", "") or "")
        if not name:
            continue
        read_at = moment(record.get("read_at"))
        held.append(
            {
                "repository": name,
                "fingerprint": str(record.get("fingerprint", "") or ""),
                "due": due(name, read_at, now),
            }
        )
    return {"held": held}
