"""What a test run reports: each failed test for the gate running it, each
class's time, and the results a parallel worker sends its parent."""

import json
import os
import pickle
import traceback
import unittest
import unittest.util
from dataclasses import asdict, dataclass
from pathlib import Path

# Bound here, so a test that patches time.monotonic moves its own clock and never a class's time.
from time import monotonic
from typing import override

from django.test.runner import RemoteTestResult, RemoteTestRunner

from hq.platform.core import stalls
from hq.platform.core.coverage_contexts import ModuleContexts
from hq.platform.core.isolation_testing import CompositionIsolation
from hq.platform.core.stalls import StallMarks, block_on_output

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


class ClassTimes:
    """Each test class's wall time, its class fixtures included, by class.

    Django's ``--durations`` times each test from ``setUp`` to its cleanups
    (``unittest.TestCase.run``), so it never sees ``setUpClass``, where a
    browser class renders every page and launches the browser, and it cannot
    say which class a parallel run waited on, since Django gives each worker
    whole classes.
    """

    def addClassTime(self, test, seconds):
        vars(self).setdefault("class_times", {})[unittest.util.strclass(type(test))] = seconds


class CompositionTextResult(FailureRecord, StallMarks, CompositionIsolation, ClassTimes, unittest.TextTestResult):
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


class CompositionRemoteResult(StallMarks, CompositionIsolation, RemoteTestResult):
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


class CompositionRemoteRunner(RemoteTestRunner):
    resultclass = CompositionRemoteResult


class ClassTimedSuite(ModuleContexts):
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
        with stalls.stall_watch.watching():
            return super().run(result, debug)

    def _handleClassSetUp(self, test, result):
        if type(test) is not getattr(result, "_previousTestClass", None):
            stalls.stall_watch.mark(f"{unittest.util.strclass(type(test))} setUpClass")
            self._timing = (test, monotonic())
        super()._handleClassSetUp(test, result)

    def _tearDownPreviousClass(self, test, result):
        previous = getattr(result, "_previousTestClass", None)
        if previous is not None and previous is not type(test):
            stalls.stall_watch.mark(f"{unittest.util.strclass(previous)} tearDownClass")
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
