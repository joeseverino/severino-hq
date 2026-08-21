from __future__ import annotations

import json
import os
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

    def test_the_registry_describes_shapes_and_names_no_connection(self):
        """The registry says how a connection is wired, never which exist.

        Which connections a deployment has is its own configuration, and it
        lives in the vault the controller resolves credentials from. Committed
        here it would be a second copy, in a public repository, that drifts.
        """

        self.assertEqual(set(self.registry), {"schema_version", "projections"})

        serialised = json.dumps(self.registry)
        # A projection may name a *field* called host_key; what must never
        # appear is key material, an address, or a host.
        self.assertNotIn("ssh-ed25519", serialised)
        self.assertNotRegex(serialised, r"\b\d{1,3}(\.\d{1,3}){3}\b")


def _by_url(routes):
    """Answer a mocked provider request by what it asked for, not by call order.

    Order-indexed fakes encode the sweep's iteration order into every test that
    uses one, so adding a connection rewrites tests that have nothing to do
    with it.
    """

    def respond(url, *args, **kwargs):
        # Longest match wins. `/tokens` is a substring of `/user/tokens/verify`,
        # so first-match would answer Cloudflare with NPM's reply.
        matches = sorted(
            (fragment for fragment in routes if fragment in url), key=len
        )
        if not matches:
            raise AssertionError(f"Unexpected provider request: {url}")
        return routes[matches[-1]]

    return respond


