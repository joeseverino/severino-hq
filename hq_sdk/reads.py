"""Read once per request.

HQ answers every request inside one projection scope: a read made twice while
one page is assembled is made once, and the next request sees current state
again. Nothing is kept between requests, so nothing can be stale.

An extension's page is assembled by several of its own functions, and three of
them wanting the same series is three reads unless they share it here::

    def net_worth_series(days=90):
        return read_once(f"example.net_worth:{days}", lambda: _load(days))

The key is the extension's to choose, and to keep apart from everybody else's:
start it with the extension's own name, and put in it every argument the
answer depends on. Outside a request (a command, a test without one) it
loads.

``day_span`` turns a run of local days into the moments to filter a datetime
column by, so a read of "sessions since the 1st" uses the column's index.
"""

from application.projection import day_span, read_once

__all__ = ["day_span", "read_once"]
