"""HQ counts itself as a service, on the machine it runs on."""

from __future__ import annotations

from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from control_plane.models import ManagedResource, OperationRequest, ProviderInventory

from .hq_self import LABEL, hq_hostnames, hq_service, served_at
from .machines import machine

SITE = override_settings(
    SEVERINO_SITE_HOST="hq.example.com",
    ALLOWED_HOSTS=["hq.example.com", "testserver"],
)


def own(*addresses):
    return mock.patch(
        "application.hq_self.host_addresses",
        return_value=frozenset({"127.0.0.1", "::1", *addresses}),
    )


def declare(name, *addresses):
    ManagedResource.objects.create(
        key=name, kind="machine", spec={"name": name, "addresses": list(addresses)}
    )


class LabelTests(TestCase):
    @override_settings(SEVERINO_SITE_NAME="Example HQ")
    def test_hq_is_called_by_the_configured_site_name(self):
        from .hq_self import SelfService, site_label

        self.assertEqual(site_label(), "Example HQ")
        self.assertEqual(SelfService(hostnames=("hq.example.com",)).label, "Example HQ")


class HostnameTests(TestCase):
    @override_settings(
        SEVERINO_SITE_HOST="hq.example.com",
        ALLOWED_HOSTS=[
            "alt.example.com",
            "hq.example.com",
            "*.example.com",
            ".example.org",
            "localhost",
            "testserver",
            "192.0.2.5",
        ],
    )
    def test_only_concrete_names_count_site_host_first(self):
        self.assertEqual(hq_hostnames(), ("hq.example.com", "alt.example.com"))


@SITE
class MachineTests(TestCase):
    def test_the_machine_its_name_answers_at_runs_it(self):
        declare("example-host", "100.64.0.9")
        declare("example-other", "100.64.0.10")
        ProviderInventory.objects.create(
            kind="adguard.rewrite",
            records=[{"domain": "hq.example.com", "answer": "100.64.0.9"}],
            observed_at=timezone.now(),
        )

        with own():
            found = machine("example-host")
            other = machine("example-other")

        self.assertTrue(found.runs_hq)
        self.assertEqual(found.hq_hostnames, ("hq.example.com",))
        self.assertEqual(found.serves_count, 1)
        self.assertFalse(other.runs_hq)

    def test_the_machine_at_its_listen_address_runs_it(self):
        declare("example-host", "192.0.2.44")

        with own("192.0.2.44"):
            self.assertTrue(machine("example-host").runs_hq)

    def test_no_address_in_common_claims_nothing(self):
        declare("example-host", "192.0.2.44")

        with own("192.0.2.99"):
            self.assertFalse(machine("example-host").runs_hq)


@SITE
class PageTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user("hq-operator", password="x" * 20)
        self.client.force_login(user)
        declare("example-host", "192.0.2.44")

    def test_the_machine_page_serves_hq(self):
        with own("192.0.2.44"):
            response = self.client.get(
                reverse("control_plane:machine", kwargs={"name": "example-host"})
            )

        self.assertContains(response, LABEL)
        self.assertContains(
            response,
            '<a href="/infrastructure/services/hq.example.com/" data-entity="Service">hq.example.com</a>',
        )
        self.assertNotContains(response, "No managed services")

    def test_the_services_list_includes_it_read_only(self):
        with own("192.0.2.44"):
            service = hq_service()
            response = self.client.get(reverse("control_plane:services"))

        self.assertEqual(service.machine, "example-host")
        self.assertContains(response, "<th>Hostname</th><th>Runs on</th><th>State</th>", html=False)
        self.assertContains(response, LABEL)
        self.assertContains(response, "hq.example.com")
        self.assertContains(
            response, reverse("control_plane:machine", kwargs={"name": "example-host"})
        )
        self.assertEqual(
            list(ManagedResource.objects.values_list("kind", flat=True)), ["machine"]
        )
        self.assertFalse(OperationRequest.objects.exists())


