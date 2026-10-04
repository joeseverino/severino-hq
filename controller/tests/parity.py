"""Compare native provider results and requests against the Python controller.

Run with the host's test interpreter and a Go provider test binary built with
``go test -c ./providers``. Every request is answered locally from a synthetic
fixture. No secrets, network connections, or persistent writes are used.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import io
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from types import SimpleNamespace
import subprocess
import sys
import tempfile
from unittest import mock
import urllib.error
import urllib.parse

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

from control_plane.observations import portainer as portainer_kinds  # noqa: E402
from control_plane.provider_adapters import (  # noqa: E402
    adguard,
    adguard_readings,
    npm,
    npm_readings,
    portainer_readings,
)
from control_plane.provider_adapters.contracts import ProviderError  # noqa: E402
from control_plane.provider_adapters.parts import part_ledger  # noqa: E402
from analytics import contracts as analytics_contracts  # noqa: E402
from controller_runtime import providers as runtime_providers  # noqa: E402
from controller_runtime import (  # noqa: E402
    cloudflare,
    cloudflare_account,
    cloudflare_analytics,
    commands,
    glance,
    host_readings,
    portainer,
    provider_http,
    provider_runtime,
    redirects,
    tailnet_api,
    tailnet_policy,
    tailscale,
)
import tls_parity  # noqa: E402


class FixtureRuntime:
    def __init__(self, fixture):
        self.fixture = fixture
        self.requests = []
        self._snapshot = {}

    def connection_prefix(self, provider, connection_ref=""):
        return provider.upper()

    def connection_refs(self, provider):
        return ("example",)

    def required(self, prefix, name):
        return {"URL": "https://example.invalid", "USERNAME": "user", "PASSWORD": "synthetic"}[name]

    def request(self, url, *, method="GET", headers=None, payload=None):
        path = url.removeprefix("https://example.invalid")
        self.requests.append({"path": path, "method": method, "payload": payload})
        if path == "/api/tokens":
            return {"token": "synthetic"}
        if path in self.fixture.get("failures", {}):
            msg = self.fixture["failures"][path]
            if self.fixture.get("provider") == "npm":
                cause = urllib.error.HTTPError(url, 403, msg, {}, None)
                raise ProviderError(msg) from cause
            raise ProviderError(msg)
        if path in self.fixture["routes"]:
            return deepcopy(self.fixture["routes"][path])
        if method == "GET":
            raise AssertionError(f"Unexpected read: {path}")
        return None

    def snapshot_value(self, key, load):
        if key not in self._snapshot:
            self._snapshot[key] = load()
        return self._snapshot[key]

    def condition(self, kind, status, reason, message):
        return {"type": kind, "status": status, "reason": reason, "message": message}


class FixtureResponse:
    def __init__(self, body, headers):
        self._body = b"" if body is None else json.dumps(body).encode()
        self.headers = headers

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


class TailnetFixture:
    """Answers the module-level calls the Python Tailscale controller makes."""

    def __init__(self, fixture):
        self.fixture = fixture
        self.requests = []

    def open_url(self, url, *, method="GET", headers=None, data=None, timeout=15):
        path = url.removeprefix(tailnet_api.TAILNET_API).removeprefix(PORTAINER_BASE)
        payload = None
        if data is not None:
            if (headers or {}).get("Content-Type") == "application/x-www-form-urlencoded":
                payload = urllib.parse.parse_qs(data.decode())
            else:
                payload = json.loads(data)
        entry = {"path": path, "method": method, "payload": payload}
        if (headers or {}).get("If-Match"):
            entry["if_match"] = headers["If-Match"]
        self.requests.append(entry)
        statuses = {**self.fixture.get("statuses", {})}
        routes = {"/oauth/token": {"access_token": "synthetic"}, **self.fixture["routes"]}
        if method != "GET":
            statuses.update(self.fixture.get("write_statuses", {}))
            routes.update(self.fixture.get("answers", {}))
        if path in statuses:
            raise urllib.error.HTTPError(url, statuses[path], "refused", {}, None)
        if path not in routes and method == "GET":
            raise AssertionError(f"Unexpected read: {path}")
        return FixtureResponse(routes.get(path), self.fixture.get("headers", {}).get(path, {}))

    def __call__(self, runtime):
        runtime.requests = self.requests
        env = {
            key: value for key, value in os.environ.items()
            if not key.endswith("_CONNECTION_REF")
            and not key.startswith(("TAILSCALE_", "SEVERINO_TAILNET_", "PORTAINER_", "HQ_CONTROLLER_"))
        }
        env.update({
            "TAILSCALE_CONNECTION_REF": "example",
            "TAILSCALE_CLIENT_ID": "synthetic",
            "TAILSCALE_CLIENT_SECRET": "synthetic",
        })
        portainer_mock = mock.patch.object(
            tailnet_policy.portainer, "list_portainer_containers", side_effect=ProviderError("No Portainer."),
        )
        if self.fixture.get("portainer"):
            env.update(portainer_env(self.fixture))
            portainer_mock = mock.patch.object(portainer_readings.socket, "gethostbyname", side_effect=OSError)
        with tempfile.TemporaryDirectory() as scratch:
            readings = {}
            for key, variable in (("tailnet_status", "SEVERINO_TAILNET_STATUS"), ("tailnet_lock", "SEVERINO_TAILNET_LOCK")):
                readings[key] = ""
                if self.fixture.get(key):
                    readings[key] = str(Path(scratch, key))
                    Path(readings[key]).write_text(self.fixture[key], encoding="utf-8")
                    env[variable] = readings[key]
            # Both modules read their file names at import time.
            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(tailscale, "TAILNET_STATUS", readings["tailnet_status"]), \
                    mock.patch.object(tailnet_policy, "TAILNET_LOCK", readings["tailnet_lock"]), \
                    mock.patch.object(provider_http, "open_url", self.open_url), \
                    portainer_mock, \
                    provider_http.provider_snapshot():
                return tailnet_surface(self.fixture)


class CloudflareFixture:
    """Answers the Cloudflare REST and GraphQL calls the Python controller makes."""

    def __init__(self, fixture):
        self.fixture = fixture
        self.requests = []

    def open_url(self, url, *, method="GET", headers=None, data=None, timeout=15):
        path = url.removeprefix("https://example.invalid")
        payload = None if data is None else json.loads(data)
        entry = {"path": path, "method": method, "payload": payload}
        if (headers or {}).get("If-Match"):
            entry["if_match"] = headers["If-Match"]
        self.requests.append(entry)
        statuses = {**self.fixture.get("statuses", {})}
        routes = {**self.fixture["routes"]}
        if method != "GET":
            statuses.update(self.fixture.get("write_statuses", {}))
            routes.update(self.fixture.get("answers", {}))
        if path in self.fixture.get("network", ()):
            raise urllib.error.URLError("unreachable")
        if path in statuses:
            body = json.dumps(self.fixture.get("error_bodies", {}).get(path, {})).encode()
            raise urllib.error.HTTPError(url, statuses[path], "refused", {}, io.BytesIO(body))
        if path not in routes and method == "GET":
            raise AssertionError(f"Unexpected read: {path}")
        return FixtureResponse(routes.get(path), {})

    def __call__(self, runtime):
        runtime.requests = self.requests
        env = {
            key: value for key, value in os.environ.items()
            if not key.endswith("_CONNECTION_REF") and not key.startswith("CLOUDFLARE_")
        }
        env.update({
            "CLOUDFLARE_DNS_CONNECTION_REF": "example",
            "CLOUDFLARE_DNS_URL": "https://example.invalid",
            "CLOUDFLARE_DNS_API_TOKEN": "synthetic",
            "CLOUDFLARE_API_CONNECTION_REF": "account",
            "CLOUDFLARE_API_URL": "https://example.invalid",
            "CLOUDFLARE_API_API_TOKEN": "synthetic",
        })
        now = datetime(2026, 1, 2, 12, tzinfo=timezone.utc)
        # Zone ids are cached for the process's life; each fixture starts cold.
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.dict(cloudflare._ZONE_IDS, clear=True), \
                mock.patch.object(provider_http, "open_url", self.open_url), \
                mock.patch.object(analytics_contracts, "datetime", wraps=datetime) as clock, \
                provider_http.provider_snapshot():
            clock.now.return_value = now
            return cloudflare_surface(self.fixture)


def cloudflare_surface(fixture):
    surface, spec, apply = fixture["surface"], fixture.get("spec"), fixture.get("apply", False)
    if surface in ("reconcile", "delete"):
        action = cloudflare.reconcile_cloudflare_record if surface == "reconcile" else cloudflare.delete_cloudflare_record
        return action(spec, apply=apply, observed=fixture.get("observed"))
    if surface == "probe_dns":
        return cloudflare._probe_cloudflare_dns("example")
    if surface == "probe_api":
        return cloudflare._probe_cloudflare_api("account")
    if surface == "analytics":
        return cloudflare_analytics.analytics(sites=cloudflare_analytics.analytics_sites(), windows=fixture.get("windows", []))
    return {
        "zones": cloudflare.list_cloudflare_zones,
        "records": cloudflare.list_cloudflare_records,
        "pages": cloudflare_account.list_pages_projects,
        "d1": cloudflare_account.list_d1_databases,
        "access_apps": cloudflare_account.list_access_apps,
        "service_tokens": cloudflare_account.list_access_service_tokens,
        "tunnels": cloudflare_account.list_tunnels,
        "edge_certificates": cloudflare_account.list_edge_certificates,
        "redirects": cloudflare_account.list_redirects,
    }[surface]()


def tailnet_surface(fixture):
    surface, spec, apply = fixture["surface"], fixture.get("spec"), fixture.get("apply", False)
    actions = {
        "reconcile_device": tailscale.reconcile_tailnet_device,
        "approve_routes": tailscale.approve_tailnet_routes,
        "reconcile_policy": tailnet_policy.reconcile_tailnet_policy,
    }
    if surface in actions:
        return actions[surface](spec, apply=apply, observed=fixture.get("observed"))
    if surface == "probe":
        return tailscale._probe_tailscale("example")
    return {
        "device_inventory": tailscale.list_tailnet_devices,
        "policy_inventory": tailnet_policy.list_tailnet_policy,
        "dns": tailscale.list_tailnet_dns,
        "settings": tailscale.list_tailnet_settings,
        "users": tailscale.list_tailnet_users,
    }[surface]()


PORTAINER_BASE = "https://example.invalid"
# Its own ref: two connections sharing one would shadow each other.
PORTAINER_REF = "portainer-example"


def portainer_env(fixture):
    env = {
        "PORTAINER_CONNECTION_REF": PORTAINER_REF,
        "PORTAINER_URL": PORTAINER_BASE,
        "PORTAINER_API_TOKEN": "synthetic",
        "HQ_CONTROLLER_ID": fixture.get("controller_id", "hq-node"),
    }
    if fixture.get("run"):
        env["HQ_CONTROLLER_RUN"] = fixture["run"]
    return env


class PortainerFixture:
    """Answers the Portainer controller's requests, which go through request_json."""

    def __init__(self, fixture):
        self.fixture = fixture
        self.requests = []

    def request_json(self, url, *, method="GET", headers=None, payload=None):
        path = url.removeprefix(PORTAINER_BASE)
        self.requests.append({"path": path, "method": method, "payload": payload})
        if path in self.fixture.get("statuses", {}):
            code = self.fixture["statuses"][path]
            failure = {401: "credential", 403: "permission"}.get(code, "")
            cause = urllib.error.HTTPError(url, code, "refused", {}, None)
            # What the real request_json raises for an HTTP error status.
            raise ProviderError("Provider request failed: HTTPError.", refusal=failure, failure=failure) from cause
        if path in self.fixture.get("failures", {}):
            raise ProviderError(self.fixture["failures"][path])
        if path in self.fixture["routes"]:
            return deepcopy(self.fixture["routes"][path])
        if method == "GET":
            raise AssertionError(f"Unexpected read: {path}")
        return None

    def __call__(self, runtime):
        runtime.requests = self.requests
        env = {
            key: value for key, value in os.environ.items()
            if not key.endswith("_CONNECTION_REF") and not key.startswith(("PORTAINER_", "HQ_CONTROLLER_"))
        }
        env.update(portainer_env(self.fixture))
        resolves = self.fixture.get("resolves")
        resolve = {"return_value": resolves} if resolves else {"side_effect": OSError}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(provider_http, "request_json", self.request_json), \
                mock.patch.object(portainer_readings.socket, "gethostbyname", **resolve), \
                provider_http.provider_snapshot():
            return portainer_surface(self.fixture)


