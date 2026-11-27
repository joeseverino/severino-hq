"""A use case's refusal reaches the caller with its own text, whichever door carried it."""

from django.test import TestCase

from hq.domains.control_plane.models import ManagedResource, OperationRequest
from hq.platform.api.tests.doors import OPERATOR, Caller, at_the_door, each_door
from hq.platform.application.tests.consent import consented
from hq.platform.core.testing import declare, managing_everything

from ..record_errors import PolicyError
from ..resource_operations import OperationCommand, request_reconcile
from ..security import cli_principal

RECONCILE = "infrastructure.resource.reconcile"
MANAGER = Caller("example-agent", "read manage_infrastructure")


def switched_off() -> ManagedResource:
    """A record HQ holds switched off, which refuses to be applied."""

    managing_everything()
    return declare("app-dns", "adguard.rewrite", {"domain": "app.example.com", "answer": "192.0.2.10"}, enabled=False)


def refusal_of(resource: ManagedResource) -> str:
    """What the use case itself says, so each adapter is held to it."""

    try:
        request_reconcile(OperationCommand(), consented=consented(cli_principal()), current_key=resource.key)
    except PolicyError as exc:
        return exc.reason
    raise AssertionError("The record was applied.")


class PolicyRefusalTextTests(TestCase):
    def setUp(self):
        at_the_door(self)
        self.resource = switched_off()
        self.reason = refusal_of(self.resource)

    def test_every_door_answers_with_the_reason(self):
        for caller in (MANAGER, OPERATOR):
            for door in each_door(self, caller):
                with door:
                    result = door.capability(
                        RECONCILE, {"idempotency_key": "example-reconcile"}, target=self.resource.key
                    )

                    self.assertFalse(result["ok"], result)
                    self.assertEqual(result["error"], {"code": PolicyError.code, "message": self.reason})
                    self.assertFalse(OperationRequest.objects.exists())
