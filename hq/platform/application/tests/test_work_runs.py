"""HQ's jobs and its automation runs are one list: filtered, ordered and paged
in the database, each row said in the words and tones a job is. Every
procedure here is a synthetic ``example.*`` one."""

from dataclasses import replace
from datetime import timedelta

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from hq.domains.control_plane.models import OperationRequest
from hq.domains.jobs.models import HEARTBEAT_GRACE, Job, JobState
from hq.platform.core.tests.estate_sizes import at_both_sizes

from ..capabilities import run_capability
from ..controller import ControllerReport, claim_next_operation, report_operation
from ..resource_operations import operation_summary
from ..security import Capability, Principal
from ..work_runs import (
    BY_TITLE,
    FAILED_FIRST,
    JOB,
    LAPSED,
    OUTCOME_TONES,
    RUN,
    TONES,
    hydrated,
    job_seen_state,
    recent_runs,
    runs_on,
    work_kinds,
    work_rows,
)
from .procedures import APP, DB, EDGE, RESTART, RESTART_RUN, Supply, manifest, run_payload

AGENT = Principal("example-agent", "mcp", frozenset({Capability.READ, Capability.MANAGE_INFRASTRUCTURE}))
LOGS = "example.service_logs"
WORKER = "example-worker"
CLAIMS = (("automation.operation", "run"),)


class WorkRunsTestCase(TestCase):
    def setUp(self):
        self.supply = Supply(self, manifest(), manifest(id=LOGS))
        self.keys = iter(range(1000))

    def run_of(self, *hosts, name=RESTART_RUN, principal=AGENT, **fields):
        answer = run_capability(
            name, run_payload(*hosts, **fields), principal=principal, idempotency_key=f"example-{next(self.keys)}"
        ).answer
        self.assertTrue(answer["ok"], answer)
        return OperationRequest.objects.get(pk=answer["operation"]["id"])

    def claimed(self, run):
        claim = claim_next_operation(WORKER, capabilities=CLAIMS)["operation"]
        self.assertEqual(claim["id"], str(run.pk))
        run.refresh_from_db()
        return run

    def reported(self, run, *, success=True, events=()):
        self.claimed(run)
        report_operation(
            str(run.pk),
            ControllerReport(
                success=success, observed_generation=run.input["generation"], message="Done.", events=list(events)
            ),
            controller_id=WORKER,
        )
        run.refresh_from_db()
        return run

    def job(self, state=JobState.SUCCEEDED, **fields):
        return Job.objects.create(**{"kind": "example.work", "label": "Example work", "state": state, **fields})

    def listed(self, **query):
        return hydrated(work_rows(**query))


class OneListTests(WorkRunsTestCase):
    def test_jobs_and_runs_are_one_list_newest_first(self):
        job = self.job()
        run = self.run_of(APP)
        Job.objects.filter(pk=job.pk).update(created_at=timezone.now() - timedelta(hours=1))

        rows = self.listed()

        self.assertEqual([(row.source, row.id) for row in rows], [(RUN, str(run.pk)), (JOB, str(job.pk))])

    def test_a_run_s_row_is_its_operation_s_summary(self):
        run = self.reported(
            self.run_of(APP, DB, correlation="example-investigation-7"),
            events=[{"host": APP, "task": "Restart", "outcome": "changed", "message": ""}],
        )

        (row,) = self.listed()

        summary = operation_summary(run)
        self.assertEqual((row.by, row.approved_by, row.note), (summary["by"], summary["approved_by"], "Done."))
        self.assertEqual((row.word, row.tone), (JobState.SUCCEEDED.label, TONES[JobState.SUCCEEDED]))
        self.assertEqual(row.hosts, (APP, DB))
        self.assertEqual(row.correlation, "example-investigation-7")
        self.assertEqual(row.events[0]["outcome"], "changed")
        self.assertEqual(row.kind, RESTART_RUN)
        self.assertIn(RESTART, row.title)
        self.assertEqual((row.started, row.finished), (run.claimed_at, run.completed_at))

    def test_an_approved_run_names_who_approved_it(self):
        run = self.run_of(APP, principal=replace(AGENT, approved_by="example-owner"))

        (row,) = self.listed()

        self.assertEqual((row.id, row.approved_by), (str(run.pk), "example-owner"))

    def test_a_job_s_row_is_its_standing(self):
        from ..asks import job_standing

        job = self.job(JobState.FAILED, error="The registry lists nothing.")

        (row,) = self.listed()

        standing = job_standing(job)
        self.assertEqual((row.word, row.note), (standing.label, standing.note))
        self.assertEqual(row.tone, TONES[JobState.FAILED])

    def test_a_held_run_is_not_listed(self):
        from hq.domains.control_plane.models import ApprovalRequest

        ApprovalRequest.objects.create(
            capability=RESTART_RUN,
            content_fingerprint="0" * 64,
            requested_actor="example-agent",
            requested_interface="mcp",
            expires_at=timezone.now() + timedelta(days=1),
        )

        self.assertEqual(self.listed(), [])

    def test_only_runs_are_listed_of_the_operations(self):
        run = self.run_of(APP)
        OperationRequest.objects.create(
            resource=run.resource, action=OperationRequest.Action.RECONCILE, idempotency_key="example-reconcile"
        )

        self.assertEqual([row.id for row in self.listed()], [str(run.pk)])


