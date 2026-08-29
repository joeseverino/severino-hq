"""Pure analytics boundaries shared by HQ and its provider adapter."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone


# Cloudflare's widest accepted query is 13w2d. HQ backfills a quarter on first
# sight, then keeps the last few completed days warm so late provider updates
# restate rather than strand a day. These are transport-neutral policy: HQ
# plans with them and the provider adapter validates against the same values.
MAX_QUERY_DAYS = 93
BACKFILL_DAYS = 90
REFRESH_DAYS = 3


def completed_window(days: int, *, today: date | None = None) -> tuple[date, date]:
    """Return the most recent ``days`` whole UTC dates, never a partial today."""

    current = today or datetime.now(timezone.utc).date()
    end = current - timedelta(days=1)
    return end - timedelta(days=max(days, 1) - 1), end
