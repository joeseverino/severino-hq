"""A service wired wrong is said so: wiring faults, the facets providers supply, and TLS HQ does not own."""

from django.test import TestCase

from hq.domains.control_plane.models import ManagedResource
from hq.domains.control_plane.provider_spec import SERVICE_FACET_IDS, SERVICE_FACETS
from hq.domains.control_plane.providers import PROVIDERS

from ..attention import services as service_attention
from ..services import find_service, service_catalog
from .service_world import declare_world, healthy


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

        self.assertTrue(service.faults)
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

        service = find_service("app.example.com")

        self.assertEqual(len(service.faults), 1)
        self.assertEqual(service.status, "attention")

    def test_the_queue_title_says_what_is_wrong(self):
        for key in ("dns-a", "dns-b"):
            healthy(
                ManagedResource.objects.create(
                    key=key,
                    kind="adguard.rewrite",
                    spec={"domain": "app.example.com", "answer": "10.0.0.10"},
                )
            )

        (fault,) = find_service("app.example.com").faults
        (insight,) = (item for item in service_attention() if item.key == "service:app.example.com")

        self.assertIn("app.example.com", insight.title)
        self.assertIn(fault.split(". ", 1)[0].rstrip("."), insight.title)

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

    def _records(self, *records, kind="cloudflare.dns_record"):
        for index, (record_type, content) in enumerate(records):
            spec = {"zone": "example.com", "name": "app.example.com", "record_type": record_type, "content": content}
            if kind == "adguard.rewrite":
                spec = {"domain": "app.example.com", "answer": content}
            healthy(ManagedResource.objects.create(key=f"rec-{index}", kind=kind, spec=spec))
        return [fault for fault in find_service("app.example.com").faults if "are set up" in fault]

    def test_an_a_and_an_aaaa_record_on_one_name_do_not_compete(self):
        self.assertEqual(self._records(("A", "198.51.100.7"), ("AAAA", "2001:db8::7")), [])

    def test_an_internal_rewrite_per_address_family_does_not_compete(self):
        self.assertEqual(
            self._records(("", "10.0.0.10"), ("", "fd00::10"), kind="adguard.rewrite"),
            [],
        )

    def test_two_records_of_one_type_on_one_name_still_compete(self):
        self.assertEqual(len(self._records(("A", "198.51.100.7"), ("A", "198.51.100.8"))), 1)

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
        Both are ``attention``, and a shared label would send an operator
        looking for a missing declaration on a name that has all three of them.
        """
        ManagedResource.objects.create(
            key="app-dns",
            kind="adguard.rewrite",
            spec={"domain": "app.example.com", "answer": "10.0.0.10"},
        )

        service = find_service("app.example.com")

        self.assertEqual(service.faults, ())
        self.assertEqual(service.status, "attention")
        self.assertEqual(service.status_label, "Not checked yet")

    def test_a_degraded_resource_outranks_a_wiring_gap(self):
        """Something that was working and is not is not the same as never wired."""
        resource = ManagedResource.objects.create(
            key="app-dns",
            kind="adguard.rewrite",
            spec={"domain": "app.example.com", "answer": "10.0.0.10"},
        )
        resource.conditions = [{"type": "Degraded", "status": True, "reason": "", "message": "No answer."}]
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
        resource.conditions = [{"type": "Degraded", "status": True, "reason": "", "message": "No answer."}]
        resource.save(update_fields=["conditions"])

        self.assertEqual(service_attention(), ())


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

        Filtered to the facets some provider supplies, in the catalog's order:
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


class TlsHqDoesNotOwnTests(TestCase):
    """A name served over TLS by a certificate HQ can never hold.

    An internally signed certificate is signed by a CA that is deliberately
    air-gapped, so HQ cannot own one and never will. Counting only declared
    certificates would report "no certificate covers this" for names served
    over TLS, so HQ looks at what the proxy is actually serving.
    """

    def a_proxy_serving(self, hostname, certificate=None):
        from django.utils import timezone

        from hq.domains.control_plane.models import ProviderInventory

        record = {"domain_names": [hostname], "forward_host": "10.0.0.9"}
        if certificate is not None:
            record["certificate"] = certificate
        ProviderInventory.objects.update_or_create(
            kind="npm.proxy_host",
            defaults={"records": [record], "observed_at": timezone.now()},
        )

    def test_a_certificate_the_proxy_serves_answers_the_question(self):
        from ..services import _certificates_in_use

        self.a_proxy_serving(
            "a-host.example.com",
            {"name": "a-host.example.com", "domains": ["a-host.example.com"]},
        )

        self.assertEqual(_certificates_in_use()["a-host.example.com"]["name"], "a-host.example.com")

    def test_the_certificate_is_read_through_the_kinds_that_declare_one(self):
        from dataclasses import replace
        from unittest import mock

        from hq.domains.control_plane.providers import PROVIDERS

        from ..services import _certificates_in_use

        self.a_proxy_serving(
            "a-host.example.com",
            {"name": "a-host.example.com", "domains": ["a-host.example.com"]},
        )
        silent = replace(PROVIDERS["npm.proxy_host"], served_certificate=None)
        with mock.patch.dict(PROVIDERS, {"npm.proxy_host": silent}):
            self.assertEqual(_certificates_in_use(), {})

    def test_a_proxy_serving_nothing_over_tls_is_not_counted(self):
        from ..services import _certificates_in_use

        self.a_proxy_serving("a-host.example.com")

        self.assertEqual(_certificates_in_use(), {})

    def test_the_reading_is_not_taken_unless_something_asks(self):
        """Most services carry a declared certificate and never reach it."""

        from ..services import _CertificatesInUse

        taken = []
        held = _CertificatesInUse(lambda: taken.append(1) or {})

        self.assertEqual(taken, [])
        held.covering("anything")
        held.covering("anything else")
        self.assertEqual(taken, [1])
