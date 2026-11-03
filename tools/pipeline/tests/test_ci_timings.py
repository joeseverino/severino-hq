"""A run's timings name the chain of jobs that set its length."""

import io
import json
from unittest import mock

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


def step(name, start, end):
    return {"name": name, "conclusion": "success", "startedAt": start, "completedAt": end}


class EndLagTests(SimpleTestCase):
    """A job's end lag is the platform's time after its last step, read apart from the steps' own."""

    def test_a_job_marked_done_after_its_last_step_says_by_how_long(self):
        # Scan from a recorded run: its last step ends at 06:19:23, the job at 06:19:25.
        scan = step("Scan", "2026-10-31T06:16:32Z", "2026-10-31T06:19:25Z") | {
            "steps": [
                step("Set up job", "2026-10-31T06:16:32Z", "2026-10-31T06:16:34Z"),
                step("Complete job", "2026-10-31T06:19:23Z", "2026-10-31T06:19:23Z"),
            ]
        }
        found = ci_timings.jobs({"jobs": [scan]})
        self.assertEqual(found[0].end_lag, 2)
        self.assertIn("end lag 2s", ci_timings.report(found))

    def test_a_job_with_no_step_that_ran_has_no_lag(self):
        image = next(job for job in ci_timings.jobs(RECORD) if job.name == "Image")
        self.assertEqual(image.end_lag, 0)


class RunWordTests(SimpleTestCase):
    def test_a_word_that_is_not_a_run_id_is_refused_before_gh_runs(self):
        for words in (["--web"], ["123", "-R"], ["12a"], ["١٢"], []):
            with (
                self.subTest(words=words),
                mock.patch.object(ci_timings.subprocess, "run") as run,
                mock.patch("sys.stderr", io.StringIO()) as said,
            ):
                self.assertEqual(ci_timings.main(words), 2)
                run.assert_not_called()
                self.assertIn("ci_timings", said.getvalue())

    def test_several_runs_each_get_a_report_under_their_id(self):
        answer = mock.Mock(returncode=0, stdout=json.dumps(RECORD))
        out = io.StringIO()
        with mock.patch.object(ci_timings.subprocess, "run", return_value=answer), mock.patch("sys.stdout", out):
            self.assertEqual(ci_timings.main(["101", "202"]), 0)
        self.assertIn("Run 101", out.getvalue())
        self.assertIn("Run 202", out.getvalue())
        self.assertEqual(out.getvalue().count("Critical path:"), 2)