def portainer_surface(fixture):
    surface, spec, apply = fixture["surface"], fixture.get("spec"), fixture.get("apply", False)
    actions = {
        "reconcile": portainer.reconcile_portainer,
        "delete": portainer.delete_portainer,
        "restart": portainer.restart_portainer_container,
        "start": portainer.start_portainer_container,
        "stop": portainer.stop_portainer_container,
    }
    if surface in actions:
        return actions[surface](spec, apply=apply, observed=fixture.get("observed"))
    if surface == "probe":
        return portainer._probe_portainer(PORTAINER_REF)
    if surface == "inventory":
        return portainer.list_portainer_containers()
    kinds = {
        "environments": portainer_kinds.ENVIRONMENT_KIND,
        "networks": portainer_kinds.NETWORK_KIND,
        "volumes": portainer_kinds.VOLUME_KIND,
        "images": portainer_kinds.IMAGE_KIND,
        "runtime": portainer_kinds.RUNTIME_KIND,
        "stacks": portainer_kinds.STACK_KIND,
    }
    return portainer_readings.READINGS[kinds[surface]](provider_runtime.RUNTIME)


def python_result(fixture):
    runtime = FixtureRuntime(fixture)
    value, error = None, ""
    with part_ledger() as refused:
        try:
            if fixture.get("provider") == "portainer":
                value = PortainerFixture(fixture)(runtime)
            elif fixture.get("provider") == "tailscale":
                value = TailnetFixture(fixture)(runtime)
            elif fixture.get("provider") == "cloudflare":
                value = CloudflareFixture(fixture)(runtime)
            elif fixture.get("provider") == "tls":
                value = tls_parity.run(fixture, runtime.requests)
            else:
                value = call_surface(runtime, fixture)
        except ProviderError as exc:
            error = str(exc)
    if is_dataclass(value):
        value = asdict(value)
    return json.loads(json.dumps({
        "result": value, "error": error, "requests": runtime.requests, "refused_parts": refused,
    }))


def call_surface(runtime, fixture):
    surface = fixture["surface"]
    if fixture.get("provider") == "npm":
        if surface in ("reconcile", "delete"):
            return getattr(npm, surface)(
                runtime, fixture["spec"], observed=fixture.get("observed"), apply=fixture["apply"],
            )
        if surface == "probe":
            return npm.probe(runtime, "example")
        handlers = {
            "inventory": npm.inventory,
            "certificates": lambda r: npm_readings.certificates(r, "example"),
            "redirects": lambda r: npm_readings.redirects(r, "example"),
            "dead_hosts": lambda r: npm_readings.dead_hosts(r, "example"),
            "streams": lambda r: npm_readings.streams(r, "example"),
            "access_lists": lambda r: npm_readings.access_lists(r, "example"),
        }
        return handlers[surface](runtime)

    if surface in ("reconcile", "delete"):
        return getattr(adguard, surface)(
            runtime, fixture["spec"], observed=fixture.get("observed"), apply=fixture["apply"],
        )
    if surface == "probe":
        return adguard.probe(runtime, "example")
    handlers = {
        "inventory": adguard.inventory,
        "clients": adguard_readings.read_clients,
        "dns": adguard_readings.read_dns,
        "queries": adguard_readings.read_query_summary,
    }
    now = datetime(2026, 1, 2, 12, tzinfo=timezone.utc)
    with mock.patch.object(adguard_readings, "datetime", wraps=datetime) as clock:
        clock.now.return_value = now
        return handlers[surface](runtime)


def mutation_fixtures():
    spec = {"domain": "example.test", "answer": "192.0.2.1"}
    rows = (
        [], [spec], [{**spec, "answer": "192.0.2.2"}],
        [{**spec, "domain": "old.test"}], [spec, spec], [{**spec, "enabled": False}],
    )
    for surface in ("reconcile", "delete"):
        for apply in (False, True):
            for records in rows:
                yield {
                    "surface": surface, "apply": apply, "spec": spec,
                    "observed": {"domain": "old.test"},
                    "routes": {"/control/rewrite/list": records},
                }


def read_fixtures():
    yield {"surface": "inventory", "routes": {"/control/rewrite/list": [
        {"domain": "example.test", "answer": "192.0.2.1"},
        {"domain": "off.test", "answer": "192.0.2.2", "enabled": False},
    ]}}
    yield {"surface": "clients", "routes": {"/control/clients": {
        "clients": [{"name": "known", "ids": ["192.0.2.1", "client-id", ""]}],
        "auto_clients": [{"ip": "192.0.2.1"}, {"ip": "192.0.2.2", "source": "ARP"}],
    }}}
    for status in ({"dns_addresses": [], "version": "example"}, {}):
        yield {"surface": "probe", "routes": {"/control/status": status}}
    routes = {
        "/control/status": {"version": "example", "running": True, "dns_addresses": ["192.0.2.1"]},
        "/control/dns_info": {"upstream_dns": ["[/example.test/]tls://resolver.example", "https://dns.example/query?private=yes", "# comment"]},
        "/control/filtering/status": {"enabled": True, "filters": [{"enabled": True, "rules_count": 10}]},
        "/control/querylog/config": {"enabled": True, "interval": 86400000},
        "/control/rewrite/settings": {"enabled": True},
    }
    yield {"surface": "dns", "routes": routes}
    yield {"surface": "dns", "routes": routes, "failures": {"/control/dns_info": "Refused."}}
    for anonymized in (False, True):
        yield {"surface": "queries", "routes": {
            "/control/querylog/config": {"enabled": True, "anonymize_client_ip": anonymized},
            "/control/rewrite/list": [{"domain": "example.test"}, {"domain": "unused.test"}, {"domain": "*.zone.test"}],
            "/control/querylog?limit=500": {"data": [
                {"time": "2026-01-02T11:00:00Z", "question": {"name": "Example.Test."}, "client": "192.0.2.1", "reason": "FilteredBlackList"},
                {"time": "2026-01-02T10:00:00Z", "question": {"name": "private.example"}},
                {"time": "2026-01-02T09:00:00Z", "question": {"name": "child.zone.test"}},
            ]},
        }}


def npm_mutation_fixtures():
    spec = {
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
        "certificate_id": 0,
        "advanced_config": "",
        "hsts_enabled": False,
        "hsts_subdomains": False,
        "trust_forwarded_proto": False,
        "serving": True,
    }
    live_match = {
        "id": 9,
        "domain_names": ["hq.example"],
        "forward_scheme": "http",
        "forward_host": "192.0.2.10",
        "forward_port": 8000,
        "caching_enabled": False,
        "block_exploits": True,
        "allow_websocket_upgrade": False,
        "access_list_id": 0,
        "certificate_id": 0,
        "ssl_forced": False,
        "http2_support": True,
        "hsts_enabled": False,
        "hsts_subdomains": False,
        "trust_forwarded_proto": False,
        "advanced_config": "",
        "locations": [],
        "enabled": True,
        "meta": {},
    }
    live_rename = {**live_match, "domain_names": ["old.example"]}
    live_update = {**live_match, "forward_port": 9000}
    rows = (
        [],
        [live_match],
        [live_update],
        [live_rename],
        [live_match, live_match],
    )
    for surface in ("reconcile", "delete"):
        for apply in (False, True):
            for records in rows:
                yield {
                    "provider": "npm",
                    "surface": surface,
                    "apply": apply,
                    "spec": spec,
                    "observed": {"domain_names": ["old.example"]},
                    "routes": {"/api/nginx/proxy-hosts": records},
                }
    yield {
        "provider": "npm",
        "surface": "reconcile",
        "apply": True,
        "spec": {**spec, "force_ssl": True, "certificate_id": 0},
        "routes": {"/api/nginx/proxy-hosts": []},
    }


def npm_read_fixtures():
    routes = {
        "/api/nginx/proxy-hosts": [
            {"id": 10, "domain_names": ["app.example.com"], "certificate_id": 1, "access_list_id": 7, "forward_scheme": "http", "forward_host": "127.0.0.1", "forward_port": 8000, "enabled": True},
            {"id": 11, "domain_names": ["off.example.com"], "certificate_id": 1, "enabled": False},
        ],
        "/api/nginx/certificates": [
            {"id": 1, "nice_name": "example wildcard", "provider": "letsencrypt", "domain_names": ["*.example.com", "example.com"], "expires_on": "2030-01-01 00:00:00", "meta": {"key": "secret"}},
            {"id": 2, "nice_name": "unused", "provider": "other", "domain_names": ["old.example.net"], "expires_on": "2030-02-01 00:00:00"},
        ],
        "/api/nginx/redirection-hosts": [
            {"id": 20, "domain_names": ["www.example.com"], "forward_scheme": "https", "forward_domain_name": "example.com", "forward_http_code": 301, "preserve_path": True, "certificate_id": 1, "ssl_forced": True, "enabled": True},
            {"id": 21, "domain_names": ["auto.example.com"], "forward_scheme": "auto", "forward_domain_name": "example.org", "forward_http_code": 302, "enabled": False},
        ],
        "/api/nginx/dead-hosts": [
            {"id": 30, "domain_names": ["gone.example.com"], "certificate_id": 1, "enabled": True},
        ],
        "/api/nginx/streams": [
            {"id": 40, "incoming_port": 2222, "forwarding_host": "192.0.2.30", "forwarding_port": 22, "tcp_forwarding": True, "udp_forwarding": False, "enabled": True},
        ],
        "/api/nginx/access-lists?expand=items,clients": [
            {"id": 7, "name": "staff", "satisfy_any": True, "pass_auth": False, "items": [{"username": "operator", "password": "x"}], "clients": [{"directive": "allow", "address": "100.64.0.0/10"}, {"directive": "deny", "address": "all"}]},
        ],
    }
    yield {"provider": "npm", "surface": "probe", "routes": {}}
    yield {"provider": "npm", "surface": "inventory", "routes": routes}
    for surface in ("certificates", "redirects", "dead_hosts", "streams", "access_lists"):
        yield {"provider": "npm", "surface": surface, "routes": routes}
    yield {"provider": "npm", "surface": "certificates", "routes": routes, "failures": {"/api/nginx/dead-hosts": "Forbidden."}}
    yield {"provider": "npm", "surface": "access_lists", "routes": routes, "failures": {"/api/nginx/proxy-hosts": "Forbidden."}}


TAILNET_STATUS = {
    "Self": {
        "ID": "n1", "HostName": "server", "PublicKey": "peer-1", "DNSName": "server.tail.example.",
        "Online": True, "TailscaleIPs": ["192.0.2.1", "2001:db8::1"], "OS": "linux",
        "Addrs": ["198.51.100.5:41641"], "KeyExpiry": "2027-01-01T00:00:00Z",
    },
    "Peer": {
        "peer-z": {
            "ID": "n2", "HostName": "laptop", "PublicKey": "peer-z", "Online": True,
            "TailscaleIPs": ["192.0.2.2"], "ExitNodeOption": True, "CurAddr": "198.51.100.6:41641",
            "Relay": "fra", "LastHandshake": "2026-01-02T11:00:00Z", "Active": True, "RxBytes": 10, "TxBytes": 20,
            "LastSeen": "2026-01-02T11:00:00Z", "KeyExpiry": "2026-06-01T00:00:00Z",
        },
        "peer-a": {"ID": "n3", "HostName": "phone", "TailscaleIPs": ["2001:db8::3"], "ExitNode": True},
        "peer-b": {"ID": "n4", "HostName": " "},
    },
}

TAILNET_DEVICES = {"devices": [
    {
        "id": "d1", "hostname": "server", "name": "server.tail.example.", "nodeKey": "peer-1",
        "addresses": ["192.0.2.1", "2001:db8::1"], "user": "operator@example.com", "tags": ["tag:server", "tag:b"],
        "advertisedRoutes": ["192.0.2.0/24", "0.0.0.0/0", "::/0"], "enabledRoutes": ["0.0.0.0/0"],
        "keyExpiryDisabled": True, "clientVersion": "1.80.0", "updateAvailable": True, "sshEnabled": True,
        "connectedToControl": True, "expires": "2027-01-01T00:00:00Z", "os": "linux",
        "lastSeen": "2026-01-02T11:00:00Z", "clientConnectivity": {"endpoints": ["198.51.100.5:41641"]},
    },
    {
        "id": "d2", "hostname": "laptop", "authorized": False, "tailnetLockError": "unsigned",
        "blocksIncomingConnections": True, "isExternal": True, "keyExpiryDisabled": False,
        "expires": "2026-06-01T00:00:00Z", "addresses": ["192.0.2.2"],
    },
    {"hostname": ""},
]}

