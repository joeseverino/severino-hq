from __future__ import annotations

import json
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from application.capabilities import execute_capability
from application.controller import ControllerReport, report_operation
from application.connections import preflight_connections
from application.infrastructure import (
    ManagedResourceCommand,
    OperationCommand,
    PolicyError,
    request_certificate_renewal,
    request_reconcile,
    save_managed_resource,
)
from application.security import cli_principal, mcp_principal

from .models import ManagedResource, OperationRequest
from .providers import (
    describe_providers,
    validate_resolved_certificate,
)
from .topology import import_topology


def certificate_spec():
    return {
        "topology_ref": "pki:jseverino-wildcard",
        "renewal_window_days": 30,
    }


def resolved_certificate_spec():
    return {
        "certificate_name": "jseverino",
        "domains": [
            "jseverino.com",
            "*.jseverino.com",
            "jseverino.net",
            "*.jseverino.net",
            "jseverino.org",
            "*.jseverino.org",
            "joeseverino.com",
            "*.joeseverino.com",
        ],
        "consumers": [
            {
                "kind": "npm",
                "topology_ref": "container:homelab-server/npm",
                "name": "jseverino-wildcard",
                "connection_ref": "homelab-npm",
                "certificate_id": 11,
                "verify_domains": ["dev.jseverino.com"],
            },
            {
                "kind": "caddy",
                "topology_ref": "container:edge/caddy",
                "name": "edge-caddy",
                "connection_ref": "edge",
                "certificate_directory": "/opt/apps/caddy/certs",
                "verify_domains": ["health.jseverino.com"],
            },
            {
                "kind": "cpanel",
                "topology_ref": "external:namecheap-cpanel",
                "name": "namecheap-shared-hosting",
                "connection_ref": "namecheap-cpanel",
                "install_domains": ["jseverino.com", "jseverino.net"],
                "verify_domains": ["jseverino.com", "quiz.jseverino.net"],
            },
        ],
        "renewal_window_days": 30,
    }


def topology_payload():
    spec = resolved_certificate_spec()
    consumers = spec.pop("consumers")
    return {
        "version": 3,
        "hosts": [
            {
                "id": "homelab-server",
                "containers": [{"id": "npm"}],
            },
            {
                "id": "edge",
                "containers": [{"id": "caddy"}],
            },
        ],
        "pki": [
            {
                "id": "jseverino-wildcard",
                "certificate_name": "jseverino",
                "domains": spec["domains"],
            }
        ],
        "externals": [{"id": "namecheap-cpanel"}],
        "dependencies": [
            {
                "from": "container:homelab-server/npm",
                "relation": "consumes",
                "to": "pki:jseverino-wildcard",
                "attributes": {
                    key: value
                    for key, value in consumers[0].items()
                    if key != "topology_ref"
                },
            },
            {
                "from": "container:edge/caddy",
                "relation": "consumes",
                "to": "pki:jseverino-wildcard",
                "attributes": {
                    key: value
                    for key, value in consumers[1].items()
                    if key != "topology_ref"
                },
            },
            {
                "from": "external:namecheap-cpanel",
                "relation": "consumes",
                "to": "pki:jseverino-wildcard",
                "attributes": {
                    key: value
                    for key, value in consumers[2].items()
                    if key != "topology_ref"
                },
            },
        ],
        "managed_resources": [],
    }


class TopologyMaterializationTests(TestCase):
    def test_import_materializes_and_updates_declared_resources(self):
        payload = topology_payload()
        payload["managed_resources"] = [
            {
                "key": "hq-dns",
                "kind": "adguard.rewrite",
                "spec": {
                    "domain": "hq.jseverino.com",
                    "answer": "192.168.1.233",
                },
                "enabled": True,
            }
        ]

        import_topology(payload)
        resource = ManagedResource.objects.get(key="hq-dns")
        self.assertEqual(resource.declaration_source, "topology")
        self.assertEqual(resource.generation, 1)

        payload["managed_resources"][0]["spec"]["answer"] = "192.168.1.234"
        import_topology(payload)
        resource.refresh_from_db()
        self.assertEqual(resource.spec["answer"], "192.168.1.234")
        self.assertEqual(resource.generation, 2)


