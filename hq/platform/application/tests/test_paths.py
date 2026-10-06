"""The request path: each hop from a reading, and a stated reason where none was read."""

from __future__ import annotations

from datetime import timedelta
from ipaddress import ip_network
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from hq.domains.control_plane.models import ManagedResource, ProviderInventory

from ..paths import hq_path, path_to, why_unread
from ..projection import projection_scope
from ..resources import get_resource
from ..security import Capability, Principal

READ = Principal("reader", "test", frozenset({Capability.READ}))
CLOUDFLARE = "example-cloudflare"
# 198.51.100.0/24 stands for a public address here; 192.0.2.0/24 stays parked.
PUBLIC_RANGE = mock.patch("hq.platform.application.reach.DOCUMENTATION", (ip_network("192.0.2.0/24"),))
EXPIRES = (timezone.now() + timedelta(days=60)).isoformat()


def store(kind, *records, reachable=True, connected=True, error=""):
    ProviderInventory.objects.update_or_create(
        kind=kind,
        defaults={
            "records": list(records),
            "reachable": reachable,
            "connected": connected,
            "error": error,
            "observed_at": timezone.now(),
            "controller_id": "example-controller",
        },
    )


def record(name, record_type, content, *, proxied=True, zone="example.com"):
    return {"zone": zone, "name": name, "record_type": record_type, "content": content,
            "proxied": proxied, "ttl": 1, "connection_ref": "example-dns"}


def declare_certificate(key="example-wildcard", *, matches=True):
    """A certificate HQ installed for app.example.com, as its status records it."""

    return ManagedResource.objects.create(
        key=key, kind="tls.certificate",
        spec={"certificate_name": "example", "domains": ["*.example.com"]},
        status={
            "issuer": "Example CA", "not_after": EXPIRES,
            "consumers": [{"domain": "app.example.com", "consumer_kind": "npm",
                           "fingerprint_sha256": "ab" * 32, "matches_expected": matches}],
        },
        last_observed_at=timezone.now(),
    )


def estate():
    """A proxied name, a tailnet name, a Pages site and a redirecting apex."""

    for key, addresses in (("edge-1", ["198.51.100.20"]), ("lab-1", ["100.64.0.10"])):
        ManagedResource.objects.create(
            key=key, kind="machine", spec={"name": key, "addresses": addresses}
        )
    store(
        "cloudflare.dns_record",
        record("shop.example.com", "A", "198.51.100.20"),
        record("example.com", "CNAME", "example-site.pages.dev"),
        record("example.net", "A", "192.0.2.1", zone="example.net"),
    )
    store("adguard.rewrite", {"domain": "app.example.com", "answer": "100.64.0.10",
                              "connection_ref": "example-adguard"})
    store(
        "npm.proxy_host",
        {"domain_names": ["app.example.com"], "forward_scheme": "http",
         "forward_host": "127.0.0.1", "forward_port": 8000, "connection_ref": "example-npm",
         "certificate": {"name": "example wildcard", "domains": ["*.example.com"],
                         "expires_on": EXPIRES, "provider": "letsencrypt"}},
    )
    store("caddy.route", {"connection_ref": "example-edge", "domain": "shop.example.com",
                          "upstream": "shop:8080"})
    store(
        "portainer.container",
        {"host": "lab-1", "name": "app", "ports": [8000], "state": "running"},
        {"host": "edge-1", "name": "shop", "ports": [8080], "state": "running"},
    )
    store(
        "cloudflare.edge_certificate",
        {"connection_ref": CLOUDFLARE, "zone": "example.com", "id": "e1",
         "hosts": ["example.com", "*.example.com"], "certificate_authority": "lets_encrypt",
         "expires_on": EXPIRES},
    )
    store("cloudflare.pages_project", {"connection_ref": CLOUDFLARE, "name": "example-site",
                                       "subdomain": "example-site.pages.dev",
                                       "domains": ["example.com"]})
    store("cloudflare.redirect", {"connection_ref": CLOUDFLARE, "zone": "example.net",
                                  "source": "rule", "hostnames": ["example.net"],
                                  "target": "https://example.com", "target_host": "example.com",
                                  "status_code": 301, "enabled": True})


