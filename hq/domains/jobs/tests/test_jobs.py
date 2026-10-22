"""What a job has to get right for anything to be trusted to it."""

import threading
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from hq.platform.application.tests.markup import text
from hq.platform.core.models import AuditLog

from ..models import HEARTBEAT_GRACE, Job
from ..runner import Failed, JobConflict, Progress, reap, run, start


class JobRunnerTests(TransactionTestCase):
    """TransactionTestCase because the work runs on another thread.

    A `TestCase` wraps each test in a transaction that the worker thread's own
    connection cannot see, so the job would look like it had never been
    created.
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

    def test_a_job_is_recorded_against_a_person_or_nobody(self):
        from django.contrib.auth.models import AnonymousUser

        person = get_user_model().objects.create_user("operator", password="unused-password")

        signed_in = self.wait(start("test.person", "Person", lambda progress: {}, requested_by=person))
        signed_out = self.wait(start("test.nobody", "Nobody", lambda progress: {}, requested_by=AnonymousUser()))

        self.assertEqual(signed_in.requested_by, person)
        self.assertIsNone(signed_out.requested_by)

    def test_a_job_started_inside_a_transaction_runs_once_it_commits(self):
        from django.db import transaction

        with transaction.atomic():
            job = start("test.later", "Later", lambda progress: {"rows": 1})
            # Not yet: its thread could not see the row.
            self.assertEqual(Job.objects.get(pk=job.pk).state, Job.State.QUEUED)

        self.assertEqual(self.wait(job).state, Job.State.SUCCEEDED)

    def test_a_thread_that_cannot_start_ends_the_job_failed(self):
        from unittest import mock

        with (
            mock.patch("hq.domains.jobs.runner.threading.Thread.start", side_effect=RuntimeError("no threads")),
            self.assertRaises(RuntimeError),
        ):
            start("test.nothread", "No thread", lambda progress: {})

        job = Job.objects.get(kind="test.nothread")
        self.assertEqual(job.state, Job.State.FAILED)
        self.assertIn("no threads", job.error)
        # The kind is free again: a job that never ran does not hold it.
        self.assertEqual(self.wait(start("test.nothread", "Again", lambda progress: {})).state, Job.State.SUCCEEDED)

    def test_a_database_failure_is_not_called_a_conflict(self):
        from unittest import mock

        from django.db import IntegrityError

        with (
            mock.patch.object(Job.objects, "create", side_effect=IntegrityError("disk I/O error")),
            self.assertRaises(IntegrityError),
        ):
            start("test.broken", "Broken", lambda progress: {})

    def test_a_queued_job_whose_process_stopped_is_reaped(self):
        job = Job.objects.create(kind="test.orphan", label="Orphan")
        Job.objects.filter(pk=job.pk).update(created_at=timezone.now() - HEARTBEAT_GRACE - timedelta(seconds=1))

        self.assertEqual(reap(), 1)

        job.refresh_from_db()
        self.assertEqual(job.state, Job.State.LOST)
        # And the kind can run again.
        self.assertEqual(self.wait(start("test.orphan", "Again", lambda progress: {})).state, Job.State.SUCCEEDED)

    def test_how_it_ended_is_audited_and_attributed(self):
        user = get_user_model().objects.create_user("operator", password="x" * 14)
        job = self.wait(start("test.audited", "Audited", lambda p: {"rows": 3}, requested_by=user))

        entry = AuditLog.objects.filter(action=AuditLog.Action.IMPORTED, object_id=str(job.pk)).first()
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
        the panel claim to still be doing the first thing: indistinguishable,
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
        # `force` is for the caller who must know the row was touched: the
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
        # And it says so. Nothing was watching when this died (that is what
        # being lost means) so the reaper writes the only record there will
        # ever be of how it ended.
        self.assertTrue(
            AuditLog.objects.filter(action=AuditLog.Action.FAILED, object_id=str(job.pk)).exists(),
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
            kind="test.status",
            label="Reading",
            state=Job.State.RUNNING,
            percent=42,
            note="halfway",
            started_at=timezone.now(),
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


class RunOnTheCallingThreadTests(TestCase):
    """A command a timer runs has no thread that outlives it."""

    def test_the_job_has_ended_when_run_returns(self):
        job = run("test.inline", "Inline", lambda progress: {"seen": 3}, actor="timer")

        self.assertEqual((job.state, job.result, job.percent), ("succeeded", {"seen": 3}, 100))
        ended = AuditLog.objects.filter(operation_id=str(job.pk), action=AuditLog.Action.IMPORTED).get()
        self.assertEqual(ended.metadata["counts"], {"seen": 3})

    def test_it_takes_the_same_slot_as_a_started_job(self):
        Job.objects.create(kind="test.inline", label="Already", state=Job.State.RUNNING)

        with self.assertRaises(JobConflict):
            run("test.inline", "Inline", lambda progress: {})

    def test_a_request_may_not_run_a_job_itself(self):
        from django.test import RequestFactory

        from hq.platform.core.outbound import OutboundInRequest, serving

        ran = []
        with serving(RequestFactory().post("/example/")), self.assertRaises(OutboundInRequest):
            run("test.inline", "Inline", lambda progress: ran.append(1) or {})

        self.assertEqual((ran, Job.objects.count()), ([], 0))

    def test_a_state_is_called_the_same_by_a_control_the_list_and_a_read(self):
        from hq.platform.application.asks import DONE, FAILED, QUEUED, RUNNING, Standing, job_standing

        for state in Job.State:
            job = Job(kind="test.words", label="Words", state=state, created_at=timezone.now())
            self.assertEqual(job_standing(job).label, job.get_state_display())
        for state in (QUEUED, RUNNING, DONE, FAILED):
            self.assertIn(Standing(state).label, Job.State.labels)

    def test_failed_is_the_sentence_the_control_says(self):
        from hq.platform.application.asks import job_standing

        def work(progress):
            raise Failed("The registry lists nothing for it.")

        job = run("test.inline", "Inline", work)

        self.assertEqual((job.state, job.error), ("failed", "The registry lists nothing for it."))
        self.assertEqual(job_standing(job).note, "The registry lists nothing for it.")
        failed = AuditLog.objects.filter(operation_id=str(job.pk), action=AuditLog.Action.FAILED).get()
        self.assertEqual(
            failed.metadata["failure"], {"message": "The registry lists nothing for it.", "kind": "Failed"}
        )

    def test_any_other_exception_keeps_its_traceback(self):
        def work(progress):
            raise OSError("connection reset")

        job = run("test.inline", "Inline", work)

        self.assertEqual(job.state, "failed")
        self.assertIn("Traceback", job.error)


class JobListOrderTests(TestCase):
    """What failed on its last run leads the list and is named above it."""

    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser("owner", "owner@example.test", "pw"))
        now = timezone.now()

        def ran(kind, label, state, hours_ago):
            job = Job.objects.create(kind=kind, label=label, state=state)
            # Dated after the insert: the column stamps itself on create.
            Job.objects.filter(pk=job.pk).update(created_at=now - timedelta(hours=hours_ago))
            return job

        self.recovered = ran("example.sync", "Sync the example", Job.State.FAILED, 5)
        ran("example.sync", "Sync the example", Job.State.SUCCEEDED, 1)
        self.failed = ran("example.read", "Read the example", Job.State.FAILED, 3)
        self.lost = ran("example.index", "Index the example", Job.State.LOST, 4)
        self.newest = ran("example.other", "Do the other thing", Job.State.SUCCEEDED, 0)

    def test_a_job_that_failed_last_leads_and_a_recovered_one_does_not(self):
        page = self.client.get(reverse("jobs:list"))

        order = [job.pk for job in page.context["object_list"]]
        self.assertEqual(order[:3], [self.failed.pk, self.lost.pk, self.newest.pk])
        self.assertGreater(order.index(self.recovered.pk), 2)
        self.assertContains(page, "2 jobs failed on their last run")
        self.assertContains(page, "Index the example, Read the example")

    def test_newest_first_is_still_one_choice_away(self):
        page = self.client.get(reverse("jobs:list"), {"sort": "-created_at"})

        self.assertEqual(page.context["object_list"][0].pk, self.newest.pk)

    def test_nothing_is_said_when_nothing_failed_last(self):
        Job.objects.exclude(state=Job.State.SUCCEEDED).delete()

        self.assertNotContains(self.client.get(reverse("jobs:list")), "on its last run")


class JobNameTests(TestCase):
    """A job is called what its declaration calls it; its kind is an identifier
    nobody reading the list is shown."""

    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser("owner", "owner@example.test", "pw"))

    def test_a_declared_job_reads_by_its_declared_name_whatever_its_run_stored(self):
        from hq.platform.application.scheduled_work import named

        Job.objects.create(kind="sessions.clear", label="sessions.clear", state=Job.State.SUCCEEDED, actor="timer")

        page = text(self.client.get(reverse("jobs:list")))

        self.assertIn(named("sessions.clear").label, page)
        self.assertNotIn("sessions.clear", page)

    def test_work_no_longer_declared_keeps_the_name_its_run_stored(self):
        Job.objects.create(kind="example.gone", label="Example work", state=Job.State.SUCCEEDED, actor="timer")

        page = text(self.client.get(reverse("jobs:list")))

        self.assertIn("Example work", page)
        self.assertNotIn("example.gone", page)

    def test_jobs_and_the_audit_log_link_to_each_other(self):
        jobs = self.client.get(reverse("jobs:list"))
        audit = self.client.get(reverse("core:audit_list"))

        # In the page's own head, not only the navigation.
        self.assertIn(reverse("core:audit_list"), [action.url for action in jobs.context["page"].actions])
        self.assertIn(reverse("jobs:list"), [action.url for action in audit.context["page"].actions])
