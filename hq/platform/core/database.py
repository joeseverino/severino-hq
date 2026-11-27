"""Upkeep the database asks of the application that holds it."""

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from typing import Any

from django.db import DEFAULT_DB_ALIAS, connections, transaction

from .sqlite import base as sqlite

# SQLite keeps no queue for its write lock: a connection waiting for it
# sleeps and tries again, at most this long apart once it has waited a while.
RETRIES_EVERY = 0.1
# A connection writing transaction after transaction lets go of the lock after
# this many seconds of it, for longer than a waiting connection sleeps.
# That costs the writer at most LETS_GO / HOLDS of its time and lets any waiter
# in within about HOLDS.
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


def serialized[T](work: Callable[[], T], using: str = DEFAULT_DB_ALIAS) -> T:
    """Run ``work`` in one transaction that holds the write lock only from its first write.

    For work that reads and derives before it writes, as a capability does: it
    holds no other writer back while it reads, and reads one snapshot, so what
    it decides without writing is consistent too. Where another writer
    committed after its snapshot, or holds the lock, at its first write
    (``sqlite.Stale``), what it decided may be stale, so it runs again holding
    the lock from the start.
    Inside a transaction already open it is part of that one.
    """

    connection = connections[using]
    if isinstance(connection, sqlite.DatabaseWrapper) and not connection.in_atomic_block:
        with suppress(sqlite.Stale), connection.optimistically(), transaction.atomic(using=using):
            return work()
    with transaction.atomic(using=using):
        return work()


def writes(using: str = DEFAULT_DB_ALIAS) -> int | None:
    """How many statements that change rows this thread's connection has run; None where HQ cannot count them."""

    connection = connections[using]
    return connection.writes if isinstance(connection, sqlite.DatabaseWrapper) else None


def holds_write_lock(using: str = DEFAULT_DB_ALIAS) -> bool:
    """Whether this thread's connection holds the write lock until it commits
    (``sqlite.DatabaseWrapper``: from its first write, begun optimistically)."""

    connection = connections[using]
    return connection.holds_write_lock if isinstance(connection, sqlite.DatabaseWrapper) else connection.in_atomic_block


@contextmanager
def without_waiting(using: str = DEFAULT_DB_ALIAS) -> Iterator[None]:
    """Within the block, take the write lock only if it is free.

    For a write worth making and not worth waiting for: a request storing an
    answer it derived. A write that would wait for another writer (an import,
    the controller landing a report) fails at once instead of holding the page
    for as long as that writer takes. A connection that already holds the lock
    keeps it, so nothing changes.
    """

    connection = connections[using]
    if connection.vendor != "sqlite" or holds_write_lock(using):
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

    Writing is a run of statements that take the lock (a ``BEGIN`` that is not
    deferred, or a write while the connection holds no lock) with no gap as long as ``LETS_GO`` between any two
    statements. Once a run has lasted ``HOLDS``, the next statement that takes
    the lock waits ``LETS_GO`` first, and a new run starts.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.began: float | None = None
        self.last = 0.0

    def before(self, *, takes_lock: bool) -> float:
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

    Work that commits and begins again within a millisecond always beats a
    waiter retrying every ``RETRIES_EVERY``, so an import would hold the
    controller's report, or a person saving a form, until their busy timeout
    fails them.
    """

    connection = connections[using]
    turns = Turns()

    def taking_turns(execute: Callable[..., Any], sql: str, params: Any, many: bool, context: Any) -> Any:
        head = sql.lstrip()[:14].upper()
        takes_lock = (head.startswith("BEGIN") and head != "BEGIN DEFERRED") or (
            not holds_write_lock(using) and head.startswith(("INSERT", "UPDATE", "DELETE", "REPLACE"))
        )
        pause = turns.before(takes_lock=takes_lock)
        if pause:
            sleep(pause)
        try:
            return execute(sql, params, many, context)
        finally:
            turns.after()

    with connection.execute_wrapper(taking_turns):
        yield
