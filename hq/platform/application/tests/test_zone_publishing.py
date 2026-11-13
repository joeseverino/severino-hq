"""Publishing a name: adopting its records, publishing a service, names served elsewhere, and letting a domain go."""

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from hq.domains.control_plane.models import ManagedResource, OperationRequest
from hq.platform.application.plugin_testing import form_keyed

from ..adoption_testing import managing_everything
from ..security import cli_principal
from ..services import prospects, service_catalog
from ..sweep import record_sweep
from ..zones import find_zone
from .zone_records import APEX, DNS_CONNECTIONS, RECORD_KIND, ZONE_KIND, ZONES, record, sweep


@override_settings(SEVERINO_INFRASTRUCTURE_ENABLE_PUBLIC_DNS=True)
class SelfClosingAdoptionTests(TestCase):
    """ "Not adopted yet" is not a state anyone should have to clear. Declaring
    a domain is the decision, and it is made once. Asking again per record puts
    a question on the page whose answer is always yes, and reports outstanding
    work nobody intends to do.
    """

    def setUp(self):
        managing_everything(*DNS_CONNECTIONS)

    def _declare_domain(self):
        ManagedResource.objects.create(
            key="example-com",
            kind=ZONE_KIND,
            spec={"zone": "example.com", "connection_ref": "cf-example"},
            enabled=True,
        )

    def test_a_sweep_takes_on_every_record_in_a_declared_domain(self):
        self._declare_domain()

        result = record_sweep(
            {
                ZONE_KIND: {"ok": True, "records": ZONES},
                RECORD_KIND: {"ok": True, "records": APEX},
            },
            principal=cli_principal(),
        )

        self.assertEqual(len(result["adopted"]), len(APEX))
        self.assertFalse(find_zone("example.com").adoptable)

    def test_a_domain_the_credential_reaches_is_taken_on_with_its_records(self):
        """Holding a token that can edit the zone is the decision."""

        record_sweep(
            {
                ZONE_KIND: {"ok": True, "records": ZONES},
                RECORD_KIND: {"ok": True, "records": APEX},
            },
            principal=cli_principal(),
        )

        self.assertIn(
            ZONE_KIND,
            set(ManagedResource.objects.values_list("kind", flat=True)),
        )
        # The records inside it, in the same sweep rather than the next one.
        self.assertEqual(ManagedResource.objects.filter(kind=RECORD_KIND).count(), len(APEX))
        self.assertFalse(find_zone("example.com").adoptable)

    def test_a_record_added_at_the_provider_later_is_taken_on_too(self):
        self._declare_domain()
        record_sweep({RECORD_KIND: {"ok": True, "records": APEX}}, principal=cli_principal())

        later = [*APEX, record("new.example.com", "A", "203.0.113.9", rid="n1")]
        result = record_sweep({RECORD_KIND: {"ok": True, "records": later}}, principal=cli_principal())

        self.assertEqual(len(result["adopted"]), 1)

    @override_settings(SEVERINO_INFRASTRUCTURE_ENABLE_PUBLIC_DNS=False)
    def test_records_are_adopted_even_where_changing_public_dns_is_off(self):
        """The switch stops HQ changing public DNS, not describing it. Adoption
        copies what the provider already publishes, so reconciling the result
        changes nothing. Refusing it would leave every public record listed as
        unadopted on a deployment that only asked not to change them.
        """

        self._declare_domain()

        result = record_sweep({RECORD_KIND: {"ok": True, "records": APEX}}, principal=cli_principal())

        self.assertEqual(len(result["adopted"]), len(APEX))
        self.assertIn(RECORD_KIND, result["recorded"])
        self.assertEqual(len(find_zone("example.com").records), len(APEX))

    @override_settings(SEVERINO_INFRASTRUCTURE_ENABLE_PUBLIC_DNS=False)
    def test_a_record_authored_by_hand_is_still_refused(self):
        """What the switch is actually for. A typed record asserts something
        the provider does not already hold, which is the change it guards."""

        from hq.platform.application.infrastructure import (
            ManagedResourceCommand,
            PolicyError,
            save_managed_resource,
        )

        with self.assertRaises(PolicyError):
            save_managed_resource(
                ManagedResourceCommand(
                    key="typed-by-hand",
                    kind=RECORD_KIND,
                    spec={key: value for key, value in APEX[0].items() if key != "record_id"},
                    enabled=True,
                ),
                principal=cli_principal(),
            )


