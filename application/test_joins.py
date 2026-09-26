"""One join engine: the domain page, the services and the topology read readings through it."""

from __future__ import annotations

import re
from datetime import timedelta
from ipaddress import ip_network
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import connection as database_connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from control_plane.dns_lookup import LookupNotFound, LookupUnavailable
from control_plane.models import ManagedResource, ProviderConnection, ProviderInventory
from control_plane.observations.public_registry import ADDRESS_KIND, DOMAIN_KIND

from .facts import Subject, readings
from .inventory import inventory_state
from .projection import projection_scope
from .security import Capability, Principal, cli_principal
from .services import service_catalog
from .topology import derive_topology
from .relationships import relationships_for
from .zone_insights import certificates, registration
from .zones import ZONE_KIND, find_zone

READ = Principal("reader", "test", frozenset({Capability.READ}))
CLOUDFLARE = "example-cloudflare"
SOON = (timezone.now() + timedelta(days=40)).isoformat()
LATER = (timezone.now() + timedelta(days=80)).isoformat()


def store(kind, *records, reachable=True, connected=True, error="", age=timedelta(0)):
    ProviderInventory.objects.update_or_create(
        kind=kind,
        defaults={
            "records": list(records),
            "reachable": reachable,
            "connected": connected,
            "error": error,
            "observed_at": timezone.now() - age,
            "controller_id": "example-controller",
        },
    )


def proxied(name, record_type, content, zone="example.com", on=True):
    """A swept Cloudflare record, proxied unless ``on`` is false."""

    return {"zone": zone, "name": name, "record_type": record_type,
            "content": content, "proxied": on, "ttl": 1}


def declare_record(key, name, record_type, content, zone="example.com"):
    return ManagedResource.objects.create(
        key=key,
        kind="cloudflare.dns_record",
        spec={
            "zone": zone,
            "name": name,
            "record_type": record_type,
            "content": content,
            "connection_ref": CLOUDFLARE,
        },
    )


EDGE = (
    {"connection_ref": CLOUDFLARE, "zone": "example.com", "id": "e1",
     "hosts": ["example.com", "*.example.com"], "status": "active",
     "certificate_authority": "example_ca", "expires_on": SOON},
    {"connection_ref": CLOUDFLARE, "zone": "example.com", "id": "e2",
     "hosts": ["www.example.com"], "status": "active",
     "certificate_authority": "other_ca", "expires_on": LATER},
    {"connection_ref": CLOUDFLARE, "zone": "example.net", "id": "e3",
     "hosts": ["example.net"], "certificate_authority": "example_ca",
     "expires_on": SOON},
)
PAGES = {
    "connection_ref": CLOUDFLARE, "name": "example-site",
    "subdomain": "example-site.pages.dev", "domains": ["example.com"],
}
ACCESS = {
    "connection_ref": CLOUDFLARE, "id": "a1", "name": "Admin",
    "domain": "admin.example.com", "policies": [{"id": "p1", "name": "Operators"}],
}
TUNNEL = {
    "connection_ref": CLOUDFLARE, "id": "t1", "name": "example-tunnel",
    "ingress": [{"hostname": "app.example.com", "service": "http://localhost:8080"}],
}


def estate():
    """A zone with two edge certificates, a Pages project, an Access app and a tunnel."""

    ManagedResource.objects.create(
        key="example-com", kind=ZONE_KIND,
        spec={"zone": "example.com", "connection_ref": CLOUDFLARE},
    )
    store(ZONE_KIND, {"zone": "example.com", "connection_ref": CLOUDFLARE})
    # Proxied: an edge certificate serves a name only through a proxied record.
    store(
        "cloudflare.dns_record",
        *(
            {"zone": "example.com", "name": name, "record_type": "CNAME",
             "content": content, "proxied": True, "ttl": 1}
            for name, content in (
                ("example.com", "example-site.pages.dev"),
                ("admin.example.com", "example-access.example.net"),
                ("app.example.com", "example-tunnel.example.net"),
            )
        ),
    )
    declare_record("apex", "example.com", "CNAME", "example-site.pages.dev")
    declare_record("admin", "admin.example.com", "CNAME", "example-access.example.net")
    declare_record("app", "app.example.com", "CNAME", "example-tunnel.example.net")
    store("cloudflare.edge_certificate", *EDGE)
    store("cloudflare.pages_project", PAGES)
    store("cloudflare.access_app", ACCESS)
    store("cloudflare.tunnel", TUNNEL)


