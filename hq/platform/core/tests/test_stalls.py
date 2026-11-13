"""A test process that stops making progress says what it was running and where every thread is."""

import contextlib
import io
import multiprocessing
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from typing import override

from django.test import SimpleTestCase

from hq.platform.core.run_report import ClassTimedSuite, CompositionRemoteResult
from hq.platform.core.stalls import DEFAULT_STALL_SECONDS, STALL_SECONDS, Stalled, StallWatch

from .test_run_report import _timed_classes

# Children a test waits on: one that sleeps, and one that keeps a core busy.
SLEEPS = "import time; time.sleep(600)"
BURNS = "while True: pass"


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

    def test_the_report_reaches_the_process_stderr_though_a_test_holds_sys_stderr(self):
        # --buffer swaps sys.stderr for a buffer it prints only once the test
        # fails, which a process that exits on the stall never reaches.
        watch = StallWatch(seconds=60)
        stderr = self.enterContext(tempfile.TemporaryFile("w+"))
        held = io.StringIO()
        with unittest.mock.patch.object(sys, "__stderr__", stderr), contextlib.redirect_stderr(held):
            watch.report("example.Tests.test_hangs")
            with unittest.mock.patch.object(os, "_exit"):
                watch.end("example.Tests.test_hangs")
        stderr.seek(0)
        written = stderr.read()
        self.assertIn("stalled 60s: example.Tests.test_hangs", written)
        self.assertIn("example.Tests.test_hangs did not stop", written)
        self.assertEqual(held.getvalue(), "")

    def test_a_test_that_patches_the_clock_is_not_stalled_by_it(self):
        # As a test that fakes time.monotonic from its setUp leaves the watch:
        # its deadline set on the real clock, then woken under the fake one.
        watch = self.watch(60)
        with watch.watching():
            watch.mark("example.Tests.test_clocked")
            with unittest.mock.patch("time.monotonic", lambda: 10.0**12):
                with watch.watching():
                    pass
                threading.Event().wait(0.2)
        self.assertNotIn("example.Tests.test_clocked", self.written(watch))

    def test_a_mark_left_unfollowed_fails_what_it_marked_after_its_report(self):
        watch = self.watch(0.01)
        with self.assertRaises(Stalled), watch.watching():
            watch.mark("example.Tests.test_stuck")
            threading.Event().wait(60)
        self.assertIn("example.Tests.test_stuck", self.written(watch))

    def child(self, program: str) -> subprocess.Popen:
        child = subprocess.Popen([sys.executable, "-c", program])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        return child

    def test_a_test_waiting_on_a_child_that_sleeps_past_the_bound_is_stopped(self):
        watch = self.watch(0.5)
        child = self.child(SLEEPS)
        with self.assertRaises(Stalled), watch.watching():
            watch.mark("example.Tests.test_waits")
            child.wait()
        self.assertIn("example.Tests.test_waits", self.written(watch))

    def test_a_test_waiting_on_a_child_that_burns_cpu_past_the_bound_is_kept(self):
        # However long load makes its wall time: the child spends CPU through it.
        began = time.monotonic()
        past = threading.Event()

        class Reading(StallWatch):
            def cpu(self):
                if time.monotonic() > began + 4 * self.seconds:
                    past.set()
                return super().cpu()

        watch = self.watch(0.5, Reading)
        self.child(BURNS)
        with watch.watching():
            watch.mark("example.Tests.test_waits")
            self.assertTrue(past.wait(120), "the watch read the tree past four bounds")
        self.assertNotIn("example.Tests.test_waits", self.written(watch))

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
        with unittest.mock.patch("hq.platform.core.stalls.stall_watch", watch):
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
        with unittest.mock.patch("hq.platform.core.stalls.stall_watch", watch):
            ClassTimedSuite(unittest.defaultTestLoader.loadTestsFromTestCase(Pages)).run(CompositionRemoteResult())

        self.assertTrue(any("second" in mark for mark in watch.marks), watch.marks)
