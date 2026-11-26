"""Open problems on the page of the thing they are about, and since when."""

from datetime import timedelta
from unittest import mock

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from hq.domains.control_plane.models import ManagedResource
from hq.domains.control_plane.provider_adapters.github import KIND as DELIVERY
from hq.platform.application.markup_testing import select, text
from hq.platform.core.testing import operator

from .. import first_seen, freshness
from ..attention import infrastructure
from ..delivery_progress import STALLED_AFTER
from ..entity_links import entity_link
from ..findings import finding_key
from ..first_seen import note_open, recorded
from ..infrastructure import resource_health
from ..problems import note_open_problems, problem_counts, problems_about
from ..projection import projection_scope
from ..report_testing import bridged
from ..resource_context import record_status
from ..security import cli_principal
from .finding_examples.delivery_findings import delivered
from .host_only import host_only
from .inventory_records import kind_read

REWRITE = "adguard.rewrite"


def one(items):
    """The only item of a sequence that holds exactly one."""

    items = list(items)
    assert len(items) == 1, items
    return items[0]


def broken(key: str = "app", message: str = "The service refused the change.") -> ManagedResource:
    kind_read(REWRITE, timezone.now())
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


class ProblemsAboutTests(TestCase):
    def setUp(self):
        first_seen._looked.clear()
        self.resource = broken()
        self.url = entity_link("resource", "app").url

    def test_a_problem_is_found_by_the_page_of_what_it_is_about(self):
        with host_only(), projection_scope():
            found = one(problems_about(self.url, cli_principal()))
            elsewhere = problems_about(entity_link("resource", "other").url, cli_principal())

        self.assertEqual(found["subject"]["url"], self.url)
        self.assertEqual(found["status"], "serious")
        self.assertEqual(elsewhere, ())

    def test_the_records_own_page_shows_the_queues_card(self):
        operator(self.client, "owner", email="owner@example.test")

        with host_only():
            page = self.client.get(self.url)
            queue = self.client.get(reverse("action_items"))

        (here,) = select(page, "id", "problems-here")
        self.assertIn("tone-serious", here.classes)
        self.assertIn("1 urgent problem", text(page))
        self.assertIn("The service refused the change.", text(page))
        # One card: the page draws the queue's own card, with no second link to itself.
        self.assertEqual(len(select(page, "data-attention-item")), 1)
        self.assertTrue(select(queue, "data-attention-item"))
        self.assertNotIn(self.url, [title.attrs.get("href") for title in select(page, "data-attention-title")])

    def test_a_page_with_nothing_open_draws_nothing(self):
        self.resource.conditions = [{"type": "Ready", "status": True}]
        self.resource.save()
        operator(self.client, "owner", email="owner@example.test")

        with host_only():
            page = self.client.get(self.url)

        self.assertNotContains(page, "problems-here")

    def test_counts_are_by_page(self):
        broken("other")

        with host_only(), projection_scope():
            counts = problem_counts(cli_principal())

        self.assertEqual(counts[self.url], 1)
        self.assertEqual(counts[entity_link("resource", "other").url], 1)

    def test_the_queue_can_be_asked_for_one_things_problems(self):
        broken("other")
        operator(self.client, "owner", email="owner@example.test")

        with host_only():
            page = self.client.get(reverse("action_items"), {"about": self.url})

        self.assertEqual(len(select(page, "data-attention-item")), 1)


