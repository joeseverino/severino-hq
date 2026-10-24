"""What a suite run selects, and what it leaves behind."""

import contextlib
import io
import json
import multiprocessing
import os
import pickle
import subprocess
import sys
import tempfile
import threading
import unittest
import unittest.mock
from dataclasses import asdict
from pathlib import Path
from typing import override

from django.test import SimpleTestCase, TestCase

from hq.config.sandbox import sweep_abandoned
from hq.platform.core.test_runner import (
    BROWSER_TESTS,
    DEFAULT_STALL_SECONDS,
    REPORTS,
    SOURCE,
    STALL_SECONDS,
    ClassTimedSuite,
    CompositionRemoteResult,
    CompositionTextResult,
    FileClonedParallelSuite,
    SeverinoTestRunner,
    Stalled,
    StallWatch,
    block_on_output,
    excluded_tags,
    report_class_times,
)


class SourceSelectionTests(SimpleTestCase):
    def test_the_whole_suite_leaves_the_source_tests_out(self):
        self.assertIn(SOURCE, excluded_tags(None, set(), set()))

    def test_asking_for_the_source_tests_runs_them(self):
        self.assertNotIn(SOURCE, excluded_tags(None, {SOURCE}, set()))

    def test_a_named_label_runs_what_it_names(self):
        self.assertNotIn(SOURCE, excluded_tags(["hq.platform.core"], set(), set()))

    def test_other_exclusions_are_kept(self):
        self.assertIn("slow", excluded_tags(None, set(), {"slow"}))


class EmptySelectionTests(SimpleTestCase):
    def test_a_tag_no_test_carries_fails_the_run(self):
        runner = SeverinoTestRunner(tags=["no-test-carries-this"], verbosity=0)
        with self.assertRaisesMessage(RuntimeError, "no-test-carries-this"):
            runner.build_suite([__name__])


class AbandonedRunTests(SimpleTestCase):
    def test_a_killed_runs_files_are_deleted_and_a_live_runs_are_kept(self):
        dead = subprocess.Popen([sys.executable, "-c", ""])
        dead.wait()
        live = os.getppid()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            abandoned = [
                f"severino-test-{dead.pid}{tail}"
                for tail in (".sqlite3", "_3.sqlite3", "_3.sqlite3-wal", ".sqlite3-shm")
            ]
            kept = [
                f"severino-test-{os.getpid()}_1.sqlite3",
                f"severino-test-{live}.sqlite3",
                f"severino-test-{live}_2.sqlite3-wal",
                "severino-test-notes.txt",
            ]
            for name in [*abandoned, *kept]:
                (root / name).touch()
            (root / f"severino-test-media-{dead.pid}-x1y2").mkdir()
            (root / f"severino-test-media-{dead.pid}-x1y2" / "receipt.pdf").touch()
            (root / f"severino-test-media-{live}-a3b4").mkdir()
            kept.append(f"severino-test-media-{live}-a3b4")

            sweep_abandoned(root, os.getpid())

            self.assertEqual(sorted(p.name for p in root.iterdir()), sorted(kept))


