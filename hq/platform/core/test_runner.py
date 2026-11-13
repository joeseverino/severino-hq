"""``--parallel`` on a WAL SQLite database, which Django does not support alone.

The test database is a WAL file on purpose, so work on a background thread
behaves as in production. Two of Django's assumptions about copying it then
fail, answered by ``_checkpoint`` and ``_use_the_file_clone``: a change to the
test database's journal mode, its path, or how work reaches a thread is checked
against both.
"""

import contextlib
import faulthandler
import io
import json
import multiprocessing
import os
import pickle
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import traceback
import unittest
import unittest.util
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from pathlib import Path

# Bound here, so a test that patches time.monotonic moves its own clock and never the stall watch's.
from time import monotonic
from typing import TYPE_CHECKING, override
lazy import inspect
lazy import pdb

from django.conf import settings
from django.db import connections
from django.db.models.signals import post_save
from django.test.runner import (
    DiscoverRunner,
    ParallelTestSuite,
    RemoteTestResult,
    RemoteTestRunner,
    _init_worker,
)
lazy from django.apps import apps
lazy from django.contrib.auth import get_user_model
lazy from django.core.management.base import BaseCommand
lazy from django.db.backends.sqlite3.creation import DatabaseCreation
lazy from django.test import TransactionTestCase

from hq.config.imports import lazily
from hq.config.sandbox import MARKERS, shared_tempdir
from hq.platform.core.live_objects import live
lazy from hq.platform.application import agent_limits, automation_adoption, plugins, roles
lazy from hq.platform.core import append_only
lazy from hq.platform.core.attribution import bind_this_thread, bound, command_operation, from_the_shell

if TYPE_CHECKING:
    from hq.platform.application.agent_limits import Limits


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


# Every user a test makes operates HQ, as every account did before a sign-in
# resolved roles, so a test of something else need not give one. A test of how
# a person comes to hold a role, or holds none, makes its users inside
# ``no_default_role``.
_default_role = ContextVar("hq_test_default_role", default=True)


@contextlib.contextmanager
def no_default_role():
    """Users made in the block hold only the role HQ itself gives them."""

    with _default_role.set(False):
        yield


def _operator_by_default(instance, created, *, raw=False, **kwargs):
    if created and not raw and _default_role.get() and isinstance(instance, get_user_model()):
        roles.give_operator(instance)


def default_people_to_operator() -> None:
    """In this process, a user a test makes holds the operator role (``no_default_role``)."""

    post_save.connect(_operator_by_default, dispatch_uid="hq.tests.operator_by_default")


# The interface of what a test writes outside every door. Named only here and in
# tests, so no product code can take a test's binding for a door's
# (tools/lint/ast-grep/rules/test-interface-bound-only-in-tests.yml).
TEST_INTERFACE = "test"


def bind_test_arrangement() -> None:
    """In this process, what a test writes outside a door is the test's own.

    A fixture, a sign-in or a direct call to a service runs on the test's
    thread outside every door, so this one binding stands for all of them. A
    door still binds its own inside it, and a thread a door starts inherits
    none of it, so a door that does not bind is still refused there.
    """

    bind_this_thread(interface=TEST_INTERFACE, actor="", operation="test")


def flush_through_the_audit_guard() -> None:
    """In this process, a TransactionTestCase's flush empties the audit log
    with its guard lifted, and installs the guard again after it.

    The flush is a ``DELETE`` of every row, which the guard refuses everywhere
    else, a production flush included.
    """

    # Django offers no hook before a test's flush, so the flush itself is wrapped.
    flush = TransactionTestCase._fixture_teardown  # noqa: SLF001 - the one place a test flushes
    if getattr(flush, "through_the_audit_guard", False):
        return

    def _fixture_teardown(self):
        with contextlib.ExitStack() as lifted:
            for alias in self._databases_names(include_mirrors=False):
                lifted.enter_context(append_only.lifted(connections[alias]))
            flush(self)

    _fixture_teardown.through_the_audit_guard = True
    TransactionTestCase._fixture_teardown = _fixture_teardown  # noqa: SLF001 - as above