class EngineTests(TestCase):
    def setUp(self):
        estate()

    def test_a_zone_joins_every_name_under_it_and_nothing_outside(self):
        found = readings().about(Subject.of(zones=("example.com",)), facets=("certificate",))

        self.assertEqual([item.record["id"] for item in found], ["e1", "e2"])

    def test_a_wildcard_key_covers_one_label_below_it(self):
        found = readings().about(Subject.of(hostnames=("app.example.com",)))

        self.assertEqual(
            {(item.kind, item.relation) for item in found},
            {
                ("cloudflare.edge_certificate", "Covered by edge certificate"),
                ("cloudflare.tunnel", "Published through tunnel"),
            },
        )

    def test_the_index_is_read_once_per_projection(self):
        with projection_scope():
            readings()
            with CaptureQueriesContext(database_connection) as queries:
                readings().about(Subject.of(zones=("example.com",)))
        self.assertEqual(len(queries), 0)


class DomainCardTests(TestCase):
    def setUp(self):
        estate()
        self.zone = find_zone("example.com")

    def test_edge_certificates_cover_the_zone_with_the_earliest_expiry_and_issuers(self):
        card = certificates(self.zone)

        self.assertEqual(card.value, "2 edge certificates")
        self.assertIn("Earliest expires", card.detail)
        self.assertIn("40 days", card.detail)
        self.assertIn("example_ca, other_ca", card.detail)

    def relationships(self):
        return relationships_for("zone:example.com", principal=READ)

    def services_card(self):
        return next(i for i in self.zone.insights if i.label == "Services")

    def test_the_pages_project_serving_the_apex_is_named(self):
        card = self.services_card()

        (label, (link,)), = card.links
        self.assertEqual((label, link.label), ("Served by", "example-site"))
        self.assertEqual(self.relationships().labels("Served by Pages project"), ("example-site",))

    def test_access_apps_protecting_names_in_the_zone_are_related(self):
        self.assertEqual(self.relationships().labels("Behind Access"), ("Admin",))
        self.assertEqual(self.services_card().note, "1 behind Access")

    def test_access_ranks_last_and_stays_out_of_the_cards(self):
        store(
            "cloudflare.access_app",
            ACCESS,
            {**ACCESS, "id": "a2", "name": "Status", "domain": "status.example.com"},
        )
        store(
            "cloudflare.dns_record",
            proxied("admin.example.com", "CNAME", "example-access.example.net"),
            proxied("status.example.com", "CNAME", "example-access.example.net"),
        )

        found = self.relationships()

        self.assertEqual(found.phrases()[-1], "Behind Access")
        self.assertEqual(found.labels("Behind Access"), ("Admin", "Status"))
        self.assertEqual(self.services_card().note, "2 behind Access")

        user = get_user_model().objects.create_user("operator", password="x" * 20)
        self.client.force_login(user)
        response = self.client.get(reverse("zones:detail", kwargs={"zone": "example.com"}))
        page = response.content.decode()

        # In Relationships, after the records; not among the cards.
        self.assertLess(page.index("<h2>Records</h2>"), page.index("Behind Access</th>"))
        self.assertNotIn('aria-label="Access application"', page)

    def test_issuers_are_named_by_the_authority_registry(self):
        store(
            "cloudflare.edge_certificate",
            {**EDGE[0], "certificate_authority": "google"},
            {**EDGE[1], "certificate_authority": "lets_encrypt"},
        )

        card = certificates(self.zone)

        self.assertIn("Issued by Google Trust Services, Let's Encrypt.", card.detail)

    def test_a_kind_not_connected_is_not_related(self):
        store("cloudflare.access_app", connected=False)

        found = self.relationships()

        self.assertNotIn("Behind Access", found.phrases())
        self.assertNotIn("Access application", found.unreadable)

    def test_a_refused_kind_is_named_not_readable(self):
        store("cloudflare.access_app", reachable=False, error="Forbidden.")

        self.assertIn("Access application", self.relationships().unreadable)

    def test_the_page_renders_the_cards(self):
        user = get_user_model().objects.create_user("operator", password="x" * 20)
        self.client.force_login(user)

        response = self.client.get(reverse("zones:detail", kwargs={"zone": "example.com"}))

        self.assertContains(response, "2 edge")
        self.assertContains(response, "example-site")
        self.assertContains(response, 'class="control-summary control-summary-four"')