TAILNET_POLICY = {
    "groups": {"group:admins": ["operator@example.com", "b@example.com"]},
    "tagOwners": {"tag:server": ["group:admins"]},
    "hosts": {"nas": "192.0.2.9"},
    "grants": [{"src": ["group:admins"], "dst": ["tag:server"], "ip": ["443", "22"]}],
    "ssh": [{"action": "accept", "src": ["group:admins"], "dst": ["tag:server"], "users": ["root", "autogroup:nonroot"]}],
    "nodeAttrs": [
        {"target": ["*"], "app": {"tailscale.com/app-connectors": [
            {"name": "saas", "connectors": ["tag:connector"], "domains": ["b.example", "a.example"]},
        ]}},
        {"target": ["tag:x"], "attr": ["funnel"]},
    ],
    "tests": [{"src": "operator@example.com", "proto": "tcp", "accept": ["tag:server:443"], "deny": ["tag:server:22"]}],
    "ratio": 1.0,
    "note": "café <ops> & \"quoted\"",
}

TAILNET_PREVIEWS = {
    "/tailnet/-/acl/preview?type=ipport&previewFor=192.0.2.1%3A22": {
        "matches": [{"users": ["group:admins"], "ports": ["tag:server:22"], "lineNumber": 7}],
    },
    "/tailnet/-/acl/preview?type=ipport&previewFor=192.0.2.1%3A443": {"matches": [
        {"users": ["operator@example.com", "group:admins"], "ports": ["tag:server:443", "*:443"]},
        {"users": [], "ports": ["tag:server:443"], "lineNumber": 12},
    ]},
    "/tailnet/-/acl/preview?type=ipport&previewFor=192.0.2.2%3A80": {"matches": []},
}


def tailnet(surface, routes=None, **extra):
    return {"provider": "tailscale", "surface": surface, "routes": routes or {}, **extra}


def tailscale_mutation_fixtures():
    status = json.dumps(TAILNET_STATUS)
    key = "/device/n1/key"
    for wanted in (False, True):
        for apply in (False, True):
            yield tailnet(
                "reconcile_device", apply=apply, tailnet_status=status,
                spec={"name": "server", "connection_ref": "", "key_expiry_disabled": wanted},
            )
    yield tailnet("reconcile_device", apply=True, tailnet_status=status,
                  spec={"name": "phone", "connection_ref": "", "key_expiry_disabled": False})
    yield tailnet("reconcile_device", apply=True, tailnet_status=status,
                  spec={"name": "missing", "connection_ref": "", "key_expiry_disabled": True})
    yield tailnet("reconcile_device", apply=True,
                  spec={"name": "server", "connection_ref": "", "key_expiry_disabled": True})
    for code in (403, 500):
        yield tailnet("reconcile_device", apply=True, tailnet_status=status, statuses={key: code},
                      spec={"name": "server", "connection_ref": "", "key_expiry_disabled": True})
    yield tailnet("reconcile_device", apply=True, tailnet_status=status, statuses={"/oauth/token": 401},
                  spec={"name": "server", "connection_ref": "", "key_expiry_disabled": True})

    routes = "/device/n2/routes"
    spec = {"name": "laptop"}
    for current in (
        {"advertisedRoutes": ["192.0.2.0/24", "0.0.0.0/0"], "enabledRoutes": ["192.0.2.0/24"]},
        {"advertisedRoutes": ["192.0.2.0/24"], "enabledRoutes": ["192.0.2.0/24"]},
        {},
    ):
        for apply in (False, True):
            yield tailnet("approve_routes", {routes: current}, apply=apply, tailnet_status=status, spec=spec)
    pending = {"advertisedRoutes": ["::/0", "0.0.0.0/0"], "enabledRoutes": []}
    yield tailnet("approve_routes", {routes: pending}, apply=True, tailnet_status=status, spec=spec,
                  answers={routes: {"advertisedRoutes": ["::/0", "0.0.0.0/0"], "enabledRoutes": ["::/0", "0.0.0.0/0"]}})
    for code in (401, 403, 500):
        yield tailnet("approve_routes", apply=True, tailnet_status=status, spec=spec, statuses={routes: code})
    yield tailnet("approve_routes", apply=True, tailnet_status=status, spec={"name": "missing"})

    live = json.dumps(TAILNET_POLICY)
    acl = {"/tailnet/-/acl": TAILNET_POLICY}
    untested = {key: value for key, value in TAILNET_POLICY.items() if key != "tests"}
    weaker = {**TAILNET_POLICY, "tests": [{"src": "other@example.com", "accept": ["tag:server:443"]}]}
    no_deny = {**TAILNET_POLICY, "tests": [{"src": "operator@example.com", "proto": "tcp", "accept": ["tag:server:22"]}]}
    stronger = {**TAILNET_POLICY, "grants": [], "tests": [*TAILNET_POLICY["tests"], {"src": "b@example.com", "deny": ["tag:server:443"]}]}
    yield tailnet("reconcile_policy", acl, apply=True, spec={"document": "  "})
    yield tailnet("reconcile_policy", acl, apply=True, spec={"document": "{not json"})
    yield tailnet("reconcile_policy", acl, apply=True, spec={"document": live})
    yield tailnet("reconcile_policy", {"/tailnet/-/acl": untested}, apply=True, spec={"document": json.dumps(untested)})
    for document in (untested, weaker, no_deny):
        yield tailnet("reconcile_policy", acl, apply=True, spec={"document": json.dumps(document)})
    validate = "/tailnet/-/acl/validate"
    yield tailnet("reconcile_policy", {**acl, validate: {"message": "test failed", "data": [{"user": "b@example.com", "errors": ["denied <443>"]}]}},
                  apply=True, spec={"document": json.dumps(stronger)})
    yield tailnet("reconcile_policy", acl, apply=True, spec={"document": json.dumps(stronger)}, statuses={validate: 400})
    for apply in (False, True):
        yield tailnet("reconcile_policy", acl, apply=apply, spec={"document": json.dumps(stronger)},
                      headers={"/tailnet/-/acl": {"etag": "\"v1\""}})
    for code in (412, 500):
        yield tailnet("reconcile_policy", acl, apply=True, spec={"document": json.dumps(stronger)},
                      write_statuses={"/tailnet/-/acl": code}, headers={"/tailnet/-/acl": {"etag": "\"v1\""}})
    # No version came back with the live read, so nothing is written; there is no second read for one.
    yield tailnet("reconcile_policy", acl, apply=True, spec={"document": json.dumps(stronger)})
    yield tailnet("reconcile_policy", acl, apply=True, spec={"document": json.dumps(stronger)},
                  headers={"/tailnet/-/acl": {"etag": ""}})
    yield tailnet("reconcile_policy", apply=True, spec={"document": live}, statuses={"/tailnet/-/acl": 403})
    # Python's == decides "already current": 1 == 1.0, True == 1, and key order is ignored.
    flagged = {**TAILNET_POLICY, "flag": True}
    yield tailnet("reconcile_policy", {"/tailnet/-/acl": flagged}, apply=True,
                  spec={"document": json.dumps({**dict(reversed(list(flagged.items()))), "ratio": 1, "flag": 1})})
    yield tailnet("reconcile_policy", acl, apply=False, spec={"document": json.dumps({**stronger, "ratio": 1.5})})
    for document in ("[1, 2]", "5", "\"text\"", "null"):
        yield tailnet("reconcile_policy", acl, apply=True, spec={"document": document})


def tailscale_read_fixtures():
    status = json.dumps(TAILNET_STATUS)
    acl = {"/tailnet/-/acl": TAILNET_POLICY}
    devices = {"/tailnet/-/devices?fields=all": TAILNET_DEVICES}
    yield tailnet("device_inventory", {**devices, **acl, **TAILNET_PREVIEWS}, tailnet_status=status)
    yield tailnet("device_inventory", {**devices, **acl, **TAILNET_PREVIEWS})
    # Python reads authorized as bool(device.get("authorized", True)): null is False.
    nulled = {"devices": [{**TAILNET_DEVICES["devices"][0], "authorized": None}, *TAILNET_DEVICES["devices"][1:]]}
    yield tailnet("device_inventory", {"/tailnet/-/devices?fields=all": nulled, **acl, **TAILNET_PREVIEWS}, tailnet_status=status)
    yield tailnet("device_inventory", tailnet_status=status,
                  statuses={"/tailnet/-/devices?fields=all": 403, "/tailnet/-/acl": 403})
    yield tailnet("device_inventory", tailnet_status=status, statuses={"/oauth/token": 401})
    yield tailnet("device_inventory", statuses={"/oauth/token": 401})
    for code in (401, 403, 500):
        yield tailnet("device_inventory", statuses={"/tailnet/-/devices?fields=all": code})
    yield tailnet("device_inventory", {"/tailnet/-/devices?fields=all": ["not", "an", "object"]})

    lock = json.dumps({
        "Enabled": True, "NodeKeySigned": False, "TrustedKeys": [{"Key": "a"}, {"Key": "b"}],
        "FilteredPeers": [{"Name": "zeta", "StableID": "s1"}, {"StableID": "s2"}, {}],
    })
    parts = {
        "/tailnet/-/settings": {"devicesApprovalOn": True, "httpsEnabled": False},
        "/tailnet/-/dns/preferences": {"magicDNS": True},
        "/tailnet/-/dns/nameservers": {"dns": ["192.0.2.53"]},
        "/tailnet/-/dns/searchpaths": {"searchPaths": ["example.test"]},
        "/tailnet/-/services": {"vipServices": [{"name": "svc:web", "addrs": ["192.0.2.80", "2001:db8::80"], "comment": "web", "ports": ["tcp:443", "tcp:80"]}]},
    }
    yield tailnet("policy_inventory", {**acl, **parts}, tailnet_lock=lock)
    yield tailnet("policy_inventory", {**acl, **parts, "/tailnet/-/settings": ["not", "an", "object"]},
                  statuses={"/tailnet/-/dns/nameservers": 403, "/tailnet/-/services": 404})
    yield tailnet("policy_inventory", {"/tailnet/-/acl": {}, **parts}, tailnet_lock="not json")
    yield tailnet("policy_inventory", statuses={"/tailnet/-/acl": 403})
    yield tailnet("policy_inventory", statuses={"/oauth/token": 401})

    yield tailnet("dns", {"/tailnet/-/dns/configuration": {
        "nameservers": [{"address": "192.0.2.53"}, "192.0.2.54", {"name": "no address"}, ""],
        "preferences": {"magicDNS": True, "overrideLocalDNS": False},
        "searchPaths": ["example.test"],
        "splitDNS": {"corp.example": [{"address": "192.0.2.55"}], "empty.example": None},
    }})
    yield tailnet("dns", {"/tailnet/-/dns/configuration": {}})
    for code in (403, 404, 401, 500):
        yield tailnet("dns", statuses={"/tailnet/-/dns/configuration": code})
    yield tailnet("dns", {"/tailnet/-/dns/configuration": ["not", "an", "object"]})

    yield tailnet("settings", {"/tailnet/-/settings": {
        "devicesApprovalOn": True, "devicesKeyDurationDays": 90, "devicesAutoUpdatesOn": False,
        "usersApprovalOn": None, "regionalRoutingOn": True, "postureIdentityCollectionOn": False,
        "httpsEnabled": True, "aclsExternallyManagedOn": False,
    }})
    yield tailnet("settings", {"/tailnet/-/settings": {"devicesApprovalOn": True, "httpsEnabled": None}})
    yield tailnet("settings", statuses={"/tailnet/-/settings": 403})

    yield tailnet("users", {"/tailnet/-/users": {"users": [
        {"id": "u1", "displayName": "Operator", "loginName": "operator@example.com", "role": "owner",
         "status": "active", "created": "2025-01-01T00:00:00Z", "lastSeen": "2026-01-02T11:00:00Z"},
        {"displayName": "no id"},
        {"id": "u2"},
    ]}})
    yield tailnet("users", statuses={"/tailnet/-/users": 401})

    yield tailnet("probe")
    yield tailnet("probe", statuses={"/oauth/token": 401})
    yield tailnet("probe", {"/oauth/token": {"token_type": "bearer"}})
    yield tailnet("probe", {"/oauth/token": ["not", "an", "object"]})


