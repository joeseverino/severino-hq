from __future__ import annotations

import datetime
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import TestCase, mock

from control_plane.provider_adapters import npm, onepassword, portainer_readings

from . import providers
from controller_runtime import (
    cloudflare,
    cloudflare_api,
    commands,
    connection_env,
    glance,
    host_readings,
    npm_certificates,
    portainer,
    provider_http,
    provider_runtime,
    tailnet_api,
    tailnet_policy,
    tailscale,
    tls,
    tls_issuance,
    tls_verification,
)
from control_plane.provider_adapters.contracts import (
    ADDRESS_FAILURE,
    CREDENTIAL_REFUSAL,
    NETWORK_FAILURE,
    ProviderError,
    ProviderResult,
)
from control_plane.providers import controller_id, observer_abilities
import ssl
import urllib.request
from .test_support import _by_url, _Page, _Recorder, A_RECORDED_CERTIFICATE, AN_OBSERVATION

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
        """A probe exists for a stated use, and every stated use is probed.

        Two sources of use, not one. A resource declares the connection it
        reconciles through; a reading declares the connection it only looks
        with. Both are reasons to carry a credential, and neither may carry one
        HQ cannot say whether it can still reach.
        """

        from control_plane.providers import PROVIDERS

        reconciled = {
            provider
            for spec in PROVIDERS.values()
            for provider in spec.connection_providers
            if provider != "ssh"
        }

        self.assertEqual(
            set(providers._CONNECTION_PROBES),
            reconciled | {ability.provider for ability in observer_abilities()},
        )

    def test_a_connection_is_reconciled_through_or_observed_with_never_both(self):
        """The two categories have to stay meaningful to be worth separating."""

        from control_plane.providers import PROVIDERS

        reconciled = {
            provider
            for spec in PROVIDERS.values()
            for provider in spec.connection_providers
        }

        self.assertEqual(reconciled & {ability.provider for ability in observer_abilities()}, set())


