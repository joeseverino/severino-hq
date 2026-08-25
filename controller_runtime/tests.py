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

    def test_every_remote_provider_has_exactly_one_health_probe(self):
        from control_plane.providers import PROVIDERS

        declared = {
            provider
            for spec in PROVIDERS.values()
            for provider in spec.connection_providers
            if provider != "ssh"
        }

        self.assertEqual(set(providers._CONNECTION_PROBES), declared)


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
    def test_connection_sweep_probes_every_credential_the_environment_carries(
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

        result = providers.connections()

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

    @mock.patch("controller_runtime.providers._cloudflare_paged")
    def test_one_sweep_reuses_successful_provider_reads_and_then_forgets_them(
        self, paged
    ):
        paged.return_value = [{"id": "zone-1", "name": "example.test"}]

        with providers.provider_snapshot():
            first = providers._cloudflare_zones()
            second = providers._cloudflare_zones()

        third = providers._cloudflare_zones()

        self.assertIs(first, second)
        self.assertEqual(third, first)
        self.assertEqual(paged.call_count, 2)

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
        # The selected action verifies the credential it actually uses, so
        # this unrelated failure is observable without becoming a global gate.

    @mock.patch.dict(
        "os.environ",
        {
            "TAILSCALE_CONNECTION_REF": "example-tailnet",
            "TAILSCALE_PROVIDER": "tailscale",
            "TAILSCALE_CLIENT_ID": "client-id",
            "TAILSCALE_CLIENT_SECRET": "client-secret",
        },
        clear=True,
    )
    @mock.patch("controller_runtime.providers.urllib.request.urlopen")
    def test_tailscale_oauth_connection_is_reported_without_secret_material(
        self, urlopen
    ):
        response = urlopen.return_value.__enter__.return_value
        response.read.return_value = b'{"access_token":"short-lived-access-token"}'

        found = providers.connections()

        self.assertEqual(
            found,
            [
                {
                    "connection_ref": "example-tailnet",
                    "provider": "tailscale",
                    "endpoint": providers.TAILNET_API,
                    "probed": True,
                    "ok": True,
                    "detail": "OAuth credential accepted.",
                    "reaches": [],
                }
            ],
        )
        urlopen.assert_called_once()
        self.assertNotIn("client-secret", json.dumps(found))
        self.assertNotIn("short-lived-access-token", json.dumps(found))

    @mock.patch.dict(
        "os.environ",
        {
            "TAILSCALE_CONNECTION_REF": "example-tailnet",
            "TAILSCALE_PROVIDER": "tailscale",
            "TAILSCALE_CLIENT_ID": "client-id",
            "TAILSCALE_CLIENT_SECRET": "client-secret",
        },
        clear=True,
    )
    @mock.patch("controller_runtime.providers.urllib.request.urlopen")
    def test_tailscale_probe_failure_is_isolated_and_safe(self, urlopen):
        urlopen.side_effect = providers.urllib.error.HTTPError(
            "https://example.invalid", 401, "Unauthorized", {}, None
        )

        found = providers.connections()

        self.assertEqual(len(found), 1)
        self.assertFalse(found[0]["ok"])
        self.assertIn("example-tailnet", found[0]["detail"])
        self.assertIn("401", found[0]["detail"])
        self.assertNotIn("client-secret", json.dumps(found))

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

    @mock.patch("controller_runtime.providers._request")
    @mock.patch("controller_runtime.providers._npm_token", return_value="token")
    @mock.patch.dict("os.environ", {"NPM_URL": "https://npm.example.test"}, clear=True)
    def test_npm_inventory_emits_safe_ingress_policy_evidence(self, _token, request):
        request.side_effect = [
            [
                {
                    "domain_names": ["hq.example.test"],
                    "access_list_id": 4,
                    "certificate_id": 0,
                }
            ],
            [
                {
                    "id": 4,
                    "name": "Tailnet only",
                    "satisfy_any": False,
                    "pass_auth": False,
                    "items": [
                        {
                            "username": "must-not-leave-provider",
                            "hint": "m***",
                            "password": "",
                        }
                    ],
                    "clients": [
                        {"directive": "allow", "address": "100.64.0.0/10"},
                        {"directive": "deny", "address": "all"},
                    ],
                }
            ],
            [],
        ]

        found = providers.list_npm()[0]["access_policy"]

        self.assertEqual(found["authorization_count"], 1)
        self.assertEqual(
            found["clients"],
            [
                {"directive": "allow", "address": "100.64.0.0/10"},
                {"directive": "deny", "address": "all"},
            ],
        )
        serialized = json.dumps(found).lower()
        self.assertNotIn("must-not-leave-provider", serialized)
        self.assertNotIn("password", serialized)
        self.assertNotIn("hint", serialized)

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

    @mock.patch("controller_runtime.worker.connections", return_value=[])
    @mock.patch("controller_runtime.worker._manage")
    def test_idle_plan_reports_connections_without_claiming(self, manage, connections):
        manage.return_value = {"ok": True, "operation": None}

        self.assertEqual(worker.run_once("test", apply=False), 0)

        self.assertEqual(manage.call_args.args[0], "peek")
        self.assertNotIn("claim", manage.call_args.args)
        connections.assert_called_once_with()

    @mock.patch("builtins.print")
    @mock.patch(
        "controller_runtime.worker.connections",
        return_value=[{"connection_ref": "broken", "ok": False}],
    )
    @mock.patch("controller_runtime.worker.execute")
    @mock.patch("controller_runtime.worker._manage")
    def test_plan_reports_broken_connections_and_returns_failure(
        self, manage, execute, connections, output
    ):
        manage.return_value = {
            "operation": {"id": "operation-1", "action": "reconcile"},
            "resource": {"key": "dns", "kind": "adguard.rewrite", "spec": {}},
        }
        execute.return_value = providers.ProviderResult(
            changed=False, status={}, conditions=[], message="Current."
        )

        self.assertEqual(worker.run_once("test", apply=False), 1)

        payload = json.loads(output.call_args.args[0])
        self.assertFalse(payload["ok"])
        self.assertFalse(payload["connections"][0]["ok"])
        connections.assert_called_once_with()
        execute.assert_called_once()

    @mock.patch("controller_runtime.worker.connections", return_value=[])
    @mock.patch("controller_runtime.worker._manage")
    def test_idle_apply_claims_after_reporting_findings(self, manage, _connections):
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
    @mock.patch("controller_runtime.worker.execute")
    @mock.patch("controller_runtime.worker._manage")
    def test_provider_failure_is_reported_without_secret(
        self, manage, execute, _connections
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
    @mock.patch("controller_runtime.worker.execute")
    @mock.patch("controller_runtime.worker._manage")
    def test_claimed_operation_executes_without_an_unrelated_global_gate(
        self, manage, execute, connections
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

        connections.assert_called_once_with()
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


class MachineNameTests(TestCase):
    """Portainer calls its own environment "local", which is nobody's hostname.

    Everything ties to a machine by name, so filing containers under "local"
    splits one machine into two: the credential and the services on one row, the
    containers on another.
    """

    @mock.patch.dict("os.environ", {"HQ_CONTROLLER_ID": ""})
    def test_the_local_socket_is_the_machine_this_runs_on(self):
        record = providers._container_record(
            {"Names": ["/app"], "State": "running"}, providers.controller_id(), ""
        )

        self.assertEqual(record["host"], os.uname().nodename)
        self.assertNotEqual(record["host"], "local")

    @mock.patch.dict("os.environ", {"HQ_CONTROLLER_ID": "a-named-host"})
    def test_the_environment_names_it_when_it_says_so(self):
        self.assertEqual(providers.controller_id(), "a-named-host")


class TailnetSweepTests(TestCase):
    """Reading the tailnet through the daemon this node is already a peer of.

    Every field is optional. Tailscale omits rather than nulls -- a device with
    expiry disabled carries no ``KeyExpiry`` and one never seen carries no
    ``LastSeen`` -- so a reader that requires any of them rejects exactly the
    devices it exists to describe.
    """

    STATUS = {
        "Self": {"HostName": "this-node", "Online": True, "OS": "linux"},
        "Peer": {
            "k1": {
                "HostName": "an-edge",
                "DNSName": "an-edge.example.ts.net.",
                "Online": True,
                "KeyExpiry": "2026-11-04T00:00:00Z",
                "TailscaleIPs": ["100.64.0.2"],
                "OS": "linux",
                "ExitNode": True,
                "ExitNodeOption": True,
            },
            "k2": {"HostName": "a-tv", "Online": False, "LastSeen": "2026-07-01T00:00:00Z"},
        },
    }

    def sweep(self, status=None, *, path=None):
        import tempfile
        from unittest import mock

        with tempfile.TemporaryDirectory() as directory:
            reading = Path(directory) / "tailnet.json"
            reading.write_text(
                json.dumps(self.STATUS if status is None else status), encoding="utf-8"
            )
            given = str(reading) if path is None else path
            with mock.patch.object(providers, "TAILNET_STATUS", given):
                return providers.list_tailnet_devices()

    def by_name(self):
        return {record["name"]: record for record in self.sweep()}

    def test_the_node_itself_is_one_of_the_machines(self):
        self.assertIn("this-node", self.by_name())

    def test_presence_comes_across(self):
        found = self.by_name()
        self.assertTrue(found["an-edge"]["online"])
        self.assertFalse(found["a-tv"]["online"])

    def test_offering_to_be_an_exit_node_is_not_being_the_one_in_use(self):
        """Two different questions. `ExitNode` is whether this peer is the exit
        node the reading machine currently routes through -- a fact about our
        own preference. `ExitNodeOption` is whether the peer offers to be one.
        A machine page saying "exit node" means the second."""

        found = self.by_name()

        self.assertTrue(found["an-edge"]["offers_exit_node"])
        self.assertTrue(found["an-edge"]["exit_node_in_use"])
        # A device that offers nothing says so on both counts.
        self.assertFalse(found["a-tv"]["offers_exit_node"])
        self.assertFalse(found["a-tv"]["exit_node_in_use"])

    def test_a_key_expiry_is_carried_and_its_absence_is_not_invented(self):
        found = self.by_name()
        self.assertEqual(found["an-edge"]["key_expires"], "2026-11-04T00:00:00Z")
        self.assertEqual(found["a-tv"]["key_expires"], "")

    def test_a_device_missing_every_optional_field_is_still_read(self):
        records = self.sweep({"Self": {"HostName": "bare"}, "Peer": {}})

        self.assertEqual(records[0]["name"], "bare")

    def test_a_nameless_device_is_dropped_rather_than_named_blank(self):
        records = self.sweep({"Self": {}, "Peer": {"k": {"HostName": ""}}})

        self.assertEqual(records, [])

    def test_a_controller_given_no_reading_says_so(self):
        """A machine not on a tailnet is a supported way to be."""

        from unittest import mock

        with mock.patch.object(providers, "TAILNET_STATUS", ""):
            with self.assertRaises(providers.ProviderError) as raised:
                providers.list_tailnet_devices()

        self.assertIn("not given a tailnet reading", str(raised.exception))

    def test_a_missing_reading_is_reported_rather_than_crashing(self):
        with self.assertRaises(providers.ProviderError) as raised:
            self.sweep(path="/nonexistent/tailnet.json")

        self.assertIn("missing", str(raised.exception))

    def test_the_controller_never_holds_the_daemon_socket(self):
        """The local API is read and write, and this only ever needed reading.

        Asserted on the module rather than trusted: the socket was mounted into
        this container once, and the process that reads it holds every provider
        credential HQ has.
        """

        source = Path(providers.__file__).read_text(encoding="utf-8")

        self.assertNotIn("tailscaled.sock", source)

    def test_an_unreachable_daemon_does_not_take_the_sweep_down(self):
        """One provider that cannot be read must not lose the other five."""

        from unittest import mock

        with mock.patch.object(
            providers, "list_tailnet_devices", side_effect=providers.ProviderError("no")
        ):
            found = providers.inventory()

        self.assertFalse(found["tailscale.device"]["ok"])
        self.assertEqual(found["tailscale.device"]["records"], [])


class TailnetDeviceTests(TestCase):
    """Asserting HQ's one decision about a device, and nothing else about it."""

    STATUS = {
        "Self": {"HostName": "this-node", "ID": "nSELF", "Online": True},
        "Peer": {
            "k1": {
                "HostName": "an-edge",
                "ID": "nEDGE",
                "Online": True,
                "KeyExpiry": "2026-11-04T00:00:00Z",
            },
            "k2": {"HostName": "a-server", "ID": "nSERV", "Online": True},
        },
    }

    def setUp(self):
        import tempfile

        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        reading = Path(self.directory.name) / "tailnet.json"
        reading.write_text(json.dumps(self.STATUS), encoding="utf-8")
        patch = mock.patch.object(providers, "TAILNET_STATUS", str(reading))
        patch.start()
        self.addCleanup(patch.stop)

    def spec(self, name="an-edge", disabled=True):
        return {
            "connection_ref": "a-tailnet",
            "name": name,
            "key_expiry_disabled": disabled,
        }

    def test_a_device_already_as_declared_is_left_alone(self):
        """a-server has no expiry, so there is nothing to assert."""

        result = providers.reconcile_tailnet_device(self.spec("a-server"))

        self.assertFalse(result.changed)

    def test_a_dry_run_says_what_it_would_do_and_does_not_do_it(self):
        with mock.patch.object(providers, "_tailnet_token") as token:
            result = providers.reconcile_tailnet_device(self.spec(), apply=False)

        self.assertTrue(result.changed)
        token.assert_not_called()

    def test_the_device_id_comes_from_the_local_reading_not_the_api(self):
        """The token is spent on the change and on nothing else."""

        self.assertEqual(providers._tailnet_device_id("an-edge"), "nEDGE")

    def test_a_device_the_tailnet_does_not_show_is_refused_by_name(self):
        with self.assertRaises(providers.ProviderError) as raised:
            providers.reconcile_tailnet_device(self.spec("a-ghost"))

        self.assertIn("a-ghost", str(raised.exception))

    def test_a_credential_without_the_scope_says_which_scope(self):
        import urllib.error

        refused = urllib.error.HTTPError("u", 403, "Forbidden", {}, None)
        with (
            mock.patch.object(providers, "_tailnet_token", return_value="t"),
            mock.patch.object(providers.urllib.request, "urlopen", side_effect=refused),
            self.assertRaises(providers.ProviderError) as raised,
        ):
            providers.reconcile_tailnet_device(self.spec())

        self.assertIn("devices:core", str(raised.exception))

    def test_an_api_key_used_as_an_oauth_client_says_so(self):
        import urllib.error

        refused = urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)
        with (
            mock.patch.dict(
                os.environ,
                {
                    "TAILNET_CONNECTION_REF": "a-tailnet",
                    "TAILNET_PROVIDER": "tailscale",
                    "TAILNET_CLIENT_ID": "id",
                    "TAILNET_CLIENT_SECRET": "secret",
                },
            ),
            mock.patch.object(providers.urllib.request, "urlopen", side_effect=refused),
            self.assertRaises(providers.ProviderError) as raised,
        ):
            providers._tailnet_token("a-tailnet")

        self.assertIn("OAuth client", str(raised.exception))

    def test_a_malformed_oauth_response_fails_as_a_safe_provider_error(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b"[]"
        with (
            mock.patch.dict(
                os.environ,
                {
                    "TAILNET_CONNECTION_REF": "a-tailnet",
                    "TAILNET_PROVIDER": "tailscale",
                    "TAILNET_CLIENT_ID": "id",
                    "TAILNET_CLIENT_SECRET": "secret",
                },
            ),
            mock.patch.object(
                providers.urllib.request, "urlopen", return_value=response
            ),
            self.assertRaises(providers.ProviderError) as raised,
        ):
            providers._tailnet_token("a-tailnet")

        self.assertEqual(
            str(raised.exception), "Tailscale did not answer the token request."
        )
        self.assertNotIn("secret", str(raised.exception))

    def test_the_token_is_never_kept(self):
        """It lasts an hour and a sweep is minutes apart, so caching it would
        only add an expiry to get wrong."""

        source = Path(providers.__file__).read_text(encoding="utf-8")

        self.assertNotIn("_TOKEN_CACHE", source)
        self.assertEqual(source.count("def _tailnet_token"), 1)
