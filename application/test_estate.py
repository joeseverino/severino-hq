"""The estate on the surfaces an operator opens first: search, dashboard, queue."""

from __future__ import annotations

from datetime import timedelta
from ipaddress import ip_network
from unittest import mock

from django.contrib.auth import get_user_model
from django.template.loader import render_to_string
from django.test import TestCase

from application.security import cli_principal
from django.urls import reverse
from django.utils import timezone

from control_plane.models import (
    DashboardConfiguration,
    ManagedResource,
    ProviderConnection,
    ProviderInventory,
    WeatherObservation,
)
from core.audit import record_event
from core.models import AuditLog

from .attention import infrastructure, tailnet
from .command_center import command_center
from .estate import attention as estate_attention, cards, estate_reading
from .glance import dashboard_panels
from .machines import machine, machine_catalog, tailnet_presence
from .projection import projection_scope
from .public_registry import wanted_addresses
from .security import Capability, Principal

READ = Principal("test", "read", frozenset({Capability.READ}))
# 203.0.113.0/24 stands in for a public range here.
PUBLIC_RANGE = mock.patch("application.reach.DOCUMENTATION", (ip_network("192.0.2.0/24"),))


def store(kind, *records, **extra):
    ProviderInventory.objects.update_or_create(
        kind=kind,
        defaults={
            "records": list(records),
            "reachable": True,
            "connected": True,
            "observed_at": timezone.now(),
            "controller_id": "example-controller",
            **extra,
        },
    )


def device(name, **fields):
    return {"name": name, "online": True, **fields}


def devices(*records, nameservers=()):
    store("tailscale.device", *records)
    store("tailscale.policy", {"dns": {"dns": list(nameservers)}})


def connection(ref, *, reachable=True, detail=""):
    return ProviderConnection.objects.create(
        connection_ref=ref,
        controller_id="example-controller",
        provider="ssh",
        endpoint="10.0.0.9:22",
        reachable=reachable,
        probed=True,
        detail=detail,
        observed_at=timezone.now(),
    )


def declared(key, kind, spec, *, status=None):
    return ManagedResource.objects.create(
        key=key,
        kind=kind,
        spec=spec,
        status=status or {},
        conditions=[{"type": "Ready", "status": True}],
        observed_generation=1,
        last_observed_at=timezone.now(),
    )


def declare_record(key, name, content):
    return declared(
        key,
        "cloudflare.dns_record",
        {
            "zone": "example.com",
            "name": name,
            "record_type": "A",
            "content": content,
            "connection_ref": "example-dns",
        },
    )


class SearchFindsTheEstateTests(TestCase):
    def setUp(self):
        devices(
            device(
                "example-host",
                addresses=["100.64.0.5"],
                dns_name="example-host.example-tailnet.ts.net",
            ),
            device("example-laptop", addresses=["100.64.0.6"]),
        )
        declare_record("app-record", "app.example.com", "100.64.0.5")
        store("cloudflare.zone", {"zone": "example.com", "connection_ref": "example-dns"})

    def estate(self, query):
        return command_center(query, principal=READ, include_live_connections=True)["estate"]

    def test_a_machine_name_finds_the_machine_first(self):
        found = self.estate("example-host")

        self.assertEqual((found[0].kind, found[0].label), ("machine", "example-host"))
        self.assertEqual(found[0].url, reverse("control_plane:machine", args=["example-host"]))

    def test_an_address_finds_the_machine_holding_it(self):
        found = self.estate("100.64.0.5")

        self.assertEqual(found[0].label, "example-host")

    def test_a_magicdns_name_finds_the_machine(self):
        self.assertEqual(
            self.estate("example-host.example-tailnet.ts.net")[0].label, "example-host"
        )

    def test_a_hostname_finds_its_service_then_its_domain(self):
        found = [(item.kind, item.label) for item in self.estate("app.example.com")]

        self.assertEqual(found[:2], [("service", "app.example.com"), ("zone", "example.com")])

    def test_the_palette_puts_the_estate_above_records_and_audit(self):
        user = get_user_model().objects.create_superuser("operator", password="x" * 20)
        self.client.force_login(user)
        ManagedResource.objects.create(key="example-host-spec", kind="machine",
                                       spec={"name": "example-host"})

        response = self.client.get(
            reverse("search"), {"q": "example-host"}, headers={"X-Command-Center": "palette"}
        )

        content = response.content.decode()
        self.assertIn("command-center-group-estate", content)
        self.assertLess(
            content.index("command-center-group-estate"),
            content.index("command-center-group-search-"),
        )

    def test_the_estate_is_not_offered_without_read(self):
        none = Principal("test", "none", frozenset())

        self.assertEqual(
            command_center("example-host", principal=none, include_live_connections=True)[
                "estate"
            ],
            (),
        )

    def test_an_empty_query_lists_no_estate(self):
        self.assertEqual(self.estate(""), ())


