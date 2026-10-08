"""Days until something runs out, and when its renewal opens. One copy.

Every surface that says "N days" reads ``days_until``, so a dashboard card and
a domain page never disagree about the same certificate by a day.
"""

import math
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from .derivations import holds_until, present
from .timestamps import moment

DEFAULT_RENEWAL_WINDOW_DAYS = 30


def days_until(when: datetime, now: datetime | None = None) -> int:
    """Days left to the nearest day; negative only once past.

    Nearest rather than truncated, so a moment set N days ahead reads N a
    second later, and never zero or less while time remains to act.
    """

    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    left = (when - (now or present())).total_seconds() / 86400
    if left > 0:
        days = max(1, math.floor(left + 0.5))
        # N holds while N - 0.5 <= left; 1 holds until the moment itself.
        holds_until(when - timedelta(days=days - 0.5) if days > 1 else when)
        return days
    days = math.floor(left)
    # Past by k days, and by k + 1 a day after that.
    holds_until(when + timedelta(days=-days))
    return days


def renewal_window(spec: Mapping[str, Any]) -> int:
    """The days before expiry a declaration renews at."""

    try:
        return int(spec.get("renewal_window_days") or DEFAULT_RENEWAL_WINDOW_DAYS)
    except (TypeError, ValueError):
        return DEFAULT_RENEWAL_WINDOW_DAYS


def renewal_opens_at(expires: datetime, window_days: int) -> datetime:
    return expires - timedelta(days=window_days)


def certificate_expiry(status: Mapping[str, Any] | None) -> datetime | None:
    """The reported ``not_after``, parsed, or None when absent or unreadable."""

    return moment(str((status or {}).get("not_after") or ""))
