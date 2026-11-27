"""Readiness knows every migration the image ships is applied, from one query and the directory listing."""

from unittest.mock import patch

from django.db import connection
from django.test import TestCase


@patch("hq.platform.core.health_views.collected", return_value=True)
class ReadinessMigrationsTests(TestCase):
    def migrations(self):
        response = self.client.get("/health/ready/")
        return response.status_code, response.json()["checks"]["migrations"]

    def test_a_database_never_migrated_is_not_ready(self, _collected):
        # SQLite's DDL is transactional, so the test's rollback restores it.
        with connection.cursor() as cursor:
            cursor.execute("ALTER TABLE django_migrations RENAME TO django_migrations_away")

        self.assertEqual(self.migrations(), (503, False))