class RegistrationFallbackTests(TestCase):
    def setUp(self):
        store(
            ZONE_KIND,
            {"zone": "example.com", "connection_ref": CLOUDFLARE,
             "registration": {"unread": "Authentication error"}},
        )
        self.zone = find_zone("example.com")

    def test_the_public_registry_gives_the_expiry_and_auto_renew_is_unknown(self):
        store(DOMAIN_KIND, {"domain": "example.com", "registrar": "Example Registrar",
                            "expires_at": LATER, "read_at": timezone.now().isoformat()})

        card = registration(self.zone)

        self.assertIn("80 days", card.value)
        self.assertIn("Auto-renew is unknown without registrar access.", card.detail)
        self.assertIn("Example Registrar", card.detail)
        self.assertFalse(card.concern)
        self.assertEqual(card.note, "Add Registrar: Domains Read (account) to see auto-renew.")
        self.assertIn("Authentication error", card.note_title)

    def test_a_registrar_name_ending_in_a_period_ends_the_sentence(self):
        store(DOMAIN_KIND, {"domain": "example.com", "registrar": "Example Registrar, Inc.",
                            "expires_at": LATER, "read_at": timezone.now().isoformat()})

        card = registration(self.zone)

        self.assertIn("via Example Registrar, Inc. Auto-renew", card.detail)
        self.assertNotIn("..", card.detail)

    def test_a_refused_permission_names_it_and_keeps_the_reason_in_the_title(self):
        store(
            ZONE_KIND,
            {"zone": "example.com", "connection_ref": CLOUDFLARE,
             "registration": {"unread": "Cloudflare refused the request: Forbidden",
                              "refusal": "permission"}},
        )
        store(DOMAIN_KIND, {"domain": "example.com", "registrar": "Example Registrar",
                            "expires_at": LATER, "read_at": timezone.now().isoformat()})
        user = get_user_model().objects.create_user("operator", password="x" * 20)
        self.client.force_login(user)

        response = self.client.get(reverse("zones:detail", kwargs={"zone": "example.com"}))

        self.assertContains(
            response,
            '<span class="muted" title="Cloudflare refused the request: Forbidden">'
            "Add Registrar: Domains Read (account) to see auto-renew.</span>",
            html=True,
        )

    def test_a_refused_credential_still_says_so(self):
        store(
            ZONE_KIND,
            {"zone": "example.com", "connection_ref": CLOUDFLARE,
             "registration": {"unread": "Cloudflare refused the request: Invalid API Token",
                              "refusal": "credential"}},
        )
        store(DOMAIN_KIND, {"domain": "example.com", "registrar": "Example Registrar",
                            "expires_at": LATER, "read_at": timezone.now().isoformat()})

        card = registration(self.zone)

        self.assertEqual(
            card.note, "Registrar not read: Cloudflare refused the request: Invalid API Token"
        )
        self.assertEqual(card.note_title, "")

    def test_without_a_public_reading_it_is_not_an_alarm(self):
        card = registration(self.zone)

        self.assertEqual(card.value, "Not read")
        self.assertFalse(card.concern)


