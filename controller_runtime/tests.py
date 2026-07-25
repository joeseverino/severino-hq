from __future__ import annotations

import json
from pathlib import Path
from unittest import TestCase, mock

from . import providers, worker


class ControllerConnectionRegistryTests(TestCase):
    def setUp(self):
        registry_path = (
            Path(__file__).resolve().parent.parent
            / "config"
            / "controller-connections.json"
        )
        self.registry = json.loads(registry_path.read_text())

    def test_api_token_uses_api_credential_website_field(self):
        self.assertEqual(
            self.registry["projections"]["api_token"]["URL"],
            {"source": "field", "label": "website"},
        )

    def test_ssh_transports_are_pinned_and_have_distinct_identities(self):
        transports = self.registry["ssh_transports"]

        self.assertEqual(set(transports), {"edge", "namecheap-cpanel"})
        for connection_ref, transport in transports.items():
            with self.subTest(connection_ref=connection_ref):
                self.assertGreater(transport["port"], 0)
                self.assertTrue(transport["user"])
                self.assertRegex(transport["host_key"], r"^ssh-ed25519 ")


class ProviderAdapterTests(TestCase):
    @mock.patch.dict(
        "os.environ", {"HQ_CONTROLLER_CA_FILE": "/run/secrets/homelab-ca.pem"}, clear=True
    )
    @mock.patch("controller_runtime.providers.ssl.create_default_context")
    def test_tls_context_adds_controller_ca_without_replacing_public_roots(
        self, create_default_context
    ):
        context = create_default_context.return_value

        self.assertIs(providers._tls_context(), context)

        context.load_verify_locations.assert_called_once_with(
            cafile="/run/secrets/homelab-ca.pem"
        )

    def test_npm_ui_url_derives_api_base_once(self):
        self.assertEqual(
            providers._npm_api_url("https://npm.example"),
            "https://npm.example/api",
        )
        self.assertEqual(
            providers._npm_api_url("https://npm.example/api"),
            "https://npm.example/api",
        )

    @mock.patch.dict(
        "os.environ",
        {
            "ADGUARD_URL": "https://adguard.example",
            "ADGUARD_USERNAME": "controller",
            "ADGUARD_PASSWORD": "secret",
        },
        clear=True,
    )
    @mock.patch("controller_runtime.providers._request")
    def test_adguard_noop_is_idempotent(self, request):
        request.return_value = [{"domain": "hq.example", "answer": "192.0.2.10"}]

        result = providers.reconcile_adguard(
            {"domain": "hq.example", "answer": "192.0.2.10"}
        )

        self.assertFalse(result.changed)
        self.assertEqual(request.call_count, 1)

    @mock.patch.dict(
        "os.environ",
        {
            "ADGUARD_URL": "https://adguard.example",
            "ADGUARD_USERNAME": "controller",
            "ADGUARD_PASSWORD": "secret",
        },
        clear=True,
    )
    @mock.patch("controller_runtime.providers._request")
    def test_adguard_update_uses_the_provider_contract(self, request):
        request.side_effect = [
            [{"domain": "hq.example", "answer": "192.0.2.9", "enabled": True}],
            None,
        ]

        result = providers.reconcile_adguard(
            {"domain": "hq.example", "answer": "192.0.2.10"}
        )

        self.assertTrue(result.changed)
        self.assertEqual(
            request.call_args_list[1].kwargs["payload"],
            {
                "target": {"domain": "hq.example", "answer": "192.0.2.9"},
                "update": {"domain": "hq.example", "answer": "192.0.2.10"},
            },
        )

    @mock.patch.dict(
        "os.environ",
        {
            "ADGUARD_URL": "https://adguard.example",
            "ADGUARD_USERNAME": "controller",
            "ADGUARD_PASSWORD": "secret-a",
            "ADGUARD_CONNECTION_REF": "homelab-adguard",
            "NPM_URL": "https://npm.example",
            "NPM_USERNAME": "controller@example.com",
            "NPM_PASSWORD": "secret-b",
            "NPM_CONNECTION_REF": "homelab-npm",
            "CLOUDFLARE_DNS_URL": "https://api.cloudflare.com/client/v4",
            "CLOUDFLARE_DNS_API_TOKEN": "secret-c",
            "CLOUDFLARE_DNS_CONNECTION_REF": "cloudflare-dns-jseverino",
        },
        clear=True,
    )
    @mock.patch("controller_runtime.providers._request")
    def test_preflight_proves_cloudflare_token_and_zone_scope(self, request):
        request.side_effect = [
            {"dns_addresses": ["0.0.0.0"]},
            {"token": "short-lived"},
            {"success": True},
            {
                "result": [
                    {"name": "jseverino.com"},
                    {"name": "jseverino.net"},
                    {"name": "jseverino.org"},
                    {"name": "joeseverino.com"},
                ]
            },
        ]

        result = providers.preflight()

        self.assertEqual(result[-1]["connection_ref"], "cloudflare-dns-jseverino")
        self.assertNotIn("secret-c", json.dumps(result))

    @mock.patch.dict(
        "os.environ",
        {
            "NPM_URL": "https://npm.example/api",
            "NPM_USERNAME": "controller@example.com",
            "NPM_PASSWORD": "secret",
        },
        clear=True,
    )
    @mock.patch("controller_runtime.providers._request")
    def test_npm_refuses_https_create_without_certificate(self, request):
        request.side_effect = [{"token": "short-lived"}, []]

        with self.assertRaisesRegex(
            providers.ProviderError, "resolved certificate ID"
        ):
            providers.reconcile_npm(
                {
                    "domain_names": ["hq.example"],
                    "forward_scheme": "http",
                    "forward_host": "192.0.2.10",
                    "forward_port": 8000,
                    "force_ssl": True,
                    "http2": True,
                    "websocket": False,
                    "caching_enabled": False,
                    "block_exploits": True,
                    "access_list_id": 0,
                    "advanced_config": "",
                }
            )

    def test_public_dns_fails_closed(self):
        with self.assertRaisesRegex(providers.ProviderError, "not enabled"):
            providers.execute(
                {"kind": "cloudflare.dns_record", "spec": {}}, "reconcile"
            )

    @mock.patch("controller_runtime.providers._observe_tls_domain")
    def test_tls_observer_reports_consumer_drift_and_public_artifact(self, observe):
        observe.side_effect = [
            {
                "domain": "hq.example",
                "not_after": "2026-07-28T00:00:00+00:00",
                "fingerprint_sha256": "old",
                "issuer": "Example CA",
                "sans": ["*.example"],
                "certificate_pem": "-----BEGIN CERTIFICATE-----\nold\n",
            },
            {
                "domain": "health.example",
                "not_after": "2026-10-07T00:00:00+00:00",
                "fingerprint_sha256": "new",
                "issuer": "Example CA",
                "sans": ["*.example"],
                "certificate_pem": "-----BEGIN CERTIFICATE-----\nnew\n",
            },
        ]

        result = providers.reconcile_tls(
            {
                "renewal_window_days": 30,
                "consumers": [
                    {
                        "kind": "npm",
                        "name": "npm",
                        "verify_domains": ["hq.example"],
                    },
                    {
                        "kind": "caddy",
                        "name": "caddy",
                        "verify_domains": ["health.example"],
                    },
                ],
            }
        )

        self.assertTrue(
            any(item["type"] == "Drifted" for item in result.conditions)
        )
        self.assertIn("BEGIN CERTIFICATE", result.status["certificate_pem"])
        self.assertNotIn("PRIVATE KEY", json.dumps(result.status))


