"""What a suite run selects, and what it leaves behind."""

import io
import os
import pickle
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from django.test import SimpleTestCase

from hq.config.sandbox import sweep_abandoned
from hq.platform.core.test_runner import (
    SOURCE,
    ClassTimedSuite,
    CompositionRemoteResult,
    CompositionTextResult,
    FileClonedParallelSuite,
    SeverinoTestRunner,
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
