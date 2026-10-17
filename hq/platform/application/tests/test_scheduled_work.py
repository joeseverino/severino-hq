"""Scheduled work: declared once, done as a job, asked for by the shipped units."""

import re
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from hq.domains.control_plane.bridge_actions import ACTIONS
from hq.domains.jobs import runner
from hq.domains.jobs.models import Job
from hq.domains.jobs.runner import Failed

from .. import scheduled_work
from ..scheduled_work import SCHEDULED, ScheduledWork

UNITS = Path(settings.BASE_DIR) / "deploy" / "systemd"
INSTANCE = re.compile(r"^(?:Unit|OnFailure)=severino-hq-job@(?P<name>.+)\.service$", re.MULTILINE)


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


class ShippedUnitTests(TestCase):
    """The shipped units and the declarations name the same work: a timer
    starts it on a schedule, or a failed unit starts it through OnFailure."""

    def asked_for(self) -> set[str]:
        return {
            match["name"]
            for unit in UNITS.rglob("*")
            if unit.is_file() and not unit.name.endswith(".example")
            for match in INSTANCE.finditer(unit.read_text())
        }

    def test_the_units_ask_for_exactly_the_work_without_a_cadence(self):
        self.assertEqual(self.asked_for(), {work.name for work in SCHEDULED if work.every is None})

    def test_work_with_a_cadence_is_asked_for_by_no_unit_of_its_own(self):
        self.assertTrue({work.name for work in SCHEDULED if work.every is not None} - {"due"})
        self.assertFalse(self.asked_for() & {work.name for work in SCHEDULED if work.every is not None})

    def test_each_timer_starts_work_of_its_own(self):
        started = [match["name"] for timer in UNITS.glob("*.timer") for match in INSTANCE.finditer(timer.read_text())]
        self.assertEqual(len(started), len(set(started)))

    def test_nothing_shipped_starts_a_process_in_the_web_container_for_it(self):
        for unit in UNITS.glob("*.service"):
            with self.subTest(unit=unit.name):
                self.assertNotIn("manage.py", unit.read_text())


class UnitFailureTests(TestCase):
    """A failed unit asks for the machine's units to be read, and for nothing else."""

    def test_the_read_is_asked_for_and_the_doorbell_rung(self):
        from hq.domains.control_plane.models import ReadRequest
        from hq.domains.control_plane.observations.host import UNIT_KIND

        from .. import cadence

        with mock.patch.object(cadence, "ring_doorbell", return_value=True) as doorbell:
            answer = scheduled_work.run("units.read")

        self.assertEqual(answer["state"], "succeeded")
        doorbell.assert_called_once_with()
        self.assertEqual(list(ReadRequest.objects.values_list("connection_ref", "kind")), [("", UNIT_KIND)])
        self.assertEqual(Job.objects.get(pk=answer["job"]).result, {"asked": UNIT_KIND, "rung": True})
        # The sweep it causes reads that kind alone, whatever the cadence says.
        with mock.patch.object(cadence, "sweep_interval", return_value=cadence.slowest_sweep_interval()):
            from hq.platform.application.inventory_testing import store

            store("host.firewall", {"record": "interface-binding"})
            verdict = cadence.sweep_due("example-controller")
        self.assertEqual((verdict["due"], verdict["only_kinds"]), (True, [UNIT_KIND]))

    def test_a_doorbell_that_cannot_be_rung_leaves_the_request_for_the_timer(self):
        from hq.domains.control_plane.models import ReadRequest

        from .. import cadence

        with mock.patch.object(cadence, "ring_doorbell", return_value=False):
            answer = scheduled_work.run("units.read")

        self.assertEqual(answer["state"], "succeeded")
        self.assertEqual(ReadRequest.objects.count(), 1)


class DueTests(TestCase):
    """The ``due`` timer does the host's work whose cadence has come round."""

    def schedule(self, run, every=timedelta(days=1)):
        due = next(work for work in SCHEDULED if work.name == "due")
        return mock.patch.object(
            scheduled_work,
            "SCHEDULED",
            (
                due,
                ScheduledWork("example.work", "Example work", run, every=every),
                ScheduledWork("example.asked", "Asked", run),
            ),
        )

    def done(self, kind, ago):
        Job.objects.create(kind=kind, label=kind, state=Job.State.SUCCEEDED, finished_at=timezone.now() - ago)

    def test_work_never_done_is_done_and_work_a_unit_asks_for_is_not(self):
        run = mock.Mock(return_value={"read": 1})
        with self.schedule(run), mock.patch("hq.platform.application.outbound_work.due", return_value=()):
            answer = scheduled_work.run("due")

        run.assert_called_once()
        self.assertEqual(Job.objects.get(kind="due").result, {"due": 1, "ended": {"example.work": "succeeded"}})
        self.assertEqual(answer["state"], "succeeded")

    def test_work_done_within_its_cadence_is_left_alone(self):
        self.done("example.work", timedelta(hours=3))
        run = mock.Mock()
        with self.schedule(run), mock.patch("hq.platform.application.outbound_work.due", return_value=()):
            scheduled_work.run("due")

        run.assert_not_called()

    def test_the_hosts_own_work_declares_how_often_it_is_done(self):
        every = {work.name: work.every for work in SCHEDULED}

        self.assertEqual(every["contacts.inbox"], timedelta(hours=1))
        self.assertEqual(
            {every[name] for name in ("audit.prune", "content.sync", "registry.refresh", "sessions.clear")},
            {timedelta(days=1)},
        )
        self.assertIsNone(every["units.read"])


class CadenceDueTests(TestCase):
    """When work is due: one check, shared by the host's and the extensions' work."""

    def done(self, ago, state=Job.State.SUCCEEDED, request=None):
        Job.objects.create(
            kind="example.work", label="Example", state=state, request=request or {}, finished_at=timezone.now() - ago
        )

    def due(self, every=timedelta(hours=1), **kwargs):
        return runner.due("example.work", every, now=timezone.now(), **kwargs)

    def test_a_timer_a_few_minutes_early_still_finds_hourly_work_due(self):
        self.done(timedelta(minutes=55))

        self.assertTrue(self.due())

    def test_work_done_well_within_its_cadence_is_not_due(self):
        self.done(timedelta(minutes=30))

        self.assertFalse(self.due())

    def test_a_failure_is_not_the_work_done(self):
        self.done(timedelta(minutes=5), state=Job.State.FAILED)

        self.assertTrue(self.due())

    def test_a_subject_narrows_to_the_work_about_it(self):
        self.done(timedelta(minutes=5), request={"subject": "other"})

        self.assertTrue(self.due(subject=""))
        self.assertFalse(self.due(subject="other"))
