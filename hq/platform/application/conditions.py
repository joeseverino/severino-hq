"""When a condition began to hold.

A resource's conditions are replaced by every report, so on their own they say
what holds now and never since when. Drift is the case that needs it: "changed
outside HQ" is only half an answer without the moment it was first seen, which
is what lets a finding name what happened near then.
"""

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from django.utils import timezone

from .timestamps import moment


def stamped(
    previous: Iterable[Any], conditions: Iterable[Mapping[str, Any]], now: datetime | None = None
) -> list[dict[str, Any]]:
    """``conditions``, each carrying ``since``: kept from the previous report
    while the same condition holds the same way, else now."""

    earlier = {
        (item.get("type"), item.get("status")): item.get("since")
        for item in previous or ()
        if isinstance(item, Mapping) and item.get("since")
    }
    stamp = (now or timezone.now()).isoformat()
    return [
        {**item, "since": earlier.get((item.get("type"), item.get("status"))) or item.get("since") or stamp}
        for item in conditions
    ]


def held_since(conditions: Iterable[Any], condition_type: str) -> datetime | None:
    """When the named condition began to hold, or None when it does not."""

    for item in conditions or ():
        if isinstance(item, Mapping) and item.get("type") == condition_type and item.get("status") is True:
            return moment(item.get("since"))
    return None