class ProviderAdapterTests(TestCase):
    def adguard(self, action, spec, *, apply=True, observed=None):
        return providers.execute(
            {
                "kind": "adguard.rewrite",
                "spec": spec,
                "observed": observed or {},
            },
            action,
            apply=apply,
        )

    @mock.patch("controller_runtime.glance._portainer_glance")
    @mock.patch("controller_runtime.commands.run_ssh")
    @mock.patch(
        "controller_runtime.connection_env.ssh_connection_refs",
        return_value=("example-ssh",),
    )
    def test_dashboard_glance_prefers_whole_host_telemetry(
        self, _connection_refs, ssh, portainer
    ):
        ssh.return_value = json.dumps(
            {
                "cpu_percent": 17.4,
                "cores": 8,
                "load_1m": 0.42,
                "memory_used": 8 * 1024**3,
                "memory_total": 32 * 1024**3,
                "storage_used": 250 * 1024**3,
                "storage_total": 1000 * 1024**3,
            }
        ).encode()

        result = glance.dashboard_glance(
            {
                "panels": ["infrastructure"],
                "targets": {
                    "infrastructure": [
                        {
                            "key": "example-host",
                            "connections": ["example-ssh"],
                        }
                    ]
                },
            }
        )

        machine = result[0]["machines"][0]
        self.assertEqual(machine["key"], "example-host")
        self.assertEqual(
            [metric["value"] for metric in machine["metrics"]],
            ["17%", "25%", "25%"],
        )
        ssh.assert_called_once_with(
            "example-ssh", "python3 -", glance._HOST_GLANCE_SCRIPT
        )
        portainer.assert_not_called()

    @mock.patch("controller_runtime.connection_env.ssh_connection_refs", return_value=())
    @mock.patch("controller_runtime.glance._portainer_glance")
    def test_dashboard_glance_labels_docker_fallback_by_scope(
        self, portainer, _connection_refs
    ):
        portainer.return_value = {
            "panel_id": "infrastructure",
            "machines": [
                {
                    "key": "example-host",
                    "status": "good",
                    "summary": "4 containers running",
                    "metrics": [
                        {"label": "Container CPU", "value": "7%", "detail": ""},
                        {
                            "label": "Container memory",
                            "value": "2 GB",
                            "detail": "",
                        },
                    ],
                }
            ],
        }

        result = glance.dashboard_glance(
            {
                "panels": ["infrastructure"],
                "targets": {
                    "infrastructure": [{"key": "example-host", "connections": []}]
                },
            }
        )

        labels = [metric["label"] for metric in result[0]["machines"][0]["metrics"]]
        self.assertEqual(labels, ["Container CPU", "Container memory"])

    @mock.patch("controller_runtime.provider_http.request_json")
    def test_weather_glance_uses_the_nws_point_contract(self, request):
        request.side_effect = [
            {
                "properties": {
                    "forecastHourly": "https://api.weather.gov/hourly",
                    "relativeLocation": {
                        "properties": {"city": "Chicago", "state": "IL"}
                    },
                }
            },
            {
                "properties": {
                    "periods": [
                        {
                            "name": "This Hour",
                            "shortForecast": "Clear",
                            "temperature": 72,
                            "temperatureUnit": "F",
                            "windDirection": "NW",
                            "windSpeed": "5 mph",
                        }
                    ]
                }
            },
            {"features": []},
        ]

        result = glance.dashboard_glance(
            {
                "panels": ["weather"],
                "targets": {"weather": {"point": "41.0000,-87.0000"}},
            }
        )

        self.assertEqual(result[0]["summary"], "Chicago, IL")
        self.assertEqual(result[0]["metrics"][0]["value"], "Clear")
        self.assertNotIn("Alerts", [metric["label"] for metric in result[0]["metrics"]])
        self.assertIn("User-Agent", request.call_args_list[0].kwargs["headers"])

    @mock.patch(
        "controller_runtime.glance._nws_glance",
        side_effect=ProviderError("No forecast for this point."),
    )
    def test_weather_failure_keeps_the_point_so_the_request_can_complete(self, _nws):
        result = glance.dashboard_glance(
            {
                "panels": ["weather"],
                "targets": {"weather": {"point": "41.0000,-87.0000"}},
            }
        )

        self.assertEqual(result[0]["panel_id"], "weather")
        self.assertEqual(result[0]["point"], "41.0000,-87.0000")
        self.assertEqual(result[0]["status"], "serious")

    @mock.patch.dict(
        "os.environ",
        {"HQ_CONTROLLER_CA_FILE": "/run/secrets/example-ca.pem"},
        clear=True,
    )
    @mock.patch("ssl.create_default_context")
    def test_tls_context_adds_controller_ca_without_replacing_public_roots(
        self, create_default_context
    ):
        context = create_default_context.return_value

        self.assertIs(provider_http.tls_context(), context)

        context.load_verify_locations.assert_called_once_with(
            cafile="/run/secrets/example-ca.pem"
        )

    def test_npm_ui_url_derives_api_base_once(self):
        self.assertEqual(
            npm.api_url("https://npm.example"),
            "https://npm.example/api",
        )
        self.assertEqual(
            npm.api_url("https://npm.example/api"),
            "https://npm.example/api",
        )

    @mock.patch.dict(
        "os.environ",
        {
            "ADGUARD_URL": "https://adguard.example",
            "ADGUARD_USERNAME": "controller",
            "ADGUARD_PASSWORD": "secret",
            "ADGUARD_CONNECTION_REF": "example-adguard",
            "ADGUARD_MANAGES": "1",
        },
        clear=True,
    )
    @mock.patch("controller_runtime.provider_http.request_json")
    def test_adguard_noop_is_idempotent(self, request):
        request.return_value = [{"domain": "hq.example", "answer": "192.0.2.10"}]

        result = self.adguard(
            "reconcile", {"domain": "hq.example", "answer": "192.0.2.10"}
        )

        self.assertFalse(result.changed)
        self.assertEqual(request.call_count, 1)

    @mock.patch.dict(
        "os.environ",
        {
            "ADGUARD_URL": "https://adguard.example",
            "ADGUARD_USERNAME": "controller",
            "ADGUARD_PASSWORD": "secret",
            "ADGUARD_CONNECTION_REF": "example-adguard",
            "ADGUARD_MANAGES": "1",
        },
        clear=True,
    )
    @mock.patch("controller_runtime.provider_http.request_json")
    def test_adguard_reports_a_rewrite_that_is_switched_off(self, request):
        """Present but disabled does not resolve, and Ready would be a lie.

        HQ does not set this field (add and update carry only domain and
        answer) so the honest position is to observe it and say so, rather
        than report a name as healthy while it answers nothing.
        """
        request.return_value = [
            {"domain": "hq.example", "answer": "192.0.2.10", "enabled": False}
        ]

        result = self.adguard(
            "reconcile", {"domain": "hq.example", "answer": "192.0.2.10"}
        )

        self.assertEqual(result.conditions[0]["type"], "Degraded")
        self.assertIs(result.status["enabled"], False)

    @mock.patch.dict(
        "os.environ",
        {
            "ADGUARD_URL": "https://adguard.example",
            "ADGUARD_USERNAME": "controller",
            "ADGUARD_PASSWORD": "secret",
            "ADGUARD_CONNECTION_REF": "example-adguard",
            "ADGUARD_MANAGES": "1",
        },
        clear=True,
    )
    @mock.patch("controller_runtime.provider_http.request_json")
    def test_adguard_delete_removes_the_live_pair(self, request):
        """AdGuard identifies a rewrite by domain *and* answer.

        Deleting with the desired answer would miss a record whose answer has
        drifted, silently leaving it in place.
        """
        request.side_effect = [
            [{"domain": "hq.example", "answer": "192.0.2.99", "enabled": True}],
            None,
        ]

        result = self.adguard(
            "delete", {"domain": "hq.example", "answer": "192.0.2.10"}
        )

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
            "ADGUARD_CONNECTION_REF": "example-adguard",
            "ADGUARD_MANAGES": "1",
        },
        clear=True,
    )
    @mock.patch("controller_runtime.provider_http.request_json")
    def test_adguard_delete_is_idempotent(self, request):
        """A retried delete finding nothing there has done what was asked.

        The queue retries, so a delete that applied and then failed to report
        runs again. Treating an absent record as failure would leave the
        operation stuck forever on work that is already complete.
        """
        request.return_value = []

        result = self.adguard("delete", {"domain": "gone.example", "answer": "x"})

        self.assertFalse(result.changed)
        self.assertEqual(result.conditions[0]["type"], "Ready")
        self.assertEqual(request.call_count, 1)

    @mock.patch.dict(
        "os.environ",
        {
            "ADGUARD_URL": "https://adguard.example",
            "ADGUARD_USERNAME": "controller",
            "ADGUARD_PASSWORD": "secret",
            "ADGUARD_CONNECTION_REF": "example-adguard",
            "ADGUARD_MANAGES": "1",
        },
        clear=True,
    )
    @mock.patch("controller_runtime.provider_http.request_json")
    def test_adguard_delete_plans_without_touching_anything(self, request):
        request.return_value = [
            {"domain": "hq.example", "answer": "192.0.2.10", "enabled": True}
        ]

        result = self.adguard(
            "delete",
            {"domain": "hq.example", "answer": "192.0.2.10"},
            apply=False,
        )

        self.assertTrue(result.changed)
        self.assertEqual(request.call_count, 1)

    @mock.patch.dict(
        "os.environ",
        {
            "ADGUARD_URL": "https://adguard.example",
            "ADGUARD_USERNAME": "controller",
            "ADGUARD_PASSWORD": "secret",
            "ADGUARD_CONNECTION_REF": "example-adguard",
            "ADGUARD_MANAGES": "1",
        },
        clear=True,
    )
    @mock.patch("controller_runtime.provider_http.request_json")
    def test_adguard_update_uses_the_provider_contract(self, request):
        request.side_effect = [
            [{"domain": "hq.example", "answer": "192.0.2.9", "enabled": True}],
            None,
        ]

        result = self.adguard(
            "reconcile", {"domain": "hq.example", "answer": "192.0.2.10"}
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
            "ADGUARD_CONNECTION_REF": "example-adguard",
            "NPM_URL": "https://npm.example",
            "NPM_USERNAME": "controller@example.com",
            "NPM_PASSWORD": "secret-b",
            "NPM_CONNECTION_REF": "example-npm",
            "CLOUDFLARE_DNS_URL": "https://api.cloudflare.com/client/v4",
            "CLOUDFLARE_DNS_API_TOKEN": "secret-c",
            "CLOUDFLARE_DNS_CONNECTION_REF": "cloudflare-dns-example",
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
    @mock.patch("controller_runtime.cloudflare_api.cloudflare_envelope")
    @mock.patch("controller_runtime.commands.run_command")
    @mock.patch("controller_runtime.commands.run_ssh")
    @mock.patch("controller_runtime.provider_http.request_json")
    def test_connection_sweep_probes_every_credential_the_environment_carries(
        self, request, ssh, _run, envelope
    ):
        """Five 1Password items, five probes, and nothing naming any of them.

        The environment is the whole inventory: which connections exist, what
        kind each is, and (for the two SSH transports) that they are
        transports at all, learned from the values their projection produced.
        """

        request.side_effect = _by_url(
            {
                "/control/status": {"dns_addresses": ["0.0.0.0"], "version": "0.107"},
                "/tokens": {"token": "short-lived"},
            }
        )
        envelope.side_effect = _by_url(
            {
                "/user/tokens/verify": {"success": True},
                "/zones": {
                    "success": True,
                    "result": [
                        {"name": "example.com"},
                        {"name": "example.net"},
                    ],
                },
            }
        )

        result = providers.connections()

        by_ref = {item["connection_ref"]: item for item in result}
        self.assertEqual(
            sorted(by_ref),
            [
                "cloudflare-dns-example",
                "example-adguard",
                "example-edge",
                "example-npm",
                "example-shared",
            ],
        )
        self.assertTrue(all(item["ok"] for item in result))
        # Classified by env prefix without a `provider` field anywhere, which
        # is what keeps an existing vault working unchanged.
        self.assertEqual(by_ref["example-adguard"]["provider"], "adguard")
        self.assertEqual(by_ref["example-edge"]["provider"], "ssh")
        # What a credential can act on is a fact only it has. HQ derives its
        # "which domain" menu from exactly this.
        self.assertEqual(
            by_ref["cloudflare-dns-example"]["reaches"],
            ["example.com", "example.net"],
        )
        self.assertEqual(by_ref["example-edge"]["reaches"], ["edge.example"])
        self.assertEqual(ssh.call_count, 2)
        self.assertNotIn("secret-c", json.dumps(result))

    @mock.patch("controller_runtime.cloudflare_api.cloudflare_paged")
    def test_one_sweep_reuses_successful_provider_reads_and_then_forgets_them(
        self, paged
    ):
        paged.return_value = [{"id": "zone-1", "name": "example.test"}]

        with provider_http.provider_snapshot():
            first = cloudflare._cloudflare_zones()
            second = cloudflare._cloudflare_zones()

        third = cloudflare._cloudflare_zones()

        self.assertIs(first, second)
        self.assertEqual(third, first)
        self.assertEqual(paged.call_count, 2)

    @mock.patch.dict(
        "os.environ",
        {
            "CLOUDFLARE_DNS_URL": "https://api.cloudflare.com/client/v4",
            "CLOUDFLARE_DNS_API_TOKEN": "secret-c",
            "CLOUDFLARE_DNS_CONNECTION_REF": "cloudflare-dns-example",
            "HQ_ACME_DIR": "/tmp",
        },
        clear=True,
    )
    @mock.patch("controller_runtime.commands.run_command")
    @mock.patch("controller_runtime.cloudflare_api.cloudflare_envelope")
    def test_one_broken_credential_does_not_hide_the_others(self, request, _run):
        """A failure is that connection's, and the sweep still reports the rest.

        The alternative loses every row the moment one token expires, which is
        precisely when an operator needs to see which of them still works.
        """

        request.side_effect = ProviderError("Token is not valid.")

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
    @mock.patch("urllib.request.urlopen")
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
                    "endpoint": tailnet_api.TAILNET_API,
                    "manages": False,
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
    @mock.patch("urllib.request.urlopen")
    def test_tailscale_probe_failure_is_isolated_and_safe(self, urlopen):
        urlopen.side_effect = urllib.error.HTTPError(
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
    @mock.patch("controller_runtime.provider_http.request_json")
    def test_npm_refuses_https_create_without_certificate(self, request):
        request.side_effect = [{"token": "short-lived"}, []]

        with self.assertRaisesRegex(ProviderError, "needs an issued certificate"):
            providers.PROVIDER_ACTIONS[("npm.proxy_host", "reconcile")](
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
    @mock.patch("controller_runtime.provider_http.request_json")
    def test_npm_delete_targets_the_host_with_that_exact_domain_set(self, request):
        request.side_effect = [
            {"token": "short-lived"},
            [
                {"id": 7, "domain_names": ["other.example"]},
                {"id": 9, "domain_names": ["hq.example"]},
            ],
            None,
        ]

        result = providers.PROVIDER_ACTIONS[("npm.proxy_host", "delete")]({"domain_names": ["hq.example"]})

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
    @mock.patch("controller_runtime.provider_http.request_json")
    def test_npm_reconcile_no_longer_asserts_hsts_off(self, request):
        """The payload replaces the whole object, so an unsent field is not spared.

        HSTS was pinned False here, which meant enabling it in NPM survived
        until the next pass and then quietly switched itself back off.
        """
        request.side_effect = [{"token": "short-lived"}, [], None]

        providers.PROVIDER_ACTIONS[("npm.proxy_host", "reconcile")](
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
            "ADGUARD_CONNECTION_REF": "example-adguard",
            "ADGUARD_MANAGES": "1",
        },
        clear=True,
    )
    @mock.patch("controller_runtime.provider_http.request_json")
    def test_adguard_renames_the_record_it_was_last_seen_holding(self, request):
        """A changed hostname moves the record rather than adding a second one.

        The controller is handed the previously observed state, so it finds
        the record by its old name instead of creating a second one.
        """
        request.side_effect = [
            [{"domain": "old.example", "answer": "192.0.2.10", "enabled": True}],
            None,
        ]

        result = self.adguard(
            "reconcile",
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
            "ADGUARD_CONNECTION_REF": "example-adguard",
            "ADGUARD_MANAGES": "1",
        },
        clear=True,
    )
    @mock.patch("controller_runtime.provider_http.request_json")
    def test_a_name_never_seen_before_is_created_not_renamed(self, request):
        """Only a *changed* name is a rename. A new resource is still a create."""
        request.side_effect = [[], None]

        self.adguard(
            "reconcile",
            {"domain": "new.example", "answer": "192.0.2.10"},
            observed={},
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
    @mock.patch("controller_runtime.provider_http.request_json")
    def test_npm_renames_in_place_by_id(self, request):
        request.side_effect = [
            {"token": "short-lived"},
            [{"id": 4, "domain_names": ["old.example"], "certificate_id": 2}],
            None,
        ]

        providers.PROVIDER_ACTIONS[("npm.proxy_host", "reconcile")](
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
    @mock.patch("controller_runtime.provider_http.request_json")
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

        with self.assertRaisesRegex(ProviderError, "newhost.example"):
            tls.delete_uploaded_certificate(
                {
                    "certificate_name": "newhost",
                    "consumers": [{"kind": "npm", "name": "newhost-npm"}],
                },
                observed={"npm_certificate_ids": {"newhost-npm": 3}},
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
    @mock.patch("controller_runtime.provider_http.request_json")
    def test_an_unbound_certificate_is_removed_by_its_id(self, request):
        """Renamed in NPM, it is still the certificate HQ installed."""

        request.side_effect = [
            {"token": "short-lived"},
            [
                {"id": 3, "nice_name": "Renamed by hand"},
                {"id": 4, "nice_name": "Severino HQ - newhost-npm"},
            ],
            [{"id": 9, "domain_names": ["other.example"], "certificate_id": 7}],
            None,
        ]

        result = tls.delete_uploaded_certificate(
            {
                "certificate_name": "newhost",
                "consumers": [{"kind": "npm", "name": "newhost-npm"}],
            },
            observed={"npm_certificate_id": 3},
        )

        self.assertTrue(result.changed)
        deletes = [
            call for call in request.call_args_list if call.kwargs.get("method") == "DELETE"
        ]
        self.assertEqual(len(deletes), 1)
        self.assertTrue(deletes[0].args[0].endswith("/nginx/certificates/3"))

    @mock.patch.dict(
        "os.environ",
        {
            "NPM_URL": "https://npm.example.test",
            "NPM_USERNAME": "controller@example.com",
            "NPM_PASSWORD": "secret",
        },
        clear=True,
    )
    @mock.patch("controller_runtime.provider_http.request_json")
    def test_a_certificate_matched_only_by_name_is_not_deleted(self, request):
        """A display name anyone can set in NPM is not proof HQ installed it."""

        request.side_effect = [
            {"token": "short-lived"},
            [{"id": 3, "nice_name": "Severino HQ - newhost-npm"}],
        ]

        with self.assertRaisesRegex(ProviderError, "no record of installing"):
            tls.delete_uploaded_certificate(
                {
                    "certificate_name": "newhost",
                    "consumers": [{"kind": "npm", "name": "newhost-npm"}],
                },
                observed={},
            )

        self.assertFalse(
            [c for c in request.call_args_list if c.kwargs.get("method") == "DELETE"]
        )

    def test_removing_from_a_target_hq_cannot_reach_is_refused_whole(self):
        """A partial delete would drop HQ's record of a file still on a host.

        The Caddy transport implements deploy and nothing else, so there is no
        remove to call, and reporting success would forget the only pointer to
        what was left behind.
        """
        with self.assertRaisesRegex(ProviderError, "by hand"):
            tls.delete_uploaded_certificate(
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

        with self.assertRaisesRegex(ProviderError, "no settings to reconcile"):
            providers.execute({"kind": "cloudflare.zone", "spec": {}}, "reconcile")

    @mock.patch("controller_runtime.tls_verification._observe_tls_domain")
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
    def test_one_unreachable_consumer_leaves_the_others_observed(self, observe):
        """A consumer that cannot be reached is reported, not propagated.

        What the other consumers are serving is still a reading, and a
        certificate whose facts are published still publishes them. Reported
        against the consumer it belongs to, so the resource says which part of
        itself this observation covers.
        """

        observe.side_effect = [
            ProviderError(
                "TLS observation failed for health.example: TimeoutError."
            ),
            {
                "domain": "hq.example",
                "not_after": "2026-10-07T00:00:00+00:00",
                "fingerprint_sha256": "current",
                "issuer": "Example CA",
                "sans": ["*.example"],
                "certificate_pem": "-----BEGIN CERTIFICATE-----\ncurrent\n",
            },
        ]

        result = tls_verification.reconcile_tls(
            {
                "renewal_window_days": 30,
                "consumers": [
                    {
                        "kind": "caddy",
                        "name": "caddy",
                        "connection_ref": "example-edge",
                        "verify_domains": ["health.example"],
                    },
                    {"kind": "npm", "name": "npm", "verify_domains": ["hq.example"]},
                ],
            }
        )

        self.assertEqual(result.status["verified_domains"], ["hq.example"])
        self.assertEqual(
            result.status["unreachable_consumers"],
            [
                {
                    "consumer": "caddy",
                    "domain": "health.example",
                    # What was tried, which is resolved from a connection only
                    # the controller holds and is the part that says what to do.
                    "endpoint": "192.0.2.20",
                    "port": "443",
                    "reason": (
                        "TLS observation failed for health.example: TimeoutError."
                    ),
                }
            ],
        )
        self.assertTrue(
            any(item["reason"] == "ConsumerUnreachable" for item in result.conditions)
        )
        self.assertFalse(any(item["type"] == "Ready" for item in result.conditions))

    @mock.patch("controller_runtime.tls_verification._observe_tls_domain")
    @mock.patch.dict("os.environ", {"NPM_URL": "https://npm-origin.example"}, clear=True)
    def test_every_consumer_unreachable_is_still_a_failure(self, observe):
        """Nothing was read, so there is no expiry and nothing to compare."""

        observe.side_effect = ProviderError(
            "TLS observation failed for hq.example: TimeoutError."
        )

        with self.assertRaises(ProviderError) as caught:
            tls_verification.reconcile_tls(
                {
                    "renewal_window_days": 30,
                    "consumers": [
                        {"kind": "npm", "name": "npm", "verify_domains": ["hq.example"]}
                    ],
                }
            )

        self.assertIn("No TLS consumer could be reached", str(caught.exception))

    @mock.patch("controller_runtime.tls_verification._observe_tls_domain")
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

        result = tls_verification.reconcile_tls(
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

        self.assertTrue(any(item["type"] == "Drifted" for item in result.conditions))
        self.assertIn("BEGIN CERTIFICATE", result.status["certificate_pem"])
        self.assertNotIn("PRIVATE KEY", json.dumps(result.status))
        self.assertEqual(
            observe.call_args_list,
            [
                mock.call("hq.example", connect_host="npm-origin.example"),
                mock.call("health.example", connect_host="192.0.2.20"),
            ],
        )

    @mock.patch("controller_runtime.tls_verification._observe_tls_domain")
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
    def test_cpanel_tls_observation_bypasses_public_proxy(self, observe):
        observe.return_value = {
            "domain": "quiz.example.test",
            "not_after": "2026-10-23T00:00:00+00:00",
            "fingerprint_sha256": "current",
            "issuer": "Example CA",
            "sans": ["*.example.test"],
            "certificate_pem": "-----BEGIN CERTIFICATE-----\ncurrent\n",
        }

        tls_verification.reconcile_tls(
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

        observe.assert_called_once_with("quiz.example.test", connect_host="192.0.2.10")

    @mock.patch.dict("os.environ", {"NPM_URL": "https://proxy.example"}, clear=True)
    def test_npm_tls_endpoint_is_derived_from_controller_connection(self):
        self.assertEqual(
            tls_verification._consumer_tls_endpoint({"kind": "npm"}),
            "proxy.example",
        )

    @mock.patch("controller_runtime.provider_http.request_json")
    @mock.patch("control_plane.provider_adapters.npm.token", return_value="token")
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

        found = providers.PROVIDER_INVENTORY["npm.proxy_host"]()[0]["access_policy"]

        self.assertEqual(found["authorization_count"], 1)
        self.assertTrue(found["implicit_deny"])
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

    @mock.patch("controller_runtime.tls.renew_tls")
    def test_renew_plan_never_mutates(self, renew):
        result = providers.execute(
            {"kind": "tls.certificate", "spec": {}}, "renew", apply=False
        )

        self.assertTrue(result.changed)
        renew.assert_not_called()

    @mock.patch("controller_runtime.provider_http.request_json")
    @mock.patch("controller_runtime.provider_http.multipart_request")
    @mock.patch("controller_runtime.npm_certificates.npm_token", return_value="token")
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

        certificate_id, identity = npm_certificates.npm_managed_certificate(
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

    @mock.patch("controller_runtime.provider_http.request_json")
    @mock.patch("controller_runtime.provider_http.multipart_request")
    @mock.patch("controller_runtime.npm_certificates.npm_token", return_value="token")
    @mock.patch.dict(
        "os.environ",
        {"NPM_URL": "https://npm.example.test"},
        clear=True,
    )
    def test_a_renamed_npm_certificate_is_found_by_its_recorded_id(
        self, _token, multipart, request
    ):
        leaf = b"-----BEGIN CERTIFICATE-----\nleaf\n-----END CERTIFICATE-----\n"
        request.side_effect = [
            [
                {"id": 22, "provider": "other", "nice_name": "Renamed by hand"},
                {"id": 30, "provider": "other", "nice_name": "Severino HQ - example-wildcard"},
            ],
            [],
        ]

        certificate_id, _ = npm_certificates.npm_managed_certificate(
            {"name": "example-wildcard", "verify_domains": []},
            ["example.test"],
            leaf,
            b"private-key",
            22,
        )

        self.assertEqual(certificate_id, 22)
        self.assertTrue(multipart.call_args.args[0].endswith("/22/upload"))
        self.assertNotIn(
            "POST", [call.kwargs.get("method") for call in request.call_args_list]
        )

    def test_an_uploaded_certificate_takes_the_observed_status_it_is_dispatched_with(self):
        with self.assertRaisesRegex(ProviderError, "did not supply"):
            providers.execute(
                {
                    "kind": "tls.uploaded_certificate",
                    "spec": {"certificate_name": "example", "consumers": []},
                    "observed": {"npm_certificate_id": 22},
                },
                "reconcile",
                apply=False,
            )

    def test_installed_ids_are_carried_into_a_report_that_installed_nothing(self):
        spec = {"consumers": [{"kind": "npm", "name": "example-npm"}]}
        known = npm_certificates.npm_certificate_ids(spec, {"npm_certificate_id": 22})
        result = npm_certificates.with_npm_certificate_ids(
            ProviderResult(changed=False, status={}, conditions=[], message=""),
            known,
        )

        self.assertEqual(result.status["npm_certificate_ids"], {"example-npm": 22})
        self.assertEqual(result.status["npm_certificate_id"], 22)

    @mock.patch("controller_runtime.provider_http.request_json")
    @mock.patch("controller_runtime.provider_http.multipart_request")
    @mock.patch("controller_runtime.npm_certificates.npm_token", return_value="token")
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

        npm_certificates.npm_managed_certificate(
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

    @mock.patch("controller_runtime.tls_verification.reconcile_tls")
    @mock.patch("controller_runtime.tls._deploy_certificate")
    @mock.patch("controller_runtime.tls_issuance.issue_certificate")
    @mock.patch("controller_runtime.tls_issuance.resumable_lineage", return_value=None)
    @mock.patch("controller_runtime.tls_issuance.validate_certificate")
    @mock.patch("controller_runtime.commands.run_ssh")
    def test_renewal_deploys_and_verifies_every_consumer(
        self, ssh, validate, _resume, issue, deploy, reconcile
    ):
        previous = tls_issuance.certificate_bundle(b"old-cert", b"old-key")
        ssh.return_value = previous
        validate.side_effect = ["old", "new"]
        issue.return_value = (b"new-cert", b"new-key")
        deploy.return_value = {}
        reconcile.return_value = ProviderResult(
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

        result = tls.renew_tls(spec)

        self.assertTrue(result.changed)
        deploy.assert_called_once_with(spec, b"new-cert", b"new-key", {}, None)
        self.assertEqual(result.conditions[0]["reason"], "Renewed")
        self.assertEqual(result.status["expected_fingerprint_sha256"], "new")
        self.assertTrue(
            all(item["matches_expected"] for item in result.status["consumers"])
        )

    @mock.patch("controller_runtime.tls_verification.reconcile_tls")
    @mock.patch(
        "controller_runtime.tls_issuance.validate_certificate", return_value="new"
    )
    @mock.patch("controller_runtime.tls_issuance.lineage")
    def test_reconcile_success_records_explicit_consumer_match_evidence(
        self, lineage, _validate, reconcile
    ):
        lineage.return_value = (b"new-cert", b"new-key")
        reconcile.return_value = ProviderResult(
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

        result = tls.apply_tls_reconcile(
            {"domains": ["example.test"], "consumers": []}
        )

        self.assertFalse(result.changed)
        self.assertEqual(result.status["expected_fingerprint_sha256"], "new")
        self.assertTrue(result.status["consumers"][0]["matches_expected"])

    @mock.patch("controller_runtime.tls_verification.reconcile_tls")
    @mock.patch("controller_runtime.tls._deploy_certificate", return_value={})
    @mock.patch("controller_runtime.tls_issuance.issue_certificate")
    @mock.patch(
        "controller_runtime.tls_issuance.resumable_lineage",
        return_value=(b"pending-cert", b"pending-key"),
    )
    @mock.patch("controller_runtime.tls_issuance.validate_certificate")
    @mock.patch("controller_runtime.commands.run_ssh")
    def test_renewal_resumes_existing_lineage_without_acme_request(
        self, ssh, validate, _resume, issue, deploy, reconcile
    ):
        ssh.return_value = tls_issuance.certificate_bundle(b"old-cert", b"old-key")
        validate.side_effect = ["old", "pending"]
        reconcile.return_value = ProviderResult(
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

        result = tls.renew_tls(spec)

        issue.assert_not_called()
        deploy.assert_called_once_with(spec, b"pending-cert", b"pending-key", {}, None)
        self.assertEqual(result.status["artifact_source"], "existing_lineage")

    @mock.patch("controller_runtime.tls_verification.reconcile_tls")
    @mock.patch("controller_runtime.tls._deploy_certificate")
    @mock.patch("controller_runtime.tls_issuance.issue_certificate")
    @mock.patch("controller_runtime.tls_issuance.resumable_lineage", return_value=None)
    @mock.patch("controller_runtime.tls_issuance.validate_certificate")
    @mock.patch("controller_runtime.commands.run_ssh")
    def test_renewal_rolls_back_previous_artifact_on_deploy_failure(
        self, ssh, validate, _resume, issue, deploy, _reconcile
    ):
        ssh.return_value = tls_issuance.certificate_bundle(b"old-cert", b"old-key")
        validate.side_effect = ["old", "new"]
        issue.return_value = (b"new-cert", b"new-key")
        deploy.side_effect = [ProviderError("failed"), None]
        spec = {
            "domains": ["example.test"],
            "consumers": [
                {"kind": "caddy", "connection_ref": "example-edge"},
            ],
        }

        with self.assertRaisesRegex(
            ProviderError, "failed.*[Rr]ollback succeeded"
        ):
            tls.renew_tls(spec)

        self.assertEqual(deploy.call_count, 2)
        deploy.assert_called_with(spec, b"old-cert", b"old-key", {}, None)


class CarriedConnectionTests(TestCase):
    """An SSH probe is a real login, so HQ may say one is not needed yet."""

    @mock.patch("controller_runtime.providers._probe_ssh")
    @mock.patch("controller_runtime.connection_env.connection_provider", return_value="")
    @mock.patch(
        "controller_runtime.connection_env.ssh_connection_refs",
        return_value=["shared-hosting", "edge"],
    )
    @mock.patch(
        "controller_runtime.connection_env.connection_prefixes",
        return_value={"shared-hosting": "SHARED_HOSTING", "edge": "EDGE"},
    )
    def test_a_carried_connection_is_reported_without_a_login(
        self, _prefixes, _refs, _provider, probe
    ):
        probe.return_value = {"detail": "ok", "reaches": ["192.0.2.30"]}

        found = {
            item["connection_ref"]: item
            for item in providers.connections(carry=frozenset({"shared-hosting"}))
        }

        probe.assert_called_once_with("edge")
        self.assertTrue(found["shared-hosting"]["carried"])
        self.assertFalse(found["shared-hosting"]["probed"])
        self.assertNotIn("carried", found["edge"])
        self.assertTrue(found["edge"]["probed"])


class MachineNameTests(TestCase):
    """Portainer calls its own environment "local", which is nobody's hostname.

    Everything ties to a machine by name, so filing containers under "local"
    splits one machine into two: the credential and the services on one row, the
    containers on another.
    """

    @mock.patch.dict("os.environ", {"HQ_CONTROLLER_ID": ""})
    def test_the_local_socket_is_the_machine_this_runs_on(self):
        record = portainer_readings.container_record(
            {"Names": ["/app"], "State": "running"}, controller_id(), ""
        )

        self.assertEqual(record["host"], os.uname().nodename)
        self.assertNotEqual(record["host"], "local")

    @mock.patch.dict("os.environ", {"HQ_CONTROLLER_ID": "a-named-host"})
    def test_the_environment_names_it_when_it_says_so(self):
        self.assertEqual(controller_id(), "a-named-host")


class CapabilityMatchesTheHandlersTests(TestCase):
    """What a provider claims the controller may do, against what it can do.

    Half of this is a choice and half is a fact. Whether an action should run
    unprompted, and why it is refused when it is, are decisions somebody makes.
    Whether the controller is *able* to run it is not: there is either code
    behind it or there is not, and that is checkable rather than assertable.

    So the declaration stays where the provider is and this is what stops it
    drifting from the handlers it describes.
    """

    def _real(self):
        """The handlers that act, minus the refusals generated from the policy."""

        # Told apart by where the function was defined, not by what it is
        # called: a refusal generated by `_refuses` carries that in its
        # qualname, and a rename of the inner function cannot quietly turn
        # every locked action into a handler that looks real.
        return {
            identity
            for identity, handler in providers.PROVIDER_ACTIONS.items()
            if not getattr(handler, "__qualname__", "").startswith("_refuses")
        }

    def _declared(self, mode):
        from control_plane.providers import controller_capability_registry

        return {
            (kind, action)
            for kind, capability in controller_capability_registry().capabilities.items()
            for action, policy in capability.actions.items()
            if policy.mode == mode
        }

    def test_nothing_claims_to_apply_without_code_behind_it(self):
        empty = sorted(self._declared("apply") - self._real())

        self.assertEqual(empty, [], f"declared apply, no handler: {empty}")

    def test_nothing_is_locked_while_quietly_having_a_handler(self):
        contradicted = sorted(self._declared("locked") & self._real())

        self.assertEqual(
            contradicted, [], f"declared locked, acts anyway: {contradicted}"
        )

    def test_every_handler_is_something_a_provider_declared(self):
        """A handler nothing declares is unreachable: `execute` is gated on
        the policy, so the code would sit there looking implemented."""

        undeclared = sorted(
            self._real() - self._declared("apply") - self._declared("locked")
        )

        self.assertEqual(undeclared, [], f"handler nothing declares: {undeclared}")


class CollectorFailureIsReportedTests(TestCase):
    """A collector that cannot read raises, and never reports emptiness.

    ``inventory`` already turns a raising collector into an unreachable kind
    that keeps what it last held and carries the reason. A collector that
    catches its own error and returns ``[]`` defeats that: the sweep succeeds,
    the kind reads as reachable and empty, and the declaration simply stops
    being confirmed with nothing anywhere saying why.

    ``tailscale.policy`` did this for a week. Every failure on that path
    raises with its own message and all of them were thrown away, which is why
    the cause had to be guessed at rather than read.
    """

    def test_a_policy_read_that_is_refused_is_reported_not_swallowed(self):
        refused = ProviderError(
            "Tailscale refused the policy read (403). The credential needs the "
            "policy_file scope."
        )
        with (
            mock.patch.dict("os.environ", {"TAILSCALE_CONNECTION_REF": "example-tailnet"}),
            mock.patch.object(tailnet_api, "tailnet_token", return_value="t"),
            mock.patch.object(tailnet_policy, "_tailnet_policy", side_effect=refused),
        ):
            swept = providers.inventory()

        entry = swept["tailscale.policy"]
        self.assertFalse(
            entry["ok"],
            "a refused policy read reported a successful sweep, so the tailnet "
            "reads as having no policy at all",
        )
        self.assertIn("policy_file", entry["error"])

A_SERVICE_ACCOUNT_TOKEN = "ops_an-example-service-account-token"

A_PASSWORD_MANAGER_CONNECTION = {
    "A_PASSWORD_MANAGER_CONNECTION_REF": "a-password-manager",
    "A_PASSWORD_MANAGER_PROVIDER": "onepassword",
    "A_PASSWORD_MANAGER_API_TOKEN": A_SERVICE_ACCOUNT_TOKEN,
}


AN_OWNERS_OWN_TAG = "an-operators-own-tag"

# The two files the item carries, named as the adapter names them.
ATTACHMENT_LABELS = onepassword.ATTACHMENT_LABELS

# Stand-ins for the certificate and its key. Not real material, and not shaped
# like any: what the tests assert is where these bytes travel and what is left
# behind, never that they parse.
A_CHAIN = b"-----BEGIN CERTIFICATE-----\nan example chain\n-----END CERTIFICATE-----\n"
A_PRIVATE_KEY = (
    b"-----BEGIN PRIVATE KEY-----\nan example key\n-----END PRIVATE KEY-----\n"
)


def _an_item(fields=None, tags=(AN_OWNERS_OWN_TAG,), files=ATTACHMENT_LABELS) -> bytes:
    """One item as `op` prints it, with whatever fields and tags are asked for.

    A field is given as its value, stored as the type this adapter publishes it
    as, or as an explicit ``(type, value)`` pair for the case that matters most:
    an item written before the expiry was typed, whose value reads identically
    and is not a date.

    A date given as ``2026-10-23`` comes back the way `op` prints one: the epoch
    seconds of midnight on that day.
    """

    stored = []
    for label, value in (fields or {}).items():
        if isinstance(value, tuple):
            field_type, value = value
        else:
            field_type = onepassword._STORED_AS[onepassword.PUBLISHED_FIELDS[label]]
            if field_type == "DATE":
                midnight = datetime.datetime.combine(
                    datetime.date.fromisoformat(value), datetime.time()
                )
                value = str(int(midnight.timestamp()))
        stored.append({"id": label, "type": field_type, "label": label, "value": value})
    return json.dumps(
        {
            "id": "an-example-item-id",
            "title": "An Example Certificate Item",
            "tags": list(tags),
            "fields": [
                {
                    "id": "notesPlain",
                    "label": "notesPlain",
                    "type": "STRING",
                    "value": "an operator's own note",
                },
                *stored,
            ],
            # A label without a dot is stored as the file's own name, which is
            # what an `op://<vault>/<item>/<label>` reference resolves against.
            "files": [{"id": f"{label}-id", "name": label} for label in files],
        }
    ).encode()


class TheDeclarationSaysWhereAndTheCodeSaysWhatTests(TestCase):
    """What a certificate publishes is fixed here; only its address is declared.

    This is the whole reason the path is safe to give a credential that can
    write. A delivery target is operator input held in HQ's database, so if it
    could name a field, a label or a value, an edited declaration would decide
    what a write-capable token wrote, and "publish the facts about a
    certificate" would quietly become "write whatever this row says".
    """

    def _publish(
        self,
        current,
        *,
        spec=None,
        status=None,
        tags=(AN_OWNERS_OWN_TAG,),
        files=ATTACHMENT_LABELS,
        lineage=True,
    ):
        """Publish once against an item already holding `current` and `tags`.

        Returns the argument lists `op` was invoked with, which is where the
        claim actually lands: the fields written are the assignments in argv.

        A lineage is laid down on disk unless asked not to, because the material
        the publisher uploads is read from the same place the deploy path
        installs from. The paths it stages are captured as they are given to
        `op`, so a test can assert both that they existed then and that nothing
        was left behind afterwards.
        """

        calls = []
        staged = []

        def run(command, *, input_bytes=None, step="command", env=None):
            calls.append(command)
            # Recorded as `op` sees them: the file has to exist at this moment,
            # and its mode is only meaningful while it does.
            for argument in command:
                if "[file]=" not in argument:
                    continue
                label, path = argument.split("[file]=", 1)
                staged.append(
                    {
                        "label": label,
                        "path": Path(path),
                        "content": Path(path).read_bytes(),
                        "mode": stat.S_IMODE(Path(path).stat().st_mode),
                        "directory_mode": stat.S_IMODE(
                            Path(path).parent.stat().st_mode
                        ),
                    }
                )
            return _an_item(current, tags, files) if command[2] == "get" else b""

        environment = dict(A_PASSWORD_MANAGER_CONNECTION)
        if lineage:
            acme = Path(self.enterContext(tempfile.TemporaryDirectory()))
            live = acme / "config" / "live" / A_RECORDED_CERTIFICATE["certificate_name"]
            live.mkdir(parents=True)
            live.joinpath("fullchain.pem").write_bytes(A_CHAIN)
            live.joinpath("privkey.pem").write_bytes(A_PRIVATE_KEY)
            environment["HQ_ACME_DIR"] = str(acme)
        self.staged = staged

        with (
            mock.patch.object(commands, "run_command", side_effect=run),
            mock.patch.dict("os.environ", environment, clear=True),
        ):
            result = tls._publish_tls_facts(
                spec or A_RECORDED_CERTIFICATE,
                ProviderResult(
                    changed=False,
                    status=dict(status or AN_OBSERVATION),
                    conditions=[
                        provider_http.condition("Ready", True, "Verified", "Current.")
                    ],
                    message="TLS consumers observed.",
                ),
                apply=True,
            )
        return calls, result

    _ASSIGNMENT = re.compile(
        r"^(?P<label>[^\[]+)\[(?P<type>\w+)\]=(?P<value>.*)$", re.S
    )

    def _assignments(self, calls):
        """Every `Label[type]=value` argument `op item edit` was given."""

        return [
            match
            for command in calls
            if command[2] == "edit"
            for argument in command
            if (match := self._ASSIGNMENT.match(argument))
        ]

    def _written(self, calls):
        """The fields written, which is not every assignment in argv.

        An attachment is an assignment too (`label[file]=path`), so counting
        those as fields would have this claim drift the moment the adapter
        started carrying the certificate as well as its description.
        """

        return {
            m["label"]: m["value"]
            for m in self._assignments(calls)
            if m["type"] != "file"
        }

    def _types(self, calls):
        return {
            m["label"]: m["type"]
            for m in self._assignments(calls)
            if m["type"] != "file"
        }

    def _tags(self, calls):
        edit = next(command for command in calls if command[2] == "edit")
        return sorted(edit[edit.index("--tags") + 1].split(","))

    def test_the_fields_written_are_the_ones_this_adapter_names(self):
        written = self._written(self._publish({})[0])

        self.assertEqual(
            sorted(written),
            sorted(onepassword.PUBLISHED_FIELDS),
        )
        self.assertEqual(written["Issued by"], "An Example Authority")
        self.assertEqual(written["Expires"], "2027-01-14")
        self.assertEqual(written["Fingerprint (SHA-256)"], "aa:bb:cc")
        self.assertEqual(written["Installed on"], "an-example-certificate-npm")
        self.assertEqual(written["Covers"], "shop.example.test, *.shop.example.test")

    def test_a_declaration_carrying_extra_keys_writes_no_extra_field(self):
        """Refused at the boundary, and ignored here as well.

        The resolved shape forbids an unknown key, so this cannot arrive through
        HQ. It is asserted at the adapter too, because the guarantee that makes
        the credential safe should not rest on one validator being in the path.
        """

        spec = {
            **A_RECORDED_CERTIFICATE,
            "publish_to": [
                {
                    **A_RECORDED_CERTIFICATE["publish_to"][0],
                    "fields": {"Recovery Phrase": "take this too"},
                    "note": "and overwrite this",
                }
            ],
        }

        written = self._written(self._publish({}, spec=spec)[0])

        self.assertEqual(sorted(written), sorted(onepassword.PUBLISHED_FIELDS))
        self.assertNotIn("Recovery Phrase", written)

    def test_nothing_on_the_item_is_deleted_and_the_note_is_left_alone(self):
        """Assignments only: `op` upserts each named field and touches no other."""

        calls, _ = self._publish({})
        edit = next(command for command in calls if command[2] == "edit")

        self.assertNotIn("notesPlain", " ".join(edit))
        for absent in ("--dry-run", "delete", "--generate-password"):
            self.assertNotIn(absent, edit)

    def test_writing_the_same_facts_twice_writes_nothing_the_second_time(self):
        """Observe, compare, write only if different.

        A certificate is observed on every controller pass and changes twice a
        year, so a publisher that wrote unconditionally would spend a write per
        pass and put a version in the item's history for every one of them.
        """

        first_calls, _ = self._publish({})
        already = self._written(first_calls)
        settled = self._tags(first_calls)

        second_calls, result = self._publish(already, tags=settled)

        self.assertEqual(
            [command[2] for command in second_calls],
            ["get"],
            "the item already said this, so nothing should have been written",
        )
        self.assertEqual(result.status["published_facts"][0]["written"], False)
        self.assertEqual(result.status["published_facts"][0]["fields"], [])
        self.assertTrue(result.status["published_facts"][0]["tagged"])

    def test_only_the_facts_that_moved_are_written(self):
        first_calls, _ = self._publish({})
        renewed = {**self._written(first_calls), "Expires": "2026-11-02"}

        written = self._written(self._publish(renewed, tags=self._tags(first_calls))[0])

        self.assertEqual(sorted(written), ["Expires"])

    def test_the_expiry_is_published_as_a_date_and_not_as_text(self):
        """So the credential store itself knows when this goes stale.

        A date 1Password can render, sort and be asked about is a warning that
        does not depend on HQ sweeping.
        """

        types = self._types(self._publish({})[0])

        self.assertEqual(types["Expires"], "date")
        self.assertEqual(
            {label for label, kind in types.items() if kind == "text"},
            set(onepassword.PUBLISHED_FIELDS) - {"Expires"},
        )

    def test_the_expiry_1password_stores_is_read_back_as_the_date_it_was_given(self):
        """`op` takes `2026-10-23`, stores epoch seconds, and prints those back.

        Read as printed, the expiry never equals the value that produced it, and
        an unchanged certificate would rewrite it on every sweep.
        """

        published = self._written(self._publish({})[0])
        expiry = published["Expires"]
        stored = json.loads(_an_item(published))["fields"]
        printed = next(f["value"] for f in stored if f["label"] == "Expires")

        self.assertNotEqual(printed, expiry, "op prints epoch seconds, not the date")
        self.assertEqual(
            onepassword._as_written({"type": "DATE", "value": printed}),
            ("DATE", expiry),
        )

    def test_a_field_whose_type_is_wrong_is_rewritten_though_it_reads_the_same(self):
        """An item written before the expiry was typed holds the same characters.

        Compared on the value alone it would agree forever, and the date would
        stay a string that merely looks like one.
        """

        as_text = {
            **self._written(self._publish({})[0]),
            "Expires": ("STRING", "2027-01-14"),
        }

        written = self._written(
            self._publish(as_text, tags=(onepassword.MANAGED_TAG,))[0]
        )

        self.assertEqual(sorted(written), ["Expires"])

    def test_the_certificate_itself_is_written_when_the_fingerprint_moves(self):
        """Facts without material would be the worse half of this.

        On renewal the expiry and fingerprint here would move while the files
        stayed at whatever was last put there by hand, and the item would state
        a date above material contradicting it.
        """

        self._publish({})

        self.assertEqual(
            [item["label"] for item in self.staged], list(ATTACHMENT_LABELS)
        )
        self.assertEqual(
            [item["content"] for item in self.staged], [A_CHAIN, A_PRIVATE_KEY]
        )

    def test_nothing_is_uploaded_when_the_item_already_holds_this_certificate(self):
        """The published fingerprint is the leaf's own content identity.

        So "is what is attached this certificate" is answered by a field already
        being written, and the key is never downloaded to compare, nor read off
        disk on a pass that changes nothing.
        """

        first_calls, _ = self._publish({})
        self.staged.clear()

        calls, result = self._publish(
            self._written(first_calls), tags=self._tags(first_calls)
        )

        self.assertEqual([command[2] for command in calls], ["get"])
        self.assertEqual(self.staged, [])
        self.assertEqual(result.status["published_facts"][0]["material"], "current")

    def test_a_missing_attachment_is_replaced_though_every_fact_agrees(self):
        """An item can carry the right facts and not the files they describe.

        Someone deleted one, or a write landed half way. Comparing only the
        facts would call that current forever.
        """

        first_calls, _ = self._publish({})
        self.staged.clear()

        self._publish(
            self._written(first_calls),
            tags=self._tags(first_calls),
            files=("fullchain",),
        )

        self.assertEqual(
            [item["label"] for item in self.staged], list(ATTACHMENT_LABELS)
        )

    def test_the_key_is_staged_privately_and_nothing_is_left_behind(self):
        """It exists as a file for exactly as long as `op` needs to read one."""

        self._publish({})

        for item in self.staged:
            self.assertEqual(item["mode"], 0o600)
            self.assertEqual(item["directory_mode"], 0o700)
            self.assertFalse(
                item["path"].exists(),
                "the staging directory should be gone once the write returns",
            )

    def test_the_staging_directory_is_removed_even_when_the_write_fails(self):
        """A private key must not outlive a failure."""

        staged = []

        def run(command, *, input_bytes=None, step="command", env=None):
            if command[2] == "get":
                return _an_item({}, (AN_OWNERS_OWN_TAG,), ())
            staged.extend(
                Path(argument.split("[file]=", 1)[1])
                for argument in command
                if "[file]=" in argument
            )
            raise ProviderError("1Password refused the write.")

        acme = Path(self.enterContext(tempfile.TemporaryDirectory()))
        live = acme / "config" / "live" / A_RECORDED_CERTIFICATE["certificate_name"]
        live.mkdir(parents=True)
        live.joinpath("fullchain.pem").write_bytes(A_CHAIN)
        live.joinpath("privkey.pem").write_bytes(A_PRIVATE_KEY)

        with (
            mock.patch.object(commands, "run_command", side_effect=run),
            mock.patch.dict(
                "os.environ",
                {**A_PASSWORD_MANAGER_CONNECTION, "HQ_ACME_DIR": str(acme)},
                clear=True,
            ),
        ):
            result = tls._publish_tls_facts(
                A_RECORDED_CERTIFICATE,
                ProviderResult(
                    changed=False,
                    status=dict(AN_OBSERVATION),
                    conditions=[
                        provider_http.condition("Ready", True, "Verified", "Current.")
                    ],
                    message="TLS consumers observed.",
                ),
                apply=True,
            )

        self.assertTrue(staged, "the write should have been reached")
        for path in staged:
            self.assertFalse(path.exists())
            self.assertFalse(path.parent.exists())
        self.assertTrue(
            result.conditions[0]["status"], "the certificate is still Ready"
        )

    def test_no_key_material_reaches_the_status_or_the_step(self):
        """What is reported is a word, never a path or anything read from one."""

        calls, result = self._publish({})
        published = result.status["published_facts"][0]

        self.assertEqual(published["material"], "written")
        reported = json.dumps(published) + result.message
        for secret in (A_PRIVATE_KEY, A_CHAIN):
            self.assertNotIn(secret.decode(), reported)
        for command in calls:
            self.assertNotIn(A_PRIVATE_KEY.decode(), " ".join(command))

    def test_the_item_is_tagged_as_hqs(self):
        """The query worth having is the inverse: untagged here means unmaintained."""

        self.assertIn(onepassword.MANAGED_TAG, self._tags(self._publish({})[0]))

    def test_a_tag_the_owner_added_by_hand_is_never_dropped(self):
        """`--tags` replaces the whole list rather than adding to it.

        So the union has to be written. Anything less and an automated pass
        quietly strips whatever a person had put on the item.
        """

        tags = self._tags(self._publish({}, tags=(AN_OWNERS_OWN_TAG, "and-another"))[0])

        self.assertEqual(
            tags, sorted([AN_OWNERS_OWN_TAG, "and-another", onepassword.MANAGED_TAG])
        )

    def test_an_untagged_item_is_written_even_when_every_fact_agrees(self):
        """The tag is part of the fixed set, so it settles like the rest."""

        first_calls, _ = self._publish({})
        already = self._written(first_calls)

        calls, result = self._publish(already, tags=())

        self.assertEqual([command[2] for command in calls], ["get", "edit"])
        self.assertEqual(self._written(calls), {})
        self.assertEqual(self._tags(calls), [onepassword.MANAGED_TAG])
        self.assertTrue(result.status["published_facts"][0]["tagged"])

    def test_a_dry_run_records_nothing(self):
        """Being asked what a reconcile would do is not permission to write."""

        calls = []

        def run(command, *, input_bytes=None, step="command", env=None):
            calls.append(command)
            return b""

        with (
            mock.patch.object(commands, "run_command", side_effect=run),
            mock.patch.object(tls_verification, "reconcile_tls") as observe,
        ):
            observe.return_value = ProviderResult(
                changed=False, status=dict(AN_OBSERVATION), conditions=[], message=""
            )
            tls._tls_reconcile(A_RECORDED_CERTIFICATE, apply=False)

        self.assertEqual(calls, [])

    def test_a_certificate_with_no_agreed_fingerprint_publishes_nothing(self):
        """Four true facts beside a stale fifth is the record that misleads.

        Consumers serving different certificates is what a drift condition is
        for. There is no single fingerprint to publish, so none of it is
        published and the item keeps saying what it last knew.
        """

        disputed = {
            "issuer": "An Example Authority",
            "not_after": "2027-01-14T09:30:00+00:00",
            "consumers": [
                {"consumer": "one", "fingerprint_sha256": "aa:bb:cc"},
                {"consumer": "two", "fingerprint_sha256": "dd:ee:ff"},
            ],
        }

        calls, result = self._publish({}, status=disputed)

        self.assertEqual(calls, [])
        self.assertIn(
            "no single fingerprint", result.status["published_facts"][0]["detail"]
        )


class TheServiceAccountTokenGoesNowhereButTheEnvironmentTests(TestCase):
    """The one credential here can write, so where it can appear is the point.

    Not in an argument list, which every process on the machine can read; not in
    the status HQ stores, which is served to API clients; not in a log line, and
    not in the message an operator is shown when it fails.
    """

    def _publish_against(self, run):
        with (
            mock.patch.object(commands, "run_command", side_effect=run),
            mock.patch.dict("os.environ", A_PASSWORD_MANAGER_CONNECTION, clear=True),
        ):
            return tls._publish_tls_facts(
                A_RECORDED_CERTIFICATE,
                ProviderResult(
                    changed=False,
                    status=dict(AN_OBSERVATION),
                    conditions=[],
                    message="TLS consumers observed.",
                ),
                apply=True,
            )

    def test_the_token_is_passed_in_the_environment_and_not_in_argv(self):
        seen = []

        def run(command, *, input_bytes=None, step="command", env=None):
            seen.append((command, env, step))
            return _an_item() if command[2] == "get" else b""

        self._publish_against(run)

        for command, env, step in seen:
            self.assertNotIn(A_SERVICE_ACCOUNT_TOKEN, " ".join(command))
            self.assertNotIn(A_SERVICE_ACCOUNT_TOKEN, step)
            self.assertEqual(env, {"OP_SERVICE_ACCOUNT_TOKEN": A_SERVICE_ACCOUNT_TOKEN})

    def test_the_token_never_reaches_the_status_hq_stores(self):
        result = self._publish_against(
            lambda command, input_bytes=None, step="command", env=None: (
                _an_item() if command[2] == "get" else b""
            )
        )

        self.assertNotIn(A_SERVICE_ACCOUNT_TOKEN, json.dumps(result.status))
        self.assertNotIn(A_SERVICE_ACCOUNT_TOKEN, result.message)

    def test_a_failure_reports_the_step_and_not_the_credential(self):
        def run(command, *, input_bytes=None, step="command", env=None):
            raise ProviderError(f"{step} failed.")

        result = self._publish_against(run)

        self.assertNotIn(A_SERVICE_ACCOUNT_TOKEN, json.dumps(result.status))
        self.assertIn("1Password read", result.status["published_facts"][0]["detail"])

    def test_a_tool_that_echoes_its_own_credential_is_struck_from_the_log(self):
        """The only place that knows the value is the one that passed it in.

        A failing command's stderr is logged for the operator, and `op` is not
        expected to print its token, but "not expected to" is not a control,
        and this is the last point at which the value is still known.
        """

        with (
            mock.patch.object(subprocess, "run") as run,
            self.assertLogs("severino.controller", level="WARNING") as logged,
        ):
            run.return_value = mock.Mock(
                returncode=1,
                stdout=b"",
                stderr=f"could not use {A_SERVICE_ACCOUNT_TOKEN}".encode(),
            )
            with self.assertRaises(ProviderError):
                commands.run_command(
                    ["op", "item", "get", "an-item"],
                    step="1Password read for a certificate",
                    env={"OP_SERVICE_ACCOUNT_TOKEN": A_SERVICE_ACCOUNT_TOKEN},
                )

        recorded = json.dumps(
            [record.__dict__ for record in logged.records], default=str
        )
        self.assertNotIn(A_SERVICE_ACCOUNT_TOKEN, recorded)
        self.assertIn("[redacted]", recorded)

    def test_the_command_runs_with_the_controllers_own_environment_too(self):
        """Replacing it outright would leave `op` without a PATH or a HOME."""

        with (
            mock.patch.object(subprocess, "run") as run,
            mock.patch.dict("os.environ", {"PATH": "/an/example/path"}, clear=True),
        ):
            run.return_value = mock.Mock(returncode=0, stdout=b"", stderr=b"")
            commands.run_command(
                ["op", "whoami"], env={"OP_SERVICE_ACCOUNT_TOKEN": "t"}, step="a step"
            )

        passed = run.call_args.kwargs["env"]
        self.assertEqual(passed["PATH"], "/an/example/path")
        self.assertEqual(passed["OP_SERVICE_ACCOUNT_TOKEN"], "t")

    def test_cloud_writer_cannot_inherit_connect_authentication(self):
        with (
            mock.patch.object(subprocess, "run") as run,
            mock.patch.dict("os.environ", {
                "OP_CONNECT_HOST": "http://127.0.0.1:8080",
                "OP_CONNECT_TOKEN": "example-connect-token",
            }),
        ):
            run.return_value = mock.Mock(returncode=0, stdout=b"", stderr=b"")
            commands.run_command(
                ["op", "item", "edit"],
                env={"OP_SERVICE_ACCOUNT_TOKEN": "t"},
                step="a step",
            )
        self.assertNotIn("OP_CONNECT_HOST", run.call_args.kwargs["env"])
        self.assertNotIn("OP_CONNECT_TOKEN", run.call_args.kwargs["env"])

    def test_the_connection_probe_proves_the_credential_without_naming_a_vault(self):
        """A vault is not a machine, and `reaches` everywhere else means one."""

        with (
            mock.patch.object(commands, "run_command") as run,
            mock.patch.dict("os.environ", A_PASSWORD_MANAGER_CONNECTION, clear=True),
        ):
            run.return_value = json.dumps(
                [{"id": "an-example-vault-id", "name": "An Example Vault"}]
            ).encode()
            probed = providers._probe_onepassword("a-password-manager")

        self.assertEqual(probed["reaches"], [])
        self.assertIn("1 vaults", probed["detail"])
        self.assertNotIn("An Example Vault", json.dumps(probed))
        self.assertNotIn(A_SERVICE_ACCOUNT_TOKEN, " ".join(run.call_args.args[0]))

    def test_a_vault_list_that_cannot_be_read_is_a_failed_probe(self):
        with (
            mock.patch.object(commands, "run_command", return_value=b"not json"),
            mock.patch.dict("os.environ", A_PASSWORD_MANAGER_CONNECTION, clear=True),
        ):
            with self.assertRaisesRegex(ProviderError, "could not read"):
                providers._probe_onepassword("a-password-manager")

    def test_an_item_that_cannot_be_read_is_a_failed_publication(self):
        """Reported, not treated as an item holding nothing.

        Read as empty, every field would look changed and be rewritten on every
        pass: the unreadable case turning into the noisiest one.
        """

        result = self._publish_against(
            lambda command, input_bytes=None, step="command", env=None: b"not json"
        )

        self.assertIn("could not read", result.status["published_facts"][0]["detail"])


class HostPerimeterTests(TestCase):
    """What an edge relies on to stay shut, and whether it is."""

    def _reading(self, *, answers=(), containers=None, unit="active"):
        payload = json.dumps(
            {
                "record": "perimeter",
                "firewall_unit": unit,
                "public_addresses": "203.0.113.5",
                "read_at": "2026-09-20T00:00:00Z",
            }
        ).encode()
        with (
            mock.patch.object(
                connection_env, "connection_refs_for_role", return_value=("an-edge",)
            ),
            mock.patch.object(commands, "run_ssh", return_value=payload),
            mock.patch.object(
                connection_env,
                "ssh_target",
                return_value={"host": "100.64.0.9", "port": 7722, "user": "u", "host_key": "k"},
            ),
            mock.patch.object(
                portainer,
                "list_portainer_containers",
                return_value=containers
                if containers is not None
                else [{"host": "an-edge", "ports": [80, 443, 9001]}],
            ),
            mock.patch.object(
                host_readings,
                "_answers_from_here",
                side_effect=lambda address, port, **_: port in answers,
            ),
        ):
            (reading,) = host_readings.list_host_perimeter()
        return reading

    def test_a_shut_perimeter_reports_nothing_answering(self):
        reading = self._reading()

        self.assertEqual(reading["answered_publicly"], [])
        self.assertEqual(reading["ports_checked"], [22, 80, 443, 7722, 9001])
        self.assertEqual(reading["firewall_unit"], "active")

    def test_the_ssh_ports_are_always_tried(self):
        """SSH answers on the tailnet only, so a public answer is the finding."""

        reading = self._reading(containers=[], answers=(7722,))

        self.assertEqual(reading["ports_checked"], [22, 7722])
        self.assertEqual(reading["answered_publicly"], [7722])

    def test_containers_are_matched_by_the_address_both_names_share(self):
        """A Portainer environment and an SSH item name one machine differently."""

        reading = self._reading(
            containers=[
                {"host": "edge-environment", "host_address": "100.64.0.9", "ports": [9001]},
                {"host": "edge-environment", "host_address": "203.0.113.5", "ports": [8443]},
                {"host": "elsewhere", "host_address": "100.64.0.20", "ports": [3000]},
            ],
        )

        self.assertEqual(reading["ports_checked"], [22, 7722, 8443, 9001])

    def test_a_port_that_answers_publicly_is_named(self):
        """The invariant failing, which has no symptom anywhere else."""

        reading = self._reading(answers=(9001,))

        self.assertEqual(reading["answered_publicly"], [9001])

    def test_which_ports_are_checked_is_never_written_down(self):
        """Derived from the containers, so a new one is covered by existing."""

        reading = self._reading(
            containers=[{"host": "an-edge", "ports": [25565]}],
        )

        self.assertEqual(reading["ports_checked"], [22, 7722, 25565])

    def test_containers_on_another_machine_are_not_checked_here(self):
        reading = self._reading(
            containers=[{"host": "somewhere-else", "ports": [80]}],
        )

        self.assertEqual(reading["ports_checked"], [22, 7722])

    def test_a_dead_unit_is_reported_rather_than_assumed(self):
        """Enabled and dead is the case with no symptom until it matters."""

        self.assertEqual(self._reading(unit="inactive")["firewall_unit"], "inactive")


class CPanelForcedCommandTests(TestCase):
    """The script HQ's key runs on shared hosting, against a stand-in `uapi`.

    It runs where HQ's own code does not (an interpreter the host chose) so
    its contract is pinned here rather than discovered on a renewal.
    """

    SCRIPT = Path(__file__).resolve().parent.parent / "deploy/targets/severino-hq-cpanel-controller"

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.installs = self.directory / "installs"
        uapi = self.directory / "uapi"
        uapi.write_text(
            "#!/bin/sh\n"
            'case "$*" in\n'
            '  *"DomainInfo domains_data"*) cat "$FIXTURES/domains.json" ;;\n'
            '  *"SSL list_certs"*) echo \'{"result":{"status":1,"errors":null,"data":[]}}\' ;;\n'
            '  *"SSL install_ssl"*) payload=$(cat); echo "$payload" >> "$FIXTURES/installs";\n'
            '     case "$payload" in *broken.example.test*)\n'
            '       echo \'{"result":{"status":0,"errors":["The certificate does not match."]}}\' ;;\n'
            '     *) echo \'{"result":{"status":1,"errors":null}}\' ;; esac ;;\n'
            "  *) exit 2 ;;\n"
            "esac\n"
        )
        uapi.chmod(0o755)
        (self.directory / "domains.json").write_text(json.dumps({"result": {"status": 1, "data": {
            "main_domain": {"domain": "example.test", "servername": "example.test",
                            "serveralias": "www.example.test parked.example"},
            "sub_domains": [{"domain": "shop.example.test", "servername": "shop.example.test",
                             "serveralias": "www.shop.example.test"},
                            {"domain": "broken.example.test", "servername": "broken.example.test",
                             "serveralias": ""}],
            "addon_domains": [],
        }}}))

    def run_script(self, operation, payload=None):
        return subprocess.run(
            [sys.executable, str(self.SCRIPT)],
            input=json.dumps(payload) if payload is not None else "",
            capture_output=True,
            text=True,
            timeout=30,
            env={
                "PATH": f"{self.directory}:{os.environ['PATH']}",
                "FIXTURES": str(self.directory),
                "SSH_ORIGINAL_COMMAND": operation,
            },
        )

    def installed_on(self):
        if not self.installs.exists():
            return []
        return [json.loads(line)["domain"] for line in self.installs.read_text().splitlines()]

    def test_it_parses_as_the_python_shared_hosting_ships(self):
        import ast

        ast.parse(self.SCRIPT.read_text(), feature_version=(3, 6))

    def test_sites_lists_each_site_with_every_name_it_serves(self):
        result = self.run_script("sites")

        self.assertEqual(result.returncode, 0, result.stderr)
        sites = json.loads(result.stdout)["sites"]
        self.assertEqual(sites["example.test"], ["example.test", "parked.example", "www.example.test"])
        self.assertEqual(sites["shop.example.test"], ["shop.example.test", "www.shop.example.test"])

    def test_deploy_installs_on_each_named_site(self):
        result = self.run_script(
            "deploy", {"sites": ["example.test", "shop.example.test"], "cert": "c", "key": "k"}
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.installed_on(), ["example.test", "shop.example.test"])

    def test_a_site_not_on_the_account_is_refused_before_anything_is_installed(self):
        result = self.run_script(
            "deploy", {"sites": ["example.test", "elsewhere.example"], "cert": "c", "key": "k"}
        )

        self.assertEqual(result.returncode, 126)
        self.assertIn("elsewhere.example", result.stderr)
        self.assertEqual(self.installed_on(), [])

    def test_a_failed_install_fails_the_deploy_and_names_the_site(self):
        result = self.run_script(
            "deploy", {"sites": ["example.test", "broken.example.test"], "cert": "c", "key": "k"}
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("broken.example.test", result.stderr)
        self.assertIn("does not match", result.stderr)

    def test_bad_input_and_unknown_operations_are_refused(self):
        for operation, payload, code in (
            ("deploy", {"cert": "c", "key": "k"}, 1),
            ("deploy", {"sites": [], "cert": "c", "key": "k"}, 1),
            ("deploy", {"sites": ["example.test"]}, 1),
            ("rm -rf ~", None, 126),
            ("", None, 126),
        ):
            with self.subTest(operation=operation, payload=payload):
                self.assertEqual(self.run_script(operation, payload).returncode, code)
        self.assertEqual(self.installed_on(), [])

    def test_the_single_site_form_still_works_for_older_controllers(self):
        result = self.run_script("deploy:example.test", {"cert": "c", "key": "k"})

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.installed_on(), ["example.test"])


class ConnectionManagesTests(TestCase):
    """A connection observes unless its item says it manages."""

    def manages(self, env):
        from unittest import mock

        with mock.patch.dict("os.environ", env, clear=True):
            return connection_env.connection_manages("example-dns")

    def test_absent_is_observe(self):
        self.assertFalse(self.manages({"CLOUDFLARE_DNS_CONNECTION_REF": "example-dns"}))

    def test_a_declared_yes_manages(self):
        for value in ("1", "true", "yes", "TRUE"):
            with self.subTest(value=value):
                self.assertTrue(
                    self.manages(
                        {
                            "CLOUDFLARE_DNS_CONNECTION_REF": "example-dns",
                            "CLOUDFLARE_DNS_MANAGES": value,
                        }
                    )
                )

    def test_anything_else_observes(self):
        for value in ("0", "no", "", "maybe"):
            with self.subTest(value=value):
                self.assertFalse(
                    self.manages(
                        {
                            "CLOUDFLARE_DNS_CONNECTION_REF": "example-dns",
                            "CLOUDFLARE_DNS_MANAGES": value,
                        }
                    )
                )

    def test_the_connection_sweep_reports_it(self):
        from unittest import mock

        env = {
            "EXAMPLE_CONNECTION_REF": "example-thing",
            "EXAMPLE_MANAGES": "1",
        }
        with mock.patch.dict("os.environ", env, clear=True):
            (found,) = providers.connections()

        self.assertIs(found["manages"], True)

    def test_every_projection_can_carry_it_optionally(self):
        registry = json.loads(
            (
                Path(__file__).resolve().parent.parent
                / "config"
                / "controller-connections.json"
            ).read_text()
        )

        for name, projection in registry["projections"].items():
            with self.subTest(projection=name):
                self.assertEqual(
                    projection["MANAGES"],
                    {"source": "field", "label": "manages", "optional": True},
                )


class NotConnectedTests(TestCase):
    """A kind nothing here can read is reported as not connected, not as empty."""

    def test_a_kind_with_no_connection_is_not_read(self):
        from unittest import mock

        lister = mock.Mock(return_value=[{"x": 1}])
        with (
            mock.patch.dict("os.environ", {}, clear=True),
            mock.patch.dict(providers.PROVIDER_INVENTORY, {"portainer.container": lister}, clear=True),
        ):
            found = providers.inventory()

        self.assertEqual(found["portainer.container"], {"ok": True, "records": [], "connected": False})
        lister.assert_not_called()

    def test_a_kind_with_its_connection_is_read(self):
        from unittest import mock

        lister = mock.Mock(return_value=[])
        env = {"PORTAINER_CONNECTION_REF": "example-portainer"}
        with (
            mock.patch.dict("os.environ", env, clear=True),
            mock.patch.dict(providers.PROVIDER_INVENTORY, {"portainer.container": lister}, clear=True),
        ):
            found = providers.inventory()

        self.assertEqual(found["portainer.container"], {"ok": True, "records": []})
        lister.assert_called_once()

    def test_a_local_tailnet_reading_counts_as_a_source(self):
        from unittest import mock

        with mock.patch.object(tailscale, "TAILNET_STATUS", "/run/example/status.json"):
            self.assertTrue(providers._has_source("tailscale.device", set()))
        with mock.patch.object(tailscale, "TAILNET_STATUS", ""):
            self.assertFalse(providers._has_source("tailscale.device", set()))

    def test_connections_shaped_as_a_deployment_names_them_are_sources(self):
        """An SSH transport under any prefix, and a provider named on the item."""

        from unittest import mock

        env = {
            "EDGE_CONNECTION_REF": "example-edge",
            "EDGE_HOST": "192.0.2.10",
            "EDGE_USER": "example",
            "EDGE_ROLE": "caddy",
            "TAILNET_CONNECTION_REF": "example-tailnet",
            "TAILNET_PROVIDER": "tailscale",
            "ONEPASSWORD_CERTIFICATES_CONNECTION_REF": "example-vault",
            "ONEPASSWORD_CERTIFICATES_PROVIDER": "onepassword",
        }
        with mock.patch.dict("os.environ", env, clear=True):
            refs = set(connection_env.ssh_connection_refs())
            connected = {
                connection_env.effective_provider(ref, refs)
                for ref in connection_env.connection_prefixes()
            }
            self.assertEqual(connected, {"ssh", "tailscale", "onepassword"})
            for kind in ("host.perimeter", "caddy.route", "tailscale.device",
                         "tailscale.policy", "tailscale.dns"):
                self.assertTrue(providers._has_source(kind, connected), kind)

    def test_a_host_reading_needs_its_mounted_source(self):
        from unittest import mock

        with mock.patch.object(host_readings, "HOST_FIREWALL", "/run/example/firewall.json"):
            self.assertTrue(providers._has_source("host.firewall", set()))
        with mock.patch.object(host_readings, "HOST_FIREWALL", ""):
            self.assertFalse(providers._has_source("host.firewall", set()))

    def test_a_host_reading_with_nothing_mounted_is_not_connected(self):
        from unittest import mock

        lister = mock.Mock(return_value=[])
        with (
            mock.patch.object(host_readings, "HOST_FIREWALL", ""),
            mock.patch.dict(providers.PROVIDER_INVENTORY, {"host.firewall": lister}, clear=True),
        ):
            found = providers.inventory()

        self.assertEqual(found["host.firewall"], {"ok": True, "records": [], "connected": False})
        lister.assert_not_called()

    def test_a_kind_off_the_connection_providers_without_a_local_source_is_not_read(self):
        from unittest import mock

        from control_plane import observations

        spec = mock.Mock(provider="example-local")
        with mock.patch.object(
            observations,
            "OBSERVATIONS",
            {**observations.OBSERVATIONS, "example_reading": spec},
        ):
            self.assertFalse(providers._has_source("example_reading", set()))


class SignInRedirectTests(TestCase):
    """An API address behind a sign-in proxy answers a login page, and HQ says so."""

    API = "https://npm.example.com/api/nginx/proxy-hosts"

    def call(self, answer):
        seen = []

        def urlopen(request, timeout=None, context=None):
            seen.append(request)
            return answer

        with mock.patch.object(urllib.request, "urlopen", urlopen):
            try:
                return provider_runtime.RUNTIME.request(
                    self.API, headers={"Authorization": "Bearer example-token"}
                ), seen
            except ProviderError as exc:
                return exc, seen

    def test_a_redirect_to_a_sign_in_page_names_its_host_only(self):
        found, _ = self.call(
            _Page(
                b"<!doctype html><title>Sign in</title>",
                landed="https://sso.example.com/oauth2/start?client_id=example-id&state=abc",
                content_type="text/html; charset=utf-8",
            )
        )

        self.assertIsInstance(found, ProviderError)
        self.assertEqual(
            str(found),
            "The address answered with a sign-in page at sso.example.com, not the API. "
            "Use the provider's direct API address.",
        )
        self.assertEqual(found.failure, ADDRESS_FAILURE)
        self.assertEqual(found.refusal, "")
        self.assertNotIn("example-id", str(found))
        self.assertNotIn("state", str(found))

    def test_a_web_page_where_json_is_expected_says_so(self):
        found, _ = self.call(
            _Page(b"  <html></html>", landed=self.API, content_type="application/json")
        )

        self.assertIsInstance(found, ProviderError)
        self.assertIn("answered with a web page, not the API", str(found))
        self.assertEqual(found.failure, ADDRESS_FAILURE)

    def test_a_redirect_to_another_host_is_refused_even_with_json(self):
        found, _ = self.call(_Page(b"[]", landed="https://other.example.com/api"))

        self.assertIsInstance(found, ProviderError)
        self.assertIn("redirected to other.example.com", str(found))
        self.assertEqual(found.failure, ADDRESS_FAILURE)

    def test_no_answer_is_the_network_and_a_401_refuses_the_credential(self):
        def raising(error):
            def urlopen(request, timeout=None, context=None):
                raise error

            return urlopen

        cases = (
            (urllib.error.URLError("no route"), NETWORK_FAILURE, ""),
            (TimeoutError(), NETWORK_FAILURE, ""),
            (
                urllib.error.HTTPError(self.API, 401, "Unauthorized", {}, None),
                CREDENTIAL_REFUSAL,
                CREDENTIAL_REFUSAL,
            ),
            (
                urllib.error.HTTPError(self.API, 500, "Server error", {}, None),
                "",
                "",
            ),
        )
        for error, failure, refusal in cases:
            with self.subTest(error=type(error).__name__):
                with mock.patch.object(
                    urllib.request, "urlopen", raising(error)
                ), self.assertRaises(ProviderError) as raised:
                    provider_runtime.RUNTIME.request(self.API)
                self.assertEqual(raised.exception.failure, failure)
                self.assertEqual(raised.exception.refusal, refusal)

    def test_a_probe_reports_why_it_failed(self):
        def probe(connection_ref):
            raise ProviderError(
                "The address answered with a web page, not the API.",
                failure=ADDRESS_FAILURE,
            )

        found = providers._probed(probe, "example-npm")

        self.assertEqual(
            found,
            {
                "ok": False,
                "detail": "The address answered with a web page, not the API.",
                "failure": ADDRESS_FAILURE,
            },
        )

    def test_json_from_the_address_asked_is_returned(self):
        found, _ = self.call(_Page(b'[{"id": 1}]', landed=self.API))

        self.assertEqual(found, [{"id": 1}])

    def test_credentials_are_not_sent_on_to_a_redirect(self):
        _, seen = self.call(_Page(b"[]", landed=self.API))

        (request,) = seen
        self.assertEqual(request.unredirected_hdrs.get("Authorization"), "Bearer example-token")
        self.assertNotIn("Authorization", request.headers)
        self.assertEqual(request.headers.get("Accept"), "application/json")


class CredentialedTransportTests(TestCase):
    """Every provider request leaves through one opener with one redirect rule."""

    TOKEN = {"Authorization": "Bearer example-token"}

    def test_a_redirect_to_another_origin_is_refused_before_it_is_sent(self):
        elsewhere = _Recorder(lambda _: (200, {"Content-Type": "application/json"}, b"{}"))
        with elsewhere:
            origin = _Recorder(
                lambda _: (302, {"Location": f"{elsewhere.url}/landing"}, b"")
            )
            with origin:
                with self.assertRaises(ProviderError) as raised:
                    provider_http.open_url(f"{origin.url}/api", headers=self.TOKEN)

        self.assertIn("redirected to 127.0.0.1, not the API", str(raised.exception))
        self.assertEqual(len(origin.seen), 1)
        self.assertEqual(origin.seen[0][2].get("Authorization"), "Bearer example-token")
        self.assertEqual(elsewhere.seen, [])

    def test_a_same_origin_redirect_of_a_read_is_followed_without_credentials(self):
        def answer(handler):
            if handler.path == "/api":
                return 302, {"Location": "/api/"}, b""
            return 200, {"Content-Type": "application/json"}, b'{"ok": true}'

        with _Recorder(answer) as origin:
            with provider_http.open_url(f"{origin.url}/api", headers=self.TOKEN) as response:
                self.assertEqual(json.loads(response.read()), {"ok": True})

        (first, second) = origin.seen
        self.assertEqual(first[2].get("Authorization"), "Bearer example-token")
        self.assertNotIn("Authorization", second[2])

    def test_a_redirected_write_is_not_replayed_as_a_read(self):
        def answer(handler):
            if handler.path == "/graphql":
                return 302, {"Location": "/elsewhere"}, b""
            return 200, {"Content-Type": "application/json"}, b"{}"

        with _Recorder(answer) as origin:
            with self.assertRaises(ProviderError) as raised:
                provider_http.open_url(
                    f"{origin.url}/graphql",
                    method="POST",
                    data=b"{}",
                    headers={**self.TOKEN, "Content-Type": "application/json"},
                )

        self.assertIn("redirected a POST request", str(raised.exception))
        self.assertEqual([seen[1] for seen in origin.seen], ["/graphql"])

    def test_a_tailnet_call_verifies_tls_with_the_controller_context(self):
        seen = []

        def urlopen(request, timeout=None, context=None):
            seen.append(context)
            return _Page(b'{"devices": []}', landed=request.full_url)

        with mock.patch.object(urllib.request, "urlopen", urlopen):
            tailnet_api.tailnet_api_devices("example-token")

        (context,) = seen
        self.assertIsInstance(context, ssl.SSLContext)
        self.assertEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)

    def test_no_provider_request_is_built_outside_the_one_opener(self):
        """An architecture test: ``urllib.request`` is reached only through ``open_url``."""

        import ast

        root = Path(__file__).resolve().parent.parent
        sources = [
            *sorted((root / "controller_runtime").glob("*.py")),
            *sorted((root / "control_plane" / "provider_adapters").glob("*.py")),
        ]
        allowed = {"_provider_request", "open_url"}
        offenders = []
        for path in sources:
            if path.name.startswith("test"):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for function in ast.walk(tree):
                if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if function.name in allowed:
                    continue
                for node in ast.walk(function):
                    if (
                        isinstance(node, ast.Attribute)
                        and node.attr in {"Request", "urlopen", "build_opener"}
                        and ast.unparse(node.value).endswith("request")
                    ):
                        offenders.append(f"{path.name}:{node.lineno} in {function.name}")
        self.assertEqual(offenders, [])


class MissingSettingTests(TestCase):
    """A missing setting is reported without naming the variable that holds it."""

    @mock.patch.dict("os.environ", {}, clear=True)
    def test_a_missing_credential_names_no_variable(self):
        with (
            self.assertLogs("severino.controller", "WARNING") as logged,
            self.assertRaises(ProviderError) as raised,
        ):
            cloudflare_api.cloudflare_token("")

        self.assertEqual(str(raised.exception), provider_http.NOT_CONFIGURED)
        self.assertNotIn("API_TOKEN", str(raised.exception))
        self.assertIn("CLOUDFLARE_DNS_API_TOKEN", "\n".join(logged.output))

    @mock.patch.dict(
        "os.environ",
        {
            "EXAMPLE_EDGE_CONNECTION_REF": "example-edge",
            "EXAMPLE_EDGE_HOST": "edge.example",
            "EXAMPLE_EDGE_PORT": "not-a-port",
        },
        clear=True,
    )
    def test_a_bad_port_names_the_connection_not_the_variable(self):
        with self.assertRaises(ProviderError) as raised:
            connection_env.ssh_target("example-edge")

        self.assertNotIn("EXAMPLE_EDGE", str(raised.exception))
        self.assertIn("example-edge", str(raised.exception))


class ManagesGateTests(TestCase):
    """A write goes only through a connection whose item declares manages."""

    OBSERVING = {"EDGE_CONNECTION_REF": "edge", "EDGE_HOST": "edge.example"}
    ROUTE = {
        "kind": "caddy.route",
        "spec": {
            "connection_ref": "edge",
            "certificate_directory": "/certs",
            "routes": [{"domain": "a.example.com", "upstream": "app:80"}],
        },
    }

    @mock.patch("controller_runtime.commands.run_ssh")
    def test_a_write_through_an_observing_connection_is_refused(self, ssh):
        with (
            mock.patch.dict("os.environ", self.OBSERVING, clear=True),
            self.assertRaisesRegex(ProviderError, "edge only observes"),
        ):
            providers.execute(self.ROUTE, "reconcile")

        ssh.assert_not_called()

    @mock.patch("controller_runtime.commands.run_ssh")
    def test_a_plan_through_an_observing_connection_still_runs(self, ssh):
        with mock.patch.dict("os.environ", self.OBSERVING, clear=True):
            result = providers.execute(self.ROUTE, "reconcile", apply=False)

        self.assertEqual(result.status, {"routes": 1})

    @mock.patch("controller_runtime.provider_http.request_json")
    def test_a_resource_naming_no_connection_needs_every_one_of_its_kind_to_manage(
        self, request
    ):
        env = {
            "ADGUARD_CONNECTION_REF": "example-adguard",
            "ADGUARD_MANAGES": "1",
            "ADGUARD_HOME_CONNECTION_REF": "example-adguard-home",
            "ADGUARD_HOME_PROVIDER": "adguard",
        }
        resource = {
            "kind": "adguard.rewrite",
            "spec": {"domain": "a.example.com", "answer": "192.0.2.1"},
        }
        with (
            mock.patch.dict("os.environ", env, clear=True),
            self.assertRaisesRegex(
                ProviderError, "example-adguard-home only observes"
            ),
        ):
            providers.execute(resource, "delete")

        request.assert_not_called()

    def test_a_write_with_no_connection_at_all_is_refused(self):
        with (
            mock.patch.dict("os.environ", {}, clear=True),
            self.assertRaisesRegex(ProviderError, "No connection"),
        ):
            providers.execute(
                {"kind": "adguard.rewrite", "spec": {"domain": "a.example.com"}},
                "reconcile",
            )

    def test_a_locked_action_keeps_its_own_reason(self):
        from control_plane.providers import controller_capability_registry

        reason = (
            controller_capability_registry()
            .capabilities["cloudflare.zone"]
            .actions["reconcile"]
            .reason
        )
        with (
            mock.patch.dict("os.environ", {}, clear=True),
            self.assertRaises(ProviderError) as raised,
        ):
            providers.execute({"kind": "cloudflare.zone", "spec": {}}, "reconcile")

        self.assertEqual(str(raised.exception), reason)