class WorkerTests(TestCase):
    @mock.patch.dict("os.environ", {"HQ_IN_PROCESS": "1"}, clear=True)
    @mock.patch("controller_runtime.worker.subprocess.run")
    def test_in_process_bridge_uses_running_image_python(self, run):
        run.return_value = mock.Mock(
            returncode=0,
            stdout='{"ok":true,"operation":null}',
        )

        worker._manage("peek")

        command = run.call_args.args[0]
        self.assertEqual(command[0], worker.sys.executable)
        self.assertTrue(command[1].endswith("/manage.py"))
        self.assertNotIn("docker", command)

    @mock.patch("controller_runtime.worker.preflight", return_value=[])
    @mock.patch("controller_runtime.worker._manage")
    def test_plan_peeks_but_never_claims(self, manage, _preflight):
        manage.return_value = {"ok": True, "operation": None}

        self.assertEqual(worker.run_once("test", apply=False), 0)

        self.assertEqual(manage.call_args.args[0], "peek")
        self.assertNotIn("claim", manage.call_args.args)

    @mock.patch("controller_runtime.worker.preflight", return_value=[])
    @mock.patch("controller_runtime.worker._manage")
    def test_apply_claims_only_supported_kinds(self, manage, _preflight):
        manage.return_value = {"ok": True, "operation": None}

        self.assertEqual(worker.run_once("test", apply=True), 0)

        arguments = manage.call_args.args
        self.assertEqual(arguments[:3], ("claim", "--controller-id", "test"))
        self.assertIn("adguard.rewrite:reconcile", arguments)
        self.assertIn("npm.proxy_host:reconcile", arguments)
        self.assertIn("tls.certificate:reconcile", arguments)
        self.assertNotIn("tls.certificate:renew", arguments)

    def test_capability_registry_drives_supported_kinds(self):
        self.assertEqual(
            worker.supported_capabilities(),
            (
                ("adguard.rewrite", "reconcile"),
                ("npm.proxy_host", "reconcile"),
                ("tls.certificate", "reconcile"),
            ),
        )

    @mock.patch("controller_runtime.worker.preflight", return_value=[])
    @mock.patch("controller_runtime.worker.execute")
    @mock.patch("controller_runtime.worker._manage")
    def test_provider_failure_is_reported_without_secret(self, manage, execute, _):
        manage.side_effect = [
            {
                "operation": {"id": "operation-1", "action": "reconcile"},
                "resource": {
                    "key": "dns",
                    "kind": "adguard.rewrite",
                    "generation": 2,
                    "spec": {},
                },
            },
            {"ok": True},
        ]
        execute.side_effect = providers.ProviderError("Provider request failed.")

        self.assertEqual(worker.run_once("test", apply=True), 1)

        report_payload = json.loads(manage.call_args_list[1].args[-1])
        self.assertFalse(report_payload["success"])
        self.assertNotIn("password", json.dumps(report_payload).lower())
