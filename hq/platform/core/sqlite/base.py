"""HQ's database engine: Django's SQLite backend, which can hold the write lock only while it writes.

SQLite has one write lock for the whole file, and Django takes it with
``BEGIN IMMEDIATE`` as ``atomic`` opens, so what a transaction reads and
derives before it writes holds every other writer back. A transaction begun
``optimistically`` takes the lock at its first statement that writes, reading
until then in autocommit. Taking the lock, it reads ``PRAGMA data_version``,
which moves only when another connection commits: unmoved since its first
read, everything it read is what the lock now holds, as if it had held the
lock from the start. Moved, what it decided may be stale, so it is ``Stale``
and can never commit; ``database.serialized`` runs it again holding the lock
from the start.

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


class Stale(DatabaseError):
    """Another connection committed between what this transaction read and its taking the lock."""


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
    """Takes its connection's write lock before the first statement that writes."""

    wrapper: DatabaseWrapper

    @override
    def execute(self, query, params=None):
        self.wrapper.before(query)
        return self.wrapper.naming_holder(super().execute, query, params)

    @override
    def executemany(self, query, param_list):
        self.wrapper.before(query)
        return self.wrapper.naming_holder(super().executemany, query, param_list)


class DatabaseWrapper(base.DatabaseWrapper):
    SchemaEditorClass = DatabaseSchemaEditor

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.next_optimistic = False
        # Inside an optimistic transaction that has not written yet, the
        # savepoints its nested blocks opened, made once it takes the lock;
        # else None.
        self.unbegun: list[str] | None = None
        # The ``data_version`` its first read saw, or None before one.
        self.seen: int | None = None
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
        self.unbegun = []

    @property
    def holds_write_lock(self) -> bool:
        """Whether this connection holds the write lock until it commits."""

        return self.in_atomic_block and self.unbegun is None

    def before(self, query: str) -> None:
        if self.stale:
            raise Stale("This transaction read what another has since changed.")
        head = query.lstrip()[:9].upper()
        if not head.startswith(UNLOCKED + MARKS):
            self.writes += 1
        if self.unbegun is None:
            return
        if head.startswith(UNLOCKED):
            if self.seen is None and not query.lstrip().upper().startswith("PRAGMA DATA_VERSION"):
                self.seen = self._data_version()
            return
        self.take_write_lock()

    def _data_version(self) -> int:
        # The raw connection's own cursor: no wrapper counts it as a statement.
        (version,) = self.connection.execute("PRAGMA data_version").fetchone()
        return int(version)

    def take_write_lock(self) -> None:
        """Begin the transaction the ``atomic`` block opened, which waits for the lock."""

        if self.unbegun is None:
            return
        # Through ``cursor()``, so every execute wrapper sees the ``BEGIN``
        # (``database.gives_way``). Unbegun until it holds, so a ``BEGIN``
        # that gave up is asked again by the next write.
        self.cursor().execute(f"BEGIN {self.transaction_mode or ''}".strip())
        savepoints, self.unbegun = self.unbegun, None
        _note_holder(self.settings_dict["NAME"])
        if self.seen is not None and self._data_version() != self.seen:
            self.stale = True
            raise Stale("This transaction read what another has since changed.")
        for sid in savepoints:
            super()._savepoint(sid)

    # A savepoint opened before the first write marks nothing to roll back
    # to, so it waits with the transaction, and one released or rolled back
    # before then is forgotten.
    @override
    def _savepoint(self, sid):
        if self.unbegun is None:
            super()._savepoint(sid)
        else:
            self.unbegun.append(sid)

    @override
    def _savepoint_commit(self, sid):
        if self.unbegun is None and not self.stale:
            super()._savepoint_commit(sid)
        elif self.unbegun is not None:
            del self.unbegun[self.unbegun.index(sid) :]

    @override
    def _savepoint_rollback(self, sid):
        if self.unbegun is None and not self.stale:
            super()._savepoint_rollback(sid)
        elif self.unbegun is not None:
            del self.unbegun[self.unbegun.index(sid) :]

    def naming_holder(self, execute, *args):
        try:
            return execute(*args)
        except base.Database.OperationalError as exc:
            if str(exc) != LOCKED:
                raise
            raise base.Database.OperationalError(f"{LOCKED}: {_holder(self.settings_dict['NAME'])}") from exc

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
        self.unbegun, self.seen, self.stale = None, None, False


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
