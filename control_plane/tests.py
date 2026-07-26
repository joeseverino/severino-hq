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
from application.controller import (
    ControllerReport,
    report_operation,
    schedule_automatic_operations,
)
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
from application.topology import sync_topology

from core.models import AuditLog

from .models import ManagedResource, OperationRequest, TopologySnapshot
from .providers import (
    describe_providers,
    validate_resolved_certificate,
)
from .topology import TopologyError, import_topology


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

    def test_reimport_is_idempotent_and_preserves_timestamps(self):
        payload = topology_payload()
        payload["managed_resources"] = [
            {
                "key": "hq-dns",
                "kind": "adguard.rewrite",
                "spec": {
                    "domain": "hq.jseverino.com",
                    "answer": "192.168.1.233",
                },
            }
        ]
        first_snapshot = import_topology(payload)
        first_resource = ManagedResource.objects.get(key="hq-dns")

        import_topology(payload)
        snapshot = TopologySnapshot.objects.get(pk="topology")
        resource = ManagedResource.objects.get(key="hq-dns")

        self.assertEqual(resource.generation, 1)
        self.assertEqual(resource.updated_at, first_resource.updated_at)
        self.assertEqual(snapshot.updated_at, first_snapshot.updated_at)

    def test_dependency_change_advances_reference_backed_resource_generation(self):
        payload = topology_payload()
        payload["managed_resources"] = [
            {
                "key": "jseverino-wildcard",
                "kind": "tls.certificate",
                "spec": certificate_spec(),
            }
        ]
        import_topology(payload)

        payload["dependencies"][0]["attributes"]["discover_covered_hosts"] = True
        payload["dependencies"][0]["attributes"]["verify_domains"] = []
        import_topology(payload)

        resource = ManagedResource.objects.get(key="jseverino-wildcard")
        self.assertEqual(resource.generation, 2)

    def test_removal_disables_resource_and_advances_generation(self):
        payload = topology_payload()
        payload["managed_resources"] = [
            {
                "key": "hq-dns",
                "kind": "adguard.rewrite",
                "spec": {
                    "domain": "hq.jseverino.com",
                    "answer": "192.168.1.233",
                },
            }
        ]
        import_topology(payload)
        payload["managed_resources"] = []

        import_topology(payload)
        resource = ManagedResource.objects.get(key="hq-dns")

        self.assertFalse(resource.enabled)
        self.assertEqual(resource.generation, 2)

    def test_manual_ownership_collision_rolls_back_entire_import(self):
        ManagedResource.objects.create(
            key="manual-resource",
            kind="adguard.rewrite",
            spec={
                "domain": "manual.jseverino.com",
                "answer": "192.168.1.10",
            },
        )
        payload = topology_payload()
        payload["managed_resources"] = [
            {
                "key": "created-before-collision",
                "kind": "adguard.rewrite",
                "spec": {
                    "domain": "new.jseverino.com",
                    "answer": "192.168.1.11",
                },
            },
            {
                "key": "manual-resource",
                "kind": "adguard.rewrite",
                "spec": {
                    "domain": "manual.jseverino.com",
                    "answer": "192.168.1.12",
                },
            },
        ]

        with self.assertRaisesRegex(TopologyError, "cannot take ownership"):
            import_topology(payload)

        self.assertFalse(
            ManagedResource.objects.filter(key="created-before-collision").exists()
        )
        self.assertFalse(TopologySnapshot.objects.exists())
        manual = ManagedResource.objects.get(key="manual-resource")
        self.assertEqual(manual.spec["answer"], "192.168.1.10")

    def test_materialization_records_sync_provenance(self):
        payload = topology_payload()
        payload["managed_resources"] = [
            {
                "key": "hq-dns",
                "kind": "adguard.rewrite",
                "spec": {
                    "domain": "hq.jseverino.com",
                    "answer": "192.168.1.233",
                },
            }
        ]

        import_topology(payload)

        event = AuditLog.objects.filter(
            object_type="Managed resource",
            object_repr="hq-dns",
        ).latest("created_at")
        self.assertEqual(event.metadata["interface"], "sync")
        self.assertEqual(event.metadata["actor"], "topology-sync")
        self.assertEqual(
            event.metadata["operation"], "infrastructure.topology.import"
        )

    def test_sync_schedules_each_pending_generation_once(self):
        payload = topology_payload()
        payload["managed_resources"] = [
            {
                "key": "hq-dns",
                "kind": "adguard.rewrite",
                "spec": {
                    "domain": "hq.jseverino.com",
                    "answer": "192.168.1.233",
                },
            }
        ]

        first = sync_topology(payload, principal=cli_principal())
        second = sync_topology(payload, principal=cli_principal())

        self.assertTrue(first["scheduled"][0]["queued"])
        self.assertFalse(second["scheduled"][0]["queued"])
        self.assertEqual(OperationRequest.objects.count(), 1)

        resource = ManagedResource.objects.get(key="hq-dns")
        resource.observed_generation = resource.generation
        resource.save(update_fields=("observed_generation", "updated_at"))
        converged = sync_topology(payload, principal=cli_principal())
        self.assertEqual(converged["scheduled"], [])