class SameWordsTests(WorkRunsTestCase):
    def test_a_run_tone_agrees_with_its_operation_summary(self):
        """``TONES`` and ``operation_summary`` give a run in each plain state one tone."""

        run = self.run_of(APP)
        self.assertEqual(operation_summary(run)["tone"], TONES[JobState.QUEUED])
        self.assertEqual(operation_summary(self.claimed(run))["tone"], TONES[JobState.RUNNING])
        report_operation(
            str(run.pk),
            ControllerReport(success=True, observed_generation=run.input["generation"], message="Done."),
            controller_id=WORKER,
        )
        run.refresh_from_db()
        self.assertEqual(operation_summary(run)["tone"], TONES[JobState.SUCCEEDED])
        failed = self.reported(self.run_of(DB, name=f"automation.{LOGS}.run"), success=False)
        self.assertEqual(operation_summary(failed)["tone"], TONES[JobState.FAILED])

    def test_a_cancelled_run_stopped_early(self):
        run = self.run_of(APP)
        OperationRequest.objects.filter(pk=run.pk).update(state="cancelled", completed_at=timezone.now())

        (row,) = self.listed()

        self.assertEqual((row.state, row.word, row.tone), (JobState.LOST, "Stopped early", TONES[JobState.LOST]))

    def test_a_run_whose_lease_ran_out_stopped_early_before_the_next_claim_ends_it(self):
        run = self.claimed(self.run_of(APP))
        OperationRequest.objects.filter(pk=run.pk).update(lease_expires_at=timezone.now() - timedelta(seconds=1))

        (row,) = self.listed()
        (stopped,) = self.listed(state=(JobState.LOST,))

        self.assertEqual((row.word, row.note), ("Stopped early", LAPSED))
        self.assertEqual(stopped.id, str(run.pk))
        self.assertEqual(self.listed(state=(JobState.RUNNING,)), [])

    def test_a_working_run_is_running(self):
        self.claimed(self.run_of(APP))

        (row,) = self.listed(state=(JobState.RUNNING,))

        self.assertEqual(row.word, "Running")

    def test_the_sql_seen_state_is_the_job_s_own(self):
        """``job_seen_state`` filters by what ``Job.seen_state`` says, for every case it reads."""

        now = timezone.now()
        long_ago = now - HEARTBEAT_GRACE - timedelta(minutes=1)
        jobs = [
            self.job(JobState.SUCCEEDED),
            self.job(JobState.FAILED),
            Job.objects.create(kind="example.a", label="A", state=JobState.RUNNING, heartbeat_at=now),
            Job.objects.create(kind="example.b", label="B", state=JobState.RUNNING, heartbeat_at=long_ago),
            Job.objects.create(kind="example.c", label="C", state=JobState.RUNNING),
            Job.objects.create(kind="example.d", label="D", state=JobState.QUEUED),
        ]
        stale = Job.objects.create(kind="example.e", label="E", state=JobState.QUEUED)
        Job.objects.filter(pk=stale.pk).update(created_at=long_ago)
        jobs.append(stale)

        seen = dict(Job.objects.annotate(seen=job_seen_state(timezone.now())).values_list("pk", "seen"))

        for job in jobs:
            job.refresh_from_db()
            with self.subTest(job=job.label):
                self.assertEqual(seen[job.pk], job.seen_state)


