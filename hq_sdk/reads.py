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

A fact that costs more than a read is derived once per change of the tables it
reads, and kept across requests until one of them is written::

    @derivation("example.weekly_totals", reads=("example.Session",))
    def weekly_totals(weeks=12):
        return _totals(weeks)

HQ counts every write to every model table, an extension's included, inside
the writing transaction, so the stored answer is never older than its rows.
``reads`` names every model the function queries; its arguments are plain
values, or ``vary`` says what of them the answer depends on; what it returns
must pickle. The name starts with the extension's own.

A derivation does not read the clock. It asks ``reached``, ``passed``,
``since``, ``whole`` or ``today`` (``present`` is the instant they compare
against), and the stored answer is derived again at the first moment one of
those answers would change, so "due in 3 days" is never served a day late.
Everything derived is dropped when HQ starts on a new release.
"""

from hq.platform.application.derivations import (
    derivation,
    passed,
    present,
    reached,
    since,
    today,
    whole,
)
from hq.platform.application.projection import day_span, read_once

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
