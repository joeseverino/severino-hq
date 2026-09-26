"""Cloudflare account readings: paging, the schema allowlist, and refusals."""

from __future__ import annotations

import json
from unittest import mock

from django.test import TestCase

from application.inventory import record_inventory
from application.security import cli_principal
from control_plane.models import ProviderInventory
from control_plane.observations import OBSERVATIONS

from . import providers

ACCOUNT = "0" * 32
SECRET = "never-stored-secret"


def _page(items, page=1, total_pages=1):
    return {
        "success": True,
        "result": items,
        "result_info": {"page": page, "total_pages": total_pages},
    }


class _Cloudflare:
    """A fake account surface: path prefix to response, recording every call."""

    def __init__(self, routes):
        self.routes = routes
        self.calls: list[str] = []

    def __call__(self, path, connection_ref=""):
        self.calls.append(path)
        if path.startswith("/accounts?"):
            return _page([{"id": ACCOUNT, "name": "Example"}])
        for prefix, response in self.routes:
            if path.split("?", 1)[0] == prefix.split("?", 1)[0] and all(
                part in path for part in prefix.split("?", 1)[1:]
            ):
                if isinstance(response, BaseException):
                    raise response
                if callable(response):
                    return response(path)
                return response
        raise AssertionError(f"unexpected request {path}")

    def paths(self, prefix):
        return [call for call in self.calls if call.startswith(prefix)]


def _stored(kind, records):
    record_inventory({kind: {"ok": True, "records": records}}, principal=cli_principal())
    return ProviderInventory.objects.get(kind=kind)


def _paged(pages):
    """Answer successive requests for one list with successive pages."""

    def answer(path):
        page = int(path.split("page=")[-1].split("&")[0])
        return _page(pages[page - 1], page, len(pages))

    return answer


