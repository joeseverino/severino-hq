"""The estate on the surfaces an operator opens first: search, dashboard, queue."""

from datetime import timedelta
from ipaddress import ip_network
from unittest import mock

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from hq.domains.control_plane.models import (
    ManagedResource,
    ProviderConnection,
)
from hq.platform.application.markup_testing import text
from hq.platform.application.security import cli_principal
from hq.platform.core.audit import record_event
from hq.platform.core.models import AuditLog
from hq.platform.core.testing import connection, declare, operator
from hq.platform.core.tests.estate_sizes import assert_cost_does_not_grow

from ..estate import estate_reading
from ..inventory_testing import store
from ..projection import projection_scope
from ..public_registry import wanted_addresses
from ..resources import get_resource
from ..security import Capability, Principal
from ..tailnet_presence import tailnet_presence
from .host_only import host_only

READ = Principal("test", "read", frozenset({Capability.READ}))
# 203.0.113.0/24 stands in for a public range here.
PUBLIC_RANGE = mock.patch("hq.platform.application.reach.DOCUMENTATION", (ip_network("192.0.2.0/24"),))


def device(name, **fields):
    return {"name": name, "online": True, **fields}


def devices(*records, nameservers=()):
    store("tailscale.device", *records)
    store("tailscale.policy", {"dns": {"dns": list(nameservers)}})


def declared(key, kind, spec, *, status=None):
    return declare(
        key,
        kind,
        spec,
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


class AuditSearchTitleTests(TestCase):
    def test_an_audit_hit_is_titled_by_the_object_label_not_its_id(self):
        operator(self.client)
        resource = declare("example-host", "machine")
        record_event(action=AuditLog.Action.UPDATED, obj=resource, type_label="Managed resource")
        event = AuditLog.objects.filter(object_id=str(resource.pk), action=AuditLog.Action.UPDATED).latest("created_at")

        self.assertNotIn(str(resource.pk), str(event))

        response = self.client.get(reverse("search"), {"q": "example-host"})

        self.assertIn("Updated example-host", text(response))
        self.assertNotContains(response, f"#{resource.pk}")


def in_days(days):
    return (timezone.now() + timedelta(days=days, hours=1)).isoformat()


class CertificateListTests(TestCase):
    """Every certificate that says when it runs out, edge and managed, soonest first."""

    def setUp(self):
        store("cloudflare.zone", {"zone": "example.com", "connection_ref": "example-dns"})

    def edge(self, *certificates):
        store(
            "cloudflare.edge_certificate",
            *(
                {
                    "connection_ref": "example-dns",
                    "zone": "example.com",
                    "id": f"e{index}",
                    "hosts": [host],
                    "status": "active",
                    **({"expires_on": expires} if expires else {}),
                }
                for index, (host, expires) in enumerate(certificates)
            ),
        )

    def certificates(self):
        with projection_scope():
            return estate_reading().certificates

    def test_only_a_certificate_that_says_when_it_expires_is_listed_soonest_first(self):
        self.edge(("example.com", in_days(40)), ("www.example.com", None))
        declared(
            "example-cert",
            "tls.certificate",
            {"certificate_name": "example-cert", "domains": ["app.example.com"]},
            status={"not_after": in_days(50)},
        )
        declared("unnamed-cert", "tls.certificate", {"domains": ["api.example.com"]}, status={"not_after": in_days(20)})
        declared("pending-cert", "tls.certificate", {"certificate_name": "pending", "domains": ["new.example.com"]})
        declare_record("app-record", "app.example.com", "100.64.0.5")

        found = self.certificates()

        self.assertEqual([item.subject for item in found], ["unnamed-cert", "example.com", "example-cert"])
        self.assertEqual([item.resource_key for item in found], ["unnamed-cert", "", "example-cert"])


class ActionItemTests(TestCase):
    def test_the_action_items_page_links_each_subject(self):
        operator(self.client)
        devices(device("example-host", update_available=True, client_version="1.102.3"))

        response = self.client.get(reverse("action_items"))

        self.assertContains(response, f'href="{reverse("control_plane:machine", args=["example-host"])}"')


def addresses(name):
    """What a machine's record says each of its addresses is, as its page's header draws them."""

    with PUBLIC_RANGE:
        read = get_resource("machines", name, principal=READ)
    return [(item["label"], item["address"], item["holder"]) for item in read["header_addresses"]]


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
        from ..public_registry import public_address

        with PUBLIC_RANGE:
            self.assertEqual(public_address("203.0.113.7:443"), "203.0.113.7")
            self.assertEqual(public_address("192.0.2.4"), "")
            self.assertEqual(public_address("10.0.0.20"), "")

    def test_the_machine_names_the_holder(self):
        store(
            "registry.address",
            {"address": "203.0.113.7", "organization": "Example Hosting", "read_at": timezone.now().isoformat()},
        )

        # A private address no other device shares is the provider's network, not a LAN.
        self.assertEqual(
            addresses("example-vps"),
            [("Tailnet", "100.64.0.10", ""), ("Public", "203.0.113.7", "Example Hosting")],
        )


class ContainerVisibilityTests(TestCase):
    def setUp(self):
        devices(device("example-host", addresses=["100.64.0.20"]))

    def read(self):
        return get_resource("machines", "example-host", principal=READ)

    def test_no_connection_reads_containers_so_none_are_not_visible(self):
        self.assertFalse(self.read()["containers_visible"])
        self.assertFalse(self.read()["containers"])
        operator(self.client)
        self.assertContains(self.client.get(reverse("control_plane:machines")), "Not visible")


class EstateQueryBudgetTests(TestCase):
    def estate_of(self, size):
        devices(*(device(f"example-host-{index}", addresses=[f"100.64.0.{index + 1}"]) for index in range(size)))
        for index in range(size):
            declare_record(f"record-{index}", f"app{index}.example.com", f"100.64.0.{index + 1}")
            connection("ssh", f"example-ssh-{index}", reachable=False)
        store("cloudflare.zone", {"zone": "example.com", "connection_ref": "example-dns"})

    def test_the_estate_card_costs_the_same_whatever_the_estate_holds(self):
        from ..dashboard import operating_snapshot

        def arrange(size):
            ManagedResource.objects.all().delete()
            ProviderConnection.objects.all().delete()
            self.estate_of(size)

        def draw(_size):
            with host_only():
                return operating_snapshot(principal=cli_principal())

        def online(snapshot):
            detail = next(card["detail"] for card in snapshot["cards"] if card["id"] == "hq.estate.machines")
            return int(detail.partition(" online")[0].rpartition(" ")[2])

        assert_cost_does_not_grow(self, sizes=(1, 6), arrange=arrange, draw=draw, rows=online)
