"""Open problems on the page of the thing they are about, and since when."""

from unittest import mock

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from .. import first_seen
from ..problems import note_open_problems
from ..report_testing import bridged
from .host_only import host_only

REWRITE = "adguard.rewrite"


class FirstSeenTests(TestCase):
    def setUp(self):
        first_seen._looked.clear()

    def test_a_report_is_followed_by_a_look_only_when_one_is_due(self):
        with host_only():
            self.assertTrue(note_open_problems())
            self.assertFalse(note_open_problems())
            first_seen._looked.clear()
            # Another process: the stored look still says none is due.
            with CaptureQueriesContext(connection) as asked:
                self.assertFalse(note_open_problems())

        # The read of the stored look, and no look, which derives the queue and records it.
        self.assertLessEqual(len(asked), 1)

    def test_a_look_that_fails_does_not_lose_the_report(self):
        with (
            host_only(),
            mock.patch("hq.platform.application.problems.note_open_problems", side_effect=RuntimeError("no")),
            self.assertLogs("severino.sweep", level="ERROR"),
        ):
            result = bridged("inventory", {REWRITE: {"ok": True, "records": []}})

        self.assertTrue(result["ok"])