@mock.patch.dict("os.environ", {}, clear=True)
class CloudflareReadingTests(TestCase):
    def _read(self, kind, routes):
        fake = _Cloudflare(routes)
        with mock.patch.object(providers, "_cloudflare_api_request", fake):
            with providers.provider_snapshot():
                records = providers.OBSERVATION_READERS[kind]()
        return records, fake

    # Pages -------------------------------------------------------------

    def test_pages_projects_are_read_to_the_last_page_and_keep_only_safe_fields(self):
        project = {
            "name": "site",
            "subdomain": "site.pages.dev",
            "domains": ["www.example.com"],
            "production_branch": "main",
            "deployment_configs": {"production": {"env_vars": {"KEY": {"value": SECRET}}}},
            "build_config": {"build_command": SECRET},
            "canonical_deployment": {
                "id": "deployment-1",
                "created_on": "2026-09-01T00:00:00Z",
                "env_vars": {"KEY": {"value": SECRET}},
                "deployment_trigger": {"metadata": {"commit_hash": "abcdef0123456789"}},
            },
        }
        pages = [[project] * 10, [dict(project, name="other")]]
        records, fake = self._read(
            "cloudflare.pages_project",
            [(f"/accounts/{ACCOUNT}/pages/projects", _paged(pages))],
        )

        self.assertEqual(len(records), 11)
        self.assertEqual(len(fake.paths(f"/accounts/{ACCOUNT}/pages/projects")), 2)
        stored = _stored("cloudflare.pages_project", records)
        self.assertNotIn(SECRET, json.dumps(stored.records))
        self.assertNotIn("deployment_configs", stored.records[0])
        self.assertEqual(stored.records[0]["deployment_commit"], "abcdef0")
        spec = OBSERVATIONS["cloudflare.pages_project"]
        self.assertEqual(
            spec.hostnames(stored.records[0]), ("www.example.com", "site.pages.dev")
        )

    def test_a_refused_pages_read_raises(self):
        refused = providers.ProviderError("Cloudflare refused the request: 403")
        with self.assertRaises(providers.ProviderError):
            self._read(
                "cloudflare.pages_project",
                [(f"/accounts/{ACCOUNT}/pages/projects", refused)],
            )

    # D1 ----------------------------------------------------------------

    def test_d1_databases_are_paged_and_each_is_read_for_its_size(self):
        databases = [
            {"name": f"db-{index}", "uuid": f"uuid-{index}", "version": "production",
             "created_at": "2026-01-01T00:00:00Z"}
            for index in range(3)
        ]
        records, fake = self._read(
            "cloudflare.d1_database",
            [
                (f"/accounts/{ACCOUNT}/d1/database?per_page",
                 _paged([databases[:2], databases[2:]])),
                (f"/accounts/{ACCOUNT}/d1/database/uuid-1",
                 providers.ProviderError("Cloudflare refused the request: 403")),
                (f"/accounts/{ACCOUNT}/d1/database/uuid-0",
                 {"success": True, "result": {"file_size": 4096, "token": SECRET}}),
                (f"/accounts/{ACCOUNT}/d1/database/uuid-2",
                 {"success": True, "result": {"file_size": 8192}}),
            ],
        )

        self.assertEqual([record["uuid"] for record in records], ["uuid-0", "uuid-1", "uuid-2"])
        self.assertEqual(len(fake.paths(f"/accounts/{ACCOUNT}/d1/database?")), 2)
        stored = _stored("cloudflare.d1_database", records)
        self.assertEqual(stored.records[0]["file_size"], 4096)
        self.assertEqual(stored.records[0]["account_id"], ACCOUNT)
        self.assertIn("403", stored.records[1]["unread"])
        self.assertNotIn(SECRET, json.dumps(stored.records))

    def test_a_refused_d1_read_raises(self):
        with self.assertRaises(providers.ProviderError):
            self._read(
                "cloudflare.d1_database",
                [(f"/accounts/{ACCOUNT}/d1/database",
                  providers.ProviderError("Cloudflare refused the request: 403"))],
            )

    # Access --------------------------------------------------------------

    APP = {
        "id": "app-1",
        "name": "Admin",
        "type": "self_hosted",
        "domain": "admin.example.com/panel",
        "destinations": [
            {"type": "public", "uri": "admin.example.com/panel"},
            {"type": "private", "hostname": "admin.example.net", "cidr": "10.0.0.0/24"},
            {"type": "private", "cidr": "10.1.0.0/24"},
        ],
        "session_duration": "24h",
        "saas_app": {"client_secret": SECRET, "client_id": SECRET},
        "scim_config": {"authentication": {"client_secret": SECRET}},
        "policies": [
            {
                "id": "policy-1",
                "name": "Operators",
                "include": [
                    {"email": {"email": "someone@example.com"}},
                    {"service_token": {"token_id": "token-1"}},
                ],
            }
        ],
    }

    def test_access_apps_keep_hostnames_and_policy_names_only(self):
        records, fake = self._read(
            "cloudflare.access_app",
            [(f"/accounts/{ACCOUNT}/access/apps",
              _paged([[self.APP], [dict(self.APP, id="app-2", saas_app=None)]]))],
        )

        self.assertEqual(len(fake.paths(f"/accounts/{ACCOUNT}/access/apps")), 2)
        stored = _stored("cloudflare.access_app", records)
        dumped = json.dumps(stored.records)
        self.assertNotIn(SECRET, dumped)
        self.assertNotIn("someone@example.com", dumped)
        self.assertNotIn("saas_app", stored.records[0])
        self.assertNotIn("scim_config", stored.records[0])
        self.assertEqual(
            stored.records[0]["destinations"], ["admin.example.com", "admin.example.net"]
        )
        self.assertEqual(
            stored.records[0]["policies"], [{"id": "policy-1", "name": "Operators"}]
        )
        self.assertEqual(
            OBSERVATIONS["cloudflare.access_app"].hostnames(stored.records[0]),
            ("admin.example.com", "admin.example.net"),
        )

    def test_a_refused_access_app_read_raises(self):
        with self.assertRaises(providers.ProviderError):
            self._read(
                "cloudflare.access_app",
                [(f"/accounts/{ACCOUNT}/access/apps",
                  providers.ProviderError("Cloudflare refused the request: 403"))],
            )

    def test_service_tokens_name_the_apps_that_admit_them_and_never_the_client_id(self):
        tokens = [
            {"id": "token-1", "name": "ci", "client_id": SECRET, "client_secret": SECRET,
             "expires_at": "2027-01-01T00:00:00Z", "created_at": "2026-01-01T00:00:00Z"},
            {"id": "token-2", "name": "unused", "client_id": SECRET},
        ]
        records, fake = self._read(
            "cloudflare.access_service_token",
            [
                (f"/accounts/{ACCOUNT}/access/service_tokens",
                 _paged([tokens[:1], tokens[1:]])),
                (f"/accounts/{ACCOUNT}/access/apps", _page([self.APP])),
            ],
        )

        self.assertEqual(len(fake.paths(f"/accounts/{ACCOUNT}/access/service_tokens")), 2)
        stored = _stored("cloudflare.access_service_token", records)
        self.assertNotIn(SECRET, json.dumps(stored.records))
        self.assertNotIn("client_id", stored.records[0])
        self.assertEqual(stored.records[0]["apps"], [{"id": "app-1", "name": "Admin"}])
        self.assertEqual(stored.records[1]["apps"], [])

    def test_service_tokens_say_when_the_apps_could_not_be_read(self):
        records, _fake = self._read(
            "cloudflare.access_service_token",
            [
                (f"/accounts/{ACCOUNT}/access/service_tokens",
                 _page([{"id": "token-1", "name": "ci"}])),
                (f"/accounts/{ACCOUNT}/access/apps",
                 providers.ProviderError("Cloudflare refused the request: 403")),
            ],
        )

        self.assertIn("apps:", records[0]["unread"])
        self.assertNotIn("apps", records[0])

    def test_a_refused_service_token_read_raises(self):
        with self.assertRaises(providers.ProviderError):
            self._read(
                "cloudflare.access_service_token",
                [(f"/accounts/{ACCOUNT}/access/service_tokens",
                  providers.ProviderError("Cloudflare refused the request: 403"))],
            )

    # Tunnels -------------------------------------------------------------

    def test_tunnels_read_ingress_and_connections_from_their_own_endpoints(self):
        tunnel = {
            "id": "tunnel-1",
            "name": "example-host",
            "status": "healthy",
            "created_at": "2026-01-01T00:00:00Z",
            "conns_active_at": "2026-01-01T00:00:01Z",
            "credentials_file": {"TunnelSecret": SECRET},
            "connections": [{"origin_ip": "198.51.100.99"}],
        }
        base = f"/accounts/{ACCOUNT}/cfd_tunnel/tunnel-1"
        records, fake = self._read(
            "cloudflare.tunnel",
            [
                (f"/accounts/{ACCOUNT}/cfd_tunnel?is_deleted=false", _page([tunnel])),
                (f"{base}/configurations", {"success": True, "result": {
                    "source": "cloudflare",
                    "config": {"ingress": [
                        {"hostname": "app.example.com", "service": "http://10.0.0.5:8080",
                         "originRequest": {"access": {"audTag": [SECRET]}}},
                        {"service": "http_status:404"},
                    ]},
                }}),
                (f"{base}/connections", {"success": True, "result": [
                    {"id": "client-1", "version": "2026.9.0", "conns": [
                        {"colo_name": "ord01", "origin_ip": "192.0.2.10"},
                    ]},
                ]}),
            ],
        )

        self.assertIn("is_deleted=false", fake.paths(f"/accounts/{ACCOUNT}/cfd_tunnel?")[0])
        stored = _stored("cloudflare.tunnel", records)
        record = stored.records[0]
        self.assertNotIn(SECRET, json.dumps(stored.records))
        self.assertEqual(
            record["ingress"],
            [{"hostname": "app.example.com", "service": "http://10.0.0.5:8080"}],
        )
        self.assertEqual(
            record["connections"],
            [{"version": "2026.9.0", "colo": "ord01", "origin_ip": "192.0.2.10"}],
        )
        spec = OBSERVATIONS["cloudflare.tunnel"]
        self.assertEqual(spec.hostnames(record), ("app.example.com",))
        self.assertEqual(spec.addresses(record), ("192.0.2.10",))

    def test_a_tunnel_whose_connections_are_refused_says_so(self):
        base = f"/accounts/{ACCOUNT}/cfd_tunnel/tunnel-1"
        records, _fake = self._read(
            "cloudflare.tunnel",
            [
                (f"/accounts/{ACCOUNT}/cfd_tunnel", _page([{"id": "tunnel-1", "name": "t"}])),
                (f"{base}/configurations", {"success": True, "result": {"source": "local"}}),
                (f"{base}/connections",
                 providers.ProviderError("Cloudflare refused the request: 403")),
            ],
        )

        self.assertEqual(records[0]["config_source"], "local")
        self.assertIn("connections:", records[0]["unread"])
        self.assertNotIn("connections", records[0])

    def test_a_refused_tunnel_read_raises(self):
        with self.assertRaises(providers.ProviderError):
            self._read(
                "cloudflare.tunnel",
                [(f"/accounts/{ACCOUNT}/cfd_tunnel",
                  providers.ProviderError("Cloudflare refused the request: 403"))],
            )

    # Edge certificates ------------------------------------------------------

    def _zones(self):
        return ("/zones", _page([
            {"id": "zone-1", "name": "example.com"},
            {"id": "zone-2", "name": "example.net"},
        ]))

    def test_certificate_packs_are_paged_per_zone(self):
        pack = {
            "id": "pack-1",
            "type": "universal",
            "hosts": ["example.com", "*.example.com"],
            "status": "active",
            "certificate_authority": "lets_encrypt",
            "certificates": [
                {"expires_on": "2026-12-01T00:00:00Z", "private_key": SECRET},
                {"expires_on": "2026-11-01T00:00:00Z"},
            ],
        }
        records, fake = self._read(
            "cloudflare.edge_certificate",
            [
                self._zones(),
                ("/zones/zone-1/ssl/certificate_packs?status=all",
                 _paged([[pack] * 50, [dict(pack, id="pack-2")]])),
                ("/zones/zone-2/ssl/certificate_packs?status=all", _page([])),
            ],
        )

        self.assertEqual(len(fake.paths("/zones/zone-1/ssl/certificate_packs")), 2)
        self.assertEqual(len(fake.paths("/zones?")), 1)
        stored = _stored("cloudflare.edge_certificate", records)
        self.assertEqual(len(stored.records), 51)
        self.assertNotIn(SECRET, json.dumps(stored.records))
        self.assertEqual(stored.records[0]["expires_on"], "2026-11-01T00:00:00Z")
        self.assertEqual(
            OBSERVATIONS["cloudflare.edge_certificate"].hostnames(stored.records[0]),
            ("example.com", "*.example.com"),
        )

    def test_a_zone_whose_packs_are_refused_carries_unread(self):
        records, _fake = self._read(
            "cloudflare.edge_certificate",
            [
                self._zones(),
                ("/zones/zone-1/ssl/certificate_packs",
                 providers.ProviderError("Cloudflare refused the request: 403")),
                ("/zones/zone-2/ssl/certificate_packs", _page([])),
            ],
        )

        self.assertEqual(records, [{"connection_ref": "", "account_id": "", "zone": "example.com",
                                    "unread": "Cloudflare refused the request: 403"}])

    def test_every_zone_refused_raises(self):
        refused = providers.ProviderError("Cloudflare refused the request: 403")
        with self.assertRaises(providers.ProviderError):
            self._read(
                "cloudflare.edge_certificate",
                [
                    self._zones(),
                    ("/zones/zone-1/ssl/certificate_packs", refused),
                    ("/zones/zone-2/ssl/certificate_packs", refused),
                ],
            )

    # Sweep sharing -----------------------------------------------------------

    def test_one_sweep_reads_the_account_and_the_apps_once(self):
        fake = _Cloudflare([
            (f"/accounts/{ACCOUNT}/access/apps", _page([self.APP])),
            (f"/accounts/{ACCOUNT}/access/service_tokens", _page([])),
        ])
        with mock.patch.object(providers, "_cloudflare_api_request", fake):
            with providers.provider_snapshot():
                providers.list_access_apps()
                providers.list_access_service_tokens()

        self.assertEqual(len(fake.paths("/accounts?")), 1)
        self.assertEqual(len(fake.paths(f"/accounts/{ACCOUNT}/access/apps")), 1)


