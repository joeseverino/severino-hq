"""Public DNS: identity, the shape of each record type, and the zone view.

The property under test throughout is that a zone holds many records for one
name, so "the same hostname" and "the same record" are different things: a zone
apex with three TXT records, four CAA records and two MX records is the case
that separates them. Getting that wrong does not fail loudly: adoption silently
keeps one record of nine, and a reconciliation edits whichever the provider
happened to return first."""

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from pydantic import ValidationError

from hq.domains.control_plane.models import ManagedResource
from hq.domains.control_plane.provider_adapters.cloudflare import DNS_RECORD_TYPES, DNS_RECORD_TYPES_BY_ID
from hq.domains.control_plane.providers import PROVIDERS, validate_spec

from ..adoption import AdoptCommand, adopt, unmanaged, unmanaged_services
from ..adoption_testing import managing_everything
from ..infrastructure import PolicyError, save_managed_resource, suggest_key
from ..security import cli_principal
from ..services import prospects
from ..sweep import record_sweep
from ..zones import adopt_zone_records, find_zone, zone_catalog
from .zone_records import APEX, DNS_CONNECTIONS, RECORD_KIND, ZONE_KIND, ZONES, record, sweep


class RecordTypeRegistryTests(TestCase):
    def test_every_declared_type_is_in_the_annotation(self):
        """The guard that stops the registry and its Literal drifting apart.

        Importing the module already raises if they disagree, so this asserts
        the invariant rather than the mechanism, and fails informatively if
        someone replaces the import-time check with something laxer.
        """

        spec_type = PROVIDERS[RECORD_KIND].spec_type
        allowed = spec_type.model_fields["record_type"].annotation
        self.assertEqual(set(DNS_RECORD_TYPES_BY_ID), set(allowed.__args__))

    def test_only_address_records_declare_a_service(self):
        declaring = {t.id for t in DNS_RECORD_TYPES if t.declares_service}
        self.assertEqual(declaring, {"A", "AAAA", "CNAME"})

    def test_every_type_says_what_removing_it_costs(self):
        # Public DNS is destructive in a way an internal rewrite is not, and the
        # removal page is generated from this.
        for record_type in DNS_RECORD_TYPES:
            with self.subTest(record_type.id):
                self.assertTrue(record_type.removal_impact.strip())


class SpecShapeTests(TestCase):
    """Rules Cloudflare enforces anyway, enforced at the form instead.

    Every one of these is a rejection the API would issue a minute later inside
    a job result. Caught here it is a red field next to the answer that caused
    it.
    """

    def test_each_rule_refuses_its_shape_at_the_form(self):
        def spec(**fields):
            return {
                "zone": "example.com",
                "name": "app.example.com",
                "record_type": "A",
                "content": "203.0.113.1",
                **fields,
            }

        refused = {
            "an MX record with no priority": spec(record_type="MX", content="mx.example.net"),
            "a priority on a type that has none": spec(priority=10),
            "a proxied TXT record": spec(record_type="TXT", content='"hello"', proxied=True),
            # Cloudflare drives a proxied record's TTL itself and reports 1 for it.
            "a proxied record with its own TTL": spec(proxied=True, ttl=300),
            "a CAA value that is not one": spec(record_type="CAA", content="letsencrypt.org"),
        }
        for case, fields in refused.items():
            with self.subTest(case), self.assertRaises(ValidationError):
                validate_spec(RECORD_KIND, fields)
        validate_spec(RECORD_KIND, spec(record_type="CAA", content='0 issue "letsencrypt.org"'))


class IdentityTests(TestCase):
    def test_nine_records_on_one_name_are_nine_things(self):
        provider = PROVIDERS[RECORD_KIND]
        identities = {provider.identity(provider.from_record(row)) for row in APEX}
        self.assertEqual(len(identities), len(APEX))

    def test_policy_records_declare_no_service(self):
        provider = PROVIDERS[RECORD_KIND]
        for row in APEX:
            spec = provider.from_record(row)
            with self.subTest(f"{row['record_type']} {row['content'][:20]}"):
                declares = bool(provider.hostnames(spec))
                self.assertEqual(declares, row["record_type"] in {"A", "AAAA", "CNAME"})

    def test_adoption_captures_a_record_exactly(self):
        provider = PROVIDERS[RECORD_KIND]
        for row in APEX:
            with self.subTest(row["record_id"]):
                # Round-trips through validation, so adoption cannot produce a
                # declaration the model would refuse on the next edit.
                validate_spec(RECORD_KIND, provider.from_record(row))

    def test_suggested_keys_distinguish_records_sharing_a_name(self):
        provider = PROVIDERS[RECORD_KIND]
        keys = set()
        for row in APEX:
            key = suggest_key(RECORD_KIND, provider.from_record(row))
            self.assertTrue(key)
            keys.add(key)
        # Three types on one name means three distinct bases; the numeric
        # suffixes that separate same-type siblings are added against the
        # database, which is empty here.
        self.assertEqual(keys, {"example-com-cname", "example-com-mx", "example-com-txt", "example-com-caa"})