class ProviderContractTests(TestCase):
    def test_provider_preflight_authenticates_without_returning_secrets(self):
        class Response:
            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self):
                return json.dumps(self.payload).encode()

        def open_url(request, timeout, context):
            self.assertEqual(timeout, 10)
            self.assertIsNotNone(context)
            if request.full_url.endswith("/control/status"):
                return Response({"dns_addresses": ["0.0.0.0"]})
            return Response({"token": "not-returned-by-probe"})

        env = {
            "ADGUARD_CONNECTION_REF": "homelab-adguard",
            "ADGUARD_URL": "https://adguard.homelab",
            "ADGUARD_USERNAME": "admin",
            "ADGUARD_PASSWORD": "secret-a",
            "NPM_CONNECTION_REF": "homelab-npm",
            "NPM_URL": "https://proxy.homelab",
            "NPM_USERNAME": "controller@example.com",
            "NPM_PASSWORD": "secret-b",
        }
        probes = preflight_connections(env, open_url=open_url)
        rendered = repr(probes)
        self.assertTrue(all(probe.ok for probe in probes))
        self.assertNotIn("secret-a", rendered)
        self.assertNotIn("secret-b", rendered)

    def test_provider_catalog_is_stable_strict_and_marks_public_effects(self):
        self.assertEqual(describe_providers(), describe_providers())
        providers = {
            item["kind"]: item for item in describe_providers()["providers"]
        }
        self.assertFalse(providers["adguard.rewrite"]["public_effect"])
        self.assertTrue(providers["cloudflare.dns_record"]["public_effect"])
        self.assertEqual(
            providers["adguard.rewrite"]["controller"]["actions"]["reconcile"][
                "mode"
            ],
            "apply",
        )
        self.assertEqual(
            providers["tls.certificate"]["controller"]["actions"]["renew"]["mode"],
            "locked",
        )
        self.assertFalse(
            providers["tls.certificate"]["spec_schema"]["additionalProperties"]
        )
        self.assertIn(
            "topology_ref",
            providers["tls.certificate"]["spec_schema"]["properties"],
        )

    def test_certificate_contract_normalizes_and_validates_deployments(self):
        spec = resolved_certificate_spec()
        spec["domains"][0] = "JSEVERINO.COM."
        self.assertEqual(
            validate_resolved_certificate(spec)["domains"][0],
            "jseverino.com",
        )

        spec["consumers"][-1]["install_domains"].append("not-covered.example")
        with self.assertRaisesRegex(ValueError, "must be present"):
            validate_resolved_certificate(spec)

    def test_invalid_provider_payload_fails_before_persistence(self):
        result = execute_capability(
            "infrastructure.resource.create",
            {
                "key": "bad-proxy",
                "kind": "npm.proxy_host",
                "spec": {
                    "domain_names": ["dev-hq.jseverino.com"],
                    "forward_scheme": "http",
                    "forward_host": "100.72.194.77",
                    "forward_port": 70000,
                    "unexpected": "never accepted",
                },
            },
            principal=cli_principal(),
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "invalid_input")
        self.assertFalse(ManagedResource.objects.exists())

    def test_public_dns_is_declarable_disabled_but_cannot_be_enabled(self):
        disabled = save_managed_resource(
            ManagedResourceCommand(
                key="future-public-hq",
                kind="cloudflare.dns_record",
                enabled=False,
                spec={
                    "zone": "jseverino.com",
                    "name": "hq.jseverino.com",
                    "record_type": "A",
                    "content": "192.0.2.1",
                },
            ),
            principal=cli_principal(),
        )
        self.assertTrue(disabled["ok"])
        with self.assertRaisesRegex(PolicyError, "Public DNS"):
            save_managed_resource(
                ManagedResourceCommand(
                    key="public-hq",
                    kind="cloudflare.dns_record",
                    spec={
                        "zone": "jseverino.com",
                        "name": "hq.jseverino.com",
                        "record_type": "A",
                        "content": "192.0.2.1",
                    },
                ),
                principal=cli_principal(),
            )


class InfrastructureWebTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="operator",
            password="test-only-password",
        )
        self.client.force_login(self.user)
        import_topology(topology_payload())
        save_managed_resource(
            ManagedResourceCommand(
                key="jseverino-wildcard",
                kind="tls.certificate",
                spec=certificate_spec(),
            ),
            principal=cli_principal(),
        )
        self.resource = ManagedResource.objects.get(key="jseverino-wildcard")

    def test_detail_distinguishes_observation_from_locked_renewal(self):
        response = self.client.get(
            reverse("control_plane:detail", kwargs={"key": self.resource.key})
        )

        self.assertContains(response, "Observe: Apply")
        self.assertContains(response, "Renew: Locked")
        self.assertContains(response, "Renewal policy")
        self.assertContains(response, ">Locked<", html=False)

    def test_public_certificate_download_never_serves_private_key(self):
        self.resource.status = {
            "certificate_pem": "-----BEGIN PRIVATE KEY-----\nunsafe\n"
        }
        self.resource.save(update_fields=("status",))
        response = self.client.get(
            reverse(
                "control_plane:certificate_download",
                kwargs={"key": self.resource.key},
            )
        )
        self.assertEqual(response.status_code, 500)

        self.resource.status = {
            "certificate_pem": "-----BEGIN CERTIFICATE-----\npublic\n"
        }
        self.resource.save(update_fields=("status",))
        response = self.client.get(
            reverse(
                "control_plane:certificate_download",
                kwargs={"key": self.resource.key},
            )
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/x-pem-file")
        self.assertIn("jseverino-wildcard-public.pem", response["Content-Disposition"])


class OperationPolicyTests(TestCase):
    def setUp(self):
        import_topology(topology_payload())
        save_managed_resource(
            ManagedResourceCommand(
                key="jseverino-wildcard",
                kind="tls.certificate",
                spec=certificate_spec(),
            ),
            principal=cli_principal(),
        )
        self.resource = ManagedResource.objects.get(key="jseverino-wildcard")

    def test_locked_controller_capability_cannot_queue_work(self):
        with self.assertRaisesRegex(PolicyError, "not provisioned"):
            request_certificate_renewal(
                OperationCommand(idempotency_key="renew-locked"),
                principal=cli_principal(),
                current_key=self.resource.key,
            )

    @override_settings(SEVERINO_INFRASTRUCTURE_ENABLE_PUBLIC_DNS=True)
    def test_locked_reconcile_capability_cannot_queue_work(self):
        save_managed_resource(
            ManagedResourceCommand(
                key="future-public-hq",
                kind="cloudflare.dns_record",
                spec={
                    "zone": "jseverino.com",
                    "name": "hq.jseverino.com",
                    "record_type": "A",
                    "content": "192.0.2.1",
                },
            ),
            principal=cli_principal(),
        )

        with self.assertRaisesRegex(PolicyError, "Public DNS reconciliation"):
            request_reconcile(
                OperationCommand(idempotency_key="public-dns-locked"),
                principal=cli_principal(),
                current_key="future-public-hq",
            )
        self.assertFalse(OperationRequest.objects.exists())

    @patch(
        "application.infrastructure.controller_action_policy",
        return_value=(True, "active"),
    )
    def test_renewal_is_blocked_outside_window(self, _policy):
        self.resource.status = {
            "not_after": (timezone.now() + timedelta(days=45)).isoformat()
        }
        self.resource.save()
        with self.assertRaisesRegex(PolicyError, "Renewal opens"):
            request_certificate_renewal(
                OperationCommand(idempotency_key="renew-too-early"),
                principal=cli_principal(),
                current_key=self.resource.key,
            )

    @patch(
        "application.infrastructure.controller_action_policy",
        return_value=(True, "active"),
    )
    def test_renewal_is_allowed_for_drift_and_idempotent(self, _policy):
        self.resource.status = {
            "not_after": (timezone.now() + timedelta(days=45)).isoformat()
        }
        self.resource.conditions = [
            {"type": "Drifted", "status": True, "reason": "NPMStale"}
        ]
        self.resource.save()
        first = request_certificate_renewal(
            OperationCommand(idempotency_key="renew-drift"),
            principal=cli_principal(),
            current_key=self.resource.key,
        )
        second = request_certificate_renewal(
            OperationCommand(idempotency_key="renew-drift"),
            principal=cli_principal(),
            current_key=self.resource.key,
        )
        self.assertTrue(first["queued"])
        self.assertFalse(second["queued"])
        self.assertEqual(OperationRequest.objects.count(), 1)

    @override_settings(SEVERINO_MCP_ENABLE_INFRASTRUCTURE=False)
    def test_mcp_infrastructure_is_fail_closed(self):
        result = execute_capability(
            "certificate.renew",
            {"idempotency_key": "mcp-denied"},
            principal=mcp_principal(),
            target=self.resource.key,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "forbidden")

    @patch(
        "application.infrastructure.controller_action_policy",
        return_value=(True, "active"),
    )
    def test_controller_claim_and_report_updates_observed_state(self, _policy):
        queued = request_reconcile(
            OperationCommand(idempotency_key="reconcile-once"),
            principal=cli_principal(),
            current_key=self.resource.key,
        )
        claimed = StringIO()
        call_command(
            "infrastructure_controller",
            "claim",
            controller_id="homelab-controller",
            stdout=claimed,
        )
        claim_payload = json.loads(claimed.getvalue())
        self.assertEqual(
            claim_payload["operation"]["id"], queued["operation"]["id"]
        )
        self.assertEqual(
            claim_payload["resource"]["spec"]["certificate_name"],
            "jseverino",
        )

        reported = StringIO()
        call_command(
            "infrastructure_controller",
            "report",
            controller_id="homelab-controller",
            operation=queued["operation"]["id"],
            payload=json.dumps(
                {
                    "success": True,
                    "observed_generation": self.resource.generation,
                    "status": {
                        "not_after": (
                            timezone.now() + timedelta(days=89)
                        ).isoformat()
                    },
                    "conditions": [
                        {"type": "Ready", "status": True, "reason": "Verified"}
                    ],
                    "message": "All consumers verified.",
                }
            ),
            stdout=reported,
        )
        self.resource.refresh_from_db()
        self.assertEqual(
            self.resource.observed_generation, self.resource.generation
        )
        self.assertEqual(
            json.loads(reported.getvalue())["operation"]["state"], "succeeded"
        )


class InfrastructureViewsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="joe", password="test-password"
        )
        self.resource = ManagedResource.objects.create(
            key="jseverino-wildcard",
            kind="tls.certificate",
            spec=certificate_spec(),
            status={"certificate_pem": "PUBLIC CERTIFICATE ONLY"},
        )
        self.client.force_login(self.user)

    def test_private_dashboard_and_public_certificate_download(self):
        dashboard = self.client.get(reverse("control_plane:list"))
        self.assertContains(dashboard, "jseverino-wildcard")

        download = self.client.get(
            reverse("control_plane:certificate_download", args=[self.resource.key])
        )
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.content, b"PUBLIC CERTIFICATE ONLY")
        self.assertNotIn(b"PRIVATE", download.content)

        report = self.client.get(
            reverse("control_plane:report_download", args=[self.resource.key])
        )
        self.assertEqual(report.status_code, 200)
        self.assertEqual(report.json()["resource"]["key"], self.resource.key)

    def test_controller_rejects_secret_material_in_status(self):
        operation = OperationRequest.objects.create(
            resource=self.resource,
            action=OperationRequest.Action.RECONCILE,
            state=OperationRequest.State.CLAIMED,
            requested_actor="operator",
            requested_interface="cli",
            idempotency_key="secret-report",
            input={"generation": self.resource.generation},
            claimed_by="controller",
            claimed_at=timezone.now(),
        )
        with self.assertRaisesRegex(ValueError, "secret-bearing"):
            report_operation(
                str(operation.id),
                ControllerReport(
                    success=True,
                    observed_generation=self.resource.generation,
                    status={"api_token": "must-never-enter-hq"},
                ),
                controller_id="controller",
            )
        with self.assertRaisesRegex(ValueError, "private-key material"):
            report_operation(
                str(operation.id),
                ControllerReport(
                    success=True,
                    observed_generation=self.resource.generation,
                    status={
                        "certificate_pem": "-----BEGIN PRIVATE KEY-----\nnope"
                    },
                ),
                controller_id="controller",
            )