class AuditSearchTitleTests(TestCase):
    def test_an_audit_hit_is_titled_by_the_object_label_not_its_id(self):
        user = get_user_model().objects.create_superuser("operator", password="x" * 20)
        self.client.force_login(user)
        resource = ManagedResource.objects.create(key="example-host", kind="machine", spec={})
        record_event(action=AuditLog.Action.UPDATED, obj=resource, type_label="Managed resource")
        event = AuditLog.objects.filter(
            object_id=str(resource.pk), action=AuditLog.Action.UPDATED
        ).latest("created_at")

        self.assertEqual(event.search_title, "Updated example-host")
        self.assertNotIn(str(resource.pk), str(event))
        self.assertEqual(event.get_absolute_url(), reverse("core:audit_detail", args=[event.pk]))

        response = self.client.get(reverse("search"), {"q": "example-host"})

        self.assertContains(response, "Updated example-host")
        self.assertNotContains(response, f"#{resource.pk}")


class SearchSnippetTests(TestCase):
    """Results read as words: labels for kinds, readouts for specs, no identifiers."""

    def group(self, query, scope):
        from .search import global_search

        found = global_search(query, principal=Principal("test", "all", frozenset(Capability)))
        return next(group for group in found["groups"] if group["scope"] == scope)["items"]

    @staticmethod
    def text(item):
        return "".join(part for part, _hit in item["snippet"])

    def test_an_audit_snippet_names_no_object_id(self):
        resource = ManagedResource.objects.create(key="example-host", kind="machine", spec={})
        record_event(
            action=AuditLog.Action.CREATED,
            obj=resource,
            type_label="Managed resource",
            message=f"Declared {resource.pk}",
        )

        items = self.group("example-host", "audit")

        self.assertTrue(items)
        for item in items:
            self.assertNotIn(str(resource.pk), self.text(item))
            self.assertNotIn(str(resource.pk)[:8], self.text(item))
        self.assertIn("Managed resource", self.text(items[0]))

    def test_a_resource_result_shows_its_label_and_readout(self):
        store("cloudflare.zone", {"zone": "example.com", "connection_ref": "example-dns"})
        declare_record("app-record", "app.example.com", "192.0.2.10")

        (item,) = self.group("app-record", "infrastructure.resources")

        from control_plane.providers import PROVIDERS

        self.assertEqual(item["badge"], PROVIDERS["cloudflare.dns_record"].label)
        self.assertNotIn("cloudflare.dns_record", item["badge"])
        self.assertIn("192.0.2.10", self.text(item))
        for raw in ("{", "connection_ref", "record_type"):
            self.assertNotIn(raw, self.text(item))
        self.assertEqual(item["url"], reverse("control_plane:service", args=["app.example.com"]))

    def test_a_machine_declaration_opens_the_machine(self):
        ManagedResource.objects.create(
            key="example-host", kind="machine", spec={"name": "example-host"}
        )

        (item,) = self.group("example-host", "infrastructure.resources")

        self.assertEqual(item["url"], reverse("control_plane:machine", args=["example-host"]))

    def test_a_kind_without_a_readout_shows_its_label(self):
        declared("example-policy", "tailscale.policy", {"document": "{}"})

        (item,) = self.group("example-policy", "infrastructure.resources")

        self.assertNotIn("tailscale.policy", item["badge"])
        self.assertNotIn("{", self.text(item))
        self.assertEqual(item["url"], reverse("control_plane:detail", args=["example-policy"]))


