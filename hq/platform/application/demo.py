"""Showing the shape of the data without showing the data, as a viewing mode.

It substitutes *values*, never *facts*: whether a credential is connected or
something needs attention stays true, since a screenshot of a green board that
was really red is the one outcome this must never produce. The host owns the
switch and the substitutions; what a "financial number" or a "personal record"
is belongs to each domain, which reaches them through ``hq_sdk.demo``.
"""

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, timedelta
from decimal import Decimal

from .money import quantize_money, to_money

# Set per request from the session of the one browser that turned the mode on,
# so it cannot follow an operator to another device, reach the API or the MCP,
# or be left on for somebody else. Nothing is written, so leaving it on costs a
# page refresh rather than a restore.
_SHOWING: ContextVar[bool] = ContextVar("hq_demo_showing", default=False)

# The same record shows the same stand-in every time, since a balance that
# changes on every refresh reads as a bug in the page rather than as a number.
# Fixed, and not a secret. It exists to keep a stand-in stable across restarts,
# not to keep the real value hidden: the real value never reaches these
# functions in a form the stand-in is derived from.
_SALT = "severino-hq/demo/v1"

# Neutral, unremarkable words. A stand-in should read as a plausible label and
# announce itself as fiction on a second look; anything evocative gets read as
# a real name somebody chose.
_FIRST = (
    "Amber",
    "Basalt",
    "Cedar",
    "Dover",
    "Ember",
    "Fenwick",
    "Garnet",
    "Harrow",
    "Indigo",
    "Juniper",
    "Kestrel",
    "Larkin",
    "Marlow",
    "Norwood",
    "Onyx",
    "Pembroke",
    "Quarry",
    "Rowan",
    "Sable",
    "Thistle",
    "Umber",
    "Vesper",
)
_SECOND = (
    "Bridge",
    "Cove",
    "Drift",
    "Field",
    "Gate",
    "Hollow",
    "Ridge",
    "Row",
    "Shore",
    "Terrace",
    "Vale",
    "Way",
    "Wharf",
    "Yard",
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


# The most figures a stand-in has. A stand-in of the real size says how large
# the real amount is, and for money that is most of what there is to know: a
# five-figure stand-in for a five-figure balance reads as plausibly the real
# one. Capped, a demo shows amounts nobody would take for anybody's.
DEMO_MAX_DIGITS = 4


def amount(value: object, *, key: str) -> Decimal:
    """A stand-in with the same sign, or the real value.

    Small amounts keep their size, because the shape of a page depends on it:
    a coffee replaced by a salary reads as broken. Large ones do not: anything
    of ``DEMO_MAX_DIGITS`` figures or more is shown at that many, so the
    stand-in says only "a thousand or more" about what it replaced. The sign
    is kept: what is owed must go on reading as owed.
    """

    real = to_money(value)
    if not showing_demo():
        return real
    size = abs(real)
    if size < 1:
        return quantize_money(Decimal(0))
    digits = min(len(str(int(size))), DEMO_MAX_DIGITS)
    floor = Decimal(10) ** (digits - 1)
    span = floor * 9
    stand_in = floor + (span * Decimal(str(_fraction(key, "amount"))))
    return quantize_money(-stand_in if real < 0 else stand_in)


def label(value: object, *, key: str) -> str:
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
# relationship in it survives exactly, which is the only version of this that
# a date-sorted page can be shown with.
_DAY_OFFSET = timedelta(days=int(_fraction("calendar", "day") * 90) - 45)


def day[T](value: T, *, key: str = "") -> T:
    """The date, moved with every other date, or the real one.

    Shifted rather than invented, by under two months, so what falls due first
    still does and urgency reads the same. ``key`` is unused, so a caller reads
    the same as every other substitution.
    """

    del key
    if not showing_demo() or not isinstance(value, date):
        return value
    return value + _DAY_OFFSET