@override_settings(SEVERINO_INFRASTRUCTURE_ENABLE_PUBLIC_DNS=True)
class UnmanagedTests(TestCase):
    def setUp(self):
        managing_everything(*DNS_CONNECTIONS)
        sweep()

    def test_records_that_serve_nothing_are_still_adoptable(self):
        """A record with no hostname (TXT, MX, CAA) still has an identity."""

        found = [item for item in unmanaged() if item.kind == RECORD_KIND]
        self.assertEqual(len(found), len(APEX))

    def test_they_do_not_appear_as_services(self):
        # A DMARC policy is not a service, and is never filed under an empty name.
        hostnames = {service.hostname for service in unmanaged_services()}
        self.assertNotIn("", hostnames)
        self.assertEqual(hostnames, {"example.com"})

    def test_a_record_is_adopted_by_identity_not_by_name(self):
        principal = cli_principal()
        target = next(
            item
            for item in unmanaged()
            if item.kind == RECORD_KIND and item.spec["record_type"] == "MX" and item.spec["priority"] == 20
        )
        adopt(AdoptCommand(kind=RECORD_KIND, token=target.token), principal=principal)
        stored = ManagedResource.objects.get(kind=RECORD_KIND)
        self.assertEqual(stored.spec["content"], "mx02.mail.example.net")
        self.assertEqual(stored.spec["priority"], 20)


@override_settings(SEVERINO_INFRASTRUCTURE_ENABLE_PUBLIC_DNS=True)
class ZoneViewTests(TestCase):
    def setUp(self):
        managing_everything(*DNS_CONNECTIONS)
        sweep(
            records=[
                *APEX,
                record("_acme-challenge.example.net", "TXT", '"leftover-one"', zone="example.net", rid="s1"),
                record("_acme-challenge.example.net", "TXT", '"leftover-two"', zone="example.net", rid="s2"),
                record("example.net", "A", "203.0.113.9", zone="example.net", rid="s3"),
            ],
            zones=[*ZONES, {"zone": "example.net", "connection_ref": "cf-example"}],
        )

    def test_a_zone_is_derived_not_stored(self):
        # Nothing has been declared, so every zone here comes from the sweep.
        self.assertEqual(ManagedResource.objects.count(), 0)
        self.assertEqual({zone.zone for zone in zone_catalog()}, {"example.com", "example.net"})

    def test_the_apex_sorts_above_its_own_subdomains(self):
        names = [r.name for r in find_zone("example.net").records]
        self.assertEqual(names[0], "example.net")

    def test_a_zone_with_no_caa_says_any_authority_may_issue(self):
        from ..zone_insights import certificates

        card = certificates(find_zone("example.net"))
        self.assertIn("any authority may issue", card.detail)
        self.assertFalse(card.concern)
        cards = {i.label: i for i in find_zone("example.net").insights}
        self.assertFalse(cards["Security"].concern)

    def test_left_over_challenge_records_are_the_one_thing_flagged(self):
        """The single judgment this page makes without a declared policy.

        A missing CAA record and a permissive DMARC policy are choices, and HQ
        holds no credential that could change either. A challenge record that
        outlived its issuance is garbage by its own definition.
        """

        cards = {i.label: i for i in find_zone("example.net").insights}
        self.assertIn("2", cards["Left-over ACME challenges"].value)
        self.assertTrue(cards["Left-over ACME challenges"].concern)
        self.assertFalse(any(card.concern for label, card in cards.items() if label != "Left-over ACME challenges"))

    def test_a_healthy_zone_flags_nothing(self):
        self.assertFalse(any(card.concern for card in find_zone("example.com").insights))

    def test_mail_names_the_host_that_receives_it(self):
        cards = {i.label: i for i in find_zone("example.com").insights}
        self.assertEqual(cards["Email"].value, "example.net")

    def test_adopting_a_whole_zone_is_all_or_nothing(self):
        result = adopt_zone_records("example.com", principal=cli_principal())
        self.assertEqual(len(result["adopted"]), len(APEX))
        self.assertEqual(ManagedResource.objects.filter(kind=RECORD_KIND).count(), len(APEX))
        self.assertEqual(len(set(result["adopted"])), len(APEX))


