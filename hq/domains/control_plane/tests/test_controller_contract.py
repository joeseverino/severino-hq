"""The controller bridge honours its OpenAPI contract.

The contract is the bridge's own messages joined to what the registry declares
(``test_bridge_registry`` holds the join and the files written from it); the Go
controller's types and client are generated from it. Each test here runs a bridge action through the bridge application, as a
request on its socket, and validates its answer against that action's response
schema, and every payload against its request schema. A field changed on
either side without the contract fails here or in the Go build.
"""

from unittest.mock import patch

from django.test import TestCase
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from hq.domains.control_plane.bridge_actions import ACTIONS
from hq.domains.control_plane.bridge_contract import contract, limit
from hq.domains.control_plane.models import DashboardConfiguration, ManagedResource
from hq.platform.application.glance import panel_specs
from hq.platform.application.infrastructure import ManagedResourceCommand, save_managed_resource
from hq.platform.application.resource_operations import (
    OperationCommand,
    request_certificate_renewal,
    request_reconcile,
)
from hq.platform.application.security import cli_principal

from . import bridge_client
from .test_control_plane import certificate_spec, declare_targets

CONTRACT_URI = "urn:hq:controller-bridge"
CONTRACT = contract()
REGISTRY = Registry().with_resource(CONTRACT_URI, Resource.from_contents(CONTRACT, default_specification=DRAFT202012))


def _pointer(action: str, *parts: str) -> str:
    escaped = [part.replace("~", "~0").replace("/", "~1") for part in ("paths", f"/{action}", "post", *parts)]
    return f"{CONTRACT_URI}#/" + "/".join(escaped)


def _validate(pointer: str, value: object) -> None:
    Draft202012Validator({"$ref": pointer}, registry=REGISTRY).validate(value)


def bridge(action: str, payload: object = None, **options: object) -> dict:
    """Run one bridge action and hold both directions to the contract."""

    if payload is not None:
        _validate(
            _pointer(action, "requestBody", "content", "application/json", "schema"),
            payload,
        )
    result = bridge_client.call(action, payload, **options)
    _validate(
        _pointer(action, "responses", "200", "content", "application/json", "schema"),
        result,
    )
    return result