class FirstSeenTests(TestCase):
    def setUp(self):
        first_seen._looked.clear()
        self.now = timezone.now()
        self.cadence = freshness.cadence(freshness.FIRST_SEEN)

    def test_what_is_open_at_the_first_look_has_no_date(self):
        note_open(["a"], now=self.now)

        self.assertEqual(recorded(), {})

    def test_what_appears_between_two_close_looks_is_dated_by_the_second(self):
        note_open(["a"], now=self.now)
        later = self.now + self.cadence.every
        note_open(["a", "b"], now=later)

        self.assertEqual(recorded(), {"b": later})

    def test_what_appears_after_a_gap_has_no_date(self):
        note_open(["a"], now=self.now)
        note_open(["a", "b"], now=self.now + self.cadence.stale_after + timedelta(minutes=1))

        self.assertEqual(recorded(), {})

    def test_a_date_is_kept_while_the_item_stays_open_and_forgotten_when_it_closes(self):
        note_open([], now=self.now)
        seen = self.now + self.cadence.every
        note_open(["a"], now=seen)
        note_open(["a"], now=seen + self.cadence.every)
        self.assertEqual(recorded(), {"a": seen})

        note_open([], now=seen + 2 * self.cadence.every)
        note_open(["a"], now=seen + 3 * self.cadence.every)

        self.assertEqual(recorded(), {"a": seen + 3 * self.cadence.every})

    def test_a_report_is_followed_by_a_look_only_when_one_is_due(self):
        with host_only():
            self.assertTrue(note_open_problems())
            self.assertFalse(note_open_problems())
            first_seen._looked.clear()
            # Another process: the stored look still says none is due.
            with CaptureQueriesContext(connection) as asked:
                self.assertFalse(note_open_problems())

        # The read of the stored look, and no look, which derives the queue and records it.
        self.assertLessEqual(len(asked), 1)

    def test_a_problem_found_by_a_report_says_since_when_on_its_card(self):
        with host_only():
            bridged("inventory", {REWRITE: {"ok": True, "records": []}})
            first_seen._looked.clear()
            broken()
            # A look due at once, the one before it still current.
            at_once = freshness.ReadCadence(timedelta(microseconds=1), self.cadence.stale_after)
            with mock.patch.dict(freshness._NAMED, {freshness.FIRST_SEEN: lambda: at_once}):
                bridged("inventory", {REWRITE: {"ok": True, "records": []}})
            with projection_scope():
                (item,) = [item for item in tuple(infrastructure()) if finding_key(item).endswith(":resource:app")]
                card = one(problems_about(entity_link("resource", "app").url, cli_principal()))

        self.assertIsNotNone(item.since)
        self.assertEqual(card["since"], item.since.isoformat())

    def test_a_look_that_fails_does_not_lose_the_report(self):
        with (
            host_only(),
            mock.patch("hq.platform.application.problems.note_open_problems", side_effect=RuntimeError("no")),
            self.assertLogs("severino.sweep", level="ERROR"),
        ):
            result = bridged("inventory", {REWRITE: {"ok": True, "records": []}})

        self.assertTrue(result["ok"])


class DeployInProgressTests(TestCase):
    """A deploy that is running is told, not raised as a problem."""

    def delivery(self, stage: str, state: str, **said) -> None:
        kind_read(DELIVERY, timezone.now())
        delivered(stage, state, **said)

    def items(self):
        with host_only(), projection_scope():
            return [item for item in tuple(infrastructure()) if finding_key(item).endswith("resource:delivery")]

    def item(self):
        (found,) = [item for item in self.items() if finding_key(item).startswith("finding:deploy-")]
        return found

    def test_a_deploy_behind_for_too_long_is_a_serious_problem(self):
        self.delivery("Compose run 9 is running", "running", behind_for=STALLED_AFTER + timedelta(minutes=1))

        found = self.items()

        self.assertIn("serious", {item.severity for item in found})
        self.assertFalse(any(item.notice for item in found))

    def test_a_failed_deploy_is_a_serious_problem(self):
        self.delivery("Compose run 9 failed", "failed")

        found = self.items()

        self.assertIn("serious", {item.severity for item in found})
        self.assertFalse(any(item.notice for item in found))

    def test_a_deploy_on_its_way_is_not_a_serious_problem_anywhere(self):
        self.delivery("Compose run 9 is running", "running")

        self.assertTrue(self.item().notice)
        self.assertNotIn("serious", {item.severity for item in self.items()})
        resource = ManagedResource.objects.get(key="delivery")
        self.assertEqual(resource_health(resource)["label"], "Deploy running")
        self.assertEqual(record_status(resource).tone, "attention")

    def test_a_run_waiting_for_approval_is_left_to_its_repository(self):
        """The waiting run is the repository's github-waiting, so the delivery raises no deploy finding of its own."""
        self.delivery("Deploy run 9 is waiting for approval", "waiting")

        found = self.items()

        self.assertFalse([item for item in found if finding_key(item).startswith("finding:deploy-")])
        self.assertNotIn("serious", {item.severity for item in found})