class ServiceColumnTests(TestCase):
    def setUp(self):
        estate()
        declare_record("parked", "parked.example.org", "A", "192.0.2.1", zone="example.org")
        declare_record("hosted", "hosted.example.com", "A", "203.0.113.7")
        store(ADDRESS_KIND, {"address": "203.0.113.7", "organisation": "Example Hosting",
                             "read_at": timezone.now().isoformat()})
        self.user = get_user_model().objects.create_user("operator", password="x" * 20)
        self.client.force_login(self.user)

    def services(self):
        return {service.hostname: service for service in service_catalog()}

    def test_an_edge_certificate_fills_the_certificate_column(self):
        facet = next(f for f in self.services()["app.example.com"].facets if f.id == "certificate")

        self.assertEqual([item.label for item in facet.readings], ["Edge certificate"])

    def test_runs_on_names_the_pages_project(self):
        self.assertEqual(
            self.services()["example.com"].origin.headline, "Cloudflare Pages · example-site"
        )

    def test_a_documentation_address_is_parked(self):
        origin = self.services()["parked.example.org"].origin

        self.assertEqual(origin.headline, "Parked")
        self.assertEqual(origin.qualifier, "")

    def test_a_public_address_is_named_by_its_holder(self):
        # 203.0.113.0/24 stands in for a public range here.
        with mock.patch("application.reach.DOCUMENTATION", (ip_network("192.0.2.0/24"),)):
            origin = self.services()["hosted.example.com"].origin

            self.assertEqual(origin.headline, "Example Hosting")

    def test_the_certificate_cell_is_one_line_per_kind(self):
        declare_record("multi", "multi.example.com", "A", "192.0.2.5")
        store("cloudflare.dns_record", proxied("multi.example.com", "A", "192.0.2.5"))
        store(
            "cloudflare.edge_certificate",
            {**EDGE[0], "hosts": ["multi.example.com"], "certificate_authority": "google"},
            {**EDGE[1], "hosts": ["multi.example.com"], "certificate_authority": "lets_encrypt"},
            {**EDGE[1], "id": "e4", "hosts": ["multi.example.com"],
             "certificate_authority": "unknown_ca"},
        )
        facet = next(
            f for f in self.services()["multi.example.com"].facets if f.id == "certificate"
        )

        (line,) = facet.reading_lines
        self.assertEqual(line.label, "Edge")
        self.assertEqual(line.detail, "Google Trust Services, Let's Encrypt, unknown_ca")
        self.assertIn("40 days", line.expiry)

        response = self.client.get(reverse("control_plane:services"))

        self.assertContains(
            response,
            'Edge <span class="muted">· Google Trust Services, Let&#x27;s Encrypt, unknown_ca</span>',
        )
        self.assertContains(response, "earliest expires")
        self.assertNotContains(response, "Edge certificate lets_encrypt")

    def test_the_service_page_names_the_issuer(self):
        store(
            "cloudflare.edge_certificate",
            {**EDGE[0], "certificate_authority": "lets_encrypt"},
        )

        response = self.client.get(
            reverse("control_plane:service", kwargs={"hostname": "admin.example.com"})
        )

        self.assertContains(response, "Let&#x27;s Encrypt")
        # Only the raw readout, which is the record as read, holds the id.
        shown = re.sub(r"<pre>.*?</pre>", "", response.content.decode(), flags=re.S)
        self.assertNotIn("lets_encrypt", shown)

    def test_the_list_shows_them(self):
        response = self.client.get(reverse("control_plane:services"))

        self.assertContains(response, "Edge certificate")
        self.assertContains(response, "Cloudflare Pages · example-site")
        self.assertContains(response, "Parked")

    def test_the_service_page_shows_what_its_readings_say(self):
        response = self.client.get(
            reverse("control_plane:service", kwargs={"hostname": "admin.example.com"})
        )

        # In Relationships, each record through the link builder, and no
        # sentence under the cards repeating them.
        self.assertContains(response, '<th scope="rowgroup" rowspan="1">Behind Access</th>')
        self.assertContains(response, '<span data-entity="Access application">Admin</span>')
        self.assertContains(response, "Covered by edge certificate</th>")
        self.assertNotContains(response, "Behind Access: ")

    def test_provider_readings_use_labels_and_hide_what_is_not_connected(self):
        store("cloudflare.d1_database", connected=False)

        state = {item["kind"]: item["label"] for item in inventory_state()}

        self.assertEqual(state["cloudflare.access_app"], "Access application")
        self.assertNotIn("cloudflare.d1_database", state)


