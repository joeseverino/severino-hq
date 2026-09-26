"""Machine, service and domain pages: endpoints, self-links, visibility, counts."""

from __future__ import annotations

from ipaddress import ip_network
from unittest import mock

from django.contrib.auth import get_user_model
from django.template import Context, Template
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from control_plane.models import ManagedResource, ProviderConnection, ProviderInventory

from .entity_links import entity_link
from .projection import projection_scope
from .public_registry import public_endpoints
from .relationships import relationships_for
from .security import Capability, Principal
from .test_hq_self import SITE, declare, own
from .topology import RELATIONS
from .zone_insights import services as services_card
from .zones import find_zone

READER = Principal("test", "reader", frozenset({Capability.READ}))


def store(kind, *records, connected=True):
    ProviderInventory.objects.update_or_create(
        kind=kind,
        defaults={
            "records": list(records),
            "reachable": True,
            "connected": connected,
            "controller_id": "example-controller",
            "observed_at": timezone.now(),
        },
    )


def login(client):
    client.force_login(get_user_model().objects.create_superuser("operator", password="x" * 20))


class PublicEndpointTests(TestCase):
    def test_ipv6_privacy_addresses_fold_into_one_prefix(self):
        found = public_endpoints(
            ("203.0.113.7", "2001:db8:0:1::a1", "2001:db8:0:1::b2", "2001:db8:0:2::1")
        )

        self.assertEqual(
            [address for address, _holder in found],
            ["203.0.113.7", "2001:db8:0:1::/64", "2001:db8:0:2::/64"],
        )

    def test_the_prefix_takes_a_holder_read_for_any_address_in_it(self):
        with mock.patch(
            "application.public_registry.holders",
            return_value={"2001:db8:0:1::a1": "", "2001:db8:0:1::b2": "Example ISP"},
        ):
            found = public_endpoints(("2001:db8:0:1::a1", "2001:db8:0:1::b2"))

        self.assertEqual(found, (("2001:db8:0:1::/64", "Example ISP"),))

    def test_the_machine_page_shows_the_prefix_once(self):
        store("tailscale.device", {
            "name": "example-laptop", "online": True, "addresses": ["100.64.0.7"],
            "endpoints": ["203.0.113.7:41641", "[2001:db8:0:1::a1]:41641",
                          "[2001:db8:0:1::b2]:41641"],
        })
        login(self.client)

        with mock.patch("application.reach.DOCUMENTATION", (ip_network("192.0.2.0/24"),)):
            response = self.client.get(reverse("control_plane:machine", args=["example-laptop"]))

        self.assertContains(response, "<code>2001:db8:0:1::/64</code>", count=1)
        self.assertContains(response, "<code>203.0.113.7</code>")
        self.assertNotContains(response, "2001:db8:0:1::a1")


class AgoFilterTests(TestCase):
    def render(self, value):
        return Template("{% load value_tags %}{{ value|ago }}").render(Context({"value": value}))

    def test_a_moment_and_a_stamp_read_as_ui_ago(self):
        from datetime import timedelta

        from .ui import MISSING, ago

        moment = timezone.now() - timedelta(hours=3)

        self.assertEqual(self.render(moment), ago(moment))
        self.assertEqual(self.render(moment.isoformat()), ago(moment))
        self.assertEqual(self.render(""), MISSING)


class SelfLinkTests(TestCase):
    def render(self, path, link):
        request = RequestFactory().get(path)
        return Template("{% load value_tags %}{% entity link %}").render(
            Context({"request": request, "link": link})
        )

    def test_a_link_to_the_page_it_is_on_renders_plain(self):
        link = entity_link("machine", "example-host")

        html = self.render(link.url, link)

        self.assertNotIn("<a ", html)
        self.assertIn("example-host", html)

    def test_a_link_elsewhere_or_to_a_fragment_stays_a_link(self):
        link = entity_link("machine", "example-host")
        self.assertIn("<a ", self.render("/elsewhere/", link))
        connection = entity_link("connection", "example-ssh")
        self.assertIn("<a ", self.render(reverse("control_plane:connections"), connection))

    def test_the_machine_page_does_not_link_to_itself_or_repeat_its_declaration(self):
        store("tailscale.device", {"name": "example-host", "online": True,
                                   "addresses": ["100.64.0.5"]})
        ManagedResource.objects.create(
            key="example-host-device", kind="tailscale.device", spec={"name": "example-host"}
        )
        login(self.client)
        url = reverse("control_plane:machine", args=["example-host"])

        response = self.client.get(url)

        self.assertNotContains(response, f'<a href="{url}" data-entity')
        self.assertContains(response, "example-host-device")
        relationships = response.context["relationships"]
        self.assertNotIn(RELATIONS["on_tailnet"].phrase, relationships.phrases())


class FacetVisibilityTests(TestCase):
    def setUp(self):
        store("cloudflare.zone", {"zone": "example.com", "connection_ref": "example-dns"})
        ManagedResource.objects.create(
            key="app-record",
            kind="cloudflare.dns_record",
            spec={"zone": "example.com", "name": "app.example.com", "record_type": "A",
                  "content": "192.0.2.10", "connection_ref": "example-dns"},
        )
        login(self.client)

    def facet(self, facet_id):
        from .services import find_service

        with projection_scope():
            service = find_service("app.example.com")
            return next(item for item in service.facets if item.id == facet_id)

    def test_a_facet_no_connected_kind_supplies_is_not_visible(self):
        from control_plane.providers import CONNECTION_LABELS

        facet = self.facet("certificate")

        self.assertTrue(facet.not_visible.startswith("Connect "), facet.not_visible)
        self.assertIn(CONNECTION_LABELS["cloudflare_api"], facet.not_visible)
        response = self.client.get(reverse("control_plane:service", args=["app.example.com"]))
        self.assertContains(response, "Not visible")
        self.assertContains(response, facet.not_visible)

    def test_a_connection_that_does_not_read_it_is_named(self):
        ProviderConnection.objects.create(
            connection_ref="example-dns", controller_id="example-controller",
            provider="cloudflare_api", reachable=True, probed=True,
            observed_at=timezone.now(),
        )

        note = self.facet("certificate").not_visible

        self.assertTrue(note.startswith("Not read through "), note)

    def test_a_facet_a_connected_kind_could_show_is_not_declared(self):
        from control_plane.providers import PROVIDERS

        kind = next(
            kind for kind, provider in PROVIDERS.items()
            if provider.facet == "proxy" and not provider.unobserved_reason
        )
        store(kind)

        self.assertEqual(self.facet("proxy").not_visible, "")


@SITE
class DomainServiceCountTests(TestCase):
    def setUp(self):
        declare("example-host", "192.0.2.44")
        store("cloudflare.zone", {"zone": "example.com", "connection_ref": "example-dns"})
        for index in range(2):
            ManagedResource.objects.create(
                key=f"record-{index}",
                kind="cloudflare.dns_record",
                spec={"zone": "example.com", "name": f"app{index}.example.com",
                      "record_type": "A", "content": "192.0.2.44",
                      "connection_ref": "example-dns"},
            )

    def test_the_card_counts_what_the_domain_contains(self):
        with own("192.0.2.44"), projection_scope():
            card = services_card(find_zone("example.com"))
            contains = relationships_for("zone:example.com", principal=READER).labels(
                RELATIONS["contains"].phrase
            )

        self.assertIn("hq.example.com", contains)
        self.assertEqual(card.value, f"{len(contains)} services")
        self.assertEqual(sorted(row.title for row in card.rows), sorted(contains))
