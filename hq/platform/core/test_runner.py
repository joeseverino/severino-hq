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

import contextlib
import faulthandler
import gc
import io
import json
import logging
import multiprocessing
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import unittest
import unittest.util
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import override

from django.conf import settings
from django.db import connections
from django.test.runner import (
    DiscoverRunner,
    ParallelTestSuite,
    RemoteTestResult,
    RemoteTestRunner,
    _init_worker,
)
lazy from django.apps import apps
lazy from django.db.backends.sqlite3.creation import DatabaseCreation

from hq.config.sandbox import sweep_abandoned
lazy from hq.platform.application import plugins
lazy from hq.platform.application.plugins import clear_plugin_composition_cache


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


# A gate names a folder here, and the run writes each test that failed into
# it, for the gate's closing line. tools/gate/verdict.py, which runs without
# the host, names it too; GateVerdictTests runs this record under a gate.
REPORTS = "HQ_TEST_REPORTS"


@dataclass(frozen=True)
class FailedTest:
    """A test that failed or errored: its id, the exception's first line, and the traceback the run printed."""

    test: str
    exception: str
    traceback: str


class FailureRecord:
    """Each failed test as the result is told of it, a parallel worker's
    replayed in the parent with its traceback (tblib). A test whose subtests
    fail is recorded once, by its first."""

    def addError(self, test, err):
        super().addError(test, err)
        self._record(test, err, self.errors)

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._record(test, err, self.failures)

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            self._record(test, err, self.failures if issubclass(err[0], test.failureException) else self.errors)

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        vars(self).setdefault("failed", {}).setdefault(test.id(), FailedTest(test.id(), "unexpected success", ""))

    def _record(self, test, err, printed) -> None:
        exception = "".join(traceback.format_exception_only(err[0], err[1])).partition("\n")[0]
        # unittest has just added the traceback it prints, as this test's last entry.
        vars(self).setdefault("failed", {}).setdefault(test.id(), FailedTest(test.id(), exception, printed[-1][1]))


def write_failures(result) -> None:
    """Write the run's failed tests where the gate running it reads them, when one does."""

    folder = os.environ.get(REPORTS)
    failed = list(vars(result).get("failed", {}).values())
    if folder and failed:
        Path(folder, f"{os.getpid()}.json").write_text(json.dumps([asdict(each) for each in failed]))


class _ClassTimes:
    """Each test class's wall time, its class fixtures included, by class."""

    def addClassTime(self, test, seconds):
        vars(self).setdefault("class_times", {})[unittest.util.strclass(type(test))] = seconds


def block_on_output() -> None:
    """Make writes to stdout and stderr wait for room again.

    A child that shares the run's pipes (Playwright's driver does) can leave
    them non-blocking, and a long report written then stops part way with
    ``BlockingIOError``, its failures never printed.
    """

    for stream in (sys.stdout, sys.stderr):
        try:
            os.set_blocking(stream.fileno(), True)
        except OSError, ValueError, io.UnsupportedOperation:
            continue


# Seconds a test process may go without progress (a test, a class fixture or a
# subtest starting, or a subtest ending) before the test it is in fails: past
# the slowest legitimate step on a loaded laptop, and short enough that a hung
# test frees the machine's heavy turn within minutes. The environment
# overrides it; 0 turns the watch off.
STALL_SECONDS = "HQ_TEST_STALL_SECONDS"
DEFAULT_STALL_SECONDS = 300.0

# How long a stalled test has to end once interrupted before its process exits,
# and how long the watch's own thread has to act before faulthandler's C
# watchdog ends the process without it: the thread needs the interpreter, which
# a call that never releases it does not give up.
_FALLBACK_GRACE = 30.0

# What interrupts the test's thread: a real signal, so a blocking call in it
# returns with EINTR and the handler runs there.
_INTERRUPT = signal.SIGALRM


