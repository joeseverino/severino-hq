"""What the service view has to get right to be worth trusting.

Every hostname here is under ``example.com`` or ``example.test``. This repository
is public, and a fixture is documentation whether or not it was meant to be."""

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from hq.domains.control_plane.models import ManagedResource
from hq.domains.projects.models import Project

from .. import estate, security
from ..attention import services as service_attention
from ..services import alias_target, find_service, service_catalog, service_reading
from .markup import within
from .service_world import declare_world, facet, healthy


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
        # Every record in place is configured; nothing behind it was read running.
        self.assertEqual((service.status, service.status_label), ("unknown", "In place"))

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

    def test_disabling_the_ingress_leaves_the_name_still_pointed_somewhere(self):
        """Losing the proxy is not the name forgetting where it was sent.

        A record still names where the name points once the ingress is gone:
        the DNS record is still there, still reconciled, still pointing this
        name at a machine HQ knows by name. Reporting nothing would be HQ
        declining to read a declaration it holds. With the proxy disabled,
        requests still arrive at app-host and nothing there answers them, and
        those are two facts rather than the absence of one. The facet is what
        went away, and the test above says so on its own.
        """

        self._wire()
        ManagedResource.objects.filter(key="app-proxy").update(enabled=False)

        service = find_service("app.example.com")

        self.assertEqual(service.origin.host, "app-host")
        self.assertFalse(service.origin.external)