def tailnet_device(name, addresses, endpoints=()):
    ProviderInventory.objects.update_or_create(
        kind="tailscale.device",
        defaults={
            "records": [
                {
                    "name": name,
                    "online": True,
                    "addresses": list(addresses),
                    "endpoints": list(endpoints),
                }
            ],
            "observed_at": timezone.now(),
        },
    )


class _Request:
    """The part of an ASGI request HQ reads: the address it arrived on."""

    def __init__(self, host):
        self.scope = {"server": (host, 443)}


@SITE
class TailnetMachineTests(TestCase):
    """A machine known only to the tailnet carries only tailnet addresses."""

    def setUp(self):
        tailnet_device(
            "example-laptop",
            ("100.64.0.5", "fd7a:115c:a1e0::5"),
            ("192.0.2.10:41641", "[2001:db8::10]:41641"),
        )

    def test_an_endpoint_on_this_host_names_the_device(self):
        with own("192.0.2.10"):
            found = machine("example-laptop")

        self.assertTrue(found.runs_hq)
        self.assertEqual(found.hq_hostnames, ("hq.example.com",))

    def test_the_address_a_request_arrived_on_names_the_machine(self):
        with own():
            service = hq_service(_Request("100.64.0.5"))
            found = machine("example-laptop", served_at=served_at(_Request("100.64.0.5")))

        self.assertEqual(service.machine, "example-laptop")
        self.assertTrue(found.runs_hq)

    def test_no_request_and_no_endpoint_in_common_claims_nothing(self):
        with own("192.0.2.99"):
            self.assertFalse(machine("example-laptop").runs_hq)
            self.assertEqual(hq_service().machine, "")

    def test_an_endpoint_two_devices_report_names_neither(self):
        ProviderInventory.objects.filter(kind="tailscale.device").update(
            records=[
                {"name": "example-laptop", "addresses": ["100.64.0.5"],
                 "endpoints": ["192.0.2.10:41641"]},
                {"name": "example-desk", "addresses": ["100.64.0.6"],
                 "endpoints": ["192.0.2.10:41642"]},
            ]
        )

        with own("192.0.2.10"):
            self.assertEqual(hq_service().machine, "")

    def test_loopback_and_link_local_name_no_machine(self):
        declare("example-host", "127.0.0.1", "169.254.0.5", "fe80::5")

        with own("169.254.0.5", "fe80::5"):
            self.assertFalse(machine("example-host").runs_hq)
            self.assertEqual(hq_service(_Request("127.0.0.1")).machine, "")
        self.assertEqual(served_at(_Request("127.0.0.1")), ())
        self.assertEqual(served_at(_Request("100.64.0.5")), ("100.64.0.5",))
        self.assertEqual(served_at(object()), ())


@SITE
class TopologyTests(TestCase):
    def test_hq_is_a_service_that_runs_on_its_machine(self):
        from .security import Capability, Principal
        from .topology import derive_topology

        declare("example-host", "192.0.2.44")
        reader = Principal("reader", "test", frozenset({Capability.READ}))

        with own("192.0.2.44"), mock.patch(
            "application.plugins.plugin_connection_specs", return_value=()
        ):
            topology = derive_topology(principal=reader)

        nodes = {node.id: node for node in topology.nodes}
        node = nodes["service:hq.example.com"]
        self.assertEqual(node.kind, "service")
        self.assertEqual(node.subtitle, LABEL)
        self.assertIn(
            ("service:hq.example.com", "machine:example-host", "runs_on"),
            {(edge.source, edge.target, edge.kind) for edge in topology.edges},
        )

    def test_the_page_seeds_the_address_a_request_arrived_on(self):
        from .security import Capability, Principal
        from .topology import derive_topology

        tailnet_device("example-laptop", ("100.64.0.5",))
        reader = Principal("reader", "test", frozenset({Capability.READ}))

        with own(), mock.patch(
            "application.plugins.plugin_connection_specs", return_value=()
        ):
            topology = derive_topology(principal=reader, request=_Request("100.64.0.5"))

        self.assertIn(
            ("service:hq.example.com", "machine:example-laptop", "runs_on"),
            {(edge.source, edge.target, edge.kind) for edge in topology.edges},
        )
