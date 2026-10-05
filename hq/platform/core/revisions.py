"""A revision per table, moved by the database on every write.

A derived fact is a function of the rows it reads. To compute it once per
change rather than once per request, something has to say when the rows
changed, and say it in the same transaction as the change: a count that
commits a moment later than its rows lets a reader pair new rows with the old
count.

The count is therefore kept by SQLite. Each model table carries three
triggers (insert, update, delete) that move the table's ``Revision`` row inside
the writing statement. That covers every write path there is: ``save()``,
``QuerySet.update()``, ``bulk_create()``, ``bulk_update()``, a cascade and raw
SQL, in an atomic block or in autocommit, from the web process or a command.
A signal sees only the first of those, and a count written from Python after
an autocommit statement is a second transaction.

``install`` runs after every migrate, because SQLite drops a table's triggers
when a migration rebuilds the table. ``revisions`` checks that the triggers are
there each time it reads, and answers None when they are not: a caller then
derives rather than trust a count nothing is keeping.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from django.apps import apps
from django.conf import settings
from django.db import DatabaseError, connection as default_connection

logger = logging.getLogger("severino.revisions")

TRIGGER_PREFIX = "hq_revision_"
EVENTS = ("insert", "update", "delete")
# The cache holding derived facts, keyed by the revisions they were derived at.
CACHE_ALIAS = "derived"


def _trigger(table: str, event: str) -> str:
    return f"{TRIGGER_PREFIX}{table}_{event}"


def _uncounted() -> set[str]:
    """Tables whose writes are the bookkeeping itself."""

    from .models import Revision

    tables = {Revision._meta.db_table}
    for config in settings.CACHES.values():
        if config.get("BACKEND", "").endswith("DatabaseCache"):
            tables.add(config["LOCATION"])
    return tables


def counted_tables(connection=default_connection) -> list[str]:
    """Every model table in the database, host and extension alike."""

    present = set(connection.introspection.table_names())
    declared = {
        model._meta.db_table
        for model in apps.get_models(include_auto_created=True)
        if model._meta.managed and not model._meta.proxy
    }
    return sorted((declared & present) - _uncounted())


def install(connection=default_connection) -> list[str]:
    """Put the triggers on every counted table; return the tables counted."""

    from .models import Revision

    if connection.vendor != "sqlite":
        return []
    quote = connection.ops.quote_name
    revision = quote(Revision._meta.db_table)
    tables = counted_tables(connection)
    wanted = {_trigger(table, event) for table in tables for event in EVENTS}
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name GLOB %s",
            [f"{TRIGGER_PREFIX}*"],
        )
        for (name,) in cursor.fetchall():
            if name not in wanted:
                cursor.execute(f"DROP TRIGGER {quote(name)}")
        for table in tables:
            for event in EVENTS:
                # The table name is a model's own, quoted as an identifier and
                # as a literal; nothing a request supplies reaches this SQL.
                cursor.execute(
                    f"CREATE TRIGGER IF NOT EXISTS {quote(_trigger(table, event))} "
                    f"AFTER {event.upper()} ON {quote(table)} BEGIN "
                    f"INSERT INTO {revision} (name, value) VALUES ('{table}', 1) "
                    "ON CONFLICT (name) DO UPDATE SET value = value + 1; END"
                )
    return tables


@dataclass(frozen=True)
class Revisions:
    """Every table's revision as one statement saw them, and which are kept."""

    counts: Mapping[str, int]
    triggers: frozenset[str]

    def of(self, tables: Iterable[str]) -> tuple[int, ...] | None:
        """Each table's revision, in the order asked; None when one is not kept."""

        found = []
        for table in tables:
            if any(_trigger(table, event) not in self.triggers for event in EVENTS):
                logger.error("Table %s has no revision triggers; deriving instead.", table)
                return None
            found.append(int(self.counts.get(table, 0)))
        return tuple(found)


def read(connection=default_connection) -> Revisions | None:
    """The revisions as they stand; None when they cannot be read.

    One statement reads the counts and the triggers that keep them, so the
    answer and the proof it can be trusted come from the same snapshot.
    """

    from .models import Revision

    if connection.vendor != "sqlite":
        return None
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT name, value FROM {connection.ops.quote_name(Revision._meta.db_table)} "
                "UNION ALL "
                "SELECT name, -1 FROM sqlite_master WHERE type = 'trigger' AND name GLOB %s",
                [f"{TRIGGER_PREFIX}*"],
            )
            rows = cursor.fetchall()
    except DatabaseError:
        logger.exception("The table revisions could not be read; deriving instead.")
        return None
    return Revisions(
        {name: value for name, value in rows if value >= 0},
        frozenset(name for name, value in rows if value < 0),
    )


def after_migrate(sender, using="default", **kwargs) -> None:
    """Create the cache table, count every table, and forget what was derived.

    Runs once per migrate, after the last app. What was derived by the code
    that ran before the migrate is dropped, so no process reads a value an
    older version of a derivation stored.
    """

    from django.core.cache import caches
    from django.core.management import call_command
    from django.db import connections

    call_command("createcachetable", database=using, verbosity=0)
    install(connections[using])
    try:
        caches[CACHE_ALIAS].clear()
    except DatabaseError:
        logger.exception("The derived cache could not be cleared after migrate.")
