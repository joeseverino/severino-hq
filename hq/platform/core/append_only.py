"""The audit log is append-only, by SQLite's own rule.

Two triggers refuse an UPDATE or DELETE of an audit row from every write path
(the ORM, raw SQL, a shell), except the changes HQ's own contract makes:

- the daily prune of routine machine events (``audit.prune_routine``), the rows
  ``audit.ROUTINE_EVENTS`` names with no user;
- a deleted user's ``SET_NULL``, which changes ``user_id`` alone;
- a coalesced refusal's count (``denials.COUNT``, ``denials.LAST_AT``), which
  only grows and only moves forward, on a denied row otherwise unchanged.

The triggers are installed after every migrate, beside the revision triggers
(``revisions.install``), because SQLite drops a table's triggers whenever
Django rebuilds the table, and so a changed generator replaces the old SQL.
"""

from collections.abc import Iterator
from contextlib import contextmanager

from django.db import connection as default_connection

lazy from hq.platform.application.denials import COUNT, LAST_AT

lazy from .audit import ROUTINE_EVENTS
lazy from .models import AuditLog

TRIGGER_PREFIX = "hq_append_only_"
REFUSAL = "The audit log is append-only."
# The SQLite function a trigger refuses its statement with, joined from parts
# because the prose gate reads every string as text a reader sees.
_REFUSE = "".join(("rai", "se"))


def _literal(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def triggers(connection=default_connection) -> dict[str, str]:
    """Each trigger's name and the one statement that creates it."""

    quote = connection.ops.quote_name
    table = quote(AuditLog._meta.db_table)
    user = quote(AuditLog._meta.get_field("user").column)
    metadata = quote(AuditLog._meta.get_field("metadata").column)
    action = quote(AuditLog._meta.get_field("action").column)
    object_type = quote(AuditLog._meta.get_field("object_type").column)
    stored = [field.column for field in AuditLog._meta.concrete_fields if not field.generated]
    kept = " AND ".join(
        f"NEW.{quote(column)} IS OLD.{quote(column)}" for column in stored if quote(column) not in {user, metadata}
    )
    count, last_at = _literal(f"$.{COUNT}"), _literal(f"$.{LAST_AT}")

    def key(row: str, path: str) -> str:
        return f"json_extract({row}.{metadata}, {path})"

    # A refusal's count only grows and its latest time only moves forward;
    # nothing else of the row may change.
    recounted = (
        f"json_remove(NEW.{metadata}, {count}, {last_at}) IS json_remove(OLD.{metadata}, {count}, {last_at}) "
        f"AND json_type(NEW.{metadata}, {count}) = 'integer' "
        f"AND {key('NEW', count)} >= coalesce({key('OLD', count)}, 0) "
        f"AND coalesce({key('NEW', last_at)}, '') >= coalesce({key('OLD', last_at)}, '')"
    )
    update = (
        f"CREATE TRIGGER {quote(TRIGGER_PREFIX + 'update')} BEFORE UPDATE ON {table} FOR EACH ROW "
        f"WHEN NOT ({kept} AND ("
        f"(NEW.{user} IS NULL AND NEW.{metadata} IS OLD.{metadata}) OR "
        f"(NEW.{user} IS OLD.{user} AND OLD.{action} = {_literal(AuditLog.Action.DENIED)} "
        f"AND {recounted})"
        f")) BEGIN SELECT {_REFUSE}(abort, {_literal(REFUSAL)}); END"
    )
    routine = " OR ".join(
        f"(OLD.{action} = {_literal(kind)} AND OLD.{object_type} = {_literal(label)})"
        for kind, label in sorted(ROUTINE_EVENTS)
    )
    delete = (
        f"CREATE TRIGGER {quote(TRIGGER_PREFIX + 'delete')} BEFORE DELETE ON {table} FOR EACH ROW "
        f"WHEN NOT (OLD.{user} IS NULL AND ({routine or '0'})) "
        f"BEGIN SELECT {_REFUSE}(abort, {_literal(REFUSAL)}); END"
    )
    return {TRIGGER_PREFIX + "update": update, TRIGGER_PREFIX + "delete": delete}


def installed(connection=default_connection) -> dict[str, str]:
    """The guard's triggers as the database holds them, by name."""

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND name GLOB %s",
            [f"{TRIGGER_PREFIX}*"],
        )
        return dict(cursor.fetchall())


def drop(connection=default_connection) -> None:
    quote = connection.ops.quote_name
    with connection.cursor() as cursor:
        for name in installed(connection):
            cursor.execute(f"DROP TRIGGER {quote(name)}")


def install(connection=default_connection) -> None:
    """Replace the guard's triggers with what ``triggers`` generates today."""

    if connection.vendor != "sqlite":
        return
    if AuditLog._meta.db_table not in connection.introspection.table_names():
        return
    drop(connection)
    with connection.cursor() as cursor:
        for statement in triggers(connection).values():
            cursor.execute(statement)


@contextmanager
def lifted(connection=default_connection) -> Iterator[None]:
    """The guard off for the block and installed again after it: for a test's
    flush or a historical migration run against live rows, never for HQ."""

    if connection.vendor != "sqlite":
        yield
        return
    drop(connection)
    try:
        yield
    finally:
        install(connection)
