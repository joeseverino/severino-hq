"""One timeline across what HQ did and what its readings saw change.

Each connection dates its own events in its own place: HQ's audit log holds
what anyone did through HQ and when a reading's records changed between two
sweeps (``inventory._record_change``), GitHub its deployments, Docker when
each container last started. Laid on one line, they answer the question drift
raises: something changed outside HQ, so what else happened then?

Read only from what is already stored; nothing here polls.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from django.utils import timezone

from control_plane.observations.github import REPOSITORY_KIND
from control_plane.observations.portainer import RUNTIME_KIND

from .facts import inventory_records
from .timestamps import moment
from .ui import ListRow, ago_short

# How far either side of a moment counts as near it.
NEAR = timedelta(hours=6)
# The actions a person or HQ took that change something. Views, sign-ins and
# routine connection probes are left out: a timeline of them is noise.
_CHANGES = ("created", "updated", "deleted", "settings_changed", "failed", "imported", "uploaded")


@dataclass(frozen=True)
class Moment:
    at: datetime
    # Where it was recorded: "HQ", "Reading", "Deploy", "Container".
    source: str
    title: str
    detail: str = ""
    url: str = ""

    @property
    def row(self) -> ListRow:
        return ListRow(
            title=self.title,
            detail=" · ".join(part for part in (self.source, self.detail) if part),
            meta=ago_short(self.at),
            url=self.url,
            external=self.url.startswith("https://"),
        )


def moments(*, since: datetime, until: datetime | None = None, limit: int = 200) -> tuple[Moment, ...]:
    """Everything dated between ``since`` and ``until``, newest first."""

    until = until or timezone.now()
    found = [*_recorded(since, until), *_deploys(since, until), *_starts(since, until)]
    return tuple(sorted(found, key=lambda item: item.at, reverse=True)[:limit])


def near(at: datetime, *, window: timedelta = NEAR, limit: int = 5) -> tuple[Moment, ...]:
    """What happened close to ``at``, closest first."""

    around = moments(since=at - window, until=at + window)
    return tuple(sorted(around, key=lambda item: abs(item.at - at))[:limit])


def _recorded(since: datetime, until: datetime) -> list[Moment]:
    from core.models import AuditLog

    from .inventory import READING_AUDIT_TYPE

    entries = (
        AuditLog.objects.filter(created_at__gte=since, created_at__lte=until)
        .filter(action__in=_CHANGES)
        .select_related("user")
        .order_by("-created_at")[:500]
    )
    found = [
        Moment(
            entry.created_at,
            "HQ",
            f"{entry.get_action_display()} {entry.object_repr or entry.type_label}".strip(),
            detail=entry.actor_label,
        )
        for entry in entries
    ]
    readings = AuditLog.objects.filter(
        created_at__gte=since, created_at__lte=until,
        action=AuditLog.Action.OBSERVED, object_type=READING_AUDIT_TYPE,
    ).order_by("-created_at")[:500]
    found += [Moment(entry.created_at, "Reading", entry.message) for entry in readings]
    return found


def _between(stamp: object, since: datetime, until: datetime) -> datetime | None:
    at = moment(stamp)
    return at if at is not None and since <= at <= until else None


def _deploys(since: datetime, until: datetime) -> list[Moment]:
    found = []
    for _snapshot, record in inventory_records(REPOSITORY_KIND):
        for deployment in record.get("deployments") or ():
            at = _between(deployment.get("created_at"), since, until)
            if at is None:
                continue
            sha = str(deployment.get("sha") or "")[:7]
            found.append(
                Moment(
                    at,
                    "Deploy",
                    f"Deployed {sha} to {deployment.get('environment') or 'an environment'}".replace("  ", " "),
                    detail=str(record.get("repository") or ""),
                    url=str(deployment.get("url") or ""),
                )
            )
    return found


def _starts(since: datetime, until: datetime) -> list[Moment]:
    found = []
    for _snapshot, record in inventory_records(RUNTIME_KIND):
        at = _between(record.get("started_at"), since, until)
        if at is not None:
            found.append(
                Moment(
                    at,
                    "Container",
                    f"{record.get('container')} started on {record.get('host')}",
                    detail="created, recreated or restarted",
                )
            )
    return found
