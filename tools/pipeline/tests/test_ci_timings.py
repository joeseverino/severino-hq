"""A run's timings name the chain of jobs that set its length."""

from django.test import SimpleTestCase

from tools.pipeline import ci_timings


def job(name, start, end, conclusion="success", steps=()):
    return {
        "name": name,
        "conclusion": conclusion,
        "startedAt": f"2026-10-21T04:{start}Z",
        "completedAt": f"2026-10-21T04:{end}Z",
        "steps": list(steps),
    }


# The shape `gh run view --json jobs` returns: a pull request's run, where the
# composition waits for the image and the verdict waits for everything.
RECORD = {
    "jobs": [
        job("Checks", "00:54", "02:15", steps=[job("Checks", "01:20", "02:12")]),
        job("Image", "00:54", "02:40"),
        job("Tests (3.15)", "00:54", "03:57"),
        job("Proven on its pull request", "00:51", "00:51", conclusion="skipped"),
        job("Composition / Build HQ", "02:43", "08:55", steps=[job("Skipped step", "03:00", "03:00", "skipped")]),
        job("Ready", "08:59", "09:08"),
    ]
}


class CriticalPathTests(SimpleTestCase):
    def test_the_path_runs_back_through_the_job_each_one_waited_for(self):
        path = ci_timings.critical_path(ci_timings.jobs(RECORD))
        self.assertEqual([job.name for job in path], ["Image", "Composition / Build HQ", "Ready"])

    def test_a_skipped_job_or_step_is_not_timed(self):
        found = ci_timings.jobs(RECORD)
        self.assertNotIn("Proven on its pull request", [job.name for job in found])
        composition = next(job for job in found if job.name.startswith("Composition"))
        self.assertEqual(composition.steps, ())

    def test_the_report_gives_each_step_and_the_path_its_wall_clock(self):
        text = ci_timings.report(ci_timings.jobs(RECORD))
        self.assertIn("52s  Checks", text)
        self.assertIn("= 494s", text)