class LocalFixture:
    """Answers the commands, HTTP calls, dials and Portainer reads the host readings,
    glance and redirects make, recording each in the order it was made."""

    def __init__(self, fixture):
        self.fixture = fixture
        self.requests = []
        self.refusals = []
        self.steps = []

    def run(self, command, input=None, **_):
        self.requests.append({"kind": "command", "argv": list(command), "input": input.decode() if input else None})
        ref = Path(command[command.index("-i") + 1]).name if "-i" in command else ""
        answer = self.fixture.get("commands", {}).get(f"{ref} {command[-1]}", {})
        if answer == "missing":
            raise FileNotFoundError(command[0])
        if answer == "overflow":
            return subprocess.CompletedProcess(command, 0, b"x" * (commands.OUTPUT_LIMIT + 1), b"")
        return subprocess.CompletedProcess(
            command, answer.get("exit", 0), answer.get("stdout", "").encode(), answer.get("stderr", "").encode(),
        )

    def request_json(self, url, *, method="GET", headers=None, payload=None):
        self.requests.append({"kind": "http", "url": url, "method": method})
        if url in self.fixture.get("failures", {}):
            raise ProviderError(self.fixture["failures"][url])
        if url in self.fixture.get("routes", {}):
            return deepcopy(self.fixture["routes"][url])
        raise AssertionError(f"Unexpected read: {url}")

    def containers(self):
        found = self.fixture.get("containers")
        if found == "refused":
            raise ProviderError("Portainer refused.")
        return deepcopy(found or [])

    def zone_answer(self, path, ref):
        del ref
        if path in self.fixture.get("failures", {}):
            raise ProviderError(self.fixture["failures"][path])
        if path in self.fixture.get("routes", {}):
            return deepcopy(self.fixture["routes"][path])
        raise AssertionError(f"Unexpected read: {path}")

    def refuse(self, part, exc, *, scope, connection_ref):
        self.refusals.append({"part": part, "reason": str(exc), "scope": scope, "connection_ref": connection_ref})

    def portainer(self):
        settings = self.fixture.get("portainer") or {}
        return (
            mock.patch.object(glance.portainer, "portainer_url", lambda ref: settings["url"]),
            mock.patch.object(glance.portainer, "portainer_headers", lambda ref: dict(settings.get("headers", {}))),
            mock.patch.object(glance.portainer, "load_portainer_environments",
                              lambda ref: deepcopy(settings.get("environments", []))),
        )

    def __call__(self):
        env = {
            key: value for key, value in os.environ.items()
            if not key.endswith("_CONNECTION_REF") and not key.startswith(("HQ_CONTROLLER", "SEVERINO_"))
        }
        env.update(self.fixture.get("env", {}))
        env["HQ_CONTROLLER_SSH_DIR"] = "/ssh"
        opened = set(self.fixture.get("open", []))
        with tempfile.TemporaryDirectory() as scratch:
            firewall = ""
            if self.fixture.get("firewall") is not None:
                firewall = str(Path(scratch, "firewall.json"))
                Path(firewall).write_text(json.dumps(self.fixture["firewall"]), encoding="utf-8")
            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(commands, "_STEP_FAILURES", self.steps), \
                    mock.patch.object(commands.subprocess, "run", self.run), \
                    mock.patch.object(provider_http, "request_json", self.request_json), \
                    mock.patch.object(host_readings, "HOST_FIREWALL", firewall), \
                    mock.patch.object(host_readings, "_answers_from_here",
                                      lambda address, port, timeout=3.0: f"{address}:{port}" in opened), \
                    mock.patch.object(host_readings.portainer, "list_portainer_containers", self.containers), \
                    mock.patch.object(glance, "controller_id", lambda: "controller"), \
                    mock.patch.dict(runtime_providers.PROVIDERS, {
                        kind: SimpleNamespace(connection_providers=()) for kind in self.fixture.get("undeclared", [])
                    }):
                patches = self.portainer()
                with patches[0], patches[1], patches[2]:
                    return self.surface()

    def surface(self):
        surface = self.fixture["surface"]
        if surface == "firewall":
            return host_readings.list_host_firewall()
        if surface == "perimeter":
            return host_readings.list_host_perimeter()
        if surface == "glance":
            return glance.dashboard_glance(self.fixture["plan"])
        if surface == "connections":
            return runtime_providers.connections(carry=frozenset(self.fixture.get("carry", [])))
        if surface == "execute":
            return runtime_providers.execute(self.fixture["resource"], self.fixture["action"], apply=self.fixture["apply"])
        if surface == "inventory":
            return runtime_providers.inventory(only=frozenset(self.fixture["only"]))
        zones = self.fixture.get("zones", {})
        return redirects.read(self.fixture["refs"], redirects.ZoneReads(
            zones=lambda ref: deepcopy(zones.get(ref, [])),
            listed=self.zone_answer,
            result=self.zone_answer,
            reason=str,
            error=ProviderError,
            refuse=self.refuse,
        ))


def local_result(fixture):
    local = LocalFixture(fixture)
    value, error = None, ""
    try:
        value = local()
    except ProviderError as exc:
        error = str(exc)
    if is_dataclass(value):
        value = asdict(value)
    if fixture["surface"] == "connections":
        # Django reads failure with .get, so an empty one is the same as none.
        value = [{key: item for key, item in record.items() if not (key == "failure" and item == "")}
                 for record in value]
    return json.loads(json.dumps({
        "result": value, "error": error, "requests": local.requests,
        "refusals": local.refusals, "steps": local.steps,
    }))

EDGE = {
    "EDGE_CONNECTION_REF": "edge", "EDGE_HOST": "203.0.113.10", "EDGE_USER": "hq", "EDGE_PORT": "7722",
    "EDGE_HOST_KEY": "ssh-ed25519 synthetic", "EDGE_ROLE": "caddy",
}


def host_fixtures():
    yield {"provider": "local", "surface": "firewall", "firewall": {"tailnet_only": True, "interface": "tailscale0"}}
    yield {"provider": "local", "surface": "firewall"}
    reading = json.dumps({"public_addresses": "203.0.113.10, 198.51.100.2,", "firewall_unit": "active",
                          "read_at": "2026-01-02T12:00:00Z"})
    containers = [
        {"host": "edge", "host_address": "", "ports": [80, 443, "9001", "x", 70000, 0]},
        {"host": "other", "host_address": "203.0.113.10", "ports": [3001]},
        {"host": "far", "host_address": "192.0.2.99", "ports": [8080]},
    ]
    opened = ["203.0.113.10:443", "198.51.100.2:22", "203.0.113.10:7722", "192.0.2.99:8080"]
    perimeter = {"provider": "local", "surface": "perimeter", "env": EDGE, "open": opened}
    yield {**perimeter, "commands": {"edge perimeter": {"stdout": reading}}, "containers": containers}
    yield {**perimeter, "commands": {"edge perimeter": {"stdout": reading}}, "containers": "refused"}
    yield {**perimeter, "commands": {"edge perimeter": {"stdout": ""}}, "containers": containers}
    yield {**perimeter, "commands": {"edge perimeter": {"stdout": json.dumps(
        {"firewall_unit": None, "read_at": 5, "public_addresses": None})}}}
    yield {**perimeter, "commands": {"edge perimeter": {"exit": 255, "stderr": "Permission denied (publickey)."}}}
    yield {**perimeter, "commands": {"edge perimeter": "missing"}}
    # More output than a step may print is a failed step, whatever the exit code.
    yield {**perimeter, "commands": {"edge perimeter": "overflow"}}
    yield {**perimeter, "env": {**EDGE, "EDGE_PORT": "70000"}}
    yield {**perimeter, "env": {**EDGE, "EDGE_USER": "-oProxyCommand=x"}}
    yield {**perimeter, "env": {key: value for key, value in EDGE.items() if key != "EDGE_ROLE"}}


POINT = "https://api.weather.gov/points/40.7128,-74.0060"
HOURLY = "https://api.weather.gov/gridpoints/OKX/33,35/forecast/hourly"
ALERTS = "https://api.weather.gov/alerts/active?point=40.7128,-74.0060"


def weather_routes(periods, features, place=None):
    properties = {"forecastHourly": HOURLY}
    if place is not None:
        properties["relativeLocation"] = {"properties": place}
    return {POINT: {"properties": properties}, HOURLY: {"properties": {"periods": periods}},
            ALERTS: {"features": features}}


def glance_fixtures():
    periods = [
        {"name": "This Afternoon", "startTime": "2026-01-02T16:00:00-05:00", "temperature": 71, "temperatureUnit": "F",
         "windSpeed": "5 mph", "windDirection": "SW", "shortForecast": "Sunny",
         "probabilityOfPrecipitation": {"value": None}},
        {"startTime": "2026-01-02T17:00:00-05:00", "temperature": 68.5, "shortForecast": "Clear",
         "probabilityOfPrecipitation": {"value": 10}},
        {"startTime": "2026-01-02T00:00:00-05:00", "temperature": 59, "shortForecast": "Rain Likely",
         "probabilityOfPrecipitation": {"value": 72.9}},
        "not a period",
        *[{"startTime": f"2026-01-03T{hour:02d}:00:00-05:00", "temperature": 60 + hour,
           "probabilityOfPrecipitation": {"value": 40}} for hour in range(1, 12)],
    ]
    features = [{"properties": {"event": "Wind Advisory", "headline": "Gusts to 50 mph"}}, {}]
    weather = {"provider": "local", "surface": "glance", "plan": {"panels": ["weather"], "targets": {"weather": {"point": " 40.71280 , -74.006 "}}}}
    yield {**weather, "routes": weather_routes(periods, features, {"city": "New York", "state": "NY"})}
    yield {**weather, "routes": weather_routes([], [], {"city": "", "state": "NJ"})}
    windy = [{"startTime": "2026-01-02T16:00:00Z", "temperature": None, "shortForecast": "Showers",
              "windChill": {"unitCode": "wmoUnit:degC", "value": None}, "probabilityOfPrecipitation": {"value": 30}}]
    yield {**weather, "routes": weather_routes(windy, [{"properties": {}}])}
    yield {**weather, "routes": weather_routes(periods, features), "failures": {HOURLY: "Weather refused."}}
    for point in ("abc", "1,2,3", "95,10", "nan,0", ""):
        yield {**weather, "plan": {"panels": ["weather"], "targets": {"weather": {"point": point}}}}

    host = json.dumps({"cpu_percent": 12.6, "cores": 8, "load_1m": 0.425, "memory_used": 3 * 2**30,
                       "memory_total": 16 * 2**30, "storage_used": 512 * 2**20, "storage_total": 0})
    servers = {"SRV_CONNECTION_REF": "srv", "SRV_HOST": "192.0.2.10", "SRV_USER": "hq", "SRV_PORT": "22",
               "SRV_HOST_KEY": "ssh-ed25519 synthetic"}
    machines = [{"key": " server ", "connections": ["absent", "srv"], "request_id": "r1"}]
    infra = {"provider": "local", "surface": "glance", "env": servers,
             "plan": {"panels": ["infrastructure", "news"], "targets": {"infrastructure": machines}}}
    yield {**infra, "commands": {"srv python3 -": {"stdout": host}}}
    yield {**infra, "commands": {"srv python3 -": {"stdout": json.dumps({"memory_used": "2048", "load_1m": "1.5", "cores": 4.9})}}}
    yield {**infra, "commands": {"srv python3 -": {"stdout": "not json"}}}
    yield {**infra, "commands": {"srv python3 -": {"exit": 1}}}
    yield {**infra, "plan": {"panels": ["infrastructure"], "targets": {"infrastructure": [
        *machines, {"key": "nas", "connections": ["unknown"], "request_id": "r2"}]}},
        "commands": {"srv python3 -": {"stdout": host}}}

    base = "https://portainer.example.invalid/api"
    docker = {"PORTAINER_CONNECTION_REF": "docker", "PORTAINER_URL": base}
    environments = [
        {"id": 1, "name": "edge-docker", "reachable": True, "local": False},
        {"id": 2, "name": "local", "reachable": True, "local": True},
        {"id": 3, "name": "gone", "reachable": False, "local": False},
    ]
    stats = {"cpu_stats": {"cpu_usage": {"total_usage": 2_000, "percpu_usage": [1, 2]}, "system_cpu_usage": 20_000},
             "precpu_stats": {"cpu_usage": {"total_usage": 1_000}, "system_cpu_usage": 10_000},
             "memory_stats": {"usage": 300 * 2**20, "stats": {"inactive_file": 100 * 2**20}}}
    routes = {}
    for environment in environments[:2]:
        prefix = f"{base}/endpoints/{environment['id']}/docker"
        routes[f"{prefix}/info"] = {"NCPU": 4, "MemTotal": 8 * 2**30}
        routes[f"{prefix}/system/df"] = {"LayersSize": 2**30, "Volumes": [{"UsageData": {"Size": 5 * 2**20}}, {}],
                                         "BuildCache": [{"Size": 7}]}
        routes[f"{prefix}/containers/json?all=false"] = [{"Id": "c1"}, {"Id": "c2"}]
        for container in ("c1", "c2"):
            routes[f"{prefix}/containers/{container}/stats?stream=false&one-shot=true"] = stats
    targets = [{"key": "edge-docker", "connections": [], "request_id": "a"},
               {"key": "controller", "connections": [], "request_id": "b"},
               {"key": "ghost", "connections": [], "request_id": "c"}]
    portainer = {"provider": "local", "surface": "glance", "env": docker, "routes": routes,
                 "portainer": {"url": base, "headers": {"X-API-Key": "synthetic"}, "environments": environments},
                 "plan": {"panels": ["infrastructure", "weather"],
                          "targets": {"infrastructure": targets, "weather": {"point": "95,0"}}}}
    yield portainer
    yield {**portainer, "routes": {**routes, f"{base}/endpoints/1/docker/containers/json?all=false": [{"Name": "no id"}]}}
    yield {**portainer, "env": {}}