@override_settings(SEVERINO_INFRASTRUCTURE_ENABLE_PUBLIC_DNS=True)
class StopManagingDomainTests(TestCase):
    """Ending a responsibility is not the same act as deleting a thing.

    Removal assumes a declaration describes something HQ made at a provider,
    correctly for a rewrite, a proxy host and a DNS record: forgetting the row
    alone would abandon them. HQ did not create the zone, and deleting it would
    be absurd, so stopping management forgets the declaration and leaves the
    zone alone.
    """

    def setUp(self):
        managing_everything(*DNS_CONNECTIONS)
        sweep()
        self.principal = cli_principal()
        self.zone = ManagedResource.objects.create(
            key="example-com",
            kind=ZONE_KIND,
            spec={"zone": "example.com", "connection_ref": "cf-example"},
            enabled=True,
        )
        record_sweep({RECORD_KIND: {"ok": True, "records": APEX}}, principal=self.principal)

    def _remove(self):
        from ..resource_operations import OperationCommand, request_removal

        return request_removal(
            OperationCommand(idempotency_key="forget-1", reason="no longer mine"),
            principal=self.principal,
            current_key="example-com",
        )

    def test_a_domain_is_forgotten_with_its_records_and_nothing_is_queued(self):
        """Left behind, record declarations would keep HQ writing to a domain
        the operator had just said was not its business; there is nothing at
        the provider for a controller to do."""

        result = self._remove()

        self.assertEqual(result["forgotten"], "example-com")
        self.assertFalse(ManagedResource.objects.filter(key="example-com").exists())
        self.assertEqual(len(result["released"]), len(APEX))
        self.assertFalse(ManagedResource.objects.filter(kind=RECORD_KIND).exists())
        self.assertFalse(OperationRequest.objects.exists())

    def test_the_records_themselves_are_untouched(self):
        """Stepping back changes nothing about the zone. Every record is still
        published, and the page still shows them: now as unmanaged."""

        self._remove()

        zone = find_zone("example.com")
        self.assertEqual(len(zone.records), len(APEX))
        self.assertFalse(zone.managed)

    def test_a_dns_record_still_deletes_at_the_provider(self):
        """The distinction has to hold in both directions: forgetting a record
        declaration would abandon a live record with nothing pointing at it."""

        from ..resource_operations import OperationCommand, request_removal

        key = ManagedResource.objects.filter(kind=RECORD_KIND).first().key
        request_removal(
            OperationCommand(idempotency_key="delete-1"),
            principal=self.principal,
            current_key=key,
        )

        self.assertTrue(OperationRequest.objects.filter(action=OperationRequest.Action.DELETE).exists())
        self.assertTrue(ManagedResource.objects.filter(key=key).exists())


class PublishAServiceTests(TestCase):
    """Starting from a name, which is the only thing HQ cannot work out.

    The name is typed once and seeds every resource the service needs.
    """

    def setUp(self):
        user = get_user_model().objects.create_user("op", password="x" * 20)
        self.client.force_login(user)

    def test_a_name_with_nothing_behind_it_has_a_page(self):
        response = self.client.get(reverse("control_plane:service", kwargs={"hostname": "new.example.com"}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "new.example.com")

    def test_it_offers_every_facet_seeded_with_the_name(self):
        response = self.client.get(reverse("control_plane:service", kwargs={"hostname": "new.example.com"}))

        offered = {facet.id for facet in response.context["missing_facets"]}
        self.assertLessEqual({"dns", "proxy"}, offered)
        self.assertContains(response, "hostname=new.example.com")

    def test_nothing_declared_is_not_reported_as_healthy(self):
        """A service with no parts does not read "Wired": that would be the
        most confident statement on a page about something that does not exist.
        """

        service = prospects(("new.example.com",))[0]

        self.assertEqual(service.status, "unknown")
        self.assertEqual(service.status_label, "Nothing set up in HQ")

    def test_a_wildcard_covering_a_name_does_not_make_it_declared(self):
        """A certificate answers for a name without anyone having declared it.

        "This name has TLS" is true, and is not the same as "this name works".
        """

        ManagedResource.objects.create(
            key="wildcard",
            kind="tls.certificate",
            spec={
                "certificate_name": "wildcard",
                "domains": ["*.example.com"],
                "install_on": ["edge"],
            },
            enabled=True,
        )

        service = prospects(("new.example.com",))[0]

        certificate = next(f for f in service.facets if f.id == "certificate")
        self.assertTrue(certificate.present)
        self.assertEqual(service.status_label, "Nothing set up in HQ")

    def test_the_hostname_is_all_it_asks_for(self):
        response = self.client.post(
            reverse("control_plane:service_start"),
            {"hostname": "New.Example.com."},
        )

        self.assertRedirects(
            response,
            reverse("control_plane:service", kwargs={"hostname": "new.example.com"}),
        )

    def test_a_name_that_is_not_a_name_is_refused(self):
        response = self.client.post(reverse("control_plane:service_start"), {"hostname": "not a hostname"})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.context["form"].errors["hostname"], ["Enter a hostname, like app.example.com."])

    def test_saving_returns_to_the_service_being_built(self):
        """Landing on each resource's own page made the next step a navigation
        problem. The service page is the thing being assembled."""

        from hq.platform.application.adoption_testing import managing_everything

        managing_everything()
        response = self.client.post(
            reverse("control_plane:create") + "?kind=adguard.rewrite&hostname=new.example.com",
            {
                "kind": "adguard.rewrite",
                "domain": "new.example.com",
                "answer": "10.0.0.7",
                **form_keyed(),
            },
        )

        self.assertRedirects(
            response,
            reverse("control_plane:service", kwargs={"hostname": "new.example.com"}),
        )


