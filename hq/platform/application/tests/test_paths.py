"""The request path: each hop from a reading, and a stated reason where none was read."""

from datetime import timedelta
from ipaddress import ip_network
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.text import capfirst

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
    return {
        "zone": zone,
        "name": name,
        "record_type": record_type,
        "content": content,
        "proxied": proxied,
        "ttl": 1,
        "connection_ref": "example-dns",
    }


def declare_certificate(key="example-wildcard", *, matches=True):
    """A certificate HQ installed for app.example.com, as its status records it."""

    return ManagedResource.objects.create(
        key=key,
        kind="tls.certificate",
        spec={"certificate_name": "example", "domains": ["*.example.com"]},
        status={
            "issuer": "Example CA",
            "not_after": EXPIRES,
            "consumers": [
                {
                    "domain": "app.example.com",
                    "consumer_kind": "npm",
                    "fingerprint_sha256": "ab" * 32,
                    "matches_expected": matches,
                }
            ],
        },
        last_observed_at=timezone.now(),
    )


def estate():
    """A proxied name, a tailnet name, a Pages site and a redirecting apex."""

    for key, addresses in (("edge-1", ["198.51.100.20"]), ("lab-1", ["100.64.0.10"])):
        ManagedResource.objects.create(key=key, kind="machine", spec={"name": key, "addresses": addresses})
    store(
        "cloudflare.dns_record",
        record("shop.example.com", "A", "198.51.100.20"),
        record("example.com", "CNAME", "example-site.pages.dev"),
        record("example.net", "A", "192.0.2.1", zone="example.net"),
    )
    store(
        "adguard.rewrite", {"domain": "app.example.com", "answer": "100.64.0.10", "connection_ref": "example-adguard"}
    )
    store(
        "npm.proxy_host",
        {
            "domain_names": ["app.example.com"],
            "forward_scheme": "http",
            "forward_host": "127.0.0.1",
            "forward_port": 8000,
            "connection_ref": "example-npm",
            "certificate": {
                "name": "example wildcard",
                "domains": ["*.example.com"],
                "expires_on": EXPIRES,
                "provider": "letsencrypt",
            },
        },
    )
    store("caddy.route", {"connection_ref": "example-edge", "domain": "shop.example.com", "upstream": "shop:8080"})
    store(
        "portainer.container",
        {"host": "lab-1", "name": "app", "ports": [8000], "state": "running"},
        {"host": "edge-1", "name": "shop", "ports": [8080], "state": "running"},
    )
    store(
        "cloudflare.edge_certificate",
        {
            "connection_ref": CLOUDFLARE,
            "zone": "example.com",
            "id": "e1",
            "hosts": ["example.com", "*.example.com"],
            "certificate_authority": "lets_encrypt",
            "expires_on": EXPIRES,
        },
    )
    store(
        "cloudflare.pages_project",
        {
            "connection_ref": CLOUDFLARE,
            "name": "example-site",
            "subdomain": "example-site.pages.dev",
            "domains": ["example.com"],
        },
    )
    store(
        "cloudflare.redirect",
        {
            "connection_ref": CLOUDFLARE,
            "zone": "example.net",
            "source": "rule",
            "hostnames": ["example.net"],
            "target": "https://example.com",
            "target_host": "example.com",
            "status_code": 301,
            "enabled": True,
        },
    )


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
            [
                ("dns", "198.51.100.20"),
                ("edge", "Cloudflare DNS"),
                ("network", ""),
                ("machine", "edge-1"),
                ("ingress", "example-edge"),
                ("upstream", "shop:8080"),
                ("machine", "edge-1"),
                ("container", "shop"),
            ],
        )
        edge = path.primary.hops[1].certificate
        self.assertEqual((edge.role, edge.issuer), ("Edge", "Let's Encrypt"))
        origin = path.primary.hops[4].certificate
        self.assertEqual(origin.role, "Origin")
        self.assertTrue(origin.unread)
        self.assertEqual(path.primary.hops[1].source.kind, "cloudflare.dns_record")
        self.assertEqual(path.primary.hops[4].source.connection, "example-edge")

    def test_a_tailnet_name_is_served_with_the_proxy_certificate(self):
        path = self.walk("app.example.com")

        self.assertEqual(
            steps(path),
            [
                ("dns", "100.64.0.10"),
                ("network", ""),
                ("machine", "lab-1"),
                ("ingress", "example-npm"),
                ("upstream", "127.0.0.1:8000"),
                ("machine", "lab-1"),
                ("container", "app"),
            ],
        )
        self.assertEqual(path.primary.via, "Internal DNS")
        self.assertEqual(path.primary.hops[1].label, "Tailnet")
        certificate = path.certificate
        self.assertEqual((certificate.role, certificate.name), ("Served", "example wildcard"))
        self.assertEqual(certificate.issuer, "Let's Encrypt")

    def test_a_declared_certificate_is_shown_as_hq_knows_it_and_attested(self):
        # NPM names HQ's upload by its own label with no authority; HQ knows better.
        declare_certificate()

        certificate = self.walk("app.example.com").certificate

        self.assertEqual(
            (certificate.name, certificate.issuer, certificate.record),
            ("example-wildcard", "Example CA", "example-wildcard"),
        )
        self.assertEqual(certificate.link.url, reverse("control_plane:detail", args=["example-wildcard"]))
        self.assertIn("example-wildcard", certificate.line)
        self.assertTrue(certificate.attestation)
        # The fingerprint is the evidence, not the words.
        self.assertNotIn("abababababab", certificate.attestation)

    def test_a_name_serving_another_certificate_is_not_shown_as_the_declared_one(self):
        declare_certificate(matches=False)

        certificate = self.walk("app.example.com").certificate

        self.assertEqual((certificate.name, certificate.attestation), ("", ""))
        self.assertIn("example-wildcard", certificate.line)

    def test_a_container_hop_says_what_is_running_and_links_its_commit(self):
        from ..paths import _running_facts

        facts = _running_facts(
            {
                "status": "Up 3 days (healthy)",
                "revision": "0123456789abcdef",
                "source": "https://github.com/example/app/",
            }
        )

        # The uptime alone: the health check is the verdict's evidence, said
        # once in the band.
        self.assertEqual(
            facts,
            (
                ("Up 3 days", ""),
                ("0123456", "https://github.com/example/app/commit/0123456789abcdef"),
            ),
        )
        # A source that is not a web address is not made into a link.
        self.assertEqual(_running_facts({"revision": "0123456789abcdef", "source": "git@x:y"}), (("0123456", ""),))

    def test_only_the_certificate_the_ingress_names_serves_the_name(self):
        from ..services import certificates_serving

        declare_certificate("example-wildcard")
        declare_certificate("example-other")
        ManagedResource.objects.create(
            key="app-proxy",
            kind="npm.proxy_host",
            spec={
                "domain_names": ["app.example.com"],
                "forward_scheme": "http",
                "forward_host": "127.0.0.1",
                "forward_port": 8000,
                "certificate_resource": "example-other",
            },
        )

        self.assertEqual(certificates_serving("app.example.com"), ("example-other",))

    def test_a_pages_site_ends_at_its_project(self):
        path = self.walk("example.com")

        self.assertEqual(
            steps(path),
            [("dns", "example-site.pages.dev"), ("edge", "Cloudflare DNS"), ("served", "example-site")],
        )
        self.assertEqual(path.certificate.role, "Edge")

    def test_a_redirecting_apex_reads_as_a_redirect_not_parked(self):
        path = self.walk("example.net")

        self.assertEqual(path.redirects_to, "example.com")
        self.assertEqual(path.primary.hops[-1].step, "redirect")
        self.assertIn("example.com", path.line)

    def test_an_unread_redirect_is_named_with_its_reason(self):
        store("cloudflare.redirect", reachable=False, error="Authentication error")

        path = self.walk("example.net")

        self.assertEqual(path.primary.hops[-1].step, "origin")
        self.assertIn(why_unread("cloudflare.redirect"), path.gaps)

    def test_an_unconnected_proxy_is_not_guessed(self):
        ProviderInventory.objects.filter(kind="npm.proxy_host").delete()

        path = self.walk("app.example.com")

        self.assertEqual(path.primary.hops[-1].step, "ingress")
        self.assertEqual(path.primary.hops[-1].unread, why_unread("npm.proxy_host"))

    def test_a_declaration_stands_in_for_a_kind_nothing_reads_and_says_so(self):
        ProviderInventory.objects.filter(kind="adguard.rewrite").delete()
        ManagedResource.objects.create(
            key="app-dns",
            kind="adguard.rewrite",
            spec={"domain": "app.example.com", "answer": "100.64.0.10"},
        )

        path = self.walk("app.example.com")

        self.assertEqual(path.primary.hops[0].source.declared, "app-dns")
        self.assertIn("app-dns", path.primary.hops[0].source.phrase)

    def test_a_name_nothing_resolves_says_why(self):
        path = self.walk("nothing.example.org")

        self.assertEqual(path.routes, ())
        (gap,) = path.gaps
        self.assertTrue(gap)

    def test_invalid_input_has_no_path(self):
        self.assertEqual(self.walk("not a host").routes, ())

    def test_the_api_refuses_a_name_that_is_not_a_host(self):
        from ..record_errors import RecordNotFound

        with self.assertRaises(RecordNotFound):
            get_resource("paths", "not a host", principal=READ)

    def test_why_unread_names_the_error(self):
        store("caddy.route", reachable=False, error="Connection refused.")

        # The reason is the product's words: the kind, then what refused it.
        self.assertEqual(why_unread("caddy.route"), "not read: Caddy route, because Connection refused")
        store("caddy.route", connected=False)
        self.assertEqual(why_unread("caddy.route"), "not read: Caddy route, because no connection reads it")


