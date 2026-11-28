"""HQ's playbook worker is known by its claims: one never seen, or not seen
within its timer's interval, takes no scan or run, so each is refused with
the reason. Every manifest here is a synthetic ``example.*`` one."""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from hq.platform.core.models import UpstreamReading

from ..capabilities import run_capability
from ..security import Capability, Principal
from .procedures import APP, RESTART_RUN, Supply, manifest, run_payload

OPERATOR = Principal("example-operator", "web", frozenset({Capability.READ, Capability.MANAGE_INFRASTRUCTURE}))
HEARTBEAT = "automation.read-heartbeat"


def seen(ago: timedelta | None) -> None:
    """The worker last arrived ``ago``, or never when None."""

    readings = UpstreamReading.objects.filter(key=HEARTBEAT)
    if ago is None:
        readings.delete()
    else:
        readings.update(observed_at=timezone.now() - ago)


class AbsentWorkerTests(TestCase):
    def setUp(self):
        self.supply = Supply(self, manifest())

    def run_it(self) -> dict:
        return run_capability(RESTART_RUN, run_payload(APP), principal=OPERATOR, idempotency_key="example-run").answer

    def test_not_seen_within_its_interval_a_run_is_refused_with_how_long(self):
        seen(timedelta(hours=3))

        answer = self.run_it()
        self.assertFalse(answer["ok"])
        self.assertIn("3\xa0hours", answer["error"]["message"])
