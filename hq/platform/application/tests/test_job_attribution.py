"""A plugin's job writes its rows as whoever asked for it, through nothing but
the SDK: a job started for a person, run to its end, names that person."""

from django.test import TestCase

from hq.platform.core.models import AuditLog
from hq_sdk.audit import record_operation
from hq_sdk.capabilities import Principal
from hq_sdk.jobs import start
from hq_sdk.testing import held_jobs

ASKER = Principal("example-person", "web", frozenset())
KIND = "example.import"


def _start(work) -> str:
    """Start ``work`` as a plugin's adapter does for ``ASKER``, and run it to its end."""

    with held_jobs() as jobs:
        job = start(KIND, "Example import", work, actor=ASKER.actor)
        jobs.run()
    return str(job.pk)


class AJobsRowsAreItsRequestersTests(TestCase):
    def test_rows_a_job_writes_name_the_person_who_asked(self):
        def work(progress):
            record_operation(KIND, "Imported the example", action=AuditLog.Action.UPDATED)
            record_operation(KIND, "Imported the rest", action=AuditLog.Action.UPDATED)
            return {}

        job = _start(work)

        rows = AuditLog.objects.filter(operation_id=job, object_type=KIND)
        self.assertEqual(rows.count(), 2)
        self.assertEqual({row.metadata["actor"] for row in rows}, {ASKER.actor})