ZONE = {"id": "z1", "name": "Example.COM.", "account": {"id": "a1"}}
RULESET = {
    "rules": [
        {"id": "r1", "action": "redirect", "description": "apex",
         "expression": '(http.host eq "www.example.com") or (http.host in {"a.example.com" "B.example.com."}) '
                       'or http.request.full_uri contains "https://legacy.example.com/x"',
         "action_parameters": {"from_value": {"target_url": {"value": "https://Example.com/path"},
                                              "status_code": 301, "preserve_query_string": True}}},
        {"id": "r2", "action": "redirect", "enabled": False, "expression": 'http.host wildcard "*.example.com"',
         "action_parameters": {"from_value": {"target_url": {"expression": 'concat("https://dest.example.net", http.request.uri.path)'}}}},
        {"id": "r3", "action": "block", "expression": "true"},
        {"action": "redirect", "expression": 'http.host strict wildcard "*.*.example.com"'},
    ],
}
PAGE_RULES = [
    {"id": "p1", "status": "disabled", "targets": [{"target": "url", "constraint": {"value": "*.old.example.com/*"}},
                                                   {"target": "other", "constraint": {"value": "x.example.com"}}],
     "actions": [{"id": "forwarding_url", "value": {"url": "https://new.example.com/$2", "status_code": 302}}]},
    {"id": "p2", "actions": [{"id": "cache_level", "value": "bypass"}]},
    {"id": "p3", "targets": [{"target": "url", "constraint": {"value": "http://Plain.example.com:8080/"}}],
     "actions": [{"id": "forwarding_url"}]},
    "junk",
]


def redirect_fixtures():
    routes = {
        "/zones/z1/rulesets": [{"id": "rs1", "phase": "http_request_dynamic_redirect"}, {"id": "rs2", "phase": "other"}],
        "/zones/z1/rulesets/rs1": RULESET,
        "/zones/z1/pagerules": PAGE_RULES,
    }
    base = {"provider": "local", "surface": "redirects", "refs": ["cf"], "routes": routes}
    yield {**base, "zones": {"cf": [ZONE, {"id": "z2", "name": ""}]}}
    yield {**base, "zones": {"cf": [ZONE]}, "failures": {"/zones/z1/pagerules": "Page rules refused."}}
    yield {**base, "zones": {"cf": [ZONE]}, "failures": {"/zones/z1/pagerules": "Page rules refused.",
                                                         "/zones/z1/rulesets": "Rulesets refused."}}
    yield {**base, "zones": {"cf": []}}
    yield {**base, "refs": ["cf", "other"], "zones": {"cf": [ZONE], "other": [{"id": "z9", "name": "other.example"}]},
           "routes": {**routes, "/zones/z9/rulesets": [], "/zones/z9/pagerules": None}}


ADGUARD = {"ADGUARD_CONNECTION_REF": "dns", "ADGUARD_URL": "https://adguard.example.invalid",
           "ADGUARD_USERNAME": "user", "ADGUARD_PASSWORD": "synthetic"}
SSH_PAIR = {
    "SRV_CONNECTION_REF": "srv", "SRV_HOST": "192.0.2.10", "SRV_USER": "hq", "SRV_PORT": "22", "SRV_HOST_KEY": "k",
    "SRV2_CONNECTION_REF": "srv2", "SRV2_HOST": "192.0.2.11", "SRV2_USER": "hq", "SRV2_PORT": "2222", "SRV2_HOST_KEY": "k",
    "SRV2_PROVIDER": "cpanel",
}


def dispatch_fixtures():
    import django

    django.setup()
    from application.controller import controller_registry

    registry = json.loads(json.dumps(controller_registry()))
    status = "https://adguard.example.invalid/control/status"
    connections = {
        "provider": "local", "surface": "connections", "registry": registry,
        "env": {**ADGUARD, **SSH_PAIR, "ADGUARD_MANAGES": "yes", "ADGUARD_STORE_VAULT": "Infra", "ADGUARD_STORE_ITEM": "dns",
                "MYSTERY_CONNECTION_REF": "mystery", "MYSTERY_URL": "https://mystery.example.invalid"},
        "routes": {status: {"version": "v0.107", "dns_addresses": ["192.0.2.53"]}},
        "commands": {"srv2 preflight": {"stdout": ""}},
    }
    yield {**connections, "carry": ["srv"]}
    yield {**connections, "carry": [], "commands": {"srv preflight": {"exit": 255}, "srv2 preflight": "missing"},
           "routes": {}, "failures": {status: "Provider request was refused."}}

    rewrite = "https://adguard.example.invalid/control/rewrite/list"
    resource = {"key": "rewrite", "kind": "adguard.rewrite", "generation": 1, "enabled": True,
                "spec": {"domain": "example.test", "answer": "192.0.2.1"}, "observed": None}
    execute = {"provider": "local", "surface": "execute", "registry": registry, "env": ADGUARD,
               "action": "reconcile", "resource": resource, "routes": {rewrite: []}}
    yield {**execute, "apply": False}
    yield {**execute, "apply": True}
    yield {**execute, "apply": True, "env": {**ADGUARD, "ADGUARD_MANAGES": "true"},
           "routes": {rewrite: [], "https://adguard.example.invalid/control/rewrite/add": None}}
    yield {**execute, "apply": True, "env": {}}
    locked = next(entry for entry in registry["locked"])
    yield {**execute, "apply": True, "action": locked["action"], "resource": {**resource, "kind": locked["kind"]}}
    yield {**execute, "apply": False, "action": "reconcile", "resource": {**resource, "kind": "unknown.kind"}}
    # A named connection of another provider passes no manages check, even one that manages.
    managed = {**ADGUARD, **EDGE, "ADGUARD_MANAGES": "true", "EDGE_MANAGES": "true"}
    yield {**execute, "apply": True, "env": managed,
           "resource": {**resource, "spec": {**resource["spec"], "connection_ref": "edge"}}}
    yield {**execute, "apply": True, "env": managed,
           "resource": {**resource, "spec": {**resource["spec"], "connection_ref": "nowhere"}}}
    # A kind the registry declares no connection for is refused, not waved through.
    yield {**execute, "apply": True, "env": managed, "undeclared": ["adguard.rewrite"]}
    yield {**execute, "apply": False, "env": managed, "undeclared": ["adguard.rewrite"]}
    # Two items carrying one ref name neither.
    yield {**execute, "apply": True, "env": {**managed, "DNS2_CONNECTION_REF": "dns", "DNS2_PROVIDER": "adguard"},
           "routes": {rewrite: [], "https://adguard.example.invalid/control/rewrite/add": None}}
    # NPM writes go through the connection the spec names, which is the one the gate checked.
    npm_env = {
        "NPM_CONNECTION_REF": "proxy", "NPM_URL": "https://npm.example.invalid", "NPM_USERNAME": "user",
        "NPM_PASSWORD": "synthetic",
        "NPM_HOME_CONNECTION_REF": "proxy-home", "NPM_HOME_PROVIDER": "npm", "NPM_HOME_MANAGES": "true",
        "NPM_HOME_URL": "https://npm-home.example.invalid", "NPM_HOME_USERNAME": "user", "NPM_HOME_PASSWORD": "synthetic",
    }
    npm_spec = {
        "domain_names": ["a.example.test"], "forward_scheme": "http", "forward_host": "192.0.2.5", "forward_port": 80,
        "force_ssl": False, "http2": False, "websocket": False, "caching_enabled": False, "block_exploits": True,
        "access_list_id": 0, "certificate_id": 0, "advanced_config": "", "hsts_enabled": False,
        "hsts_subdomains": False, "trust_forwarded_proto": False, "serving": True, "connection_ref": "proxy-home",
    }
    home = "https://npm-home.example.invalid/api"
    npm_resource = {"key": "proxy", "kind": "npm.proxy_host", "generation": 1, "enabled": True, "spec": npm_spec,
                    "observed": None}
    for action in ("reconcile", "delete"):
        yield {**execute, "apply": True, "env": npm_env, "action": action, "resource": npm_resource,
               "routes": {f"{home}/tokens": {"token": "synthetic"}, f"{home}/nginx/proxy-hosts": [],
                          **({f"{home}/nginx/proxy-hosts": []} if action == "delete" else {})}}
    yield {**execute, "apply": True, "env": npm_env, "action": "reconcile",
           "resource": {**npm_resource, "spec": {**npm_spec, "connection_ref": "proxy"}}}

    inventory = {"provider": "local", "surface": "inventory", "registry": registry,
                 "only": ["adguard.rewrite", "host.firewall", "host.perimeter", "tailscale.device"]}
    yield {**inventory, "env": {}}
    yield {**inventory, "env": {**ADGUARD, **EDGE}, "firewall": {"tailnet_only": True},
           "routes": {rewrite: [{"domain": "example.test", "answer": "192.0.2.1"}]},
           "commands": {"edge perimeter": {"stdout": json.dumps({"public_addresses": "", "firewall_unit": "active"})}}}
    yield {**inventory, "env": ADGUARD, "failures": {rewrite: "Provider request was refused."}}
    # Failure text reaches a report cut to its limit, by characters.
    yield {**inventory, "env": ADGUARD, "failures": {rewrite: "é" * 300 + "x" * 300}}
    yield {**connections, "carry": ["srv"], "routes": {}, "failures": {status: "refused: " + "é" * 600}}