def _bridge(**responses):
    """Answer a mocked bridge call by which action it is, not by call order.

    Order-indexed fakes encode the pass's exact sequence into every test that
    uses one, so adding a step rewrites tests that have nothing to do with it.
    """

    def respond(*args, **kwargs):
        action = args[0] if args else ""
        if action in responses:
            return responses[action]
        raise AssertionError(f"Unexpected bridge call: {action}")

    return respond


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
            # Two SSH connections, recognised by the values their projection
            # produces rather than by anything naming them here.
            "EXAMPLE_EDGE_CONNECTION_REF": "example-edge",
            "EXAMPLE_EDGE_HOST": "edge.example",
            "EXAMPLE_EDGE_USER": "controller",
            "EXAMPLE_EDGE_PORT": "22",
            "EXAMPLE_EDGE_HOST_KEY": "example-host-key",
            "EXAMPLE_SHARED_CONNECTION_REF": "example-shared",
            "EXAMPLE_SHARED_HOST": "shared.example",
            "EXAMPLE_SHARED_USER": "controller",
            "EXAMPLE_SHARED_PORT": "22",
            "EXAMPLE_SHARED_HOST_KEY": "example-host-key",
        },
        clear=True,
    )
    @mock.patch("controller_runtime.providers._run")
    @mock.patch("controller_runtime.providers._ssh")
    @mock.patch("controller_runtime.providers._request")
    def test_preflight_probes_every_connection_the_environment_carries(
        self, request, ssh, _run
    ):
        """Five 1Password items, five probes, and nothing naming any of them.

        The environment is the whole inventory: which connections exist, what
        kind each is, and -- for the two SSH transports -- that they are
        transports at all, learned from the values their projection produced.
        """

        request.side_effect = _by_url(
            {
                "/control/status": {"dns_addresses": ["0.0.0.0"], "version": "0.107"},
                "/tokens": {"token": "short-lived"},
                "/user/tokens/verify": {"success": True},
                "/zones": {
                    "result": [
                        {"name": "jseverino.com"},
                        {"name": "jseverino.net"},
                    ]
                },
            }
        )

        result = providers.preflight()

        by_ref = {item["connection_ref"]: item for item in result}
        self.assertEqual(
            sorted(by_ref),
            [
                "cloudflare-dns-jseverino",
                "example-edge",
                "example-shared",
                "homelab-adguard",
                "homelab-npm",
            ],
        )
        self.assertTrue(all(item["ok"] for item in result))
        # Classified by env prefix without a `provider` field anywhere, which
        # is what keeps an existing vault working unchanged.
        self.assertEqual(by_ref["homelab-adguard"]["provider"], "adguard")
        self.assertEqual(by_ref["example-edge"]["provider"], "ssh")
        # What a credential can act on is a fact only it has. HQ derives its
        # "which domain" menu from exactly this.
        self.assertEqual(
            by_ref["cloudflare-dns-jseverino"]["reaches"],
            ["jseverino.com", "jseverino.net"],
        )
        self.assertEqual(by_ref["example-edge"]["reaches"], ["edge.example"])
        self.assertEqual(ssh.call_count, 2)
        self.assertNotIn("secret-c", json.dumps(result))

    @mock.patch.dict(
        "os.environ",
        {
            "CLOUDFLARE_DNS_URL": "https://api.cloudflare.com/client/v4",
            "CLOUDFLARE_DNS_API_TOKEN": "secret-c",
            "CLOUDFLARE_DNS_CONNECTION_REF": "cloudflare-dns-jseverino",
            "HQ_ACME_DIR": "/tmp",
        },
        clear=True,
    )
    @mock.patch("controller_runtime.providers._run")
    @mock.patch("controller_runtime.providers._request")
    def test_one_broken_credential_does_not_hide_the_others(self, request, _run):
        """A failure is that connection's, and the sweep still reports the rest.

        The alternative loses every row the moment one token expires, which is
        precisely when an operator needs to see which of them still works.
        """

        request.side_effect = providers.ProviderError("Token is not valid.")

        found = providers.connections()

        self.assertEqual(len(found), 1)
        self.assertFalse(found[0]["ok"])
        self.assertIn("Token is not valid.", found[0]["detail"])
        # And read as a gate rather than as a report, the same failure stops
        # the pass instead of being carried into an operation.
        with self.assertRaises(providers.ProviderError):
            providers.preflight()

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
    def test_adguard_renames_the_record_it_was_last_seen_holding(self, request):
        """A changed hostname moves the record rather than adding a second one.

        Without the previously observed state the controller searches for the
        new name, does not find it, and creates it -- leaving the old name
        resolving forever with nothing in HQ pointing at it, and no delete path
        that knows it exists.
        """
        request.side_effect = [
            [{"domain": "old.example", "answer": "192.0.2.10", "enabled": True}],
            None,
        ]

        result = providers.reconcile_adguard(
            {"domain": "new.example", "answer": "192.0.2.10"},
            observed={"domain": "old.example", "answer": "192.0.2.10"},
        )

        self.assertTrue(result.changed)
        update = request.call_args_list[-1]
        self.assertTrue(update.args[0].endswith("/control/rewrite/update"))
        self.assertEqual(
            update.kwargs["payload"],
            {
                "target": {"domain": "old.example", "answer": "192.0.2.10"},
                "update": {"domain": "new.example", "answer": "192.0.2.10"},
            },
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
    def test_a_name_never_seen_before_is_created_not_renamed(self, request):
        """Only a *changed* name is a rename. A new resource is still a create."""
        request.side_effect = [[], None]

        providers.reconcile_adguard(
            {"domain": "new.example", "answer": "192.0.2.10"}, observed={}
        )

        self.assertTrue(request.call_args_list[-1].args[0].endswith("/rewrite/add"))

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
    def test_npm_renames_in_place_by_id(self, request):
        request.side_effect = [
            {"token": "short-lived"},
            [{"id": 4, "domain_names": ["old.example"], "certificate_id": 2}],
            None,
        ]

        providers.reconcile_npm(
            {
                "domain_names": ["new.example"],
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
            },
            observed={"domain_names": ["old.example"]},
        )

        update = request.call_args_list[-1]
        self.assertTrue(update.args[0].endswith("/nginx/proxy-hosts/4"))
        self.assertEqual(update.kwargs["payload"]["domain_names"], ["new.example"])

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
    def test_removing_a_certificate_still_serving_a_host_is_refused(self, request):
        """Deleting it would take TLS down on whatever is bound to it.

        Naming the hosts is the actionable half: they have to be pointed at
        something else first.
        """
        request.side_effect = [
            {"token": "short-lived"},
            [{"id": 3, "nice_name": "Severino HQ - newhost-npm"}],
            [{"id": 9, "domain_names": ["newhost.example"], "certificate_id": 3}],
        ]

        with self.assertRaisesRegex(providers.ProviderError, "newhost.example"):
            providers.delete_uploaded_certificate(
                {
                    "certificate_name": "newhost",
                    "consumers": [{"kind": "npm", "name": "newhost-npm"}],
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
    def test_an_unbound_certificate_is_removed(self, request):
        request.side_effect = [
            {"token": "short-lived"},
            [{"id": 3, "nice_name": "Severino HQ - newhost-npm"}],
            [{"id": 9, "domain_names": ["other.example"], "certificate_id": 7}],
            None,
        ]

        result = providers.delete_uploaded_certificate(
            {
                "certificate_name": "newhost",
                "consumers": [{"kind": "npm", "name": "newhost-npm"}],
            }
        )

        self.assertTrue(result.changed)
        removal = request.call_args_list[-1]
        self.assertTrue(removal.args[0].endswith("/nginx/certificates/3"))
        self.assertEqual(removal.kwargs["method"], "DELETE")

    def test_removing_from_a_target_hq_cannot_reach_is_refused_whole(self):
        """A partial delete would drop HQ's record of a file still on a host.

        The Caddy transport implements deploy and nothing else, so there is no
        remove to call -- and reporting success would forget the only pointer to
        what was left behind.
        """
        with self.assertRaisesRegex(providers.ProviderError, "by hand"):
            providers.delete_uploaded_certificate(
                {
                    "certificate_name": "newhost",
                    "consumers": [
                        {"kind": "npm", "name": "newhost-npm"},
                        {"kind": "caddy", "name": "newhost-caddy"},
                    ],
                }
            )

    def test_changing_a_zone_itself_fails_closed(self):
        """Public DNS records apply now; the zone's own settings do not.

        The credential can read the zones and read and write their records, and
        answers 403 to everything else. So a request to reconcile the zone must
        fail here, in the controller, rather than reaching Cloudflare to be
        refused there.
        """

        with self.assertRaisesRegex(providers.ProviderError, "Zone Settings"):
            providers.execute({"kind": "cloudflare.zone", "spec": {}}, "reconcile")

    @mock.patch("controller_runtime.providers._certificate_registry")
    @mock.patch("controller_runtime.providers._observe_tls_domain")
    @mock.patch.dict(
        "os.environ",
        {
            "NPM_URL": "https://npm-origin.example",
            "EXAMPLE_EDGE_CONNECTION_REF": "example-edge",
            "EXAMPLE_EDGE_HOST": "192.0.2.20",
            "EXAMPLE_EDGE_PORT": "22",
            "EXAMPLE_EDGE_USER": "controller",
            "EXAMPLE_EDGE_HOST_KEY": "ssh-ed25519 AAAA",
        },
        clear=True,
    )
    def test_tls_observer_reports_consumer_drift_and_public_artifact(
        self, observe, registry
    ):
        registry.return_value = {
            "connections": {
                "edge": {"projection": "ssh_transport", "env_prefix": "EDGE"}
            }
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
                        "connection_ref": "example-edge",
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
    @mock.patch.dict(
        "os.environ",
        {
            "EXAMPLE_CPANEL_CONNECTION_REF": "example-cpanel",
            "EXAMPLE_CPANEL_HOST": "192.0.2.10",
            "EXAMPLE_CPANEL_PORT": "22",
            "EXAMPLE_CPANEL_USER": "controller",
            "EXAMPLE_CPANEL_HOST_KEY": "ssh-ed25519 AAAA",
        },
        clear=True,
    )
    def test_cpanel_tls_observation_bypasses_public_proxy(self, observe, registry):
        registry.return_value = {
            "connections": {
                "cpanel": {"projection": "ssh_transport", "env_prefix": "CPANEL"}
            }
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
                        "connection_ref": "example-cpanel",
                        "verify_domains": ["quiz.example.test"],
                    }
                ],
            }
        )

        observe.assert_called_once_with(
            "quiz.example.test", connect_host="192.0.2.10"
        )

    @mock.patch.dict("os.environ", {"NPM_URL": "https://proxy.example"}, clear=True)
    def test_npm_tls_endpoint_is_derived_from_controller_connection(self):
        self.assertEqual(
            providers._consumer_tls_endpoint({"kind": "npm"}),
            "proxy.example",
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
                {"id": 3, "domain_names": ["proxy.invalid"], "certificate_id": 7},
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
                {"kind": "caddy", "connection_ref": "example-edge"},
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
            "consumers": [{"kind": "caddy", "connection_ref": "example-edge"}],
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
                {"kind": "caddy", "connection_ref": "example-edge"},
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

    @mock.patch("controller_runtime.worker.connections", return_value=[])
    @mock.patch("controller_runtime.worker.preflight")
    @mock.patch("controller_runtime.worker._manage")
    def test_idle_apply_claims_without_provider_preflight(
        self, manage, preflight, _connections
    ):
        manage.side_effect = _bridge(
            **{
                "sweep-due": {"ok": True, "due": True},
                "connections": {"ok": True},
                "inventory": {"ok": True},
                "schedule": {"ok": True},
                "claim": {"ok": True, "operation": None},
            }
        )

        self.assertEqual(worker.run_once("test", apply=True), 0)

        called = [call.args[0] for call in manage.call_args_list]
        # Both sweeps, before anything is claimed. What HQ can reach is reported
        # ahead of what it found there, so an empty inventory can be read
        # against the credential that would have filled it.
        self.assertEqual(
            called, ["sweep-due", "connections", "inventory", "schedule", "claim"]
        )
        arguments = manage.call_args.args
        self.assertEqual(arguments[:3], ("claim", "--controller-id", "test"))
        self.assertIn("adguard.rewrite:reconcile", arguments)
        self.assertIn("npm.proxy_host:reconcile", arguments)
        self.assertIn("tls.certificate:reconcile", arguments)
        self.assertIn("tls.certificate:renew", arguments)
        preflight.assert_not_called()

    def test_capability_registry_drives_supported_kinds(self):
        """What the controller offers is the registry, minus what is locked.

        Derived rather than listed: a provider added to the registry appears
        here without this test being edited, which is the whole claim the
        registry makes.
        """
        from control_plane.providers import controller_capability_registry

        expected = tuple(
            sorted(
                (kind, action)
                for kind, capability in (
                    controller_capability_registry().capabilities.items()
                )
                for action, settings in capability.actions.items()
                # A locked action needs a credential the controller does not
                # hold -- a zone's own settings, for one -- so it is declared
                # and never offered.
                if settings.mode != "locked"
            )
        )

        self.assertEqual(worker.supported_capabilities(), expected)
        # And the exclusion is real, not vacuous.
        self.assertNotIn(("cloudflare.zone", "reconcile"), expected)

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

    @mock.patch("controller_runtime.worker.connections", return_value=[])
    @mock.patch("controller_runtime.worker.preflight", return_value=[])
    @mock.patch("controller_runtime.worker.execute")
    @mock.patch("controller_runtime.worker._manage")
    def test_provider_failure_is_reported_without_secret(
        self, manage, execute, _preflight, _connections
    ):
        manage.side_effect = _bridge(
            **{
                "sweep-due": {"ok": True, "due": True},
                "connections": {"ok": True, "recorded": []},
                "inventory": {"ok": True, "recorded": []},
                "schedule": {"ok": True, "scheduled": []},
                "claim": {
                    "operation": {"id": "operation-1", "action": "reconcile"},
                    "resource": {
                        "key": "dns",
                        "kind": "adguard.rewrite",
                        "generation": 2,
                        "spec": {},
                    },
                },
                "report": {"ok": True},
            }
        )
        execute.side_effect = providers.ProviderError("Provider request failed.")

        self.assertEqual(worker.run_once("test", apply=True), 1)

        report_payload = json.loads(manage.call_args.args[-1])
        self.assertFalse(report_payload["success"])
        self.assertNotIn("password", json.dumps(report_payload).lower())

    @mock.patch("controller_runtime.worker.connections", return_value=[])
    @mock.patch("controller_runtime.worker.preflight")
    @mock.patch("controller_runtime.worker.execute")
    @mock.patch("controller_runtime.worker._manage")
    def test_claimed_operation_preflights_before_provider_execution(
        self, manage, execute, preflight, _connections
    ):
        manage.side_effect = _bridge(
            **{
                "sweep-due": {"ok": True, "due": True},
                "connections": {"ok": True, "recorded": []},
                "inventory": {"ok": True, "recorded": []},
                "schedule": {"ok": True, "scheduled": []},
                "claim": {
                    "operation": {"id": "operation-1", "action": "reconcile"},
                    "resource": {
                        "key": "dns",
                        "kind": "adguard.rewrite",
                        "generation": 2,
                        "spec": {},
                    },
                },
                "report": {"ok": True},
            }
        )
        execute.return_value = providers.ProviderResult(
            changed=False,
            status={},
            conditions=[],
            message="Current.",
        )

        self.assertEqual(worker.run_once("test", apply=True), 0)

        preflight.assert_called_once_with()
        execute.assert_called_once()


CLOUDFLARE_ENV = {
    "CLOUDFLARE_DNS_URL": "https://api.cloudflare.com/client/v4",
    "CLOUDFLARE_DNS_API_TOKEN": "secret-c",
    "CLOUDFLARE_DNS_CONNECTION_REF": "cloudflare-dns-example",
}

ZONE = {"id": "zone1", "name": "example.com", "status": "active", "plan": {"name": "Free"}}


def live(record_id, rtype, name, content, **extra):
    """A record shaped exactly as Cloudflare returns one.

    The field set was read off the real API rather than assumed: `priority` is
    present and null except on MX, CAA carries a formatted `content` *and* a
    `data` object, and a proxied record always reports ttl 1.
    """

    record = {
        "id": record_id,
        "type": rtype,
        "name": name,
        "content": content,
        "proxied": extra.get("proxied", False),
        "ttl": extra.get("ttl", 1),
        "priority": extra.get("priority"),
        "data": extra.get("data"),
    }
    return record


@mock.patch.dict("os.environ", CLOUDFLARE_ENV, clear=True)
class CloudflareAdapterTests(TestCase):
    """The code that actually changes public DNS.

    Every case here is one where getting it wrong is expensive and quiet: a
    record edited into existence twice, a sibling deleted along with its
    neighbour, or a record that reports as drifted against itself and is
    rewritten on every pass forever.
    """

    def setUp(self):
        # Module-level cache of zone name -> id. Harmless in a controller run
        # that lasts a second; between tests it would carry one case's zones
        # into the next.
        providers._ZONE_IDS.clear()

    def _calls(self, request):
        return [call.args[0] for call in request.call_args_list]

    @mock.patch("controller_runtime.providers._cloudflare_request")
    def test_a_missing_record_is_created(self, request):
        request.side_effect = [[ZONE], [], live("new1", "A", "app.example.com", "203.0.113.1")]

        result = providers.reconcile_cloudflare_record({
            "zone": "example.com", "name": "app.example.com",
            "record_type": "A", "content": "203.0.113.1", "proxied": False, "ttl": 1,
        })

        self.assertTrue(result.changed)
        created = request.call_args_list[-1]
        self.assertEqual(created.kwargs["method"], "POST")
        self.assertEqual(created.kwargs["payload"]["type"], "A")
        self.assertEqual(created.kwargs["payload"]["content"], "203.0.113.1")
        self.assertEqual(result.status["record_id"], "new1")

    @mock.patch("controller_runtime.providers._cloudflare_request")
    def test_a_matching_record_is_left_alone(self, request):
        request.side_effect = [
            [ZONE],
            [live("r1", "A", "app.example.com", "203.0.113.1")],
        ]

        result = providers.reconcile_cloudflare_record({
            "zone": "example.com", "name": "app.example.com",
            "record_type": "A", "content": "203.0.113.1", "proxied": False, "ttl": 1,
        })

        self.assertFalse(result.changed)
        # Two reads and no write. A reconciler that rewrites an already-correct
        # record burns an API call per pass and hides real changes in the log.
        self.assertEqual(request.call_count, 2)

    @mock.patch("controller_runtime.providers._cloudflare_request")
    def test_a_changed_value_updates_that_record_in_place(self, request):
        request.side_effect = [
            [ZONE],
            [live("r1", "A", "app.example.com", "203.0.113.1")],
            live("r1", "A", "app.example.com", "203.0.113.9"),
        ]

        result = providers.reconcile_cloudflare_record(
            {
                "zone": "example.com", "name": "app.example.com",
                "record_type": "A", "content": "203.0.113.9", "proxied": False, "ttl": 1,
            },
            observed={"record_id": "r1"},
        )

        self.assertTrue(result.changed)
        written = request.call_args_list[-1]
        self.assertEqual(written.kwargs["method"], "PUT")
        self.assertIn("/dns_records/r1", written.args[0])

    @mock.patch("controller_runtime.providers._cloudflare_request")
    def test_retargeting_a_record_moves_it_rather_than_cloning_it(self, request):
        """The bug class that made renaming create a second record.

        Name and value both change at once, so nothing matches by content. The
        record id HQ was last seen holding is the only thing that still
        identifies it -- without that this becomes a create, and the old record
        keeps answering with nothing in HQ pointing at it.
        """

        request.side_effect = [
            [ZONE],
            [live("r1", "A", "old.example.com", "203.0.113.1")],
            live("r1", "A", "new.example.com", "203.0.113.7"),
        ]

        result = providers.reconcile_cloudflare_record(
            {
                "zone": "example.com", "name": "new.example.com",
                "record_type": "A", "content": "203.0.113.7", "proxied": False, "ttl": 1,
            },
            observed={"record_id": "r1"},
        )

        self.assertTrue(result.changed)
        written = request.call_args_list[-1]
        self.assertEqual(written.kwargs["method"], "PUT")
        self.assertIn("/dns_records/r1", written.args[0])

    @mock.patch("controller_runtime.providers._cloudflare_request")
    def test_one_of_nine_records_on_a_name_is_the_one_edited(self, request):
        """A zone apex holds many records. Matching by name would pick a coin toss."""

        siblings = [
            live("c1", "CAA", "example.com", '0 issue "letsencrypt.org"',
                 data={"flags": 0, "tag": "issue", "value": "letsencrypt.org"}),
            live("c2", "CAA", "example.com", '0 issuewild "letsencrypt.org"',
                 data={"flags": 0, "tag": "issuewild", "value": "letsencrypt.org"}),
            live("m1", "MX", "example.com", "mx01.example.net", priority=10),
            live("m2", "MX", "example.com", "mx02.example.net", priority=20),
        ]
        request.side_effect = [[ZONE], siblings, live("m2", "MX", "example.com", "mx03.example.net", priority=20)]

        providers.reconcile_cloudflare_record(
            {
                "zone": "example.com", "name": "example.com",
                "record_type": "MX", "content": "mx03.example.net",
                "priority": 20, "proxied": False, "ttl": 1,
            },
            observed={"record_id": "m2"},
        )

        self.assertIn("/dns_records/m2", request.call_args_list[-1].args[0])

    @mock.patch("controller_runtime.providers._cloudflare_request")
    def test_caa_is_sent_as_three_fields_not_as_a_string(self, request):
        """Cloudflare returns CAA as one string and accepts it only as data."""

        request.side_effect = [[ZONE], [], live("c1", "CAA", "example.com", '0 issue "letsencrypt.org"')]

        providers.reconcile_cloudflare_record({
            "zone": "example.com", "name": "example.com",
            "record_type": "CAA", "content": '0 issue "letsencrypt.org"',
            "proxied": False, "ttl": 1,
        })

        payload = request.call_args_list[-1].kwargs["payload"]
        self.assertEqual(payload["data"], {"flags": 0, "tag": "issue", "value": "letsencrypt.org"})
        self.assertNotIn("content", payload)

    @mock.patch("controller_runtime.providers._cloudflare_request")
    def test_an_mx_carries_its_priority_and_an_address_record_does_not(self, request):
        request.side_effect = [[ZONE], [], live("m1", "MX", "example.com", "mx.example.net", priority=10)]
        providers.reconcile_cloudflare_record({
            "zone": "example.com", "name": "example.com",
            "record_type": "MX", "content": "mx.example.net",
            "priority": 10, "proxied": False, "ttl": 1,
        })
        self.assertEqual(request.call_args_list[-1].kwargs["payload"]["priority"], 10)

        providers._ZONE_IDS.clear()
        request.reset_mock()
        request.side_effect = [[ZONE], [], live("a1", "A", "app.example.com", "203.0.113.1")]
        providers.reconcile_cloudflare_record({
            "zone": "example.com", "name": "app.example.com",
            "record_type": "A", "content": "203.0.113.1", "proxied": False, "ttl": 1,
        })
        payload = request.call_args_list[-1].kwargs["payload"]
        self.assertNotIn("priority", payload)
        # proxied is only sent for the types that can carry it; Cloudflare
        # rejects the field outright on a TXT or MX record.
        self.assertIn("proxied", payload)

    @mock.patch("controller_runtime.providers._cloudflare_request")
    def test_a_txt_value_matches_whether_or_not_it_was_typed_quoted(self, request):
        """Cloudflare stores TXT quoted and returns it quoted, always."""

        request.side_effect = [
            [ZONE],
            [live("t1", "TXT", "example.com", '"v=spf1 -all"')],
        ]

        result = providers.reconcile_cloudflare_record({
            "zone": "example.com", "name": "example.com",
            "record_type": "TXT", "content": "v=spf1 -all", "proxied": False, "ttl": 1,
        })

        self.assertFalse(result.changed)

    @mock.patch("controller_runtime.providers._cloudflare_request")
    def test_a_name_typed_in_capitals_is_not_permanent_drift(self, request):
        """Cloudflare lowercases names, so sending the typed case never matches."""

        request.side_effect = [
            [ZONE],
            [live("a1", "A", "app.example.com", "203.0.113.1")],
        ]

        result = providers.reconcile_cloudflare_record({
            "zone": "example.com", "name": "APP.example.com",
            "record_type": "A", "content": "203.0.113.1", "proxied": False, "ttl": 1,
        })

        self.assertFalse(result.changed)

    @mock.patch("controller_runtime.providers._cloudflare_request")
    def test_a_caa_value_with_extra_spaces_is_not_permanent_drift(self, request):
        request.side_effect = [
            [ZONE],
            [live("c1", "CAA", "example.com", '0 issue "letsencrypt.org"',
                  data={"flags": 0, "tag": "issue", "value": "letsencrypt.org"})],
        ]

        result = providers.reconcile_cloudflare_record({
            "zone": "example.com", "name": "example.com",
            "record_type": "CAA", "content": '0  issue   "letsencrypt.org"',
            "proxied": False, "ttl": 1,
        })

        self.assertFalse(result.changed)

    @mock.patch("controller_runtime.providers._cloudflare_request")
    def test_delete_removes_only_the_record_it_owns(self, request):
        siblings = [
            live("t1", "TXT", "example.com", '"one"'),
            live("t2", "TXT", "example.com", '"two"'),
            live("t3", "TXT", "example.com", '"three"'),
        ]
        request.side_effect = [[ZONE], siblings, None]

        result = providers.delete_cloudflare_record(
            {"zone": "example.com", "name": "example.com",
             "record_type": "TXT", "content": '"two"'},
            observed={"record_id": "t2"},
        )

        self.assertTrue(result.changed)
        deleted = request.call_args_list[-1]
        self.assertEqual(deleted.kwargs["method"], "DELETE")
        self.assertIn("/dns_records/t2", deleted.args[0])

    @mock.patch("controller_runtime.providers._cloudflare_request")
    def test_deleting_something_already_gone_is_success(self, request):
        """Deletion has to be idempotent: the queue retries a delete that
        applied and then failed to report, and a second attempt finding nothing
        has achieved exactly what was asked."""

        request.side_effect = [[ZONE], []]

        result = providers.delete_cloudflare_record(
            {"zone": "example.com", "name": "gone.example.com",
             "record_type": "A", "content": "203.0.113.1"},
        )

        self.assertFalse(result.changed)
        self.assertEqual(request.call_count, 2)

    @mock.patch("controller_runtime.providers._cloudflare_request")
    def test_a_zone_the_credential_cannot_see_is_named(self, request):
        request.side_effect = [[ZONE]]

        with self.assertRaisesRegex(providers.ProviderError, "elsewhere.example"):
            providers.reconcile_cloudflare_record({
                "zone": "elsewhere.example", "name": "app.elsewhere.example",
                "record_type": "A", "content": "203.0.113.1", "proxied": False, "ttl": 1,
            })

    @mock.patch("controller_runtime.providers._cloudflare_request")
    def test_every_page_of_a_long_zone_is_read(self, request):
        """Cloudflare returns 100 records at most.

        A zone that outgrew one page would have its tail reported as absent, and
        absent is the word this system acts on -- the reconciler would set about
        recreating records that were there all along.
        """

        first = [live(f"r{i}", "A", f"h{i}.example.com", "203.0.113.1") for i in range(100)]
        second = [live("r100", "A", "h100.example.com", "203.0.113.1")]
        request.side_effect = [[ZONE], first, second]

        records = providers.list_cloudflare_records()

        self.assertEqual(len(records), 101)
        self.assertIn("page=2", self._calls(request)[-1])

    @mock.patch("controller_runtime.providers._cloudflare_request")
    def test_the_inventory_reports_what_hq_can_express(self, request):
        request.side_effect = [
            [ZONE],
            [ZONE],
            [live("m1", "MX", "example.com", "mx.example.net", priority=10)],
        ]

        zones = providers.list_cloudflare_zones()
        records = providers.list_cloudflare_records()

        self.assertEqual(zones[0]["zone"], "example.com")
        self.assertEqual(zones[0]["connection_ref"], "cloudflare-dns-example")
        self.assertEqual(records[0]["record_id"], "m1")
        self.assertEqual(records[0]["priority"], 10)
        self.assertEqual(records[0]["zone"], "example.com")


class ControllerStepReportingTests(TestCase):
    """A failure should name the step that failed, not the module that ran it."""

    def test_a_failing_step_is_named(self):
        from controller_runtime.providers import ProviderError, _run

        with mock.patch("controller_runtime.providers.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=1, stdout=b"", stderr=b"boom")
            with self.assertRaises(ProviderError) as caught:
                _run(["/bin/false"], step="SSH preflight for somewhere")
        self.assertIn("SSH preflight for somewhere", str(caught.exception))
        self.assertNotIn("Certificate", str(caught.exception))

    def test_subprocess_output_never_reaches_the_result(self):
        """Remote paths and messages belong in the log, not in a provider result."""

        from controller_runtime.providers import ProviderError, _run

        with mock.patch("controller_runtime.providers.subprocess.run") as run:
            run.return_value = mock.Mock(
                returncode=1, stdout=b"", stderr=b"/home/someone/secret/path missing"
            )
            with self.assertRaises(ProviderError) as caught:
                _run(["/bin/false"], step="a step")
        self.assertNotIn("/home/someone", str(caught.exception))


class WorkerEntryPointTests(TestCase):
    """The worker's own start-up, which nothing else exercises.

    Every other test calls `run_once` directly and never builds the parser, so
    a default that reads a field the contract no longer carries raised nothing
    until the controller started -- which is after the image is deployed.
    """

    def test_it_starts_and_names_the_machine_it_runs_on(self):
        with mock.patch.object(worker.sys, "argv", ["worker"]), mock.patch.object(
            worker, "run_once", return_value=0
        ) as run:
            self.assertEqual(worker.main(), 0)

        self.assertEqual(run.call_args.args[0], os.uname().nodename)
        self.assertFalse(run.call_args.kwargs["apply"])

    @mock.patch.dict("os.environ", {"HQ_CONTROLLER_ID": "a-named-controller"})
    def test_the_environment_names_it_when_it_says_so(self):
        with mock.patch.object(worker.sys, "argv", ["worker"]), mock.patch.object(
            worker, "run_once", return_value=0
        ) as run:
            worker.main()

        self.assertEqual(run.call_args.args[0], "a-named-controller")

    def test_apply_is_off_unless_asked_for(self):
        with mock.patch.object(worker.sys, "argv", ["worker", "--apply"]), (
            mock.patch.object(worker, "run_once", return_value=0)
        ) as run:
            worker.main()

        self.assertTrue(run.call_args.kwargs["apply"])
