"""When each open item of the queue was first seen.

An item is derived from what holds now, so on its own it never says since
when. The moment is recorded where the estate is written: a report from the
controller is followed, at most every ``LOOK_EVERY``, by one look at what is
open (``note_open``). A page only reads what a look recorded.

A date is recorded only when it is known. An item open at the first look ever
began at a time nobody recorded, and so did one that appears after a gap with
no look: both are kept with no date, and a card then says none.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import replace
from datetime import datetime, timedelta
from functools import wraps
from typing import Any, TypeVar

from django.utils import timezone

from .projection import read_once
from .readings import record, stored
from .timestamps import moment

READING_KEY = "attention.first-seen"
# How often a report is followed by a look. A look derives the whole queue,
# so it is not taken with every report.
LOOK_EVERY = timedelta(minutes=15)
# How long before a look the one before it may be for a new item to be dated
# by this one. Past it the item began somewhere in a gap.
KNOWN_WITHIN = timedelta(hours=1)

_Item = TypeVar("_Item")


def about(item: Any) -> str:
    """What an item is about, for as long as it is the same matter."""

    return str(getattr(item, "key", "") or f"{item.eyebrow}:{item.title}")


def recorded() -> dict[str, datetime]:
    """Every open item whose start is known, by what it is about. One read."""

    return read_once("first_seen.recorded", _recorded)


def _recorded() -> dict[str, datetime]:
    found = stored(READING_KEY)
    seen = found.value if found is not None and isinstance(found.value, dict) else {}
    return {key: at for key, at in ((key, moment(stamp)) for key, stamp in seen.items()) if at}


def with_first_seen(items: Iterable[_Item]) -> tuple[_Item, ...]:
    """``items``, each saying since when where it does not already.

    A notice reports an occurrence and carries its own date in its words.
    """

    items = tuple(items)
    if not items:
        return items
    seen = recorded()
    return tuple(
        item
        if item.since is not None or item.notice or about(item) not in seen
        else replace(item, since=seen[about(item)])
        for item in items
    )


def dated(provider: Callable[[], Iterable[_Item]]) -> Callable[[], tuple[_Item, ...]]:
    """A queue provider whose items say since when."""

    @wraps(provider)
    def provide() -> tuple[_Item, ...]:
        return with_first_seen(provider())

    return provide


# When this process last knew of a look, so a report that follows one closely
# asks the database nothing. The stored reading stays the authority.
_looked: list[datetime] = []


def look_due(now: datetime | None = None) -> bool:
    """Whether the last look is old enough for another. At most one read."""

    now = now or timezone.now()
    if _looked and timedelta(0) <= now - _looked[0] < LOOK_EVERY:
        return False
    found = stored(READING_KEY)
    if found is None or now - found.observed_at >= LOOK_EVERY:
        return True
    _looked[:] = [found.observed_at]
    return False


def note_open(keys: Iterable[str], *, now: datetime | None = None) -> None:
    """Record which items are open at this look.

    An item already recorded keeps what it has. A new one is dated now when
    the look before this one was within ``KNOWN_WITHIN``. One that is no
    longer open is forgotten, so it is dated again if it returns.
    """

    now = now or timezone.now()
    found = stored(READING_KEY)
    known = dict(found.value) if found is not None and isinstance(found.value, dict) else {}
    watched = found is not None and now - found.observed_at <= KNOWN_WITHIN
    stamp = now.isoformat() if watched else None
    record(READING_KEY, {key: known[key] if key in known else stamp for key in keys}, observed_at=now)
    _looked[:] = [now]
