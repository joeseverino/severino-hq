"""HQ words an age one way, from one function."""

from datetime import UTC, datetime, timedelta

from django.test import SimpleTestCase

from ..moments import ago

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


class AgePhrasingTests(SimpleTestCase):
    def test_an_age_is_one_unit_and_a_stamp_is_read_as_written(self):
        cases = {
            timedelta(seconds=59): "just now",
            timedelta(seconds=60): "1\xa0minute ago",
            timedelta(minutes=59): "59\xa0minutes ago",
            timedelta(hours=1): "1\xa0hour ago",
            timedelta(hours=5, minutes=40): "5\xa0hours ago",
            timedelta(days=5, hours=15): "5\xa0days ago",
        }
        for age, said in cases.items():
            with self.subTest(age=age):
                self.assertEqual(ago(NOW - age, NOW), said)
                self.assertEqual(ago((NOW - age).isoformat(), NOW), said)

    def test_no_moment_is_never_and_a_future_one_is_just_now(self):
        for missing in ("", "not a time"):
            self.assertEqual(ago(missing, NOW), "never")
        self.assertEqual(ago(NOW + timedelta(minutes=5), NOW), "just now")
