"""An agent refused for asking too much is one audit row a window, with the count of its refusals."""

from unittest import mock

from django.test import TestCase

from hq.platform.core.models import AuditLog

from ..denials import COALESCE_WINDOW, record_limited, settle


class Clock:
    def __init__(self) -> None:
        self.now = 5000.0

    def __call__(self) -> float:
        return self.now


class LimitedDenialTests(TestCase):
    def setUp(self):
        self.clock = Clock()
        self.enterContext(mock.patch("hq.platform.application.denials.monotonic", self.clock))

    def _rows(self, actor):
        return AuditLog.objects.filter(action=AuditLog.Action.DENIED, metadata__actor=actor).order_by("id")

    def _refuse(self, actor, times):
        for _ in range(times):
            record_limited(interface="api", reason="rate_limited", actor=actor, source="192.0.2.10")

    def test_a_flood_in_one_window_is_one_row_that_is_given_its_count_when_the_agent_is_next_admitted(self):
        self._refuse("example-flood", 5)
        self.assertEqual([row.metadata["count"] for row in self._rows("example-flood")], [1])

        self.clock.now += COALESCE_WINDOW.total_seconds()
        settle("example-flood")

        self.assertEqual([row.metadata["count"] for row in self._rows("example-flood")], [5])

    def test_a_refusal_after_the_window_opens_a_row_and_closes_the_last_with_its_count(self):
        self._refuse("example-two-windows", 3)
        self.clock.now += COALESCE_WINDOW.total_seconds()
        self._refuse("example-two-windows", 1)

        self.assertEqual([row.metadata["count"] for row in self._rows("example-two-windows")], [3, 1])

    def test_each_agent_and_reason_is_counted_apart(self):
        self._refuse("example-one", 2)
        record_limited(interface="api", reason="too_many_calls", actor="example-one")
        self._refuse("example-other", 1)

        self.assertEqual(self._rows("example-one").count(), 2)
        self.assertEqual(self._rows("example-other").count(), 1)
        row = self._rows("example-one").first()
        self.assertEqual(
            {key: row.metadata[key] for key in ("interface", "actor", "reason", "authenticated")},
            {"interface": "api", "actor": "example-one", "reason": "rate_limited", "authenticated": True},
        )