class Stalled(Exception):
    """Raised in a test that made no progress for the stall bound.

    An ``Exception``, so unittest records it as the error of the test or class
    fixture it ends, Django's fixtures roll back, and the run goes on. Code
    under test that swallows it leaves the test stalled, and the process exits
    after the grace.
    """


def suspended_greenlets(stream) -> None:
    """The stack of every greenlet that is not running. Playwright's sync API
    runs the test on one and its event loop on another, so the thread's stack
    shows only the loop and the test's own call is here."""

    if (greenlet := sys.modules.get("greenlet")) is None:
        return
    for each in gc.get_objects():
        if isinstance(each, greenlet.greenlet) and each.gr_frame is not None:
            stream.write(f"--- suspended greenlet {each!r} (most recent call last) ---\n")
            stream.writelines(traceback.format_stack(each.gr_frame))


class StallWatch:
    """Fail the test or class fixture a test process is in once it has made no
    progress for ``seconds``, saying where it is stuck.

    A daemon thread waits for the deadline that each ``mark`` moves. When it
    passes, the thread writes what was running, whatever the evidence
    callbacks add (the browser's own log, from ``BrowserGate``), and every
    thread's stack, then interrupts the thread that runs the tests, whose
    handler raises ``Stalled`` there. The test fails and the run goes on. If no
    mark follows within ``grace``, the test could not be stopped, and the
    process exits; a parallel worker first tells Django's parent, which then
    stops the run rather than wait for results that will never come.
    ``faulthandler.dump_traceback_later`` stands behind the thread, for a call
    that holds the interpreter so the thread cannot run.
    """

    def __init__(self, seconds: float | None, stream=None, grace: float = _FALLBACK_GRACE):
        self.seconds = seconds
        self.stream = stream
        self.grace = grace
        self.evidence: list = []
        # A parallel worker's share of the run's worker counter: negative, it
        # stops Django's parent (ParallelTestSuite.run).
        self.workers = None
        self._ready = threading.Condition()
        self._watching = 0
        self._label: str | None = None
        self._deadline: float | None = None
        self._marks = 0
        self._thread: threading.Thread | None = None
        self._main: int | None = None
        self._previous = None
        self._due: str | None = None

    @classmethod
    def from_environment(cls) -> StallWatch:
        value = os.environ.get(STALL_SECONDS, "").strip()
        return cls(float(value) if value else DEFAULT_STALL_SECONDS)

    @property
    def enabled(self) -> bool:
        return bool(self.seconds)

    @contextlib.contextmanager
    def watching(self):
        """Watch this process while the block runs, restoring what an outer
        run was watching when it ends."""

        if not self.enabled:
            yield
            return
        with self._ready:
            saved = (self._label, self._deadline)
            self._watching += 1
            if self._watching == 1:
                self._take_interrupt()
            if self._thread is None:
                self._thread = threading.Thread(target=self._wait, name="stall-watch", daemon=True)
                self._thread.start()
        try:
            yield
        finally:
            with self._ready:
                self._watching -= 1
                self._label, self._deadline = saved if self._watching else (None, None)
                if not self._watching:
                    self._give_back_interrupt()
                self._ready.notify()
            if self._deadline is None:
                faulthandler.cancel_dump_traceback_later()

    def _take_interrupt(self) -> None:
        # Only the main thread may set a handler, and only its handlers run.
        if threading.current_thread() is threading.main_thread():
            self._main = threading.get_ident()
            self._previous = signal.signal(_INTERRUPT, self._interrupted)

    def _give_back_interrupt(self) -> None:
        if self._main is not None:
            signal.signal(_INTERRUPT, self._previous)
            self._main = self._previous = self._due = None

    def _interrupted(self, signum, frame) -> None:
        label, self._due = self._due, None
        if label is not None:
            raise Stalled(f"{label} made no progress for {self.seconds:g}s; its stall report is above")
        if callable(self._previous):
            self._previous(signum, frame)

    def mark(self, label: str) -> None:
        """``label`` starts now; it has ``seconds`` to finish or be followed."""

        if not self._watching:
            return
        with self._ready:
            self._label = label
            self._deadline = time.monotonic() + self.seconds
            self._marks += 1
            self._ready.notify()
        self._backstop(self.seconds + self.grace)

    @staticmethod
    def _backstop(seconds: float) -> None:
        """Arm faulthandler's watchdog, which dumps every stack and exits the
        process. To the process's own stderr: a test may have swapped
        ``sys.stderr`` for a stream with no descriptor."""

        faulthandler.dump_traceback_later(seconds, exit=True, file=sys.__stderr__)

    @contextlib.contextmanager
    def paused(self):
        """No deadline while the block runs: a wait on a child process that
        has a watch of its own. What was running has the whole bound again
        once it ends."""

        with self._ready:
            label, self._deadline = self._label, None
        faulthandler.cancel_dump_traceback_later()
        try:
            yield
        finally:
            if label is not None:
                self.mark(label)

    def report(self, label: str) -> None:
        """Write what has stalled, the evidence for it, and every thread's stack."""

        block_on_output()
        stream = self.stream or sys.stderr
        stream.write(f"\n=== stalled {self.seconds:g}s: {label} (pid {os.getpid()}) ===\n")
        for evidence in list(self.evidence):
            evidence(stream)
        stream.write("--- every thread's stack ---\n")
        stream.flush()
        faulthandler.dump_traceback(file=stream, all_threads=True)
        suspended_greenlets(stream)
        stream.write(f"=== end of stall report: {label} ===\n")
        stream.flush()

    def stop(self, label: str, marks: int) -> None:
        """Fail what stalled, or end the process if it does not stop."""

        # Armed before the interrupt, so the end of the watch, which the
        # interrupt can bring about, always comes after it and cancels it.
        self._backstop(self.grace + _FALLBACK_GRACE)
        if self._main is not None:
            self._due = label
            signal.pthread_kill(self._main, _INTERRUPT)
        with self._ready:
            moved = self._ready.wait_for(lambda: self._marks != marks or not self._watching, timeout=self.grace)
        if not moved:
            self.end(label)

    def end(self, label: str) -> None:
        """Exit the process: what stalled did not stop when interrupted."""

        stream = self.stream or sys.stderr
        stream.write(f"=== {label} did not stop {self.grace:g}s after it was interrupted: the process exits ===\n")
        stream.flush()
        if self.workers is not None:
            with self.workers.get_lock():
                self.workers.value = -1
        os._exit(1)

    def _wait(self) -> None:
        while True:
            with self._ready:
                while self._deadline is None or (left := self._deadline - time.monotonic()) > 0:
                    self._ready.wait(None if self._deadline is None else left)
                label, marks, self._deadline = self._label or "", self._marks, None
            faulthandler.cancel_dump_traceback_later()
            self.report(label)
            self.stop(label, marks)


