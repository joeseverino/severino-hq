"""HQ's database engine: Django's SQLite backend, which can hold the write lock only while it writes.

SQLite has one write lock for the whole file, and Django takes it with
``BEGIN IMMEDIATE`` as ``atomic`` opens, so what a transaction reads and
derives before it writes holds every other writer back. A transaction begun
``optimistically`` is ``BEGIN DEFERRED`` instead: WAL gives it one snapshot
from its first read, and its first statement that writes asks for the lock.
SQLite grants it only while that snapshot is the newest, so everything it read
is what the lock now holds, as if it had held the lock from the start, and a
transaction that never writes decided on one consistent state. Inside a read
transaction SQLite never waits for the lock (sqlite.org/rescode.html#busy):
refused, what it decided may be stale, so it is ``Stale`` and can never
commit; ``database.serialized`` runs it again holding the lock from the start.

The connection that takes the lock notes who it is beside the database file,
so a writer that gives up names what last took it in its error, which the
server error log keeps.
"""

import json
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, override

from django.db import DatabaseError
from django.db.backends.sqlite3 import base, schema

lazy from hq.platform.core import attribution, tracing

# A statement that starts with one of these takes no lock of its own: a read,
# or the ``BEGIN`` that takes it.
UNLOCKED = ("SELECT", "PRAGMA", "EXPLAIN", "VALUES", "BEGIN")
# Statements that mark or end a transaction's part, which change no row.
MARKS = ("SAVEPOINT", "RELEASE", "ROLLBACK")
LOCKED = "database is locked"
# SQLite's answers to a read transaction's first write: another connection
# holds the lock (SQLITE_BUSY), or committed after this one's snapshot
# (SQLITE_BUSY_SNAPSHOT).
BUSY, BUSY_SNAPSHOT = 5, 517


class Stale(DatabaseError):
    """Another connection committed, or was writing, while this transaction read before its first write."""


class DatabaseSchemaEditor(schema.DatabaseSchemaEditor):
    @override
    def add_field(self, model, field):
        # SQLite refuses ALTER TABLE ADD COLUMN of a STORED column on a table
        # with rows (sqlite.org/gencol.html), and Django takes that path for a
        # nullable field with no default. Remaking the table, as Django does for
        # every column ALTER TABLE cannot add, computes the column for each row.
        if field.generated and field.db_persist:
            self._remake_table(model, create_field=field)
        else:
            super().add_field(model, field)


class Cursor(base.SQLiteCursorWrapper):
    """Runs each statement through its connection, which knows what it locks."""

    wrapper: DatabaseWrapper

    @override
    def execute(self, query, params=None):
        return self.wrapper.running(super().execute, query, params)

    @override
    def executemany(self, query, param_list):
        return self.wrapper.running(super().executemany, query, param_list)


class DatabaseWrapper(base.DatabaseWrapper):
    SchemaEditorClass = DatabaseSchemaEditor

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.next_optimistic = False
        # Inside a transaction begun optimistically that has not written yet.
        self.reading = False
        self.stale = False
        # How many statements that change rows this connection has run, so a
        # projection knows when what it read may have changed under it.
        self.writes = 0

    @override
    def create_cursor(self, name=None):
        cursor = self.connection.cursor(factory=Cursor)
        cursor.wrapper = self
        return cursor

    @contextmanager
    def optimistically(self) -> Iterator[None]:
        """Begin the transaction ``atomic`` opens within the block optimistically."""

        self.next_optimistic = True
        try:
            yield
        finally:
            self.next_optimistic = False

    @override
    def _start_transaction_under_autocommit(self):
        if not self.next_optimistic:
            super()._start_transaction_under_autocommit()
            _note_holder(self.settings_dict["NAME"])
            return
        self.next_optimistic = False
        # Through ``cursor()``, so every execute wrapper sees the ``BEGIN``
        # (``database.gives_way``).
        self.cursor().execute("BEGIN DEFERRED")
        self.reading = True

    @property
    def holds_write_lock(self) -> bool:
        """Whether this connection holds the write lock until it commits."""

        return self.in_atomic_block and not self.reading

    def running(self, execute, query, *args):
        head = query.lstrip()[:9].upper()
        # Marks still run, so the blocks it is inside end as they began.
        if self.stale and not head.startswith(MARKS):
            raise Stale("This transaction read what another has since changed.")
        changes = not head.startswith(UNLOCKED + MARKS)
        if changes:
            self.writes += 1
        upgrades = changes and self.reading
        try:
            ran = execute(query, *args)
        except base.Database.OperationalError as exc:
            if upgrades and (exc.sqlite_errorcode == BUSY_SNAPSHOT or (exc.sqlite_errorcode == BUSY and self._waits())):
                self.stale = True
                raise Stale("This transaction read what another has since changed.") from exc
            if str(exc) != LOCKED:
                raise
            raise base.Database.OperationalError(f"{LOCKED}: {_holder(self.settings_dict['NAME'])}") from exc
        if upgrades:
            self.reading = False
            _note_holder(self.settings_dict["NAME"])
        return ran

    def _waits(self) -> bool:
        """Whether a write here would wait for the lock (``database.without_waiting`` says not)."""

        # The raw connection's own cursor: no wrapper counts it as a statement.
        (waits,) = self.connection.execute("PRAGMA busy_timeout").fetchone()
        return int(waits) > 0

    @override
    def _commit(self):
        if self.stale:
            # Even where the code it ran caught the error, it never commits.
            self._rollback()
            raise Stale("This transaction read what another has since changed.")
        self._ended()
        return super()._commit()

    @override
    def _rollback(self):
        self._ended()
        return super()._rollback()

    @override
    def _close(self):
        self._ended()
        return super()._close()

    def _ended(self) -> None:
        self.reading, self.stale = False, False


def _note_path(database: str) -> Path:
    return Path(f"{database}-writer")


def _note_holder(database: str) -> None:
    """Write who took the lock beside ``database``; only the holder writes, so writes never overlap."""

    if not database or database == ":memory:" or str(database).startswith("file:"):
        return
    door = attribution.bound()
    note = {
        "at": time.time(),
        "pid": os.getpid(),
        "operation": door.operation if door else "",
        "by": f"{door.interface}/{door.actor}" if door and door.actor else "",
        "request": tracing.trace_id(),
    }
    path = _note_path(database)
    staged = path.with_name(f"{path.name}.{os.getpid()}")
    try:
        staged.write_text(json.dumps(note), encoding="utf-8")
        staged.replace(path)
    except OSError:
        # The note names a holder for an error; a write that fails costs only that.
        return


def _holder(database: str) -> str:
    """Who last took the write lock of ``database``, and how long ago, as a writer that gave up says it."""

    try:
        note = json.loads(_note_path(database).read_text(encoding="utf-8"))
    except OSError, ValueError:
        return "no HQ connection has noted taking the write lock"
    held = max(time.time() - float(note.get("at") or 0), 0)
    parts = [f"last taken {held:.1f}s ago by {note.get('operation') or 'work outside any operation'}"]
    if note.get("by"):
        parts.append(f"for {note['by']}")
    if note.get("request") and note["request"] != tracing.NO_TRACE:
        parts.append(f"in request {note['request']}")
    parts.append(f"in process {note.get('pid')}")
    return " ".join(parts)
