"""The machine page with a realistic tailnet: reached-through, the facts panel, relations."""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from control_plane.models import ProviderConnection, ProviderInventory
from control_plane.observations import OBSERVATIONS
from control_plane.providers import PROVIDERS

from .connections import connection_readings
from .facts import facts_about
from .machines import machine, machine_catalog
from .relationships import relationships_for
from .security import Capability, Principal
from .tailnet import TAILNET_KIND

CONTROLLER = "example-controller"
TAILNET_REF = "example-tailnet"
DNS = "tailscale.dns"
PAGES = "cloudflare.pages_project"


def npm_kind():
    """A joinable kind only an NPM connection reads, from the registry."""

    return next(
        kind
        for kind, spec in sorted(PROVIDERS.items())
        if "npm" in spec.connection_providers and spec.from_record and spec.hostnames
    )


def store(kind, *records, reachable=True, connected=True, error=""):
    ProviderInventory.objects.update_or_create(
        kind=kind,
        defaults={
            "records": list(records),
            "reachable": reachable,
            "connected": connected,
            "error": error,
            "controller_id": CONTROLLER,
            "observed_at": timezone.now(),
        },
    )


def device(name, address, os="linux"):
    return {
        "name": name,
        "addresses": [address],
        "dns_name": f"{name}.example.ts.net",
        "os": os,
        "online": True,
    }


def tailnet_connection(ref=TAILNET_REF):
    return ProviderConnection.objects.create(
        connection_ref=ref,
        controller_id=CONTROLLER,
        provider=PROVIDERS[TAILNET_KIND].connection_providers[0],
        reaches=[],
        reachable=True,
        probed=True,
        observed_at=timezone.now(),
    )


def estate():
    """Two devices, a DNS reading naming one, a refused and a not-connected kind."""

    tailnet_connection()
    store(
        TAILNET_KIND,
        device("example-host", "100.64.0.1"),
        device("example-laptop", "100.64.0.2", os="macOS"),
    )
    store(
        DNS,
        {"record": "dns", "nameservers": ["100.64.0.1", "192.0.2.53"], "magic_dns": True},
    )
    store(PAGES, reachable=False, error="Cloudflare refused the read (403).")
    store(npm_kind(), reachable=False, connected=False, error="No connection.")


READER = Principal("reader", "test", frozenset({Capability.READ}))


class ReachedThroughTests(TestCase):
    def setUp(self):
        estate()

    def test_a_tailnet_device_is_reached_through_the_connection_that_read_it(self):
        found = {item.name: item for item in machine_catalog()}

        self.assertEqual(found["example-host"].reached_by, (TAILNET_REF,))
        self.assertEqual(found["example-laptop"].reached_by, (TAILNET_REF,))
        self.assertTrue(found["example-host"].reachable)
        # Nothing opens a shell or a Docker environment there.
        self.assertEqual(found["example-host"].opened_by, ())

    def test_a_record_naming_its_connection_wins(self):
        tailnet_connection("other-tailnet")
        record = device("example-host", "100.64.0.1")
        record["connection_ref"] = "other-tailnet"
        store(TAILNET_KIND, record)

        self.assertEqual(machine("example-host").reached_by, ("other-tailnet",))

    def test_the_list_page_and_header_name_the_connection(self):
        user = get_user_model().objects.create_user("machine-op", password="x" * 20)
        self.client.force_login(user)

        listing = self.client.get(reverse("control_plane:machines"))
        page = self.client.get(reverse("control_plane:machine", kwargs={"name": "example-host"}))

        self.assertContains(listing, f"<code>{TAILNET_REF}</code>")
        self.assertContains(page, f"<code>{TAILNET_REF}</code>")
        self.assertNotContains(page, "No credential reaches this machine")
        self.assertNotContains(page, "no credential")

    def test_the_connections_page_names_the_devices(self):
        reading = next(r for r in connection_readings() if r.connection_ref == TAILNET_REF)

        self.assertEqual(
            sorted(name for name, _url in reading.machines),
            ["example-host", "example-laptop"],
        )


