"""Readiness knows every migration the image ships is applied, from one query and the directory listing."""

from unittest.mock import patch

from django.db import connection
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.recorder import MigrationRecorder
from django.test import TestCase

from hq.platform.core import health_views


@patch("hq.platform.core.health_views.collected", return_value=True)
class ReadinessMigrationsTests(TestCase):
    def migrations(self):
        response = self.client.get("/health/ready/")
        return response.status_code, response.json()["checks"]["migrations"]

    def test_a_migrated_database_is_ready_without_building_the_graph(self, _collected):
        with patch.object(MigrationLoader, "build_graph", side_effect=AssertionError("the probe built the graph")):
            self.assertEqual(self.migrations(), (200, True))

    def test_a_database_one_migration_behind_the_image_is_not_ready(self, _collected):
        latest = MigrationRecorder.Migration.objects.order_by("-applied", "-id").first()
        latest.delete()

        self.assertEqual(self.migrations(), (503, False))

    def test_a_database_never_migrated_is_not_ready(self, _collected):
        # SQLite's DDL is transactional, so the test's rollback restores it.
        with connection.cursor() as cursor:
            cursor.execute("ALTER TABLE django_migrations RENAME TO django_migrations_away")

        self.assertEqual(self.migrations(), (503, False))

    def test_a_migration_this_image_does_not_ship_leaves_it_ready(self, _collected):
        """An earlier image started on a database a later one migrated."""

        MigrationRecorder.Migration.objects.create(app="example", name="0999_from_a_later_image")

        self.assertEqual(self.migrations(), (200, True))

    def test_the_listing_names_what_djangos_loader_names(self, _collected):
        loader = MigrationLoader(connection, load=False)
        loader.load_disk()

        self.assertEqual(health_views.shipped_migrations(), set(loader.disk_migrations))
