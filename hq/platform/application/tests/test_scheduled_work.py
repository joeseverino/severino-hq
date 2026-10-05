"""Scheduled work: declared once, done as a job, asked for by the shipped timers."""

from __future__ import annotations

import re
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.test import TestCase, TransactionTestCase

from hq.domains.control_plane.bridge_actions import ACTIONS
from hq.domains.jobs.models import Job
from hq.domains.jobs.runner import Failed

from .. import scheduled_work
from ..scheduled_work import SCHEDULED, ScheduledWork

UNITS = Path(settings.BASE_DIR) / "deploy" / "systemd"
INSTANCE = re.compile(r"^Unit=severino-hq-job@(?P<name>.+)\.service$", re.MULTILINE)


def declared(run) -> mock._patch:
    return mock.patch.object(scheduled_work, "SCHEDULED", (ScheduledWork("example.work", "Example work", run),))


class RunTests(TestCase):
    def test_work_is_done_as_a_job_and_answered_as_it_ended(self):
        with declared(lambda progress: {"read": 3}):
            answer = scheduled_work.run("example.work")
        job = Job.objects.get(pk=answer["job"])
        self.assertEqual((answer["name"], answer["state"]), ("example.work", "succeeded"))
        self.assertEqual((job.kind, job.actor, job.result), ("example.work", "timer", {"read": 3}))

    def test_work_that_says_why_it_failed_is_answered_with_the_sentence(self):
        def work(progress):
            raise Failed("The index did not answer.")

        with declared(work):
            answer = scheduled_work.run("example.work")
        self.assertEqual((answer["state"], answer["note"]), ("failed", "The index did not answer."))

    def test_work_already_under_way_is_not_done_twice(self):
        Job.objects.create(kind="example.work", label="Example work", state=Job.State.RUNNING)
        work = mock.Mock()
        with declared(work):
            answer = scheduled_work.run("example.work")
        work.assert_not_called()
        self.assertEqual(answer["state"], "running")
        self.assertNotIn("job", answer)

    def test_work_nobody_declared_is_refused(self):
        with self.assertRaisesMessage(ValueError, "No scheduled work named 'example.other'"):
            scheduled_work.run("example.other")
        self.assertFalse(Job.objects.exists())

    def test_the_bridge_answers_for_it(self):
        action = next(action for action in ACTIONS if action.name == "job")
        with declared(lambda progress: None):
            answer = action.run({"name": "example.work"}, None)
        self.assertEqual(answer["state"], "succeeded")


class StartTests(TransactionTestCase):
    def test_hq_starts_work_on_a_thread_of_its_own(self):
        with declared(lambda progress: {"read": 1}), mock.patch("hq.domains.jobs.runner.threading.Thread") as thread:
            self.assertTrue(scheduled_work.start("example.work"))
        thread.return_value.start.assert_called_once_with()
        self.assertEqual(Job.objects.get().state, Job.State.QUEUED)

    def test_starting_what_is_already_live_is_no(self):
        Job.objects.create(kind="example.work", label="Example work", state=Job.State.QUEUED)
        with declared(lambda progress: None):
            self.assertFalse(scheduled_work.start("example.work"))


class ShippedTimerTests(TestCase):
    """The timers and the declarations name the same work, each exactly once."""

    def asked_for(self) -> list[str]:
        return sorted(
            match["name"]
            for timer in UNITS.glob("*.timer")
            for match in INSTANCE.finditer(timer.read_text())
        )

    def test_every_timer_asks_for_declared_work_and_all_of_it_is_asked_for(self):
        self.assertEqual(self.asked_for(), sorted(work.name for work in SCHEDULED))

    def test_nothing_shipped_starts_a_process_in_the_web_container_for_it(self):
        for unit in UNITS.glob("*.service"):
            with self.subTest(unit=unit.name):
                self.assertNotIn("manage.py", unit.read_text())
