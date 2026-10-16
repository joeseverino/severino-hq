"""Upkeep the database asks of the application that holds it."""

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from django.db import DEFAULT_DB_ALIAS, connections

# SQLite keeps no queue for its write lock: a connection waiting for it
# sleeps and tries again, at most this long apart once it has waited a while.
RETRIES_EVERY = 0.1
# A connection writing transaction after transaction lets go of the lock after
# this many seconds of it, for longer than a waiting connection sleeps.
HOLDS = 1.0
LETS_GO = RETRIES_EVERY * 1.5

# Every table is checked, not only those this connection queried: the runs
# that call this (a migration, the nightly prune) touch few tables themselves.
# SQLite bounds the work with its own analysis limit and re-analyzes a table
# only when it has no statistics or its size moved tenfold, so a run with
# nothing to do is close to free.
OPTIMIZE = "PRAGMA optimize=0x10002"


def optimize(using: str = DEFAULT_DB_ALIAS) -> None:
    """Let SQLite refresh the statistics its query planner reads.

    After a schema change and once a day, as SQLite recommends for a database
    whose connections come and go. Never on a request: it can take the write
    lock.
    """

    connection = connections[using]
    if connection.vendor != "sqlite":
        return
    with connection.cursor() as cursor:
        cursor.execute(OPTIMIZE)


@contextmanager
def without_waiting(using: str = DEFAULT_DB_ALIAS) -> Iterator[None]:
    """Within the block, take the write lock only if it is free.

    For a write worth making and not worth waiting for: a request storing an
    answer it derived. A write that would wait for another writer (an import,
    the controller landing a report) fails at once instead of holding the page
    for as long as that writer takes. Inside a transaction the connection
    already holds the lock, so nothing changes.
    """

    connection = connections[using]
    if connection.vendor != "sqlite" or connection.in_atomic_block:
        yield
        return
    with connection.cursor() as cursor:
        cursor.execute("PRAGMA busy_timeout")
        (waits,) = cursor.fetchone()
        cursor.execute("PRAGMA busy_timeout = 0")
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute(f"PRAGMA busy_timeout = {int(waits)}")


class Turns:
    """When a connection writing back to back lets another writer have a turn.

    Writing is a run of statements that take the lock (a ``BEGIN``, or a write
    outside a transaction) with no gap as long as ``LETS_GO`` between any two
    statements. Once a run has lasted ``HOLDS``, the next statement that takes
    the lock waits ``LETS_GO`` first, and a new run starts.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.began: float | None = None
        self.last = 0.0

    def before(self, takes_lock: bool) -> float:
        """Seconds to let go before this statement."""

        now = self.clock()
        if self.began is None or now - self.last >= LETS_GO:
            self.began = now
        elif takes_lock and now - self.began >= HOLDS:
            self.began = now + LETS_GO
            return LETS_GO
        return 0.0

    def after(self) -> None:
        self.last = self.clock()


@contextmanager
def gives_way(using: str = DEFAULT_DB_ALIAS, *, sleep: Callable[[float], None] = time.sleep) -> Iterator[None]:
    """Within the block, writing back to back gives other writers a turn.

    A waiting connection tries again every ``RETRIES_EVERY`` at most, and work
    that commits one transaction and begins the next within a millisecond wins
    every time, so an import would hold the controller's report, or a person
    saving a form, until their busy timeout fails them. Here the work lets go
    for ``LETS_GO`` after each ``HOLDS`` of writing, which costs it at most
    that share of its time and lets any waiter in within about ``HOLDS``.
    """

    connection = connections[using]
    turns = Turns()

    def taking_turns(execute: Callable[..., Any], sql: str, params: Any, many: bool, context: Any) -> Any:
        head = sql.lstrip()[:7].upper()
        takes_lock = head.startswith("BEGIN") or (
            not connection.in_atomic_block and head.startswith(("INSERT", "UPDATE", "DELETE", "REPLACE"))
        )
        pause = turns.before(takes_lock)
        if pause:
            sleep(pause)
        try:
            return execute(sql, params, many, context)
        finally:
            turns.after()

    with connection.execute_wrapper(taking_turns):
        yield
