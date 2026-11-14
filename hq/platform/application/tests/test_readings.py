from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from hq.platform.application import readings
from hq.platform.application.projection import projection_scope


class StoredManyTests(TestCase):
    def test_a_projection_asks_for_each_reading_once(self):
        readings.record("example:a", 1)
        with projection_scope(), CaptureQueriesContext(connection) as captured:
            first = readings.stored_many(["example:a", "example:b"])
            again = readings.stored_many(["example:b", "example:a"])

        self.assertEqual(len(captured), 1)
        self.assertEqual({key: found.value for key, found in first.items()}, {"example:a": 1})
        self.assertEqual(again, first)

    def test_a_reading_written_within_a_projection_is_read_as_written(self):
        readings.record("example:a", 1)
        with projection_scope():
            readings.stored_many(["example:a", "example:b"])
            readings.record("example:a", 2)
            readings.record("example:b", 3)
            found = readings.stored_many(["example:a", "example:b"])

        self.assertEqual({key: reading.value for key, reading in found.items()}, {"example:a": 2, "example:b": 3})

    def test_outside_a_projection_each_call_reads_the_table(self):
        readings.record("example:a", 1)
        readings.stored_many(["example:a"])
        readings.record("example:a", 2)

        self.assertEqual(readings.stored_many(["example:a"])["example:a"].value, 2)
