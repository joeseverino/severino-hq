"""What a run reports: each failed test, each class's time, and a worker's results as its parent reads them."""

import contextlib
import io
import json
import os
import pickle
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from dataclasses import asdict
from pathlib import Path

from django.test import Client, SimpleTestCase

from hq.platform.core.run_report import (
    REPORTS,
    ClassTimedSuite,
    CompositionRemoteResult,
    CompositionTextResult,
    report_class_times,
)
from hq.platform.core.test_runner import FileClonedParallelSuite, SeverinoTestRunner


def _timed_classes(clock):
    """Two classes whose fixtures and test advance ``clock`` by known amounts."""

    class Slow(unittest.TestCase):
        @classmethod
        def setUpClass(cls):
            clock[0] += 5

        def test_it(self):
            clock[0] += 1

    class Quick(unittest.TestCase):
        def test_it(self):
            clock[0] += 2

    return Slow, Quick


class ClassTimeTests(SimpleTestCase):
    """Each class's wall time reaches the run's result, its fixtures included,
    whether it ran here or in a parallel worker."""

    def run_timed(self, result):
        """The tests run, as the parent holds them: a suite empties itself as it runs."""

        clock = [0.0]
        slow, quick = _timed_classes(clock)
        loader = unittest.defaultTestLoader
        tests = [*loader.loadTestsFromTestCase(slow), *loader.loadTestsFromTestCase(quick)]
        with unittest.mock.patch("hq.platform.core.run_report.monotonic", lambda: clock[0]):
            ClassTimedSuite(tests).run(result)
        return tests

    def times(self, result):
        return {name.rsplit(".", 1)[-1]: seconds for name, seconds in result.class_times.items()}

    def test_a_serial_run_times_each_class_with_its_set_up(self):
        result = CompositionTextResult(io.StringIO(), descriptions=True, verbosity=0)
        self.run_timed(result)

        self.assertEqual(self.times(result), {"Slow": 6, "Quick": 2})

    def test_a_workers_times_reach_the_parent(self):
        worker = CompositionRemoteResult()
        tests = self.run_timed(worker)

        parent = CompositionTextResult(io.StringIO(), descriptions=True, verbosity=0)
        for event in pickle.loads(pickle.dumps(worker.events)):
            FileClonedParallelSuite.handle_event(None, parent, tests, event)
        self.assertEqual(self.times(parent), {"Slow": 6, "Quick": 2})

    def test_the_report_puts_the_slowest_first_beside_the_load(self):
        result = CompositionTextResult(io.StringIO(), descriptions=True, verbosity=0)
        result.class_times = {"example.Quick": 2.0, "example.Slow": 6.0}
        stream = io.StringIO()
        report_class_times(result, stream, (1.0, 2.0, 3.0))

        said = stream.getvalue()
        self.assertIn("load average", said)
        self.assertLess(said.index("example.Slow"), said.index("example.Quick"))


def _failing_class():
    """A failure, an error, a test whose subtests fail, and a pass."""

    class Failing(unittest.TestCase):
        def test_compares(self):
            self.assertEqual("expected", "found")

        def test_raises(self):
            raise LookupError("no example.invalid record")

        def test_subtests(self):
            for n in range(2):
                with self.subTest(n=n):
                    self.assertEqual(n, -1)

        def test_passes(self):
            pass

    return Failing


class FailureRecordTests(SimpleTestCase):
    """Each failed test reaches the gate running the suite as the result saw
    it, whether it ran here or in a parallel worker."""

    def run_failing(self, result):
        tests = list(unittest.defaultTestLoader.loadTestsFromTestCase(_failing_class()))
        unittest.TestSuite(tests).run(result)
        return tests

    def assert_recorded(self, result):
        failed = {each.test.rsplit(".", 1)[-1]: each for each in vars(result)["failed"].values()}
        self.assertEqual(set(failed), {"test_compares", "test_raises", "test_subtests"})
        self.assertEqual(failed["test_compares"].exception, "AssertionError: 'expected' != 'found'")
        self.assertEqual(failed["test_raises"].exception, "LookupError: no example.invalid record")
        self.assertIn('raise LookupError("no example.invalid record")', failed["test_raises"].traceback)
        self.assertEqual(failed["test_subtests"].exception, "AssertionError: 0 != -1")

    def test_a_serial_run_records_each_failed_test_once(self):
        result = CompositionTextResult(io.StringIO(), descriptions=True, verbosity=0)
        self.run_failing(result)

        self.assert_recorded(result)

    def test_a_workers_failures_reach_the_parent_with_their_tracebacks(self):
        # A class local to a function does not pickle, so this holds that a
        # worker sends none of a failed subtest's case.
        worker = CompositionRemoteResult()
        tests = self.run_failing(worker)

        parent = CompositionTextResult(io.StringIO(), descriptions=True, verbosity=0)
        for event in pickle.loads(pickle.dumps(worker.events)):
            FileClonedParallelSuite.handle_event(None, parent, tests, event)
        self.assert_recorded(parent)

    def run_suite(self, folder: str | None):
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(_failing_class())
        with unittest.mock.patch.dict(os.environ), contextlib.redirect_stderr(io.StringIO()):
            os.environ.pop(REPORTS, None)
            if folder:
                os.environ[REPORTS] = folder
            return SeverinoTestRunner(verbosity=0).run_suite(suite)

    def test_the_run_writes_them_where_its_gate_reads_them(self):
        with tempfile.TemporaryDirectory() as folder:
            result = self.run_suite(folder)

            written = [json.loads(path.read_text()) for path in Path(folder).iterdir()]
        self.assertEqual(written, [[asdict(each) for each in vars(result)["failed"].values()]])
        self.assert_recorded(result)

    def test_a_run_no_gate_runs_writes_nothing(self):
        with unittest.mock.patch.object(Path, "write_text") as write:
            self.run_suite(None)

        write.assert_not_called()