def commands_from_the_shell() -> None:
    """In this process, a management command a test starts runs as ``manage.py``
    runs it: inside the shell's door, as the local operator.

    So a command that binds someone else inside it is refused here, as it is in
    production. A command started inside a door that has bound its own (a job,
    another command) keeps that door's binding, as it does in production.
    """

    # Django offers no hook before a command runs; call_command and
    # run_from_argv both pass through BaseCommand.execute, so it is wrapped.
    execute = BaseCommand.execute
    if getattr(execute, "from_the_shell", False):
        return

    def _execute(self, *args, **options):
        here = bound()
        if here is None or here.interface != TEST_INTERFACE or here.actor:
            return execute(self, *args, **options)
        named = type(self).__module__.rpartition(".")[2]
        with from_the_shell(command_operation(["manage.py", named])):
            return execute(self, *args, **options)

    _execute.from_the_shell = True
    BaseCommand.execute = _execute


# Outside the checkout. Hypothesis caches constants it finds in every module it
# can import, installed extensions included, and a cache in the tree is text
# the host must never carry. Outside the run's own temporary directory too, so
# a failure it found replays in the next run.
HYPOTHESIS_HOME = shared_tempdir() / "severino-hq-hypothesis"


def hypothesis_profiles() -> None:
    """In this process, every property runs under one profile: ``dev`` here,
    Hypothesis's own ``ci`` where it detects CI, ``HYPOTHESIS_PROFILE`` over both.

    ``ci`` keeps what Hypothesis's built-in one sets (derandomized, no database,
    the failing blob printed) and runs more of each. ``dev`` stays light enough
    for ``fast`` and replays a failure it found from its database.
    """

    try:
        from hypothesis import settings as hypothesis  # noqa: PLC0415 - a tools-group dependency the image lacks
        from hypothesis.configuration import set_hypothesis_home_dir  # noqa: PLC0415 - as above
        from hypothesis.database import DirectoryBasedExampleDatabase  # noqa: PLC0415 - as above
    except ImportError:
        return
    set_hypothesis_home_dir(HYPOTHESIS_HOME)
    hypothesis.register_profile(
        "dev",
        max_examples=25,
        stateful_step_count=20,
        deadline=None,
        database=DirectoryBasedExampleDatabase(HYPOTHESIS_HOME / "examples"),
    )
    hypothesis.register_profile("ci", hypothesis.get_profile("ci"), max_examples=200, stateful_step_count=50)
    ci = hypothesis.get_current_profile_name() == "ci"
    hypothesis.load_profile(os.environ.get("HYPOTHESIS_PROFILE") or ("ci" if ci else "dev"))


_markers: tuple[int, Path] | None = None


def own_markers() -> Path:
    """This process's directory of the controller's markers, with their
    settings pointed into it.

    The run's own directory is every worker's, so a heartbeat one worker's
    test wrote would decide another's controller standing as it ran. Keyed by
    process, since a forked worker inherits its parent's.
    """

    global _markers  # noqa: PLW0603 - one directory per process, made on first use
    if _markers is None or _markers[0] != os.getpid():
        directory = Path(tempfile.mkdtemp(prefix="markers-"))
        for setting, name in MARKERS:
            setattr(settings, setting, str(directory / name))
        _markers = (os.getpid(), directory)
    return _markers[1]


def _markers_held() -> dict[str, tuple[bytes, int]]:
    return {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in own_markers().iterdir()}


def _hold_markers(held: dict[str, tuple[bytes, int]]) -> None:
    """Leave exactly ``held`` in the markers directory, each as old as it was."""

    for path in own_markers().iterdir():
        path.unlink(missing_ok=True)
    for name, (content, mtime) in held.items():
        path = own_markers() / name
        path.write_bytes(content)
        os.utime(path, ns=(mtime, mtime))


