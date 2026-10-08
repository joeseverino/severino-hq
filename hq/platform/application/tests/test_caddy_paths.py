"""Caddy routes on the request path: the certificate each serves, the container behind it."""

from datetime import timedelta
from ipaddress import ip_network
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from hq.domains.control_plane.models import ManagedResource, ProviderInventory

from ..facts import facts_about
from ..findings import findings
from ..paths import path_to
from ..projection import projection_scope
from ..resource_context import origin_machine
from ..security import cli_principal
from ..services import service_catalog
from ..topology import topology

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
@mock.patch("hq.platform.application.reach.DOCUMENTATION", (ip_network("192.0.2.0/24"),))
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


PLACEHOLDER = "{http.request.host}:443"


@mock.patch("hq.platform.application.reach.DOCUMENTATION", (ip_network("192.0.2.0/24"),))
class RequestedHostRouteTests(TestCase):
    """A route forwarding to whichever host was asked for names no origin to find."""

    def setUp(self):
        machine("edge-1", "198.51.100.20")
        store("cloudflare.dns_record",
              {"zone": "example.dev", "name": "example.dev", "record_type": "A",
               "content": "198.51.100.20", "proxied": False, "ttl": 1},
              {"zone": "example.dev", "name": "*.example.dev", "record_type": "A",
               "content": "198.51.100.20", "proxied": False, "ttl": 1})

    def routes(self, upstream, **extra):
        store("caddy.route", *(
            {"connection_ref": "example-edge", "domain": domain, "upstream": upstream, **extra}
            for domain in ("example.dev", "*.example.dev")
        ))

    def rules(self):
        with projection_scope():
            found = findings(principal=cli_principal())
        return found

    def test_the_path_ends_at_the_proxy_and_resolves_nothing(self):
        self.routes(PLACEHOLDER, to_requested_host=True)

        with projection_scope():
            hops = [(hop.step, hop.name) for hop in path_to("example.dev").primary.hops]

        self.assertIn("ingress", [step for step, _ in hops])
        self.assertFalse({"upstream", "machine", "container"} & {step for step, _ in hops[hops.index(
            next(hop for hop in hops if hop[0] == "ingress")) + 1:]})

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

    def test_no_fact_names_the_placeholder_as_an_origin(self):
        self.routes(PLACEHOLDER, to_requested_host=True)

        with projection_scope():
            facts = facts_about(["example.dev"], [])

        self.assertTrue(any(fact.source_kind == "caddy.route" for fact in facts))
        self.assertNotIn("Origin", [fact.label for fact in facts if fact.source_kind == "caddy.route"])
        self.assertNotIn(PLACEHOLDER, [fact.value for fact in facts])

    def test_the_catalogue_and_the_graph_read_it_without_an_origin(self):
        self.routes(PLACEHOLDER, to_requested_host=True)

        with projection_scope():
            services = service_catalog()
            graph = topology(principal=cli_principal())

        self.assertNotIn("http.request.host", str(graph))
        for service in services:
            if service.origin is not None:
                self.assertNotIn("http.request.host", service.origin.address)

    def test_a_declared_route_with_a_placeholder_names_no_machine(self):
        resource = ManagedResource(
            key="example-dev-caddy", kind="caddy.route",
            spec={"connection_ref": "example-edge", "domain": "example.dev", "upstream": PLACEHOLDER},
        )

        self.assertIsNone(origin_machine(resource))


class ServedCertificateExpiryTests(TestCase):
    """A certificate only a proxy's routes name still warns before it lapses."""

    def routes(self, days, *domains):
        expires = (timezone.now() + timedelta(days=days)).isoformat()
        store("caddy.route", *(
            {"connection_ref": "example-edge", "domain": domain, "upstream": "shop:8080",
             "certificate": {"name": "example.dev", "provider": "Example Root CA",
                             "expires_on": expires,
                             "domains": ["example.dev", "*.example.dev"]}}
            for domain in domains
        ))

    def found(self):
        with projection_scope():
            return findings(principal=cli_principal(), rule="certificate-expiring")["findings"]

    def test_one_near_expiry_is_raised_once_with_every_name_it_serves(self):
        self.routes(5, "example.dev", "*.example.dev")

        (finding,) = self.found()

        self.assertEqual(finding["severity"], "serious")
        self.assertTrue(finding["title"].startswith("Certificate example.dev expires"))
        self.assertIn({"label": "Held in", "value": "example-edge"}, finding["evidence"])
        self.assertIn({"label": "Serves", "value": "*.example.dev, example.dev"},
                      finding["evidence"])

    def test_a_distant_one_and_a_route_caddy_manages_say_nothing(self):
        self.routes(400, "example.dev")
        self.assertEqual(self.found(), [])

        store("caddy.route", {"connection_ref": "example-edge", "domain": "example.dev",
                              "upstream": "shop:8080", "certificate_unread": "managed by Caddy"})
        self.assertEqual(self.found(), [])

