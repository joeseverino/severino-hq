"""The suite's output shows a test's logs when that test fails, and only then."""

from __future__ import annotations

import io
import logging
import unittest
from contextlib import redirect_stderr

from django.test import SimpleTestCase

from core.test_runner import CompositionTextResult

logger = logging.getLogger("severino.test_held_logs")


def _case(fails: bool):
    # Built here rather than at module level, where the suite would collect it
    # and run a test that fails on purpose.
    class Sample(unittest.TestCase):
        def test_sample(self):
            if fails:
                logger.warning("the clue a failing test left")
                self.fail("deliberately")
            logger.warning("expected noise from a passing test")

    return Sample


def _run(case) -> str:
    captured = io.StringIO()
    result = CompositionTextResult(io.StringIO(), descriptions=False, verbosity=0)
    root = logging.getLogger()
    handlers = list(root.handlers)
    # The sample's own level, so the run's SEVERINO_LOG_LEVEL cannot filter it.
    level = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        with redirect_stderr(captured):
            unittest.defaultTestLoader.loadTestsFromTestCase(case).run(result)
    finally:
        root.handlers = handlers
        logger.setLevel(level)
    return captured.getvalue()


class HeldLogsTests(SimpleTestCase):
    def test_a_passing_test_prints_nothing(self):
        self.assertNotIn("expected noise", _run(_case(fails=False)))

    def test_a_failing_test_prints_its_logs_and_names_itself(self):
        output = _run(_case(fails=True))
        self.assertIn("the clue a failing test left", output)
        self.assertIn("Sample.test_sample", output)
