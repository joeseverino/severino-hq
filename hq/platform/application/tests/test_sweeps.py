"""A page a person loads is answered from what is stored, sweeps or not."""

from unittest import mock

from django.test import TestCase
from django.urls import reverse

from hq.domains.control_plane.models import ManagedResource, ProviderInventory
from hq.platform.application import derivations
from hq.platform.application.dashboard import waiting
from hq.platform.application.derivations import counting, derive_ahead
from hq.platform.application.projection import projection_scope
from hq.platform.core import ahead
from hq.platform.core.bench import seed, unchanged_sweep


class SweepTests(TestCase):
    """A page a person loads is answered from what is stored, sweeps or not.

    Runs with whatever extensions are installed: their providers are part of
    every page here.
    """

    PAGES = ("action_item_count", "dashboard", "action_items")

    @classmethod
    def setUpTestData(cls):
        cls.seeded = seed(0.05)
        # The first sweep of a seeded estate adopts what it finds; every one
        # after it changes nothing but when it was read.
        with mock.patch("hq.platform.application.cadence._touch"):
            unchanged_sweep()

    def setUp(self):
        derivations.forget()
        self.addCleanup(derivations.forget)
        self.client.force_login(self.seeded.user)
        patch = mock.patch("hq.platform.application.cadence._touch")
        patch.start()
        self.addCleanup(patch.stop)

    def _load(self) -> None:
        for name in self.PAGES:
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)

    def _swept(self) -> None:
        with ahead.keeping(inline=True), self.captureOnCommitCallbacks(execute=True):
            unchanged_sweep()

    def test_the_sweep_moves_what_the_estate_is_keyed_on(self):
        self._load()
        before = derivations.every_revision()

        unchanged_sweep()

        self.assertNotEqual(derivations.every_revision(), before)

    def test_a_request_after_a_sweep_that_changed_nothing_derives_nothing(self):
        self._load()

        self._swept()

        for name in self.PAGES:
            with self.subTest(page=name), counting() as (ran, _served):
                self.assertEqual(self.client.get(reverse(name)).status_code, 200)
                self.assertEqual(dict(ran), {})

    def test_without_deriving_ahead_that_request_pays_for_the_estate(self):
        self._load()

        unchanged_sweep()

        with counting() as (ran, _served):
            self.client.get(reverse("dashboard"))
        self.assertIn("estate.topology", ran)

    def test_the_count_is_still_not_modified_after_a_sweep_that_changed_nothing(self):
        url = reverse("action_item_count")
        held = self.client.get(url).headers["ETag"]

        self._swept()

        with counting() as (ran, _served):
            again = self.client.get(url, headers={"if-none-match": held})
        self.assertEqual(again.status_code, 304)
        self.assertEqual(again.headers["ETag"], held)
        self.assertEqual(dict(ran), {})

    def _fresh_count(self) -> int:
        with projection_scope(), derivations.uncached():
            return waiting(self.seeded.user.pk)

    def test_a_real_change_is_seen_by_the_very_next_request(self):
        url = reverse("action_item_count")
        first = self.client.get(url)

        for enabled, ahead_of_it in ((False, False), (True, True)):
            with self.subTest(enabled=enabled, derived_ahead=ahead_of_it):
                held = self.client.get(url).headers["ETag"]
                ManagedResource.objects.update(enabled=enabled)
                if ahead_of_it:
                    derive_ahead()

                again = self.client.get(url, headers={"if-none-match": held})

                self.assertEqual(again.status_code, 200)
                self.assertEqual(again.json()["count"], self._fresh_count())
                self.assertNotEqual(again.headers["ETag"], held)
        self.assertEqual(again.json(), first.json())

    def test_a_reading_that_changed_in_a_sweep_is_seen_by_the_very_next_request(self):
        self._load()
        row = ProviderInventory.objects.exclude(records=[]).order_by("kind").first()
        before = self.client.get(reverse("control_plane:findings")).content

        with ahead.keeping(inline=True), self.captureOnCommitCallbacks(execute=True):
            ProviderInventory.objects.filter(pk=row.pk).update(reachable=False, error="refused")

        with counting() as (ran, _served):
            after = self.client.get(reverse("control_plane:findings")).content
        self.assertNotEqual(after, before)
        self.assertEqual(dict(ran), {})
