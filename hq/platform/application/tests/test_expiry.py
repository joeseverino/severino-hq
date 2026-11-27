"""One days-left rule, and one derivation of a resource for every adapter."""

from datetime import UTC, datetime, timedelta

from django.test import SimpleTestCase

from ..expiry import days_until, renewal_opens_at, renewal_window

NOW = datetime(2026, 1, 1, tzinfo=UTC)


class DaysUntilTests(SimpleTestCase):
    def test_a_naive_moment_is_utc(self):
        self.assertEqual(days_until(datetime(2026, 1, 11), NOW), 10)  # noqa: DTZ001 - the naive moment is under test

    def test_renewal_window_and_opening(self):
        self.assertEqual(renewal_window({}), 30)
        self.assertEqual(renewal_window({"renewal_window_days": "14"}), 14)
        self.assertEqual(renewal_window({"renewal_window_days": "soon"}), 30)
        self.assertEqual(renewal_opens_at(NOW, 30), NOW - timedelta(days=30))
