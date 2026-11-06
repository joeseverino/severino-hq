"""A stored reading no read is known to have answered loses its moment; every
other reading keeps the moment it had."""

from datetime import timedelta

from django.utils import timezone

from hq.platform.core.tests.migration_steps import MigrationStepTestCase

BEFORE = ("control_plane", "0042_reconcile_label")
AFTER = ("control_plane", "0043_inventory_read_moment_only_answered")


class ReadMomentMigrationTests(MigrationStepTestCase):
    def setUp(self):
        inventory = self.migrate(BEFORE).get_model("control_plane", "ProviderInventory").objects
        self.read = timezone.now() - timedelta(hours=1)
        inventory.create(kind="example.answered", records=[], reachable=True, observed_at=self.read)
        inventory.create(kind="example.held", records=[{"name": "a"}], reachable=False, observed_at=self.read)
        inventory.create(kind="example.unanswered", records=[], reachable=False, observed_at=self.read)

    def test_only_an_unreachable_reading_with_nothing_held_loses_its_moment(self):
        inventory = self.migrate(AFTER).get_model("control_plane", "ProviderInventory").objects

        self.assertEqual(
            dict(inventory.values_list("kind", "observed_at")),
            {"example.answered": self.read, "example.held": self.read, "example.unanswered": None},
        )

    def test_going_back_gives_every_reading_a_moment(self):
        self.migrate(AFTER)
        inventory = self.migrate(BEFORE).get_model("control_plane", "ProviderInventory").objects

        self.assertFalse(inventory.filter(observed_at__isnull=True).exists())
