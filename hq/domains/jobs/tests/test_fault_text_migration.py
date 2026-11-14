"""Migration 0006 leaves no stored job error holding a fault's own text, and a failed job shows its sentence whole."""

from types import SimpleNamespace

from django.test import SimpleTestCase
from django.utils import timezone

from hq.platform.application.work_runs import job_run, standing_of
from hq.platform.core.tests.migration_steps import MigrationStepTestCase

# A value the fault's text held, standing in for a credential.
HELD = "example-held-value-0xC0FFEE"
TRACEBACK = (
    "Traceback (most recent call last):\n"
    '  File "/app/hq/domains/jobs/runner.py", line 240, in _work\n'
    "    result = work(progress)\n"
    f"requests.exceptions.ConnectionError: https://user:{HELD}@example.com refused\n"
)
# A long traceback the old runner kept only the end of.
CUT = f'    progress()\n  File "/app/hq/work.py", line 9, in step\n    raise\nValueError: {HELD}\n'
SENTENCE = "The registry lists nothing for it."


class StoredFaultTextTests(MigrationStepTestCase):
    def test_each_stored_fault_becomes_a_sentence_and_a_sentence_stands(self):
        apps = self.migrate(("jobs", "0005_ids_are_time_ordered"))
        Job = apps.get_model("jobs", "Job")
        errors = {
            "traceback": TRACEBACK,
            "cut": CUT,
            "unstarted": f"Could not start: can't start new thread {HELD}",
            "sentence": SENTENCE,
        }
        for kind, error in errors.items():
            Job.objects.create(kind=f"example.{kind}", label=kind, state="failed", error=error)

        apps = self.migrate(("jobs", "0006_failures_hold_no_fault_text"))

        after = {row.kind.removeprefix("example."): row.error for row in apps.get_model("jobs", "Job").objects.all()}
        self.assertEqual(after["sentence"], SENTENCE)
        self.assertEqual(after["traceback"], "It stopped on an unexpected ConnectionError.")
        self.assertEqual(after["cut"], "It stopped on an unexpected ValueError.")
        self.assertEqual(after["unstarted"], "HQ could not start it; its log holds why.")
        for error in after.values():
            self.assertNotIn(HELD, error)
            self.assertNotIn("\n", error)


class StandingTests(SimpleTestCase):
    def test_a_failed_job_shows_its_whole_sentence(self):
        said = "Two hosts refused the change.\nRetry after the maintenance window."
        job = SimpleNamespace(
            pk=1,
            kind="example.work",
            seen_state="failed",
            seen_error=said,
            note="",
            created_at=timezone.now(),
            started_at=None,
            finished_at=None,
            duration_seconds=None,
            percent=None,
        )

        self.assertEqual(standing_of(job_run(job)).note, said)