class ExternallyServedNameTests(TestCase):
    """A public record pointing straight at something *is* the routing.

    A name served by Cloudflare Pages has no proxy and never will.
    """

    def _record(self, key, name, rtype, content):
        ManagedResource.objects.create(
            key=key,
            kind=RECORD_KIND,
            spec={
                "zone": "example.com",
                "name": name,
                "record_type": rtype,
                "content": content,
                "proxied": False,
                "ttl": 1,
            },
            enabled=True,
        )

    def test_a_cname_declares_where_the_name_is_served(self):
        self._record("apex", "example.com", "CNAME", "example.pages.dev")

        origin = prospects(("example.com",))[0].origin

        self.assertIsNotNone(origin)
        self.assertEqual(origin.address, "example.pages.dev")

    def test_it_is_named_as_outside_the_network_not_as_unknown(self):
        """Same missing lookup, very different meaning. An ingress pointing at
        an address no host claims is something HQ cannot describe and probably
        should; a CNAME to a Pages site is simply not HQ's to manage."""

        self._record("apex", "example.com", "CNAME", "example.pages.dev")

        origin = prospects(("example.com",))[0].origin

        self.assertFalse(origin.known)
        self.assertTrue(origin.external)

    def test_a_record_that_routes_nothing_declares_no_origin(self):
        self._record("policy", "example.com", "TXT", '"v=spf1 -all"')

        self.assertIsNone(prospects(("example.com",))[0].origin)

    def test_an_internal_proxy_origin_is_not_called_external(self):
        ManagedResource.objects.create(
            key="proxy",
            kind="npm.proxy_host",
            spec={
                "domain_names": ["app.example.com"],
                "forward_scheme": "http",
                "forward_host": "10.0.0.5",
                "forward_port": 8080,
                "ssl_forced": True,
                "http2_support": True,
                "allow_websocket_upgrade": False,
                "caching_enabled": False,
                "block_exploits": True,
                "access_list_id": 0,
                "advanced_config": "",
                "hsts_enabled": False,
                "hsts_subdomains": False,
                "trust_forwarded_proto": False,
                "certificate_resource": "",
                "enabled": True,
            },
            enabled=True,
        )

        origin = prospects(("app.example.com",))[0].origin

        self.assertFalse(origin.external)


