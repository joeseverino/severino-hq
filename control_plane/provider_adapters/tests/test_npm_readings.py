"""Nginx Proxy Manager readings from example payloads, kept only as each schema allows."""

from __future__ import annotations

import urllib.error
from types import SimpleNamespace

from django.test import SimpleTestCase

from control_plane.observations import OBSERVATIONS
from control_plane.provider_adapters.contracts import PERMISSION_REFUSAL, ProviderError
from control_plane.reading_parts import clean_refused_parts, refused_parts

from .. import npm_readings
from ..parts import part_ledger

CERTIFICATES = [
    {"id": 1, "nice_name": "example wildcard", "provider": "letsencrypt",
     "domain_names": ["*.example.com", "example.com"], "expires_on": "2030-01-01 00:00:00",
     "meta": {"certificate_key": "-----BEGIN PRIVATE KEY-----never stored"}},
    {"id": 2, "nice_name": "unused", "provider": "other", "domain_names": ["old.example.net"],
     "expires_on": "2030-02-01 00:00:00"},
]
PROXY_HOSTS = [
    {"id": 10, "domain_names": ["app.example.com"], "certificate_id": 1, "access_list_id": 7,
     "enabled": True},
    {"id": 11, "domain_names": ["off.example.com"], "certificate_id": 1, "enabled": False},
]
REDIRECTION_HOSTS = [
    {"id": 20, "domain_names": ["www.example.com"], "forward_scheme": "https",
     "forward_domain_name": "example.com", "forward_http_code": 301, "preserve_path": True,
     "certificate_id": 1, "ssl_forced": True, "enabled": True},
    {"id": 21, "domain_names": ["auto.example.com"], "forward_scheme": "auto",
     "forward_domain_name": "example.org", "forward_http_code": 302, "enabled": False},
]
DEAD_HOSTS = [{"id": 30, "domain_names": ["gone.example.com"], "certificate_id": 1,
               "enabled": True}]
STREAMS = [{"id": 40, "incoming_port": 2222, "forwarding_host": "192.0.2.30",
            "forwarding_port": 22, "tcp_forwarding": True, "udp_forwarding": False,
            "enabled": True, "meta": {}}]
ACCESS_LISTS = [
    {"id": 7, "name": "staff", "satisfy_any": True, "pass_auth": False,
     "items": [{"username": "operator", "password": ""}],
     "clients": [{"directive": "allow", "address": "100.64.0.0/10"},
                 {"directive": "deny", "address": "all"}]},
]

LISTS = {
    "/nginx/certificates": CERTIFICATES,
    "/nginx/proxy-hosts": PROXY_HOSTS,
    "/nginx/redirection-hosts": REDIRECTION_HOSTS,
    "/nginx/dead-hosts": DEAD_HOSTS,
    "/nginx/streams": STREAMS,
    "/nginx/access-lists?expand=items,clients": ACCESS_LISTS,
}


class _Runtime:
    """The runtime surface a reading uses, answering from ``LISTS``."""

    def __init__(self, refuse=(), refs=("example-npm",)):
        self.refuse = set(refuse)
        self.refs = tuple(refs)
        self.calls = []
        self.snapshot = {}

    def connection_refs(self, provider):
        return self.refs

    def connection_prefix(self, provider, connection_ref=""):
        return "NPM"

    def required(self, prefix, name):
        return {"URL": "https://npm.example.test", "USERNAME": "u", "PASSWORD": "p"}[name]

    def snapshot_value(self, key, load):
        if key not in self.snapshot:
            self.snapshot[key] = load()
        return self.snapshot[key]

    def request(self, url, *, method="GET", headers=None, payload=None):
        if url.endswith("/tokens"):
            return {"token": "short-lived"}
        path = url.split("/api", 1)[1]
        self.calls.append(path)
        if path in self.refuse:
            raise ProviderError("failed") from urllib.error.HTTPError(url, 403, "denied", {}, None)
        return LISTS[path]


def declared(kind):
    """The reader an admitted adapter declares for ``kind``."""

    from control_plane.provider_adapters import CONTROLLER_PROVIDER_ADAPTERS

    (reader,) = [a.readings[kind] for a in CONTROLLER_PROVIDER_ADAPTERS if kind in a.readings]
    return reader


def read_with_parts(kind, runtime=None):
    """The kept records and the parts refused, as the stored snapshot says them."""

    with part_ledger() as ledger:
        records = declared(kind)(runtime or _Runtime())
    kept, refused = OBSERVATIONS[kind].clean(records)
    assert refused == 0, records
    snapshot = SimpleNamespace(
        kind=kind, reachable=True, refused_parts=clean_refused_parts(kind, ledger)
    )
    return kept, refused_parts(snapshot)


def read(kind, runtime=None):
    return read_with_parts(kind, runtime)[0]


class DeclarationTests(SimpleTestCase):
    def test_the_npm_adapter_declares_every_npm_reading(self):
        from control_plane.provider_adapters import CONTROLLER_PROVIDER_ADAPTERS

        (adapter,) = [a for a in CONTROLLER_PROVIDER_ADAPTERS
                      if any(d.kind == "npm.proxy_host" for d in a.definitions)]
        self.assertEqual(dict(adapter.readings), npm_readings.READINGS)
        self.assertEqual(
            sorted(adapter.readings),
            sorted(kind for kind, spec in OBSERVATIONS.items() if spec.provider == "npm"),
        )


