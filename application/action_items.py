"""Which action items a person has already seen.

An item is read until it changes. Its fingerprint covers what it says and how
many it stands for, so a count that grows or a detail that moves brings it back.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from django.db import transaction

from core.models import ActionItemRead


def fingerprint(item: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            [item["source_id"], item["label"], item["detail"], item["count"], item["status"]]
        ).encode()
    ).hexdigest()


def with_read_state(items: list[dict[str, Any]], user) -> list[dict[str, Any]]:
    """The items, each with its ``key`` and whether ``user`` has read it. One query."""

    keyed = [(fingerprint(item), item) for item in items]
    read = set(
        ActionItemRead.objects.filter(user=user, key__in=[key for key, _ in keyed])
        .values_list("key", flat=True)
    )
    return [{**item, "key": key, "read": key in read} for key, item in keyed]


def unread_count(items: list[dict[str, Any]], user) -> int:
    return sum(item["count"] for item in with_read_state(items, user) if not item["read"])


def mark(user, keys, *, read: bool, current: list[dict[str, Any]]) -> None:
    """Mark ``keys`` read or unread. Only keys of items that exist now are kept,
    and read marks for items that have gone are forgotten.
    """

    live = {fingerprint(item) for item in current}
    wanted = set(keys) & live
    with transaction.atomic():
        if read:
            ActionItemRead.objects.bulk_create(
                [ActionItemRead(user=user, key=key) for key in wanted], ignore_conflicts=True
            )
        else:
            ActionItemRead.objects.filter(user=user, key__in=wanted).delete()
        ActionItemRead.objects.filter(user=user).exclude(key__in=live).delete()