def steps(path):
    return [(hop.step, hop.name) for hop in path.primary.hops]


@PUBLIC_RANGE
class PathTests(TestCase):
    def setUp(self):
        estate()

    def walk(self, hostname):
        with projection_scope():
            return path_to(hostname)

    def test_a_proxied_public_name_meets_the_edge_then_its_origin(self):
        path = self.walk("shop.example.com")

        self.assertEqual(
            steps(path),
            [("dns", "A 198.51.100.20"), ("edge", "Cloudflare DNS"), ("network", ""),
             ("machine", "edge-1"), ("ingress", "example-edge"), ("upstream", "shop:8080"),
             ("machine", "edge-1"), ("container", "shop")],
        )
        edge = path.primary.hops[1].certificate
        self.assertEqual((edge.role, edge.issuer), ("Edge", "Let's Encrypt"))
        origin = path.primary.hops[4].certificate
        self.assertEqual(origin.role, "Origin")
        self.assertIn("Caddy route reports no certificate for this name", origin.unread)
        self.assertEqual(path.primary.hops[1].source.kind, "cloudflare.dns_record")
        self.assertEqual(path.primary.hops[4].source.connection, "example-edge")

    def test_a_tailnet_name_is_served_with_the_proxy_certificate(self):
        path = self.walk("app.example.com")

        self.assertEqual(
            steps(path),
            [("dns", "100.64.0.10"), ("network", ""), ("machine", "lab-1"),
             ("ingress", "example-npm"), ("upstream", "127.0.0.1:8000"),
             ("machine", "lab-1"), ("container", "app")],
        )
        self.assertEqual(path.primary.via, "Internal DNS record")
        self.assertEqual(path.primary.hops[1].label, "Tailnet")
        certificate = path.certificate
        self.assertEqual((certificate.role, certificate.name), ("Served", "example wildcard"))
        self.assertEqual(certificate.issuer, "Let's Encrypt")
        self.assertEqual(path.primary.line,
                         "Internal DNS record → Tailnet → lab-1 → Proxy host → app")

    def test_a_declared_certificate_is_shown_as_hq_knows_it_and_attested(self):
        # NPM names HQ's upload by its own label with no authority; HQ knows better.
        declare_certificate()

        certificate = self.walk("app.example.com").certificate

        self.assertEqual(
            (certificate.name, certificate.issuer, certificate.record),
            ("example-wildcard", "Example CA", "example-wildcard"),
        )
        self.assertEqual(certificate.link.url, reverse("control_plane:detail", args=["example-wildcard"]))
        self.assertTrue(certificate.line.startswith("Served certificate example-wildcard · Example CA, expires"))
        self.assertTrue(certificate.attestation.startswith("confirmed on this site"))
        self.assertNotIn("abababababab", certificate.attestation)

    def test_a_name_serving_another_certificate_is_not_shown_as_the_declared_one(self):
        declare_certificate(matches=False)

        certificate = self.walk("app.example.com").certificate

        self.assertEqual(certificate.name, "")
        self.assertEqual(certificate.attestation, "")
        self.assertEqual(
            certificate.line,
            "Not verified: app.example.com serves a certificate other than example-wildcard",
        )

    def test_a_container_hop_says_what_is_running_and_links_its_commit(self):
        from ..paths import _running_facts

        facts = _running_facts({
            "status": "Up 3 days (healthy)",
            "revision": "0123456789abcdef",
            "source": "https://github.com/example/app/",
        })

        # The uptime alone: the health check is the verdict's evidence, said
        # once in the band.
        self.assertEqual(facts, (
            ("Up 3 days", ""),
            ("0123456", "https://github.com/example/app/commit/0123456789abcdef"),
        ))
        # A source that is not a web address is not made into a link.
        self.assertEqual(_running_facts({"revision": "0123456789abcdef", "source": "git@x:y"}),
                         (("0123456", ""),))

    def test_only_the_certificate_the_ingress_names_serves_the_name(self):
        from ..services import certificates_serving

        declare_certificate("example-wildcard")
        declare_certificate("example-other")
        ManagedResource.objects.create(
            key="app-proxy", kind="npm.proxy_host",
            spec={"domain_names": ["app.example.com"], "forward_scheme": "http",
                  "forward_host": "127.0.0.1", "forward_port": 8000,
                  "certificate_resource": "example-other"},
        )

        self.assertEqual(certificates_serving("app.example.com"), ("example-other",))

    def test_a_pages_site_ends_at_its_project(self):
        path = self.walk("example.com")

        self.assertEqual(
            steps(path),
            [("dns", "CNAME example-site.pages.dev"), ("edge", "Cloudflare DNS"),
             ("served", "example-site")],
        )
        self.assertEqual(path.certificate.role, "Edge")

    def test_a_redirecting_apex_reads_as_a_redirect_not_parked(self):
        path = self.walk("example.net")

        self.assertEqual(path.redirects_to, "example.com")
        self.assertEqual(path.primary.hops[-1].step, "redirect")
        self.assertIn("Redirects to example.com", path.line)
        self.assertNotIn("Parked", path.line)

    def test_an_unread_redirect_is_named_with_its_reason(self):
        store("cloudflare.redirect", reachable=False, error="Authentication error")

        path = self.walk("example.net")

        self.assertEqual(path.primary.hops[-1].step, "origin")
        self.assertIn("not read: Redirect, because Authentication error", path.gaps)

    def test_an_unconnected_proxy_is_not_guessed(self):
        ProviderInventory.objects.filter(kind="npm.proxy_host").delete()

        path = self.walk("app.example.com")

        self.assertEqual(path.primary.hops[-1].step, "ingress")
        self.assertEqual(
            path.primary.hops[-1].unread, "not read: Proxy host, because no connection reads it"
        )

    def test_a_declaration_stands_in_for_a_kind_nothing_reads_and_says_so(self):
        ProviderInventory.objects.filter(kind="adguard.rewrite").delete()
        ManagedResource.objects.create(
            key="app-dns", kind="adguard.rewrite",
            spec={"domain": "app.example.com", "answer": "100.64.0.10"},
        )

        path = self.walk("app.example.com")

        self.assertEqual(path.primary.hops[0].source.declared, "app-dns")
        self.assertEqual(path.primary.hops[0].source.phrase, "In HQ as app-dns, not read")

    def test_a_name_nothing_resolves_says_why(self):
        path = self.walk("nothing.example.org")

        self.assertEqual(path.routes, ())
        self.assertEqual(path.gaps, ("No DNS record HQ reads names this host.",))

    def test_invalid_input_has_no_path(self):
        self.assertEqual(self.walk("not a host").routes, ())

    def test_the_api_refuses_a_name_that_is_not_a_host(self):
        from ..resources import ResourceNotFound

        with self.assertRaises(ResourceNotFound):
            get_resource("paths", "not a host", principal=READ)

    def test_why_unread_names_the_error(self):
        store("caddy.route", reachable=False, error="Connection refused.")

        self.assertEqual(why_unread("caddy.route"),
                         "not read: Caddy route, because Connection refused")


