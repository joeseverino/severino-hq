"""The controller bridge honours its OpenAPI contract.

controller/api/hq-controller.openapi.json is the one description of every
bridge message; the Go controller's types are generated from it. Each test
here runs a bridge action through the management command, exactly as the
controller does, and validates its output against that action's response
schema, and every payload against its request schema. A field changed on
either side without the contract fails here or in the Go build.
"""

from __future__ import annotations

import json
from io import StringIO
from typing import get_args
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from hq.platform.application.glance import panel_specs
from hq.platform.application.infrastructure import ManagedResourceCommand, save_managed_resource
from hq.platform.application.resource_operations import (
    OperationCommand,
    request_certificate_renewal,
    request_reconcile,
)
from hq.platform.application.security import cli_principal
from hq.domains.control_plane.bridge_contract import contract, keyword, limit
from hq.domains.control_plane.management.commands.infrastructure_controller import ACTIONS
from hq.domains.control_plane.models import DashboardConfiguration, ManagedResource
from hq.domains.control_plane.observations import OBSERVATIONS
from hq.domains.control_plane.provider_adapters.contracts import FAILURES, REFUSALS
from hq.domains.control_plane.provider_adapters.tls import TLSConsumer
from hq.domains.control_plane.providers import OBSERVATION_KINDS, PROVIDERS

from .test_control_plane import certificate_spec, declare_targets

CONTRACT_URI = "urn:hq:controller-bridge"
CONTRACT = contract()
SWEPT = set(CONTRACT["components"]["schemas"]["SweptKind"]["enum"])
REGISTRY = Registry().with_resource(
    CONTRACT_URI, Resource.from_contents(CONTRACT, default_specification=DRAFT202012)
)


