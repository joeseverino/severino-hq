"""check --remote's reads of GitHub, where the shell contracts cannot reach without a clock."""

import json
import subprocess
import time
from contextlib import redirect_stderr
from io import StringIO
from unittest import mock

from django.test import tag

from hq.platform.core.tests.task import RefError
from tools.gate import check
from tools.gate.tests.base import GateTestCase

SHA = "a" * 40
REPOSITORY = "example-owner/example-repo"


class FakeApi:
    """CI on SHA, in progress until it has been read `finish_after` times."""

    def __init__(self, finish_after: int):
        self.finish_after = finish_after
        self.reads = 0

    def __call__(self, args):
        path = args[-1]
        if "/check-runs" in path:
            return json.dumps([{"check_runs": []}])
        self.reads += 1
        status = "completed" if self.reads > self.finish_after else "in_progress"
        run = {
            "id": 11,
            "path": check.CI,
            "event": "pull_request",
            "status": status,
            "conclusion": "success" if status == "completed" else None,
            "created_at": "2026-10-19T10:00:00Z",
        }
        return json.dumps([{"workflow_runs": [run]}])


@tag("source")
class SettleTests(GateTestCase):
    def github(self, finish_after: int) -> check.GitHub:
        github = check.GitHub(SHA, REPOSITORY)
        github.api = check.API(FakeApi(finish_after))
        return github

    def test_a_watch_gh_refuses_is_waited_between_and_then_refused_in_its_words(self):
        github = self.github(finish_after=100)
        refused = subprocess.CompletedProcess([], 1, "", "HTTP 401: Bad credentials (https://api.github.com)\n")
        waits: list[float] = []

        with (
            mock.patch.object(check.subprocess, "run", return_value=refused),
            mock.patch.object(check.time, "sleep", waits.append),
            redirect_stderr(StringIO()) as err,
        ):
            settled = github.settle(time.monotonic() + 3600, 30)

        self.assertFalse(settled)
        self.assertIn("Bad credentials", err.getvalue())
        self.assertTrue(waits)
        self.assertEqual(set(waits), {30})

    def test_a_watch_that_works_is_not_held_against_the_run(self):
        github = self.github(finish_after=check.WATCH_FAILURES * 2)
        watched = subprocess.CompletedProcess([], 0, "", "")

        with (
            mock.patch.object(check.subprocess, "run", return_value=watched),
            mock.patch.object(check.time, "sleep", self.fail),
        ):
            self.assertTrue(github.settle(time.monotonic() + 3600, 30))


def _repository(answers: dict[str, list[str] | None]):
    """A git that answers each command by its arguments, refusing one answered None."""

    def git(*args: str) -> list[str]:
        answer = answers[" ".join(args)]
        if answer is None:
            raise RefError("no upstream configured")
        return answer

    return git


@tag("source")
class CheckedCommitTests(GateTestCase):
    def checked(self, answers: dict[str, list[str] | None]) -> str:
        with mock.patch.object(check, "git", _repository(answers)):
            return check.checked_commit()

    def test_a_head_a_remote_branch_holds_is_the_commit_checked(self):
        self.assertEqual(self.checked({"rev-parse HEAD": [SHA], f"branch -r --contains {SHA}": ["origin/main"]}), SHA)

    def test_an_unpushed_head_is_read_through_the_upstream_tip_holding_its_tree(self):
        tip = "b" * 40
        answers = {
            "rev-parse HEAD": [SHA],
            f"branch -r --contains {SHA}": [],
            "rev-parse @{upstream}": [tip],
            "rev-parse HEAD^{tree}": ["c" * 40],
            f"rev-parse {tip}^{{tree}}": ["c" * 40],
        }

        self.assertEqual(self.checked(answers), tip)

    def test_an_upstream_tip_with_another_tree_or_no_upstream_checks_nothing(self):
        tip = "b" * 40
        another = {
            "rev-parse HEAD": [SHA],
            f"branch -r --contains {SHA}": [],
            "rev-parse @{upstream}": [tip],
            "rev-parse HEAD^{tree}": ["c" * 40],
            f"rev-parse {tip}^{{tree}}": ["d" * 40],
        }
        none = {"rev-parse HEAD": [SHA], f"branch -r --contains {SHA}": [], "rev-parse @{upstream}": None}
        for case, answers in (("another tree", another), ("no upstream", none)):
            with self.subTest(case):
                self.assertEqual(self.checked(answers), "")
