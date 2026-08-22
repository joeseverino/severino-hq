"""What the service view has to get right to be worth trusting.

Every hostname here is under ``example.com`` or ``example.test``. This repository
is public, and a fixture is documentation whether or not it was meant to be.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from control_plane.models import ManagedResource
from control_plane.providers import PROVIDERS, SERVICE_FACETS, SERVICE_FACET_IDS
from projects.models import Project

from .attention import services as service_attention
from .sections import services as service_cards
from .services import (
    alias_target,
    find_service,
    service_catalog,
    service_reading,
)

APP_HOST = {
    "name": "app-host",
    "role": "Docker host",
    "addresses": ["10.0.0.10"],
}


def declare_world(containers: bool = False):
    """The machine these tests forward to, and optionally what answers on it.

    A container sharing its machine's network publishes no ports for a sweep to
    find, so what it answers on is declared. That is the case these fixtures
    exercise: nothing here has ever been swept.
    """

    ManagedResource.objects.create(key="app-host", kind="machine", spec=APP_HOST)
    if not containers:
        return
    for name, port in (("web", 8000), ("cache", 6379)):
        ManagedResource.objects.create(
            key=f"app-host-{name}",
            kind="portainer.container",
            spec={
                "connection_ref": "a-portainer",
                "host": "app-host",
                "name": name,
                "serves_ports": [port],
            },
        )


def healthy(resource: ManagedResource) -> ManagedResource:
    resource.conditions = [{"type": "Ready", "status": True, "reason": "", "message": ""}]
    resource.save(update_fields=["conditions"])
    return resource


def facet(service, facet_id):
    """One facet of a service, by name rather than by position."""

    return next(item for item in service.facets if item.id == facet_id)


class ServiceCompositionTests(TestCase):
    """The join itself: three declarations meeting on one name."""

    def setUp(self):
        declare_world()

    def _wire(self, hostname="app.example.com", *, certificate=True):
        healthy(
            ManagedResource.objects.create(
                key="app-dns",
                kind="adguard.rewrite",
                spec={"domain": hostname, "answer": "10.0.0.10"},
            )
        )
        healthy(
            ManagedResource.objects.create(
                key="app-proxy",
                kind="npm.proxy_host",
                spec={
                    "domain_names": [hostname],
                    "forward_scheme": "http",
                    "forward_host": "10.0.0.10",
                    "forward_port": 8000,
                },
            )
        )
        if certificate:
            healthy(
                ManagedResource.objects.create(
                    key="wildcard",
                    kind="tls.certificate",
                    spec={
                    "certificate_name": "wildcard",
                    "domains": ["example.com", "*.example.com"],
                    "install_on": ["a-proxy"],
                    "renewal_window_days": 30,
                },
                )
            )

    def test_three_resources_compose_into_one_service(self):
        self._wire()

        catalog = service_catalog()

        self.assertEqual([service.hostname for service in catalog], ["app.example.com"])
        service = catalog[0]
        self.assertEqual(
            {facet.id: [claim.resource_key for claim in facet.claims] for facet in service.facets if facet.claims},
            {"dns": ["app-dns"], "proxy": ["app-proxy"], "certificate": ["wildcard"]},
        )
        self.assertEqual(service.faults, ())
        self.assertEqual(service.status, "good")

    def test_a_wildcard_certificate_covers_a_name_it_does_not_list(self):
        """The certificate lists ``*.example.com``; the service is a subdomain.

        Matched through the same wildcard rule the certificate provider itself
        validates with, so coverage cannot mean one thing at declaration time and
        another on this page.
        """
        self._wire()

        certificate = facet(find_service("app.example.com"), "certificate")

        self.assertEqual(certificate.id, "certificate")
        self.assertTrue(certificate.present)

    def test_a_wildcard_never_invents_a_service_of_its_own(self):
        """Covering is not declaring, or the board would list ``*.example.com``."""
        healthy(
            ManagedResource.objects.create(
                key="wildcard",
                kind="tls.certificate",
                spec={
                    "certificate_name": "wildcard",
                    "domains": ["example.com", "*.example.com"],
                    "install_on": ["a-proxy"],
                    "renewal_window_days": 30,
                },
            )
        )

        self.assertEqual(service_catalog(), ())

    def test_names_that_differ_only_in_case_or_a_trailing_dot_are_one_service(self):
        healthy(
            ManagedResource.objects.create(
                key="app-dns",
                kind="adguard.rewrite",
                spec={"domain": "App.Example.com.", "answer": "10.0.0.10"},
            )
        )
        healthy(
            ManagedResource.objects.create(
                key="app-proxy",
                kind="npm.proxy_host",
                spec={
                    "domain_names": ["app.example.com"],
                    "forward_scheme": "http",
                    "forward_host": "10.0.0.10",
                    "forward_port": 8000,
                },
            )
        )

        catalog = service_catalog()

        self.assertEqual(len(catalog), 1)
        self.assertTrue(facet(catalog[0], "dns").present)
        self.assertTrue(facet(catalog[0], "proxy").present)

    def test_a_disabled_resource_stops_supplying_its_facet(self):
        self._wire()
        ManagedResource.objects.filter(key="app-proxy").update(enabled=False)

        service = find_service("app.example.com")

        self.assertFalse(facet(service, "proxy").present)
        self.assertIsNone(service.origin)


class OriginResolutionTests(TestCase):
    def setUp(self):
        declare_world(containers=True)

    def _proxy(self, forward_host="10.0.0.10", forward_port=8000):
        healthy(
            ManagedResource.objects.create(
                key="app-proxy",
                kind="npm.proxy_host",
                spec={
                    "domain_names": ["app.example.com"],
                    "forward_scheme": "http",
                    "forward_host": forward_host,
                    "forward_port": forward_port,
                },
            )
        )
        return find_service("app.example.com")

    def test_a_forwarding_address_resolves_to_a_host_and_its_container(self):
        origin = self._proxy().origin

        self.assertTrue(origin.known)
        self.assertEqual(origin.host, "app-host")
        self.assertEqual(origin.container, "web")

    def test_a_port_no_container_claims_names_the_host_and_stops(self):
        """A host HQ can name; a container it cannot. Silence beats a guess."""
        origin = self._proxy(forward_port=9999).origin

        self.assertEqual(origin.host, "app-host")
        self.assertEqual(origin.container, "")

    def test_a_port_that_is_only_a_substring_does_not_claim_a_container(self):
        """``800`` must not match the container listening on ``8000``.

        A substring match is the failure mode of reading ports out of prose, and
        it produces a confident wrong answer rather than a visible gap.
        """
        origin = self._proxy(forward_port=800).origin

        self.assertEqual(origin.container, "")

    def test_an_address_no_host_claims_is_reported_as_unknown(self):
        service = self._proxy(forward_host="10.9.9.9")

        self.assertFalse(service.origin.known)
        self.assertIn("cannot match to any machine", " ".join(service.faults))


class WiringFaultTests(TestCase):
    """The faults exist only in the join, which is why no resource page has them."""

    def setUp(self):
        declare_world(containers=True)

    def test_a_served_name_with_no_covering_certificate_is_a_fault(self):
        healthy(
            ManagedResource.objects.create(
                key="app-proxy",
                kind="npm.proxy_host",
                spec={
                    "domain_names": ["app.example.test"],
                    "forward_scheme": "http",
                    "forward_host": "10.0.0.10",
                    "forward_port": 8000,
                },
            )
        )

        service = find_service("app.example.test")

        self.assertIn("no declared certificate covers it", " ".join(service.faults))
        self.assertEqual(service.status, "attention")

    def test_two_declarations_of_the_same_kind_contradict_each_other(self):
        for key in ("dns-a", "dns-b"):
            healthy(
                ManagedResource.objects.create(
                    key=key,
                    kind="adguard.rewrite",
                    spec={"domain": "app.example.com", "answer": "10.0.0.10"},
                )
            )

        faults = " ".join(find_service("app.example.com").faults)

        self.assertIn("Two adguard.rewrite resources declare this name", faults)

    def test_two_declarations_of_different_kinds_on_one_facet_are_not_a_fault(self):
        """An internal answer and a public one are both DNS and legitimately differ."""
        healthy(
            ManagedResource.objects.create(
                key="internal",
                kind="adguard.rewrite",
                spec={"domain": "app.example.com", "answer": "10.0.0.10"},
            )
        )
        healthy(
            ManagedResource.objects.create(
                key="public",
                kind="cloudflare.dns_record",
                spec={
                    "zone": "example.com",
                    "name": "app.example.com",
                    # A name rather than an address: the addresses reserved for
                    # writing about addresses are the ones a parked name uses,
                    # and this test is about two kinds on one facet.
                    "record_type": "CNAME",
                    "content": "app.pages.dev",
                },
            )
        )

        self.assertEqual(find_service("app.example.com").faults, ())

    def test_a_txt_record_is_policy_and_never_becomes_a_service(self):
        healthy(
            ManagedResource.objects.create(
                key="spf",
                kind="cloudflare.dns_record",
                spec={
                    "zone": "example.com",
                    "name": "example.com",
                    "record_type": "TXT",
                    "content": "v=spf1 -all",
                },
            )
        )

        self.assertEqual(service_catalog(), ())

    def test_declared_but_unobserved_is_not_the_same_word_as_incomplete(self):
        """Fully wired and never verified is what every new service looks like.

        Both are ``attention``, and sharing a label sent an operator looking for
        a missing declaration on a name that had all three of them.
        """
        ManagedResource.objects.create(
            key="app-dns",
            kind="adguard.rewrite",
            spec={"domain": "app.example.com", "answer": "10.0.0.10"},
        )

        service = find_service("app.example.com")

        self.assertEqual(service.faults, ())
        self.assertEqual(service.status, "attention")
        self.assertEqual(service.status_label, "Unverified")

    def test_a_degraded_resource_outranks_a_wiring_gap(self):
        """Something that was working and is not is not the same as never wired."""
        resource = ManagedResource.objects.create(
            key="app-dns",
            kind="adguard.rewrite",
            spec={"domain": "app.example.com", "answer": "10.0.0.10"},
        )
        resource.conditions = [
            {"type": "Degraded", "status": True, "reason": "", "message": "No answer."}
        ]
        resource.save(update_fields=["conditions"])

        self.assertEqual(find_service("app.example.com").status, "serious")

    def test_health_is_not_reported_twice_under_two_names(self):
        """``attention.infrastructure`` owns resource health; this owns wiring.

        Both feed one queue. A degraded resource appearing in both would make the
        number of things needing attention wrong, in the one place that number is
        supposed to be trustworthy.
        """
        resource = ManagedResource.objects.create(
            key="app-dns",
            kind="adguard.rewrite",
            spec={"domain": "app.example.com", "answer": "10.0.0.10"},
        )
        resource.conditions = [
            {"type": "Degraded", "status": True, "reason": "", "message": "No answer."}
        ]
        resource.save(update_fields=["conditions"])

        self.assertEqual(service_attention(), ())


class ServiceResolutionTests(TestCase):
    def test_a_certificate_still_covers_its_names_when_it_cannot_resolve(self):
        """A target that is gone stops it installing, not from covering.

        The names are authored on the certificate, so they survive a failed
        resolution. Raising instead would take out the dashboard, the nav queue
        and this page at once over one missing target.
        """
        healthy(
            ManagedResource.objects.create(
                key="wildcard",
                kind="tls.certificate",
                spec={
                    "certificate_name": "wildcard",
                    "domains": ["*.example.com"],
                    "install_on": ["a-target-that-is-gone"],
                    "renewal_window_days": 30,
                },
            )
        )
        healthy(
            ManagedResource.objects.create(
                key="app-proxy",
                kind="npm.proxy_host",
                spec={
                    "domain_names": ["app.example.com"],
                    "forward_scheme": "http",
                    "forward_host": "10.0.0.10",
                    "forward_port": 8000,
                },
            )
        )

        service = find_service("app.example.com")

        certificate = facet(service, "certificate")
        self.assertTrue(certificate.present)
        self.assertNotIn(
            "no declared certificate covers it", " ".join(service.faults)
        )

    def test_a_project_publishing_to_a_name_is_an_annotation_not_a_requirement(self):
        healthy(
            ManagedResource.objects.create(
                key="app-dns",
                kind="adguard.rewrite",
                spec={"domain": "app.example.com", "answer": "10.0.0.10"},
            )
        )
        healthy(
            ManagedResource.objects.create(
                key="tool-dns",
                kind="adguard.rewrite",
                spec={"domain": "tool.example.com", "answer": "10.0.0.10"},
            )
        )
        Project.objects.create(
            name="The app", slug="the-app", public_url="https://app.example.com/"
        )

        catalog = {service.hostname: service for service in service_catalog()}

        self.assertEqual(catalog["app.example.com"].project["name"], "The app")
        # A service run but not built here is still a service.
        self.assertIsNone(catalog["tool.example.com"].project)


class ServiceSurfaceTests(TestCase):
    """What the rest of HQ sees, and what it must not see."""

    def setUp(self):
        healthy(
            ManagedResource.objects.create(
                key="app-proxy",
                kind="npm.proxy_host",
                spec={
                    "domain_names": ["app.example.com"],
                    "forward_scheme": "http",
                    "forward_host": "10.0.0.10",
                    "forward_port": 8000,
                },
            )
        )

    def test_a_fault_reaches_the_composed_queue_with_its_own_link(self):
        item = service_attention()[0]

        self.assertEqual(item.url, reverse(
            "control_plane:service", kwargs={"hostname": "app.example.com"}
        ))
        self.assertEqual(item.status, "attention")

    def test_the_card_counts_services_and_names_the_incomplete_ones(self):
        card = service_cards()[0]

        self.assertEqual(card["value"], "1")
        self.assertEqual(card["detail"], "1 incompletely wired")

    def test_no_services_means_no_card(self):
        ManagedResource.objects.all().delete()

        self.assertEqual(service_cards(), ())
        self.assertEqual(service_reading(), {"total": 0, "incomplete": 0})


class ProviderFacetContractTests(TestCase):
    """A provider joins the service view by declaring, not by being listed.

    This is the property that makes the surface scale: adding a provider is one
    dataclass in ``control_plane.providers``, and it appears on the board, in the
    queue and in the API without another file being opened. These guard the
    declaration staying self-describing, because the moment something downstream
    special-cases a kind by name that stops being true.
    """

    def test_every_declared_facet_is_one_the_surface_knows(self):
        for provider in PROVIDERS.values():
            if provider.facet:
                self.assertIn(provider.facet, SERVICE_FACET_IDS, provider.kind)

    def test_a_participating_provider_says_how_to_read_its_hostnames(self):
        for provider in PROVIDERS.values():
            if provider.facet:
                self.assertIsNotNone(
                    provider.hostnames,
                    f"{provider.kind} claims facet {provider.facet!r} but supplies "
                    "no way to read hostnames out of its spec.",
                )

    def test_a_new_provider_joins_the_board_without_editing_the_surface(self):
        """The plug-and-play claim, exercised rather than asserted in a comment."""
        from dataclasses import replace

        invented = replace(
            PROVIDERS["adguard.rewrite"],
            kind="invented.rewrite",
            summary="A provider that did not exist when the surface was written.",
        )

        PROVIDERS[invented.kind] = invented
        try:
            healthy(
                ManagedResource.objects.create(
                    key="invented",
                    kind=invented.kind,
                    spec={"domain": "new.example.com", "answer": "10.0.0.10"},
                )
            )
            hostnames = [service.hostname for service in service_catalog()]
        finally:
            del PROVIDERS[invented.kind]

        self.assertIn("new.example.com", hostnames)

    def test_the_facet_order_is_declared_once(self):
        """Templates render columns from SERVICE_FACETS, so nothing restates it.

        Filtered to the facets some provider supplies, in the catalogue's order:
        a facet can be declared before the provider that fills it exists, and
        until then it is not a column with nothing in it.
        """
        supplyable = {provider.facet for provider in PROVIDERS.values() if provider.facet}
        catalog_order = [facet for facet, _ in SERVICE_FACETS if facet in supplyable]

        healthy(
            ManagedResource.objects.create(
                key="app-dns",
                kind="adguard.rewrite",
                spec={"domain": "app.example.com", "answer": "10.0.0.10"},
            )
        )

        self.assertEqual(
            [facet.id for facet in find_service("app.example.com").facets],
            catalog_order,
        )


class AliasNavigationTests(TestCase):
    """A second name for a service is not a service with nothing behind it."""

    def setUp(self):
        ManagedResource.objects.create(
            key="site", kind="cloudflare.dns_record", enabled=True,
            spec={"zone": "example.com", "name": "example.com", "record_type": "CNAME",
                  "content": "example.pages.dev", "proxied": True, "ttl": 1},
        )
        ManagedResource.objects.create(
            key="www", kind="cloudflare.dns_record", enabled=True,
            spec={"zone": "example.com", "name": "www.example.com",
                  "record_type": "CNAME", "content": "example.com",
                  "proxied": True, "ttl": 1},
        )

    def test_an_alias_names_the_service_it_stands_for(self):
        self.assertEqual(alias_target("www.example.com"), "example.com")

    def test_a_real_service_is_not_an_alias(self):
        self.assertEqual(alias_target("example.com"), "")

    def test_the_alias_page_goes_to_the_service_rather_than_reporting_nothing(self):
        """It reported "Nothing declared" for a name whose record is healthy."""

        user = get_user_model().objects.create_user("op", password="x")
        self.client.force_login(user)
        response = self.client.get(
            reverse("control_plane:service", kwargs={"hostname": "www.example.com"})
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response["Location"],
            reverse("control_plane:service", kwargs={"hostname": "example.com"}),
        )


class OriginNoteTests(TestCase):
    """Where a name is served is said once, in whichever place says it best.

    It was a fifth card on a four-card row, in the largest type, mostly
    restating the two cards it sat beside.
    """

    def test_it_is_silent_when_a_facet_already_names_the_container(self):
        from control_plane.providers import NameContext

        from .services import Facet, Origin, Running, Service

        running = Running(
            name="probe", host="a-docker-host", stack="probe", image="",
            state="running", status="", ports=(8099,), network_mode="bridge",
            host_address="", portainer_managed=False, connection_ref="",
            observed_at=None,
        )
        service = Service(
            hostname="probe.invalid",
            facets=(
                Facet(
                    id="runtime", label="Runtime", observed=running,
                    context=NameContext(),
                ),
            ),
            origin=Origin(address="10.0.0.9:8099", host="a-docker-host",
                          container="probe"),
        )

        self.assertFalse(service.origin_is_news)

    def test_it_speaks_up_when_something_outside_answers_the_name(self):
        from .services import Origin, Service

        service = Service(
            hostname="jseverino.com",
            facets=(),
            origin=Origin(address="jseverino.pages.dev"),
        )

        self.assertTrue(service.origin_is_news)

    def test_it_speaks_up_when_no_machine_claims_the_address(self):
        """The one case worth interrupting for: ingress forwards somewhere HQ
        cannot describe, reconcile or reach."""

        from .services import Origin, Service

        service = Service(
            hostname="probe.invalid",
            facets=(),
            origin=Origin(address="10.9.9.9:8080"),
        )

        self.assertTrue(service.origin_is_news)

    def test_it_speaks_up_when_nothing_identified_what_is_running(self):
        from control_plane.providers import NameContext

        from .services import Facet, Origin, Service

        service = Service(
            hostname="probe.invalid",
            facets=(Facet(id="runtime", label="Runtime", context=NameContext()),),
            origin=Origin(address="10.0.0.9:8099", host="a-docker-host"),
        )

        self.assertTrue(service.origin_is_news)


class OriginWordingTests(TestCase):
    """One fact, one phrasing, whichever surface asks for it.

    The board rendered "unknown host" beside a name whose own page said "Served
    by Cloudflare Pages". Both were reading the same Origin and only one of them
    had learned about the third case.
    """

    def test_something_outside_is_named_rather_than_called_unknown(self):
        from .services import Origin

        origin = Origin(address="jseverino.pages.dev")

        self.assertTrue(origin.external)
        self.assertEqual(origin.qualifier, "")
        self.assertNotEqual(origin.headline, "unknown host")

    def test_a_known_machine_reads_as_itself(self):
        from .services import Origin

        origin = Origin(address="10.0.0.9:8000", host="a-docker-host",
                        container="probe")

        self.assertEqual(origin.headline, "a-docker-host · probe")
        self.assertEqual(origin.qualifier, "")

    def test_an_address_nothing_claims_still_says_so(self):
        """The caveat has to survive: an ingress pointing somewhere HQ cannot
        describe is worth interrupting for."""

        from .services import Origin

        origin = Origin(address="10.9.9.9:8080")

        self.assertEqual(origin.qualifier, "unknown host")


class ConnectedMachineTests(TestCase):
    """A machine HQ holds a credential for is not an unknown host.

    The proxy in front of the cPanel site read as "unknown host" while the
    connections page listed that exact address under a name.
    """

    def test_an_address_a_connection_points_at_is_named(self):
        from control_plane.models import ProviderConnection
        from django.utils import timezone

        from .services import _locate

        ProviderConnection.objects.create(
            connection_ref="a-shared-host", controller_id="a-controller",
            provider="ssh", endpoint="203.0.113.10:21098", reaches=[],
            reachable=True, probed=True, observed_at=timezone.now(),
        )

        origin = _locate("203.0.113.10:443", ())

        self.assertEqual(origin.host, "a-shared-host")
        self.assertTrue(origin.known)
        self.assertEqual(origin.qualifier, "")

    def test_an_address_no_credential_points_at_is_still_unknown(self):
        from .services import _locate

        origin = _locate("10.9.9.9:443", ())

        self.assertEqual(origin.qualifier, "unknown host")

    def test_a_url_endpoint_matches_by_its_hostname(self):
        from control_plane.models import ProviderConnection
        from django.utils import timezone

        from .services import _locate

        ProviderConnection.objects.create(
            connection_ref="a-proxy", controller_id="a-controller",
            provider="npm", endpoint="https://proxy.example", reaches=[],
            reachable=True, probed=True, observed_at=timezone.now(),
        )

        origin = _locate("proxy.example:81", ())

        self.assertEqual(origin.host, "a-proxy")


class PortlessOriginTests(TestCase):
    """An address with no port is all host.

    `rpartition` puts the whole string in its last element when the separator
    is absent, so a DNS answer naming a machine HQ knows was matched against an
    empty host and read as somewhere it had never heard of.
    """

    def test_a_bare_address_matches_the_machine_it_names(self):
        from .services import _locate

        origin = _locate(
            "10.0.0.9",
            ({"name": "a-docker-host", "addresses": ["10.0.0.9"]},),
        )

        self.assertEqual(origin.host, "a-docker-host")
        self.assertFalse(origin.external)

    def test_a_bare_address_matches_a_connection_too(self):
        from control_plane.models import ProviderConnection
        from django.utils import timezone

        from .services import _locate

        ProviderConnection.objects.create(
            connection_ref="a-shared-host", controller_id="a-controller",
            provider="ssh", endpoint="203.0.113.10:21098", reaches=[],
            reachable=True, probed=True, observed_at=timezone.now(),
        )

        origin = _locate("203.0.113.10", ())

        self.assertEqual(origin.headline, "a-shared-host")

    def test_a_name_nothing_claims_is_still_answered_elsewhere(self):
        """The heuristic it guards must survive: a portless address nothing
        knows is a name answered outside this network."""

        from .services import _locate

        origin = _locate("jseverino.pages.dev", ())

        self.assertTrue(origin.external)


class ParkedNameTests(TestCase):
    """A record that exists is not a name that answers.

    Boards ask whether a record is declared and reconciled, and both are true of
    a name pointed at an address reserved for documentation -- so a parked
    domain reads as a working service.
    """

    def test_a_documentation_address_is_a_wiring_fault(self):
        from .services import Origin, _points_nowhere

        self.assertIn("reserved", _points_nowhere(Origin(address="192.0.2.1")))
        self.assertIn("reserved", _points_nowhere(Origin(address="203.0.113.9:443")))

    def test_an_unspecified_address_is_too(self):
        from .services import Origin, _points_nowhere

        self.assertIn("reached", _points_nowhere(Origin(address="0.0.0.0")))

    def test_a_real_address_is_not(self):
        from .services import Origin, _points_nowhere

        self.assertEqual(_points_nowhere(Origin(address="10.0.0.9:8000")), "")
        self.assertEqual(_points_nowhere(Origin(address="jseverino.pages.dev")), "")

    def test_nothing_routed_is_not_a_parked_name(self):
        from .services import _points_nowhere

        self.assertEqual(_points_nowhere(None), "")


class LoopbackOriginTests(TestCase):
    """A proxy forwarding to itself still names a machine.

    Terminating TLS and forwarding over loopback is the safer arrangement --
    the request never crosses a network between the proxy and the thing it
    serves -- and it made every such service unresolvable: no machine is
    declared at 127.0.0.1 because every machine is, so matching by address
    reported "unknown host" for the one hop that never left the box.
    """

    def a_container_on(self, host, name, port):
        from django.utils import timezone

        from control_plane.models import ProviderInventory
        from .services import CONTAINER_KIND

        ProviderInventory.objects.update_or_create(
            kind=CONTAINER_KIND,
            defaults={
                "records": [
                    {"name": name, "host": host, "ports": [port], "state": "running"}
                ],
                "observed_at": timezone.now(),
            },
        )

    def test_loopback_resolves_to_the_machine_listening_on_that_port(self):
        from .services import _locate

        self.a_container_on("a-docker-host", "an-app", 8000)

        origin = _locate("127.0.0.1:8000", ({"name": "a-docker-host"},))

        self.assertEqual(origin.host, "a-docker-host")
        self.assertEqual(origin.container, "an-app")

    def test_a_loopback_port_nothing_listens_on_names_no_machine(self):
        """Better silent than confidently wrong about which box it meant."""

        from .services import _locate

        self.a_container_on("a-docker-host", "an-app", 8000)

        self.assertEqual(_locate("127.0.0.1:9999", ({"name": "a-docker-host"},)).host, "")

    def test_two_machines_listening_on_that_port_is_reported_as_silence(self):
        from django.utils import timezone

        from control_plane.models import ProviderInventory
        from .services import CONTAINER_KIND, _locate

        ProviderInventory.objects.update_or_create(
            kind=CONTAINER_KIND,
            defaults={
                "records": [
                    {"name": "one", "host": "host-a", "ports": [8000], "state": "running"},
                    {"name": "two", "host": "host-b", "ports": [8000], "state": "running"},
                ],
                "observed_at": timezone.now(),
            },
        )

        origin = _locate("127.0.0.1:8000", ({"name": "host-a"}, {"name": "host-b"}))

        self.assertEqual(origin.host, "")


class WwwIsTheSameSiteTests(TestCase):
    """`www.example.com` and `example.com` as two address records are one site.

    A CNAME says "I am that name" and already folded. An address record says
    only where to go, so the pair read as two services with two certificates
    and two verdicts about one website.

    Narrow on purpose. Every other subdomain sharing an address is a different
    service on one host -- mail and a quiz on one cPanel are not each other --
    so the rule is the one prefix that conventionally means the same site.
    """

    def aliases(self, origins, declared=None):
        from .services import _aliases

        return _aliases(declared or set(origins), origins)

    def test_www_folds_into_the_apex_when_both_point_at_one_place(self):
        found = self.aliases(
            {"example.com": "203.0.113.9", "www.example.com": "203.0.113.9"}
        )

        self.assertEqual(found, {"www.example.com": "example.com"})

    def test_www_pointing_somewhere_else_stays_its_own_service(self):
        """Two addresses is two places, whatever the names suggest."""

        found = self.aliases(
            {"example.com": "203.0.113.9", "www.example.com": "203.0.113.10"}
        )

        self.assertEqual(found, {})

    def test_another_subdomain_on_the_same_address_is_not_folded(self):
        found = self.aliases(
            {"example.com": "203.0.113.9", "mail.example.com": "203.0.113.9"}
        )

        self.assertEqual(found, {})

    def test_an_apex_nobody_declares_leaves_www_alone(self):
        found = self.aliases({"www.example.com": "203.0.113.9"})

        self.assertEqual(found, {})