class EstateCardTests(TestCase):
    def setUp(self):
        devices(device("example-host"), device("example-laptop", online=False))
        declare_record("app-record", "app.example.com", "100.64.0.5")
        connection("example-ssh", reachable=False, detail="Timed out")
        declared(
            "example-cert",
            "tls.certificate",
            {"certificate_name": "example-cert", "domains": ["app.example.com"]},
            status={"not_after": (timezone.now() + timedelta(days=12)).isoformat()},
        )
        store(
            "cloudflare.zone",
            {
                "zone": "example.com",
                "connection_ref": "example-dns",
                "registration": {
                    "expires_at": (timezone.now() + timedelta(days=200)).isoformat(),
                    "auto_renew": True,
                },
            },
        )

    def cards(self):
        with projection_scope():
            return {card["id"]: card for card in cards()}

    def test_every_figure_links_to_its_page(self):
        found = self.cards()

        self.assertEqual(found["hq.estate.machines"]["value"], "1")
        self.assertEqual(found["hq.estate.machines"]["detail"], "1 offline")
        self.assertEqual(found["hq.estate.machines"]["url"], reverse("control_plane:machines"))
        self.assertEqual(found["hq.estate.services"]["url"], reverse("control_plane:services"))
        self.assertEqual(found["hq.estate.domains"]["value"], "1")
        self.assertEqual(found["hq.estate.domains"]["url"], reverse("zones:index"))
        self.assertEqual(found["hq.estate.connections"]["value"], "1")
        self.assertEqual(
            found["hq.estate.connections"]["detail"], "example-ssh not answering"
        )
        self.assertEqual(
            found["hq.estate.connections"]["url"], reverse("control_plane:connections")
        )

    def test_the_nearest_expiries_link_to_their_subject(self):
        found = self.cards()

        self.assertEqual(found["hq.estate.certificate"]["detail"], "example-cert · Certificate")
        self.assertEqual(
            found["hq.estate.certificate"]["url"],
            reverse("control_plane:detail", args=["example-cert"]),
        )
        self.assertIn("days", found["hq.estate.certificate"]["value"])
        self.assertEqual(found["hq.estate.registration"]["detail"], "example.com · Registration")
        self.assertEqual(
            found["hq.estate.registration"]["url"], reverse("zones:detail", args=["example.com"])
        )

    def test_the_dashboard_shows_the_estate_card(self):
        user = get_user_model().objects.create_superuser("operator", password="x" * 20)
        self.client.force_login(user)

        response = self.client.get(reverse("dashboard"))

        self.assertContains(response, "<h2>Estate")
        self.assertContains(response, "Machines online")
        self.assertContains(response, f'href="{reverse("control_plane:connections")}"')

    def test_a_refused_credential_is_said_as_refused(self):
        from control_plane.provider_adapters.contracts import CREDENTIAL_REFUSAL

        ProviderConnection.objects.all().delete()
        ProviderConnection.objects.create(
            connection_ref="example-cf", controller_id="example-controller",
            provider="cloudflare_api", reachable=True, probed=True,
            observed_at=timezone.now(),
        )
        store(
            "cloudflare.pages_project", reachable=False, refusal=CREDENTIAL_REFUSAL,
            error="Invalid API token",
        )

        with projection_scope():
            found = estate_reading().connections

        self.assertEqual([(item.ref, item.state) for item in found], [("example-cf", "refused")])


