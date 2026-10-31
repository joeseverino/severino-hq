"""``infrastructure.resource.update`` says HQ then applies the change, so the write queues the apply."""

from unittest.mock import patch

from django.test import TestCase, override_settings

from hq.platform.application.adoption_testing import managing_everything
from hq.platform.application.capabilities import execute_capability
from hq.platform.application.infrastructure import ManagedResourceCommand, save_managed_resource
from hq.platform.application.security import cli_principal, mcp_principal

from ..models import ManagedResource, OperationRequest

PROXY = {
    "domain_names": ["app.example.com"],
    "forward_scheme": "http",
    "forward_host": "192.0.2.10",
    "forward_port": 8000,
}


class SavingADeclarationAppliesItTests(TestCase):
    """``infrastructure.resource.update`` says HQ then applies the change, so the write queues the apply."""

    def setUp(self):
        managing_everything()
        save_managed_resource(
            ManagedResourceCommand(key="example-proxy", kind="npm.proxy_host", spec=dict(PROXY)),
            principal=cli_principal(),
        )
        OperationRequest.objects.all().delete()
        ManagedResource.objects.filter(key="example-proxy").update(observed_generation=1)

    def _update(self, *, principal=None, **spec):
        return execute_capability(
            "infrastructure.resource.update",
            {"key": "example-proxy", "kind": "npm.proxy_host", "spec": {**PROXY, **spec}},
            principal=principal or cli_principal(),
            target="example-proxy",
        )

    @patch("hq.platform.application.resource_operations.ring_doorbell")
    def test_an_update_queues_the_reconcile_of_its_new_generation(self, doorbell):
        with self.captureOnCommitCallbacks(execute=True):
            result = self._update(forward_port=8080)

        self.assertTrue(result["ok"], result)
        resource = ManagedResource.objects.get(key="example-proxy")
        operation = OperationRequest.objects.get(resource=resource)
        self.assertEqual(operation.action, OperationRequest.Action.RECONCILE)
        self.assertEqual(operation.state, OperationRequest.State.QUEUED)
        self.assertEqual(operation.input["generation"], resource.generation)
        self.assertEqual(operation.requested_interface, "cli")
        self.assertEqual(result["applying"]["id"], str(operation.id))
        doorbell.assert_called_once()

    def test_a_save_that_changes_nothing_queues_nothing(self):
        result = self._update()

        self.assertTrue(result["ok"], result)
        self.assertIsNone(result["applying"])
        self.assertFalse(OperationRequest.objects.exists())

    def test_a_record_switched_off_is_not_applied(self):
        save_managed_resource(
            ManagedResourceCommand(key="example-proxy", kind="npm.proxy_host", spec=dict(PROXY), enabled=False),
            principal=cli_principal(),
            current_key="example-proxy",
        )

        self.assertFalse(OperationRequest.objects.exists())

    def test_a_kind_applied_only_when_asked_is_not_queued(self):
        save_managed_resource(
            ManagedResourceCommand(
                key="a-domain", kind="cloudflare.zone", spec={"zone": "example.com", "connection_ref": "cf-example"}
            ),
            principal=cli_principal(),
        )

        self.assertFalse(OperationRequest.objects.exists())

    def test_a_copy_of_what_is_live_has_nothing_to_apply(self):
        save_managed_resource(
            ManagedResourceCommand(key="example-proxy", kind="npm.proxy_host", spec={**PROXY, "forward_port": 8080}),
            principal=cli_principal(),
            current_key="example-proxy",
            copied_from_live=True,
        )

        self.assertFalse(OperationRequest.objects.exists())

    @override_settings(SEVERINO_MCP_ENABLE_INFRASTRUCTURE=False)
    def test_a_caller_without_infrastructure_authority_changes_and_queues_nothing(self):
        result = self._update(principal=mcp_principal(), forward_port=8080)

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "forbidden")
        self.assertEqual(ManagedResource.objects.get(key="example-proxy").spec["forward_port"], 8000)
        self.assertFalse(OperationRequest.objects.exists())
