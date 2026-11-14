"""When a condition began to hold.

A resource's conditions are replaced by every report, so on their own they say
what holds now and never since when. Drift is the case that needs it: "changed
outside HQ" is only half an answer without the moment it was first seen, which
is what lets a finding name what happened near then.
"""

import json
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from django.utils import timezone

from .timestamps import moment


def _condition_types() -> dict[str, str]:
    """The bridge's condition types by their generated names, from the schema
    the controller's Go constants are generated from."""

    path = Path(__file__).resolve().parents[2] / "domains" / "control_plane" / "bridge_base.json"
    base = json.loads(path.read_text(encoding="utf-8"))
    declared = base["components"]["schemas"]["ConditionType"]
    return dict(zip(declared["x-enum-varnames"], declared["enum"], strict=True))


_TYPES = _condition_types()
READY = _TYPES["ConditionReady"]
DRIFTED = _TYPES["ConditionDrifted"]
DEGRADED = _TYPES["ConditionDegraded"]
# What says something is wrong with a resource, whatever its own state.
PROBLEMS = (DRIFTED, DEGRADED)


def holding(conditions: Iterable[Any] | None, *types: str) -> list[dict[str, Any]]:
    """The conditions that hold now, of ``types`` when any are named."""

    return [
        item
        for item in conditions or ()
        if isinstance(item, dict) and item.get("status") is True and (not types or item.get("type") in types)
    ]


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

    found = holding(conditions, condition_type)
    return moment(found[0].get("since")) if found else None