class EdgeCertificateExpiryTests(TestCase):
    """Edge certificates renew themselves; only a missed renewal is the operator's."""

    def setUp(self):
        store("cloudflare.zone", {"zone": "example.com", "connection_ref": "example-dns"})
        declared(
            "example-cert",
            "tls.certificate",
            {"certificate_name": "example-cert", "domains": ["app.example.com"],
             "renewal_window_days": 30},
            status={"not_after": (timezone.now() + timedelta(days=50)).isoformat()},
        )

    def edge(self, days):
        store(
            "cloudflare.edge_certificate",
            {"connection_ref": "example-dns", "zone": "example.com", "id": "e1",
             "hosts": ["example.com"], "status": "active",
             "expires_on": (timezone.now() + timedelta(days=days, hours=1)).isoformat()},
        )

    def figure(self):
        with projection_scope():
            return {card["id"]: card for card in cards()}["hq.estate.certificate"]

    def items(self):
        with projection_scope():
            return [item for item in estate_attention() if item.eyebrow == "Certificates"]

    def test_an_edge_certificate_outside_the_window_is_not_the_next_expiry(self):
        self.edge(40)

        figure = self.figure()

        self.assertEqual(figure["detail"], "example-cert · Certificate")
        self.assertNotIn("status", figure)
        self.assertEqual(self.items(), [])

    def test_an_edge_certificate_inside_the_window_says_renewal_failed(self):
        self.edge(10)

        figure = self.figure()

        self.assertEqual(
            figure["detail"], "example.com · Edge · not renewed by its provider"
        )
        self.assertEqual(figure["status"], "attention")
        self.assertEqual(figure["url"], reverse("zones:detail", args=["example.com"]))
        (item,) = self.items()
        self.assertEqual(item.status, "attention")
        self.assertEqual(item.title, "example.com expires in 10 days")
        self.assertIn("should have by now", item.body)
        self.assertEqual(item.subject.url, reverse("zones:detail", args=["example.com"]))


class StaleGlanceTests(TestCase):
    def test_a_reading_older_than_its_cadence_is_shown_as_of_its_age_and_steps_aside(self):
        DashboardConfiguration.objects.create(pk=1, weather_point="41.8781,-87.6298")
        machine_spec = ManagedResource.objects.create(
            key="example-host", kind="machine", spec={"name": "example-host"}
        )
        from control_plane.models import DashboardMachine

        from . import readings

        DashboardMachine.objects.create(machine=machine_spec, position=0)
        readings.record(
            readings.machine_telemetry("example-host"),
            {"metrics": [{"label": "CPU", "value": "4%"}]},
            observed_at=timezone.now() - timedelta(days=26),
        )
        WeatherObservation.objects.create(
            point="41.8781,-87.6298",
            payload={"metrics": [{"label": "Now", "value": "Clear"}]},
            observed_at=timezone.now() - timedelta(minutes=1),
        )

        panels = dashboard_panels()

        self.assertEqual([panel["id"] for panel in panels][-1], f"machine-{machine_spec.pk}")
        old = panels[-1]
        self.assertTrue(old["outdated"])
        self.assertFalse(panels[0]["outdated"])
        html = render_to_string("core/_dashboard_glance.html", {"dashboard_panels": panels})
        self.assertIn("is-outdated", html)
        self.assertIn("as of <time", html)

    def test_a_current_reading_is_not_outdated(self):
        from .glance import expected_cadence

        DashboardConfiguration.objects.create(pk=1, weather_point="41.8781,-87.6298")
        WeatherObservation.objects.create(
            point="41.8781,-87.6298",
            payload={"metrics": [{"label": "Now", "value": "Clear"}]},
            observed_at=timezone.now() - expected_cadence() + timedelta(minutes=1),
        )

        weather = next(panel for panel in dashboard_panels() if panel["id"] == "weather")

        self.assertFalse(weather["outdated"])


class OldGlanceTests(TestCase):
    def test_an_old_reading_stays_on_the_dashboard_with_its_age(self):
        DashboardConfiguration.objects.create(pk=1, weather_point="41.8781,-87.6298")
        WeatherObservation.objects.update_or_create(
            point="41.8781,-87.6298",
            defaults={
                "payload": {"metrics": [{"label": "Now", "value": "Clear"}]},
                "observed_at": timezone.now() - timedelta(days=20),
            },
        )
        from .glance import glance_context

        html = render_to_string("core/_dashboard_glance.html", glance_context())

        self.assertNotIn("<details class=\"glance-quiet", html)
        self.assertIn("is-outdated", html)
        self.assertIn("as of <time", html)

