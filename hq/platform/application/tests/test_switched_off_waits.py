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
from hq.platform.core.models import AuditLog

from ..adoption_testing import managing_everything
from ..controller import claim_next_operation, peek_next_operation
from ..infrastructure import ManagedResourceCommand, save_managed_resource
from ..resource_operations import OperationCommand, request_reconcile, request_removal
from ..security import cli_principal
from ..sweep import record_sweep
from .inventory_records import A_REWRITE, a_sweep

KIND = "adguard.rewrite"
RECONCILES = ((KIND, "reconcile"),)


def switch(resource: ManagedResource, *, enabled: bool) -> None:
    save_managed_resource(
        ManagedResourceCommand(key=resource.key, kind=resource.kind, spec=resource.spec, enabled=enabled),
        principal=cli_principal(),
        current_key=resource.key,
        sent=frozenset({"enabled"}),
    )


class SwitchedOffRecord(TestCase):
    def setUp(self):
        super().setUp()
        managing_everything()
        record_sweep(a_sweep(**{KIND: [A_REWRITE]}), principal=cli_principal())
        self.resource = ManagedResource.objects.get(kind=KIND)
        request_reconcile(
            OperationCommand(idempotency_key="apply-1"), principal=cli_principal(), current_key=self.resource.key
        )


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
        self.assertTrue(
            AuditLog.objects.filter(
                object_id=str(operation.pk),
                metadata__operation="infrastructure.resource.update",
                metadata__actor=cli_principal().actor,
            ).exists()
        )

    def test_its_removal_is_still_handed_over(self):
        request_removal(
            OperationCommand(idempotency_key="remove-1"), principal=cli_principal(), current_key=self.resource.key
        )

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
        self.assertEqual(OperationRequest.objects.get().state, OperationRequest.State.CANCELLED)
