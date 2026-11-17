"""The recording fast:map makes: each test's lines under its own context, read back by module for fast's map."""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from coverage import CoverageData
from django.test import SimpleTestCase

from tools.gate import testmap
from tools.gate.chain import ROOT

CODE = """\
def fixture():
    return 1


def first():
    return 2


def second():
    return 3
"""

TESTS = """\
import unittest

from sample import code


class SampleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        code.fixture()

    def test_first(self):
        code.first()

    def test_second(self):
        code.second()
"""

# Records the sample's tests as fast:map records the suite: the C tracer, and the runner's own suite class.
RECORD = """\
import sys
import unittest

import coverage

root, data_file = sys.argv[1], sys.argv[2]
measuring = coverage.Coverage(data_file=data_file, source=[root], config_file=False)
measuring.set_option("run:core", "ctrace")
measuring.start()
import django

django.setup()
from hq.platform.core.run_report import ClassTimedSuite

import sample.tests.test_code as module

loader = unittest.TestLoader()
loader.suiteClass = ClassTimedSuite
outcome = unittest.TextTestRunner(stream=sys.stderr).run(loader.loadTestsFromModule(module))
measuring.stop()
measuring.save()
sys.exit(not outcome.wasSuccessful())
"""


def line_of(source: str, text: str) -> int:
    return next(n for n, line in enumerate(source.splitlines(), 1) if text in line)


class RecordingTests(SimpleTestCase):
    def test_each_test_s_lines_are_its_own_and_its_class_fixture_s_are_its_module_s(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            (root / "sample" / "tests").mkdir(parents=True)
            for path, text in {
                "sample/__init__.py": "",
                "sample/code.py": CODE,
                "sample/tests/__init__.py": "",
                "sample/tests/test_code.py": TESTS,
                "record.py": RECORD,
            }.items():
                (root / path).write_text(text)
            environment = {key: value for key, value in os.environ.items() if not key.startswith("COVERAGE")}
            environment["PYTHONPATH"] = os.pathsep.join((str(root), str(ROOT)))
            data_file = root / "data"
            run = subprocess.run(
                [sys.executable, str(root / "record.py"), str(root), str(data_file)],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            data = CoverageData(basename=str(data_file))
            data.read()
            ran = {
                line: set(contexts) for line, contexts in data.contexts_by_lineno(str(root / "sample/code.py")).items()
            }
            module = "sample.tests.test_code"
            self.assertEqual(ran[line_of(CODE, "return 1")], {module})
            (first,) = ran[line_of(CODE, "return 2")]
            (second,) = ran[line_of(CODE, "return 3")]
            self.assertNotEqual(first, second, "the two tests' lines share one context")
            from hq.platform.core.coverage_contexts import label_of, module_of

            self.assertEqual(label_of(first), f"{module}.SampleTests.test_first")
            self.assertEqual(label_of(second), f"{module}.SampleTests.test_second")
            self.assertEqual({module_of(first), module_of(second)}, {module})

            found = testmap.read(data, root)
            self.assertEqual(found.ran, frozenset({module}))
            self.assertEqual(found.tests_of("sample/code.py"), frozenset({module}))