class _CompositionIsolation:
    """Make process caches, and the agent limits and the controller's markers
    every request shares, obey the same per-test isolation as the database.

    The limits and markers are isolated as ``setUpTestData`` rows are: each
    class sets up on a new set, and each of its tests gets a copy of what that
    setup left, so a stream an estate opens or a heartbeat it writes in
    ``setUpClass`` reaches every test of the class and nothing one test takes
    or writes reaches the next.
    """

    _class: type | None = None
    _class_limits: Limits | None = None
    _class_markers: dict[str, tuple[bytes, int]] | None = None

    def startTest(self, test):
        plugins.clear_plugin_composition_cache()
        automation_adoption.forget()
        if type(test) is not self._class:
            # The first of its class: what the set holds is what its setup left.
            self._class, self._class_limits = type(test), agent_limits.LIMITS
            self._class_markers = _markers_held()
        else:
            _hold_markers(self._class_markers or {})
        agent_limits.LIMITS = _copied(self._class_limits)
        return super().startTest(test)

    def stopTest(self, test):
        # The next class sets up on a new set. A new set rather than an
        # emptied one: a call a test left running gives its slot back to the
        # set it took it from.
        agent_limits.LIMITS = agent_limits.Limits()
        _hold_markers({})
        return super().stopTest(test)


def _copied(limits: agent_limits.Limits) -> agent_limits.Limits:
    """A set holding what ``limits`` holds, each count its own and behind a lock of its own."""

    copied = agent_limits.Limits.__new__(agent_limits.Limits)
    with limits._lock:  # noqa: SLF001 - the copy is the set's whole state, which it offers no way to read
        held = vars(limits)
        copied.__dict__.update(
            {name: dict(value) if isinstance(value, dict) else value for name, value in held.items()}
        )
    copied._lock = threading.Lock()  # noqa: SLF001 - a lock of its own, not the original's
    return copied


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
        # A worker's exception sent as text reads as the exception it stands in for.
        said = str(err[1]) if isinstance(err[1], SentException) else traceback.format_exception_only(err[0], err[1])
        exception = "".join(said).partition("\n")[0]
        # unittest has just added the traceback it prints, as this test's last entry.
        vars(self).setdefault("failed", {}).setdefault(test.id(), FailedTest(test.id(), exception, printed[-1][1]))


def write_failures(result) -> None:
    """Write the run's failed tests where the gate running it reads them, when one does."""

    folder = os.environ.get(REPORTS)
    failed = list(vars(result).get("failed", {}).values())
    if folder and failed:
        Path(folder, f"{os.getpid()}.json").write_text(json.dumps([asdict(each) for each in failed]))


class _ClassTimes:
    """Each test class's wall time, its class fixtures included, by class.

    Django's ``--durations`` times each test from ``setUp`` to its cleanups
    (``unittest.TestCase.run``), so it never sees ``setUpClass``, where a
    browser class renders every page and launches the browser, and it cannot
    say which class a parallel run waited on, since Django gives each worker
    whole classes.
    """

    def addClassTime(self, test, seconds):
        vars(self).setdefault("class_times", {})[unittest.util.strclass(type(test))] = seconds


def breakpoint_on_the_terminal() -> None:
    """``breakpoint()`` in a test: pdb on the process's own stdout, since
    --buffer holds the test's ``sys.stdout``, where the prompt would wait
    unseen."""

    debugger = pdb.Pdb(stdout=sys.__stdout__)
    # Without pyrepl (no terminal), pdb reads through input(), which prompts on sys.stdout.
    debugger.use_rawinput = False
    debugger.set_trace(inspect.currentframe().f_back)


def block_on_output() -> None:
    """Make writes to stdout and stderr wait for room again.

    A child that shares the run's pipes (Playwright's driver does) can leave
    them non-blocking, and a long report written then stops part way with
    ``BlockingIOError``, its failures never printed.
    """

    for stream in (sys.stdout, sys.stderr):
        try:
            os.set_blocking(stream.fileno(), True)
        except OSError, ValueError, io.UnsupportedOperation:  # noqa: S112 - a stream with no descriptor has no blocking mode to set
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
    for each in live(greenlet.greenlet):
        if each.gr_frame is not None:
            stream.write(f"--- suspended greenlet {each!r} (most recent call last) ---\n")
            stream.writelines(traceback.format_stack(each.gr_frame))


