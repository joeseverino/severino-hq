"""A browser gate's stall report carries Playwright's own log, and only then."""

import io
import os
import unittest.mock
from pathlib import Path

from django.test import SimpleTestCase

from hq.platform.core.test_runner import StallWatch
from hq.platform.core.tests.browser import gate


class _Driver:
    """``sync_playwright()``: a driver that writes its debug log where its
    environment says when it starts, as Playwright's does."""

    def start(self):
        if path := os.environ.get("DEBUG_FILE"):
            Path(path).write_text(f"pw:browser <launching> chromium ({os.environ['DEBUG']})\n", encoding="utf-8")
        return self


class DriverLogTests(SimpleTestCase):
    def started(self, seconds):
        """The watch a class's driver started under, its class cleanups due at the end of the test."""

        watch = StallWatch(seconds)
        test = self

        class Owner:
            @classmethod
            def addClassCleanup(cls, function, *args):
                test.addCleanup(function, *args)

        with unittest.mock.patch.object(gate, "stall_watch", watch):
            gate.start_driver(Owner, _Driver)
        return watch

    def test_a_watched_run_shows_the_drivers_log_in_its_report(self):
        watch = self.started(60)
        stream = io.StringIO()
        for evidence in watch.evidence:
            evidence(stream)

        self.assertIn("<launching> chromium", stream.getvalue())
        self.assertNotIn("DEBUG_FILE", os.environ)

    def test_an_unwatched_run_logs_nothing(self):
        self.assertEqual(self.started(None).evidence, [])