class CertificateTests(SimpleTestCase):
    def test_a_certificate_serves_the_enabled_names_of_every_host_using_it(self):
        wildcard, unused = read("npm.certificate")

        self.assertEqual(
            wildcard["serves"], ["app.example.com", "www.example.com", "gone.example.com"]
        )
        self.assertEqual(wildcard["expires_on"], "2030-01-01 00:00:00")
        self.assertEqual(wildcard["connection_ref"], "example-npm")
        self.assertEqual(unused.get("serves", []), [])
        self.assertNotIn("PRIVATE KEY", str(wildcard))

    def test_the_issuer_and_expiry_are_read_off_the_record(self):
        spec = OBSERVATIONS["npm.certificate"]
        (record, _unused) = read("npm.certificate")

        self.assertEqual(spec.issuer(record), "Let's Encrypt")
        self.assertEqual(spec.expires(record), "2030-01-01 00:00:00")
        self.assertEqual(spec.hostnames(record)[0], "app.example.com")

    def test_a_host_list_it_may_not_see_is_a_refused_part_naming_its_permission(self):
        records, (refused,) = read_with_parts(
            "npm.certificate", _Runtime(refuse={"/nginx/dead-hosts"})
        )

        self.assertNotIn("gone.example.com", records[0]["serves"])
        self.assertIn("app.example.com", records[0]["serves"])
        self.assertNotIn("unread", records[0])
        self.assertEqual((refused.part.name, refused.refusal, refused.connection_ref),
                         ("dead_hosts", PERMISSION_REFUSAL, "example-npm"))
        self.assertEqual(refused.missing, ("dead_hosts: view",))
        self.assertEqual(refused.phrase, "Names served by 404 hosts not read: missing dead_hosts: view")

    def test_every_host_list_readable_refuses_no_part(self):
        _records, refused = read_with_parts("npm.certificate")

        self.assertEqual(refused, ())

    def test_the_certificate_list_refused_is_a_refused_read(self):
        with self.assertRaises(ProviderError) as raised:
            read("npm.certificate", _Runtime(refuse={"/nginx/certificates"}))

        self.assertEqual(raised.exception.refusal, PERMISSION_REFUSAL)
        self.assertIn("certificates: view", str(raised.exception))


class HostReadingTests(SimpleTestCase):
    def test_a_redirection_host_names_its_target_host(self):
        forced, auto = read("npm.redirect")
        spec = OBSERVATIONS["npm.redirect"]

        self.assertEqual(
            (forced["target"], forced["target_host"], forced["status_code"], forced["certificate"]),
            ("https://example.com", "example.com", 301, "example wildcard"),
        )
        self.assertEqual(spec.redirects_to(forced), "example.com")
        self.assertEqual(auto["target"], "example.org")
        self.assertEqual(spec.redirects_to(auto), "")
        self.assertEqual(spec.hostnames(auto), ())

    def test_a_stream_forwards_a_port(self):
        (stream,) = read("npm.stream")
        spec = OBSERVATIONS["npm.stream"]

        self.assertEqual(spec.upstream(stream, ""), "192.0.2.30:22")
        self.assertEqual(spec.addresses(stream), ("192.0.2.30",))
        self.assertEqual(spec.title(stream), "TCP 2222 to 192.0.2.30:22")

    def test_an_access_list_names_its_rules_logins_and_the_names_it_guards(self):
        (staff,) = read("npm.access_list")

        self.assertEqual(staff["protects"], ["app.example.com"])
        self.assertEqual(staff["logins"], ["operator"])
        self.assertEqual(staff["clients"][0], {"directive": "allow", "address": "100.64.0.0/10"})
        self.assertNotIn("password", str(staff))

    def test_proxy_hosts_it_may_not_see_leave_an_access_list_with_a_refused_part(self):
        (staff,), (refused,) = read_with_parts(
            "npm.access_list", _Runtime(refuse={"/nginx/proxy-hosts"})
        )

        self.assertEqual(staff.get("protects", []), [])
        self.assertEqual(staff["logins"], ["operator"])
        self.assertEqual((refused.part.name, refused.missing),
                         ("proxy_hosts", ("proxy_hosts: view",)))

    def test_a_404_host_is_read(self):
        (dead,) = read("npm.dead_host")

        self.assertEqual((dead["hostnames"], dead["certificate"]),
                         (["gone.example.com"], "example wildcard"))

    def test_each_list_is_read_once_per_sweep(self):
        runtime = _Runtime()
        for kind in ("npm.certificate", "npm.redirect", "npm.dead_host", "npm.access_list"):
            read(kind, runtime)

        self.assertEqual(runtime.calls.count("/nginx/proxy-hosts"), 1)
        self.assertEqual(runtime.calls.count("/nginx/certificates"), 1)

    def test_no_connection_named_reads_the_sole_one(self):
        (stream,) = read("npm.stream", _Runtime(refs=()))

        self.assertEqual(stream["connection_ref"], "")
