"""Caddy routes on the request path: the certificate each serves, the container behind it."""

from __future__ import annotations

from datetime import timedelta
from ipaddress import ip_network
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from control_plane.models import ManagedResource, ProviderInventory

from .paths import path_to
from .projection import projection_scope

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
    ManagedResource.objects.create(
        key=name, kind="machine", spec={"name": name, "addresses": [address]}
    )


# 198.51.100.0/24 stands for a public address here; 192.0.2.0/24 stays parked.
@mock.patch("application.reach.DOCUMENTATION", (ip_network("192.0.2.0/24"),))
class CaddyPathTests(TestCase):
    def setUp(self):
        machine("edge-1", "198.51.100.20")
        machine("lab-1", "100.64.0.10")
        store("cloudflare.dns_record",
              {"zone": "example.com", "name": "shop.example.com", "record_type": "A",
               "content": "198.51.100.20", "proxied": False, "ttl": 1})
        store("portainer.container",
              {"host": "edge-1", "name": "shop", "ports": [8080], "state": "running"},
              {"host": "lab-1", "name": "shop", "ports": [8080], "state": "running"})

    def walk(self):
        with projection_scope():
            return path_to("shop.example.com")

    def route(self, **extra):
        store("caddy.route", {"connection_ref": "example-edge", "domain": "shop.example.com",
                              "upstream": "shop:8080", **extra})

    def test_the_path_names_the_certificate_the_route_serves(self):
        self.route(certificate={"name": "example.com", "provider": "Example CA",
                                "expires_on": EXPIRES, "domains": ["*.example.com"]})

        ingress = next(hop for hop in self.walk().primary.hops if hop.step == "ingress")

        self.assertEqual(ingress.certificate.role, "Served")
        self.assertEqual((ingress.certificate.name, ingress.certificate.issuer),
                         ("example.com", "Example CA"))
        self.assertFalse(ingress.certificate.unread)
        self.assertIn("60 days", ingress.certificate.expiry)

    def test_the_path_says_why_a_route_names_no_certificate(self):
        self.route(certificate_unread="the edge target does not report its certificate")

        ingress = next(hop for hop in self.walk().primary.hops if hop.step == "ingress")

        self.assertEqual(
            ingress.certificate.unread,
            "not read: Served certificate, because the edge target does not report its "
            "certificate",
        )

    def test_a_container_name_resolves_on_the_proxy_machine(self):
        self.route()

        hops = [(hop.step, hop.name) for hop in self.walk().primary.hops]

        self.assertEqual(hops[-2:], [("machine", "edge-1"), ("container", "shop")])
