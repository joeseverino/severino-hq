"""How many submissions are unread, as D1 last said.

Submissions themselves stay in D1; only the count is kept here.
"""

from __future__ import annotations

from datetime import timedelta

from application import readings

from . import d1

UNREAD = d1.UNREAD
REFRESH_AFTER = timedelta(minutes=5)


def unread() -> tuple[int, str]:
    """The stored count and whether the last read reached D1. No network."""

    reading = readings.stored(UNREAD)
    if reading is None:
        return 0, "unknown"
    return int(reading.value.get("count", 0)), str(reading.value.get("status", "ok"))


def refresh() -> None:
    """Read D1 again if the stored count is older than a few minutes."""

    def fetch():
        try:
            return {"count": d1.get_unread_count(), "status": "ok"}
        except d1.D1Error:
            return {"count": unread()[0], "status": "unavailable"}

    readings.refresh(UNREAD, fetch, older_than=REFRESH_AFTER)


def recent(limit: int = 4) -> list[dict]:
    """The latest submissions, read live. The unread count comes with them."""

    rows, count = d1.get_dashboard_state(limit=limit)
    readings.record(UNREAD, {"count": count, "status": "ok"})
    return rows
