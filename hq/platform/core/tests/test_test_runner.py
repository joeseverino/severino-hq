"""What a suite run selects, and what it leaves behind."""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from hq.config.sandbox import sweep_abandoned
from hq.platform.core.test_runner import SOURCE, SeverinoTestRunner, excluded_tags


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
