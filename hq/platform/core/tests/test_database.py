"""SQLite refreshes its planner statistics after a migration and nightly, never on a request."""

import threading
import time
from unittest.mock import patch

from django.db import connection, transaction
from django.test import TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext

from hq.platform.core import database
from hq.platform.core.models import AuditLog


def optimizations(queries) -> list[str]:
    return [query["sql"] for query in queries if "optimize" in query["sql"].lower()]


class OptimizeTests(TestCase):
    def test_another_database_is_left_alone(self):
        with patch.object(connection, "vendor", "postgresql"), CaptureQueriesContext(connection) as queries:
            database.optimize()
        self.assertEqual(optimizations(queries), [])


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
