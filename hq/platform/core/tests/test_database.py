"""SQLite refreshes its planner statistics after a migration and nightly, never on a request."""

from __future__ import annotations

from io import StringIO
from unittest.mock import patch

from django.apps import apps
from django.core.management import call_command
from django.db import connection
from django.db.models.signals import post_migrate
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from hq.platform.core import database


def optimizations(queries) -> list[str]:
    return [query["sql"] for query in queries if "optimize" in query["sql"].lower()]


class OptimizeTests(TestCase):
    def test_it_asks_sqlite_to_check_every_table(self):
        with CaptureQueriesContext(connection) as queries:
            database.optimize()
        self.assertEqual(optimizations(queries), ["PRAGMA optimize=0x10002"])

    def test_another_database_is_left_alone(self):
        with patch.object(connection, "vendor", "postgresql"), CaptureQueriesContext(connection) as queries:
            database.optimize()
        self.assertEqual(optimizations(queries), [])

    def test_a_migration_runs_it_once(self):
        with CaptureQueriesContext(connection) as queries:
            for config in apps.get_app_configs():
                post_migrate.send(
                    sender=config, app_config=config, verbosity=0, interactive=False, using="default"
                )
        self.assertEqual(optimizations(queries), ["PRAGMA optimize=0x10002"])

    def test_the_nightly_prune_runs_it(self):
        with CaptureQueriesContext(connection) as queries:
            call_command("prune_audit", stdout=StringIO())
        self.assertEqual(optimizations(queries), ["PRAGMA optimize=0x10002"])

    def test_counting_what_a_prune_would_delete_does_not(self):
        with CaptureQueriesContext(connection) as queries:
            call_command("prune_audit", "--check-only", stdout=StringIO())
        self.assertEqual(optimizations(queries), [])

    def test_a_request_never_runs_it(self):
        with CaptureQueriesContext(connection) as queries:
            self.client.get("/health/ready/")
            self.client.get("/accounts/login/")
        self.assertEqual(optimizations(queries), [])
