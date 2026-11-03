"""Every gate tool's test keeps its verdicts out of the real `var/gate/`."""

import importlib
import inspect
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from django.test import tag

from tools.gate import verdict as gate_verdict
from tools.gate.tests.base import GateTestCase

REAL = gate_verdict.ROOT / "var" / "gate"


@tag("source")
class GateIsolationTests(GateTestCase):
    def test_every_gate_test_inherits_the_base_that_keeps_its_verdicts_apart(self):
        here = Path(__file__).parent
        modules = [importlib.import_module(f"{__package__}.{path.stem}") for path in sorted(here.glob("test*.py"))]
        self.assertIn(inspect.getmodule(self), modules)
        loose = [
            f"{module.__name__}.{name}"
            for module in modules
            for name, cls in inspect.getmembers(module, inspect.isclass)
            if issubclass(cls, unittest.TestCase)
            and cls.__module__ == module.__name__
            and not issubclass(cls, GateTestCase)
        ]
        self.assertEqual(loose, [])

    def test_a_verdict_closed_in_a_test_lands_in_its_own_folder(self):
        self.assertNotEqual(gate_verdict.VERDICTS, REAL)

    def test_a_gate_a_test_runs_as_a_subprocess_keeps_its_verdict_in_the_classs_folder(self):
        name = f"isolation-{os.getpid()}"
        subprocess.run(
            [sys.executable, "-m", "tools.gate.verdict", name, sys.executable, "-c", "pass"],
            cwd=gate_verdict.ROOT,
            check=True,
            capture_output=True,
        )

        self.assertTrue(Path(gate_verdict.VERDICTS, f"{name}.txt").is_file())
        self.assertFalse(Path(REAL, f"{name}.txt").exists())

    def test_another_processs_write_to_the_real_folder_fails_no_class(self):
        """Another gate (an extension's plugin check) writes there while the suite runs."""

        class Writes(GateTestCase):
            def test_nothing(self):
                pass

        Writes.setUpClass()
        self.addCleanup(Writes.doClassCleanups)
        REAL.mkdir(parents=True, exist_ok=True)
        handle, planted = tempfile.mkstemp(prefix="plugin-check-", suffix=".txt", dir=REAL)
        os.close(handle)
        self.addCleanup(os.unlink, planted)
        Writes.tearDownClass()
