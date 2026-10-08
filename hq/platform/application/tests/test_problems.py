"""Open problems on the page of the thing they are about, and since when."""

from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from hq.domains.control_plane.models import ManagedResource
from hq.domains.control_plane.provider_adapters.github import COMPOSE_WORKFLOW, CURRENT, KIND as DELIVERY

from .. import first_seen
from ..attention import infrastructure
from ..delivery_progress import STALLED_AFTER
from ..entity_links import entity_link
from ..first_seen import KNOWN_WITHIN, LOOK_EVERY, note_open, recorded
from ..infrastructure import resource_health
from ..problems import note_open_problems, problem_counts, problems_about
from ..projection import projection_scope
from ..resource_context import record_status
from ..security import cli_principal
from ..sweep import record_sweep

REWRITE = "adguard.rewrite"


def one(items):
    """The only item of a sequence that holds exactly one."""

    items = list(items)
    assert len(items) == 1, items
    return items[0]


def broken(key: str = "app", message: str = "The service refused the change.") -> ManagedResource:
    spec = {"domain": f"{key}.example.com", "answer": "192.0.2.10", "enabled": True}
    return ManagedResource.objects.create(
        key=key,
        kind=REWRITE,
        spec=spec,
        # Read back as set, so the one thing open about it is what it reports.
        status=dict(spec),
        generation=1,
        observed_generation=1,
        last_observed_at=timezone.now(),
        conditions=[{"type": "Degraded", "status": True, "reason": "Refused", "message": message}],
    )


def host_only():
    return mock.patch("hq.platform.application.domains.extension_domains", return_value=())


class ProblemsAboutTests(TestCase):
    def setUp(self):
        first_seen._looked.clear()
        self.resource = broken()
        self.url = entity_link("resource", "app").url

    def test_a_problem_is_found_by_the_page_of_what_it_is_about(self):
        with host_only(), projection_scope():
            found = one(problems_about(self.url))
            elsewhere = problems_about(entity_link("resource", "other").url)

        self.assertEqual(found["subject"]["url"], self.url)
        self.assertEqual(found["status"], "serious")
        self.assertEqual(elsewhere, ())

    def test_the_records_own_page_shows_the_queues_card(self):
        self.client.force_login(get_user_model().objects.create_superuser("owner", "owner@example.test", "pw"))

        with host_only():
            page = self.client.get(self.url)
            queue = self.client.get(reverse("action_items"))

        self.assertContains(page, 'class="card problems-here"')
        self.assertContains(page, "1 problem")
        self.assertContains(page, "The service refused the change.")
        # One card: the page draws the queue's own markup, with no second link to itself.
        self.assertContains(page, 'class="attention-row"', count=1)
        self.assertContains(queue, 'class="attention-row"')
        self.assertNotContains(page, f'class="attention-title" href="{self.url}"')

    def test_a_page_with_nothing_open_draws_nothing(self):
        self.resource.conditions = [{"type": "Ready", "status": True}]
        self.resource.save()
        self.client.force_login(get_user_model().objects.create_superuser("owner", "owner@example.test", "pw"))

        with host_only():
            page = self.client.get(self.url)

        self.assertNotContains(page, "problems-here")

    def test_counts_are_by_page(self):
        broken("other")

        with host_only(), projection_scope():
            counts = problem_counts()

        self.assertEqual(counts[self.url], 1)
        self.assertEqual(counts[entity_link("resource", "other").url], 1)

    def test_the_queue_can_be_asked_for_one_things_problems(self):
        broken("other")
        self.client.force_login(get_user_model().objects.create_superuser("owner", "owner@example.test", "pw"))

        with host_only():
            page = self.client.get(reverse("action_items"), {"about": self.url})

        self.assertContains(page, 'class="attention-row"', count=1)


