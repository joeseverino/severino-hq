"""The bench estate's machine ``lab-1`` has automation runs in each state, as
many more as the estate is larger, so a page that lists them is measured."""

from django.test import TestCase
from django.utils import timezone

from hq.domains.control_plane.models import OperationRequest

from .estate_sizes import at_both_sizes

State = OperationRequest.State


def _runs(_seeded) -> dict[str, int]:
    now = timezone.now()
    runs = OperationRequest.objects.filter(action=OperationRequest.Action.RUN)
    on = runs.filter(hosts__host="lab-1")
    return {
        "on lab-1": on.count(),
        "done": on.filter(state=State.SUCCEEDED).count(),
        "failed": on.filter(state=State.FAILED).count(),
        "called off": on.filter(state=State.CANCELLED).count(),
        "working": on.filter(state=State.CLAIMED, lease_expires_at__gt=now).count(),
        "worker gone quiet": runs.filter(state=State.CLAIMED, lease_expires_at__lte=now).count(),
        "waiting": runs.filter(state=State.QUEUED).count(),
    }


class BenchRunTests(TestCase):
    def test_lab_1_has_a_run_in_each_state_and_more_at_a_larger_size(self):
        small, large = at_both_sizes(_runs)

        for state, count in small.items():
            with self.subTest(state=state):
                self.assertGreater(count, 0)
        self.assertGreater(large["on lab-1"], small["on lab-1"])
