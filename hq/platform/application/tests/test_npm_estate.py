"""NPM readings on the request path, the service page, the API and as findings."""

from __future__ import annotations

from datetime import timedelta
from ipaddress import ip_network
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from hq.domains.control_plane.models import ManagedResource
from hq.domains.control_plane.observations import OBSERVATIONS

from ..derived_reads import get_reading, serialize_path
from ..findings import findings
from ..inventory import record_inventory
from ..inventory_testing import store
from ..paths import path_to
from ..path_dependencies import depends_on
from ..projection import projection_scope
from ..relationships import relationships_for
from ..security import Capability, Principal, cli_principal

READER = Principal("reader", "test", frozenset({Capability.READ}))
NPM = "example-npm"
PUBLIC_RANGE = mock.patch("hq.platform.application.reach.DOCUMENTATION", (ip_network("192.0.2.0/24"),))


def in_days(days):
    return (timezone.now() + timedelta(days=days)).isoformat()


def certificate(expires_on, *serves):
    return {"connection_ref": NPM, "id": 1, "name": "example wildcard", "provider": "letsencrypt",
            "domains": ["*.example.com"], "expires_on": expires_on, "serves": list(serves)}


def estate():
    ManagedResource.objects.create(
        key="lab-1", kind="machine", spec={"name": "lab-1", "addresses": ["100.64.0.10"]}
    )
    ManagedResource.objects.create(
        key="edge-1", kind="machine", spec={"name": "edge-1", "addresses": ["198.51.100.20"]}
    )
    ManagedResource.objects.create(
        key="app-proxy", kind="npm.proxy_host",
        spec={"domain_names": ["app.example.com"], "forward_scheme": "http",
              "forward_host": "127.0.0.1", "forward_port": 8000},
    )
    store(
        "adguard.rewrite",
        *({"domain": name, "answer": "100.64.0.10", "connection_ref": "example-adguard"}
          for name in ("app.example.com", "www.example.com", "gone.example.com")),
    )
    store("cloudflare.dns_record", {"zone": "example.com", "name": "shop.example.com",
                                    "record_type": "A", "content": "198.51.100.20",
                                    "proxied": True, "ttl": 1})
    store("npm.proxy_host", {"domain_names": ["app.example.com"], "forward_scheme": "http",
                             "forward_host": "127.0.0.1", "forward_port": 8000,
                             "connection_ref": NPM, "access_list_id": 7})
    store("npm.redirect",
          {"connection_ref": NPM, "id": 20, "hostnames": ["www.example.com", "shop.example.com"],
           "target": "https://example.com", "target_host": "example.com", "status_code": 301,
           "enabled": True})
    store("npm.dead_host", {"connection_ref": NPM, "id": 30, "hostnames": ["gone.example.com"],
                            "enabled": True})
    store("npm.stream", {"connection_ref": NPM, "id": 40, "incoming_port": 2222,
                         "forwarding_host": "198.51.100.20", "forwarding_port": 22, "tcp": True,
                         "enabled": True})
    store("npm.certificate", certificate(in_days(60), "app.example.com", "www.example.com"))
    store("npm.access_list",
          {"connection_ref": NPM, "id": 7, "name": "staff", "satisfy_any": True,
           "clients": [{"directive": "allow", "address": "100.64.0.0/10"},
                       {"directive": "deny", "address": "all"}],
           "logins": ["operator"], "protects": ["app.example.com"]})


def walk(name):
    with projection_scope():
        return path_to(name)


def steps(route):
    return [(hop.step, hop.name) for hop in route.hops]


@PUBLIC_RANGE
class PathTests(TestCase):
    def setUp(self):
        estate()

    def test_an_npm_redirect_answers_at_the_ingress_with_its_certificate(self):
        path = walk("www.example.com")

        self.assertEqual(steps(path.primary)[-1], ("redirect", "example.com"))
        self.assertEqual(path.redirects_to, "example.com")
        hop = path.primary.hops[-1]
        self.assertEqual(hop.source.kind, "npm.redirect")
        self.assertEqual((hop.certificate.role, hop.certificate.name, hop.certificate.issuer),
                         ("Served", "example wildcard", "Let's Encrypt"))

    def test_behind_the_edge_an_npm_redirect_is_the_origins_not_the_edges(self):
        path = walk("shop.example.com")

        kinds = [(hop.step, hop.source.kind if hop.source else "") for hop in path.primary.hops]
        self.assertIn(("edge", "cloudflare.dns_record"), kinds)
        self.assertEqual(kinds[-1], ("redirect", "npm.redirect"))
        self.assertEqual(path.primary.hops[-2].name, "edge-1")

    def test_a_404_host_ends_the_path(self):
        end = walk("gone.example.com").ends_at

        self.assertEqual((end.step, end.label, end.source.kind),
                         ("origin", "Answers 404", "npm.dead_host"))

    def test_a_stream_is_another_route_into_the_ingress_and_not_a_dependency(self):
        path = walk("app.example.com")

        (forward,) = [route for route in path.routes if route.port]
        self.assertEqual(forward.port, 2222)
        self.assertEqual(path.primary.port, None)
        self.assertIn(("ingress", "port 2222"), steps(forward))
        self.assertIn(("machine", "edge-1"), steps(forward))
        self.assertNotIn("edge-1", [item.name for item in depends_on(path)])
        self.assertEqual(serialize_path(path)["routes"][1]["port"], 2222)

    def test_a_stream_from_another_npm_is_not_on_this_ingress(self):
        store("npm.stream", {"connection_ref": "other-npm", "id": 41, "incoming_port": 2223,
                             "forwarding_host": "198.51.100.20", "forwarding_port": 22,
                             "tcp": True, "enabled": True})

        self.assertEqual([route.port for route in walk("app.example.com").routes if route.port], [])


