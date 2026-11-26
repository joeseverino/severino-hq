"""HQ's jobs and its automation runs are one list: filtered, ordered and paged
in the database, each row said in the words and tones a job is. Every
procedure here is a synthetic ``example.*`` one."""

from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from hq.domains.control_plane.models import OperationRequest
from hq.domains.jobs.models import HEARTBEAT_GRACE, Job, JobState
from hq.platform.core.testing import AGENT, operator, run_ended
from hq.platform.core.tests.estate_sizes import at_both_sizes

from ..controller import ControllerReport, claim_next_operation, report_operation
from ..resource_operations import operation_summary
from ..resources import list_resource
from ..security import Principal, operator_capabilities
from ..work_runs import (
    JOB,
    NEWEST,
    RUN,
    STOPPED_REPORTING,
    TaskOutcome,
    hydrated,
    job_seen_state,
    runs_on,
    work,
    work_kinds,
)
from .procedures import APP, APPROVER, DB, RESTART, RESTART_RUN, Supply, approved_run, manifest, run_payload

LOGS = "example.service_logs"
WORKER = "example-worker"
CLAIMS = (("automation.operation", "run"),)
# What asks the automation worker for a scan, and what it rings on commit.
SCAN = OperationRequest.Action.SCAN
DOORBELL = "hq.platform.application.cadence._ring"
READER = Principal("example-reader", "test", operator_capabilities())


