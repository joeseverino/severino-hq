"""What a job has to get right for anything to be trusted to it."""

from __future__ import annotations

import threading
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from core.models import AuditLog

from .models import HEARTBEAT_GRACE, Job
from .runner import JobConflict, Progress, reap, start


class JobRunnerTests(TransactionTestCase):
    """TransactionTestCase because the work runs on another thread.

    A `TestCase` wraps each test in a transaction that the worker thread's own
    connection cannot see, so the job would look like it had never been
    created. This is the failure that makes people give up on testing threads
    and assert nothing instead.
    """

    def wait(self, job, seconds=10):
        deadline = timezone.now() + timedelta(seconds=seconds)
        while timezone.now() < deadline:
            job.refresh_from_db()
            if not job.is_live:
                return job
        raise AssertionError(f"job stayed {job.state}")

    def test_the_result_the_work_returns_is_what_the_job_reports(self):
        job = self.wait(start("test.ok", "Fine", lambda progress: {"rows": 7}))

        self.assertEqual(job.state, Job.State.SUCCEEDED)
        self.assertEqual(job.result, {"rows": 7})
        self.assertEqual(job.percent, 100)

    def test_a_raising_job_records_the_traceback_rather_than_vanishing(self):
        def explode(progress):
            raise ValueError("the archive was not where it said")

        job = self.wait(start("test.boom", "Boom", explode))

        self.assertEqual(job.state, Job.State.FAILED)
        # The message and the line that raised it. An exception on a thread is
        # otherwise printed to a stream nobody is reading and the row sits at
        # `running` forever.
        self.assertIn("the archive was not where it said", job.error)
        self.assertIn("explode", job.error)

    def test_two_of_a_kind_cannot_run_at_once(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        first = start("test.solo", "First", lambda progress: gate.wait(5) and {})

        with self.assertRaises(JobConflict):
            start("test.solo", "Second", lambda progress: {})

        gate.set()
        self.wait(first)

    def test_a_finished_job_frees_its_kind(self):
        self.wait(start("test.serial", "First", lambda progress: {}))
        second = self.wait(start("test.serial", "Second", lambda progress: {}))

        self.assertEqual(second.state, Job.State.SUCCEEDED)

    def test_how_it_ended_is_audited_and_attributed(self):
        user = get_user_model().objects.create_user("operator", password="x" * 14)
        job = self.wait(
            start("test.audited", "Audited", lambda p: {"rows": 3}, requested_by=user)
        )

        entry = AuditLog.objects.filter(
            action=AuditLog.Action.IMPORTED, object_id=str(job.pk)
        ).first()
        self.assertIsNotNone(entry, "a finished job must say so in the audit log")
        # Attributed. There is no request on a worker thread, so an audit row
        # written without being told the user records the work as having been
        # done by nobody.
        self.assertEqual(entry.user, user)


class ProgressTests(TestCase):
    def test_a_new_note_is_never_dropped(self):
        """The limit suppresses repetition, not progress.

        Every step of a long job reports once as it begins, and consecutive
        steps can begin within the same window. Rate-limiting those away makes
        the panel claim to still be doing the first thing -- indistinguishable,
        from outside, from the job having hung.
        """
        job = Job.objects.create(kind="test.notes", label="Notes")
        progress = Progress(job)

        progress("fingerprinting", 2)
        progress("reading the sessions", 5)  # immediately after, and different

        job.refresh_from_db()
        self.assertEqual(job.note, "reading the sessions")
        self.assertEqual(job.percent, 5)

    def test_the_same_note_repeated_is_rate_limited(self):
        job = Job.objects.create(kind="test.repeat", label="Repeat")
        progress = Progress(job)

        progress("counting", 10)
        progress("counting", 90)  # same note, inside the window: dropped

        job.refresh_from_db()
        self.assertEqual(job.percent, 10)

    def test_force_writes_a_repeated_note_anyway(self):
        # `force` is for the caller who must know the row was touched -- the
        # last report of a run, or a heartbeat during a step long enough to
        # look dead. It overrides the window; it is not how ordinary progress
        # gets through, which is why a changed note does not need it.
        job = Job.objects.create(kind="test.rate", label="Rate")
        progress = Progress(job)

        progress("counting", 10)
        progress("counting", 20)  # same note, inside the window: dropped
        job.refresh_from_db()
        self.assertEqual(job.percent, 10)

        progress("counting", 30, force=True)
        job.refresh_from_db()
        self.assertEqual(job.percent, 30)

    def test_progress_does_not_flood_the_audit_log(self):
        # Written with `update`, which fires no post_save. A job reporting
        # every few seconds for two minutes would otherwise leave sixty audit
        # rows saying nothing, and bury the ones that mean something.
        job = Job.objects.create(kind="test.quiet", label="Quiet")
        before = AuditLog.objects.count()

        for index in range(5):
            Progress(job)(f"step {index}", index * 10, force=True)

        self.assertEqual(AuditLog.objects.count(), before)


class ReapTests(TestCase):
    def test_a_job_whose_process_stopped_is_marked_lost(self):
        stale = timezone.now() - HEARTBEAT_GRACE - timedelta(minutes=1)
        job = Job.objects.create(
            kind="test.reap",
            label="Gone",
            state=Job.State.RUNNING,
            started_at=stale,
            heartbeat_at=stale,
        )

        self.assertEqual(reap(), 1)
        job.refresh_from_db()
        self.assertEqual(job.state, Job.State.LOST)
        # And it says so. Nothing was watching when this died -- that is what
        # being lost means -- so the reaper writes the only record there will
        # ever be of how it ended.
        self.assertTrue(
            AuditLog.objects.filter(
                action=AuditLog.Action.FAILED, object_id=str(job.pk)
            ).exists(),
            "a lost job must leave a record of having ended",
        )

    def test_a_slow_job_that_is_still_beating_is_left_alone(self):
        job = Job.objects.create(
            kind="test.slow",
            label="Working",
            state=Job.State.RUNNING,
            started_at=timezone.now() - timedelta(hours=1),
            heartbeat_at=timezone.now(),
        )

        self.assertEqual(reap(), 0)
        job.refresh_from_db()
        self.assertEqual(job.state, Job.State.RUNNING)

    def test_a_lost_job_stops_blocking_its_kind(self):
        # The reason reap exists at all: the live-job constraint is what stops
        # two imports running, so a job that died holding it would lock the
        # kind out until somebody edited the database.
        stale = timezone.now() - HEARTBEAT_GRACE - timedelta(minutes=1)
        Job.objects.create(
            kind="test.blocked",
            label="Gone",
            state=Job.State.RUNNING,
            started_at=stale,
            heartbeat_at=stale,
        )
        reap()

        Job.objects.create(kind="test.blocked", label="Next")  # must not raise


class JobStatusViewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("watcher", password="x" * 14)
        self.client.force_login(self.user)

    def test_status_reports_what_a_watching_page_needs(self):
        job = Job.objects.create(
            kind="test.status", label="Reading", state=Job.State.RUNNING,
            percent=42, note="halfway", started_at=timezone.now(),
        )

        payload = self.client.get(reverse("jobs:status", args=[job.pk])).json()

        self.assertEqual(payload["percent"], 42)
        self.assertEqual(payload["note"], "halfway")
        self.assertTrue(payload["live"])

    def test_status_requires_a_session(self):
        job = Job.objects.create(kind="test.private", label="Private")
        self.client.logout()

        response = self.client.get(reverse("jobs:status", args=[job.pk]))

        self.assertEqual(response.status_code, 302)
