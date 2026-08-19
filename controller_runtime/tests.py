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
    def test_adguard_reports_a_rewrite_that_is_switched_off(self, request):
        """Present but disabled does not resolve, and Ready would be a lie.

        HQ does not set this field -- add and update carry only domain and
        answer -- so the honest position is to observe it and say so, rather
        than report a name as healthy while it answers nothing.
        """
        request.return_value = [
            {"domain": "hq.example", "answer": "192.0.2.10", "enabled": False}
        ]

        result = providers.reconcile_adguard(
            {"domain": "hq.example", "answer": "192.0.2.10"}
        )

        self.assertEqual(result.conditions[0]["type"], "Degraded")
        self.assertIs(result.status["enabled"], False)

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
    def test_adguard_delete_removes_the_live_pair(self, request):
        """AdGuard identifies a rewrite by domain *and* answer.

        Deleting with the desired answer would miss a record whose answer has
        drifted, silently leaving it in place.
        """
        request.side_effect = [
            [{"domain": "hq.example", "answer": "192.0.2.99", "enabled": True}],
            None,
        ]

        result = providers.delete_adguard({"domain": "hq.example", "answer": "192.0.2.10"})

        self.assertTrue(result.changed)
        deletion = request.call_args_list[-1]
        self.assertTrue(deletion.args[0].endswith("/control/rewrite/delete"))
        self.assertEqual(
            deletion.kwargs["payload"],
            {"domain": "hq.example", "answer": "192.0.2.99"},
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
    def test_adguard_delete_is_idempotent(self, request):
        """A retried delete finding nothing there has done what was asked.

        The queue retries, so a delete that applied and then failed to report
        runs again. Treating an absent record as failure would leave the
        operation stuck forever on work that is already complete.
        """
        request.return_value = []

        result = providers.delete_adguard({"domain": "gone.example", "answer": "x"})

        self.assertFalse(result.changed)
        self.assertEqual(result.conditions[0]["type"], "Ready")
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
    def test_adguard_delete_plans_without_touching_anything(self, request):
        request.return_value = [
            {"domain": "hq.example", "answer": "192.0.2.10", "enabled": True}
        ]

        result = providers.delete_adguard(
            {"domain": "hq.example", "answer": "192.0.2.10"}, apply=False
        )

        self.assertTrue(result.changed)
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
            "HQ_ACME_DIR": "/tmp",
            "HQ_CONTROLLER_SSH_DIR": "/tmp",
        },
        clear=True,
    )
    @mock.patch("controller_runtime.providers._run")
    @mock.patch("controller_runtime.providers._ssh")
    @mock.patch("controller_runtime.providers._request")
    def test_preflight_proves_cloudflare_token_and_zone_scope(
        self, request, ssh, _run
    ):
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

        self.assertTrue(
            any(
                item["connection_ref"] == "cloudflare-dns-jseverino"
                for item in result
            )
        )
        self.assertEqual(ssh.call_count, 2)
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
                    "hsts_enabled": False,
                    "hsts_subdomains": False,
                    "trust_forwarded_proto": False,
                    "serving": True,
                }
            )

    @mock.patch.dict(
        "os.environ",
        {
            "NPM_URL": "https://npm.example.test",
            "NPM_USERNAME": "controller@example.com",
            "NPM_PASSWORD": "secret",
        },
        clear=True,
    )
    @mock.patch("controller_runtime.providers._request")
    def test_npm_delete_targets_the_host_with_that_exact_domain_set(self, request):
        request.side_effect = [
            {"token": "short-lived"},
            [
                {"id": 7, "domain_names": ["other.example"]},
                {"id": 9, "domain_names": ["hq.example"]},
            ],
            None,
        ]

        result = providers.delete_npm({"domain_names": ["hq.example"]})

        self.assertTrue(result.changed)
        deletion = request.call_args_list[-1]
        self.assertTrue(deletion.args[0].endswith("/nginx/proxy-hosts/9"))
        self.assertEqual(deletion.kwargs["method"], "DELETE")

    @mock.patch.dict(
        "os.environ",
        {
            "NPM_URL": "https://npm.example.test",
            "NPM_USERNAME": "controller@example.com",
            "NPM_PASSWORD": "secret",
        },
        clear=True,
    )
    @mock.patch("controller_runtime.providers._request")
    def test_npm_reconcile_no_longer_asserts_hsts_off(self, request):
        """The payload replaces the whole object, so an unsent field is not spared.

        HSTS was pinned False here, which meant enabling it in NPM survived
        until the next pass and then quietly switched itself back off.
        """
        request.side_effect = [{"token": "short-lived"}, [], None]

        providers.reconcile_npm(
            {
                "domain_names": ["hq.example"],
                "forward_scheme": "http",
                "forward_host": "192.0.2.10",
                "forward_port": 8000,
                "force_ssl": False,
                "http2": True,
                "websocket": False,
                "caching_enabled": False,
                "block_exploits": True,
                "access_list_id": 0,
                "advanced_config": "",
                "hsts_enabled": True,
                "hsts_subdomains": True,
                "trust_forwarded_proto": True,
                "serving": True,
            }
        )

        sent = request.call_args_list[-1].kwargs["payload"]
        self.assertTrue(sent["hsts_enabled"])
        self.assertTrue(sent["hsts_subdomains"])
        self.assertTrue(sent["trust_forwarded_proto"])

    def test_public_dns_fails_closed(self):
        with self.assertRaisesRegex(providers.ProviderError, "not enabled"):
            providers.execute(
                {"kind": "cloudflare.dns_record", "spec": {}}, "reconcile"
            )

    @mock.patch("controller_runtime.providers._certificate_registry")
    @mock.patch("controller_runtime.providers._observe_tls_domain")
    @mock.patch.dict(
        "os.environ", {"NPM_URL": "https://npm-origin.example"}, clear=True
    )
    def test_tls_observer_reports_consumer_drift_and_public_artifact(
        self, observe, registry
    ):
        registry.return_value = {
            "ssh_transports": {"edge": {"host": "192.0.2.20"}}
        }
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
                        "connection_ref": "edge",
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
        self.assertEqual(
            observe.call_args_list,
            [
                mock.call("hq.example", connect_host="npm-origin.example"),
                mock.call("health.example", connect_host="192.0.2.20"),
            ],
        )

    @mock.patch("controller_runtime.providers._certificate_registry")
    @mock.patch("controller_runtime.providers._observe_tls_domain")
    def test_cpanel_tls_observation_bypasses_public_proxy(self, observe, registry):
        registry.return_value = {
            "ssh_transports": {"cpanel": {"host": "192.0.2.10"}}
        }
        observe.return_value = {
            "domain": "quiz.example.test",
            "not_after": "2026-10-23T00:00:00+00:00",
            "fingerprint_sha256": "current",
            "issuer": "Example CA",
            "sans": ["*.example.test"],
            "certificate_pem": "-----BEGIN CERTIFICATE-----\ncurrent\n",
        }

        providers.reconcile_tls(
            {
                "renewal_window_days": 30,
                "consumers": [
                    {
                        "kind": "cpanel",
                        "name": "cpanel",
                        "connection_ref": "cpanel",
                        "verify_domains": ["quiz.example.test"],
                    }
                ],
            }
        )

        observe.assert_called_once_with(
            "quiz.example.test", connect_host="192.0.2.10"
        )

    @mock.patch.dict("os.environ", {"NPM_URL": "https://proxy.homelab"}, clear=True)
    def test_npm_tls_endpoint_is_derived_from_controller_connection(self):
        self.assertEqual(
            providers._consumer_tls_endpoint({"kind": "npm"}, {}),
            "proxy.homelab",
        )

    @mock.patch("controller_runtime.providers.renew_tls")
    def test_renew_plan_never_mutates(self, renew):
        result = providers.execute(
            {"kind": "tls.certificate", "spec": {}}, "renew", apply=False
        )

        self.assertTrue(result.changed)
        renew.assert_not_called()

    @mock.patch("controller_runtime.providers._request")
    @mock.patch("controller_runtime.providers._multipart_request")
    @mock.patch("controller_runtime.providers._npm_token", return_value="token")
    @mock.patch.dict(
        "os.environ",
        {"NPM_URL": "https://npm.example.test"},
        clear=True,
    )
    def test_npm_certificate_is_resolved_and_reloads_already_bound_hosts(
        self, _token, multipart, request
    ):
        leaf = b"-----BEGIN CERTIFICATE-----\nleaf\n-----END CERTIFICATE-----\n"
        chain = b"-----BEGIN CERTIFICATE-----\nchain\n-----END CERTIFICATE-----\n"
        request.side_effect = [
            [],
            {"id": 22, "provider": "other"},
            [{"id": 7, "domain_names": ["dev.example.test"], "certificate_id": 22}],
            {},
        ]

        certificate_id, identity = providers._npm_managed_certificate(
            {
                "name": "example-wildcard",
                "verify_domains": ["dev.example.test"],
            },
            ["example.test", "*.example.test"],
            leaf + chain,
            b"private-key",
        )

        self.assertEqual(multipart.call_count, 2)
        files = multipart.call_args.kwargs["files"]
        self.assertEqual(files["certificate"][1], leaf)
        self.assertEqual(files["certificate_key"][1], b"private-key")
        self.assertEqual(files["intermediate_certificate"][1], chain)
        self.assertTrue(multipart.call_args.args[0].endswith("/22/upload"))
        self.assertEqual(certificate_id, 22)
        self.assertEqual(identity["nice_name"], "Severino HQ - example-wildcard")
        self.assertEqual(request.call_args.kwargs["payload"], {"certificate_id": 22})

    @mock.patch("controller_runtime.providers._request")
    @mock.patch("controller_runtime.providers._multipart_request")
    @mock.patch("controller_runtime.providers._npm_token", return_value="token")
    @mock.patch.dict(
        "os.environ",
        {"NPM_URL": "https://npm.example.test"},
        clear=True,
    )
    def test_npm_certificate_rebinds_every_covered_host_only(
        self, _token, multipart, request
    ):
        certificate = b"-----BEGIN CERTIFICATE-----\nleaf\n-----END CERTIFICATE-----\n"
        request.side_effect = [
            [],
            {"id": 22, "provider": "other"},
            [
                {"id": 1, "domain_names": ["hq.example.test"], "certificate_id": 11},
                {"id": 2, "domain_names": ["sso.example.test"], "certificate_id": 11},
                {"id": 3, "domain_names": ["proxy.homelab"], "certificate_id": 7},
                {"id": 4, "domain_names": ["off.example.test"], "enabled": False},
            ],
            {},
            {},
        ]

        providers._npm_managed_certificate(
            {
                "name": "example-wildcard",
                "verify_domains": [],
                "discover_covered_hosts": True,
            },
            ["example.test", "*.example.test"],
            certificate,
            b"private-key",
        )

        self.assertEqual(multipart.call_count, 2)
        rebound_urls = [call.args[0] for call in request.call_args_list[-2:]]
        self.assertEqual(
            rebound_urls,
            [
                "https://npm.example.test/api/nginx/proxy-hosts/1",
                "https://npm.example.test/api/nginx/proxy-hosts/2",
            ],
        )
        for call in request.call_args_list[-2:]:
            self.assertEqual(call.kwargs["payload"]["certificate_id"], 22)

    @mock.patch("controller_runtime.providers.reconcile_tls")
    @mock.patch("controller_runtime.providers._deploy_certificate")
    @mock.patch("controller_runtime.providers._issue_certificate")
    @mock.patch("controller_runtime.providers._resumable_lineage", return_value=None)
    @mock.patch("controller_runtime.providers._validate_certificate")
    @mock.patch("controller_runtime.providers._ssh")
    def test_renewal_deploys_and_verifies_every_consumer(
        self, ssh, validate, _resume, issue, deploy, reconcile
    ):
        previous = providers._certificate_bundle(b"old-cert", b"old-key")
        ssh.return_value = previous
        validate.side_effect = ["old", "new"]
        issue.return_value = (b"new-cert", b"new-key")
        deploy.return_value = {}
        reconcile.return_value = providers.ProviderResult(
            changed=False,
            status={
                "consumers": [
                    {"fingerprint_sha256": "new", "consumer_kind": "npm"},
                    {"fingerprint_sha256": "new", "consumer_kind": "caddy"},
                ]
            },
            conditions=[],
            message="observed",
        )
        spec = {
            "domains": ["example.test"],
            "consumers": [
                {"kind": "caddy", "connection_ref": "edge"},
            ],
        }

        result = providers.renew_tls(spec)

        self.assertTrue(result.changed)
        deploy.assert_called_once_with(spec, b"new-cert", b"new-key")
        self.assertEqual(result.conditions[0]["reason"], "Renewed")
        self.assertEqual(result.status["expected_fingerprint_sha256"], "new")
        self.assertTrue(
            all(item["matches_expected"] for item in result.status["consumers"])
        )

    @mock.patch("controller_runtime.providers.reconcile_tls")
    @mock.patch("controller_runtime.providers._validate_certificate", return_value="new")
    @mock.patch("controller_runtime.providers._lineage")
    def test_reconcile_success_records_explicit_consumer_match_evidence(
        self, lineage, _validate, reconcile
    ):
        lineage.return_value = (b"new-cert", b"new-key")
        reconcile.return_value = providers.ProviderResult(
            changed=False,
            status={
                "consumers": [
                    {
                        "consumer": "npm",
                        "domain": "hq.example.test",
                        "fingerprint_sha256": "new",
                    }
                ]
            },
            conditions=[],
            message="observed",
        )

        result = providers.apply_tls_reconcile(
            {"domains": ["example.test"], "consumers": []}
        )

        self.assertFalse(result.changed)
        self.assertEqual(result.status["expected_fingerprint_sha256"], "new")
        self.assertTrue(result.status["consumers"][0]["matches_expected"])

    @mock.patch("controller_runtime.providers.reconcile_tls")
    @mock.patch("controller_runtime.providers._deploy_certificate", return_value={})
    @mock.patch("controller_runtime.providers._issue_certificate")
    @mock.patch(
        "controller_runtime.providers._resumable_lineage",
        return_value=(b"pending-cert", b"pending-key"),
    )
    @mock.patch("controller_runtime.providers._validate_certificate")
    @mock.patch("controller_runtime.providers._ssh")
    def test_renewal_resumes_existing_lineage_without_acme_request(
        self, ssh, validate, _resume, issue, deploy, reconcile
    ):
        ssh.return_value = providers._certificate_bundle(b"old-cert", b"old-key")
        validate.side_effect = ["old", "pending"]
        reconcile.return_value = providers.ProviderResult(
            changed=False,
            status={"consumers": [{"fingerprint_sha256": "pending"}]},
            conditions=[],
            message="observed",
        )
        spec = {
            "certificate_name": "example",
            "domains": ["example.test"],
            "renewal_window_days": 30,
            "consumers": [{"kind": "caddy", "connection_ref": "edge"}],
        }

        result = providers.renew_tls(spec)

        issue.assert_not_called()
        deploy.assert_called_once_with(spec, b"pending-cert", b"pending-key")
        self.assertEqual(result.status["artifact_source"], "existing_lineage")

    @mock.patch("controller_runtime.providers.reconcile_tls")
    @mock.patch("controller_runtime.providers._deploy_certificate")
    @mock.patch("controller_runtime.providers._issue_certificate")
    @mock.patch("controller_runtime.providers._resumable_lineage", return_value=None)
    @mock.patch("controller_runtime.providers._validate_certificate")
    @mock.patch("controller_runtime.providers._ssh")
    def test_renewal_rolls_back_previous_artifact_on_deploy_failure(
        self, ssh, validate, _resume, issue, deploy, _reconcile
    ):
        ssh.return_value = providers._certificate_bundle(b"old-cert", b"old-key")
        validate.side_effect = ["old", "new"]
        issue.return_value = (b"new-cert", b"new-key")
        deploy.side_effect = [providers.ProviderError("failed"), None]
        spec = {
            "domains": ["example.test"],
            "consumers": [
                {"kind": "caddy", "connection_ref": "edge"},
            ],
        }

        with self.assertRaisesRegex(
            providers.ProviderError, "failed.*[Rr]ollback succeeded"
        ):
            providers.renew_tls(spec)

        self.assertEqual(deploy.call_count, 2)
        deploy.assert_called_with(spec, b"old-cert", b"old-key")


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

    @mock.patch("controller_runtime.worker.preflight")
    @mock.patch("controller_runtime.worker._manage")
    def test_idle_plan_peeks_without_provider_preflight(self, manage, preflight):
        manage.return_value = {"ok": True, "operation": None}

        self.assertEqual(worker.run_once("test", apply=False), 0)

        self.assertEqual(manage.call_args.args[0], "peek")
        self.assertNotIn("claim", manage.call_args.args)
        preflight.assert_not_called()

    @mock.patch("controller_runtime.worker.preflight")
    @mock.patch("controller_runtime.worker._manage")
    def test_idle_apply_claims_without_provider_preflight(self, manage, preflight):
        manage.return_value = {"ok": True, "operation": None}

        self.assertEqual(worker.run_once("test", apply=True), 0)

        arguments = manage.call_args.args
        self.assertEqual(arguments[:3], ("claim", "--controller-id", "test"))
        self.assertEqual(
            manage.call_args_list[0].args[0], "inventory"
        )
        self.assertEqual(
            manage.call_args_list[1].args,
            ("schedule", "--controller-id", "test"),
        )
        self.assertIn("adguard.rewrite:reconcile", arguments)
        self.assertIn("npm.proxy_host:reconcile", arguments)
        self.assertIn("tls.certificate:reconcile", arguments)
        self.assertIn("tls.certificate:renew", arguments)
        preflight.assert_not_called()

    def test_capability_registry_drives_supported_kinds(self):
        self.assertEqual(
            worker.supported_capabilities(),
            (
                ("adguard.rewrite", "delete"),
                ("adguard.rewrite", "reconcile"),
                ("npm.proxy_host", "delete"),
                ("npm.proxy_host", "reconcile"),
                ("tls.certificate", "reconcile"),
                ("tls.certificate", "renew"),
            ),
        )

    def test_removal_is_never_scheduled_automatically(self):
        """The scheduler converges declarations. It must not decide to delete.

        Reconciliation moves the world toward what HQ says and is safe to run
        unattended. Removal takes down something that is currently serving, and
        an automatic one would mean a bad declaration could delete a live record
        with nobody having asked for it.
        """
        from control_plane.providers import enabled_controller_actions

        automatic = enabled_controller_actions(automatic_only=True)

        self.assertEqual([a for _, a in automatic if a == "delete"], [])

    def test_every_declared_controller_action_has_exactly_one_dispatch(self):
        from control_plane.providers import controller_capability_registry

        declared = {
            (kind, action)
            for kind, capability in controller_capability_registry().capabilities.items()
            for action in capability.actions
        }

        self.assertEqual(set(providers.PROVIDER_ACTIONS), declared)

    @mock.patch("controller_runtime.worker.preflight", return_value=[])
    @mock.patch("controller_runtime.worker.execute")
    @mock.patch("controller_runtime.worker._manage")
    def test_provider_failure_is_reported_without_secret(self, manage, execute, _):
        manage.side_effect = [
            # The inventory sweep runs first on every apply pass.
            {"ok": True, "recorded": []},
            {"ok": True, "scheduled": []},
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

        report_payload = json.loads(manage.call_args_list[3].args[-1])
        self.assertFalse(report_payload["success"])
        self.assertNotIn("password", json.dumps(report_payload).lower())

    @mock.patch("controller_runtime.worker.preflight")
    @mock.patch("controller_runtime.worker.execute")
    @mock.patch("controller_runtime.worker._manage")
    def test_claimed_operation_preflights_before_provider_execution(
        self, manage, execute, preflight
    ):
        manage.side_effect = [
            # The inventory sweep runs first on every apply pass.
            {"ok": True, "recorded": []},
            {"ok": True, "scheduled": []},
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
        execute.return_value = providers.ProviderResult(
            changed=False,
            status={},
            conditions=[],
            message="Current.",
        )

        self.assertEqual(worker.run_once("test", apply=True), 0)

        preflight.assert_called_once_with()
        execute.assert_called_once()