@PUBLIC_RANGE
class ReadTests(TestCase):
    def setUp(self):
        estate()

    def test_the_api_returns_the_path_the_page_walks(self):
        found = get_resource("paths", "app.example.com", principal=READ)

        route = found["routes"][0]
        self.assertEqual(route["via"], "Internal DNS record")
        self.assertEqual([hop["step"] for hop in route["hops"]][:3], ["dns", "network", "machine"])
        self.assertEqual(route["hops"][3]["certificate"]["name"], "example wildcard")

    def test_a_reader_without_read_is_refused(self):
        from ..resources import ResourceError
        from ..security import AuthorizationError

        with self.assertRaises((AuthorizationError, ResourceError)):
            get_resource("paths", "app.example.com",
                         principal=Principal("nobody", "test", frozenset()))


@PUBLIC_RANGE
@override_settings(SEVERINO_SITE_HOST="app.example.com", ALLOWED_HOSTS=["app.example.com", "testserver"])
class HqPathTests(TestCase):
    def setUp(self):
        estate()

    def test_hq_path_ends_at_hq(self):
        with projection_scope():
            path = hq_path()

        hops = path.primary.hops
        self.assertEqual(hops[-1].step, "hq")
        self.assertEqual([hop.step for hop in hops][:3], ["dns", "network", "machine"])

    def test_the_connections_page_renders_hq_path(self):
        user = get_user_model().objects.create_user("operator", password="pw", is_staff=True)
        self.client.force_login(user)

        response = self.client.get(reverse("control_plane:connections"))

        self.assertContains(response, "How this page reached you")
        self.assertContains(response, "Proxy host")


