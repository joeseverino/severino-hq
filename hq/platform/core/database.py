"""Upkeep the database asks of the application that holds it."""

from __future__ import annotations

from django.db import DEFAULT_DB_ALIAS, connections

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