class PublicDNSPolicyTests(TestCase):
    """The switch that governs changing public DNS, and what it should govern.

    It exists because public DNS is the one surface where a mistake is
    immediately everybody's problem. It is not a reason to refuse a declaration
    that no controller can act on.
    """

    def setUp(self):
        from hq.platform.application.adoption_testing import managing_everything

        managing_everything()
        self.principal = cli_principal()

    def _save(self, kind, spec, key):
        from ..infrastructure import ManagedResourceCommand

        return save_managed_resource(
            ManagedResourceCommand(key=key, kind=kind, spec=spec, enabled=True),
            principal=self.principal,
        )

    @override_settings(SEVERINO_INFRASTRUCTURE_ENABLE_PUBLIC_DNS=False)
    def test_a_domain_can_be_declared_while_changing_dns_is_switched_off(self):
        # A domain declaration records what HQ is responsible for. Its
        # reconcile is locked, so there is nothing for the switch to protect
        # against, and refusing it would stop an operator saying what HQ owns
        # while preventing no change to anything.
        self._save(ZONE_KIND, {"zone": "example.com", "connection_ref": "cf"}, "d")
        self.assertTrue(ManagedResource.objects.filter(key="d").exists())

    @override_settings(SEVERINO_INFRASTRUCTURE_ENABLE_PUBLIC_DNS=False)
    def test_a_record_is_still_refused_while_it_is_switched_off(self):
        with self.assertRaises(PolicyError):
            self._save(
                RECORD_KIND,
                {
                    "zone": "example.com",
                    "name": "app.example.com",
                    "record_type": "A",
                    "content": "203.0.113.1",
                },
                "r",
            )

    @override_settings(SEVERINO_INFRASTRUCTURE_ENABLE_PUBLIC_DNS=True)
    def test_a_record_is_allowed_once_it_is_switched_on(self):
        self._save(
            RECORD_KIND,
            {
                "zone": "example.com",
                "name": "app.example.com",
                "record_type": "A",
                "content": "203.0.113.1",
            },
            "r",
        )
        self.assertTrue(ManagedResource.objects.filter(key="r").exists())


@override_settings(SEVERINO_INFRASTRUCTURE_ENABLE_PUBLIC_DNS=True)
class EphemeralRecordTests(TestCase):
    """There is one owner of a declared domain, and it is HQ.

    An ACME challenge is not another system's record. HQ's own controller makes
    it, and HQ's own issuance deletes it seconds later. What separates it from
    the rest is not who made it but how long it is meant to last: working
    material inside one operation, rather than desired state HQ holds to.
    Declaring one would have HQ recreating it the moment issuance was finished
    with it: HQ fighting itself.
    """

    def setUp(self):
        managing_everything(*DNS_CONNECTIONS)
        sweep(
            records=[
                record("example.com", "A", "203.0.113.1", rid="a1"),
                record("_acme-challenge.example.com", "TXT", '"token-one"', rid="s1"),
                record("_acme-challenge.example.com", "TXT", '"token-two"', rid="s2"),
            ]
        )
        self.zone = find_zone("example.com")

    def test_working_material_is_not_listed_among_the_records(self):
        listed = {r.name for r in self.zone.listed}
        self.assertEqual(listed, {"example.com"})

    def test_working_material_is_never_offered_for_adoption(self):
        self.assertEqual([r.name for r in self.zone.adoptable], ["example.com"])

    def test_taking_on_a_domain_does_not_take_on_its_working_material(self):
        adopt_zone_records("example.com", principal=cli_principal())

        declared = ManagedResource.objects.filter(kind=RECORD_KIND)
        self.assertEqual([r.spec["name"] for r in declared], ["example.com"])

    def test_but_ones_that_outlived_their_issuance_are_still_reported(self):
        """Invisible is right for a record that lives seconds. A leftover is a
        real problem and keeps its own card."""

        card = next(i for i in self.zone.insights if i.label == "Left-over ACME challenges")
        self.assertIn("2", card.value)
        self.assertTrue(card.concern)