class BlockingOutputTests(SimpleTestCase):
    def test_a_pipe_left_non_blocking_waits_for_room_again_before_the_report(self):
        read, write = os.pipe()
        self.addCleanup(os.close, read)
        with os.fdopen(write, "w") as stream:
            os.set_blocking(write, False)
            with (
                unittest.mock.patch.object(sys, "stdout", stream),
                unittest.mock.patch.object(sys, "stderr", io.StringIO()),
            ):
                block_on_output()
            self.assertTrue(os.get_blocking(write))


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
        with unittest.mock.patch("hq.platform.core.test_runner.time.monotonic", lambda: clock[0]):
            ClassTimedSuite(tests).run(result)
        return tests

    def times(self, result):
        return {name.rsplit(".", 1)[-1]: seconds for name, seconds in result.class_times.items()}

    def test_a_serial_run_times_each_class_with_its_set_up(self):
        result = CompositionTextResult(io.StringIO(), True, 0)
        self.run_timed(result)

        self.assertEqual(self.times(result), {"Slow": 6, "Quick": 2})

    def test_a_workers_times_reach_the_parent(self):
        worker = CompositionRemoteResult()
        tests = self.run_timed(worker)

        parent = CompositionTextResult(io.StringIO(), True, 0)
        for event in pickle.loads(pickle.dumps(worker.events)):
            FileClonedParallelSuite.handle_event(None, parent, tests, event)
        self.assertEqual(self.times(parent), {"Slow": 6, "Quick": 2})

    def test_the_report_puts_the_slowest_first_beside_the_load(self):
        result = CompositionTextResult(io.StringIO(), True, 0)
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

    def run_failing(self, result, subtests=True):
        tests = list(unittest.defaultTestLoader.loadTestsFromTestCase(_failing_class()))
        tests = [test for test in tests if subtests or test._testMethodName != "test_subtests"]
        unittest.TestSuite(tests).run(result)
        return tests

    def assert_recorded(self, result, subtests=True):
        failed = {each.test.rsplit(".", 1)[-1]: each for each in vars(result)["failed"].values()}
        self.assertEqual(set(failed), {"test_compares", "test_raises", *(["test_subtests"] if subtests else [])})
        self.assertEqual(failed["test_compares"].exception, "AssertionError: 'expected' != 'found'")
        self.assertEqual(failed["test_raises"].exception, "LookupError: no example.invalid record")
        self.assertIn('raise LookupError("no example.invalid record")', failed["test_raises"].traceback)
        if subtests:
            self.assertEqual(failed["test_subtests"].exception, "AssertionError: 0 != -1")

    def test_a_serial_run_records_each_failed_test_once(self):
        result = CompositionTextResult(io.StringIO(), True, 0)
        self.run_failing(result)

        self.assert_recorded(result)

    def test_a_workers_failures_reach_the_parent_with_their_tracebacks(self):
        # A worker pickles a failed subtest's test, which a class local to a
        # function cannot be; the parent records a subtest as a serial run does.
        worker = CompositionRemoteResult()
        tests = self.run_failing(worker, subtests=False)

        parent = CompositionTextResult(io.StringIO(), True, 0)
        for event in pickle.loads(pickle.dumps(worker.events)):
            FileClonedParallelSuite.handle_event(None, parent, tests, event)
        self.assert_recorded(parent, subtests=False)

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


class _Marks(StallWatch):
    def __init__(self):
        super().__init__(seconds=600)
        self.marks = []

    @override
    def mark(self, label):
        self.marks.append(label)
        super().mark(label)