@PUBLIC_RANGE
class ListedServiceTests(TestCase):
    """HQ's name and observed names are rows of the one services list."""

    def setUp(self):
        estate()
        ManagedResource.objects.create(
            key="example-com-zone", kind="cloudflare.zone",
            spec={"zone": "example.com", "connection_ref": CLOUDFLARE},
        )
        ManagedResource.objects.create(
            key="example-net-zone", kind="cloudflare.zone",
            spec={"zone": "example.net", "connection_ref": CLOUDFLARE},
        )
        store("cloudflare.access_app", {"connection_ref": CLOUDFLARE, "id": "a1", "name": "Admin",
                                        "domain": "admin.example.com"})
        user = get_user_model().objects.create_user("operator", password="pw", is_staff=True)
        self.client.force_login(user)

    def listed(self):
        from ..service_list import listed_services

        with projection_scope():
            return {service.hostname: service for service in listed_services()}

    def test_names_readings_serve_are_listed_as_observed(self):
        found = self.listed()

        self.assertTrue(found["admin.example.com"].is_observed)
        # The badge says observed; the state says what the reading does.
        self.assertEqual(
            found["admin.example.com"].status_label, "Read only"
        )
        self.assertTrue(found["example.net"].is_observed)
        self.assertEqual(found["example.net"].status_label, "Read only")
        self.assertEqual(found["example.net"].health.detail, "Redirects to example.com")
        # A certificate's names are not services.
        self.assertNotIn("*.example.com", found)

    def test_a_name_outside_every_known_domain_is_not_listed(self):
        self.assertNotIn("example-site.pages.dev", self.listed())

    def test_an_observed_row_links_to_a_page_that_is_not_empty(self):
        response = self.client.get(reverse("control_plane:services"))
        self.assertContains(response, "Read only")
        self.assertContains(response, reverse("control_plane:service", args=["admin.example.com"]))

        page = self.client.get(reverse("control_plane:service", args=["example.net"]))
        self.assertContains(page, "Redirects to")

    def test_the_redirect_is_a_relation_between_services(self):
        from ..topology import derive_topology

        with projection_scope():
            edges = derive_topology(principal=READ).edges
        self.assertIn(
            ("service:example.net", "service:example.com", "redirects_to"),
            {(edge.source, edge.target, edge.kind) for edge in edges},
        )

    @override_settings(SEVERINO_SITE_HOST="app.example.com",
                       ALLOWED_HOSTS=["app.example.com", "testserver"])
    def test_hq_is_a_marked_row_not_a_second_table(self):
        found = self.listed()

        self.assertTrue(found["app.example.com"].is_hq)
        response = self.client.get(reverse("control_plane:services"))
        # One row among the rest, with no mark widening its column.
        self.assertNotContains(response, '<span class="pill">HQ</span>', html=False)
        self.assertContains(response, 'app.example.com</a>', html=False)