PORTAINER_ENDPOINTS = [
    {"Id": 1, "Name": "local", "URL": "unix:///var/run/docker.sock", "Type": 1, "Status": 1,
     "Agent": {"Version": "2.19.4"},
     "Snapshots": [{"DockerVersion": "26.0"}, {"DockerVersion": "27.1", "RunningContainerCount": 3, "ContainerCount": 4, "Time": 1767355200}]},
    {"Id": 2, "Name": "edge-vps", "URL": "tcp://192.0.2.20:9001", "Type": 2, "Status": 1, "Snapshots": []},
    {"Id": 3, "Name": "down-box", "URL": "tcp://192.0.2.30:9001", "Type": 9, "Status": 2, "Snapshots": ["not a snapshot"]},
]
PORTAINER_STACKS = [
    {"Id": 10, "Name": "web", "EndpointId": 1, "Status": 1, "EntryPoint": "docker-compose.yml", "ProjectPath": "/data/compose/10"},
    {"Id": 11, "Name": "orphan", "EndpointId": 1, "Status": 2, "ProjectPath": "/data/compose/11"},
    {"Id": 12, "Name": "web", "EndpointId": 2, "Status": 1},
    {"Id": 13, "Name": "", "EndpointId": 1},
]
PORTAINER_LOCAL = [
    {"Id": "abcdef0123456789", "Names": ["/web-app-1"], "Image": "ghcr.io/example/web:1", "ImageID": "sha256:img1",
     "Labels": {"com.docker.compose.project": "web", "com.docker.compose.project.working_dir": "/srv/web",
                "com.docker.compose.project.config_files": "/srv/web/compose.yml, /srv/web/override.yml",
                "com.docker.compose.service": "app",
                "org.opencontainers.image.source": "https://example.invalid/web", "org.opencontainers.image.revision": "abc123"},
     "State": "running", "Status": "Up 2 hours",
     "Ports": [{"IP": "0.0.0.0", "PrivatePort": 80, "PublicPort": 8080, "Type": "tcp"}, {"IP": "::", "PrivatePort": 80, "PublicPort": 8080}],
     "HostConfig": {"NetworkMode": "web_default"}, "NetworkSettings": {"Networks": {"web_default": {}, "proxy": {}}},
     "Mounts": [{"Type": "volume", "Name": "web_data", "Destination": "/data", "RW": True},
                {"Type": "bind", "Source": "/srv/web/conf", "Destination": "/conf", "RW": False}]},
    {"Id": "1234567890abcdef", "Names": ["/web-db-1"], "Image": "postgres:16", "ImageID": "sha256:img2",
     "Labels": {"com.docker.compose.project": "web", "com.docker.compose.service": "db"},
     "State": "running", "Status": "Up", "Ports": [{"IP": "127.0.0.1", "PublicPort": 5432}, {"PrivatePort": 9999}],
     "NetworkSettings": {"Networks": {"web_default": {}}},
     "Mounts": [{"Type": "bind", "Source": "/srv/web/conf", "Destination": "/etc/db", "RW": True}, {"Type": "volume", "Destination": "/anon"}]},
    {"Id": "ffff0000ffff0000", "Names": ["/hq-controller"], "Labels": {"severino-hq.run": "nonce-1"}, "State": "running",
     "Ports": [{"IP": "0.0.0.0", "PublicPort": 9100}]},
    {"Id": "eeee0000eeee0000", "Names": ["/loose"], "Image": "busybox", "State": "exited", "Status": "Exited (0)",
     "HostConfig": {"NetworkMode": "host"}},
]
PORTAINER_EDGE = [
    {"Id": "2222000022220000", "Names": ["/web-app-1"], "Labels": {"com.docker.compose.project": "web"}, "State": "running",
     "Ports": [{"IP": "0.0.0.0", "PublicPort": 443}, {"IP": "0.0.0.0", "PublicPort": 80}]},
]
PORTAINER_LONG_PROFILE = "seccomp=" + "{" + "\"defaultAction\": \"SCMP_ACT_ERRNO\", " * 3 + "}"
PORTAINER_DOCKER = {
    "/api/endpoints/1/docker/containers/json?all=1": PORTAINER_LOCAL,
    "/api/endpoints/2/docker/containers/json?all=1": PORTAINER_EDGE,
    "/api/endpoints/1/docker/networks": [
        {"Id": "n1", "Name": "web_default", "Driver": "bridge", "Scope": "local", "Internal": False,
         "IPAM": {"Config": [{"Subnet": "172.20.0.0/16"}, {"Gateway": "172.20.0.1"}]}},
        {"Id": "n2", "Name": "proxy", "Driver": "bridge", "Internal": True, "IPAM": {"Config": None}},
        {"Name": ""},
    ],
    "/api/endpoints/2/docker/networks": [{"Id": "n9", "Name": "host", "Driver": "host", "Scope": "local"}],
    "/api/endpoints/1/docker/volumes": {"Volumes": [
        {"Name": "web_data", "Driver": "local", "Mountpoint": "/var/lib/docker/volumes/web_data/_data",
         "Labels": {"com.docker.compose.project": "web"}, "CreatedAt": "2026-01-01T00:00:00Z"},
        {"Name": "unused", "Driver": "local"},
    ]},
    "/api/endpoints/2/docker/volumes": {"Volumes": None},
    "/api/endpoints/1/docker/images/json": [
        {"Id": "sha256:img1", "RepoTags": ["ghcr.io/example/web:1", "<none>:<none>"],
         "RepoDigests": ["ghcr.io/example/web@sha256:d1", "<none>@<none>"], "Created": 1767355200, "Size": 1234},
        {"Id": "sha256:img2", "RepoTags": None, "Created": "not a time", "Size": 1.5},
        {"Id": ""},
    ],
    "/api/endpoints/2/docker/images/json": [],
    "/api/endpoints/1/docker/containers/abcdef0123456789/json": {
        "Image": "sha256:img1",
        "Config": {"User": "1000:1000", "Labels": {"com.docker.compose.project": "web", "com.docker.compose.service": "app"},
                   "ExposedPorts": {"80/tcp": {}, "443/tcp": {}, "abc/udp": {}}, "Healthcheck": {"Test": ["CMD", "true"]}},
        "HostConfig": {"Privileged": False, "ReadonlyRootfs": True, "NetworkMode": "web_default",
                       "CapAdd": ["NET_ADMIN", ""], "CapDrop": ["ALL"],
                       "SecurityOpt": ["no-new-privileges:true", PORTAINER_LONG_PROFILE],
                       "Devices": [{"PathOnHost": "/dev/fuse"}, "not a device"],
                       "PortBindings": {"80/tcp": [{"HostIp": "", "HostPort": "8080"}], "22/tcp": None},
                       "Memory": 536870912, "NanoCpus": 1500000000, "PidsLimit": 200, "RestartPolicy": {"Name": "unless-stopped"}},
        "State": {"Health": {"Status": "healthy"}, "StartedAt": "2026-01-02T10:00:00Z"},
        "Mounts": [{"Type": "volume", "Name": "web_data", "Destination": "/data", "RW": True},
                   {"Type": "bind", "Source": "/srv/web/conf", "Destination": "/conf", "RW": False}, "not a mount"],
        "RestartCount": 2,
    },
    "/api/endpoints/1/docker/containers/1234567890abcdef/json": {
        "Image": "sha256:img2", "Config": {"Healthcheck": {"Test": ["NONE"]}}, "HostConfig": {"NanoCpus": 125000000},
    },
    "/api/endpoints/1/docker/containers/ffff0000ffff0000/json": {"Config": {"Labels": {"severino-hq.run": "nonce-1"}}},
    "/api/endpoints/1/docker/containers/eeee0000eeee0000/json": None,
    "/api/endpoints/2/docker/containers/2222000022220000/json": {"Config": {"Labels": {"com.docker.compose.project": "web"}}},
}
PORTAINER_ROUTES = {"/api/endpoints": PORTAINER_ENDPOINTS, "/api/stacks": PORTAINER_STACKS, **PORTAINER_DOCKER}
PORTAINER_SPEC = {
    "host": "local", "name": "web", "compose": "services: {}",
    "environment": [{"name": "A", "value": "1"}, {"name": "B"}], "port": 8080,
}


def portainer_case(surface, routes=None, **extra):
    return {"provider": "portainer", "surface": surface, "routes": PORTAINER_ROUTES if routes is None else routes, **extra}


def portainer_mutation_fixtures():
    same = {**PORTAINER_ROUTES, "/api/stacks/10/file": {"StackFileContent": "services: {}"}}
    drifted = {**PORTAINER_ROUTES, "/api/stacks/10/file": {"StackFileContent": "services: {old: {}}"}}
    for apply in (False, True):
        yield portainer_case("reconcile", same, apply=apply, spec=PORTAINER_SPEC)
        yield portainer_case("reconcile", drifted, apply=apply, spec=PORTAINER_SPEC)
        yield portainer_case("reconcile", apply=apply, spec={**PORTAINER_SPEC, "name": "fresh"})
        yield portainer_case("reconcile", {**PORTAINER_ROUTES, "/api/stacks/12/file": {}}, apply=apply,
                        spec={**PORTAINER_SPEC, "host": "192.0.2.20", "port": None, "connection_ref": PORTAINER_REF})
    yield portainer_case("reconcile", same, apply=True, spec={**PORTAINER_SPEC, "host": "hq-node"})
    yield portainer_case("reconcile", same, apply=True, spec={**PORTAINER_SPEC, "host": "hq-node"}, controller_id="elsewhere")
    yield portainer_case("reconcile", same, apply=True, spec={**PORTAINER_SPEC, "host": "down-box"})
    yield portainer_case("reconcile", same, apply=True, spec={**PORTAINER_SPEC, "host": "it's-nowhere"})
    yield portainer_case("reconcile", {**same, "/api/stacks": [*PORTAINER_STACKS, {"Id": 14, "Name": "web", "EndpointId": 1.0}]},
                    apply=True, spec=PORTAINER_SPEC)
    yield portainer_case("reconcile", same, apply=True, spec=PORTAINER_SPEC, statuses={"/api/stacks/10/file": 403})
    yield portainer_case("reconcile", drifted, apply=True, spec=PORTAINER_SPEC, statuses={"/api/stacks/10?endpointId=1": 500})
    yield portainer_case("reconcile", same, apply=True, spec=PORTAINER_SPEC, statuses={"/api/endpoints": 401})
    for apply in (False, True):
        yield portainer_case("delete", apply=apply, spec=PORTAINER_SPEC)
        yield portainer_case("delete", apply=apply, spec={**PORTAINER_SPEC, "name": "fresh"})
    for verb in ("restart", "start", "stop"):
        for apply in (False, True):
            yield portainer_case(verb, apply=apply, spec={"host": "local", "name": "web-app-1"})
    yield portainer_case("stop", apply=True, spec={"host": "local", "name": "loose"})
    yield portainer_case("start", apply=True, spec={"host": "local", "name": "ghost"})
    yield portainer_case("restart", apply=True, spec={"host": "local", "name": "web-app-1"},
                    statuses={"/api/endpoints/1/docker/containers/abcdef0123456789/restart": 403})


def portainer_read_fixtures():
    for run in ("", "nonce-1"):
        yield portainer_case("inventory", run=run)
    yield portainer_case("inventory", controller_id="")
    yield portainer_case("inventory", statuses={"/api/endpoints": 401})
    yield portainer_case("inventory", statuses={"/api/endpoints/2/docker/containers/json?all=1": 403})
    yield portainer_case("probe")
    yield portainer_case("probe", controller_id="")
    yield portainer_case("probe", statuses={"/api/endpoints": 403})
    yield portainer_case("environments")
    yield portainer_case("environments", resolves="192.0.2.5")
    for code in (401, 403, 500):
        yield portainer_case("environments", statuses={"/api/endpoints": code})
    yield portainer_case("environments", {"/api/endpoints": None})
    yield portainer_case("environments", {"/api/endpoints": [{"Id": 4.0, "Name": 7, "URL": 5, "Type": True, "Status": True,
                                                          "Snapshots": [{"Time": "1767355200", "ContainerCount": 1.5}]}]})
    for surface in ("networks", "volumes", "images", "runtime", "stacks"):
        yield portainer_case(surface)
        yield portainer_case(surface, run="nonce-1")
        yield portainer_case(surface, statuses={"/api/endpoints/2/docker/containers/json?all=1": 403})
        yield portainer_case(surface, statuses={
            "/api/endpoints/1/docker/containers/json?all=1": 401,
            "/api/endpoints/2/docker/containers/json?all=1": 403,
        })
    yield portainer_case("runtime", statuses={"/api/endpoints/1/docker/containers/1234567890abcdef/json": 500})
    yield portainer_case("networks", statuses={"/api/endpoints": 403})
    yield portainer_case("stacks", statuses={"/api/stacks": 403})


def portainer_reach_fixtures():
    status = json.dumps(TAILNET_STATUS)
    acl = {"/tailnet/-/acl": TAILNET_POLICY}
    devices = {"/tailnet/-/devices?fields=all": TAILNET_DEVICES}
    previews = {**TAILNET_PREVIEWS, "/tailnet/-/acl/preview?type=ipport&previewFor=192.0.2.1%3A8080": {
        "matches": [{"users": ["operator@example.com"], "ports": ["tag:server:8080"], "lineNumber": 9}],
    }}
    for run in ("", "nonce-1"):
        yield tailnet("device_inventory", {**devices, **acl, **previews, **PORTAINER_ROUTES},
                      tailnet_status=status, portainer=True, run=run)
    yield tailnet("device_inventory", {**devices, **acl, **previews, **PORTAINER_ROUTES},
                  tailnet_status=status, portainer=True, statuses={"/api/endpoints": 403})


def envelope(result, **info):
    body = {"success": True, "errors": [], "messages": [], "result": result}
    if info:
        body["result_info"] = info
    return body


def cf(surface, routes=None, **extra):
    return {"provider": "cloudflare", "surface": surface, "routes": routes or {}, **extra}


CF_ZONES = [
    {"id": "z1", "name": "example.com", "status": "active", "account": {"id": "acc1", "name": "Account"}, "plan": {"id": "free", "name": "Free Website"}},
    {"id": "z2", "name": "Example.NET.", "status": "pending", "account": {"id": "acc1"}, "plan": {}},
    {"id": "z3", "name": ""},
]
CF_RECORDS = [
    {"id": "r1", "type": "A", "name": "example.com", "content": "192.0.2.1", "proxied": True, "ttl": 1},
    {"id": "r2", "type": "MX", "name": "example.com", "content": "mail.example.com", "priority": 10, "ttl": 3600},
    {"id": "r3", "type": "TXT", "name": "example.com", "content": "\"v=spf1 -all\"", "ttl": 1},
    {"id": "r4", "type": "CAA", "name": "example.com", "content": "0 issue \"letsencrypt.org\"", "ttl": 1,
     "data": {"flags": 0, "tag": "issue", "value": "letsencrypt.org"}},
    {"id": "r5", "type": "", "name": "blank.example.com"},
]
CF_DNS = {
    "/zones?per_page=100&page=1": envelope(CF_ZONES),
    "/zones/z1/dns_records?per_page=100&page=1": envelope(CF_RECORDS),
}
CF_ACCOUNTS = {"/accounts?per_page=50&page=1": envelope([{"id": "acc1", "name": "Account"}])}
CF_API_ZONES = {"/zones?per_page=50&page=1": envelope(CF_ZONES)}
CF_VERIFY = "/user/tokens/verify"
CF_ACTIVE = {CF_VERIFY: envelope({"id": "tok", "status": "active", "expires_on": "2027-01-01T00:00:00Z"})}
CF_AUTH = {"errors": [{"code": 10000, "message": "Authentication error"}]}
CF_RECORD_SPECS = {
    "A": {"zone": "Example.com.", "name": "example.com", "record_type": "a", "content": "192.0.2.1", "ttl": 1, "proxied": True},
    "MX": {"zone": "example.com", "name": "example.com", "record_type": "MX", "content": "mail.example.com", "ttl": 3600, "priority": 10},
    "TXT": {"zone": "example.com", "name": "example.com", "record_type": "TXT", "content": "\"v=spf1 -all\"", "ttl": 1},
    "CAA": {"zone": "example.com", "name": "example.com", "record_type": "CAA", "content": "0 issue \"letsencrypt.org\"", "ttl": 1},
}


