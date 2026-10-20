"""``--parallel`` on a WAL SQLite database, which Django does not support alone.

The test database is a file in WAL mode deliberately: it gives the suite the
host's journal mode, locking and timeout, so work on a background thread behaves
as it will in production. Two of Django's assumptions do not hold under that,
and both concern when and where the file gets copied.

**Copying skips the sidecar.** Django clones with ``shutil.copy`` of the one
``.sqlite3`` file, but WAL keeps recent pages in ``-wal`` until a checkpoint,
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

import logging
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from django.conf import settings
from django.db import connections
from django.test.runner import (
    DiscoverRunner,
    ParallelTestSuite,
    RemoteTestResult,
    RemoteTestRunner,
    _init_worker,
)

from hq.config.sandbox import sweep_abandoned


def performance_cores() -> int | None:
    """Performance cores on Apple silicon (``hw.perflevel0``); None elsewhere."""

    if sys.platform != "darwin":
        return None
    try:
        found = subprocess.run(
            ["/usr/sbin/sysctl", "-n", "hw.perflevel0.physicalcpu"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except OSError, subprocess.CalledProcessError:
        return None
    return int(found) if found.isdigit() else None


# One test worker per performance core: a worker on an efficiency core slows
# every test it takes, and extra workers push a small machine into swap. Set at
# import, before Django reads DJANGO_TEST_PROCESSES; a value already set, or an
# explicit --parallel, wins.
if (cores := performance_cores()) is not None:
    os.environ.setdefault("DJANGO_TEST_PROCESSES", str(cores))


class _CompositionIsolation:
    """Make process caches obey the same per-test isolation as the database."""

    def startTest(self, test):
        from hq.platform.application.plugins import clear_plugin_composition_cache

        clear_plugin_composition_cache()
        return super().startTest(test)


class _HeldLogs(logging.Handler):
    """One test's log records, kept until the test's outcome is known.

    Many tests exercise a failure on purpose (a refused token, an audit write
    that raises) and the code under test logs it, correctly. Printed as they
    happen, those records would fill the suite's output with warnings and
    tracebacks that are all expected, and a real one could not be told from
    them. ``--buffer`` would not hold them: it swaps ``sys.stdout`` and
    ``sys.stderr``, but a logging handler took the real stream when logging
    was configured. So the root logger writes here for the length of each test, and the
    records are printed only if that test fails or errors. Nothing is dropped
    that anyone needs: a passing test's expected noise is the only thing that
    goes unseen.
    """

    def __init__(self):
        super().__init__(logging.NOTSET)
        self.records: list[logging.LogRecord] = []
        self.console: list[logging.Handler] = []

    def emit(self, record):
        self.records.append(record)

    def take_over(self, root: logging.Logger) -> None:
        # Idempotent, and done at the first test in each process: a parallel
        # worker is a fresh interpreter whose logging Django configured again.
        if root.handlers != [self]:
            self.console = [h for h in root.handlers if h is not self]
            root.handlers = [self]

    def replay(self, test) -> None:
        if not self.records:
            return
        formatter = next((h.formatter for h in self.console if h.formatter), None)
        stream = sys.stderr
        stream.write(f"\n--- log output of {test.id()} ---\n")
        for record in self.records:
            stream.write((formatter.format(record) if formatter else record.getMessage()) + "\n")
        stream.flush()


_held_logs = _HeldLogs()


class _LogsOnFailure:
    """Show a test's logs when, and only when, the test fails."""

    def startTest(self, test):
        _held_logs.take_over(logging.getLogger())
        _held_logs.records.clear()
        return super().startTest(test)

    def addError(self, test, err):
        _held_logs.replay(test)
        return super().addError(test, err)

    def addFailure(self, test, err):
        _held_logs.replay(test)
        return super().addFailure(test, err)

    def addSubTest(self, test, subtest, err):
        if err is not None:
            _held_logs.replay(subtest)
        return super().addSubTest(test, subtest, err)


class CompositionTextResult(_LogsOnFailure, _CompositionIsolation, unittest.TextTestResult):
    pass


class CompositionRemoteResult(_LogsOnFailure, _CompositionIsolation, RemoteTestResult):
    pass


class CompositionRemoteRunner(RemoteTestRunner):
    resultclass = CompositionRemoteResult