class PanelTests(TestCase):
    """The machine's Relationships section, read off the relation graph."""

    def setUp(self):
        estate()

    def relationships(self, name):
        return relationships_for(f"machine:{name}", principal=READER)

    def test_a_reading_says_what_it_is_to_the_machine(self):
        found = self.relationships("example-host")

        relation = OBSERVATIONS[DNS].relation
        self.assertEqual(found.labels(relation), ("100.64.0.1, 192.0.2.53 · MagicDNS on",))
        (item,) = found.group(relation).items
        self.assertEqual(item.source.label, TAILNET_REF)
        self.assertIsNotNone(item.observed_at)
        self.assertEqual(len(found.readouts), 1)

    def test_another_machine_on_the_resolver_list_is_not_this_one(self):
        found = self.relationships("example-laptop")

        self.assertNotIn(OBSERVATIONS[DNS].relation, found.phrases())

    def test_it_is_reached_through_the_tailnet_connection(self):
        found = self.relationships("example-host")

        self.assertEqual(found.labels("Reached through"), (TAILNET_REF,))
        (item,) = found.group("Reached through").items
        self.assertIn("#connection-", item.entity.url)

    def test_the_refused_kind_is_not_readable_and_the_unconnected_one_is_absent(self):
        found = self.relationships("example-host")

        self.assertIn(OBSERVATIONS[PAGES].label, found.unreadable)
        self.assertNotIn(PROVIDERS[npm_kind()].label, found.unreadable)

    def test_the_page_renders_the_section_with_one_not_readable_line(self):
        user = get_user_model().objects.create_user("panel-op", password="x" * 20)
        self.client.force_login(user)

        page = self.client.get(reverse("control_plane:machine", kwargs={"name": "example-host"}))

        self.assertContains(page, "<h2>Relationships</h2>")
        self.assertNotContains(page, "What HQ knows")
        self.assertContains(page, "Not readable:", count=1)
        self.assertContains(page, "?focus=machine%3Aexample-host")
        self.assertContains(page, "Raw readings")

    def test_an_unconnected_kind_yields_no_facts(self):
        facts = facts_about(("example-host",), ("100.64.0.1",))

        self.assertNotIn(npm_kind(), {fact.source_kind for fact in facts})

    def test_an_unconnected_tailnet_yields_no_facts(self):
        store(TAILNET_KIND, reachable=False, connected=False, error="No connection.")

        facts = facts_about(("example-host",), ("100.64.0.1",))

        self.assertNotIn(TAILNET_KIND, {fact.source_kind for fact in facts})


class RelationTests(TestCase):
    def test_every_reading_says_what_it_is_to_a_subject(self):
        for kind, spec in OBSERVATIONS.items():
            with self.subTest(kind=kind):
                self.assertTrue(spec.relation.strip())
                self.assertNotIn("\u2014", spec.relation + spec.address_relation)

    def test_the_join_picks_the_phrase(self):
        tunnel = OBSERVATIONS["cloudflare.tunnel"]
        store(
            "cloudflare.tunnel",
            {
                "id": "t1",
                "name": "example-tunnel",
                "ingress": [{"hostname": "app.example.com"}],
                "connections": [{"origin_ip": "192.0.2.20"}],
            },
        )

        by_name = {f.label: f.value for f in facts_about(("app.example.com",), ())}
        by_address = {f.label: f.value for f in facts_about((), ("192.0.2.20",))}

        self.assertEqual(by_name[tunnel.relation], "example-tunnel")
        self.assertEqual(by_address[tunnel.address_relation], "example-tunnel")


class CatalogQueryCountTests(TestCase):
    def _count(self, devices):
        ProviderConnection.objects.all().delete()
        tailnet_connection()
        store(
            TAILNET_KIND,
            *(device(f"example-host-{i}", f"100.64.0.{i + 1}") for i in range(devices)),
        )
        with CaptureQueriesContext(connection) as captured:
            catalog = machine_catalog()
        self.assertEqual(len(catalog), devices)
        self.assertTrue(all(item.reached_by == (TAILNET_REF,) for item in catalog))
        return len(captured)

    def test_reached_through_costs_no_more_queries_for_more_machines(self):
        self.assertEqual(self._count(2), self._count(20))
