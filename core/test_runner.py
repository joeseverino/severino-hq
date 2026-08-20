"""``--parallel`` on a WAL SQLite database, which Django does not support alone.

The test database is a file in WAL mode deliberately: it gives the suite the
host's journal mode, locking and timeout, so work on a background thread behaves
as it will in production. Two of Django's assumptions do not hold under that,
and both concern when and where the file gets copied.

**Copying skips the sidecar.** Django clones with ``shutil.copy`` of the one
``.sqlite3`` file, but WAL keeps recent pages in ``-wal`` until a checkpoint --
after ``migrate``, that is most of the schema. ``setup_databases`` checkpoints
before cloning.

**Workers do not stay on the file.** Under ``spawn``, Django copies each clone
into a shared-cache in-memory database, which locks whole tables rather than
using WAL and so deadlocks a background thread against the test that started it.
``FileClonedParallelSuite`` keeps workers on the cloned file, as Django does
under ``fork``.

Anything that changes the test database's journal mode, its path, or how work
reaches a thread should be checked against both.

Diagnosing a parallel failure needs ``tblib``: without it the worker cannot
pickle its traceback and the real error is replaced by ``cannot pickle
'traceback' object``. Use ``--parallel=1`` instead, or install it.
"""

from __future__ import annotations

from django.db import connections
from django.test.runner import DiscoverRunner, ParallelTestSuite, _init_worker


def _use_the_file_clone(creation, worker_id):
    """Point a worker at the database file that was cloned for it.

    Under ``spawn`` -- the default start method on macOS -- Django copies each
    worker's clone into a shared-cache *in-memory* database. In-memory SQLite
    locks whole tables instead of using WAL, which deadlocks any test whose work
    runs on a background thread against the test that started it, until the
    busy timeout expires.

    ``clone_test_db`` has already written the file. Using it gives a worker the
    same journal mode, locking and timeout as a serial run, and as production.
    It is what Django itself does under ``fork``.
    """

    settings_dict = creation.get_test_db_clone_settings(worker_id)
    if creation.is_in_memory_db(settings_dict["NAME"]):
        # Nothing was cloned to disk; leave Django's handling alone.
        return creation.__class__.setup_worker_connection(creation, worker_id)
    creation.connection.settings_dict.update(settings_dict)
    creation.connection.close()


def _init_worker_on_file_databases(counter, *args, **kwargs):
    """Django's worker startup, with SQLite kept on disk.

    The patch is applied here rather than at import because it must take effect
    inside the worker process, which under ``spawn`` is a fresh interpreter
    that imported this module afresh.
    """

    from django.db.backends.sqlite3.creation import DatabaseCreation

    DatabaseCreation.setup_worker_connection = _use_the_file_clone
    return _init_worker(counter, *args, **kwargs)


class FileClonedParallelSuite(ParallelTestSuite):
    # A plain function, not a staticmethod: Django reads `self.init_worker
    # .__func__`, which only exists on a bound method.
    init_worker = _init_worker_on_file_databases


def _checkpoint(alias: str) -> None:
    """Fold a WAL sidecar back into the database file it belongs to."""

    connection = connections[alias]
    if connection.vendor != "sqlite":
        return
    with connection.cursor() as cursor:
        cursor.execute("PRAGMA journal_mode")
        if (cursor.fetchone() or [""])[0].lower() != "wal":
            return
        # TRUNCATE rather than PASSIVE: PASSIVE gives up if a reader is mid-read
        # and reports success anyway, which would put us right back to copying a
        # file that is missing its newest pages.
        cursor.execute("PRAGMA wal_checkpoint(TRUNCATE)")


class SeverinoTestRunner(DiscoverRunner):
    """Django's runner, with parallel workers on checkpointed database files."""

    parallel_test_suite = FileClonedParallelSuite

    def setup_databases(self, **kwargs):
        # Build the database first and clone it second, rather than letting one
        # call do both, because the checkpoint has to happen between the two.
        requested = self.parallel
        self.parallel = 0
        try:
            config = super().setup_databases(**kwargs)
        finally:
            self.parallel = requested
        if requested <= 1:
            return config

        for connection, _old_name, _destroy in config:
            _checkpoint(connection.alias)
            for index in range(requested):
                connection.creation.clone_test_db(
                    suffix=str(index + 1),
                    verbosity=self.verbosity,
                    keepdb=self.keepdb,
                )
        return config
