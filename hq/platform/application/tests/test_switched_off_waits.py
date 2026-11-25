"""Work queued for a record before it was switched off never reaches the controller.

Switching a declaration off pauses HQ's changes to its record. Every path that
queues work checks the switch when it queues; a reconcile already waiting
would otherwise be handed over, and the controller would write the record HQ
was told to leave alone.
"""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from hq.domains.control_plane.models import ManagedResource, OperationRequest
from hq.platform.application.tests.consent import consented
from hq.platform.core.models import AuditLog
from hq.platform.core.testing import managing_everything

from ..capabilities import execute_capability
from ..controller import claim_next_operation, peek_next_operation
from ..report_testing import bridged
from ..resource_operations import OperationCommand, request_reconcile, request_removal
from ..security import cli_principal
from .inventory_records import A_REWRITE, a_sweep

KIND = "adguard.rewrite"
RECONCILES = ((KIND, "reconcile"),)


def switch(resource: ManagedResource, *, enabled: bool) -> None:
    answer = execute_capability(
        "infrastructure.resource.update", {"enabled": enabled}, principal=cli_principal(), target=resource.key
    )
    assert answer["ok"], answer


class SwitchedOffRecord(TestCase):
    def setUp(self):
        super().setUp()
        managing_everything()
        bridged("inventory", a_sweep(**{KIND: [A_REWRITE]}))
        self.resource = ManagedResource.objects.get(kind=KIND)
        request_reconcile(OperationCommand(), consented=consented(cli_principal()), current_key=self.resource.key)


class QueuedBeforeTheSwitchOffTests(SwitchedOffRecord):
    def setUp(self):
        super().setUp()
        switch(self.resource, enabled=False)

    def test_the_controller_is_handed_nothing(self):
        self.assertIsNone(peek_next_operation(capabilities=RECONCILES)["operation"])
        self.assertIsNone(claim_next_operation("test-controller", capabilities=RECONCILES)["operation"])

    def test_the_switch_off_calls_the_waiting_work_off_as_its_own(self):
        operation = OperationRequest.objects.get()

        self.assertEqual(operation.state, OperationRequest.State.CANCELLED)
        self.assertEqual(operation.result["reason"], "SwitchedOff")
        self.assertTrue(
            AuditLog.objects.filter(
                object_id=str(operation.pk),
                metadata__operation="infrastructure.resource.update",
                metadata__actor=cli_principal().actor,
            ).exists()
        )

    def test_its_removal_is_still_handed_over(self):
        request_removal(OperationCommand(), consented=consented(cli_principal()), current_key=self.resource.key)

        claim = claim_next_operation("test-controller", capabilities=((KIND, "delete"),))

        self.assertEqual(claim["operation"]["action"], "delete")

    def test_switched_on_again_it_is_applied(self):
        switch(self.resource, enabled=True)

        claim = claim_next_operation("test-controller", capabilities=RECONCILES)

        self.assertEqual(claim["resource"]["key"], self.resource.key)
        self.assertTrue(claim["resource"]["enabled"])


class ClaimedBeforeTheSwitchOffTests(SwitchedOffRecord):
    def test_work_whose_lease_lapses_is_called_off_not_handed_over_again(self):
        claim_next_operation("test-controller", capabilities=RECONCILES)
        switch(self.resource, enabled=False)
        OperationRequest.objects.update(lease_expires_at=timezone.now() - timedelta(seconds=1))

        self.assertIsNone(claim_next_operation("test-controller", capabilities=RECONCILES)["operation"])
        ended = OperationRequest.objects.get()
        self.assertEqual(ended.state, OperationRequest.State.CANCELLED)
        self.assertEqual(ended.result["reason"], "SwitchedOff")