@PUBLIC_RANGE
class ServicePageTests(TestCase):
    """Summary first, then the path hop by hop, then what it depends on."""

    def setUp(self):
        estate()
        user = get_user_model().objects.create_user("operator", password="pw", is_staff=True)
        self.client.force_login(user)

    def page(self, hostname):
        return self.client.get(reverse("control_plane:service", args=[hostname])).content.decode()

    def test_the_summary_comes_before_the_path_and_the_impact(self):
        response = self.client.get(reverse("control_plane:service", args=["app.example.com"]))
        page = response.content.decode()

        summary = {item.label: item for item in response.context["summary"]}
        # The path is drawn once, in its own section, not repeated as a card.
        self.assertNotIn("Path", summary)
        # Where it runs is the path's to say, hop by hop, not a tile above it.
        self.assertNotIn("Where it runs", summary)
        self.assertIn('/infrastructure/machines/lab-1/', page)
        # Health first; "What it is" only when it says more than "declared".
        self.assertNotIn("What it is", summary)
        order = [page.index(text) for text in (
            ">Health<", ">Certificate<",
            'id="path"',
        )]
        self.assertEqual(order, sorted(order))
        self.assertIn("Served certificate example wildcard · Let&#x27;s Encrypt", page)
        self.assertIn("If it is removed, the name stops reaching the app.", page)

    def test_the_served_certificate_is_a_verified_lock_with_a_short_tip(self):
        declare_certificate()

        page = self.page("app.example.com")

        # Not a link of its own: the hop's name beside it already is one.
        self.assertIn('<span class="cert-mark" tabindex="0" data-tip="Confirmed\nexample-wildcard · Example CA', page)
        self.assertIn('data-tip-host="app.example.com"', page)

    def test_a_redirecting_apex_is_not_parked_and_its_target_names_it(self):
        page = self.page("example.net")
        self.assertIn("Redirects to example.com", page)
        self.assertNotIn("Parked", page)

        # Said once, where every relation is: under Relationships.
        target = self.page("example.com")
        self.assertIn("Redirected from", target)

    def test_a_hop_nothing_reads_says_why_on_the_page(self):
        ProviderInventory.objects.filter(kind="npm.proxy_host").delete()

        self.assertIn("Not read: Proxy host, because no connection reads it", self.page("app.example.com"))


@PUBLIC_RANGE
class ObservedNameTests(TestCase):
    """A name only a reading names starts its path from what observed it."""

    def setUp(self):
        estate()
        ManagedResource.objects.create(
            key="example-com-zone", kind="cloudflare.zone",
            spec={"zone": "example.com", "connection_ref": CLOUDFLARE},
        )
        store("cloudflare.access_app", {"connection_ref": CLOUDFLARE, "id": "a1", "name": "Admin",
                                        "domain": "admin.example.com"})
        # No connection reads internal DNS.
        ProviderInventory.objects.filter(kind="adguard.rewrite").delete()
        user = get_user_model().objects.create_user("operator", password="pw", is_staff=True)
        self.client.force_login(user)

    def walk(self, hostname):
        with projection_scope():
            return path_to(hostname)

    def test_what_was_read_comes_before_what_was_not(self):
        path = self.walk("admin.example.com")

        self.assertEqual(
            path.gaps,
            (
                "No public DNS record names this host.",
                "not read: Internal DNS record, because no connection reads it",
            ),
        )
        (hop,) = path.observed
        self.assertEqual((hop.step, hop.label, hop.name), ("observed", "Behind Access", "Admin"))
        self.assertEqual(hop.source.connection, CLOUDFLARE)
        self.assertEqual(path.observed_line, "Behind Access")

    def test_a_redirect_observer_names_where_it_sends(self):
        self.assertEqual(self.walk("example.net").observed_line, "Redirects to example.com")

    def test_the_page_says_no_record_and_access_first(self):
        response = self.client.get(reverse("control_plane:service", args=["admin.example.com"]))
        page = response.content.decode()
        summary = {item.label: item for item in response.context["summary"]}

        self.assertIn("No public DNS record names this host.", page)
        self.assertEqual(summary["Health"].value, "Read only")
        self.assertEqual(summary["Health"].detail, "Behind Access · No DNS record")
        self.assertEqual(
            summary["What it is"].value,
            "Read through Access application Admin. Not in HQ's settings.",
        )
        section = page[page.index('id="path"'):page.index('id="parts"')]
        self.assertLess(section.index("No public DNS record"), section.index("Not read: Internal"))
        self.assertIn("Behind Access:", section)

    def test_the_public_record_is_offered_first_in_a_public_zone(self):
        page = self.client.get(
            reverse("control_plane:service", args=["admin.example.com"])
        ).content.decode()

        self.assertLess(page.index("Add public DNS record"), page.index("Add internal DNS record"))

    def test_outside_a_public_zone_the_order_is_the_registry_order(self):
        page = self.client.get(
            reverse("control_plane:service", args=["box.example.test"])
        ).content.decode()

        self.assertLess(page.index("Add internal DNS record"), page.index("Add public DNS record"))

    def test_the_row_names_its_observer_and_says_observed_once(self):
        response = self.client.get(reverse("control_plane:services"))
        row = next(
            chunk for chunk in response.content.decode().split("<tr>")
            if 'data-entity="Service">admin.example.com</a>' in chunk
        )

        self.assertEqual(row.count("Read only"), 1)
        self.assertNotIn("Observed", row)
        self.assertIn(">Cloudflare Access<", row)
        self.assertIn('<span class="muted">No record</span>', row)
        self.assertNotIn("service-observed", row)

    def test_the_api_returns_the_observing_readings(self):
        from ..service_list import get_service

        found = get_service("admin.example.com")

        (observed,) = found["path"]["observed"]
        self.assertEqual((observed["relation"], observed["name"]), ("Behind Access", "Admin"))
        self.assertEqual(found["service"]["status_label"], "Read only")