@PUBLIC_RANGE
class ReadTests(TestCase):
    def setUp(self):
        estate()

    def test_the_api_returns_the_path_the_page_walks(self):
        found = get_resource("paths", "app.example.com", principal=READ)

        route = found["routes"][0]
        self.assertEqual(route["via"], "Internal DNS")
        self.assertEqual([hop["step"] for hop in route["hops"]][:3], ["dns", "network", "machine"])
        self.assertEqual(route["hops"][3]["certificate"]["name"], "example wildcard")

    def test_a_reader_without_read_is_refused(self):
        from ..resources import ResourceError
        from ..security import AuthorizationError

        with self.assertRaises((AuthorizationError, ResourceError)):
            get_resource("paths", "app.example.com", principal=Principal("nobody", "test", frozenset()))


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

        path = response.context["connections"].hq_path
        self.assertEqual(path.primary.hops[-1].step, "hq")
        self.assertContains(response, reverse("control_plane:machine", args=["lab-1"]))


@PUBLIC_RANGE
class ListedServiceTests(TestCase):
    """HQ's name and observed names are rows of the one services list."""

    def setUp(self):
        estate()
        ManagedResource.objects.create(
            key="example-com-zone",
            kind="cloudflare.zone",
            spec={"zone": "example.com", "connection_ref": CLOUDFLARE},
        )
        ManagedResource.objects.create(
            key="example-net-zone",
            kind="cloudflare.zone",
            spec={"zone": "example.net", "connection_ref": CLOUDFLARE},
        )
        store(
            "cloudflare.access_app",
            {"connection_ref": CLOUDFLARE, "id": "a1", "name": "Admin", "domain": "admin.example.com"},
        )
        user = get_user_model().objects.create_user("operator", password="pw", is_staff=True)
        self.client.force_login(user)

    def listed(self):
        from ..service_list import listed_services

        with projection_scope():
            return {service.hostname: service for service in listed_services()}

    def test_names_readings_serve_are_listed_as_observed(self):
        found = self.listed()

        for name in ("admin.example.com", "example.net"):
            self.assertTrue(found[name].is_observed, name)
            self.assertEqual(found[name].status, "unknown", name)
        self.assertEqual(found["example.net"].health.detail, found["example.net"].path.observed_line)
        self.assertEqual(found["example.net"].path.redirects_to, "example.com")
        # A certificate's names are not services.
        self.assertNotIn("*.example.com", found)

    def test_a_name_outside_every_known_domain_is_not_listed(self):
        self.assertNotIn("example-site.pages.dev", self.listed())

    def test_an_observed_row_links_to_a_page_that_is_not_empty(self):
        response = self.client.get(reverse("control_plane:services"))
        self.assertContains(response, reverse("control_plane:service", args=["admin.example.com"]))

        page = self.client.get(reverse("control_plane:service", args=["example.net"]))
        self.assertContains(page, reverse("control_plane:service", args=["example.com"]))

    def test_the_redirect_is_a_relation_between_services(self):
        from ..topology import derive_topology

        with projection_scope():
            edges = derive_topology(principal=READ).edges
        self.assertIn(
            ("service:example.net", "service:example.com", "redirects_to"),
            {(edge.source, edge.target, edge.kind) for edge in edges},
        )

    @override_settings(SEVERINO_SITE_HOST="app.example.com", ALLOWED_HOSTS=["app.example.com", "testserver"])
    def test_hq_is_a_marked_row_not_a_second_table(self):
        found = self.listed()

        self.assertTrue(found["app.example.com"].is_hq)
        response = self.client.get(reverse("control_plane:services"))
        self.assertContains(response, reverse("control_plane:service", args=["app.example.com"]))


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

        labels = [item.label for item in response.context["summary"]]
        # Health first; the path is drawn once, in its own section, and says where it runs.
        self.assertEqual(labels[:2], ["Health", "Certificate"])
        self.assertNotIn("Path", labels)
        self.assertNotIn("Where it runs", labels)
        self.assertIn(reverse("control_plane:machine", args=["lab-1"]), page)
        self.assertLess(page.index('id="path"'), page.index('id="parts"'))

    def test_the_served_certificate_is_a_verified_lock_with_a_short_tip(self):
        declare_certificate()

        page = self.page("app.example.com")

        self.assertIn('data-tip-host="app.example.com"', page)
        self.assertIn("example-wildcard · Example CA", page)

    def test_a_redirect_links_both_ends(self):
        self.assertIn(reverse("control_plane:service", args=["example.com"]), self.page("example.net"))
        self.assertIn(reverse("control_plane:service", args=["example.net"]), self.page("example.com"))

    def test_a_hop_nothing_reads_says_why_on_the_page(self):
        ProviderInventory.objects.filter(kind="npm.proxy_host").delete()

        self.assertIn(capfirst(why_unread("npm.proxy_host")), self.page("app.example.com"))