class ControllerContractTests(TestCase):
    def setUp(self):
        from hq.platform.application.adoption_testing import managing_everything

        managing_everything()
        declare_targets()
        save_managed_resource(
            ManagedResourceCommand(key="example-wildcard", kind="tls.certificate", spec=certificate_spec()),
            principal=cli_principal(),
        )
        self.resource = ManagedResource.objects.get(key="example-wildcard")

    def test_every_bridge_action_is_in_the_contract(self):
        self.assertEqual(
            {f"/{action.name}" for action in ACTIONS},
            set(CONTRACT["paths"]),
        )

    def test_the_contract_names_every_analytics_dimension(self):
        from hq.domains.analytics.models import RumDaily

        dimension = CONTRACT["components"]["schemas"]["AnalyticsRow"]["properties"]["dimension"]
        self.assertEqual(sorted(dimension["enum"]), sorted(RumDaily.Dimension.values))

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
        _validate(f"{CONTRACT_URI}#/components/schemas/GitHubDeliverySpec", delivery)
        route = {"domain": "app.example.com", "upstream": "app:8080"}
        validate_spec("caddy.route", {"connection_ref": "example-edge", **route})
        _validate(f"{CONTRACT_URI}#/components/schemas/CaddyRouteInFile", route)

    def test_a_limit_the_contract_does_not_state_is_an_error(self):
        for path in (
            ("NoSuchSchema", "maxLength"),
            ("CaddyRouteInFile", "properties", "domain", "minimum"),
            ("CaddyRouteInFile", "properties", "domain", "pattern"),
        ):
            with self.subTest(path=path), self.assertRaises((LookupError, ValueError)):
                limit(*path)

    def test_an_empty_queue(self):
        self.assertIsNone(bridge("peek")["operation"])
        self.assertIsNone(bridge("claim", controller_id="example-controller")["operation"])

    @patch(
        "hq.platform.application.resource_operations.controller_action_policy",
        return_value=(True, "active"),
    )
    def test_peek_claim_and_report(self, _policy):
        request_reconcile(
            OperationCommand(idempotency_key="contract-once"),
            principal=cli_principal(),
            current_key=self.resource.key,
        )
        self.assertIsNotNone(bridge("peek")["operation"])
        claimed = bridge("claim", controller_id="example-controller")
        report = {
            "success": True,
            "observed_generation": claimed["resource"]["generation"],
            "status": {},
            "conditions": [
                {
                    "type": "Ready",
                    "status": True,
                    "reason": "Verified",
                    "message": "Verified.",
                }
            ],
            "message": "All consumers verified.",
        }
        bridge(
            "report",
            report,
            controller_id="example-controller",
            operation=claimed["operation"]["id"],
        )

    @patch(
        "hq.platform.application.resource_operations.certificate_renewal_allowed",
        return_value=(True, "active"),
    )
    def test_a_renewal_carries_its_verification_policy(self, _policy):
        request_certificate_renewal(
            OperationCommand(idempotency_key="contract-renew"),
            principal=cli_principal(),
            current_key=self.resource.key,
        )
        expected = {"timeout_seconds": 180, "interval_seconds": 5}
        self.assertEqual(bridge("peek")["verification"], expected)
        claimed = bridge("claim", controller_id="example-controller")
        self.assertEqual(claimed["operation"]["action"], "renew")
        self.assertEqual(claimed["verification"], expected)

    def test_a_certificate_reconcile_verifies_under_the_renewal_policy(self):
        request_reconcile(
            OperationCommand(idempotency_key="contract-plain"),
            principal=cli_principal(),
            current_key=self.resource.key,
        )
        self.assertEqual(
            bridge("peek")["verification"],
            {"timeout_seconds": 180, "interval_seconds": 5},
        )

    def test_export(self):
        self.assertEqual(
            bridge("export", resource=self.resource.key)["resource"]["key"],
            self.resource.key,
        )

    def test_schedule(self):
        bridge("schedule", controller_id="example-controller")

    def test_sweep_due_before_and_after_a_sweep(self):
        first = bridge("sweep-due", controller_id="example-controller")
        self.assertNotIn("age_seconds", first)
        bridge(
            "inventory",
            {"adguard.rewrite": {"ok": True, "records": []}},
            controller_id="example-controller",
        )
        self.assertIn("age_seconds", bridge("sweep-due", controller_id="example-controller"))

    def test_registry(self):
        # The host alone: a composition admits extensions of its own.
        with patch("hq.platform.application.controller.admitted_sources", return_value=()):
            registry = bridge("registry")
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
            registry = bridge("registry")
        self.assertEqual(registry["extensions"], [source])

    def test_glance_plan(self):
        bridge("glance-plan", controller_id="example-controller")

    def test_analytics_plan(self):
        plan = bridge("analytics-plan", [{"connection_ref": "cloudflare", "site_tag": "site"}])
        self.assertEqual(len(plan["windows"]), 1)

    def test_analytics_readings_are_held_to_the_contract(self):
        vitals = {
            "date": "2026-01-01",
            "sample_interval": 1,
            "cumulative_layout_shift": 0.05,
            "largest_contentful_paint_ms": 1800,
            "interaction_to_next_paint_ms": None,
            "first_contentful_paint_ms": 900,
            "time_to_first_byte_ms": 120,
            **{
                f"{metric}_{band}": 0
                for metric in ("lcp", "inp", "cls")
                for band in ("good", "needs_improvement", "poor")
            },
        }
        readings = {
            "sites": [
                {
                    "site_tag": "site",
                    "host": "example.com",
                    "connection_ref": "cloudflare",
                    "start": "2026-01-01",
                    "end": "2026-01-01",
                    "rows": [
                        {
                            "dimension": "path",
                            "value": "/",
                            "date": "2026-01-01",
                            "pageviews": 12,
                            "visits": 9,
                            "sample_interval": 1,
                        }
                    ],
                    "vitals": [vitals],
                }
            ]
        }
        bridge("analytics", readings, controller_id="example-controller")

    def test_reports_are_acknowledged(self):
        bridge(
            "connections",
            [
                {
                    "connection_ref": "adguard",
                    "provider": "adguard",
                    "endpoint": "https://dns.example",
                    "manages": True,
                    "probed": True,
                    "ok": True,
                    "detail": "AdGuard v0.107",
                    "reaches": [],
                }
            ],
            controller_id="example-controller",
        )
        bridge(
            "steps",
            [
                {
                    "step": "adguard.rewrite:reconcile",
                    "subject": "adguard",
                    "reason": "refused",
                }
            ],
            controller_id="example-controller",
        )
        bridge(
            "inventory",
            {
                "adguard.rewrite": {
                    "ok": True,
                    "records": [
                        {
                            "connection_ref": "adguard",
                            "domain": "a.example",
                            "answer": "192.0.2.10",
                            "enabled": True,
                        }
                    ],
                    "refused_parts": [
                        {
                            "part": "querylog",
                            "refusal": "permission",
                            "reason": "Provider request was refused.",
                            "scope": "",
                            "connection_ref": "adguard",
                        }
                    ],
                }
            },
            controller_id="example-controller",
        )