class FilterTests(WorkRunsTestCase):
    def test_a_machine_s_runs_are_the_runs_that_named_it(self):
        on_app = self.reported(self.run_of(APP, DB))
        on_edge = self.run_of(EDGE)
        self.job()

        self.assertEqual([row.id for row in runs_on(DB)], [str(on_app.pk)])
        self.assertEqual({row.id for row in self.listed(host=(APP,))}, {str(on_app.pk)})
        self.assertEqual([row.id for row in runs_on(EDGE)], [str(on_edge.pk)])

    def test_an_operation_s_recent_runs_are_its_own(self):
        restart = self.reported(self.run_of(APP))
        self.run_of(DB, name=f"automation.{LOGS}.run")

        self.assertEqual([row.id for row in recent_runs(RESTART)], [str(restart.pk)])

    def test_a_kind_lists_its_jobs_or_its_runs(self):
        job = self.job()
        run = self.run_of(APP)

        self.assertEqual([row.id for row in self.listed(kind=("example.work",))], [str(job.pk)])
        self.assertEqual([row.id for row in self.listed(kind=(RESTART_RUN,))], [str(run.pk)])
        self.assertEqual(self.listed(kind=("automation.example.unknown.run",)), [])

    def test_a_state_filters_both_sources(self):
        failed_job = self.job(JobState.FAILED)
        failed_run = self.reported(self.run_of(APP), success=False)
        self.job()

        self.assertEqual(
            {(row.source, row.id) for row in self.listed(state=(JobState.FAILED,))},
            {(JOB, str(failed_job.pk)), (RUN, str(failed_run.pk))},
        )

    def test_a_search_reads_a_job_s_label_and_a_run_s_procedure(self):
        job = self.job(label="Example import")
        run = self.run_of(APP)

        self.assertEqual([row.id for row in self.listed(search="import")], [str(job.pk)])
        self.assertEqual([row.id for row in self.listed(search="service_restart")], [str(run.pk)])

    def test_a_page_is_cut_in_the_database(self):
        for _ in range(3):
            self.job(kind=f"example.{next(self.keys)}")
        rows = work_rows()

        self.assertEqual(rows.count(), 3)
        self.assertEqual(len(hydrated(rows[1:3])), 2)

    def test_the_oldest_first_and_by_state_orders(self):
        first = self.job(JobState.SUCCEEDED, kind="example.first")
        Job.objects.filter(pk=first.pk).update(created_at=timezone.now() - timedelta(hours=2))
        second = self.job(JobState.FAILED, kind="example.second")

        self.assertEqual([row.id for row in self.listed(order="created_at")], [str(first.pk), str(second.pk)])
        self.assertEqual([row.state for row in self.listed(order="state")], ["failed", "succeeded"])


class ManyValuesTests(WorkRunsTestCase):
    def test_a_filter_of_several_values_keeps_what_matches_any(self):
        failed = self.job(JobState.FAILED, kind="example.failed")
        lost = self.job(JobState.LOST, kind="example.lost")
        self.job(kind="example.done")

        listed = {row.id for row in self.listed(state=(JobState.FAILED, JobState.LOST))}
        kinds = {row.id for row in self.listed(kind=("example.failed", "example.lost"))}

        self.assertEqual(listed, {str(failed.pk), str(lost.pk)})
        self.assertEqual(kinds, listed)

    def test_a_run_on_two_of_the_machines_asked_for_is_one_row(self):
        run = self.run_of(APP, DB)

        self.assertEqual([row.id for row in self.listed(host=(APP, DB))], [str(run.pk)])