class TopologyEstateTests(TestCase):
    def setUp(self):
        estate()
        ManagedResource.objects.create(
            key="example-host", kind="machine",
            spec={"name": "example-host", "addresses": ["100.64.0.53"]},
        )
        store("tailscale.dns", {"record": "dns", "nameservers": ["100.64.0.53"]})
        for ref, provider in ((CLOUDFLARE, "cloudflare_api"), ("example-tailnet", "tailscale")):
            ProviderConnection.objects.create(
                controller_id="example-controller", connection_ref=ref, provider=provider,
                endpoint="https://api.example.test", reaches=["example.com"],
                reachable=True, probed=True, observed_at=timezone.now(),
            )
        self.finance = ConnectionSpecFixture.spec()

    def project(self):
        with mock.patch(
            "application.plugins.plugin_connection_specs", return_value=(self.finance,)
        ):
            topology = derive_topology(principal=READ)
        return (
            {node.id: node for node in topology.nodes},
            {(edge.source, edge.target, edge.label): edge for edge in topology.edges},
        )

    def test_machines_services_and_zones_are_nodes_of_their_own(self):
        nodes, _ = self.project()

        self.assertEqual(nodes["machine:example-host"].kind, "machine")
        self.assertEqual(nodes["service:app.example.com"].kind, "service")
        self.assertEqual(nodes["zone:example.com"].kind, "zone")

    def test_a_domain_specific_target_stays_a_target(self):
        nodes, _ = self.project()

        account = next(node for node in nodes.values() if node.label == "Example checking")
        self.assertEqual(account.kind, "target")
        self.assertNotIn("example.com", {n.label for n in nodes.values() if n.kind == "target"})

    def test_readings_are_edges_from_the_connection_that_read_them(self):
        _, edges = self.project()
        cloudflare = f"connection:infrastructure.controllers:example-controller:{CLOUDFLARE}"
        tailnet = "connection:infrastructure.controllers:example-controller:example-tailnet"

        for source, target, label in (
            (cloudflare, "service:admin.example.com", "Behind Access"),
            (cloudflare, "service:app.example.com", "Published through tunnel"),
            (cloudflare, "service:app.example.com", "Covered by edge certificate"),
            (tailnet, "machine:example-host", "Tailnet DNS server"),
        ):
            edge = edges[(source, target, label)]
            self.assertEqual(edge.kind, "reading")
            self.assertTrue(edge.observed_at)
            self.assertTrue(edge.source_kind)

    def test_an_edge_exists_only_while_its_reading_does(self):
        store("cloudflare.access_app")
        store("cloudflare.tunnel", TUNNEL, connected=False)

        _, edges = self.project()

        labels = {label for _, _, label in edges}
        self.assertNotIn("Behind Access", labels)
        self.assertNotIn("Published through tunnel", labels)

    def test_a_stale_reading_is_an_edge_that_says_so(self):
        store("cloudflare.access_app", ACCESS, age=timedelta(days=30))

        _, edges = self.project()

        edge = next(e for (_, _, label), e in edges.items() if label == "Behind Access")
        self.assertEqual(edge.status, "attention")

    def test_the_page_draws_estate_lanes(self):
        user = get_user_model().objects.create_user("operator", password="x" * 20)
        self.client.force_login(user)
        with mock.patch(
            "application.plugins.plugin_connection_specs", return_value=(self.finance,)
        ):
            response = self.client.get(reverse("control_plane:topology"))

        kinds = [group["kind"] for group in response.context["topology_groups"]]
        self.assertLess(kinds.index("machine"), kinds.index("target"))
        self.assertIn("service", kinds)
        self.assertIn("zone", kinds)


class ZoneFactsTests(TestCase):
    def test_a_registration_does_not_drop_another_resources_facts(self):
        ManagedResource.objects.create(
            key="example-com", kind=ZONE_KIND,
            spec={"zone": "example.com", "connection_ref": CLOUDFLARE},
        )
        store(ZONE_KIND, {"zone": "example.com", "connection_ref": CLOUDFLARE,
                          "registration": {"expires_at": LATER, "auto_renew": True}})
        ManagedResource.objects.create(
            key="example-certificate", kind="tls.certificate",
            spec={"domains": ["example.com"]},
            status={"unreachable_consumers": [{"domain": "app.example.com"}]},
        )
        with mock.patch("application.plugins.plugin_connection_specs", return_value=()):
            nodes = {node.id: node for node in derive_topology(principal=READ).nodes}

        self.assertIn(("unreachable", "app.example.com"),
                      nodes["resource:example-certificate"].facts)
        self.assertIn(("domain", "example.com"), nodes["resource:example-com"].facts)