@PUBLIC_RANGE
@override_settings(SEVERINO_SITE_HOST="hq.example.com", ALLOWED_HOSTS=["hq.example.com", "testserver"])
class HqHealthTests(TestCase):
    """HQ's health is what HQ knows about itself, never its mode."""

    def setUp(self):
        estate()
        user = get_user_model().objects.create_user("operator", password="pw", is_staff=True)
        self.client.force_login(user)

    def summary(self):
        response = self.client.get(reverse("control_plane:service", args=["hq.example.com"]))
        return {item.label: item for item in response.context["summary"]}

    def test_hq_answering_this_request_is_up(self):
        health = self.summary()["Health"]

        self.assertEqual((health.value, health.tone), ("Up", "good"))
        self.assertEqual(health.detail, "Answering this request.")

    def test_a_finding_about_hq_needs_attention(self):
        with mock.patch("hq.platform.application.service_health.hq_findings", return_value=("one",)):
            health = self.summary()["Health"]

        self.assertEqual((health.value, health.tone), ("Up", "attention"))
        self.assertIn("1 open problem is about HQ.", health.detail)

    def test_only_findings_about_hq_count(self):
        from ..finding_model import Finding
        from ..service_health import _hq_findings

        def finding(subject):
            return Finding("example", subject, "t", "attention", "e")

        raised = (
            finding("service:hq.example.com"),
            finding("controller:abc"),
            finding("service:shop.example.com"),
            finding("machine:edge-1"),
        )
        with projection_scope(), mock.patch(
            "hq.platform.application.findings.derive_findings", return_value=raised
        ):
            found = _hq_findings()

        self.assertEqual(
            [item.subject for item in found], ["service:hq.example.com", "controller:abc"]
        )

    def test_a_name_nothing_observes_or_declares_is_nothing_declared(self):
        from ..services import prospects

        with projection_scope():
            (service,) = prospects(("nothing.example.org",))

        self.assertEqual(service.status_label, "Nothing set up in HQ")


class RoutedNamesTests(TestCase):
    """The names HQ knows a record for, and whether it knows them all."""

    def test_a_declared_record_the_last_read_missed_still_names_its_host(self):
        from ..paths import routed_names

        estate()
        ManagedResource.objects.create(
            key="declared-rewrite", kind="adguard.rewrite",
            spec={"domain": "declared.example.com", "answer": "100.64.0.10"},
        )

        with projection_scope():
            names = routed_names()

        self.assertIn("declared.example.com", names)
        self.assertIn("app.example.com", names)

    def test_every_route_is_read_only_when_every_routing_kind_answered(self):
        from ..paths import reads_every_route

        estate()
        with projection_scope():
            self.assertTrue(reads_every_route())

        ProviderInventory.objects.filter(kind="caddy.route").delete()
        with projection_scope():
            self.assertFalse(reads_every_route())
