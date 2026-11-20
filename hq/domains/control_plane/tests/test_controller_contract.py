"""The controller bridge honors its OpenAPI contract.

The contract is the bridge's own messages joined to what the registry declares
(``test_bridge_registry`` holds the join and the files written from it); the Go
controller's types and client are generated from it. Each test here runs a
bridge action through ``bridge_client.call``, which holds its answer to the
action's response schema; the application holds every payload to its request
schema. A field changed on either side without the contract fails here or in
the Go build.
"""

from unittest.mock import patch

from django.test import TestCase

from hq.domains.control_plane.bridge_contract import contract, departs, limit
from hq.domains.control_plane.models import DashboardConfiguration, ManagedResource, OperationRequest
from hq.platform.application.glance import panel_specs
from hq.platform.application.infrastructure import ManagedResourceCommand, save_managed_resource
from hq.platform.application.resource_operations import (
    OperationCommand,
    request_certificate_renewal,
    request_reconcile,
)
from hq.platform.application.security import cli_principal
from hq.platform.application.tests.consent import consented

from . import bridge_client
from .certificate_cases import certificate_spec, declare_targets

CERTIFICATE = [f"tls.certificate:{action}" for action in ("reconcile", "renew")]
CONTRACT = contract()


class ControllerContractTests(TestCase):
    def setUp(self):
        from hq.platform.application.adoption_testing import managing_everything

        managing_everything()
        declare_targets()
        save_managed_resource(
            ManagedResourceCommand(key="example-wildcard", kind="tls.certificate", spec=certificate_spec()),
            consented=consented(cli_principal()),
        )
        OperationRequest.objects.all().delete()  # applied, so the reconcile its save queued is done
        self.resource = ManagedResource.objects.get(key="example-wildcard")

    def test_the_contract_names_every_glance_panel(self):
        # The weather panel is declared only once a point is configured.
        configuration = DashboardConfiguration(weather_point="40.0,-75.0")
        self.assertEqual(
            set(CONTRACT["components"]["schemas"]["GlancePanelID"]["enum"]),
            {spec.id for spec in panel_specs(configuration)},
        )

    def test_a_declared_spec_matches_the_contracts_schema(self):
        from hq.domains.control_plane.providers import validate_spec

        delivery = validate_spec("github.delivery", {"repository": "example/host"})
        self.assertIsNone(departs("GitHubDeliverySpec", delivery))
        route = {"domain": "app.example.com", "upstream": "app:8080"}
        validate_spec("caddy.route", {"connection_ref": "example-edge", **route})
        self.assertIsNone(departs("CaddyRouteInFile", route))

    def test_a_limit_the_contract_does_not_state_is_an_error(self):
        for path in (
            ("NoSuchSchema", "maxLength"),
            ("CaddyRouteInFile", "properties", "domain", "minimum"),
            ("CaddyRouteInFile", "properties", "domain", "pattern"),
        ):
            with self.subTest(path=path), self.assertRaises((LookupError, ValueError)):
                limit(*path)

    def test_an_empty_queue(self):
        self.assertIsNone(bridge_client.call("peek", capability=CERTIFICATE)["operation"])
        self.assertIsNone(
            bridge_client.call("claim", controller_id="example-controller", capability=CERTIFICATE)["operation"]
        )

    @patch(
        "hq.platform.application.resource_operations.certificate_renewal_allowed",
        return_value=(True, "active"),
    )
    def test_a_renewal_carries_its_verification_policy(self, _policy):
        request_certificate_renewal(
            OperationCommand(),
            consented=consented(cli_principal()),
            current_key=self.resource.key,
        )
        expected = {"timeout_seconds": 180, "interval_seconds": 5}
        self.assertEqual(bridge_client.call("peek", capability=CERTIFICATE)["verification"], expected)
        claimed = bridge_client.call("claim", controller_id="example-controller", capability=CERTIFICATE)
        self.assertEqual(claimed["operation"]["action"], "renew")
        self.assertEqual(claimed["verification"], expected)

    def test_a_certificate_reconcile_verifies_under_the_renewal_policy(self):
        request_reconcile(
            OperationCommand(),
            consented=consented(cli_principal()),
            current_key=self.resource.key,
        )
        self.assertEqual(
            bridge_client.call("peek", capability=CERTIFICATE)["verification"],
            {"timeout_seconds": 180, "interval_seconds": 5},
        )

    def test_export(self):
        self.assertEqual(
            bridge_client.call("export", resource=self.resource.key)["resource"]["key"],
            self.resource.key,
        )

    def test_schedule(self):
        bridge_client.call("schedule", controller_id="example-controller")

    def test_sweep_due_before_and_after_a_sweep(self):
        first = bridge_client.call("sweep-due", controller_id="example-controller")
        self.assertNotIn("age_seconds", first)
        bridge_client.call(
            "inventory",
            {"adguard.rewrite": {"ok": True, "records": []}},
            controller_id="example-controller",
        )
        self.assertIn("age_seconds", bridge_client.call("sweep-due", controller_id="example-controller"))

    def test_registry(self):
        # The host alone: a composition admits extensions of its own.
        with patch("hq.platform.application.controller.admitted_sources", return_value=()):
            registry = bridge_client.call("registry")
        self.assertIn({"kind": "adguard.rewrite", "action": "reconcile"}, registry["capabilities"])
        self.assertIn("tls.uploaded_certificate", registry["material_kinds"])
        self.assertTrue(all(entry["reason"] for entry in registry["locked"]))
        self.assertEqual(registry["extensions"], [])

    def test_the_registry_names_the_extensions_the_image_composes(self):
        source = {
            "plugin": "example",
            "source_repository": "example/ext",
            "source_workflow": ".github/workflows/admit.yml",
            "source_commit": "0" * 40,
        }
        with patch("hq.platform.application.controller.admitted_sources", return_value=(source,)):
            registry = bridge_client.call("registry")
        self.assertEqual(registry["extensions"], [source])

    def test_glance_plan(self):
        bridge_client.call("glance-plan", controller_id="example-controller")

    def test_analytics_plan(self):
        plan = bridge_client.call("analytics-plan", [{"connection_ref": "cloudflare", "site_tag": "site"}])
        self.assertEqual(len(plan["windows"]), 1)