class OrderTests(WorkRunsTestCase):
    def ago(self, record, hours):
        type(record).objects.filter(pk=record.pk).update(created_at=timezone.now() - timedelta(hours=hours))
        return record

    def test_what_ended_badly_on_its_kind_s_latest_run_leads(self):
        recovered = self.ago(self.job(JobState.FAILED, kind="example.sync"), 5)
        self.ago(self.job(kind="example.sync"), 1)
        failed = self.ago(self.job(JobState.FAILED, kind="example.read"), 3)
        failed_run = self.ago(self.reported(self.run_of(APP), success=False), 4)
        newest = self.job(kind="example.other")

        order = [row.id for row in self.listed(order=FAILED_FIRST)]

        self.assertEqual(order[:3], [str(failed.pk), str(failed_run.pk), str(newest.pk)])
        self.assertGreater(order.index(str(recovered.pk)), 2)

    def test_a_run_that_failed_before_a_later_one_does_not_lead(self):
        self.ago(self.reported(self.run_of(APP), success=False), 4)
        later = self.reported(self.run_of(APP))
        job = self.ago(self.job(), 1)

        self.assertEqual([row.id for row in self.listed(order=FAILED_FIRST)][:2], [str(later.pk), str(job.pk)])

    def test_a_to_z_reads_what_each_is_called(self):
        zebra = self.job(label="zebra work", kind="example.zebra")
        alpha = self.job(label="Alpha work", kind="example.alpha")
        run = self.run_of(APP)

        self.assertEqual([row.id for row in self.listed(order=BY_TITLE)], [str(alpha.pk), str(run.pk), str(zebra.pk)])

    def test_a_declared_job_is_called_by_its_declaration_and_other_work_by_what_it_stored(self):
        from ..scheduled_work import named

        declared = self.job(kind="sessions.clear", label="sessions.clear")
        gone = self.job(kind="example.gone", label="")

        titles = {row.id: row.title for row in self.listed()}

        self.assertEqual(titles, {str(declared.pk): named("sessions.clear").label, str(gone.pk): "example.gone"})


class WorkKindsTests(WorkRunsTestCase):
    def test_each_kind_is_named_by_its_latest_run(self):
        Job.objects.filter(pk=self.job(JobState.FAILED, label="Old name").pk).update(
            created_at=timezone.now() - timedelta(hours=1)
        )
        self.job(label="Example work")
        self.reported(self.run_of(APP), success=False)

        kinds = {found.kind: (found.title, found.state) for found in work_kinds()}

        self.assertEqual(kinds["example.work"], ("Example work", JobState.SUCCEEDED))
        self.assertEqual(kinds[RESTART_RUN][1], JobState.FAILED)
        self.assertIn(RESTART, kinds[RESTART_RUN][0])
        self.assertEqual([row.title for row in self.listed(kind=(RESTART_RUN,))], [kinds[RESTART_RUN][0]])


class EvidenceTests(WorkRunsTestCase):
    def test_a_claimed_run_names_its_worker_and_its_lease_and_a_finished_one_neither(self):
        run = self.claimed(self.run_of(APP))

        (working,) = self.listed()
        report_operation(
            str(run.pk),
            ControllerReport(success=True, observed_generation=run.input["generation"], message="Done."),
            controller_id=WORKER,
        )
        (done,) = self.listed()

        self.assertEqual((working.worker, working.lease_until), (WORKER, run.lease_expires_at))
        self.assertEqual((done.worker, done.lease_until), ("", None))

    def test_each_task_outcome_is_said_in_a_tone(self):
        outcomes = ("ok", "changed", "skipped", "failed", "unreachable")
        self.reported(
            self.run_of(APP),
            events=[
                {"host": APP, "task": f"Task {outcome}", "outcome": outcome, "message": ""} for outcome in outcomes
            ],
        )

        (row,) = self.listed()

        self.assertEqual(set(OUTCOME_TONES), set(outcomes))
        self.assertEqual([event["tone"] for event in row.events], [OUTCOME_TONES[outcome] for outcome in outcomes])
        self.assertEqual(set(OUTCOME_TONES.values()) - set(TONES.values()), set())


class WorkRunsBudgetTests(TestCase):
    """A page of work costs the same queries however much has run."""

    def _queries(self, _seeded) -> int:
        with CaptureQueriesContext(connection) as page:
            rows = work_rows()
            self.assertGreater(rows.count(), 0)
            listed = hydrated(rows[:40])
            on = runs_on("lab-1")
        self.assertEqual({row.source for row in listed} | {row.source for row in on}, {JOB, RUN})
        return len(page)

    def test_a_page_and_a_machine_s_runs_cost_the_same_at_both_sizes(self):
        small, large = at_both_sizes(self._queries)

        self.assertEqual(large, small)
