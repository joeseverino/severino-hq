"""A use case's refusal reaches the caller with its own text, whichever adapter carried it."""

import json
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import TestCase, override_settings

from hq.domains.control_plane.models import ManagedResource, OperationRequest
from hq.platform.mcp import services
from hq.platform.mcp.identity import reset_principal, set_principal

from ..adoption_testing import managing_everything
from ..capabilities import execute_capability
from ..record_errors import PolicyError
from ..resource_operations import OperationCommand, request_reconcile
from ..security import cli_principal, mcp_principal

RECONCILE = "infrastructure.reconcile"


def switched_off() -> ManagedResource:
    """A record HQ holds switched off, which refuses to be applied."""

    managing_everything()
    return ManagedResource.objects.create(
        key="app-dns",
        kind="adguard.rewrite",
        spec={"domain": "app.example.com", "answer": "192.0.2.10"},
        enabled=False,
    )


def refusal_of(resource: ManagedResource) -> str:
    """What the use case itself says, so each adapter is held to it."""

    try:
        request_reconcile(
            OperationCommand(idempotency_key="direct"), principal=cli_principal(), current_key=resource.key
        )
    except PolicyError as exc:
        return exc.reason
    raise AssertionError("The record was applied.")


class PolicyRefusalTextTests(TestCase):
    def setUp(self):
        self.resource = switched_off()
        self.reason = refusal_of(self.resource)

    def assertRefused(self, result):
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["error"], {"code": PolicyError.code, "message": self.reason})
        self.assertFalse(OperationRequest.objects.exists())

    def test_the_chokepoint_answers_with_the_reason(self):
        self.assertRefused(execute_capability(RECONCILE, {}, principal=cli_principal(), target=self.resource.key))

    def test_the_operator_command_line_answers_with_the_reason(self):
        request = {"tool": "execute_capability", "arguments": {"name": RECONCILE, "payload": {}, "target": "app-dns"}}
        output = StringIO()
        with mock.patch("sys.stdin", StringIO(json.dumps(request))):
            call_command("hq_call", stdout=output)

        self.assertRefused(json.loads(output.getvalue()))

    @override_settings(SEVERINO_MCP_ENABLE_INFRASTRUCTURE=True)
    def test_an_agent_is_answered_with_the_reason(self):
        bound = set_principal(mcp_principal())
        self.addCleanup(reset_principal, bound)

        self.assertRefused(services.execute_capability(RECONCILE, {}, target=self.resource.key))