stall_watch = StallWatch.from_environment()


class _StallMarks:
    """Restart the stall watch as each test begins and each subtest ends."""

    def startTest(self, test):
        stall_watch.mark(test.id())
        return super().startTest(test)

    def addSubTest(self, test, subtest, err):
        stall_watch.mark(f"{test.id()} after {subtest.id().removeprefix(test.id()).strip()}")
        return super().addSubTest(test, subtest, err)


class CompositionTextResult(
    FailureRecord, _StallMarks, _LogsOnFailure, _CompositionIsolation, _ClassTimes, unittest.TextTestResult
):
    def printErrors(self):
        block_on_output()
        super().printErrors()


class CompositionRemoteResult(_StallMarks, _LogsOnFailure, _CompositionIsolation, RemoteTestResult):
    def addClassTime(self, test, seconds):
        # A worker's result keeps nothing: the parent replays this event
        # against the subsuite's test at that index.
        self.events.append(("addClassTime", self.test_index, seconds))


class ClassTimedSuite(unittest.TestSuite):
    """A suite that tells its result how long each class took, from its
    ``setUpClass`` to the end of its ``tearDownClass``, and keeps the stall
    watch on while it runs.

    Under ``--parallel`` each worker's subsuite is this type too, since Django
    partitions a suite into suites of its own type, so the time is measured
    where the class ran and reaches the parent as an event, and the watch runs
    in the process the tests run in. The parent only replays their events.
    """

    @override
    def run(self, result, debug=False):
        if getattr(result, "_testRunEntered", False):
            return super().run(result, debug)
        with stall_watch.watching():
            return super().run(result, debug)

    def _handleClassSetUp(self, test, result):
        if type(test) is not getattr(result, "_previousTestClass", None):
            stall_watch.mark(f"{unittest.util.strclass(type(test))} setUpClass")
            self._timing = (test, time.monotonic())
        super()._handleClassSetUp(test, result)

    def _tearDownPreviousClass(self, test, result):
        previous = getattr(result, "_previousTestClass", None)
        if previous is not None and previous is not type(test):
            stall_watch.mark(f"{unittest.util.strclass(previous)} tearDownClass")
        super()._tearDownPreviousClass(test, result)
        timing = vars(self).pop("_timing", None) if previous is not type(test) else None
        if timing and (record := getattr(result, "addClassTime", None)):
            first, began = timing
            record(first, time.monotonic() - began)