class WorkRunsTestCase(TestCase):
    def setUp(self):
        self.supply = Supply(self, manifest(), manifest(id=LOGS))
        self.keys = iter(range(1000))

    def run_of(self, *hosts, name=RESTART_RUN, **fields):
        answer = approved_run(
            self, run_payload(*hosts, **fields), principal=AGENT, key=f"example-{next(self.keys)}", name=name
        )
        self.assertTrue(answer["ok"], answer)
        return OperationRequest.objects.get(pk=answer["operation"]["id"])

    def claimed(self, run):
        claim = claim_next_operation(WORKER, capabilities=CLAIMS)["operation"]
        self.assertEqual(claim["id"], str(run.pk))
        run.refresh_from_db()
        return run

    def reported(self, run, *, success=True, events=(), message="Done."):
        self.claimed(run)
        run_ended(str(run.pk), WORKER, events=events, success=success, message=message)
        run.refresh_from_db()
        return run

    def job(self, state=JobState.SUCCEEDED, **fields):
        return Job.objects.create(**{"kind": "example.work", "label": "Example work", "state": state, **fields})

    def listed(self):
        return hydrated(work().kept().order_by(*NEWEST))

    def read(self, **query):
        """What the list read answers for ``query``, newest first."""

        asked = {"sort": "-created_at", "limit": 100, **query}
        return list_resource("work", asked, principal=READER, fields=["*"])["items"]


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

        summary = operation_summary(row, run)
        self.assertEqual((row.by.words, row.by.approved_by, row.note), (summary["by"], summary["approved_by"], "Done."))
        self.assertEqual((row.word, row.tone), (JobState.SUCCEEDED.label, JobState.SUCCEEDED.tone))
        self.assertEqual(row.evidence.hosts, (APP, DB))
        self.assertEqual(row.evidence.correlation, "example-investigation-7")
        self.assertEqual(row.evidence.events[0]["outcome"], "changed")
        self.assertEqual(row.kind, RESTART_RUN)
        self.assertIn(RESTART, row.title)
        self.assertEqual((row.started, row.finished), (run.claimed_at, run.completed_at))

    def test_a_run_s_message_is_shown_as_it_was_written_whatever_it_holds(self):
        from ..service_context import Cell

        said = "example: registry.example.com/app:1@sha256:" + "0f" * 32 + " moved"
        self.reported(self.run_of(APP), message=said)

        (row,) = self.listed()

        self.assertEqual(Cell(row.note).text, said)

    def test_who_started_each_job_and_asked_each_run_is_read_at_once(self):

        from hq.domains.jobs.runner import AUDIT_TYPE, run as run_job
        from hq.platform.core.models import AuditLog

        from ..history import asked_by, who_did

        asked = self.run_of(APP)
        person = operator()
        job = run_job("example.work", "Example work", lambda progress: {}, requested_by=person)

        with CaptureQueriesContext(connection) as sent:
            rows = {row.source: row for row in self.listed()}

        self.assertEqual(rows[JOB].by, who_did((AUDIT_TYPE,), [job.pk])[str(job.pk)])
        self.assertEqual(rows[RUN].by, asked_by(OperationRequest, [asked])[str(asked.pk)])
        self.assertNotEqual(rows[JOB].by, rows[RUN].by)
        self.assertEqual(len([query for query in sent if AuditLog._meta.db_table in query["sql"]]), 1)

    def test_an_approved_run_names_who_approved_it(self):
        run = self.run_of(APP)

        (row,) = self.listed()

        self.assertEqual((row.id, row.by.approved_by), (str(run.pk), APPROVER))

    def test_a_job_s_row_is_its_standing(self):
        from ..work_runs import standing_of

        said = "Two hosts refused the change.\nRetry after the maintenance window."
        self.job(JobState.FAILED, error=said)

        (row,) = self.listed()

        standing = standing_of(row)
        self.assertEqual((row.word, row.note), (standing.label, said))
        self.assertEqual(row.tone, JobState.FAILED.tone)

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
    def test_a_run_in_each_plain_state_takes_that_state_s_tone(self):
        """A run waiting, under way, done and failed is toned as a job in that state is."""

        def tone(run):
            return next(row.tone for row in self.listed() if row.id == str(run.pk))

        run = self.run_of(APP)
        self.assertEqual(tone(run), JobState.QUEUED.tone)
        self.assertEqual(tone(self.claimed(run)), JobState.RUNNING.tone)
        report_operation(
            str(run.pk),
            ControllerReport(success=True, observed_generation=run.input["generation"], message="Done."),
            controller_id=WORKER,
        )
        self.assertEqual(tone(run), JobState.SUCCEEDED.tone)
        failed = self.reported(self.run_of(DB, name=f"automation.{LOGS}.run"), success=False)
        self.assertEqual(tone(failed), JobState.FAILED.tone)

    def test_a_run_not_yet_ended_says_only_its_state(self):
        """Its State column says Waiting or Running; its Result has nothing yet."""

        run = self.run_of(APP)
        (row,) = self.listed()
        self.assertEqual((row.id, row.word, row.note), (str(run.pk), JobState.QUEUED.label, ""))

        self.claimed(run)
        (row,) = self.listed()
        self.assertEqual((row.word, row.note), (JobState.RUNNING.label, ""))

    def test_a_cancelled_run_stopped_early(self):
        run = self.run_of(APP)
        OperationRequest.objects.filter(pk=run.pk).update(state="cancelled", completed_at=timezone.now())

        (row,) = self.listed()

        self.assertEqual((row.state, row.word, row.tone), (JobState.LOST, "Stopped early", JobState.LOST.tone))

    def test_a_run_whose_lease_ran_out_stopped_early_before_the_next_claim_ends_it(self):
        run = self.claimed(self.run_of(APP))
        OperationRequest.objects.filter(pk=run.pk).update(lease_expires_at=timezone.now() - timedelta(seconds=1))

        (row,) = self.listed()
        (stopped,) = self.read(state=JobState.LOST)

        self.assertEqual((row.word, row.note), ("Stopped early", STOPPED_REPORTING))
        self.assertEqual(stopped["id"], str(run.pk))
        self.assertEqual(self.read(state=JobState.RUNNING), [])

    def test_a_working_run_is_running(self):
        self.claimed(self.run_of(APP))

        (row,) = self.read(state=JobState.RUNNING)

        self.assertEqual(row["state_label"], "Running")

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


