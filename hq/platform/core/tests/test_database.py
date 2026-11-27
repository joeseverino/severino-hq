"""SQLite's one write lock: refreshed statistics, turns between writers, and a lock taken only to write."""

import threading
import time
from unittest.mock import patch

from django.db import OperationalError, connection, transaction
from django.test import TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext

from hq.platform.core import bindings, database
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


def another_writer() -> str:
    """What a write from another connection that will not wait answers: "wrote", or its error."""

    answered = []

    def write():
        try:
            with database.without_waiting():
                AuditLog.objects.create(action="updated", object_type="report", message="another writer")
            answered.append("wrote")
        except OperationalError as exc:
            answered.append(str(exc))
        finally:
            connection.close()

    writer = threading.Thread(target=write)
    writer.start()
    writer.join()
    return answered[0]


class WriteLockTests(TransactionTestCase):
    """A serialized transaction holds the write lock from its first write, and
    never commits a decision another writer's commit made stale."""

    def written(self) -> list[str]:
        return list(AuditLog.objects.filter(object_type="example").values_list("message", flat=True))

    def test_reading_holds_no_writer_back_and_a_decision_it_made_stale_runs_again(self):
        seen, after = [], []

        def work():
            with transaction.atomic():
                seen.append(AuditLog.objects.filter(object_type="report").count())
            if len(seen) == 1:
                self.assertEqual(another_writer(), "wrote")
            AuditLog.objects.create(action="created", object_type="example", message=f"saw {seen[-1]}")
            after.append(another_writer())

        database.serialized(work)
        self.assertEqual(seen, [0, 1])
        self.assertEqual(self.written(), ["saw 1"])
        self.assertIn("database is locked", after[-1])

    def test_a_writer_that_gives_up_names_what_holds_the_lock(self):
        with (
            bindings.operation_context(interface="web", actor="example", operation="example.slow"),
            transaction.atomic(),
        ):
            refused = another_writer()
        self.assertIn("database is locked", refused)
        self.assertIn("example.slow", refused)
