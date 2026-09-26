"""Which action items a person has already seen.

An item is identified by what it is about (its source and key), and read state
records the revision that was seen: its status and how many it stands for. It
comes back unread when it gets worse or its count changes, never because its
wording moved, and marking one item never touches another.
"""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from typing import Any

from django.db import transaction
from django.utils import timezone

from core.models import ActionItemRead

# How long a read mark outlives its item. Long enough that a finding which
# clears and returns within a sweep or two is still recognised as seen.
FORGET_AFTER = timedelta(days=30)


def item_key(source_id: str, item: Any) -> str:
    about = item.key or f"{item.eyebrow}:{item.title}"
    return f"{source_id}:{about}"[:200]


def item_revision(item: Any) -> str:
    return hashlib.sha256(
        json.dumps([item.status, item.magnitude or 1]).encode()
    ).hexdigest()[:16]


def with_read_state(items: list[dict[str, Any]], user) -> list[dict[str, Any]]:
    """The items, each with whether ``user`` has read this revision of it. One query."""

    seen = dict(
        ActionItemRead.objects.filter(user=user, key__in=[item["key"] for item in items])
        .values_list("key", "revision")
    )
    return [{**item, "read": seen.get(item["key"]) == item["revision"]} for item in items]


def unread_count(items: list[dict[str, Any]], user) -> int:
    return sum(item["count"] for item in with_read_state(items, user) if not item["read"])


def mark(user, keys, *, read: bool, current: list[dict[str, Any]]) -> None:
    """Mark the named items read (at their current revision) or unread.

    Only items that exist now can be marked. Marks for items that have gone are
    kept for a while, then forgotten.
    """

    live = {item["key"]: item["revision"] for item in current}
    wanted = [key for key in dict.fromkeys(keys) if key in live]
    now = timezone.now()
    with transaction.atomic():
        if read:
            for key in wanted:
                ActionItemRead.objects.update_or_create(
                    user=user, key=key, defaults={"revision": live[key], "read_at": now}
                )
        else:
            ActionItemRead.objects.filter(user=user, key__in=wanted).delete()
        ActionItemRead.objects.filter(user=user, read_at__lt=now - FORGET_AFTER).exclude(
            key__in=live
        ).delete()


def filter_items(
    items: list[dict[str, Any]], *, query: str = "", status: str = "", source: str = ""
) -> list[dict[str, Any]]:
    """The items matching an exact status and source, and words in their text."""

    query = query.strip().casefold()
    status = status.strip()
    source = source.strip()
    return [
        item
        for item in items
        if (not status or item["status"] == status)
        and (not source or item["source_id"] == source)
        and (
            not query
            or query
            in " ".join((item["source"], item["label"], item["detail"], item["action"])).casefold()
        )
    ]