# Enough to show where a run's wall time went: a parallel run lasts as long as
# its slowest class.
SLOWEST_SHOWN = 10


def report_class_times(result, stream, began_load: tuple[float, ...]) -> None:
    """The slowest classes of a run, and the load the machine was under."""

    times = sorted(vars(result).get("class_times", {}).items(), key=lambda item: item[1], reverse=True)
    if not times:
        return
    block_on_output()
    load = " / ".join(f"{a:.2f} {b:.2f} {c:.2f}" for a, b, c in (began_load, os.getloadavg()))
    stream.write(f"\nSlowest test classes (load average at start / end: {load}):\n")
    for name, seconds in times[:SLOWEST_SHOWN]:
        stream.write(f"  {seconds:8.1f}s  {name}\n")
    stream.flush()


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

    DatabaseCreation.setup_worker_connection = _use_the_file_clone
    stall_watch.workers = counter
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

    configs = {config.name: config.name for config in apps.get_app_configs()}
    configs |= {f"{type(c).__module__}.{type(c).__qualname__}": c.name for c in apps.get_app_configs()}
    return [configs.get(entry, entry) for entry in plugins.installed_plugin_apps()]


def tests_in(suite: unittest.TestSuite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from tests_in(test)
        else:
            yield test


# The browser gates' package: a test whose class comes from it drives a browser.
BROWSER_TESTS = "hq.platform.core.tests.browser"


def drives_a_browser(test: unittest.TestCase) -> bool:
    return any(
        kind.__module__ == BROWSER_TESTS or kind.__module__.startswith(f"{BROWSER_TESTS}.")
        for kind in type(test).__mro__
    )


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
    test_suite = ClassTimedSuite

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

    def needs_the_turn(self, suite: unittest.TestSuite) -> bool:
        """Whether running ``suite`` loads the machine, so it takes the heavy
        turn (tools/lib/heavy.sh): it makes a test database or drives a browser."""

        return bool(self.get_databases(suite)) or any(map(drives_a_browser, tests_in(suite)))

    def get_resultclass(self):
        return super().get_resultclass() or CompositionTextResult

    @override
    def run_suite(self, suite, **kwargs):
        began_load = os.getloadavg()
        try:
            result = super().run_suite(suite, **kwargs)
        except multiprocessing.TimeoutError as stopped:
            # Django's parent stops on a negative worker count, which a worker
            # also sets when it exits on a stall (StallWatch.end).
            stopped.add_note("Or a worker exited on a test that did not stop when interrupted: see its stall report.")
            raise
        report_class_times(result, sys.stderr, began_load)
        write_failures(result)
        return result

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