class StallWatchTests(SimpleTestCase):
    """A test process that stops making progress says what it was running and
    where every thread is, and only a process running tests does."""

    def written(self, watch):
        watch.stream.seek(0)
        return watch.stream.read()

    def watch(self, seconds, kind=StallWatch):
        watch = kind(seconds=seconds)
        # A real file: faulthandler writes the stacks to its descriptor.
        watch.stream = self.enterContext(tempfile.TemporaryFile("w+"))
        return watch

    def test_the_report_names_what_stalled_shows_the_evidence_and_every_stack(self):
        watch = self.watch(60)
        watch.evidence.append(lambda stream: stream.write("browser launched\n"))
        watch.report("example.Tests.test_it")

        said = self.written(watch)
        self.assertIn("example.Tests.test_it", said)
        self.assertIn("browser launched", said)
        self.assertIn("test_the_report_names_what_stalled_shows_the_evidence_and_every_stack", said)

    def test_the_report_shows_a_call_waiting_on_another_greenlet(self):
        # As Playwright's sync API leaves a test: switched away mid-call. The
        # greenlet package comes only with the browser group, so a stand-in
        # carries its documented surface: the type, and gr_frame on a
        # suspended one.
        class Suspended:
            def __init__(self, frame):
                self.gr_frame = frame

        def waiting_on_the_browser():
            yield

        call = waiting_on_the_browser()
        next(call)
        self.addCleanup(call.close)
        self.waiting = Suspended(call.gi_frame)
        stand_in = unittest.mock.Mock(greenlet=Suspended)
        watch = self.watch(60)
        with unittest.mock.patch.dict(sys.modules, {"greenlet": stand_in}):
            watch.report("example.Tests.test_it")

        self.assertIn("waiting_on_the_browser", self.written(watch))

    def test_a_mark_left_unfollowed_fails_what_it_marked_after_its_report(self):
        watch = self.watch(0.01)
        with self.assertRaises(Stalled), watch.watching():
            watch.mark("example.Tests.test_stuck")
            threading.Event().wait(60)
        self.assertIn("example.Tests.test_stuck", self.written(watch))

    def test_a_test_that_will_not_stop_ends_its_process(self):
        ended = threading.Event()

        class Ending(StallWatch):
            @override
            def end(self, label):
                ended.set()

        watch = self.watch(0.01, Ending)
        watch.grace = 0.05
        with watch.watching():
            watch.mark("example.Tests.test_swallows")
            while not ended.is_set():
                with contextlib.suppress(Stalled):
                    self.assertTrue(ended.wait(60))

    def test_a_worker_that_ends_stops_its_parent_and_exits_failed(self):
        watch = self.watch(60)
        watch.workers = multiprocessing.Value("i", 2)
        with unittest.mock.patch.object(os, "_exit") as exited:
            watch.end("example.Tests.test_swallows")
        self.assertLess(watch.workers.value, 0)
        self.assertNotEqual(exited.call_args.args[0], 0)

    def test_a_pause_holds_no_deadline_and_restarts_the_bound(self):
        watch = self.watch(600)
        with watch.watching():
            watch.mark("example.Tests.test_waits")
            with watch.paused():
                self.assertIsNone(watch._deadline)
            self.assertIsNotNone(watch._deadline)
            self.assertEqual(watch._label, "example.Tests.test_waits")

    def test_a_process_that_only_replays_results_is_not_watched(self):
        watch = self.watch(0.01)
        watch.mark("example.Tests.test_it")
        self.assertIsNone(watch._thread)

    def test_unset_the_default_bound_watches(self):
        with unittest.mock.patch.dict(os.environ, {STALL_SECONDS: ""}):
            self.assertEqual(StallWatch.from_environment().seconds, DEFAULT_STALL_SECONDS)

    def test_zero_watches_nothing(self):
        with unittest.mock.patch.dict(os.environ, {STALL_SECONDS: "0"}):
            watch = StallWatch.from_environment()
        with watch.watching():
            watch.mark("example.Tests.test_it")
        self.assertFalse(watch.enabled)
        self.assertIsNone(watch._thread)

    def test_a_run_inside_a_test_gives_the_outer_test_back_its_watch(self):
        watch = self.watch(600)
        with watch.watching():
            watch.mark("outer")
            with watch.watching():
                watch.mark("inner")
            self.assertEqual(watch._label, "outer")
        self.assertIsNone(watch._deadline)

    def test_a_suite_marks_each_class_fixture_and_test(self):
        watch = _Marks()
        slow, quick = _timed_classes([0.0])
        loader = unittest.defaultTestLoader
        tests = [*loader.loadTestsFromTestCase(slow), *loader.loadTestsFromTestCase(quick)]
        with unittest.mock.patch("hq.platform.core.test_runner.stall_watch", watch):
            ClassTimedSuite(tests).run(CompositionRemoteResult())

        self.assertEqual(
            [mark.rsplit(".", 1)[-1] for mark in watch.marks],
            ["Slow setUpClass", "test_it", "Slow tearDownClass", "Quick setUpClass", "test_it", "Quick tearDownClass"],
        )

    def test_a_subtest_that_ends_is_progress(self):
        class Pages(unittest.TestCase):
            def test_each(self):
                for page in ("first", "second"):
                    with self.subTest(page=page):
                        pass

        watch = _Marks()
        with unittest.mock.patch("hq.platform.core.test_runner.stall_watch", watch):
            ClassTimedSuite(unittest.defaultTestLoader.loadTestsFromTestCase(Pages)).run(CompositionRemoteResult())

        self.assertTrue(any("second" in mark for mark in watch.marks), watch.marks)


class HeavyTurnTests(SimpleTestCase):
    """A run takes the machine's heavy turn only when it makes a test
    database or drives a browser."""

    def setUp(self):
        class Plain(SimpleTestCase):
            def test_it(self):
                pass

        self.plain = Plain

    def needs_the_turn(self, *kinds):
        loader = unittest.defaultTestLoader
        suite = unittest.TestSuite(loader.loadTestsFromTestCase(kind) for kind in kinds)
        return SeverinoTestRunner(verbosity=0).needs_the_turn(suite)

    def test_tests_without_a_database_or_a_browser_run_at_once(self):
        self.assertFalse(self.needs_the_turn(self.plain))

    def test_one_test_with_a_database_takes_the_turn(self):
        class Stored(TestCase):
            def test_it(self):
                pass

        class Asks(self.plain):
            databases = {"default"}

        self.assertTrue(self.needs_the_turn(self.plain, Stored))
        self.assertTrue(self.needs_the_turn(Asks))

    def test_a_browser_test_takes_the_turn_wherever_its_class_is_declared(self):
        gate = type("Gate", (self.plain,), {"__module__": f"{BROWSER_TESTS}.gate"})

        class Drawn(gate):
            pass

        self.assertTrue(self.needs_the_turn(self.plain, Drawn))