class OrderTests(WorkRunsTestCase):
    def ago(self, record, hours):
        type(record).objects.filter(pk=record.pk).update(created_at=timezone.now() - timedelta(hours=hours))
        return record

    def test_a_run_that_failed_before_a_later_one_does_not_lead(self):
        self.ago(self.reported(self.run_of(APP), success=False), 4)
        later = self.reported(self.run_of(APP))
        job = self.ago(self.job(), 1)

        self.assertEqual([row["id"] for row in self.read(sort="failed")][:2], [str(later.pk), str(job.pk)])

    def test_a_declared_job_is_called_by_its_declaration_and_other_work_by_what_it_stored(self):
        from ..outbound_work import declared_work

        declared = self.job(kind="sessions.clear", label="sessions.clear")
        gone = self.job(kind="example.gone", label="")

        titles = {row.id: row.title for row in self.listed()}

        self.assertEqual(
            titles, {str(declared.pk): declared_work()["sessions.clear"].label, str(gone.pk): "example.gone"}
        )

    def test_a_run_is_called_by_its_playbook_s_title_and_found_by_it(self):
        """The title the playbook was adopted with, on every list of work; a
        run of a playbook no longer adopted keeps its id."""

        restarted = self.run_of(APP)
        adopted = (SimpleNamespace(id=RESTART, title="Restart a service"),)
        with mock.patch("hq.platform.application.automation_adoption.procedures", return_value=adopted):
            (row,) = self.listed()
            (found,) = self.read(query="restart a serv")
            (machine_row,) = runs_on(APP)
        self.assertEqual((row.id, row.title, found["id"]), (str(restarted.pk), "Restart a service", str(restarted.pk)))
        self.assertEqual(machine_row.title, "Restart a service")
        with mock.patch("hq.platform.application.automation_adoption.procedures", return_value=()):
            (row,) = self.listed()
        self.assertIn(RESTART, row.title)


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
        self.assertEqual([row["title"] for row in self.read(kind=RESTART_RUN)], [kinds[RESTART_RUN][0]])


class ScanTests(WorkRunsTestCase):
    """A scan HQ asks for is listed with the rest of the automation work,
    named by its action."""

    def scanned(self):
        from ..automation_sources import queue_scan
        from ..security import internal_principal

        with mock.patch(DOORBELL), self.captureOnCommitCallbacks(execute=True):
            scan = queue_scan(principal=internal_principal("controller"), once="example-scan")
        self.assertIsNotNone(scan)
        return scan

    def test_a_scan_is_listed_by_its_action_s_words(self):
        scan = self.scanned()
        title = SCAN.label
        run = self.run_of(APP)

        listed = {row.id: row for row in self.listed()}

        self.assertEqual(set(listed), {str(scan.pk), str(run.pk)})
        row = listed[str(scan.pk)]
        self.assertEqual((row.source, row.kind, row.title), (RUN, SCAN, title))
        self.assertEqual((row.word, row.evidence.hosts), (JobState.QUEUED.label, ()))

    def test_a_scan_is_a_kind_of_work_the_list_is_filtered_by(self):
        scan = self.scanned()
        self.run_of(APP)

        kinds = {found.kind: found for found in work_kinds()}

        self.assertIn(SCAN, kinds)
        self.assertEqual([row["id"] for row in self.read(kind=SCAN)], [str(scan.pk)])
        self.assertEqual([row["kind"] for row in self.read(kind=RESTART_RUN)], [RESTART_RUN])
        self.assertNotIn(str(scan.pk), [row["id"] for row in self.read(host=APP)])


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

        self.assertEqual((working.evidence.worker, working.evidence.lease_until), (WORKER, run.lease_expires_at))
        self.assertEqual((done.evidence.worker, done.evidence.lease_until), ("", None))

    def test_each_task_outcome_is_said_in_a_tone(self):
        outcomes = ("started", "ok", "changed", "skipped", "failed", "unreachable")
        self.reported(
            self.run_of(APP),
            events=[
                {"host": APP, "task": f"Task {outcome}", "outcome": outcome, "message": ""} for outcome in outcomes
            ],
        )

        (row,) = self.listed()

        self.assertEqual(set(TaskOutcome), set(outcomes))
        self.assertEqual([event["tone"] for event in row.evidence.events], [outcome.tone for outcome in TaskOutcome])
        self.assertEqual({outcome.tone for outcome in TaskOutcome} - {state.tone for state in JobState}, set())


class WorkRunsBudgetTests(TestCase):
    """A page of work costs the same queries however much has run."""

    def _queries(self, _seeded) -> int:
        with CaptureQueriesContext(connection) as page:
            listed = list_resource("work", {"limit": 40}, principal=READER)["items"]
            on = runs_on("lab-1")
        self.assertEqual({row["source"] for row in listed} | {row.source for row in on}, {JOB, RUN})
        return len(page)

    def test_a_page_and_a_machine_s_runs_cost_the_same_at_both_sizes(self):
        small, large = at_both_sizes(self._queries)

        self.assertEqual(large, small)