class ActionItemTests(TestCase):
    def test_an_update_names_the_version_other_devices_run(self):
        devices(
            device("example-host", update_available=True, client_version="1.102.3-t0abc"),
            device("example-laptop", client_version="1.102.4-t0def"),
        )

        (item,) = [item for item in tailnet() if item.key == "tailnet-update:example-host"]

        self.assertEqual(item.body, "1.102.3 → 1.102.4.")
        self.assertEqual(item.subject.url, reverse("control_plane:machine", args=["example-host"]))

    def test_an_update_with_no_newer_device_says_what_runs(self):
        devices(device("example-host", update_available=True, client_version="1.102.3"))

        (item,) = [item for item in tailnet() if item.key == "tailnet-update:example-host"]

        self.assertEqual(item.body, "Runs 1.102.3.")

    def test_a_connection_that_does_not_answer_is_an_action_item(self):
        connection("example-ssh", reachable=False, detail="Timed out")

        found = [item for item in infrastructure() if "connection-not-answering" in item.key]

        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].title, "example-ssh is not answering")
        self.assertIn("Timed out", found[0].body)
        self.assertEqual(found[0].subject.label, "example-ssh")
        self.assertTrue(found[0].subject.url)

    def test_a_domain_that_will_not_renew_reaches_the_queue(self):
        declared("example-zone", "cloudflare.zone",
                 {"zone": "example.com", "connection_ref": "example-dns"})
        store(
            "cloudflare.zone",
            {
                "zone": "example.com",
                "connection_ref": "example-dns",
                "registration": {
                    "expires_at": (timezone.now() + timedelta(days=40)).date().isoformat(),
                    "auto_renew": False,
                    "registrar": "Example Registrar",
                },
            },
        )

        found = [item for item in infrastructure() if "registration-lapsing" in item.key]

        self.assertEqual(len(found), 1)
        self.assertIn("will not renew", found[0].title)
        self.assertEqual(found[0].subject.url, reverse("zones:detail", args=["example.com"]))

    def test_a_machine_offline_past_the_threshold_that_serves_something(self):
        devices(
            device(
                "example-host",
                online=False,
                addresses=["100.64.0.5"],
                last_seen=(timezone.now() - timedelta(hours=5)).isoformat(),
            ),
            device(
                "example-phone",
                online=False,
                last_seen=(timezone.now() - timedelta(days=5)).isoformat(),
            ),
            device(
                "example-restart",
                online=False,
                addresses=["100.64.0.7"],
                last_seen=(timezone.now() - timedelta(minutes=5)).isoformat(),
            ),
        )
        declare_record("app-record", "app.example.com", "100.64.0.5")
        declare_record("other-record", "other.example.com", "100.64.0.7")

        with projection_scope():
            found = [item for item in estate_attention() if item.eyebrow == "Machines"]

        self.assertEqual([item.title for item in found], ["example-host is offline"])
        self.assertEqual(found[0].status, "serious")
        self.assertEqual(
            found[0].subject.url, reverse("control_plane:machine", args=["example-host"])
        )

    def test_a_managed_certificate_inside_its_renewal_window(self):
        for key, days in (("soon-cert", 5), ("later-cert", 80)):
            declared(
                key,
                "tls.certificate",
                {"certificate_name": key, "domains": ["app.example.com"],
                 "renewal_window_days": 30},
                status={"not_after": (timezone.now() + timedelta(days=days, hours=1)).isoformat()},
            )

        with projection_scope():
            found = [item for item in estate_attention() if item.eyebrow == "Certificates"]

        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].status, "serious")
        self.assertIn("soon-cert expires in", found[0].title)
        self.assertEqual(found[0].subject.url, reverse("control_plane:detail", args=["soon-cert"]))

    def test_the_action_items_page_links_each_subject(self):
        user = get_user_model().objects.create_superuser("operator", password="x" * 20)
        self.client.force_login(user)
        devices(device("example-host", update_available=True, client_version="1.102.3"))

        response = self.client.get(reverse("action_items"))

        self.assertContains(
            response,
            f'<a href="{reverse("control_plane:machine", args=["example-host"])}">example-host</a>',
        )


