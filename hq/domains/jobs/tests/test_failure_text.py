"""A failed job's record says that it failed and how to find out why, never the fault's own text.

The fault's text can carry anything the work held: a credential, a path, a
value someone sent. Its page, its API answer and its audit row show the
record; the traceback is in HQ's log alone.
"""

import json
from unittest import mock

from django.test import TestCase

from hq.platform.application.work_runs import job_run
from hq.platform.core.models import AuditLog

from ..models import Job
from ..runner import run, start
from ..testing import held_jobs

# A value the work held when it failed, standing in for a credential.
HELD = "example-held-value-0xC0FFEE"


def _audited(job: Job) -> str:
    rows = AuditLog.objects.filter(operation_id=str(job.pk), action=AuditLog.Action.FAILED)
    return json.dumps([(row.summary, row.metadata) for row in rows], default=str)


class FaultTests(TestCase):
    def test_a_fault_is_recorded_by_its_kind_and_its_trace_is_logged(self):
        def work(progress):
            raise ConnectionError(f"https://user:{HELD}@example.com refused")

        with self.assertLogs("severino.jobs", "ERROR") as logged:
            job = run("test.fault", "Fault", work)

        self.assertEqual(job.state, Job.State.FAILED)
        self.assertIn("ConnectionError", job.error)
        self.assertNotIn(HELD, job.error)
        self.assertNotIn("Traceback", job.error)
        self.assertEqual(job_run(job).note, job.error)
        self.assertIn('"kind": "ConnectionError"', _audited(job))
        self.assertNotIn(HELD, _audited(job))
        self.assertIn(HELD, "\n".join(logged.output))
        self.assertIn("Traceback", "\n".join(logged.output))

    def test_a_thread_that_cannot_start_keeps_its_reason_to_the_log(self):
        with (
            held_jobs(),
            mock.patch("hq.domains.jobs.runner.threading.Thread") as thread,
            self.assertRaises(RuntimeError),
            self.assertLogs("severino.jobs", "ERROR") as logged,
        ):
            thread.return_value.start.side_effect = RuntimeError(HELD)
            start("test.unstarted", "Unstarted", lambda progress: {})

        job = Job.objects.get(kind="test.unstarted")
        self.assertEqual(job.state, Job.State.FAILED)
        self.assertTrue(job.error)
        self.assertNotIn(HELD, job.error)
        self.assertNotIn(HELD, _audited(job))
        self.assertIn(HELD, "\n".join(logged.output))