class ConnectionSpecFixture:
    @staticmethod
    def spec():
        from .connections import ConnectionInstance, ConnectionLink, ConnectionSpec

        return ConnectionSpec(
            "example.finance", "Accounts", "A synthetic account connection.", Capability.READ,
            lambda: (
                ConnectionInstance(
                    "bank", "Example bank", "example_bank", "good", "Healthy",
                    targets=(ConnectionLink("Example checking"),),
                ),
            ),
        )


class QueryCostTests(TestCase):
    """The pages cost the same however many readings and names there are."""

    def setUp(self):
        estate()
        user = get_user_model().objects.create_user("operator", password="x" * 20)
        self.client.force_login(user)
        ProviderConnection.objects.create(
            controller_id="example-controller", connection_ref=CLOUDFLARE,
            provider="cloudflare_api", endpoint="https://api.example.test",
            reaches=["example.com"], reachable=True, probed=True, observed_at=timezone.now(),
        )

    def grow(self, count):
        for index in range(count):
            declare_record(f"n{index}", f"n{index}.example.com", "CNAME", "example.com")
        store(
            "cloudflare.access_app",
            *[{**ACCESS, "id": f"a{index}", "domain": f"n{index}.example.com"}
              for index in range(count)],
        )
        store(
            "cloudflare.edge_certificate",
            *EDGE,
            *[{**EDGE[1], "id": f"e{index}", "hosts": [f"n{index}.example.com"]}
              for index in range(count)],
        )

    def count(self, fetch):
        with CaptureQueriesContext(database_connection) as queries:
            fetch()
        return len(queries)

    def assert_flat(self, fetch):
        small = self.count(fetch)
        self.grow(20)
        large = self.count(fetch)
        self.assertLessEqual(large, small, f"{small} then {large}")

    def test_the_domain_page(self):
        self.assert_flat(
            lambda: self.client.get(reverse("zones:detail", kwargs={"zone": "example.com"}))
        )

    def test_the_services_list(self):
        self.assert_flat(lambda: self.client.get(reverse("control_plane:services")))

    def test_the_topology(self):
        with mock.patch("application.plugins.plugin_connection_specs", return_value=()):
            self.assert_flat(lambda: derive_topology(principal=READ))