@override_settings(SEVERINO_INFRASTRUCTURE_ENABLE_PUBLIC_DNS=True)
class UnrepresentableRecordTests(TestCase):
    """Cloudflare holds record types HQ's model does not describe.

    SRV, NS, PTR, SVCB and more. HQ deliberately models the six it can act on,
    and the rest still exist in the zone. A sweep must not fail because of one
    of them: recording what a provider holds cannot depend on being able to
    declare all of it, and adoption runs inside that same sweep.
    """

    def setUp(self):
        managing_everything(*DNS_CONNECTIONS)
        ManagedResource.objects.create(
            key="example-com",
            kind=ZONE_KIND,
            spec={"zone": "example.com", "connection_ref": "cf-example"},
            enabled=True,
        )
        self.records = [
            record("example.com", "A", "203.0.113.1", rid="a1"),
            record("_sip._tcp.example.com", "SRV", "10 5 5060 sip.example.com", rid="s1"),
            record("example.com", "NS", "ns1.example.net", rid="n1"),
        ]

    def test_the_sweep_records_and_takes_on_what_it_can_express(self):
        """One record HQ cannot model must not cost it the whole zone."""

        result = record_sweep(
            {
                ZONE_KIND: {"ok": True, "records": ZONES},
                RECORD_KIND: {"ok": True, "records": self.records},
            },
            principal=cli_principal(),
        )

        self.assertIn(RECORD_KIND, result["recorded"])
        self.assertEqual(len(result["adopted"]), 1)
        declared = ManagedResource.objects.filter(kind=RECORD_KIND)
        self.assertEqual([r.spec["record_type"] for r in declared], ["A"])

    def test_they_are_still_visible_and_never_outstanding_work(self):
        """A record HQ cannot manage is still a record in the zone, and will
        never be adopted, so it is not work the page reports."""

        record_sweep(
            {
                ZONE_KIND: {"ok": True, "records": ZONES},
                RECORD_KIND: {"ok": True, "records": self.records},
            },
            principal=cli_principal(),
        )

        listed = {(r.record_type, r.name) for r in find_zone("example.com").listed}
        self.assertIn(("SRV", "_sip._tcp.example.com"), listed)
        self.assertIn(("NS", "example.com"), listed)
        self.assertEqual(find_zone("example.com").adoptable, ())


class KnownHostTests(TestCase):
    """Who runs a name, read off the name, in one table both cards share."""

    def test_a_recognized_operator_is_named_the_way_people_say_it(self):
        from ..known_hosts import operator

        self.assertEqual(operator("example.pages.dev"), "Cloudflare Pages")
        self.assertEqual(operator("mx01.mail.icloud.com"), "iCloud")
        self.assertEqual(operator("node.example.ts.net"), "Tailscale")

    def test_an_unknown_host_falls_back_to_its_domain(self):
        from ..known_hosts import operator

        self.assertEqual(operator("srv1.someplace.example"), "someplace.example")

    def test_an_address_is_not_chopped_into_a_domain(self):
        """The last two labels of 198.51.100.72 is "100.72", not a domain,
        not an address, and nothing anyone could act on."""

        from ..known_hosts import operator

        self.assertEqual(operator("198.51.100.72"), "198.51.100.72")


class AliasRecordPlacementTests(TestCase):
    """Where an alias's own declaration belongs.

    Dropped, the CNAME that makes www work appears on no service page at all: a
    real resource, still reconciled, invisible. Merged into the target's
    facets, the two CNAMEs read as two records competing for one name, and HQ
    calls a working site Incomplete.
    """

    def _record(self, key, name, content):
        ManagedResource.objects.create(
            key=key,
            kind=RECORD_KIND,
            spec={
                "zone": "example.com",
                "name": name,
                "record_type": "CNAME",
                "content": content,
                "proxied": False,
                "ttl": 1,
            },
            enabled=True,
        )

    def setUp(self):
        self._record("site", "example.com", "example.pages.dev")
        self._record("www", "www.example.com", "example.com")
        self.service = prospects(("example.com",))[0]

    def test_an_alias_does_not_invent_a_wiring_fault(self):
        self.assertEqual(self.service.faults, ())
        self.assertNotEqual(self.service.status_label, "Incomplete")

    def test_the_dns_card_keeps_one_answer(self):
        dns = next(f for f in self.service.facets if f.id == "dns")

        self.assertEqual(len(dns.claims), 1)
        self.assertEqual(dns.claims[0].resource_key, "site")

    def test_the_alias_record_is_still_reachable_from_the_service(self):
        keys = {claim.resource_key for _, claim in self.service.alias_claims}

        self.assertEqual(keys, {"www"})

    def test_it_is_labeled_with_the_name_it_serves(self):
        alias, _ = self.service.alias_claims[0]

        self.assertEqual(alias, "www.example.com")

    def test_it_appears_on_the_page_it_belongs_to(self):
        user = get_user_model().objects.create_user("op", password="x" * 20)
        self.client.force_login(user)

        response = self.client.get(reverse("control_plane:service", kwargs={"hostname": "example.com"}))

        self.assertContains(response, reverse("control_plane:detail", args=["www"]))