class RelationTests(TestCase):
    def setUp(self):
        estate()

    def test_the_certificate_and_access_list_join_the_names_they_serve(self):
        with projection_scope():
            found = relationships_for("service:app.example.com", principal=READER)

        self.assertEqual(found.labels("Served with certificate"), ("example wildcard",))
        self.assertEqual(found.labels("Behind access list"), ("staff",))

    def test_the_stream_joins_the_machine_it_forwards_to(self):
        with projection_scope():
            found = relationships_for("machine:edge-1", principal=READER)

        self.assertEqual(found.labels("Receives a forward through"),
                         ("TCP 2222 to 198.51.100.20:22",))


class CertificateFindingTests(TestCase):
    def setUp(self):
        estate()

    def found(self):
        return findings(principal=READER, rule="certificate-expiring")["findings"]

    def test_a_certificate_inside_a_week_is_serious_and_names_what_it_serves(self):
        store("npm.certificate", certificate(in_days(5), "app.example.com"))

        (finding,) = self.found()

        self.assertEqual(finding["severity"], "serious")
        self.assertTrue(finding["title"].startswith("NPM certificate example wildcard expires"))
        self.assertIn({"label": "Serves", "value": "app.example.com"}, finding["evidence"])

    def test_an_expired_one_says_so_and_a_distant_one_is_not_a_finding(self):
        store("npm.certificate", certificate(in_days(-2)))
        self.assertEqual(self.found()[0]["title"], "NPM certificate example wildcard has expired")

        store("npm.certificate", certificate(in_days(60)))
        self.assertEqual(self.found(), [])

    def test_one_serving_no_name_is_cleanup_not_an_outage(self):
        store("npm.certificate", certificate(in_days(2)))

        (finding,) = self.found()

        self.assertEqual(finding["severity"], "attention")
        self.assertIn("serves no name", finding["explanation"])
        self.assertIn("rather than renew it", finding["explanation"])

    def test_one_a_proxy_host_also_names_is_raised_once(self):
        expires = in_days(5)
        store("npm.certificate", certificate(expires, "app.example.com"))
        store("npm.proxy_host", {"domain_names": ["app.example.com"], "forward_scheme": "http",
                                 "forward_host": "127.0.0.1", "forward_port": 8000,
                                 "connection_ref": NPM,
                                 "certificate": {"name": "example wildcard",
                                                 "expires_on": expires}})

        (finding,) = self.found()

        self.assertTrue(finding["title"].startswith("NPM certificate"))

    def test_a_certificate_with_no_expiry_says_nothing(self):
        store("npm.certificate", certificate("not a date"))

        self.assertEqual(self.found(), [])


class DeliveryTests(TestCase):
    def setUp(self):
        estate()

    def test_the_service_page_says_who_is_allowed(self):
        self.client.force_login(
            get_user_model().objects.create_superuser("operator", password="x" * 20)
        )

        response = self.client.get(reverse("control_plane:service", args=["app.example.com"]))

        self.assertContains(response, "Who is allowed")
        self.assertContains(response, "allow 100.64.0.0/10; deny all")
        self.assertContains(response, "Address or login")

    def test_key_material_never_reaches_the_api(self):
        record_inventory(
            {"npm.certificate": {"ok": True, "records": [
                {**certificate(in_days(60)), "meta": {"certificate_key": "PRIVATE"}}
            ]}},
            principal=cli_principal(),
        )

        found = get_reading("npm.certificate")

        self.assertEqual(found["requires"], list(OBSERVATIONS["npm.certificate"].requires))
        self.assertIn("certificates: view", found["requires"])
        self.assertNotIn("PRIVATE", str(found))
        self.assertEqual(found["items"][0]["name"], "example wildcard")