class SurfacesAgreeTests(TestCase):
    """The service page and the machine board must name the same machine.

    Both answer "where is this served", from the same declarations, through the
    same index, in two different functions. When one grows a rule the other
    does not, a name appears under one machine on its own page and under
    another, or none, on the board. The rule they share is stated once; this
    notices when only one of them reads it.
    """

    def setUp(self):
        declare_world(containers=True)

    def _boards(self):
        from hq.platform.application.machines import machine_catalog

        # Read as an attribute, not through getattr with a default: a default
        # turns a renamed field into an empty set, and an empty set is what this
        # is trying to tell the difference between.
        return {entry.name: set(entry.hostnames) for entry in machine_catalog()}

    def test_a_proxied_name_is_filed_where_the_ingress_forwards(self):
        """And not also under the box the record points at."""

        healthy(
            ManagedResource.objects.create(
                key="app-rewrite",
                kind="adguard.rewrite",
                # The ingress itself, which is where the *name* goes.
                spec={"domain": "app.example.com", "answer": "10.0.0.99"},
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
        boards = self._boards()

        self.assertEqual(service.origin.host, "app-host")
        self.assertIn("app.example.com", boards.get("app-host", set()))

    def test_a_name_with_only_a_record_is_filed_where_the_record_points(self):
        healthy(
            ManagedResource.objects.create(
                key="app-rewrite",
                kind="adguard.rewrite",
                spec={"domain": "app.example.com", "answer": "10.0.0.10"},
            )
        )

        service = find_service("app.example.com")
        boards = self._boards()

        self.assertEqual(service.origin.host, "app-host")
        self.assertIn("app.example.com", boards.get("app-host", set()))


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
        self.assertEqual([fault for fault in service.faults if "certificate" in fault.lower()], [])

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
        Project.objects.create(name="The app", slug="the-app", public_url="https://app.example.com/")

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

        self.assertEqual(item.url, reverse("control_plane:service", kwargs={"hostname": "app.example.com"}))
        self.assertEqual(item.status, "attention")

    def test_the_estate_card_counts_services_and_names_the_incomplete_ones(self):
        card = next(c for c in estate.cards(principal=security.cli_principal()) if c["id"] == "hq.estate.services")

        self.assertEqual(card["value"], "1")
        self.assertEqual(card["detail"], "1 not fully set up")
        self.assertEqual(card["url"], reverse("control_plane:services"))

    def test_the_services_table_sorts_once_it_holds_two_rows(self):
        self.client.force_login(get_user_model().objects.create_user("op", password="x"))

        def sorted_headings():
            html = self.client.get(reverse("control_plane:services")).content.decode()
            return [heading for heading in within(html, "class", "table-scroll") if "aria-sort" in heading.attrs]

        self.assertEqual(sorted_headings(), [])
        ManagedResource.objects.create(
            key="docs-dns", kind="adguard.rewrite", spec={"domain": "docs.example.com", "answer": "10.0.0.10"}
        )
        self.assertNotEqual(sorted_headings(), [])

    def test_no_estate_means_no_card(self):
        ManagedResource.objects.all().delete()

        self.assertEqual(estate.cards(principal=security.cli_principal()), ())
        self.assertEqual(service_reading(), {"total": 0, "incomplete": 0})


class AliasNavigationTests(TestCase):
    """A second name for a service is not a service with nothing behind it."""

    def setUp(self):
        ManagedResource.objects.create(
            key="site",
            kind="cloudflare.dns_record",
            enabled=True,
            spec={
                "zone": "example.com",
                "name": "example.com",
                "record_type": "CNAME",
                "content": "example.pages.dev",
                "proxied": True,
                "ttl": 1,
            },
        )
        ManagedResource.objects.create(
            key="www",
            kind="cloudflare.dns_record",
            enabled=True,
            spec={
                "zone": "example.com",
                "name": "www.example.com",
                "record_type": "CNAME",
                "content": "example.com",
                "proxied": True,
                "ttl": 1,
            },
        )

    def test_an_alias_names_the_service_it_stands_for_and_a_real_service_is_none(self):
        self.assertEqual(alias_target("www.example.com"), "example.com")
        self.assertEqual(alias_target("example.com"), "")

    def test_a_proxied_record_is_answered_by_its_provider_whatever_its_readout_says(self):
        from dataclasses import replace
        from unittest import mock

        from hq.domains.control_plane.providers import PROVIDERS

        spec = PROVIDERS["cloudflare.dns_record"]
        quiet = replace(spec, readout=lambda spec, status: (("Record", "", ""),))
        with mock.patch.dict(PROVIDERS, {"cloudflare.dns_record": quiet}):
            self.assertTrue(find_service("example.com").provider_answers)

    def test_the_alias_page_goes_to_the_service_rather_than_reporting_nothing(self):
        """It reported "Nothing declared" for a name whose record is healthy."""

        user = get_user_model().objects.create_user("op", password="x")
        self.client.force_login(user)
        response = self.client.get(reverse("control_plane:service", kwargs={"hostname": "www.example.com"}))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response["Location"],
            reverse("control_plane:service", kwargs={"hostname": "example.com"}),
        )


class ParkedNameTests(TestCase):
    """A record that exists is not a name that answers.

    Boards ask whether a record is declared and reconciled, and both are true of
    a name pointed at an address reserved for documentation, so a parked
    domain reads as a working service.
    """

    def test_a_documentation_or_unspecified_address_is_a_wiring_fault_and_a_real_one_is_not(self):
        from ..services import _points_nowhere
        from ..whereabouts import Origin

        for address, parked in (
            ("192.0.2.1", True),
            ("203.0.113.9:443", True),
            ("0.0.0.0", True),
            ("10.0.0.9:8000", False),
            ("example.pages.dev", False),
        ):
            with self.subTest(address):
                self.assertEqual(bool(_points_nowhere(Origin(address=address))), parked)
        self.assertNotEqual(_points_nowhere(Origin(address="192.0.2.1")), _points_nowhere(Origin(address="0.0.0.0")))
        self.assertEqual(_points_nowhere(None), "")


class WwwIsTheSameSiteTests(TestCase):
    """`www.example.com` and `example.com` as two address records are one site.

    A CNAME says "I am that name" and already folds. An address record says
    only where to go, so without this the pair reads as two services with two
    certificates and two verdicts about one website.

    Narrow on purpose. Every other subdomain sharing an address is a different
    service on one host: mail and a quiz on one cPanel are not each other, so
    the rule is the one prefix that conventionally means the same site.
    """

    def aliases(self, origins, declared=None):
        from ..service_declarations import _aliases

        return _aliases(declared or set(origins), origins)

    def test_only_www_at_the_apexs_own_address_folds_into_it(self):
        """Two addresses is two places, whatever the names suggest."""
        for origins, folded in (
            ({"example.com": "203.0.113.9", "www.example.com": "203.0.113.9"}, {"www.example.com": "example.com"}),
            ({"example.com": "203.0.113.9", "www.example.com": "203.0.113.10"}, {}),
            ({"example.com": "203.0.113.9", "mail.example.com": "203.0.113.9"}, {}),
            ({"www.example.com": "203.0.113.9"}, {}),
        ):
            with self.subTest(origins):
                self.assertEqual(self.aliases(origins), folded)


class ServicePageLinkTests(SimpleTestCase):
    """A service's own URL is the entity link's, so a wildcard or metadata name has none."""

    def test_a_wildcard_or_metadata_name_has_no_page(self):
        from ..entity_links import entity_link
        from ..services import Service

        self.assertEqual(Service(hostname="*.example.com", facets=()).url, "")
        self.assertEqual(Service(hostname="_dmarc.example.com", facets=()).url, "")
        self.assertEqual(
            Service(hostname="www.example.com", facets=()).url,
            entity_link("service", "www.example.com").url,
        )
