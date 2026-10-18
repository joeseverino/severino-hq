"""Django's SQLite backend, with every connection in WAL mode.

WAL is a property of the file and persists, so on a database that is already
WAL the switch reads the header and changes nothing. On a file not yet WAL it
has to write the header: it reads the file, then upgrades to the write lock
while still holding its read. SQLite never calls the busy handler for that
upgrade, since two readers waiting for each other would deadlock
(``sqlite3_busy_handler``), so a process opening a new file while another is
switching it fails with "database is locked" at once, whatever the timeout.
A failed statement releases its locks, so trying again until the connection's
own busy timeout runs out is the wait the busy handler would have given.
"""

import sqlite3
import time
from typing import Any, override

from django.db.backends.sqlite3 import base

from hq.platform.core.database import RETRIES_EVERY


def use_wal(conn: sqlite3.Connection) -> None:
    """Put the connection's database in WAL mode, waiting out another switch."""

    (waits_ms,) = conn.execute("PRAGMA busy_timeout").fetchone()
    deadline = time.monotonic() + waits_ms / 1000
    pause = 0.001
    while True:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as error:
            if error.sqlite_errorcode != sqlite3.SQLITE_BUSY or time.monotonic() >= deadline:
                raise
        time.sleep(pause)
        pause = min(pause * 2, RETRIES_EVERY)


class DatabaseWrapper(base.DatabaseWrapper):
    @override
    def get_new_connection(self, conn_params: dict[str, Any]) -> sqlite3.Connection:
        conn = super().get_new_connection(conn_params)
        use_wal(conn)
        return conn
