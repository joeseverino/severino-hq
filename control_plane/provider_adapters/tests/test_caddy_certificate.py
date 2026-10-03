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


def leaf(names=("example.com", "*.example.com"), organization="Example CA", expires=None) -> bytes:
    expires = expires or EXPIRES
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
        .not_valid_before(expires - timedelta(days=90))
        .not_valid_after(expires)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(n) for n in names]), False)
        .sign(key, hashes.SHA256())
    )
    return certificate.public_bytes(serialization.Encoding.PEM)


def config(*hosts, files=1):
    found = {"apps": {"http": {"servers": {"srv0": {"routes": [
        {"match": [{"host": [host]}],
         "handle": [{"handler": "reverse_proxy", "upstreams": [{"dial": f"{host.split('.')[0]}:80"}]}]}
        for host in hosts
    ]}}}}}
    if files:
        found["apps"]["tls"] = {"certificates": {"load_files": [
            {"certificate": f"/certs/{index}/fullchain.pem", "key": f"/certs/{index}/privkey.pem",
             "tags": [f"cert{index}"]}
            for index in range(files)
        ]}}
    return found


class Edge:
    """An edge target answering ``routes`` and ``certificates``.

    ``version=3`` refuses ``certificates`` and answers ``certificate`` with the
    first leaf alone, as a target of that version does.
    """

    def __init__(self, routes, certificate: bytes | Exception, *, version: int = 4):
        self.routes = routes
        self.certificate = certificate
        self.version = version
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
        if operation == "certificates" and self.version < 4:
            raise ProviderError("exit 126")
        if operation == "certificate":
            end = b"-----END CERTIFICATE-----\n"
            return self.certificate.split(end)[0] + end
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
        edge = Edge(config("shop.example.com", files=0), leaf())

        (route,) = caddy.inventory(edge)

        self.assertEqual(edge.asked, ["routes"])
        self.assertNotIn("certificate", route)
        self.assertIn("manages this name's certificate itself", route["certificate_unread"])

    def test_an_older_edge_target_says_it_must_be_redeployed(self):
        refused = ProviderError("exit 126")
        edge = Edge(config("shop.example.com"), refused)

        (route,) = caddy.inventory(edge)

        self.assertEqual(route["upstream"], "shop:80")
        self.assertIn("redeploy it at version 4", route["certificate_unread"])

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


PREVIEW = ("example.dev", "*.example.dev")


class TwoFileCertificatesTests(SimpleTestCase):
    """An edge that loads two certificates from files: each route names the one that covers it."""

    def edge(self, *hosts, version=4, second=None):
        both = leaf() + (second or leaf(PREVIEW, organization="Example Private Root"))
        return Edge(config(*hosts, files=2), both, version=version)

    def routes(self, edge):
        return {route["domain"]: route for route in caddy.inventory(edge)}

    def test_both_loaded_certificates_are_read(self):
        found = caddy.loaded_certificates(leaf() + leaf(PREVIEW, organization="Example Private Root"))

        self.assertEqual(
            [(item["provider"], item["domains"]) for item in found],
            [("Example CA", ("example.com", "*.example.com")), ("Example Private Root", PREVIEW)],
        )

    def test_each_route_is_matched_to_the_certificate_covering_its_name(self):
        found = self.routes(self.edge(
            "shop.example.com", "example.dev", "*.example.dev", "preview.example.dev", "other.example.net"
        ))

        self.assertEqual(found["shop.example.com"]["certificate"]["provider"], "Example CA")
        for name in ("example.dev", "*.example.dev", "preview.example.dev"):
            with self.subTest(name=name):
                self.assertEqual(found[name]["certificate"]["provider"], "Example Private Root")
                self.assertEqual(found[name]["certificate"]["domains"], PREVIEW)
        self.assertNotIn("certificate", found["other.example.net"])
        self.assertIn("manages this name's certificate itself",
                      found["other.example.net"]["certificate_unread"])

    def test_a_wildcard_covers_one_label_only(self):
        found = self.routes(self.edge("deep.preview.example.dev"))

        self.assertNotIn("certificate", found["deep.preview.example.dev"])

    def test_the_order_the_edge_reports_them_in_does_not_decide_the_match(self):
        reversed_edge = Edge(
            config("shop.example.com", "preview.example.dev", files=2),
            leaf(PREVIEW, organization="Example Private Root") + leaf(),
        )

        found = self.routes(reversed_edge)

        self.assertEqual(found["shop.example.com"]["certificate"]["provider"], "Example CA")
        self.assertEqual(found["preview.example.dev"]["certificate"]["provider"], "Example Private Root")

    def test_a_certificate_naming_the_host_is_chosen_over_a_wildcard(self):
        exact = leaf(("shop.example.com",), organization="Example Exact")

        found = self.routes(self.edge("shop.example.com", "blog.example.com", second=exact))

        self.assertEqual(found["shop.example.com"]["certificate"]["provider"], "Example Exact")
        self.assertEqual(found["blog.example.com"]["certificate"]["provider"], "Example CA")

    def test_of_two_covering_alike_the_one_valid_longest_is_chosen(self):
        later = leaf(organization="Example Renewed", expires=EXPIRES + timedelta(days=30))

        found = self.routes(self.edge("shop.example.com", second=later))

        self.assertEqual(found["shop.example.com"]["certificate"]["provider"], "Example Renewed")

    def test_an_older_target_reporting_one_of_two_says_the_other_was_not_read(self):
        edge = self.edge("shop.example.com", "preview.example.dev", version=3)

        found = self.routes(edge)

        self.assertEqual(edge.asked, ["routes", "certificates", "certificate"])
        self.assertEqual(found["shop.example.com"]["certificate"]["provider"], "Example CA")
        self.assertNotIn("certificate", found["preview.example.dev"])
        self.assertIn("loads 2 certificates from files and its target reports 1",
                      found["preview.example.dev"]["certificate_unread"])

    def test_no_private_key_is_recorded(self):
        found = self.routes(self.edge("shop.example.com", "preview.example.dev"))

        self.assertNotIn("PRIVATE", json.dumps(found, default=str))
