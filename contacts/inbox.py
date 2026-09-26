"""The unread count and the latest submissions, as D1 last said.

Submissions stay in D1; HQ keeps the unread count and, for the newest
submissions, the id, date, submitter name, status and country. The dashboard,
the header count and the search page read that stored copy. D1 is read by
`refresh`, which the `refresh_contacts_inbox` timer runs every few minutes and
a write to D1 runs after it changes a submission; no GET reads D1 for these.
"""

from __future__ import annotations

from datetime import timedelta

from application import readings

from . import d1

UNREAD = d1.UNREAD
REFRESH_AFTER = timedelta(hours=1)
RECENT_LIMIT = 4
# How many of the newest submissions are kept for search.
KEPT_LIMIT = 500


def unread() -> tuple[int, str]:
    """The stored count and whether the last read reached D1. No network."""

    reading = readings.stored(UNREAD)
    if reading is None:
        return 0, "unknown"
    return int(reading.value.get("count", 0)), str(reading.value.get("status", "ok"))


def _rows() -> list[dict]:
    reading = readings.stored(UNREAD)
    return list(reading.value.get("rows", [])) if reading else []


def recent() -> list[dict]:
    """The latest submissions, as stored by the last refresh. No network."""

    return _rows()[:RECENT_LIMIT]


def search(q: str, limit: int) -> list[dict]:
    """Stored submissions whose submitter name contains ``q``. No network.

    Email and message text stay in D1, so they are searched on the contacts
    page, which reads D1.
    """

    wanted = q.strip().casefold()
    if not wanted:
        return []
    return [row for row in _rows() if wanted in str(row.get("name", "")).casefold()][
        :limit
    ]


def refresh(*, force: bool = False) -> None:
    """Read D1 again if forced or the stored state is older than a few minutes.

    One request answers the count and the rows, so they cannot disagree. An
    outage keeps the last rows and count and says so.
    """

    def fetch():
        try:
            rows, count = d1.get_dashboard_state(limit=KEPT_LIMIT)
            return {"count": count, "rows": rows, "status": "ok"}
        except d1.D1Error:
            return {"count": unread()[0], "rows": _rows(), "status": "unavailable"}

    readings.refresh(UNREAD, fetch, older_than=timedelta(0) if force else REFRESH_AFTER)