@PUBLIC_RANGE
class ObservedNameTests(TestCase):
    """A name only a reading names starts its path from what observed it."""

    def setUp(self):
        estate()
        ManagedResource.objects.create(
            key="example-com-zone",
            kind="cloudflare.zone",
            spec={"zone": "example.com", "connection_ref": CLOUDFLARE},
        )
        store(
            "cloudflare.access_app",
            {"connection_ref": CLOUDFLARE, "id": "a1", "name": "Admin", "domain": "admin.example.com"},
        )
        # No connection reads internal DNS.
        ProviderInventory.objects.filter(kind="adguard.rewrite").delete()
        user = get_user_model().objects.create_user("operator", password="pw", is_staff=True)
        self.client.force_login(user)

    def walk(self, hostname):
        with projection_scope():
            return path_to(hostname)

    def test_what_was_read_comes_before_what_was_not(self):
        path = self.walk("admin.example.com")

        missing, unread = path.gaps
        self.assertTrue(missing)
        self.assertEqual(unread, why_unread("adguard.rewrite"))
        (hop,) = path.observed
        self.assertEqual((hop.step, hop.label, hop.name), ("observed", "Behind Access", "Admin"))
        self.assertEqual(hop.source.connection, CLOUDFLARE)
        self.assertEqual(path.observed_line, hop.label)

    def test_a_redirect_observer_names_where_it_sends(self):
        self.assertIn("example.com", self.walk("example.net").observed_line)

    def test_the_page_says_no_record_and_access_first(self):
        response = self.client.get(reverse("control_plane:service", args=["admin.example.com"]))
        page = response.content.decode()
        summary = {item.label: item for item in response.context["summary"]}
        path = self.walk("admin.example.com")

        self.assertEqual(summary["Health"].tone, "unknown")
        self.assertIn(path.observed_line, summary["Health"].detail)
        self.assertIn("Admin", summary["What it is"].value)
        section = page[page.index('id="path"') : page.index('id="parts"')]
        missing, unread = path.gaps
        self.assertLess(section.index(missing), section.index(capfirst(unread)))

    def test_the_public_record_is_offered_first_in_a_public_zone(self):
        self.assertEqual(self.offered("admin.example.com"), ["cloudflare.dns_record", "adguard.rewrite"])

    def test_outside_a_public_zone_the_order_is_the_registry_order(self):
        self.assertEqual(self.offered("box.example.test"), ["adguard.rewrite", "cloudflare.dns_record"])

    def offered(self, hostname):
        page = self.client.get(reverse("control_plane:service", args=[hostname])).content.decode()
        kinds = ("cloudflare.dns_record", "adguard.rewrite")
        return sorted(kinds, key=lambda kind: page.index(f"kind={kind}"))

    def test_the_row_links_its_observer(self):
        (hop,) = self.walk("admin.example.com").observed

        response = self.client.get(reverse("control_plane:services"))

        self.assertContains(response, hop.link.url)
        self.assertContains(response, hop.source.short)

    def test_the_api_returns_the_observing_readings(self):
        from ..service_list import get_service

        found = get_service("admin.example.com")

        (observed,) = found["path"]["observed"]
        self.assertEqual((observed["relation"], observed["name"]), ("Behind Access", "Admin"))
        self.assertEqual(found["service"]["status"], "unknown")


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

        self.assertEqual(health.tone, "good")

    def test_a_finding_about_hq_needs_attention(self):
        with mock.patch("hq.platform.application.service_health.hq_findings", return_value=("one",)):
            health = self.summary()["Health"]

        self.assertEqual(health.tone, "attention")
        self.assertIn("1 open problem", health.detail)

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
        with projection_scope(), mock.patch("hq.platform.application.findings.derive_findings", return_value=raised):
            found = _hq_findings()

        self.assertEqual([item.subject for item in found], ["service:hq.example.com", "controller:abc"])

    def test_a_name_nothing_observes_or_declares_is_nothing_declared(self):
        from ..services import prospects

        with projection_scope():
            (service,) = prospects(("nothing.example.org",))

        self.assertEqual(service.status, "unknown")
        self.assertFalse(service.is_observed)


class RoutedNamesTests(TestCase):
    """The names HQ knows a record for, and whether it knows them all."""

    def test_a_declared_record_the_last_read_missed_still_names_its_host(self):
        from ..paths import routed_names

        estate()
        ManagedResource.objects.create(
            key="declared-rewrite",
            kind="adguard.rewrite",
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
