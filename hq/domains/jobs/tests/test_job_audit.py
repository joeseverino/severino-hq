"""A job in the audit log: one row, naming it, saying how it ended, attributed."""

from django.contrib.auth import get_user_model
from django.test import TestCase

from hq.domains.jobs.runner import run
from hq.platform.core.audit import ROUTINE_EVENTS, record_event
from hq.platform.core.models import AuditLog

# Written out, so the module imports at a ref that has neither name.
BACKGROUND_JOB = "Background job"
SCHEDULER = "timer"


def _rows(job) -> list[AuditLog]:
    return list(AuditLog.objects.filter(object_type=BACKGROUND_JOB, object_id=str(job.pk)))


def _changes_something(progress) -> dict:
    record_event(action=AuditLog.Action.UPDATED, type_label="example.record", message="Changed one")
    return {}


class OneRowPerJobTests(TestCase):
    def test_a_job_is_one_row_naming_what_it_is(self):
        job = run("example.sweep", "Sweep the examples", _changes_something, actor=SCHEDULER)

        (row,) = _rows(job)
        # Named by what it is: a state in its name would be stale by the time it is read.
        self.assertEqual(row.object_repr, "Sweep the examples")
        self.assertNotIn(str(job.pk), row.message)
        self.assertEqual(row.message, row.message.strip())

    def test_a_run_that_changed_something_ends_done_above_what_it_changed(self):
        job = run("example.sweep", "Sweep the examples", _changes_something, actor=SCHEDULER)

        (row,) = _rows(job)
        self.assertEqual(row.action, "done")
        changed = AuditLog.objects.get(object_type="example.record")
        self.assertEqual(changed.operation_id, row.operation_id)

    def test_a_scheduled_run_that_changed_nothing_is_routine(self):
        job = run("example.check", "Check the examples", lambda progress: {"seen": 4}, actor=SCHEDULER)

        (row,) = _rows(job)
        self.assertIn((row.action, row.object_type), ROUTINE_EVENTS)
        self.assertIsNone(row.user)

    def test_a_run_someone_asked_for_is_never_routine(self):
        person = get_user_model().objects.create_user("operator", password="x" * 14)
        asked = [
            run("example.mine", "Mine", lambda progress: {}, actor="operator", requested_by=person),
            run("example.agents", "An agent's", lambda progress: {}, actor="example-agent"),
        ]

        for job in asked:
            (row,) = _rows(job)
            self.assertNotIn((row.action, row.object_type), ROUTINE_EVENTS, job.label)
            self.assertEqual(row.action, "done")

    def test_the_row_says_who_asked(self):
        person = get_user_model().objects.create_user("operator", password="x" * 14)
        cases = (
            (run("example.a", "A", lambda progress: {}, actor="operator", requested_by=person), "operator"),
            (run("example.b", "B", lambda progress: {}, actor="example-agent"), "example-agent"),
            (run("example.c", "C", lambda progress: {}, actor=SCHEDULER), SCHEDULER),
        )

        for job, who in cases:
            (row,) = _rows(job)
            self.assertEqual(row.actor_label, who, job.label)
        self.assertEqual(_rows(cases[0][0])[0].user, person)

    def test_the_row_records_each_fact_once(self):
        job = run("example.import", "Import", lambda progress: {"created": 3, "seen": 5, "source": "example.csv"})

        (row,) = _rows(job)
        # The counts are their facet's; the rest of the result sits beside them.
        self.assertEqual(row.metadata["counts"], {"created": 3, "seen": 5})
        self.assertEqual(row.metadata["source"], "example.csv")
        self.assertFalse({"created", "seen"} & set(row.metadata))
        # The job is the row's object and its kind the operation, each said once.
        self.assertEqual((row.object_id, row.metadata["operation"]), (str(job.pk), "example.import"))
        self.assertNotIn(str(job.pk), row.metadata.values())
        self.assertNotIn("example.import", [value for key, value in row.metadata.items() if key != "operation"])