class MachineRoleTests(TestCase):
    def setUp(self):
        devices(
            device(
                "example-exit",
                addresses=["100.64.0.8"],
                advertised_routes=["0.0.0.0/0", "::/0"],
                enabled_routes=["0.0.0.0/0", "::/0"],
            ),
            device("example-dns", addresses=["100.64.0.5"]),
            device(
                "example-unapproved",
                addresses=["100.64.0.9"],
                advertised_routes=["0.0.0.0/0", "::/0"],
            ),
            nameservers=["100.64.0.5"],
        )

    def roles(self, name):
        return [role.label for role in machine(name).roles]

    def test_an_approved_exit_node_and_the_tailnet_nameserver(self):
        self.assertEqual(self.roles("example-exit"), ["Exit node"])
        self.assertEqual(self.roles("example-dns"), ["Tailnet DNS"])
        self.assertEqual(self.roles("example-unapproved"), [])
        self.assertEqual(machine("example-exit").serves_count, 1)

    def test_the_serves_card_and_the_list_show_roles(self):
        user = get_user_model().objects.create_superuser("operator", password="x" * 20)
        self.client.force_login(user)

        page = self.client.get(reverse("control_plane:machine", args=["example-exit"]))
        board = self.client.get(reverse("control_plane:machines"))

        self.assertContains(page, "Exit node")
        self.assertNotContains(page, "No managed services.")
        self.assertContains(board, "Tailnet DNS")


class PublicAddressTests(TestCase):
    def setUp(self):
        devices(
            device(
                "example-vps",
                addresses=["100.64.0.10"],
                tags=["tag:server"],
                endpoints=[
                    "10.0.0.20:41641",
                    "100.64.0.10:41641",
                    "192.0.2.4:41641",
                    "203.0.113.7:41641",
                    "[fe80::1]:41641",
                ],
            )
        )

    def test_public_addresses_come_from_the_client_endpoints(self):
        with PUBLIC_RANGE:
            self.assertEqual(tailnet_presence()["example-vps"].public_addresses, ("203.0.113.7",))
            self.assertIn("203.0.113.7", wanted_addresses())

    def test_a_declared_address_is_public_by_the_same_ranges(self):
        from .public_registry import public_address

        with PUBLIC_RANGE:
            self.assertEqual(public_address("203.0.113.7:443"), "203.0.113.7")
            self.assertEqual(public_address("192.0.2.4"), "")
            self.assertEqual(public_address("10.0.0.20"), "")

    def test_the_machine_page_names_the_holder(self):
        store(
            "registry.address",
            {"address": "203.0.113.7", "organisation": "Example Hosting",
             "read_at": timezone.now().isoformat()},
        )
        user = get_user_model().objects.create_superuser("operator", password="x" * 20)
        self.client.force_login(user)

        with PUBLIC_RANGE:
            response = self.client.get(reverse("control_plane:machine", args=["example-vps"]))

        self.assertContains(
            response,
            '<span class="muted">Public</span> <code>203.0.113.7</code> '
            '<span class="muted">Example Hosting</span>',
            html=False,
        )
        self.assertContains(response, '<span class="muted">Tailnet</span> <code>100.64.0.10</code>')
        # A private address no other device shares is the provider's network, not a LAN.
        self.assertNotContains(response, '<span class="muted">LAN</span>')

    def home(self, *extra):
        devices(
            device(
                "example-phone",
                addresses=["100.64.0.11"],
                endpoints=[
                    "10.0.50.23:41641", "203.0.113.9:41641", "192.0.0.6:41641",
                    # Its carrier's address: its own, and still not where it lives.
                    "[2001:db8:0:9::1]:41641",
                ],
            ),
            device(
                "example-docker-host",
                addresses=["100.64.0.12"],
                endpoints=["172.17.0.1:41641", "10.0.50.40:41641", "203.0.113.9:41641"],
            ),
            device(
                "example-other-docker-host",
                addresses=["100.64.0.13"],
                endpoints=["172.17.0.1:41641"],
            ),
            *extra,
        )
        user = get_user_model().objects.create_superuser("operator", password="x" * 20)
        self.client.force_login(user)

    def header(self, name):
        with PUBLIC_RANGE:
            return self.client.get(reverse("control_plane:machine", args=[name]))

    def test_a_device_behind_a_shared_address_shows_its_lan_and_no_public_address(self):
        self.home()
        response = self.header("example-phone")

        self.assertContains(response, '<span class="muted">Tailnet</span> <code>100.64.0.11</code>')
        self.assertContains(response, '<span class="muted">LAN</span> <code>10.0.50.23</code>')
        self.assertNotContains(response, "203.0.113.9")
        self.assertNotContains(response, "2001:db8:0:9")
        self.assertNotContains(response, "Public")

    def test_a_container_bridge_is_not_the_lan(self):
        self.home()

        self.assertContains(
            self.header("example-docker-host"), '<span class="muted">LAN</span> <code>10.0.50.40</code>'
        )
        self.assertNotContains(self.header("example-other-docker-host"), "LAN")

    def test_an_address_inside_an_advertised_subnet_route_is_the_lan(self):
        self.home(
            device(
                "example-router",
                addresses=["100.64.0.14"],
                advertised_routes=["10.0.60.0/24", "0.0.0.0/0"],
                endpoints=["10.0.60.5:41641"],
            )
        )

        self.assertContains(
            self.header("example-router"), '<span class="muted">LAN</span> <code>10.0.60.5</code>'
        )

    def test_a_special_purpose_address_is_not_public(self):
        from .reach import is_public

        self.assertFalse(is_public("192.0.0.6"))
        with PUBLIC_RANGE:
            self.assertTrue(is_public("203.0.113.7"))

    def test_without_the_public_range_nothing_is_public(self):
        self.assertEqual(
            {item.name: item.public_addresses for item in machine_catalog()}["example-vps"], ()
        )


