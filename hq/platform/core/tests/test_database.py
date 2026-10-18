"""SQLite refreshes its planner statistics after a migration and nightly, never on a request."""

import threading
import time
from io import StringIO
from unittest.mock import patch

from django.apps import apps
from django.core.management import call_command
from django.db import connection, transaction
from django.db.models.signals import post_migrate
from django.test import SimpleTestCase, TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext

from hq.platform.core import database
from hq.platform.core.models import AuditLog


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
                post_migrate.send(sender=config, app_config=config, verbosity=0, interactive=False, using="default")
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


class TurnsTests(SimpleTestCase):
    """Writing back to back lets go once it has held the lock for a while."""

    def setUp(self):
        self.now = 0.0
        self.turns = database.Turns(clock=lambda: self.now)

    def statement(self, *, takes_lock=True, after=0.01) -> float:
        self.now += after
        pause = self.turns.before(takes_lock)
        self.now += pause
        self.turns.after()
        return pause

    def test_writing_back_to_back_lets_go_after_it_has_held(self):
        pauses = [self.statement() for _ in range(250)]

        self.assertEqual(pauses.count(database.LETS_GO), 2)
        self.assertGreater(database.LETS_GO, database.RETRIES_EVERY)

    def test_a_gap_was_already_a_turn(self):
        pauses = [self.statement(after=database.LETS_GO + 0.001) for _ in range(50)]

        self.assertEqual(set(pauses), {0.0})

    def test_only_a_statement_that_takes_the_lock_waits(self):
        for _ in range(150):
            self.statement(takes_lock=False)

        self.assertEqual(self.statement(takes_lock=False), 0.0)
        self.assertEqual(self.statement(), database.LETS_GO)


class GivesWayTests(TransactionTestCase):
    """A connection waiting for the lock gets it while another writes back to back."""

    def test_a_waiting_writer_gets_in_within_about_a_turn(self):
        stop, going = threading.Event(), threading.Event()
        commits = [0]
        # Each importer transaction holds the lock at least this long.
        held = 0.02

        def importing():
            try:
                with database.gives_way():
                    while not stop.is_set():
                        with transaction.atomic():
                            AuditLog.objects.create(action="imported", object_type="bench", message="batch")
                            time.sleep(held)
                        commits[0] += 1
                        if commits[0] == 5:
                            going.set()
            finally:
                connection.close()

        writer = threading.Thread(target=importing)
        writer.start()
        try:
            self.assertTrue(going.wait(10))
            before = commits[0]
            with transaction.atomic():
                AuditLog.objects.create(action="updated", object_type="report", message="the controller")
            landed = commits[0] - before
        finally:
            stop.set()
            writer.join()

        # Within two turns of the importer's writing, however long its commits take.
        self.assertLessEqual(landed, 2 * database.HOLDS / held)

    def test_without_sqlite_or_inside_a_transaction_nothing_changes(self):
        with transaction.atomic(), database.without_waiting():
            AuditLog.objects.create(action="updated", object_type="report", message="held")
        self.assertTrue(AuditLog.objects.filter(message="held").exists())
