"""``--parallel`` on a WAL SQLite database, which Django does not support alone.

The test database is a WAL file on purpose, so work on a background thread
behaves as in production. Two of Django's assumptions about copying it then
fail, answered by ``checkpoint`` and ``use_the_file_clone``: a change to the
test database's journal mode, its path, or how work reaches a thread is checked
against both.
"""

from django.db import connections


def use_the_file_clone(creation, worker_id):
    """Point a worker at the database file that was cloned for it.

    Under ``spawn`` (macOS's default) and ``forkserver`` (Linux's) Django
    copies each clone into a shared-cache in-memory database, which locks whole
    tables instead of using WAL and so deadlocks a test against work it started
    on another thread. The file keeps a serial run's journal mode, as Django
    does under ``fork``; under those two methods Django names a file clone even
    for an in-memory test database.
    """

    creation.connection.settings_dict.update(creation.get_test_db_clone_settings(worker_id))
    creation.connection.close()


def checkpoint(alias: str) -> None:
    """Fold a WAL sidecar back into the database file it belongs to.

    Django clones with ``shutil.copy`` of the one ``.sqlite3`` file, and WAL keeps
    the newest pages (after ``migrate``, most of the schema) in ``-wal``.
    """

    connection = connections[alias]
    if connection.vendor != "sqlite":
        return
    with connection.cursor() as cursor:
        cursor.execute("PRAGMA journal_mode")
        if (cursor.fetchone() or [""])[0].lower() != "wal":
            return
        # TRUNCATE rather than PASSIVE: PASSIVE gives up if a reader is mid-read
        # and reports success anyway, which would leave the clone copied from a
        # file that is missing its newest pages.
        cursor.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def clone_for_workers(config, workers: int, *, verbosity: int, keepdb: bool) -> None:
    """Give each of ``workers`` its own copy of every test database Django built."""

    for connection, _old_name, _destroy in config:
        checkpoint(connection.alias)
        for index in range(workers):
            connection.creation.clone_test_db(suffix=str(index + 1), verbosity=verbosity, keepdb=keepdb)
