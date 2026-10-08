"""Jobs under test: recorded as a request leaves them, run when the test says.

A job's work runs on its own thread and its own database connection, which a
``TestCase`` transaction cannot be seen from. ``held_jobs`` records each job
as ``runner.start`` does and holds its work, so a test can assert what the
request answered before any of the work ran, then ``run`` it on the test's
connection.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from unittest import mock

from . import runner


class HeldJobs:
    def __init__(self) -> None:
        self.waiting: list[tuple[Any, Any]] = []

    def _thread(self, *, target: Any, args: tuple[Any, Any], **_named: Any) -> Any:
        self.waiting.append(args)
        return SimpleNamespace(start=lambda: None)

    def run(self) -> None:
        """Run every held job to its end, as its thread would have."""

        # Inside a request this would do the work the request was held from.
        serving = runner.serving_request()
        if serving:
            raise runner.OutboundInRequest(f"{serving} tried to run the jobs it started.")
        waiting, self.waiting = self.waiting, []
        # The test's connection is the only one that can see the job.
        with mock.patch.object(runner, "close_old_connections"), mock.patch.object(runner, "connection"):
            for job_id, work in waiting:
                runner._run(job_id, work)


@contextmanager
def held_jobs() -> Iterator[HeldJobs]:
    held = HeldJobs()
    with mock.patch.object(runner.threading, "Thread", held._thread):
        yield held