class WhatCountsAsAServiceTests(TestCase):
    """A hostname is not a service just because a record type says so. Two
    modeling errors, rather than bad data, can fill the board with rows that
    are not services.
    """

    def _record(self, key, name, rtype, content):
        ManagedResource.objects.create(
            key=key,
            kind=RECORD_KIND,
            spec={
                "zone": "example.com",
                "name": name,
                "record_type": rtype,
                "content": content,
                "proxied": False,
                "ttl": 1,
            },
            enabled=True,
        )

    def test_an_underscore_label_is_never_a_service_and_a_cname_elsewhere_is_one(self):
        """RFC 8552 reserves them for metadata about a domain, not hosts in it.
        The record type cannot tell: excluding TXT because it carries policy
        covers _dmarc and misses sig1._domainkey, a DKIM delegation published
        as a CNAME, where the type says "an address, and therefore a service"
        while the name says it is a signing key.
        """

        self._record("dkim", "sig1._domainkey.example.com", "CNAME", "sig1.dkim.example.com.at.icloudmailadmin.com")
        self._record("site", "example.com", "CNAME", "example.pages.dev")
        self._record("docs", "docs.example.com", "CNAME", "elsewhere.example.net")

        names = {service.hostname for service in service_catalog()}

        self.assertEqual(names, {"example.com", "docs.example.com"})

    def test_a_cname_to_another_service_is_an_alias_of_it(self):
        """One site, not two. Listed separately, www would appear on the board
        with its own health, its own certificate and its own "not routed".
        """

        self._record("site", "example.com", "CNAME", "example.pages.dev")
        self._record("www", "www.example.com", "CNAME", "example.com")

        catalog = {service.hostname: service for service in service_catalog()}

        self.assertEqual(set(catalog), {"example.com"})
        self.assertEqual(catalog["example.com"].aliases, ("www.example.com",))

    def test_an_alias_still_reaches_the_service_it_points_at(self):
        self._record("site", "example.com", "CNAME", "example.pages.dev")
        self._record("www", "www.example.com", "CNAME", "example.com")

        # Asked for by its alias, the page is still the service's.
        self.assertEqual(prospects(("www.example.com",))[0].aliases, ())
        self.assertEqual(prospects(("example.com",))[0].aliases, ("www.example.com",))


class ExternallyAnsweredFacetTests(TestCase):
    """A facet that cannot apply is not a facet that is missing.

    A Pages site is never offered an NPM proxy in front of it.
    """

    def setUp(self):
        ManagedResource.objects.create(
            key="site",
            kind=RECORD_KIND,
            spec={
                "zone": "example.com",
                "name": "example.com",
                "record_type": "CNAME",
                "content": "example.pages.dev",
                "proxied": False,
                "ttl": 1,
            },
            enabled=True,
        )
        user = get_user_model().objects.create_user("op", password="x" * 20)
        self.client.force_login(user)

    def test_a_routing_facet_is_not_reported_missing_when_the_name_leaves_the_network(self):
        response = self.client.get(reverse("control_plane:service", kwargs={"hostname": "example.com"}))
        ingress = next(f for f in prospects(("example.com",))[0].facets if f.id == "proxy")

        # A working arrangement, not a gap, and said once. Every facet that
        # routes takes this branch, so a sentence in the card would be printed
        # once per card, and again in the origin note. Not needed is not
        # missing: no gap is listed for the routing part.
        self.assertNotIn("proxy", [facet.id for facet in response.context["missing_facets"]])
        # Asserted on the facet rather than on the page, because the
        # certificate facet says the same sentence for its own good reason.
        self.assertFalse(ingress.present)
        self.assertTrue(ingress.routes)

    def test_an_internally_served_name_still_asks_for_its_ingress(self):
        """The excuse is external routing, not any missing origin."""

        ManagedResource.objects.create(
            key="rewrite",
            kind="adguard.rewrite",
            spec={"domain": "app.example.com", "answer": "10.0.0.9"},
            enabled=True,
        )

        response = self.client.get(reverse("control_plane:service", kwargs={"hostname": "app.example.com"}))

        self.assertIn("proxy", [facet.id for facet in response.context["missing_facets"]])


class ProxyDecisionTests(TestCase):
    """Whether Cloudflare answers for a name is a question, not a knob. Folded
    into Options with the TTL, the decision that determines whether the address
    is published at all would be made silently by a default.
    """

    def test_it_is_asked_rather_than_folded_away(self):
        from ..provider_forms import spec_form_class

        form = spec_form_class(RECORD_KIND)()

        self.assertIn("proxied", [f.name for f in form.primary])
        self.assertNotIn("proxied", [f.name for f in form.advanced])

    def test_it_still_defaults_to_off(self):
        from hq.domains.control_plane.providers import validate_spec

        spec = validate_spec(
            RECORD_KIND,
            {
                "zone": "example.com",
                "name": "app.example.com",
                "record_type": "A",
                "content": "203.0.113.1",
            },
        )

        self.assertFalse(spec["proxied"])

    def test_routine_tuning_is_still_folded_away(self):
        from ..provider_forms import spec_form_class

        folded = [f.name for f in spec_form_class(RECORD_KIND)().advanced]

        self.assertEqual(set(folded), {"priority", "ttl"})