class UnpicklableError(Exception):
    """An exception holding what cannot pickle, as a jsonschema error holds its validator's functions."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.check = lambda: message


class UnpicklableFailure(AssertionError):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.check = lambda: message


def _unpicklable_class():
    class Unpicklable(unittest.TestCase):
        def test_errors(self):
            raise UnpicklableError("example.invalid schema refused")

        def test_fails(self):
            raise UnpicklableFailure("example.invalid page differs")

        def test_subtest_fails(self):
            with self.subTest(n=1):
                raise UnpicklableFailure("example.invalid subtest differs")

    return Unpicklable


class UnpicklableFailureTests(SimpleTestCase):
    """A worker's failure whose exception does not pickle still reaches the
    parent as the failure it is, its exception's text and the line it was
    raised on, rather than ending the run on multiprocessing's error."""

    def test_each_reaches_the_parent_as_its_text_and_kind(self):
        tests = list(unittest.defaultTestLoader.loadTestsFromTestCase(_unpicklable_class()))
        worker = CompositionRemoteResult()
        unittest.TestSuite(tests).run(worker)

        parent = CompositionTextResult(io.StringIO(), descriptions=True, verbosity=0)
        for event in pickle.loads(pickle.dumps(worker.events)):
            FileClonedParallelSuite.handle_event(None, parent, tests, event)
        failed = {each.test.rsplit(".", 1)[-1]: each for each in vars(parent)["failed"].values()}
        self.assertEqual(
            {name: each.exception for name, each in failed.items()},
            {
                "test_errors": f"{__name__}.UnpicklableError: example.invalid schema refused",
                "test_fails": f"{__name__}.UnpicklableFailure: example.invalid page differs",
                "test_subtest_fails": f"{__name__}.UnpicklableFailure: example.invalid subtest differs",
            },
        )
        self.assertIn('raise UnpicklableError("example.invalid schema refused")', failed["test_errors"].traceback)
        self.assertEqual((len(parent.errors), len(parent.failures)), (1, 2))


class _Held:
    """Cases a test runs on its own, kept off the module so discovery never
    runs them, and importable by name so a spawned worker can unpickle them."""

    class ClientPages(unittest.TestCase):
        """A case holding a test client ready to serve, whose handler does not
        pickle. Not a SimpleTestCase, which leaves such an attribute out when
        it is pickled."""

        def setUp(self):
            self.client = Client()
            self.client.handler.load_middleware()

        def test_each(self):
            for page in ("first", "second"):
                with self.subTest(page=page):
                    self.assertEqual(page, "first", "example.invalid page")

        def test_passes(self):
            pass


def report_both_ways(path: str) -> None:
    """Run ``_Held.ClientPages`` serially and in two workers, and write what each result lists to ``path``."""

    import django

    django.setup()
    report = {}
    for way in ("serial", "parallel"):
        tests = list(unittest.defaultTestLoader.loadTestsFromTestCase(_Held.ClientPages))
        result = CompositionTextResult(io.StringIO(), descriptions=True, verbosity=0)
        if way == "serial":
            ClassTimedSuite(tests).run(result)
        else:
            suite = FileClonedParallelSuite([ClassTimedSuite([test]) for test in tests], processes=2)
            suite.serialized_aliases = suite.used_aliases = set()
            suite.run(result)
        report[way] = {
            "failures": [[str(test), test.id(), printed] for test, printed in result.failures],
            "errors": [[str(test), printed] for test, printed in result.errors],
            "successful": result.wasSuccessful(),
            "failed": sorted(vars(result).get("failed", {})),
        }
    Path(path).write_text(json.dumps(report))


class ParallelSubtestTests(SimpleTestCase):
    """A failed subtest in a parallel worker reaches the run's result under its
    own name, as a serial run reports it, though its case does not pickle."""

    def test_a_subtest_whose_case_holds_a_client_is_reported_as_a_serial_run_reports_it(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, "report.json")
            # Its own process: a parallel worker, as this test may run in, may
            # not start workers of its own.
            ran = subprocess.run(
                [sys.executable, "-c", f"from {__name__} import report_both_ways; report_both_ways({str(path)!r})"],
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
            )
            self.assertEqual(ran.returncode, 0, ran.stderr[-4000:])
            report = json.loads(path.read_text())

        serial, parallel = report["serial"], report["parallel"]
        self.assertEqual(
            [each[:2] for each in parallel["failures"]], [each[:2] for each in serial["failures"]], parallel
        )
        [(name, test_id, printed)] = parallel["failures"]
        self.assertIn("page='second'", name)
        self.assertIn("page='second'", test_id)
        self.assertIn("example.invalid page", printed)
        self.assertEqual(parallel["errors"], [])
        self.assertFalse(parallel["successful"])
        self.assertEqual(parallel["failed"], serial["failed"])
