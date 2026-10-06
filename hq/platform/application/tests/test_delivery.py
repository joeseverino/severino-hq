"""Continuous delivery is scheduled once per stage, and only through a connection that manages it."""

from __future__ import annotations

from django.test import TestCase
from django.utils import timezone

from hq.domains.control_plane.models import ManagedResource, OperationRequest
from hq.domains.control_plane.provider_adapters.github import COMPOSE_WORKFLOW, CURRENT, KIND

from ..controller import schedule_automatic_operations


def drifted(message: str) -> list[dict]:
    return [{"type": "Degraded", "status": True, "reason": "Reported", "message": message}]


class DeliveryScheduleTests(TestCase):
    def setUp(self):
        from hq.platform.application.adoption_testing import managing_everything

        managing_everything()
        self.resource = ManagedResource.objects.create(
            key="delivery",
            kind=KIND,
            spec={
                "repository": "example/host",
                "workflow": COMPOSE_WORKFLOW,
                "branch": "main",
                "production": CURRENT,
            },
            generation=1,
            observed_generation=1,
            conditions=drifted("example.alpha: bbbbbbb is approved, production still runs aaaaaaa. No deploy has started."),
        )

    def settle(self):
        OperationRequest.objects.update(
            state=OperationRequest.State.SUCCEEDED, completed_at=timezone.now()
        )

    def test_each_new_stage_is_one_reconcile(self):
        self.assertEqual(len(schedule_automatic_operations("example-controller")["scheduled"]), 1)
        self.settle()
        self.assertEqual(schedule_automatic_operations("example-controller")["scheduled"], [])

        self.resource.conditions = drifted(
            "example.alpha: bbbbbbb is approved, production still runs aaaaaaa. Deploy run 9 is waiting for approval."
        )
        self.resource.save(update_fields=["conditions"])
        self.assertEqual(len(schedule_automatic_operations("example-controller")["scheduled"]), 1)
        self.assertEqual(OperationRequest.objects.count(), 2)

    def test_a_current_delivery_is_left_alone(self):
        self.resource.conditions = [{"type": "Ready", "status": True, "reason": "Observed", "message": ""}]
        self.resource.save(update_fields=["conditions"])

        self.assertEqual(schedule_automatic_operations("example-controller")["scheduled"], [])
