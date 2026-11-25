"""Canonical, non-mutating HQ projections shared by delivery adapters."""

from typing import Literal, TypedDict

from django.utils import timezone

from . import records
lazy from .projection import iso


class SystemHealth(TypedDict):
    status: Literal["ok"]
    checked_at: str
    database: Literal["ok"]
    # Each record domain's own count.
    counts: dict[str, int]


def system_health() -> SystemHealth:
    """Prove database access and return only non-sensitive record counts."""

    return {
        "status": "ok",
        "checked_at": iso(timezone.now()),
        "database": "ok",
        # Each record domain's own count, read off its declaration.
        "counts": records.counts(),
    }