def cloudflare_record_fixtures():
    created = {"id": "r9", "type": "A", "name": "new.example.com", "content": "192.0.2.9", "proxied": False, "ttl": 1}
    for kind, spec in CF_RECORD_SPECS.items():
        yield cf("reconcile", CF_DNS, apply=True, spec=spec)
    changes = (
        ({**CF_RECORD_SPECS["A"], "proxied": False}, "/zones/z1/dns_records/r1", None),
        ({**CF_RECORD_SPECS["MX"], "priority": 20}, "/zones/z1/dns_records/r2", None),
        ({**CF_RECORD_SPECS["A"], "ttl": 300}, "/zones/z1/dns_records/r1", None),
        ({**CF_RECORD_SPECS["CAA"], "content": "0 issue \"pki.goog\""}, "/zones/z1/dns_records/r4", {"record_id": "r4"}),
        ({**CF_RECORD_SPECS["A"], "content": "192.0.2.2"}, "/zones/z1/dns_records/r1", {"record_id": "r1"}),
        ({**CF_RECORD_SPECS["A"], "name": "new.example.com", "content": "192.0.2.9", "proxied": False}, "/zones/z1/dns_records", None),
        ({**CF_RECORD_SPECS["TXT"], "content": "\"v=spf1 include:example.net -all\""}, "/zones/z1/dns_records", {"record_id": "gone"}),
    )
    for spec, path, observed in changes:
        for apply in (False, True):
            answer = {**created, "content": spec["content"]} if path.endswith("dns_records") else {"id": "r1", "type": spec["record_type"].upper(), "name": spec["name"], "content": spec["content"]}
            yield cf("reconcile", CF_DNS, apply=apply, spec=spec, observed=observed,
                     answers={path: envelope(answer), "/zones/z1/dns_records/r4": envelope({"id": "r4", "type": "CAA", "name": "example.com", "data": {"flags": 0, "tag": "issue", "value": "pki.goog"}})})
    yield cf("reconcile", CF_DNS, apply=True, spec={**CF_RECORD_SPECS["A"], "content": "192.0.2.9"})
    yield cf("reconcile", CF_DNS, apply=True, spec={**CF_RECORD_SPECS["CAA"], "content": "bogus"})
    yield cf("reconcile", CF_DNS, apply=True, spec={**CF_RECORD_SPECS["A"], "zone": "missing.example"})
    yield cf("reconcile", CF_DNS, apply=True, spec={**CF_RECORD_SPECS["A"], "content": "192.0.2.9"},
             write_statuses={"/zones/z1/dns_records": 400},
             error_bodies={"/zones/z1/dns_records": {"errors": [{"code": 81057, "message": "An identical record already exists."}, {"message": " "}, {"code": 1}]}})
    yield cf("reconcile", CF_DNS, apply=True, spec={**CF_RECORD_SPECS["A"], "content": "192.0.2.9"},
             write_statuses={"/zones/z1/dns_records": 500})
    read = "/zones/z1/dns_records?per_page=100&page=1"
    yield cf("reconcile", {**CF_DNS, **CF_ACTIVE}, apply=True, spec=CF_RECORD_SPECS["A"],
             statuses={read: 401}, error_bodies={read: CF_AUTH})
    yield cf("reconcile", CF_DNS, apply=True, spec=CF_RECORD_SPECS["A"],
             statuses={read: 401, CF_VERIFY: 401}, error_bodies={read: CF_AUTH})
    yield cf("reconcile", CF_DNS, apply=True, spec=CF_RECORD_SPECS["A"], statuses={read: 403}, error_bodies={read: CF_AUTH})
    yield cf("reconcile", CF_DNS, apply=True, spec=CF_RECORD_SPECS["A"], statuses={read: 400},
             error_bodies={read: {"errors": [{"message": "Invalid API Token"}]}})
    yield cf("reconcile", {**CF_DNS, read: {"success": False, "errors": [{"message": "Missing permission"}]}},
             apply=True, spec=CF_RECORD_SPECS["A"])
    yield cf("reconcile", CF_DNS, apply=True, spec=CF_RECORD_SPECS["A"], network=["/zones?per_page=100&page=1"])

    for apply in (False, True):
        yield cf("delete", CF_DNS, apply=apply, spec=CF_RECORD_SPECS["MX"], answers={"/zones/z1/dns_records/r2": envelope({"id": "r2"})})
        yield cf("delete", CF_DNS, apply=apply, spec={**CF_RECORD_SPECS["A"], "content": "192.0.2.7"}, observed={"record_id": "r1"},
                 answers={"/zones/z1/dns_records/r1": envelope({"id": "r1"})})
    yield cf("delete", CF_DNS, apply=True, spec={**CF_RECORD_SPECS["A"], "content": "192.0.2.7"})
    yield cf("delete", CF_DNS, apply=True, spec=CF_RECORD_SPECS["MX"], write_statuses={"/zones/z1/dns_records/r2": 404},
             error_bodies={"/zones/z1/dns_records/r2": {"errors": [{"message": "Record does not exist."}]}})
    yield cf("delete", CF_DNS, apply=True, spec={**CF_RECORD_SPECS["A"], "zone": "missing.example"})


def cloudflare_zone_fixtures():
    registrations = "/accounts/acc1/registrar/registrations?per_page=50"
    posture = {}
    for zone_id, values in (("z1", ("strict", "1.2", "on", "off", "")), ("z2", ("flexible", None, "zrt", 1, False))):
        for setting, value in zip(cloudflare.ZONE_POSTURE_SETTINGS, values):
            posture[f"/zones/{zone_id}/settings/{setting}"] = envelope({"id": setting, "value": value, "editable": True})
    routes = {
        **CF_DNS, **CF_ACCOUNTS, **posture,
        registrations: envelope([
            {"domain_name": "example.com", "expires_at": "2027-03-01T00:00:00Z", "auto_renew": True, "locked": True, "status": "active"},
            {"domain_name": "", "status": "active"},
        ], cursor="next/page", per_page=50),
        f"{registrations}&cursor=next%2Fpage": envelope([
            {"domain_name": "Example.NET.", "auto_renew": False, "status": "expiring"},
        ]),
    }
    yield cf("zones", routes)
    yield cf("zones", routes, statuses={registrations: 403, "/zones/z2/settings/min_tls_version": 403},
             error_bodies={registrations: CF_AUTH})
    yield cf("zones", {**routes, **CF_ACTIVE}, statuses={registrations: 401}, error_bodies={registrations: CF_AUTH})
    yield cf("zones", routes, statuses={"/accounts?per_page=50&page=1": 401, CF_VERIFY: 401},
             error_bodies={"/accounts?per_page=50&page=1": CF_AUTH})
    yield cf("zones", {**routes, "/accounts?per_page=50&page=1": envelope([{"id": "acc1"}, {"id": "acc2"}])})
    yield cf("zones", routes, statuses={"/zones?per_page=100&page=1": 403}, error_bodies={"/zones?per_page=100&page=1": CF_AUTH})
    yield cf("records", {**CF_DNS, "/zones/z2/dns_records?per_page=100&page=1": envelope([
        {"id": "r6", "type": "aaaa", "name": "v6.example.net", "content": "2001:db8::1", "ttl": 300},
        {"id": "r7", "type": "CNAME", "name": "www.example.net", "content": "example.net", "proxied": True},
    ])})
    many = [{"id": f"p{index}", "type": "TXT", "name": f"t{index}.example.com", "content": "x"} for index in range(100)]
    yield cf("records", {
        "/zones?per_page=100&page=1": envelope(CF_ZONES[:1]),
        "/zones/z1/dns_records?per_page=100&page=1": envelope(many),
        "/zones/z1/dns_records?per_page=100&page=2": envelope(CF_RECORDS[:1]),
    })


def cloudflare_account_fixtures():
    pages = "/accounts/acc1/pages/projects?per_page=10"
    yield cf("pages", {**CF_ACCOUNTS,
        f"{pages}&page=1": envelope([{
            "name": "site", "subdomain": "site.pages.dev", "domains": ["example.com", "site.pages.dev"], "production_branch": "main",
            "canonical_deployment": {"id": "dep1", "created_on": "2026-01-01T00:00:00Z",
                                     "deployment_trigger": {"type": "push", "metadata": {"branch": "main", "commit_hash": "abcdef1234567", "commit_message": "x"}}},
        }], total_pages=2, page=1),
        f"{pages}&page=2": envelope([{"name": "bare"}], total_pages=2, page=2),
    })
    yield cf("pages", {**CF_ACCOUNTS, f"{pages}&page=1": envelope({"not": "a list"})})
    yield cf("pages", {**CF_ACCOUNTS, f"{pages}&page=1": envelope([{"name": "site"}, "junk"])})
    yield cf("pages", {"/accounts?per_page=50&page=1": envelope([])})

    d1 = "/accounts/acc1/d1/database?per_page=100&page=1"
    databases = envelope([
        {"uuid": "u1", "name": "db", "created_at": "2025-06-01T00:00:00Z", "version": "production"},
        {"uuid": "u2", "name": "db2"},
        {"uuid": "u3", "name": "db3"},
    ])
    yield cf("d1", {**CF_ACCOUNTS, d1: databases,
        "/accounts/acc1/d1/database/u1": envelope({"uuid": "u1", "file_size": 12345}),
        "/accounts/acc1/d1/database/u3": envelope({"uuid": "u3", "file_size": 1.5}),
    }, statuses={"/accounts/acc1/d1/database/u2": 403}, error_bodies={"/accounts/acc1/d1/database/u2": CF_AUTH})
    yield cf("d1", {**CF_ACCOUNTS, d1: databases}, statuses={"/accounts/acc1/d1/database/u1": 401, CF_VERIFY: 401},
             error_bodies={"/accounts/acc1/d1/database/u1": CF_AUTH})

    apps = "/accounts/acc1/access/apps?per_page=100&page=1"
    app_list = envelope([
        {"id": "app1", "name": "HQ", "type": "self_hosted", "domain": "hq.example.com", "session_duration": "24h",
         "destinations": [{"type": "public", "uri": "HQ.example.com/path"}, {"type": "private", "hostname": "db.example."},
                          {"type": "public", "uri": "hq.example.com"}, {"type": "private", "cidr": "10.0.0.0/8"}],
         "policies": [{"id": "pol1", "name": "staff", "include": [{"service_token": {"token_id": "tok1"}}]},
                      {"id": "pol2", "name": "any", "include": [{"any_valid_service_token": {}}]}]},
        {"id": "app2", "name": "Mail", "type": "saas", "policies": [{"id": "pol3", "include": [{"email": {"email": "a@example.com"}}]}]},
        {"id": "app3", "type": "self_hosted", "domain": "tool.example.com",
         "policies": [{"id": "pol4", "name": "ci", "include": [{"service_token": {"token_id": "tok2"}}]}]},
    ])
    yield cf("access_apps", {**CF_ACCOUNTS, apps: app_list})
    tokens = "/accounts/acc1/access/service_tokens?per_page=100&page=1"
    token_list = envelope([
        {"id": "tok1", "name": "ci", "client_id": "never-read", "expires_at": "2027-01-01T00:00:00Z", "created_at": "2025-01-01T00:00:00Z"},
        {"id": "tok2", "name": "deploy"},
        {"id": "tok3", "name": "unused"},
    ])
    yield cf("service_tokens", {**CF_ACCOUNTS, apps: app_list, tokens: token_list})
    yield cf("service_tokens", {**CF_ACCOUNTS, tokens: token_list}, statuses={apps: 403}, error_bodies={apps: CF_AUTH})
    yield cf("service_tokens", CF_ACCOUNTS, statuses={tokens: 403}, error_bodies={tokens: CF_AUTH})

    tunnels = "/accounts/acc1/cfd_tunnel?is_deleted=false&per_page=100&page=1"
    base = "/accounts/acc1/cfd_tunnel"
    yield cf("tunnels", {**CF_ACCOUNTS,
        tunnels: envelope([
            {"id": "t1", "name": "home", "status": "healthy", "created_at": "2025-01-01T00:00:00Z", "conns_active_at": "2026-01-02T00:00:00Z"},
            {"id": "t2", "name": "spare", "status": "inactive"},
            {"id": "t3", "name": "bare", "status": "down"},
        ]),
        f"{base}/t1/configurations": envelope({"tunnel_id": "t1", "source": "cloudflare", "config": {"ingress": [
            {"hostname": "hq.example.com", "service": "http://localhost:8000"}, {"service": "http_status:404"},
        ]}}),
        f"{base}/t1/connections": envelope([
            {"id": "c1", "version": "2026.1.0", "conns": [{"colo_name": "fra01", "origin_ip": "198.51.100.5", "client_version": "x"}]},
            {"id": "c2", "conns": [{"colo_name": "ams01", "client_version": "2025.9.0"}, {"colo_name": "lhr01"}]},
        ]),
        f"{base}/t3/configurations": envelope({"tunnel_id": "t3"}),
        f"{base}/t3/connections": envelope([]),
    }, statuses={f"{base}/t2/configurations": 403}, network=[f"{base}/t2/connections"])

    packs = "/zones/{}/ssl/certificate_packs?status=all&per_page=50&page=1"
    pack_list = envelope([
        {"id": "pk1", "type": "universal", "hosts": ["example.com", "*.example.com"], "status": "active", "certificate_authority": "google",
         "certificates": [{"id": "c1", "expires_on": "2026-04-01T00:00:00Z"}, {"id": "c2", "expires_on": "2026-03-01T00:00:00Z"}, {"id": "c3"}]},
        {"id": "pk2", "type": "advanced", "status": "pending_validation"},
    ])
    yield cf("edge_certificates", {**CF_API_ZONES, packs.format("z1"): pack_list},
             statuses={packs.format("z2"): 403}, error_bodies={packs.format("z2"): CF_AUTH})
    yield cf("edge_certificates", CF_API_ZONES, statuses={packs.format("z1"): 403, packs.format("z2"): 403},
             error_bodies={packs.format("z1"): CF_AUTH})
    yield cf("edge_certificates", {"/zones?per_page=50&page=1": envelope([])})

    rulesets = "/zones/{}/rulesets?per_page=50&page=1"
    redirect_routes = {
        **CF_API_ZONES,
        rulesets.format("z1"): envelope([
            {"id": "rs1", "phase": "http_request_dynamic_redirect", "kind": "zone", "name": "redirects"},
            {"id": "rs2", "phase": "http_request_firewall_custom", "kind": "zone", "name": "waf"},
        ]),
        "/zones/z1/rulesets/rs1": envelope({"id": "rs1", "phase": "http_request_dynamic_redirect", "rules": [
            {"id": "rule1", "action": "redirect", "description": "www", "expression": "(http.host eq \"www.example.com\") or (http.host in {\"a.example.com\" \"*.b.example.com\"})",
             "action_parameters": {"from_value": {"target_url": {"value": "https://example.com/"}, "status_code": 301, "preserve_query_string": True}}},
            {"id": "rule2", "action": "redirect", "enabled": False, "expression": "http.request.full_uri wildcard \"https://old.example.com/*\"",
             "action_parameters": {"from_value": {"target_url": {"expression": "wildcard_replace(http.request.full_uri, \"https://old.example.com/*\", \"https://new.example.com/${1}\")"}}}},
            {"id": "rule3", "action": "block", "expression": "true"},
        ]}),
        "/zones/z1/pagerules": envelope([
            {"id": "pr1", "status": "active", "targets": [{"target": "url", "constraint": {"operator": "matches", "value": "*.example.com/*"}}],
             "actions": [{"id": "forwarding_url", "value": {"url": "https://example.com/$2", "status_code": 301}}]},
            {"id": "pr2", "status": "disabled", "targets": [{"target": "url", "constraint": {"operator": "matches", "value": "http://Shop.Example.com:8080/x"}}],
             "actions": [{"id": "forwarding_url", "value": {"url": "https://shop.example.net", "status_code": 302}}]},
            {"id": "pr3", "status": "active", "targets": [], "actions": [{"id": "ssl", "value": "full"}]},
        ]),
        "/zones/z2/pagerules": envelope([]),
    }
    yield cf("redirects", redirect_routes, statuses={rulesets.format("z2"): 403}, error_bodies={rulesets.format("z2"): CF_AUTH})
    every = {path: 403 for path in (rulesets.format("z1"), rulesets.format("z2"), "/zones/z1/pagerules", "/zones/z2/pagerules")}
    yield cf("redirects", CF_API_ZONES, statuses=every)
    yield cf("redirects", CF_API_ZONES, statuses={"/zones?per_page=50&page=1": 401, CF_VERIFY: 401},
             error_bodies={"/zones?per_page=50&page=1": CF_AUTH})


