"""The base every gate tool's test inherits.

A gate keeps its closing verdict in `var/gate/`, where a reader whose copy of
the output was cut looks for it, so a test's fixture verdict there passes for
a real one. Each class keeps its verdicts in a folder of its own, named both
in this process and in the environment every subprocess it starts inherits.
A gate a test runs hands the tests it saw fail to the gate it runs under, so
the class runs outside the gate that runs the suite, whose verdict would
otherwise name a fixture's failures.
"""

import os
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import override
from unittest.mock import patch

from django.test import SimpleTestCase

from tools.gate import verdict as gate_verdict

# `python -m unittest` with the suite's failure record, so a gate reads the
# failed tests of a real loader and runner as it reads the suite's.
UNITTEST = """
import sys, unittest
from hq.platform.core.test_runner import FailureRecord, write_failures

class Result(FailureRecord, unittest.TextTestResult):
    pass

run = unittest.main(
    module=None, argv=["unittest", *sys.argv[1:]], exit=False, testRunner=unittest.TextTestRunner(resultclass=Result)
)
write_failures(run.result)
sys.exit(not run.result.wasSuccessful())
"""


IDENTITY = ["-c", "user.name=Example", "-c", "user.email=dev@example.com", "-c", "commit.gpgsign=false"]


def fixture_git(root: Path) -> Callable[..., str]:
    """git in the fixture repository at ``root``, committing as an example identity."""

    def git(*args: str) -> str:
        done = subprocess.run(["git", *IDENTITY, *args], cwd=root, check=True, capture_output=True, text=True)
        return done.stdout.strip()

    return git


class GateTestCase(SimpleTestCase):
    @classmethod
    @override
    def setUpClass(cls) -> None:
        super().setUpClass()
        kept = cls.enterClassContext(tempfile.TemporaryDirectory())
        cls.enterClassContext(patch.object(gate_verdict, "VERDICTS", Path(kept)))
        cls.enterClassContext(patch.dict(os.environ))
        os.environ[gate_verdict.VERDICTS_FOLDER] = kept
        os.environ.pop(gate_verdict.REPORTS, None)
        # What a gate shows is the same on a terminal, in CI and in an agent's shell.
        os.environ[gate_verdict.OUTPUT] = "short"

    @override
    def setUp(self) -> None:
        super().setUp()
        # A gate a test opens in this process is that test's alone.
        self.enterContext(patch.object(gate_verdict, "OPENED", None))
        self.addCleanup(lambda: gate_verdict.OPENED and gate_verdict.OPENED.shut())
