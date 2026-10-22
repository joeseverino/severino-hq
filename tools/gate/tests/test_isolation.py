"""Every gate tool's test keeps its verdicts out of the real `var/gate/`."""

import importlib
import inspect
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from django.test import tag

from tools.gate import verdict as gate_verdict
from tools.gate.tests import base
from tools.gate.tests.base import GateTestCase


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
        self.assertNotEqual(gate_verdict.VERDICTS, base.REAL_VERDICTS)

    def test_a_class_that_changes_the_real_folder_fails(self):
        class Writes(GateTestCase):
            def test_nothing(self):
                pass

        with tempfile.TemporaryDirectory() as real, patch.object(base, "REAL_VERDICTS", Path(real)):
            Writes.setUpClass()
            self.addCleanup(Writes.doClassCleanups)
            Path(real, "check.txt").write_text("[check] FAILED\n")
            with self.assertRaisesRegex(AssertionError, "check.txt"):
                Writes.tearDownClass()
