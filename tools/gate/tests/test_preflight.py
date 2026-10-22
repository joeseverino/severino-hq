"""Preflight's reads of GitHub, where the shell contracts cannot reach without a clock."""

import json
import subprocess
import time
from contextlib import redirect_stderr
from io import StringIO
from unittest import mock

from django.test import tag

from tools.gate import preflight
from tools.gate.tests.base import GateTestCase
from tools.pipeline import ci_report

SHA = "a" * 40
REPOSITORY = "example-owner/example-repo"


class FakeApi:
    """CI on SHA, in progress until it has been read `finish_after` times."""

    def __init__(self, finish_after: int):
        self.finish_after = finish_after
        self.reads = 0

    def __call__(self, args, env, stdin):
        path = args[-1]
        if "/check-runs" in path:
            return json.dumps([{"check_runs": []}])
        self.reads += 1
        status = "completed" if self.reads > self.finish_after else "in_progress"
        run = {
            "id": 11,
            "path": preflight.CI,
            "event": "pull_request",
            "status": status,
            "conclusion": "success" if status == "completed" else None,
            "created_at": "2026-10-19T10:00:00Z",
        }
        return json.dumps([{"workflow_runs": [run]}])


@tag("source")
class SettleTests(GateTestCase):
    def github(self, finish_after: int) -> preflight.GitHub:
        github = preflight.GitHub(SHA, REPOSITORY)
        github.api = ci_report.GitHub(None, REPOSITORY, run=FakeApi(finish_after))
        return github

    def test_a_watch_gh_refuses_is_waited_between_and_then_refused_in_its_words(self):
        github = self.github(finish_after=100)
        refused = subprocess.CompletedProcess([], 1, "", "HTTP 401: Bad credentials (https://api.github.com)\n")
        waits: list[float] = []

        with (
            mock.patch.object(preflight.subprocess, "run", return_value=refused),
            mock.patch.object(preflight.time, "sleep", waits.append),
            redirect_stderr(StringIO()) as err,
        ):
            settled = github.settle(time.monotonic() + 3600, 30)

        self.assertFalse(settled)
        self.assertIn("Bad credentials", err.getvalue())
        self.assertTrue(waits)
        self.assertEqual(set(waits), {30})

    def test_a_watch_that_works_is_not_held_against_the_run(self):
        github = self.github(finish_after=preflight.WATCH_FAILURES * 2)
        watched = subprocess.CompletedProcess([], 0, "", "")

        with (
            mock.patch.object(preflight.subprocess, "run", return_value=watched),
            mock.patch.object(preflight.time, "sleep", self.fail),
        ):
            self.assertTrue(github.settle(time.monotonic() + 3600, 30))