@override_settings(SEVERINO_RDAP_ENDPOINT="https://rdap.example.test")
class PublicRegistryRefreshTests(TestCase):
    def refresh(self, **kwargs):
        from .public_registry import refresh

        return refresh(principal=cli_principal(), **kwargs)

    def allocation(self, address):
        return {
            "name": "EXAMPLE-NET", "handle": "NET-1", "country": "ZZ",
            "entities": [{"roles": ["registrant"],
                          "vcardArray": ["vcard", [["fn", {}, "text", "Example Hosting"]]]}],
        }

    def registration(self, domain):
        return {
            "events": [{"eventAction": "expiration", "eventDate": LATER}],
            "entities": [{"roles": ["registrar"],
                          "vcardArray": ["vcard", [["fn", {}, "text", "Example Registrar"]]]}],
        }

    def test_a_refresh_stores_readings_the_engine_joins(self):
        self.refresh(addresses=("203.0.113.7",), domains=("example.com",),
                     allocations=self.allocation, registrations=self.registration)

        found = readings().about(Subject.of(addresses=("203.0.113.7",), hostnames=("example.com",)))
        self.assertEqual(
            {(item.relation, item.title) for item in found},
            {("Address held by", "Example Hosting"), ("Registered through", "Example Registrar")},
        )

    def test_a_second_request_within_the_hour_reads_nothing(self):
        self.refresh(addresses=("203.0.113.7",), domains=(),
                     allocations=self.allocation, registrations=self.registration)
        refused = mock.Mock(side_effect=AssertionError("looked up again"))

        self.refresh(addresses=("203.0.113.7",), domains=(),
                     allocations=refused, registrations=refused)

    def test_a_fresh_record_is_not_read_again(self):
        self.refresh(addresses=("203.0.113.7",), domains=(),
                     allocations=self.allocation, registrations=self.registration)
        refused = mock.Mock(side_effect=AssertionError("looked up again"))

        self.refresh(addresses=("203.0.113.7",), domains=(),
                     allocations=refused, registrations=refused, force=True)

    def test_no_record_is_said_on_the_record(self):
        def missing(address):
            raise LookupNotFound("The registry has no record of that.")

        self.refresh(addresses=("203.0.113.7",), domains=(), allocations=missing)

        (record,) = ProviderInventory.objects.get(kind=ADDRESS_KIND).records
        self.assertEqual(record["unread"], "The registry has no record of that.")

    def test_an_unreachable_registry_is_stored_as_a_refused_read(self):
        def down(address):
            raise LookupUnavailable("The registry could not be reached.")

        self.refresh(addresses=("203.0.113.7",), domains=(), allocations=down)

        stored = ProviderInventory.objects.get(kind=ADDRESS_KIND)
        self.assertFalse(stored.reachable)
        self.assertIn("could not be reached", stored.error)

    @override_settings(SEVERINO_RDAP_ENDPOINT="")
    def test_an_unconfigured_registry_is_not_connected(self):
        self.refresh(addresses=("203.0.113.7",), domains=())

        self.assertFalse(ProviderInventory.objects.get(kind=ADDRESS_KIND).connected)

    def test_the_command_needs_no_network_when_nothing_is_due(self):
        from io import StringIO

        from django.core.management import call_command

        with (
            mock.patch("application.public_registry.registry", side_effect=AssertionError),
            mock.patch("application.public_registry.domain_registry", side_effect=AssertionError),
        ):
            call_command("refresh_public_registry", stdout=StringIO())

    def test_no_page_reads_a_registry(self):
        from django.urls import NoReverseMatch

        with self.assertRaises(NoReverseMatch):
            reverse("control_plane:public_registry_refresh")


class NamingTests(TestCase):
    def test_authority_codes_map_to_names_and_unknown_codes_pass_through(self):
        from control_plane.certificate_authorities import authority_name

        self.assertEqual(authority_name("google"), "Google Trust Services")
        self.assertEqual(authority_name("lets_encrypt"), "Let's Encrypt")
        self.assertEqual(authority_name("sectigo"), "Sectigo")
        self.assertEqual(authority_name("digicert"), "DigiCert")
        self.assertEqual(authority_name("ssl_com"), "SSL.com")
        self.assertEqual(authority_name("example_ca"), "example_ca")
        self.assertEqual(authority_name(""), "")

    def test_a_sentence_ending_on_a_period_gets_no_second_one(self):
        from .ui import ended

        self.assertEqual(ended("Via Example, Inc."), "Via Example, Inc.")
        self.assertEqual(ended("Via Example"), "Via Example.")
        self.assertEqual(ended(""), "")

    def test_cloudflare_refusals_are_classified_once(self):
        from control_plane.provider_adapters.contracts import cloudflare_refusal

        self.assertEqual(cloudflare_refusal("Authentication error"), "permission")
        self.assertEqual(cloudflare_refusal("Invalid API Token"), "credential")
        self.assertEqual(cloudflare_refusal("", status=401), "credential")
        self.assertEqual(cloudflare_refusal("", status=403), "permission")
        self.assertEqual(cloudflare_refusal("Rate limited"), "")

    def test_the_facts_panel_names_the_issuer(self):
        from .facts import facts_about

        store("cloudflare.edge_certificate", {**EDGE[1], "certificate_authority": "google"})
        store("cloudflare.dns_record", proxied("www.example.com", "A", "192.0.2.5"))

        facts = facts_about(("www.example.com",), ())

        self.assertIn(
            ("Issued by", "Google Trust Services"),
            {(fact.label, fact.value) for fact in facts},
        )