class StallWatch:
    """Fail the test or class fixture a test process is in once it has made no
    progress for ``seconds``, saying where it is stuck.

    A daemon thread waits for the deadline each ``mark`` moves, then reports and
    interrupts the test thread, whose handler raises ``Stalled``. With no mark
    within ``grace`` the process exits, a parallel worker first telling Django's
    parent so the run stops (docs/Development.md).
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
            self._deadline = monotonic() + self.seconds
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
        # The process's own stderr: the test's may be --buffer's, printed only once it fails.
        stream = self.stream or sys.__stderr__
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

        stream = self.stream or sys.__stderr__
        stream.write(f"=== {label} did not stop {self.grace:g}s after it was interrupted: the process exits ===\n")
        stream.flush()
        if self.workers is not None:
            with self.workers.get_lock():
                self.workers.value = -1
        os._exit(1)

    def _wait(self) -> None:
        while True:
            with self._ready:
                while self._deadline is None or (left := self._deadline - monotonic()) > 0:
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


class CompositionTextResult(FailureRecord, _StallMarks, _CompositionIsolation, _ClassTimes, unittest.TextTestResult):
    def printErrors(self):
        block_on_output()
        super().printErrors()


@dataclass(frozen=True)
class SentSubtest:
    """A failed subtest as a parallel worker sends it to the parent: what the
    parent's result reads from one, as text.

    A ``_SubTest`` carries its whole test case, so one whose case holds a test
    client or an open connection does not pickle, and Django's worker then
    raises rather than report the failure.
    """

    name: str
    description: str
    short_description: str | None
    failureException: type[BaseException]

    @classmethod
    def of(cls, subtest: unittest.TestCase) -> SentSubtest:
        return cls(subtest.id(), str(subtest), subtest.shortDescription(), subtest.failureException)

    def id(self) -> str:
        return self.name

    def shortDescription(self) -> str | None:
        return self.short_description

    def __str__(self) -> str:
        return self.description


class SentException(Exception):
    """An exception a parallel worker could not pickle, as its text, so the
    parent reports the failure. Django's worker otherwise raises, and the
    whole run ends on multiprocessing's MaybeEncodingError."""


class SentFailure(SentException, AssertionError):
    """A failed assertion a worker could not pickle, still counted a failure."""


def sendable(test: unittest.TestCase, err):
    """``err`` as a worker can send it: itself when it pickles, else its
    text in a SentException with its traceback (tblib pickles that), or
    without one if even that does not pickle."""

    if err is None or _pickles(err):
        return err
    kind, value, trace = err
    text = "".join(traceback.format_exception_only(kind, value)).rstrip("\n")
    sent = SentFailure if issubclass(kind, test.failureException) else SentException
    return (sent, sent(text), trace if _pickles(trace) else None)


def _pickles(value) -> bool:
    try:
        pickle.loads(pickle.dumps(value))  # noqa: S301 - a value of its own, round-tripped to learn whether it crosses to the parent
    except Exception:  # noqa: BLE001 - whatever pickling raises, the value cannot cross to the parent.
        return False
    return True


class CompositionRemoteResult(_StallMarks, _CompositionIsolation, RemoteTestResult):
    def addError(self, test, err):
        super().addError(test, sendable(test, err))

    def addFailure(self, test, err):
        super().addFailure(test, sendable(test, err))

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, sendable(test, err))

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest if err is None else SentSubtest.of(subtest), sendable(test, err))

    def addClassTime(self, _test, seconds):
        # A worker's result keeps nothing: the parent replays this event
        # against the subsuite's test at that index.
        self.events.append(("addClassTime", self.test_index, seconds))


class ClassTimedSuite(unittest.TestSuite):
    """A suite that tells its result how long each class took, from its
    ``setUpClass`` to the end of its ``tearDownClass``, and keeps the stall
    watch on while it runs.

    Django partitions a suite into suites of its own type, so under
    ``--parallel`` both happen in the worker the class ran in.
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
            self._timing = (test, monotonic())
        super()._handleClassSetUp(test, result)

    def _tearDownPreviousClass(self, test, result):
        previous = getattr(result, "_previousTestClass", None)
        if previous is not None and previous is not type(test):
            stall_watch.mark(f"{unittest.util.strclass(previous)} tearDownClass")
        super()._tearDownPreviousClass(test, result)
        timing = vars(self).pop("_timing", None) if previous is not type(test) else None
        if timing and (record := getattr(result, "addClassTime", None)):
            first, began = timing
            record(first, monotonic() - began)


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

    Under ``spawn`` (macOS's default) and ``forkserver`` (Linux's) Django
    copies each clone into a shared-cache in-memory database, which locks whole
    tables instead of using WAL and so deadlocks a test against work it started
    on another thread. The file keeps a serial run's journal mode, as Django
    does under ``fork``; under those two methods Django names a file clone even
    for an in-memory test database.
    """

    creation.connection.settings_dict.update(creation.get_test_db_clone_settings(worker_id))
    creation.connection.close()