def _pointer(action: str, *parts: str) -> str:
    escaped = [
        part.replace("~", "~0").replace("/", "~1")
        for part in ("paths", f"/{action}", "post", *parts)
    ]
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
        options["payload"] = "-"
    out = StringIO()
    with patch(
        "sys.stdin", StringIO(json.dumps(payload) if payload is not None else "")
    ):
        call_command("infrastructure_controller", action, stdout=out, **options)
    result = json.loads(out.getvalue())
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
            ManagedResourceCommand(
                key="example-wildcard", kind="tls.certificate", spec=certificate_spec()
            ),
            principal=cli_principal(),
        )
        self.resource = ManagedResource.objects.get(key="example-wildcard")

    def test_every_bridge_action_is_in_the_contract(self):
        self.assertEqual(
            {f"/{action.name}" for action in ACTIONS},
            set(CONTRACT["paths"]),
        )

    def test_the_contract_names_every_kind_the_registry_declares(self):
        schemas = CONTRACT["components"]["schemas"]
        self.assertEqual(
            set(schemas["ResourceKind"]["enum"]),
            set(PROVIDERS) | set(OBSERVATION_KINDS),
        )

    def test_every_controller_reading_is_swept(self):
        controller_read = {
            kind for kind, spec in OBSERVATIONS.items() if spec.read_by == "controller"
        }
        self.assertEqual(sorted(controller_read - SWEPT), [])
        self.assertEqual(sorted(SWEPT - set(PROVIDERS) - controller_read), [])

    def test_a_kind_nothing_sweeps_says_why(self):
        for kind, provider in sorted(PROVIDERS.items()):
            if kind in SWEPT:
                continue
            with self.subTest(kind=kind):
                self.assertTrue(
                    provider.unobserved_reason,
                    f"nothing sweeps {kind!r} and its provider does not say why",
                )

    def test_a_swept_kind_does_not_claim_otherwise(self):
        for kind in sorted(SWEPT & set(PROVIDERS)):
            with self.subTest(kind=kind):
                self.assertFalse(PROVIDERS[kind].unobserved_reason)

    def test_the_contract_names_every_analytics_dimension(self):
        from hq.domains.analytics.models import RumDaily

        dimension = CONTRACT["components"]["schemas"]["AnalyticsRow"]["properties"]["dimension"]
        self.assertEqual(sorted(dimension["enum"]), sorted(RumDaily.Dimension.values))

    def test_the_contract_names_every_failure_class(self):
        schemas = CONTRACT["components"]["schemas"]
        self.assertEqual(set(schemas["FailureClass"]["enum"]), {"", *FAILURES})
        self.assertEqual(set(schemas["Refusal"]["enum"]), {"", *REFUSALS})

    def test_the_contract_names_every_connection_provider(self):
        from hq.domains.control_plane.provider_adapters import CONNECTIONS

        self.assertEqual(
            set(CONTRACT["components"]["schemas"]["ConnectionProvider"]["enum"]),
            set(CONNECTIONS),
        )

    def test_the_contract_names_every_reading_part(self):
        from hq.domains.control_plane.reading_parts import parts_of

        declared = {
            part
            for kind in set(PROVIDERS) | set(OBSERVATION_KINDS)
            for part in parts_of(kind)
        }
        self.assertEqual(
            set(CONTRACT["components"]["schemas"]["ReadingPartName"]["enum"]), declared
        )

    def test_the_contract_names_every_tls_consumer_kind(self):
        union, _field = get_args(TLSConsumer)
        kinds = {
            literal
            for model in get_args(union)
            for literal in get_args(model.model_fields["kind"].annotation)
        }
        self.assertEqual(
            set(CONTRACT["components"]["schemas"]["TLSConsumerKind"]["enum"]), kinds
        )

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

    def test_a_keyword_the_contract_does_not_state_is_an_error(self):
        for path in (
            ("NoSuchSchema", "pattern"),
            ("CaddyRouteInFile", "properties", "domain", "default"),
            ("CaddyRouteInFile", "properties", "domain", "maxLength"),
            ("GitHubDeliveryProduction", "enum", 1),
        ):
            with self.subTest(path=path), self.assertRaises((LookupError, ValueError)):
                keyword(*path)
        with self.assertRaises(ValueError):
            limit("CaddyRouteInFile", "properties", "domain", "pattern")

    def test_an_empty_queue(self):
        self.assertIsNone(bridge("peek")["operation"])
        self.assertIsNone(
            bridge("claim", controller_id="example-controller")["operation"]
        )

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
        self.assertIn(
            "age_seconds", bridge("sweep-due", controller_id="example-controller")
        )

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
            "plugin": "example", "source_repository": "example/ext",
            "source_workflow": ".github/workflows/admit.yml", "source_commit": "0" * 40,
        }
        with patch("hq.platform.application.controller.admitted_sources", return_value=(source,)):
            registry = bridge("registry")
        self.assertEqual(registry["extensions"], [source])

    def test_glance_plan(self):
        bridge("glance-plan", controller_id="example-controller")

    def test_analytics_plan(self):
        plan = bridge(
            "analytics-plan", [{"connection_ref": "cloudflare", "site_tag": "site"}]
        )
        self.assertEqual(len(plan["windows"]), 1)

    def test_analytics_readings_are_held_to_the_contract(self):
        vitals = {
            "date": "2026-01-01", "sample_interval": 1, "cumulative_layout_shift": 0.05,
            "largest_contentful_paint_ms": 1800, "interaction_to_next_paint_ms": None,
            "first_contentful_paint_ms": 900, "time_to_first_byte_ms": 120,
            **{f"{metric}_{band}": 0 for metric in ("lcp", "inp", "cls")
               for band in ("good", "needs_improvement", "poor")},
        }
        readings = {"sites": [{
            "site_tag": "site", "host": "example.com", "connection_ref": "cloudflare",
            "start": "2026-01-01", "end": "2026-01-01",
            "rows": [{"dimension": "path", "value": "/", "date": "2026-01-01",
                      "pageviews": 12, "visits": 9, "sample_interval": 1}],
            "vitals": [vitals],
        }]}
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
