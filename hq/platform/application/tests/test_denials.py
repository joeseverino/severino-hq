"""Refusals, recorded: attributed when the caller is known, counted when not."""

from unittest import mock

from django.test import TestCase

from hq.platform.core.models import AuditLog

from ..denials import record_denial
from ..refusal_codes import Code


def _denials():
    return AuditLog.objects.filter(action=AuditLog.Action.DENIED).order_by("id")


class NeverInTheWayTests(TestCase):
    def test_a_failure_to_record_is_swallowed(self):
        with mock.patch("hq.platform.application.denials.record_event", side_effect=RuntimeError("disk full")):
            record_denial(interface="mcp", reason=Code.AGENTS_PAUSED, actor="example-agent")

        self.assertEqual(_denials().count(), 0)


class WhoColumnTests(TestCase):
    def test_an_unsigned_caller_is_named_by_its_address(self):
        self.assertIn("100.64.0.7", AuditLog(metadata={"source": "100.64.0.7"}).actor_label)