def _use_the_file_clone(creation, worker_id):
    """Point a worker at the database file that was cloned for it.

    Under ``spawn`` (the default start method on macOS) Django copies each
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
        # Nothing was cloned to disk; leave Django's handling alone. It returns
        # nothing, and so does this: one exit value on both paths rather than a
        # value on one and a fall-through on the other.
        creation.__class__.setup_worker_connection(creation, worker_id)
        return
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
    runner_class = CompositionRemoteRunner


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
        # and reports success anyway, which would leave the clone copied from a
        # file that is missing its newest pages.
        cursor.execute("PRAGMA wal_checkpoint(TRUNCATE)")


# The deployment identity the suite runs under, whatever the shell exports: the
# values CI sets. A developer's `mise.local.toml` names a real host, and HQ derives its
# own hostname from it, which changes what pages read.
NEUTRAL_IDENTITY = {
    "DJANGO_ALLOWED_HOSTS": "127.0.0.1,testserver",
    "DJANGO_CSRF_TRUSTED_ORIGINS": "",
    "SEVERINO_SITE_HOST": "",
}


def plugin_test_modules() -> list[str]:
    """The module of each installed plugin's Django app, where its tests live."""

    from django.apps import apps

    from hq.platform.application.plugins import installed_plugin_apps

    configs = {config.name: config.name for config in apps.get_app_configs()}
    configs |= {f"{type(c).__module__}.{type(c).__qualname__}": c.name for c in apps.get_app_configs()}
    return [configs.get(entry, entry) for entry in installed_plugin_apps()]


def tests_in(suite: unittest.TestSuite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from tests_in(test)
        else:
            yield test


def require_tests_from(suite: unittest.TestSuite, modules: list[str]) -> None:
    """Refuse a run in which an installed plugin contributed no test."""

    ids = [test.id() for test in tests_in(suite)]
    silent = [module for module in modules if not any(i.startswith(module + ".") for i in ids)]
    if silent:
        raise RuntimeError(f"{len(silent)} installed plugin(s) contributed no test to this run.")


# Tests that read the repository rather than run the product: the docs, the
# workflows, the Dockerfile, the source itself. They hold for a checkout, so
# they run once, on one (`mise run checks:source`), and never in an image,
# which ships none of what they read.
SOURCE = "source"


def excluded_tags(labels: list[str] | None, tags: set[str], exclude: set[str]) -> set[str]:
    """The tags a run leaves out: a whole-suite run leaves out the source
    tests unless it asks for them. A named label runs what it names."""

    if labels or SOURCE in tags:
        return exclude
    return exclude | {SOURCE}


class SeverinoTestRunner(DiscoverRunner):
    """Django's runner, with parallel workers on checkpointed database files."""

    parallel_test_suite = FileClonedParallelSuite

    def __init__(self, *args, installed_plugins=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.installed_plugins = installed_plugins

    @classmethod
    def add_arguments(cls, parser):
        super().add_arguments(parser)
        parser.add_argument(
            "--installed-plugins",
            action="store_true",
            help="Run every installed plugin's tests and none of the host's.",
        )

    def setup_test_environment(self, **kwargs):
        # Environment first, so spawned workers load the same settings.
        os.environ.update(NEUTRAL_IDENTITY)
        settings.ALLOWED_HOSTS = ["127.0.0.1", "testserver"]
        settings.CSRF_TRUSTED_ORIGINS = []
        settings.SEVERINO_SITE_HOST = ""
        super().setup_test_environment(**kwargs)

    def build_suite(self, test_labels=None, **kwargs):
        # A run with no labels is the whole suite: the host's and every
        # installed plugin's, so a composed run tests what it composes. Named
        # labels run what they name.
        self.exclude_tags = excluded_tags(test_labels, self.tags, self.exclude_tags)
        if test_labels:
            suite = super().build_suite(test_labels, **kwargs)
        else:
            modules = plugin_test_modules()
            if self.installed_plugins and not modules:
                raise RuntimeError("--installed-plugins: no plugin is installed.")
            suite = super().build_suite([*modules] if self.installed_plugins else [".", *modules], **kwargs)
            require_tests_from(suite, modules)
        if self.tags and next(tests_in(suite), None) is None:
            # A gate that selects by tag and finds nothing has checked nothing.
            raise RuntimeError(f"No test carries the tag(s) {', '.join(sorted(self.tags))}.")
        return suite

    def get_resultclass(self):
        return super().get_resultclass() or CompositionTextResult

    def setup_databases(self, **kwargs):
        # Here, in the process that starts the run and before it makes files
        # of its own: a killed run never reached its teardown.
        sweep_abandoned(Path(tempfile.gettempdir()), os.getpid())
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
