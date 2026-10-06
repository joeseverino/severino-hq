"""Read once per request, and derive once per change.

HQ answers every request inside one projection scope: a read made twice while
one page is assembled is made once, and the next request sees current state
again. Nothing is kept between requests, so nothing can be stale.

An extension's page is assembled by several of its own functions, and three of
them wanting the same series is three reads unless they share it here::

    def weekly_totals(weeks=12):
        return read_once(f"example.weekly_totals:{weeks}", lambda: _load(weeks))

The key is the extension's to choose, and to keep apart from everybody else's:
start it with the extension's own name, and put in it every argument the
answer depends on. Outside a request (a command, a test without one) it
loads.

``day_span`` turns a run of local days into the moments to filter a datetime
column by, so a read of "sessions since the 1st" uses the column's index.

What an extension contributes to a page many domains share (its attention
items, its dashboard cards, its overview, each calendar source's events) is
already derived once per change: HQ asks each of those providers through a
derivation of its own, and an extension does nothing to be kept. A provider
answers from its rows, the clock and whether a demo is showing, and returns a
value that pickles.

A fact of the extension's own that costs more than a read is derived the same
way, and kept across requests until a table it reads is written::

    @derivation("example.weekly_totals", reads=("example.Session",))
    def weekly_totals(weeks=12):
        return _totals(weeks)

HQ counts every write to every model table, an extension's included, inside
the writing transaction, so the stored answer is never older than its rows.
``reads`` names the models the function queries, and any it reads beyond them
is learned the first time; its arguments are plain values, or ``vary`` says
what of them the answer depends on; what it returns must pickle. The name
starts with the extension's own.

The clock is an input too. HQ cannot see where an extension reads it, so an
extension's answer stands a minute at most and never past local midnight:
"due in 3 days" turns on time, and no age is more than a minute behind. A
derivation that asks ``reached``, ``passed``, ``since``, ``whole`` or ``today``
(``present`` is the instant they compare against) is also derived again at the
moment one of those answers would change. Everything derived is dropped when
HQ starts on a new release.
"""

from collections.abc import Callable
from typing import Any, TypeVar

from hq.platform.application import derivations as _derivations
from hq.platform.application.derivations import (
    passed,
    present,
    reached,
    since,
    today,
    whole,
)
from hq.platform.application.projection import day_span, read_once

_T = TypeVar("_T")


def derivation(
    name: str, *, reads: tuple[str, ...], vary: Callable[..., Any] | None = None
) -> Callable[[Callable[..., _T]], Callable[..., _T]]:
    """Declare an extension's function as a derived fact of the tables it reads.

    The host's own, declared as one whose reads of the clock it cannot see.
    """

    return _derivations.derivation(name, reads=reads, vary=vary, unseen=True)


__all__ = [
    "day_span",
    "derivation",
    "passed",
    "present",
    "reached",
    "read_once",
    "since",
    "today",
    "whole",
]