class ZonePostureTests(TestCase):
    @mock.patch("controller_runtime.providers._cloudflare_api_request")
    def test_each_posture_setting_is_read_on_its_own(self, request):
        request.side_effect = lambda path, *_: {
            "success": True,
            "result": {"id": path.rsplit("/", 1)[-1], "value": "on"},
        }

        posture = providers._cloudflare_zone_posture("zone-1")

        self.assertEqual(
            [call.args[0] for call in request.call_args_list],
            [f"/zones/zone-1/settings/{name}" for name in providers.ZONE_POSTURE_SETTINGS],
        )
        self.assertEqual(set(posture), set(providers.ZONE_POSTURE_SETTINGS))

    @mock.patch("controller_runtime.providers._cloudflare_api_request")
    def test_a_refused_posture_carries_its_reason(self, request):
        request.side_effect = providers.ProviderError("Cloudflare refused: 403")

        self.assertEqual(
            providers._cloudflare_zone_posture("zone-1"),
            {"unread": "Cloudflare refused: 403"},
        )

    @mock.patch("controller_runtime.providers._cloudflare_api_request")
    def test_a_partly_refused_posture_names_what_it_missed(self, request):
        def answer(path, *_):
            if path.endswith("/tls_1_3"):
                raise providers.ProviderError("Cloudflare refused: 403")
            return {"success": True, "result": {"value": "full"}}

        request.side_effect = answer

        posture = providers._cloudflare_zone_posture("zone-1")

        self.assertEqual(posture["ssl"], "full")
        self.assertNotIn("tls_1_3", posture)
        self.assertEqual(posture["unread"], "tls_1_3: Cloudflare refused: 403")


class AccountListTests(TestCase):
    @mock.patch("controller_runtime.providers._cloudflare_api_request")
    def test_total_pages_decides_over_a_short_page(self, request):
        request.side_effect = [_page([{"id": "a"}], 1, 2), _page([{"id": "b"}], 2, 2)]

        found = providers._cloudflare_api_list("/accounts/x/pages/projects", per_page=10)

        self.assertEqual([item["id"] for item in found], ["a", "b"])
