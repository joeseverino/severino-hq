"""The certificate each Caddy route serves, read through the edge's certificate operation."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from django.test import SimpleTestCase

from control_plane.providers import PROVIDERS

from .. import caddy
from ..contracts import ProviderError

EXPIRES = datetime(2026, 12, 1, tzinfo=timezone.utc)


def leaf(names=("example.com", "*.example.com"), organization="Example CA") -> bytes:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, names[0]),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, organization),
    ])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(EXPIRES - timedelta(days=90))
        .not_valid_after(EXPIRES)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(n) for n in names]), False)
        .sign(key, hashes.SHA256())
    )
    return certificate.public_bytes(serialization.Encoding.PEM)


def config(*hosts, files=True):
    found = {"apps": {"http": {"servers": {"srv0": {"routes": [
        {"match": [{"host": [host]}],
         "handle": [{"handler": "reverse_proxy", "upstreams": [{"dial": f"{host.split('.')[0]}:80"}]}]}
        for host in hosts
    ]}}}}}
    if files:
        found["apps"]["tls"] = {"certificates": {"load_files": [
            {"certificate": "/certs/fullchain.pem", "key": "/certs/privkey.pem", "tags": ["cert0"]}
        ]}}
    return found


class Edge:
    """An edge target answering ``routes`` and, from v3, ``certificate``."""

    def __init__(self, routes, certificate: bytes | Exception):
        self.routes = routes
        self.certificate = certificate
        self.asked: list[str] = []

    def connection_refs_for_role(self, role):
        return ("example-edge",)

    def ssh_connection_refs(self):
        return ("example-edge",)

    def ssh(self, connection_ref, operation, payload=None):
        self.asked.append(operation)
        if operation == "routes":
            return json.dumps(self.routes).encode()
        if isinstance(self.certificate, Exception):
            raise self.certificate
        return self.certificate


class CaddyCertificateTests(SimpleTestCase):
    def test_a_route_its_loaded_certificate_covers_serves_it(self):
        edge = Edge(config("shop.example.com", "example.com", "other.example.net"), leaf())

        found = {route["domain"]: route for route in caddy.inventory(edge)}

        certificate = found["shop.example.com"]["certificate"]
        self.assertEqual(certificate["name"], "example.com")
        self.assertEqual(certificate["provider"], "Example CA")
        self.assertEqual(certificate["expires_on"], EXPIRES.isoformat())
        self.assertEqual(certificate["domains"], ("example.com", "*.example.com"))
        self.assertIn("certificate", found["example.com"])
        self.assertNotIn("certificate", found["other.example.net"])
        self.assertIn("manages this name's certificate itself",
                      found["other.example.net"]["certificate_unread"])
        self.assertNotIn("PRIVATE", json.dumps(found, default=str))

    def test_an_edge_that_loads_no_file_is_not_asked(self):
        edge = Edge(config("shop.example.com", files=False), leaf())

        (route,) = caddy.inventory(edge)

        self.assertEqual(edge.asked, ["routes"])
        self.assertNotIn("certificate", route)
        self.assertIn("manages this name's certificate itself", route["certificate_unread"])

    def test_an_older_edge_target_says_it_must_be_redeployed(self):
        refused = ProviderError("exit 126")
        edge = Edge(config("shop.example.com"), refused)

        (route,) = caddy.inventory(edge)

        self.assertEqual(route["upstream"], "shop:80")
        self.assertIn("redeploy it at version 3", route["certificate_unread"])

    def test_a_certificate_that_does_not_parse_is_said_so(self):
        (route,) = caddy.inventory(Edge(config("shop.example.com"), b"not a certificate"))

        self.assertIn("did not parse", route["certificate_unread"])

    def test_the_route_kind_states_what_it_serves(self):
        served = PROVIDERS["caddy.route"].served_certificate

        with_certificate = served({"domain": "Shop.Example.com.",
                                   "certificate": {"name": "example.com"}})
        without = served({"domain": "shop.example.com", "certificate_unread": "why"})

        self.assertEqual(with_certificate.hostnames, ("shop.example.com",))
        self.assertEqual((without.certificate, without.unread), ({}, "why"))
        self.assertIsNone(served({"domain": "shop.example.com"}))