class ProviderContractTests(TestCase):
    def test_resolved_certificate_accepts_wildcard_covered_cpanel_vhost(self):
        spec = resolved_certificate_spec()
        cpanel = next(
            item for item in spec["consumers"] if item["kind"] == "cpanel"
        )
        cpanel["install_domains"] = ["quiz.jseverino.net"]

        validate_resolved_certificate(spec)

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
            if request.full_url.endswith("/user/tokens/verify"):
                return Response({"success": True})
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
            "CLOUDFLARE_DNS_CONNECTION_REF": "cloudflare-dns-jseverino",
            "CLOUDFLARE_DNS_URL": "https://api.cloudflare.com/client/v4",
            "CLOUDFLARE_DNS_API_TOKEN": "secret-c",
        }
        probes = preflight_connections(env, open_url=open_url)
        rendered = repr(probes)
        self.assertTrue(all(probe.ok for probe in probes))
        self.assertNotIn("secret-a", rendered)
        self.assertNotIn("secret-b", rendered)
        self.assertNotIn("secret-c", rendered)

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
            "apply",
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

    def test_detail_shows_active_observation_and_policy_gated_renewal(self):
        self.resource.status = {
            "not_after": (timezone.now() + timedelta(days=89, hours=23)).isoformat(),
            "certificate_pem": "-----BEGIN CERTIFICATE-----\npublic\n",
            "consumers": [
                {
                    "consumer": "jseverino-wildcard",
                    "domain": "hq.jseverino.com",
                },
                {
                    "consumer": "jseverino-wildcard",
                    "domain": "sso.jseverino.com",
                },
            ],
        }
        self.resource.save(update_fields=("status",))
        response = self.client.get(
            reverse("control_plane:detail", kwargs={"key": self.resource.key})
        )

        self.assertContains(response, "Automatic")
        self.assertContains(response, "resumes automatically after restarts")
        self.assertContains(response, "Renewal policy")
        self.assertContains(response, "90 days remaining")
        self.assertContains(response, "hq.jseverino.com")
        self.assertContains(response, "sso.jseverino.com")
        self.assertNotContains(response, "BEGIN CERTIFICATE")
        self.assertContains(response, "certificate_available")
        self.assertContains(response, "True")

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

    def test_controller_automatically_queues_due_certificate_renewal(self):
        self.resource.status = {
            "not_after": (timezone.now() + timedelta(days=29)).isoformat()
        }
        self.resource.conditions = [
            {"type": "Ready", "status": True, "reason": "Verified"}
        ]
        self.resource.observed_generation = self.resource.generation
        self.resource.save()

        result = schedule_automatic_operations("homelab-server")

        operation = OperationRequest.objects.get()
        self.assertEqual(result["scheduled"], [str(operation.id)])
        self.assertEqual(operation.action, OperationRequest.Action.RENEW)
        self.assertEqual(operation.requested_interface, "controller")

    def test_controller_automatically_reconciles_new_topology_generation(self):
        self.resource.status = {
            "not_after": (timezone.now() + timedelta(days=89)).isoformat()
        }
        self.resource.conditions = [
            {"type": "Ready", "status": True, "reason": "Verified"}
        ]
        self.resource.observed_generation = self.resource.generation - 1
        self.resource.save()

        schedule_automatic_operations("homelab-server")

        self.assertEqual(
            OperationRequest.objects.get().action,
            OperationRequest.Action.RECONCILE,
        )

    @patch("application.controller._scheduler_now")
    def test_failed_automatic_renewal_retries_once_on_the_next_day(self, now):
        current = timezone.now()
        now.return_value = current
        self.resource.status = {
            "not_after": (current + timedelta(days=29)).isoformat()
        }
        self.resource.observed_generation = self.resource.generation
        self.resource.save()

        schedule_automatic_operations("homelab-server")
        operation = OperationRequest.objects.get()
        operation.state = OperationRequest.State.FAILED
        operation.save(update_fields=("state", "updated_at"))
        schedule_automatic_operations("homelab-server")
        self.assertEqual(OperationRequest.objects.count(), 1)

        now.return_value = current + timedelta(days=1)
        schedule_automatic_operations("homelab-server")
        self.assertEqual(OperationRequest.objects.count(), 2)

    def test_active_controller_capability_queues_renewal_work(self):
        result = request_certificate_renewal(
            OperationCommand(idempotency_key="renew-active"),
            principal=cli_principal(),
            current_key=self.resource.key,
        )

        self.assertTrue(result["queued"])
        self.assertEqual(result["operation"]["action"], "renew")

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

    def test_legacy_operation_evidence_is_not_mislabeled_as_a_mismatch(self):
        OperationRequest.objects.create(
            resource=self.resource,
            action=OperationRequest.Action.RECONCILE,
            state=OperationRequest.State.SUCCEEDED,
            requested_actor="joe",
            requested_interface="web",
            idempotency_key="legacy-success",
            input={"generation": self.resource.generation},
            result={
                "message": "Certificate consumers already match the managed lineage.",
                "status": {
                    "consumers": [
                        {
                            "consumer": "npm",
                            "domain": "hq.example.test",
                            "fingerprint_sha256": "legacy",
                        }
                    ]
                },
            },
        )

        detail = self.client.get(
            reverse("control_plane:detail", args=[self.resource.key])
        )

        self.assertContains(detail, "observed (legacy result)")
        self.assertNotContains(detail, "did not match")

    def test_failed_operation_renders_guidance_and_expected_observed_evidence(self):
        OperationRequest.objects.create(
            resource=self.resource,
            action=OperationRequest.Action.RECONCILE,
            state=OperationRequest.State.FAILED,
            requested_actor="homelab-controller",
            requested_interface="controller",
            idempotency_key="structured-failure",
            result={
                "message": "Verification did not converge.",
                "conditions": [
                    {
                        "type": "Degraded",
                        "status": True,
                        "reason": "VerificationFailed",
                        "message": "One consumer serves the previous certificate.",
                    }
                ],
                "status": {
                    "expected_fingerprint_sha256": "expected-fingerprint",
                    "consumers": [
                        {
                            "consumer": "npm",
                            "domain": "hq.example.test",
                            "fingerprint_sha256": "observed-fingerprint",
                            "matches_expected": False,
                        }
                    ],
                },
            },
        )

        detail = self.client.get(
            reverse("control_plane:detail", args=[self.resource.key])
        )

        self.assertContains(detail, "One consumer serves the previous certificate.")
        self.assertContains(detail, "Provider reason:")
        self.assertContains(detail, "VerificationFailed")
        self.assertContains(detail, "1 affected target")
        self.assertContains(detail, "Expected")
        self.assertContains(detail, "observed")
        self.assertContains(detail, "Raw controller result")

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

    def test_failed_controller_report_preserves_last_verified_status(self):
        verified = {
            "not_after": "2026-10-23T00:00:00+00:00",
            "certificate_pem": "-----BEGIN CERTIFICATE-----\npublic\n",
        }
        self.resource.status = verified
        self.resource.save(update_fields=("status",))
        operation = OperationRequest.objects.create(
            resource=self.resource,
            action=OperationRequest.Action.RENEW,
            state=OperationRequest.State.CLAIMED,
            requested_actor="operator",
            requested_interface="cli",
            idempotency_key="preserve-verified-status",
            input={"generation": self.resource.generation},
            claimed_by="controller",
            claimed_at=timezone.now(),
        )

        report_operation(
            str(operation.id),
            ControllerReport(
                success=False,
                observed_generation=self.resource.generation,
                status={"expected_fingerprint_sha256": "new"},
                conditions=[
                    {
                        "type": "Degraded",
                        "status": True,
                        "reason": "ProviderError",
                        "message": "One consumer did not converge.",
                    }
                ],
                message="One consumer did not converge.",
            ),
            controller_id="controller",
        )

        self.resource.refresh_from_db()
        operation.refresh_from_db()
        self.assertEqual(self.resource.status, verified)
        self.assertEqual(
            operation.result["status"]["expected_fingerprint_sha256"], "new"
        )
