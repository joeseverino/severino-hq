"""A job whose process stopped: a page says so without writing it, and the
next job of its kind is not held up by it."""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from hq.platform.application.tests.markup import text
from hq.platform.core.models import AuditLog

from ..models import HEARTBEAT_GRACE, Job
from ..runner import run


def stopped(kind: str = "example.gone") -> Job:
    """A running job nothing has heard from for longer than the grace."""

    stale = timezone.now() - HEARTBEAT_GRACE - timedelta(minutes=1)
    return Job.objects.create(
        kind=kind, label="Example gone", state=Job.State.RUNNING, started_at=stale, heartbeat_at=stale
    )


class StaleJobPageTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser("owner", "owner@example.test", "pw"))
        self.job = stopped()

    def assertUnchanged(self, before: int) -> None:
        self.job.refresh_from_db()
        self.assertEqual(self.job.state, Job.State.RUNNING, "a GET leaves the job as it was")
        self.assertEqual(AuditLog.objects.count(), before, "a GET records nothing")

    def test_the_list_shows_it_stopped_early_and_failed_last_without_writing(self):
        before = AuditLog.objects.count()

        page = self.client.get(reverse("jobs:list"))

        self.assertEqual(page.status_code, 200)
        self.assertIn(Job.State.LOST.label, text(page))
        self.assertEqual(page.context["failed_last"], ["Example gone"])
        self.assertUnchanged(before)

    def test_its_status_says_it_stopped_without_writing(self):
        before = AuditLog.objects.count()

        payload = self.client.get(reverse("jobs:status", args=[self.job.pk])).json()

        self.assertEqual((payload["state"], payload["live"], payload["label"]), ("failed", False, Job.State.LOST.label))
        self.assertUnchanged(before)


class StaleJobSlotTests(TestCase):
    def test_the_next_job_of_its_kind_runs_and_the_stopped_one_is_marked_lost(self):
        job = stopped("example.work")

        ran = run("example.work", "Example work", lambda progress: {"seen": 1})

        self.assertEqual(ran.state, Job.State.SUCCEEDED)
        job.refresh_from_db()
        self.assertEqual(job.state, Job.State.LOST)
