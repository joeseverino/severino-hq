"""Showing the shape of the data without showing the data.

A viewing mode, and deliberately nothing more than that. It lives in the session
of the one browser that turned it on, so it cannot follow an operator to another
device, cannot reach the API or the MCP, and cannot be left switched on for
somebody else. Nothing is written, so leaving it on costs a page refresh rather
than a restore.

Two rules decide what it may touch, and both exist because a demo that lies
about the wrong thing is worse than no demo at all.

It substitutes *values*, never *facts*. Whether a credential is connected,
whether a sweep is failing, whether something needs attention -- those stay
true. An operator showing HQ to somebody should still be looking at their own
system, and a screenshot of a green board that was actually red is the one
outcome this must never produce.

And a substitution is stable. The same record shows the same stand-in every
time, because a balance that changes on every refresh reads as a bug in the
page rather than as a number. Derived from the record's own key and a fixed
salt: no state to store, and nothing that has to survive a restart.

The host owns the mechanism and knows nothing about who uses it. What a
"financial number" or a "personal record" is belongs to whichever domain holds
one; this module only offers the substitutions and the switch, and every domain
reaches them through ``hq_sdk.demo``.
"""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Iterator

from .money import quantize_money, to_money


_SHOWING: ContextVar[bool] = ContextVar("hq_demo_showing", default=False)

# Fixed, and not a secret. It exists to keep a stand-in stable across restarts,
# not to keep the real value hidden -- the real value never reaches these
# functions in a form the stand-in is derived from.
_SALT = "severino-hq/demo/v1"

# Neutral, unremarkable words. A stand-in should read as a plausible label and
# announce itself as fiction on a second look; anything evocative gets read as
# a real name somebody chose.
_FIRST = (
    "Amber", "Basalt", "Cedar", "Dover", "Ember", "Fenwick", "Garnet", "Harrow",
    "Indigo", "Juniper", "Kestrel", "Larkin", "Marlow", "Norwood", "Onyx",
    "Pembroke", "Quarry", "Rowan", "Sable", "Thistle", "Umber", "Vesper",
)
_SECOND = (
    "Bridge", "Cove", "Drift", "Field", "Gate", "Hollow", "Ridge", "Row",
    "Shore", "Terrace", "Vale", "Way", "Wharf", "Yard",
)


@contextmanager
def demo_scope(showing: bool) -> Iterator[None]:
    """Show substituted values for the duration of one request or call."""

    token = _SHOWING.set(bool(showing))
    try:
        yield
    finally:
        _SHOWING.reset(token)


def showing_demo() -> bool:
    """Whether the caller is inside a request that asked for substitutions."""

    return _SHOWING.get()


def _fraction(key: str, stream: str = "") -> float:
    """A stable 0–1 from a record's key, and nothing derived from its value."""

    digest = hashlib.sha256(f"{_SALT}/{stream}/{key}".encode()).hexdigest()
    return int(digest[:12], 16) / float(16**12)


def amount(value: Any, *, key: str) -> Decimal:
    """A stand-in of the same magnitude and sign, or the real value.

    Magnitude is kept because it is the shape rather than the secret: six
    figures replaced by three reads as an empty record, and a page laid out
    for one is misread as broken. The sign is kept for the same reason -- what
    is owed must go on reading as owed.
    """

    real = to_money(value)
    if not showing_demo():
        return real
    size = abs(real)
    if size < 1:
        return quantize_money(Decimal("0"))
    digits = len(str(int(size)))
    floor = Decimal(10) ** (digits - 1)
    span = floor * 9
    stand_in = floor + (span * Decimal(str(_fraction(key, "amount"))))
    return quantize_money(-stand_in if real < 0 else stand_in)


def label(value: Any, *, key: str) -> str:
    """A stable stand-in name, or the real one.

    Two neutral words rather than a scrambled original: a partially masked name
    is still a name, and reading half of one is how somebody works out the
    other half.
    """

    if not showing_demo():
        return str(value)
    first = _FIRST[int(_fraction(key, "first") * len(_FIRST)) % len(_FIRST)]
    second = _SECOND[int(_fraction(key, "second") * len(_SECOND)) % len(_SECOND)]
    return f"{first} {second}"


# One offset for every date, rather than one per record. Per-record shifts read
# as order-preserving and are not: two records move by different amounts, so a
# page sorted by date reorders and something due next week can be drawn after
# something due next year. A single shift moves the calendar, and every
# relationship in it survives exactly -- which is the only version of this that
# a date-sorted page can be shown with.
_DAY_OFFSET = timedelta(days=int(_fraction("calendar", "day") * 90) - 45)


def day(value: Any, *, key: str = "") -> Any:
    """The date, moved with every other date, or the real one.

    Shifted rather than invented so the shape survives: what falls due before
    something else still does, and what is close is still close. Under two
    months, so urgency reads the same -- a renewal that needed acting on this
    week must not become one that can wait.

    ``key`` is accepted and unused, so a caller reads the same as every other
    substitution and nothing has to remember which of the three is different.
    """

    del key
    if not showing_demo() or not isinstance(value, date):
        return value
    return value + _DAY_OFFSET