class FirstSeenTests(TestCase):
    def setUp(self):
        first_seen._looked.clear()
        self.now = timezone.now()

    def test_what_is_open_at_the_first_look_has_no_date(self):
        note_open(["a"], now=self.now)

        self.assertEqual(recorded(), {})

    def test_what_appears_between_two_close_looks_is_dated_by_the_second(self):
        note_open(["a"], now=self.now)
        later = self.now + LOOK_EVERY
        note_open(["a", "b"], now=later)

        self.assertEqual(recorded(), {"b": later})

    def test_what_appears_after_a_gap_has_no_date(self):
        note_open(["a"], now=self.now)
        note_open(["a", "b"], now=self.now + KNOWN_WITHIN + timedelta(minutes=1))

        self.assertEqual(recorded(), {})

    def test_a_date_is_kept_while_the_item_stays_open_and_forgotten_when_it_closes(self):
        note_open([], now=self.now)
        seen = self.now + LOOK_EVERY
        note_open(["a"], now=seen)
        note_open(["a"], now=seen + LOOK_EVERY)
        self.assertEqual(recorded(), {"a": seen})

        note_open([], now=seen + 2 * LOOK_EVERY)
        note_open(["a"], now=seen + 3 * LOOK_EVERY)

        self.assertEqual(recorded(), {"a": seen + 3 * LOOK_EVERY})

    def test_a_report_is_followed_by_a_look_only_when_one_is_due(self):
        with host_only():
            self.assertTrue(note_open_problems())
            self.assertFalse(note_open_problems())
            first_seen._looked.clear()
            # Another process: the stored look still says none is due.
            with self.assertNumQueries(1):
                self.assertFalse(note_open_problems())

    def test_a_problem_found_by_a_report_says_since_when_on_its_card(self):
        with host_only():
            record_sweep({REWRITE: {"ok": True, "records": []}}, principal=cli_principal())
            first_seen._looked.clear()
            broken()
            with mock.patch.object(first_seen, "LOOK_EVERY", timedelta(0)):
                record_sweep({REWRITE: {"ok": True, "records": []}}, principal=cli_principal())
            with projection_scope():
                (item,) = [item for item in infrastructure() if item.key.endswith(":resource:app")]
                card = one(problems_about(entity_link("resource", "app").url))

        self.assertIsNotNone(item.since)
        self.assertEqual(card["since"], item.since.isoformat())

    def test_a_look_that_fails_does_not_lose_the_report(self):
        with (
            host_only(),
            mock.patch("hq.platform.application.problems.note_open_problems", side_effect=RuntimeError("no")),
            self.assertLogs("severino.sweep", level="ERROR"),
        ):
            result = record_sweep({REWRITE: {"ok": True, "records": []}}, principal=cli_principal())

        self.assertTrue(result["ok"])


class DeployInProgressTests(TestCase):
    """A deploy that is running is told, not raised as a problem."""

    def delivery(self, stage: str, *, behind_for: timedelta = timedelta(minutes=1), **extension) -> None:
        since = (timezone.now() - behind_for).isoformat()
        message = f"example.alpha: bbbbbbb is approved, production still runs aaaaaaa. {stage}."
        ManagedResource.objects.create(
            key="delivery",
            kind=DELIVERY,
            spec={"repository": "example/host", "workflow": COMPOSE_WORKFLOW, "branch": "main", "production": CURRENT},
            generation=1,
            observed_generation=1,
            last_observed_at=timezone.now(),
            status={"extensions": [
                {"plugin": "example.alpha", "running": "a" * 40, "admitted": "b" * 40, "stage": stage,
                 "run_url": "https://github.example.com/example/host/actions/runs/9", **extension},
            ]},
            conditions=[{"type": "Degraded", "status": True, "reason": "NotDelivered", "message": message, "since": since}],
        )

    def items(self):
        with host_only(), projection_scope():
            return [item for item in infrastructure() if item.key.endswith("resource:delivery")]

    def item(self):
        (found,) = [item for item in self.items() if item.key == "resource:delivery"]
        return found

    def test_a_running_deploy_is_a_notice(self):
        self.delivery("Compose run 9 is running")

        item = self.item()

        self.assertTrue(item.notice)
        self.assertEqual((item.status, item.title), ("attention", "A deploy is running"))
        self.assertIn("after 30 minutes", item.body)

    def test_a_deploy_not_started_yet_is_a_notice(self):
        self.delivery("No deploy has started")

        self.assertEqual((self.item().notice, self.item().title), (True, "A deploy is about to start"))

    def test_the_stage_the_reading_states_is_read_before_its_sentence(self):
        self.delivery("Compose run 9 failed", stage_state="running")

        self.assertTrue(self.item().notice)

    def test_a_deploy_behind_for_too_long_is_a_serious_problem(self):
        self.delivery("Compose run 9 is running", behind_for=STALLED_AFTER + timedelta(minutes=1))

        found = self.items()

        self.assertIn("serious", {item.status for item in found})
        self.assertFalse(any(item.notice for item in found))

    def test_a_failed_deploy_is_a_serious_problem(self):
        self.delivery("Compose run 9 failed")

        found = self.items()

        self.assertIn("serious", {item.status for item in found})
        self.assertFalse(any(item.notice for item in found))

    def test_a_deploy_on_its_way_is_not_a_serious_problem_anywhere(self):
        self.delivery("Compose run 9 is running")

        self.assertNotIn("serious", {item.status for item in self.items()})
        resource = ManagedResource.objects.get(key="delivery")
        self.assertEqual(resource_health(resource)["label"], "Deploy running")
        self.assertEqual(record_status(resource).tone, "pending")

    def test_a_run_waiting_for_approval_is_something_to_do(self):
        self.delivery("Deploy run 9 is waiting for approval")

        item = self.item()

        self.assertEqual((item.notice, item.status), (False, "attention"))
        self.assertEqual(item.title, "A deploy is waiting for your approval")
        self.assertEqual(item.url, "https://github.example.com/example/host/actions/runs/9")