class ContainerVisibilityTests(TestCase):
    def setUp(self):
        devices(device("example-host", addresses=["100.64.0.20"]))
        user = get_user_model().objects.create_superuser("operator", password="x" * 20)
        self.client.force_login(user)

    def page(self):
        return self.client.get(reverse("control_plane:machine", args=["example-host"]))

    def test_no_connection_reads_containers_so_none_are_not_visible(self):
        self.assertContains(self.page(), "Not visible")

    def test_a_connected_container_reader_makes_none_a_reading(self):
        from .services import CONTAINER_KIND

        store(CONTAINER_KIND)

        response = self.page()
        self.assertNotContains(response, "Not visible")
        self.assertContains(response, "None")


class EstateQueryBudgetTests(TestCase):
    def snapshot_queries(self):
        from django.db import connection as db
        from django.test.utils import CaptureQueriesContext

        from .dashboard import operating_snapshot

        with (
            mock.patch("application.domains.extension_domains", return_value=()),
            mock.patch("application.plugins.plugin_connection_specs", return_value=()),
            mock.patch("contacts.d1.query", side_effect=AssertionError("a page render called D1")),
            CaptureQueriesContext(db) as queries,
        ):
            operating_snapshot(principal=cli_principal())
        return len(queries)

    def estate_of(self, size):
        devices(*(device(f"example-host-{index}", addresses=[f"100.64.0.{index + 1}"])
                  for index in range(size)))
        for index in range(size):
            declare_record(f"record-{index}", f"app{index}.example.com", f"100.64.0.{index + 1}")
            connection(f"example-ssh-{index}", reachable=False)
        store("cloudflare.zone", {"zone": "example.com", "connection_ref": "example-dns"})

    def test_the_estate_card_costs_the_same_whatever_the_estate_holds(self):
        self.estate_of(1)
        small = self.snapshot_queries()
        ManagedResource.objects.all().delete()
        ProviderConnection.objects.all().delete()
        self.estate_of(6)
        large = self.snapshot_queries()

        self.assertEqual(small, large)

    def test_the_estate_reads_what_the_dashboard_already_read(self):
        """Every catalogue it reads is shared with the queue in one projection."""

        self.estate_of(3)
        with_estate = self.snapshot_queries()
        with (
            mock.patch("application.estate.cards", return_value=()),
            mock.patch("application.estate.attention", return_value=()),
        ):
            without = self.snapshot_queries()

        self.assertLessEqual(with_estate - without, 1)