def _init_worker_on_file_databases(counter, *args, **kwargs):
    """Django's worker startup, with SQLite kept on disk and imports lazy.

    Both take effect here rather than at import because a worker under
    ``spawn`` or ``forkserver`` starts from a fresh interpreter, which never
    ran ``manage.py``'s ``main()``. Lazy before ``django.setup()``, so a worker
    holds what its tests reach, as the process that started the run does.
    """

    lazily()
    DatabaseCreation.setup_worker_connection = _use_the_file_clone
    stall_watch.workers = counter
    started = _init_worker(counter, *args, **kwargs)
    own_markers()
    default_people_to_operator()
    flush_through_the_audit_guard()
    commands_from_the_shell()
    bind_test_arrangement()
    hypothesis_profiles()
    return started


class FileClonedParallelSuite(ParallelTestSuite):
    # A worker's traceback reaches the parent through tblib; without it the error
    # reads "cannot pickle 'traceback' object", so run --parallel=1 or install it.
    # A plain function, not a staticmethod: Django reads `self.init_worker
    # .__func__`, which only exists on a bound method.
    init_worker = _init_worker_on_file_databases
    runner_class = CompositionRemoteRunner


def _checkpoint(alias: str) -> None:
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


def browser_failures() -> Path:
    """Where a browser run keeps the shots of what failed: in the checkout,
    since they outlive the run, in a folder of the task that ran it, so each
    leg of ``mise run browser`` keeps its own."""

    task = os.environ.get("MISE_TASK_NAME") or "test"
    return Path(settings.BASE_DIR, "var", "browser-failures", re.sub(r"[^\w.-]", "-", task))


def require_tests_from(suite: unittest.TestSuite, modules: list[str]) -> None:
    """Refuse a run in which an installed plugin contributed no test."""

    ids = [test.id() for test in tests_in(suite)]
    silent = [module for module in modules if not any(i.startswith(module + ".") for i in ids)]
    if silent:
        raise RuntimeError(f"{len(silent)} installed plugin(s) contributed no test to this run.")


# Tests that read the repository rather than run the product: the docs, the
# workflows, the Dockerfile, the source itself. They hold for a checkout, so
# they run once, on one (`mise run lint:source`), and never in an image,
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
        # A run's output is its failures: Django's --buffer drops what a passing
        # test prints and prints a failing one's with it, and verbosity 0 its
        # progress (the dots, the test databases); -v 1 shows that again.
        parser.set_defaults(buffer=True, verbosity=0)
        parser.add_argument(
            "--no-buffer", dest="buffer", action="store_false", help="Print what passing tests print too."
        )

    def setup_test_environment(self, **kwargs):
        # Environment first, so spawned workers load the same settings.
        os.environ.update(NEUTRAL_IDENTITY)
        settings.ALLOWED_HOSTS = ["127.0.0.1", "testserver"]
        settings.CSRF_TRUSTED_ORIGINS = []
        settings.SEVERINO_SITE_HOST = ""
        super().setup_test_environment(**kwargs)
        sys.breakpointhook = breakpoint_on_the_terminal
        own_markers()
        default_people_to_operator()
        flush_through_the_audit_guard()
        commands_from_the_shell()
        bind_test_arrangement()
        hypothesis_profiles()

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
        if any(drives_a_browser(test) for test in tests_in(suite)):
            # Here, in the process that starts the run and before any worker
            # shoots: the last run's shots of this task give way to this one's.
            shutil.rmtree(browser_failures(), ignore_errors=True)
        if self.tags and next(tests_in(suite), None) is None:
            # A gate that selects by tag and finds nothing has checked nothing.
            untagged = f" Their tests run with `mise run test {' '.join(test_labels)}`." if test_labels else ""
            raise RuntimeError(f"No test carries the tag(s) {', '.join(sorted(self.tags))}.{untagged}")
        return suite

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