CF_SITES = {"/accounts/acc1/rum/site_info/list?per_page=100&page=1": envelope([
    {"site_tag": "s2", "ruleset": {"zone_name": "Blog.Example.com."}},
    {"site_tag": "s1", "ruleset": {"zone_name": "example.com"}},
    {"site_tag": "s3", "ruleset": {}},
    {"ruleset": {"zone_name": "x.example"}},
])}


def cloudflare_probe_fixtures():
    zones = "/zones?per_page=100&page=1"
    yield cf("probe_dns", {**CF_ACTIVE, zones: envelope(CF_ZONES)})
    yield cf("probe_dns", {CF_VERIFY: envelope({"id": "tok", "status": "active"}), zones: envelope(None)})
    many = [{"id": f"z{n:03}", "name": f"zone{n:03}.example"} for n in range(101)]
    yield cf("probe_dns", {**CF_ACTIVE, zones: envelope(many[:100], total_pages=2), "/zones?per_page=100&page=2": envelope(many[100:], total_pages=2)})
    yield cf("probe_dns", {**CF_ACTIVE, zones: envelope(many[:100]), "/zones?per_page=100&page=2": envelope(many[100:])})
    yield cf("probe_dns", {CF_VERIFY: envelope({"id": "tok", "status": "active"}), zones: envelope([{"id": "z9", "name": "b.example"}, {"id": "z8", "name": "A.example"}, {"id": "z7"}])})
    yield cf("probe_dns", {**CF_ACTIVE, zones: {"success": False, "errors": [{"message": "Missing permission"}]}})
    yield cf("probe_dns", {zones: envelope(CF_ZONES)}, statuses={CF_VERIFY: 401}, error_bodies={CF_VERIFY: {"errors": [{"message": "Invalid API Token"}]}})
    yield cf("probe_api", {**CF_ACTIVE, **CF_ACCOUNTS, **CF_SITES})
    yield cf("probe_api", {**CF_ACTIVE, **CF_ACCOUNTS, "/accounts/acc1/rum/site_info/list?per_page=100&page=1": envelope([
        {"site_tag": "s1", "ruleset": {"zone_name": "example.com"}}])})
    yield cf("probe_api", {**CF_ACTIVE, "/accounts?per_page=50&page=1": envelope([{"id": "acc1"}, {"id": "acc2"}, {"name": "no id"}])})
    yield cf("probe_api", {**CF_ACTIVE, "/accounts?per_page=50&page=1": envelope(None)})
    yield cf("probe_api", {CF_VERIFY: {"success": False, "errors": []}})
    yield cf("probe_api", {CF_VERIFY: {"success": True, "result": None}, **CF_ACCOUNTS, **CF_SITES})


def cloudflare_analytics_fixtures():
    def group(**dimensions):
        return {"count": 12, "sum": {"visits": 5}, "avg": {"sampleInterval": 2}, "dimensions": {"date": "2025-12-01", **dimensions}}

    account = {
        "path": [group(requestPath="/"), group(requestPath=""), {"count": None, "dimensions": {"requestPath": "/x" * 300}}],
        "referrer": [group(refererHost="news.example")],
        "country": [group(countryName="DE")],
        "device": [group(deviceType="desktop")],
        "browser": [group(userAgentBrowser="Firefox")],
        "os": [{"dimensions": {"userAgentOS": "Linux"}}],
        "vitals": [
            {"dimensions": {"date": "2025-12-01"}, "avg": {"sampleInterval": 1},
             "quantiles": {"largestContentfulPaintP75": 2500500, "interactionToNextPaintP75": -1, "firstContentfulPaintP75": 1500,
                           "timeToFirstByteP75": None, "cumulativeLayoutShiftP75": 0.0512},
             "sum": {"lcpGood": 9, "lcpNeedsImprovement": 1, "lcpPoor": 0, "inpGood": 3, "clsPoor": 2}},
            {"quantiles": {}},
        ],
    }
    graphql = {"data": {"viewer": {"accounts": [account]}}}
    sites = {**CF_ACCOUNTS, **CF_SITES}
    windows = [
        {"connection_ref": "account", "site_tag": "s1", "start": "2025-12-01", "end": "2025-12-05", "reason": "backfill"},
        {"connection_ref": "account", "site_tag": "s2", "start": "2025-12-30", "end": "2026-01-02", "reason": "refresh"},
    ]
    yield cf("analytics", {**sites, "/graphql": graphql})
    yield cf("analytics", {**sites, "/graphql": graphql}, windows=windows)
    for window in (
        {"start": "2025-01-01", "end": "2025-12-31"},
        {"start": "2025-12-05", "end": "2025-12-01"},
        {"start": "not a date", "end": "2025-12-01"},
        {"start": "2025-09-30", "end": "2025-12-31"},
    ):
        yield cf("analytics", {**sites, "/graphql": graphql}, windows=[{**windows[0], **window}])
    yield cf("analytics", {**sites, "/graphql": {"data": {"viewer": {"accounts": []}}}})
    yield cf("analytics", {**sites, "/graphql": {"errors": [{"message": "quota exceeded"}], "data": None}})
    yield cf("analytics", {**sites, "/graphql": {"errors": ["not an object"]}})
    yield cf("analytics", sites, write_statuses={"/graphql": 401}, statuses={CF_VERIFY: 401}, error_bodies={"/graphql": CF_AUTH})
    yield cf("analytics", {**sites, **CF_ACTIVE}, write_statuses={"/graphql": 401}, error_bodies={"/graphql": CF_AUTH})
    yield cf("analytics", sites, network=["/graphql"])
    yield cf("analytics", {**CF_ACCOUNTS, "/accounts/acc1/rum/site_info/list?per_page=100&page=1": envelope([])})


def cloudflare_fixtures():
    yield from cloudflare_record_fixtures()
    yield from cloudflare_zone_fixtures()
    yield from cloudflare_account_fixtures()
    yield from cloudflare_probe_fixtures()
    yield from cloudflare_analytics_fixtures()


def tls_fixtures():
    """Issuance, verification, delivery, refusals and partial failures."""

    return list(tls_parity.tls_fixtures(tls_parity.generate_pki()))


def go_result(binary, fixture):
    answer = subprocess.run(
        [str(binary.resolve()), "-test.run=^TestParityChild$"],
        input=json.dumps(fixture), text=True, capture_output=True, check=True,
        env={"HQ_CONTROLLER_PARITY_CHILD": "1"}, timeout=10,
    )
    return json.loads(answer.stdout)


def both_results(binary, fixture, scratch, expect):
    """Each side gets the same fresh scratch tree at the same path."""

    if fixture.get("provider") != "tls":
        return expect(fixture), go_result(binary, fixture)
    root = Path(scratch, "s")
    fixture = tls_parity.with_scratch(fixture, root)
    tls_parity.materialize(fixture, root)
    expected = python_result(fixture)
    tls_parity.materialize(fixture, root)
    return expected, go_result(binary, fixture)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    options = parser.parse_args()
    groups = (
        ("AdGuard", [*mutation_fixtures(), *read_fixtures()], python_result),
        ("NPM", [*npm_mutation_fixtures(), *npm_read_fixtures()], python_result),
        ("Tailscale", [*tailscale_mutation_fixtures(), *tailscale_read_fixtures()], python_result),
        ("Portainer", [*portainer_mutation_fixtures(), *portainer_read_fixtures(), *portainer_reach_fixtures()], python_result),
        ("Cloudflare", [*cloudflare_fixtures()], python_result),
        ("TLS", tls_fixtures(), python_result),
        ("Host readings", list(host_fixtures()), local_result),
        ("Glance", list(glance_fixtures()), local_result),
        ("Redirects", list(redirect_fixtures()), local_result),
        ("Dispatch", list(dispatch_fixtures()), local_result),
    )
    with tempfile.TemporaryDirectory() as scratch:
        for name, fixtures, expect in groups:
            check_group(options.binary, scratch, name, fixtures, expect)


def check_group(binary, scratch, name, fixtures, expect):
    for index, fixture in enumerate(fixtures):
        expected, actual = both_results(binary, fixture, scratch, expect)
        if fixture.get("surface") == "inventory":
            # Providers are read at once, so the order requests interleave in is not part of the result.
            for side in (expected, actual):
                side["requests"] = sorted(side["requests"], key=lambda request: json.dumps(request, sort_keys=True))
        if actual != expected:
            raise AssertionError(
                f"{name} Fixture {index} ({fixture['surface']}) differs:\n"
                f"Python: {json.dumps(expected, sort_keys=True)}\n"
                f"Go: {json.dumps(actual, sort_keys=True)}"
            )
    print(f"{name} parity: {len(fixtures)} fixtures match results, requests and partial failures.")

if __name__ == "__main__":
    main()
