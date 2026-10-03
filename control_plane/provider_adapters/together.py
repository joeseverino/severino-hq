"""Reading several independent things at once.

A sweep is mostly waiting: each request is quick, and there are hundreds of
them. Asked one after another, the waits add up to minutes; asked a few at a
time, they overlap. This is the one place that overlap is arranged, so every
reader that uses it keeps the same two promises.

The sweep's context goes with each read. The per-sweep snapshot and the ledger
of refused parts are context variables, which a new thread does not inherit, so
each read runs in a copy of the caller's context: it shares the snapshot it was
handed and reports into the ledger it was opened under.

And the answer does not depend on timing. Results come back in the order the
items were given, and the first failure, in that order, is the one raised.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from contextvars import copy_context
from typing import TypeVar

Item = TypeVar("Item")
Found = TypeVar("Found")

# A few, not many. Enough for the waits to overlap, few enough that a provider
# sees a client and not a burst. GitHub allows 100 concurrent requests and 900
# points a minute on its REST endpoints, a GET costing one, so four at a time
# is far inside both; what bounds a sweep there is the installation's 5,000
# requests an hour, which reading faster does not spend any faster.
READ_WORKERS = 4


def read_each(
    items: Iterable[Item], read: Callable[[Item], Found], *, workers: int = READ_WORKERS
) -> list[Found]:
    """``read(item)`` for every item, a few at a time, in the order given."""

    todo = list(items)
    count = min(workers, len(todo))
    if count < 2:
        return [read(item) for item in todo]
    with ThreadPoolExecutor(max_workers=count, thread_name_prefix="read") as pool:
        futures = [pool.submit(copy_context().run, read, item) for item in todo]
        # As soon as any read fails, whichever it is, reads not yet started are
        # not started: one item failing fails the whole read, so asking the
        # provider for the rest buys nothing. Only reads submitted after the
        # failed one can still be waiting, so the first failure in the order
        # asked is still the one a caller sees.
        _done, waiting = wait(futures, return_when=FIRST_EXCEPTION)
        for future in waiting:
            future.cancel()
        return [future.result() for future in futures]
