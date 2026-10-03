"""Which action items a person has set aside, and the queue under its domains.

An action item is open until its owner stops raising it. What a person can do
about one they are not going to act on now is set it aside: it leaves their
queue and their counts, and waits below. An item is identified by what it is
about (its source and key), and what is recorded is the revision that was set
aside: its status and how many it stands for. It comes back on its own when it
gets worse or its count changes, never because its wording moved, and setting
one aside never touches another.
"""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from typing import Any

from django.db import transaction
from django.utils import timezone

from core.models import ActionItemRead

# How long a set-aside mark outlives its item. Long enough that a finding which
# clears and returns within a sweep or two is still recognised as set aside.
FORGET_AFTER = timedelta(days=30)


def item_key(source_id: str, item: Any) -> str:
    about = item.key or f"{item.eyebrow}:{item.title}"
    return f"{source_id}:{about}"[:200]


def item_revision(item: Any) -> str:
    """What makes a dismissed item come back.

    Something to resolve returns when it gets worse or stands for more. A
    notice has nothing to get worse: it returns when it reports something else.
    """

    if getattr(item, "notice", False):
        seen = ["notice", item.body]
    else:
        seen = [item.status, item.magnitude or 1]
    return hashlib.sha256(json.dumps(seen).encode()).hexdigest()[:16]


def with_aside_state(items: list[dict[str, Any]], user) -> list[dict[str, Any]]:
    """The items, each with whether ``user`` set this revision of it aside. One query."""

    aside = dict(
        ActionItemRead.objects.filter(user=user, key__in=[item["key"] for item in items])
        .values_list("key", "revision")
    )
    return [{**item, "aside": aside.get(item["key"]) == item["revision"]} for item in items]


def waiting_count(items: list[dict[str, Any]], user) -> int:
    """How many items wait on ``user``: items, not the numbers they carry.

    An item's ``count`` is what it stands for (four containers failing one
    check) and is shown on the item, not summed into the badge.
    """

    return count_waiting(with_aside_state(items, user))


def count_waiting(items: list[dict[str, Any]]) -> int:
    """How many of ``items``, already carrying their state, are not set aside."""

    return sum(1 for item in items if not item["aside"])


def split_waiting(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """What waits, as what needs doing and what is only being told.

    The one place that draws the line, from what each item says of itself, so
    the queue page, the dashboard and any count agree on it.
    """

    waiting = [item for item in items if not item["aside"]]
    return (
        [item for item in waiting if not item.get("notice")],
        [item for item in waiting if item.get("notice")],
    )


def set_aside(user, keys, *, aside: bool, current: list[dict[str, Any]]) -> None:
    """Set the named items aside (at their current revision), or bring them back.

    Only items that exist now can be set aside. Marks for items that have gone
    are kept for a while, then forgotten.
    """

    live = {item["key"]: item["revision"] for item in current}
    wanted = [key for key in dict.fromkeys(keys) if key in live]
    now = timezone.now()
    with transaction.atomic():
        if aside:
            for key in wanted:
                ActionItemRead.objects.update_or_create(
                    user=user, key=key, defaults={"revision": live[key], "read_at": now}
                )
        else:
            ActionItemRead.objects.filter(user=user, key__in=wanted).delete()
        ActionItemRead.objects.filter(user=user, read_at__lt=now - FORGET_AFTER).exclude(
            key__in=live
        ).delete()


def by_source(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The queue under the domain that raised each item, families folded.

    Derived from what every item already carries, so a domain that starts
    raising items has its section, and a family its fold, without anybody
    listing either. The queue's own order is kept: a domain appears where its
    first (and so its worst) item does, and so does a family within it. A
    family of one is that item alone.
    """

    groups: dict[str, dict[str, Any]] = {}
    for item in items:
        group = groups.setdefault(
            item["source_id"],
            {"id": item["source_id"], "label": item["source"], "items": [], "entries": {}},
        )
        group["items"].append(item)
        family = item.get("family", "")
        entry = group["entries"].setdefault(
            ("family", family) if family else ("item", item["key"]),
            {"family": family, "id": family_id(item) if family else "", "items": []},
        )
        entry["items"].append(item)
    return [
        {
            **group,
            "entries": [
                {**entry, "folded": len(entry["items"]) > 1, "status": entry["items"][0]["status"]}
                for entry in group["entries"].values()
            ],
        }
        for group in groups.values()
    ]


def family_id(item: dict[str, Any]) -> str:
    """A family, named apart from another source's family of the same name."""

    return f'{item["source_id"]}:{item.get("family", "")}'


def of_family(items: list[dict[str, Any]], named: str) -> list[dict[str, Any]]:
    """The items of the family ``by_source`` gave that id."""

    return [item for item in items if item.get("family") and family_id(item) == named]


def row_value(item: dict[str, Any]) -> str:
    """What a row's own button names: the revision it was drawn with, then its key.

    Carrying the revision is what lets one item be dismissed without composing
    the whole queue again to look it up.
    """

    return f'{item["revision"]} {item["key"]}'


def named_rows(values) -> dict[str, str]:
    """Key to revision, for each well-formed value a row's button posted."""

    named: dict[str, str] = {}
    for value in values:
        revision, _, key = str(value).partition(" ")
        if key and len(key) <= 200 and 0 < len(revision) <= 16 and revision.isalnum():
            named[key] = revision
    return named


def dismiss_rows(user, rows: dict[str, str]) -> None:
    """Dismiss the named rows at the revisions they were drawn with.

    Nothing is composed and nothing is checked against the live queue: a row
    names only itself, the mark is the reader's own, and one for a revision
    that is not the current one does not hide the item.
    """

    now = timezone.now()
    with transaction.atomic():
        for key, revision in rows.items():
            ActionItemRead.objects.update_or_create(
                user=user, key=key, defaults={"revision": revision, "read_at": now}
            )


def restore_rows(user, keys) -> None:
    """Bring the named items back. Needs nothing but their keys."""

    ActionItemRead.objects.filter(user=user, key__in=list(keys)).delete()


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
