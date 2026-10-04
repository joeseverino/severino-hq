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
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.core.management import call_command
from django.test import TestCase
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from application.infrastructure import ManagedResourceCommand, save_managed_resource
from application.resource_operations import (
    OperationCommand,
    request_certificate_renewal,
    request_reconcile,
)
from application.security import cli_principal
from control_plane.management.commands.infrastructure_controller import ACTIONS
from control_plane.models import ManagedResource

from .test_control_plane import certificate_spec, declare_targets

CONTRACT_PATH = (
    Path(settings.BASE_DIR) / "controller" / "api" / "hq-controller.openapi.json"
)
CONTRACT_URI = "urn:hq:controller-bridge"
CONTRACT = json.loads(CONTRACT_PATH.read_text())
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
        from application.adoption_testing import managing_everything

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

    def test_an_empty_queue(self):
        self.assertIsNone(bridge("peek")["operation"])
        self.assertIsNone(
            bridge("claim", controller_id="example-controller")["operation"]
        )

    @patch(
        "application.resource_operations.controller_action_policy",
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
        "application.resource_operations.certificate_renewal_allowed",
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
        registry = bridge("registry")
        self.assertIn({"kind": "adguard.rewrite", "action": "reconcile"}, registry["capabilities"])
        self.assertIn("tls.uploaded_certificate", registry["material_kinds"])
        self.assertTrue(all(entry["reason"] for entry in registry["locked"]))

    def test_glance_plan(self):
        bridge("glance-plan", controller_id="example-controller")

    def test_analytics_plan(self):
        plan = bridge(
            "analytics-plan", [{"connection_ref": "cloudflare", "site_tag": "site"}]
        )
        self.assertEqual(len(plan["windows"]), 1)

    def test_analytics_readings_are_held_to_the_contract(self):
        from controller_runtime import cloudflare_analytics

        data = {"viewer": {"accounts": [{
            "path": [{"count": 12, "sum": {"visits": 9}, "avg": {"sampleInterval": 1},
                      "dimensions": {"date": "2026-01-01", "requestPath": "/"}}],
            "vitals": [{"count": 3, "avg": {"sampleInterval": 1},
                        "quantiles": {"largestContentfulPaintP75": 1800000, "interactionToNextPaintP75": -1,
                                      "firstContentfulPaintP75": 900000, "timeToFirstByteP75": 120000,
                                      "cumulativeLayoutShiftP75": 0.05},
                        "sum": {"lcpGood": 2, "lcpPoor": 1}, "dimensions": {"date": "2026-01-01"}}],
        }]}}
        site = {"site_tag": "site", "host": "example.com", "account": "account", "connection_ref": "cloudflare"}
        with patch.object(cloudflare_analytics, "_cloudflare_graphql", return_value=data):
            readings = cloudflare_analytics.analytics(sites=[site], windows=[])
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
