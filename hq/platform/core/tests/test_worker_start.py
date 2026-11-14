"""A parallel test worker imports as the process that started the run does."""

import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from django.test import SimpleTestCase

from hq.config import imports
from hq.platform.core.run_report import ClassTimedSuite, CompositionTextResult
from hq.platform.core.test_runner import FileClonedParallelSuite


class _Held:
    """Out of discovery's reach: run only inside a worker by ``run_in_workers``."""

    class ImportMode(SimpleTestCase):
        def test_imports_wait_for_first_use_but_eagers(self):
            self.assertEqual(sys.get_lazy_imports(), "all")
            self.assertIs(sys.get_lazy_imports_filter(), imports._lazy)

        def test_in_a_second_worker_too(self):
            self.test_imports_wait_for_first_use_but_eagers()


def run_in_workers(path: str) -> None:
    """Run ``_Held.ImportMode`` in two workers from a process started as a spawned worker is, and write the outcome."""

    import django

    django.setup()
    tests = list(unittest.defaultTestLoader.loadTestsFromTestCase(_Held.ImportMode))
    result = CompositionTextResult(io.StringIO(), descriptions=True, verbosity=0)
    suite = FileClonedParallelSuite([ClassTimedSuite([test]) for test in tests], processes=2)
    suite.serialized_aliases = suite.used_aliases = set()
    suite.run(result)
    Path(path).write_text(
        json.dumps(
            {
                "ran": result.testsRun,
                "problems": [printed for _test, printed in result.failures + result.errors],
            }
        )
    )


class WorkerImportTests(SimpleTestCase):
    def test_a_worker_imports_lazily_with_the_eager_exceptions(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, "report.json")
            # Its own process, started without manage.py's main(), as a worker
            # is; and a parallel worker, as this test may run in, may not start
            # workers of its own.
            ran = subprocess.run(
                [sys.executable, "-c", f"from {__name__} import run_in_workers; run_in_workers({str(path)!r})"],
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
            )
            self.assertEqual(ran.returncode, 0, ran.stderr[-4000:])
            report = json.loads(path.read_text())

        self.assertEqual(report["ran"], 2)
        self.assertEqual(report["problems"], [])
