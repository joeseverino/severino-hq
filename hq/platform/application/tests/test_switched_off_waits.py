"""Work queued for a record before it was switched off never reaches the controller.

Switching a declaration off pauses HQ's changes to its record. A reconcile
already waiting would otherwise be handed over, and the controller would
write the record HQ was told to leave alone.
"""

from django.test import TestCase

from hq.domains.control_plane.models import ManagedResource
from hq.platform.application.tests.consent import consented
from hq.platform.core.testing import managing_everything

from ..capabilities import execute_capability
from ..controller import claim_next_operation, peek_next_operation
from ..report_testing import bridged
from ..resource_operations import OperationCommand, request_reconcile
from ..security import cli_principal
from .inventory_records import A_REWRITE, a_sweep

KIND = "adguard.rewrite"
RECONCILES = ((KIND, "reconcile"),)


class QueuedBeforeTheSwitchOffTests(TestCase):
    def test_the_controller_is_handed_nothing(self):
        managing_everything()
        bridged("inventory", a_sweep(**{KIND: [A_REWRITE]}))
        resource = ManagedResource.objects.get(kind=KIND)
        request_reconcile(OperationCommand(), consented=consented(cli_principal()), current_key=resource.key)
        answer = execute_capability(
            "infrastructure.resource.update", {"enabled": False}, principal=cli_principal(), target=resource.key
        )
        self.assertTrue(answer["ok"], answer)

        self.assertIsNone(peek_next_operation(capabilities=RECONCILES)["operation"])
        self.assertIsNone(claim_next_operation("test-controller", capabilities=RECONCILES)["operation"])
