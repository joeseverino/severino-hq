"""Caddy routes on the request path: the certificate each serves, the container behind it."""

from datetime import timedelta
from ipaddress import ip_network
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from hq.domains.control_plane.models import ProviderInventory
from hq.platform.core.testing import declare

from ..findings import findings
from ..paths import path_to
from ..projection import projection_scope
from ..security import cli_principal

CONTROLLER = "example-controller"
EXPIRES = (timezone.now() + timedelta(days=60)).isoformat()


def store(kind, *records):
    ProviderInventory.objects.update_or_create(
        kind=kind,
        defaults={
            "records": list(records),
            "reachable": True,
            "connected": True,
            "controller_id": CONTROLLER,
            "observed_at": timezone.now(),
        },
    )


def machine(name, address):
    declare(name, "machine", {"name": name, "addresses": [address]})


# 198.51.100.0/24 stands for a public address here; 192.0.2.0/24 stays parked.
@mock.patch("hq.platform.application.reach.DOCUMENTATION", (ip_network("192.0.2.0/24"),))
class CaddyPathTests(TestCase):
    def setUp(self):
        machine("edge-1", "198.51.100.20")
        machine("lab-1", "100.64.0.10")
        store(
            "cloudflare.dns_record",
            {
                "zone": "example.com",
                "name": "shop.example.com",
                "record_type": "A",
                "content": "198.51.100.20",
                "proxied": False,
                "ttl": 1,
            },
        )
        store(
            "portainer.container",
            {"host": "edge-1", "name": "shop", "ports": [8080], "state": "running"},
            {"host": "lab-1", "name": "shop", "ports": [8080], "state": "running"},
        )

    def walk(self):
        with projection_scope():
            return path_to("shop.example.com")

    def route(self, **extra):
        store(
            "caddy.route",
            {"connection_ref": "example-edge", "domain": "shop.example.com", "upstream": "shop:8080", **extra},
        )

    def test_the_path_names_the_certificate_the_route_serves(self):
        self.route(
            certificate={
                "name": "example.com",
                "provider": "Example CA",
                "expires_on": EXPIRES,
                "domains": ["*.example.com"],
            }
        )

        ingress = next(hop for hop in self.walk().primary.hops if hop.step == "ingress")

        self.assertEqual((ingress.certificate.name, ingress.certificate.issuer), ("example.com", "Example CA"))
        self.assertFalse(ingress.certificate.unread)
        self.assertIn("60 days", ingress.certificate.expiry)

    def test_the_path_says_why_a_route_names_no_certificate(self):
        self.route(certificate_unread="the edge target does not report its certificate")

        ingress = next(hop for hop in self.walk().primary.hops if hop.step == "ingress")

        self.assertIn("the edge target does not report its certificate", ingress.certificate.unread)


PLACEHOLDER = "{http.request.host}:443"


@mock.patch("hq.platform.application.reach.DOCUMENTATION", (ip_network("192.0.2.0/24"),))
class RequestedHostRouteTests(TestCase):
    """A route forwarding to whichever host was asked for names no origin to find."""

    def setUp(self):
        machine("edge-1", "198.51.100.20")
        store(
            "cloudflare.dns_record",
            {
                "zone": "example.dev",
                "name": "example.dev",
                "record_type": "A",
                "content": "198.51.100.20",
                "proxied": False,
                "ttl": 1,
            },
            {
                "zone": "example.dev",
                "name": "*.example.dev",
                "record_type": "A",
                "content": "198.51.100.20",
                "proxied": False,
                "ttl": 1,
            },
        )

    def routes(self, upstream, **extra):
        store(
            "caddy.route",
            *(
                {"connection_ref": "example-edge", "domain": domain, "upstream": upstream, **extra}
                for domain in ("example.dev", "*.example.dev")
            ),
        )

    def rules(self):
        with projection_scope():
            return findings(principal=cli_principal())

    def test_it_raises_what_a_route_caddy_answers_itself_raises_and_no_more(self):
        self.routes("")
        answered_here = self.rules()
        self.routes(PLACEHOLDER, to_requested_host=True)
        passed_through = self.rules()

        self.assertEqual(
            sorted((item["rule"], item["subject"]) for item in passed_through["findings"]),
            sorted((item["rule"], item["subject"]) for item in answered_here["findings"]),
        )
        self.assertNotIn("http.request.host", str(passed_through))
