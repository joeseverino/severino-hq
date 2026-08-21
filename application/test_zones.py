"""Public DNS: identity, the shape of each record type, and the zone view.

The property under test throughout is that a zone holds many records for one
name. Every provider before this one held exactly one, so "the same hostname"
and "the same record" meant the same thing everywhere -- and a zone apex with
three TXT records, four CAA records and two MX records is the case that makes
them different. Getting that wrong does not fail loudly: adoption silently keeps
one record of nine, and a reconciliation edits whichever the provider happened
to return first.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from control_plane.models import (
    ManagedResource,
    OperationRequest,
    ProviderInventory,
)
from control_plane.providers import (
    DNS_RECORD_TYPES,
    DNS_RECORD_TYPES_BY_ID,
    PROVIDERS,
    validate_spec,
)

from .infrastructure import PolicyError, save_managed_resource, suggest_key
from .inventory import (
    AdoptCommand,
    adopt,
    unmanaged,
    unmanaged_services,
)
from .sweep import record_sweep
from .security import cli_principal
from .services import service_catalog, service_or_prospect
from .zones import (
    adopt_zone_records,
    find_zone,
    unreachable_zones,
    zone_catalog,
)

# A ceiling, not a measurement. Raised deliberately when a page genuinely
# needs another read; tripped accidentally when a property starts querying
# per row.
DOMAIN_PAGE_QUERY_BUDGET = 20

ZONE_KIND = "cloudflare.zone"
RECORD_KIND = "cloudflare.dns_record"


def record(name, rtype, content, *, zone="example.com", rid="x", **extra):
    return {
        "zone": zone,
        "record_id": rid,
        "name": name,
        "record_type": rtype,
        "content": content,
        "priority": extra.get("priority"),
        "proxied": extra.get("proxied", False),
        "ttl": extra.get("ttl", 1),
    }


# One name, nine records. This is the fixture the whole module exists for.
APEX = [
    record("example.com", "CNAME", "example.pages.dev", rid="r1", proxied=True),
    record("example.com", "MX", "mx01.mail.example.net", rid="r2", priority=10),
    record("example.com", "MX", "mx02.mail.example.net", rid="r3", priority=20),
    record("example.com", "TXT", '"v=spf1 include:example.net -all"', rid="r4"),
    record("example.com", "TXT", '"apple-domain=A1B2C3D4"', rid="r5"),
    record("example.com", "TXT", '"openai-domain-verification=dv-x"', rid="r6"),
    record("example.com", "CAA", '0 issue "letsencrypt.org"', rid="r7"),
    record("example.com", "CAA", '0 issuewild "letsencrypt.org"', rid="r8"),
    record("example.com", "CAA", '0 iodef "mailto:security@example.com"', rid="r9"),
]
ZONES = [{"zone": "example.com", "connection_ref": "cf-example"}]


def sweep(zones=ZONES, records=APEX):
    now = timezone.now()
    for kind, rows in ((ZONE_KIND, zones), (RECORD_KIND, records)):
        ProviderInventory.objects.update_or_create(
            kind=kind,
            defaults={
                "records": rows,
                "reachable": True,
                "error": "",
                "observed_at": now,
                "controller_id": "test",
            },
        )


class RecordTypeRegistryTests(TestCase):
    def test_every_declared_type_is_in_the_annotation(self):
        """The guard that stops the registry and its Literal drifting apart.

        Importing the module already raises if they disagree, so this asserts
        the invariant rather than the mechanism -- and fails informatively if
        someone replaces the import-time check with something laxer.
        """

        spec_type = PROVIDERS[RECORD_KIND].spec_type
        allowed = spec_type.model_fields["record_type"].annotation
        self.assertEqual(
            set(DNS_RECORD_TYPES_BY_ID), set(allowed.__args__)
        )

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

    def test_mx_requires_a_priority(self):
        with self.assertRaises(Exception) as caught:
            validate_spec(RECORD_KIND, {
                "zone": "example.com", "name": "example.com",
                "record_type": "MX", "content": "mx.example.net",
            })
        self.assertIn("priority", str(caught.exception))

    def test_priority_is_refused_on_types_that_have_none(self):
        with self.assertRaises(Exception):
            validate_spec(RECORD_KIND, {
                "zone": "example.com", "name": "app.example.com",
                "record_type": "A", "content": "203.0.113.1", "priority": 10,
            })

    def test_cloudflare_will_not_proxy_a_txt_record(self):
        with self.assertRaises(Exception):
            validate_spec(RECORD_KIND, {
                "zone": "example.com", "name": "example.com",
                "record_type": "TXT", "content": '"hello"', "proxied": True,
            })

    def test_a_proxied_record_must_leave_ttl_automatic(self):
        # Cloudflare drives the TTL of a proxied record itself and reports 1 for
        # it regardless. Storing anything else reports drift forever against a
        # value the provider will never agree to.
        with self.assertRaises(Exception):
            validate_spec(RECORD_KIND, {
                "zone": "example.com", "name": "app.example.com",
                "record_type": "A", "content": "203.0.113.1",
                "proxied": True, "ttl": 300,
            })

    def test_caa_value_must_be_well_formed(self):
        with self.assertRaises(Exception):
            validate_spec(RECORD_KIND, {
                "zone": "example.com", "name": "example.com",
                "record_type": "CAA", "content": "letsencrypt.org",
            })
        validate_spec(RECORD_KIND, {
            "zone": "example.com", "name": "example.com",
            "record_type": "CAA", "content": '0 issue "letsencrypt.org"',
        })


class IdentityTests(TestCase):
    def test_nine_records_on_one_name_are_nine_things(self):
        provider = PROVIDERS[RECORD_KIND]
        identities = {
            provider.identity(provider.from_record(row)) for row in APEX
        }
        self.assertEqual(len(identities), len(APEX))

    def test_policy_records_declare_no_service(self):
        provider = PROVIDERS[RECORD_KIND]
        for row in APEX:
            spec = provider.from_record(row)
            with self.subTest(f"{row['record_type']} {row['content'][:20]}"):
                declares = bool(provider.hostnames(spec))
                self.assertEqual(
                    declares, row["record_type"] in {"A", "AAAA", "CNAME"}
                )

    def test_a_txt_value_compares_equal_whether_or_not_it_was_quoted(self):
        """Cloudflare returns TXT quoted whichever way it was sent.

        Without normalising, a record typed without quotes reports as drifted
        against itself forever -- HQ sends `v=spf1 ...`, Cloudflare answers
        `"v=spf1 ..."`, and neither is wrong.
        """

        provider = PROVIDERS[RECORD_KIND]
        bare = provider.identity({
            "zone": "example.com", "name": "example.com",
            "record_type": "TXT", "content": "v=spf1 -all",
        })
        quoted = provider.identity({
            "zone": "example.com", "name": "example.com",
            "record_type": "TXT", "content": '"v=spf1 -all"',
        })
        self.assertEqual(bare, quoted)

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
        sweep()

    def test_records_that_serve_nothing_are_still_adoptable(self):
        """The regression that motivated separating identity from hostnames.

        Identity used to be the hostname, and a record with no hostname reported
        as having no identity -- so every TXT, MX and CAA record in every zone
        was invisible to the one screen built to find unmanaged things.
        """

        found = [item for item in unmanaged() if item.kind == RECORD_KIND]
        self.assertEqual(len(found), len(APEX))

    def test_they_do_not_appear_as_services(self):
        # A DMARC policy is not a service, and filing one under a service whose
        # name is the empty string is how it used to look.
        hostnames = {service.hostname for service in unmanaged_services()}
        self.assertNotIn("", hostnames)
        self.assertEqual(hostnames, {"example.com"})

    def test_a_record_is_adopted_by_identity_not_by_name(self):
        principal = cli_principal()
        target = next(
            item for item in unmanaged()
            if item.kind == RECORD_KIND
            and item.spec["record_type"] == "MX"
            and item.spec["priority"] == 20
        )
        adopt(AdoptCommand(kind=RECORD_KIND, token=target.token), principal=principal)
        stored = ManagedResource.objects.get(kind=RECORD_KIND)
        self.assertEqual(stored.spec["content"], "mx02.mail.example.net")
        self.assertEqual(stored.spec["priority"], 20)


@override_settings(SEVERINO_INFRASTRUCTURE_ENABLE_PUBLIC_DNS=True)
class ZoneViewTests(TestCase):
    def setUp(self):
        sweep(
            records=APEX + [
                record("_acme-challenge.example.net", "TXT", '"leftover-one"',
                       zone="example.net", rid="s1"),
                record("_acme-challenge.example.net", "TXT", '"leftover-two"',
                       zone="example.net", rid="s2"),
                record("example.net", "A", "203.0.113.9", zone="example.net", rid="s3"),
            ],
            zones=ZONES + [{"zone": "example.net", "connection_ref": "cf-example"}],
        )

    def test_a_zone_is_derived_not_stored(self):
        # Nothing has been declared, so every zone here comes from the sweep.
        self.assertEqual(ManagedResource.objects.count(), 0)
        self.assertEqual({zone.zone for zone in zone_catalog()},
                         {"example.com", "example.net"})

    def test_the_apex_sorts_above_its_own_subdomains(self):
        names = [r.name for r in find_zone("example.net").records]
        self.assertEqual(names[0], "example.net")

    def test_a_zone_with_no_caa_says_any_authority_may_issue(self):
        cards = {i.label: i for i in find_zone("example.net").insights}
        self.assertIn("any authority may issue", cards["Certificates"].detail)
        self.assertFalse(cards["Certificates"].concern)

    def test_left_over_challenge_records_are_the_one_thing_flagged(self):
        """The single judgement this page makes without a declared policy.

        A missing CAA record and a permissive DMARC policy are choices, and HQ
        holds no credential that could change either. A challenge record that
        outlived its issuance is garbage by its own definition.
        """

        cards = {i.label: i for i in find_zone("example.net").insights}
        self.assertEqual(cards["Left-over ACME challenges"].value, "2 left behind")
        self.assertTrue(cards["Left-over ACME challenges"].concern)
        self.assertFalse(any(
            card.concern for label, card in cards.items()
            if label != "Left-over ACME challenges"
        ))

    def test_a_healthy_zone_flags_nothing(self):
        self.assertFalse(any(card.concern for card in find_zone("example.com").insights))

    def test_mail_names_the_host_that_receives_it(self):
        cards = {i.label: i for i in find_zone("example.com").insights}
        self.assertEqual(cards["Email"].value, "example.net")

    def test_adopting_a_whole_zone_is_all_or_nothing(self):
        result = adopt_zone_records("example.com", principal=cli_principal())
        self.assertEqual(len(result["adopted"]), len(APEX))
        self.assertEqual(
            ManagedResource.objects.filter(kind=RECORD_KIND).count(), len(APEX)
        )
        self.assertEqual(len(set(result["adopted"])), len(APEX))


class PublicDNSPolicyTests(TestCase):
    """The switch that governs changing public DNS, and what it should govern.

    It exists because public DNS is the one surface where a mistake is
    immediately everybody's problem. It is not a reason to refuse a declaration
    that no controller can act on.
    """

    def setUp(self):
        self.principal = cli_principal()

    def _save(self, kind, spec, key):
        from .infrastructure import ManagedResourceCommand

        return save_managed_resource(
            ManagedResourceCommand(key=key, kind=kind, spec=spec, enabled=True),
            principal=self.principal,
        )

    @override_settings(SEVERINO_INFRASTRUCTURE_ENABLE_PUBLIC_DNS=False)
    def test_a_domain_can_be_declared_while_changing_dns_is_switched_off(self):
        # A domain declaration records what HQ is responsible for. Its reconcile
        # is locked, so there is nothing for the switch to protect against, and
        # refusing it stopped an operator saying what HQ owns while preventing
        # no change to anything.
        self._save(ZONE_KIND, {"zone": "example.com", "connection_ref": "cf"}, "d")
        self.assertTrue(ManagedResource.objects.filter(key="d").exists())

    @override_settings(SEVERINO_INFRASTRUCTURE_ENABLE_PUBLIC_DNS=False)
    def test_a_record_is_still_refused_while_it_is_switched_off(self):
        with self.assertRaises(PolicyError):
            self._save(RECORD_KIND, {
                "zone": "example.com", "name": "app.example.com",
                "record_type": "A", "content": "203.0.113.1",
            }, "r")

    @override_settings(SEVERINO_INFRASTRUCTURE_ENABLE_PUBLIC_DNS=True)
    def test_a_record_is_allowed_once_it_is_switched_on(self):
        self._save(RECORD_KIND, {
            "zone": "example.com", "name": "app.example.com",
            "record_type": "A", "content": "203.0.113.1",
        }, "r")
        self.assertTrue(ManagedResource.objects.filter(key="r").exists())


class ZonePageTests(TestCase):
    def setUp(self):
        sweep()
        user = get_user_model().objects.create_user("op", password="x" * 20)
        self.client.force_login(user)

    def test_the_index_goes_straight_to_a_managed_domain(self):
        ManagedResource.objects.create(
            key="example-com", kind=ZONE_KIND,
            spec={"zone": "example.com", "connection_ref": "cf-example"},
            enabled=True,
        )
        response = self.client.get(reverse("zones:index"))
        self.assertRedirects(
            response, reverse("zones:detail", kwargs={"zone": "example.com"})
        )

    def test_the_index_offers_what_is_there_when_nothing_is_managed(self):
        response = self.client.get(reverse("zones:index"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "example.com")

    def test_a_domain_page_lists_every_record_in_it(self):
        response = self.client.get(
            reverse("zones:detail", kwargs={"zone": "example.com"})
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "mx02.mail.example.net")
        self.assertContains(response, "letsencrypt.org")

    def test_an_unknown_domain_is_a_404_not_an_empty_page(self):
        response = self.client.get(
            reverse("zones:detail", kwargs={"zone": "nope.example"})
        )
        self.assertEqual(response.status_code, 404)


class ProviderSurfaceTests(TestCase):
    def test_a_record_is_not_offered_by_the_generic_add_page(self):
        """It needs a zone, and the page it belongs on has already named one.

        Declared on the provider rather than excluded in the view, so the picker
        never grows a hand-maintained list of kinds to leave out.
        """

        self.assertEqual(PROVIDERS[RECORD_KIND].created_from, "zone")
        self.assertEqual(PROVIDERS[ZONE_KIND].created_from, "")

    def test_a_locked_kind_does_not_promise_to_apply_anything(self):
        """The form told every operator their resource would be applied.

        True of most kinds and false of any whose actions are locked -- which is
        exactly the case where knowing that saving changes nothing matters most.
        The capability registry already said so; the page just was not asking.
        """

        from control_plane.views import _apply_note

        self.assertNotIn("applies this at the provider", _apply_note(ZONE_KIND))
        self.assertIn("Zone Settings", _apply_note(ZONE_KIND))
        self.assertIn("applies this at the provider", _apply_note(RECORD_KIND))

    def test_a_zone_declares_no_service_facet(self):
        # A zone is a namespace, not a name that answers. Given a facet it would
        # put every managed domain on the services board with nothing behind it.
        self.assertEqual(PROVIDERS[ZONE_KIND].facet, "")
        self.assertIsNone(PROVIDERS[ZONE_KIND].hostnames)


@override_settings(SEVERINO_INFRASTRUCTURE_ENABLE_PUBLIC_DNS=True)
class ZoneInsightTests(TestCase):
    """What the domain page says, and why any of it is worth saying.

    The cards it replaced restated DNS records back at the operator -- true,
    already visible in Cloudflare's own dashboard, and no reason to have built
    this. These join the zone to what HQ holds elsewhere.
    """

    def _certificate(self, key="wildcard", domains=("example.com", "*.example.com"),
                     kind="tls.certificate", not_after="2099-01-01T00:00:00+00:00"):
        return ManagedResource.objects.create(
            key=key,
            kind=kind,
            spec={
                "certificate_name": key,
                "domains": list(domains),
                "install_on": ["edge"],
            },
            status={"not_after": not_after},
            enabled=True,
        )

    def _insight(self, zone_name, label):
        zone = find_zone(zone_name)
        return next((i for i in zone.insights if i.label == label), None)

    def test_a_certificate_covering_the_zone_is_named_and_linked(self):
        sweep()
        self._certificate()

        card = self._insight("example.com", "Certificates")

        self.assertEqual(card.value, "wildcard")
        self.assertTrue(card.url)
        self.assertFalse(card.concern)
        # The expiry and nothing else. Listing what it covered and confirming
        # CAA allowed it took four lines to say "this is fine", which a card
        # says better by being short.
        self.assertTrue(card.detail.startswith("Expires"))

    def test_caa_that_forbids_the_renewing_authority_is_flagged(self):
        """The failure this page exists to catch.

        A CAA record naming who may issue is a security control, and it becomes
        an outage the day it excludes the authority renewing the certificate
        already serving the domain. Cloudflare does not know which certificates
        HQ renews; the certificate registry does not know what the zone
        permits. Only the join sees it, and it fails silently until expiry.
        """

        sweep(records=[
            record("example.com", "CAA", '0 issue "digicert.com"', rid="c1"),
            record("example.com", "A", "203.0.113.1", rid="a1"),
        ])
        self._certificate()

        card = self._insight("example.com", "Certificates")

        self.assertTrue(card.concern)
        self.assertIn("digicert.com", card.detail)
        self.assertIn("will be refused", card.detail)

    def test_caa_that_permits_the_renewing_authority_is_not_flagged(self):
        sweep(records=[
            record("example.com", "CAA", '0 issue "letsencrypt.org"', rid="c1"),
            record("example.com", "A", "203.0.113.1", rid="a1"),
        ])
        self._certificate()

        card = self._insight("example.com", "Certificates")

        self.assertFalse(card.concern)

    def test_an_uploaded_certificate_is_not_cross_checked_against_caa(self):
        """HQ did not choose who signed it, so it cannot predict a renewal.

        Flagging one would assert a fact HQ does not have -- and the operator
        who uploaded it renews it themselves, by hand, from wherever it came.
        """

        sweep(records=[
            record("example.com", "CAA", '0 issue "digicert.com"', rid="c1"),
            record("example.com", "A", "203.0.113.1", rid="a1"),
        ])
        self._certificate(kind="tls.uploaded_certificate")

        card = self._insight("example.com", "Certificates")

        self.assertFalse(card.concern)

    def test_a_certificate_card_does_not_recite_other_domains(self):
        """A wildcard covering four domains explained itself with another one.

        Listing coverage at all made the card the tallest thing on the page,
        and on joeseverino.com it opened with three jseverino.com names. What a
        certificate covers belongs on the certificate.
        """

        sweep()
        self._certificate(domains=("other.example", "*.other.example", "example.com"))

        card = self._insight("example.com", "Certificates")

        self.assertNotIn("other.example", card.detail)

    def test_a_zone_with_no_managed_certificate_says_what_may_issue(self):
        sweep(records=[record("example.com", "CAA", '0 issue "letsencrypt.org"', rid="c1")])

        card = self._insight("example.com", "Certificates")

        self.assertEqual(card.value, "None managed here")
        self.assertIn("letsencrypt.org", card.detail)

    def test_email_names_who_receives_the_mail(self):
        """"2 mail servers" was true and useless.

        The count of MX records is a redundancy detail; the question is who has
        the mailbox, and the records already say -- both point at example.net.
        """

        sweep(records=APEX + [
            record("_dmarc.example.com", "TXT", '"v=DMARC1; p=reject"', rid="d1"),
        ])
        card = self._insight("example.com", "Email")

        self.assertEqual(card.value, "example.net")
        self.assertIn("SPF is published.", card.detail)
        self.assertIn("DMARC rejects forgeries.", card.detail)

    def test_a_recognised_mail_host_is_named_the_way_people_say_it(self):
        sweep(records=[
            record("example.com", "MX", "mx01.mail.icloud.com", rid="m1", priority=10),
            record("example.com", "MX", "mx02.mail.icloud.com", rid="m2", priority=10),
        ])

        self.assertEqual(self._insight("example.com", "Email").value, "iCloud")

    def test_mail_split_across_providers_is_not_summarised_into_one(self):
        sweep(records=[
            record("example.com", "MX", "mx01.mail.icloud.com", rid="m1", priority=10),
            record("example.com", "MX", "mx.other.example", rid="m2", priority=20),
        ])

        self.assertEqual(self._insight("example.com", "Email").value, "2 mail servers")

    def test_a_domain_with_no_mail_records_says_it_is_forgeable(self):
        sweep(records=[record("example.com", "A", "203.0.113.1", rid="a1")])

        card = self._insight("example.com", "Email")

        self.assertEqual(card.value, "Not configured")
        self.assertIn("claims to come from it", card.detail)

    def test_iodef_is_not_counted_as_an_issuing_authority(self):
        """It names where to report a violation, not who may issue.

        Counted as an issuer, a domain looks restricted to an email address --
        and a certificate HQ renews would be flagged as doomed when it is fine.
        """

        sweep(records=[
            record("example.com", "CAA", '0 issue "letsencrypt.org"', rid="c1"),
            record("example.com", "CAA", '0 iodef "mailto:sec@example.com"', rid="c2"),
            record("example.com", "A", "203.0.113.1", rid="a1"),
        ])
        self._certificate()

        card = self._insight("example.com", "Certificates")

        self.assertFalse(card.concern)

    def test_one_failing_contributor_does_not_lose_the_page(self):
        """This is the screen opened to find out what is wrong."""

        sweep()
        with mock.patch(
            "application.zone_insights.certificates", side_effect=RuntimeError("boom")
        ):
            insights = find_zone("example.com").insights

        labels = {i.label for i in insights}
        self.assertNotIn("Certificates", labels)
        self.assertIn("Email", labels)


@override_settings(SEVERINO_INFRASTRUCTURE_ENABLE_PUBLIC_DNS=True)
class EphemeralRecordTests(TestCase):
    """There is one owner of a declared domain, and it is HQ.

    An ACME challenge is not another system's record. HQ's own controller makes
    it, and HQ's own issuance deletes it seconds later. What separates it from
    the rest is not who made it but how long it is meant to last: working
    material inside one operation, rather than desired state HQ holds to.
    Declaring one would have HQ recreating it the moment issuance was finished
    with it -- HQ fighting itself.
    """

    def setUp(self):
        sweep(records=[
            record("example.com", "A", "203.0.113.1", rid="a1"),
            record("_acme-challenge.example.com", "TXT", '"token-one"', rid="s1"),
            record("_acme-challenge.example.com", "TXT", '"token-two"', rid="s2"),
        ])
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

        card = next(
            i for i in self.zone.insights
            if i.label == "Left-over ACME challenges"
        )
        self.assertEqual(card.value, "2 left behind")
        self.assertTrue(card.concern)


@override_settings(SEVERINO_INFRASTRUCTURE_ENABLE_PUBLIC_DNS=True)
class ServicesInsightTests(TestCase):
    """A count that opens onto the list, for a domain with more than a card holds."""

    def setUp(self):
        sweep()
        for index in range(3):
            ManagedResource.objects.create(
                key=f"host-{index}",
                kind="adguard.rewrite",
                spec={"domain": f"h{index}.example.com", "answer": "10.0.0.1"},
                enabled=True,
            )

    def _card(self):
        return next(
            i for i in find_zone("example.com").insights if i.label == "Services"
        )

    def test_the_value_is_a_count(self):
        self.assertEqual(self._card().value, "3 services")

    def test_every_service_in_the_domain_is_carried_for_the_dialog(self):
        titles = {row.title for row in self._card().rows}
        self.assertEqual(
            titles, {"h0.example.com", "h1.example.com", "h2.example.com"}
        )

    def test_the_link_still_reaches_a_page_that_does_the_same_job(self):
        """The dialog is an enhancement. Without a real href behind it, a card
        that summarises thirty services into a number loses them entirely the
        moment the dialog does not open."""

        self.assertEqual(self._card().url, reverse("control_plane:services"))

    def test_services_in_another_domain_are_not_counted(self):
        ManagedResource.objects.create(
            key="elsewhere",
            kind="adguard.rewrite",
            spec={"domain": "app.other.example", "answer": "10.0.0.2"},
            enabled=True,
        )

        self.assertEqual(self._card().value, "3 services")


@override_settings(SEVERINO_INFRASTRUCTURE_ENABLE_PUBLIC_DNS=True)
class SelfClosingAdoptionTests(TestCase):
    """"Not adopted yet" is not a state anyone should have to clear.

    Declaring a domain is the decision, and it is made once. Asking again per
    record put a question on the page whose answer was always yes, and reported
    outstanding work nobody intended to do.
    """

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

    def test_a_domain_hq_was_not_made_responsible_for_is_left_alone(self):
        """Seeing a zone is not being asked to manage it."""

        result = record_sweep(
            {
                ZONE_KIND: {"ok": True, "records": ZONES},
                RECORD_KIND: {"ok": True, "records": APEX},
            },
            principal=cli_principal(),
        )

        self.assertEqual(result["adopted"], [])
        self.assertEqual(ManagedResource.objects.count(), 0)

    def test_a_record_added_at_the_provider_later_is_taken_on_too(self):
        self._declare_domain()
        record_sweep(
            {RECORD_KIND: {"ok": True, "records": APEX}}, principal=cli_principal()
        )

        later = APEX + [record("new.example.com", "A", "203.0.113.9", rid="n1")]
        result = record_sweep(
            {RECORD_KIND: {"ok": True, "records": later}}, principal=cli_principal()
        )

        self.assertEqual(len(result["adopted"]), 1)

    @override_settings(SEVERINO_INFRASTRUCTURE_ENABLE_PUBLIC_DNS=False)
    def test_a_refused_adoption_does_not_lose_the_sweep(self):
        """Recording what a provider holds must not depend on being allowed to
        declare it. Losing the whole inventory because one record could not be
        adopted is a far worse trade."""

        self._declare_domain()

        result = record_sweep(
            {RECORD_KIND: {"ok": True, "records": APEX}}, principal=cli_principal()
        )

        self.assertEqual(result["adopted"], [])
        self.assertIn(RECORD_KIND, result["recorded"])
        self.assertEqual(len(find_zone("example.com").records), len(APEX))


class ServiceFacetOfferTests(TestCase):
    """What a service page offers when a facet is missing.

    It offered the provider's identifier -- "Add cloudflare.dns_record" -- which
    names the provider correctly and the offer not at all. Every provider
    already carries the sentence it should have used.
    """

    def test_a_missing_facet_is_offered_by_name_not_by_identifier(self):
        from .services import Facet

        offers = dict(Facet(id="dns", label="DNS").declarable)

        # Reads mid-sentence -- the page renders "Add public DNS record" --
        # with only the first letter lowered, so the acronym survives. Lowering
        # the whole label produced "add public dns record".
        self.assertIn(RECORD_KIND, offers)
        self.assertEqual(offers[RECORD_KIND], "public DNS record")
        self.assertEqual(offers["adguard.rewrite"], "internal DNS record")

    def test_a_certificate_can_be_started_from_a_name_nothing_covers(self):
        """The old rule -- chosen from what exists, never created here -- was
        written when every certificate predated HQ owning them. It stops being
        true the first time a domain arrives that no wildcard covers.

        Only ever rendered for a facet nothing supplies, so a name already
        covered is never invited to grow a certificate of its own.
        """

        from .services import Facet

        offers = dict(Facet(id="certificate", label="Certificate").declarable)

        self.assertEqual(offers["tls.certificate"], "TLS certificate")

    def test_an_uploaded_certificate_is_offered_beside_the_issued_one(self):
        """Both ways of getting a certificate, offered where one is needed.

        It was excluded because it needs material only the operator has -- true,
        and no longer a reason: the form that creates one collects the file on
        the same page. Left out, the only certificate offered for a `.homelab`
        name was the one Let's Encrypt cannot issue, and the option that works
        was reachable only by knowing to go and find it in the registry.
        """

        from .services import Facet

        offers = dict(Facet(id="certificate", label="Certificate").declarable)

        self.assertIn("tls.certificate", offers)
        self.assertIn("tls.uploaded_certificate", offers)


@override_settings(SEVERINO_INFRASTRUCTURE_ENABLE_PUBLIC_DNS=True)
class UnrepresentableRecordTests(TestCase):
    """Cloudflare holds record types HQ's model does not describe.

    SRV, NS, PTR, SVCB and more. HQ deliberately models the six it can act on,
    and the rest still exist in the zone. What must not happen is a sweep that
    fails because of one of them: recording what a provider holds cannot depend
    on being able to declare all of it, and adoption now runs inside that same
    sweep.
    """

    def setUp(self):
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

    def test_one_unrepresentable_record_does_not_fail_the_sweep(self):
        result = record_sweep(
            {
                ZONE_KIND: {"ok": True, "records": ZONES},
                RECORD_KIND: {"ok": True, "records": self.records},
            },
            principal=cli_principal(),
        )

        self.assertIn(RECORD_KIND, result["recorded"])

    def test_the_records_it_can_express_are_still_taken_on(self):
        """One record HQ cannot model must not cost it the whole zone."""

        result = record_sweep(
            {
                ZONE_KIND: {"ok": True, "records": ZONES},
                RECORD_KIND: {"ok": True, "records": self.records},
            },
            principal=cli_principal(),
        )

        self.assertEqual(len(result["adopted"]), 1)
        declared = ManagedResource.objects.filter(kind=RECORD_KIND)
        self.assertEqual([r.spec["record_type"] for r in declared], ["A"])

    def test_they_are_still_visible_rather_than_silently_dropped(self):
        """A record HQ cannot manage is still a record in the zone.

        Hidden, the page would claim to show what a domain publishes while
        quietly omitting part of it -- the one thing this surface must never do.
        """

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

    def test_they_are_not_reported_as_outstanding_work(self):
        """They will never be adopted, so counting them means the page reports
        work that can never be finished."""

        record_sweep(
            {
                ZONE_KIND: {"ok": True, "records": ZONES},
                RECORD_KIND: {"ok": True, "records": self.records},
            },
            principal=cli_principal(),
        )

        self.assertEqual(find_zone("example.com").adoptable, ())


@override_settings(SEVERINO_INFRASTRUCTURE_ENABLE_PUBLIC_DNS=True)
class StopManagingDomainTests(TestCase):
    """Ending a responsibility is not the same act as deleting a thing.

    Removal assumes a declaration describes something HQ made at a provider,
    correctly for a rewrite, a proxy host and a DNS record: forgetting the row
    alone would abandon them. HQ did not create the zone, and deleting it would
    be absurd -- so removal was refused outright, and there was no way to stop
    managing a domain at all.
    """

    def setUp(self):
        sweep()
        self.principal = cli_principal()
        self.zone = ManagedResource.objects.create(
            key="example-com",
            kind=ZONE_KIND,
            spec={"zone": "example.com", "connection_ref": "cf-example"},
            enabled=True,
        )
        record_sweep(
            {RECORD_KIND: {"ok": True, "records": APEX}}, principal=self.principal
        )

    def _remove(self):
        from .infrastructure import OperationCommand, request_removal

        return request_removal(
            OperationCommand(idempotency_key="forget-1", reason="no longer mine"),
            principal=self.principal,
            current_key="example-com",
        )

    def test_a_domain_can_be_stopped_being_managed(self):
        result = self._remove()

        self.assertEqual(result["forgotten"], "example-com")
        self.assertFalse(ManagedResource.objects.filter(key="example-com").exists())

    def test_nothing_is_queued_for_a_controller(self):
        """There is nothing at the provider for it to do."""

        self._remove()

        self.assertFalse(OperationRequest.objects.exists())

    def test_the_record_declarations_inside_it_are_released_too(self):
        """Left behind, HQ would keep writing to a domain the operator had just
        said was not its business."""

        result = self._remove()

        self.assertEqual(len(result["released"]), len(APEX))
        self.assertFalse(ManagedResource.objects.filter(kind=RECORD_KIND).exists())

    def test_the_records_themselves_are_untouched(self):
        """Stepping back changes nothing about the zone. Every record is still
        published, and the page still shows them -- now as unmanaged."""

        self._remove()

        zone = find_zone("example.com")
        self.assertEqual(len(zone.records), len(APEX))
        self.assertFalse(zone.managed)

    def test_a_dns_record_still_deletes_at_the_provider(self):
        """The distinction has to hold in both directions: forgetting a record
        declaration would abandon a live record with nothing pointing at it."""

        from .infrastructure import OperationCommand, request_removal

        key = ManagedResource.objects.filter(kind=RECORD_KIND).first().key
        request_removal(
            OperationCommand(idempotency_key="delete-1"),
            principal=self.principal,
            current_key=key,
        )

        self.assertTrue(
            OperationRequest.objects.filter(
                action=OperationRequest.Action.DELETE
            ).exists()
        )
        self.assertTrue(ManagedResource.objects.filter(key=key).exists())


@override_settings(SEVERINO_INFRASTRUCTURE_ENABLE_PUBLIC_DNS=True)
class DomainPageCostTests(TestCase):
    """The page must not cost more as a zone grows.

    A domain view is built from three reads -- the declarations, the last sweep,
    and the unmanaged diff between them -- and then sliced. Anything that scales
    with the number of records means a per-row query hiding in a property, which
    is invisible until a zone has two hundred records in it.
    """

    def _zone_with(self, count):
        sweep(records=[
            record(f"h{i}.example.com", "A", "203.0.113.1", rid=f"r{i}")
            for i in range(count)
        ])
        ManagedResource.objects.get_or_create(
            key="example-com",
            kind=ZONE_KIND,
            defaults={
                "spec": {"zone": "example.com", "connection_ref": "cf-example"},
                "enabled": True,
            },
        )

    def _queries(self, count):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        self._zone_with(count)
        user = get_user_model().objects.create_user(f"op{count}", password="x" * 20)
        self.client.force_login(user)
        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(
                reverse("zones:detail", kwargs={"zone": "example.com"})
            )
        self.assertEqual(response.status_code, 200)
        return len(captured)

    def test_the_page_does_not_cost_more_per_record(self):
        small = self._queries(5)
        ManagedResource.objects.filter(kind=RECORD_KIND).delete()
        large = self._queries(60)

        # Counts only, never the SQL: this repository is public and a failure
        # message carrying a query would put an extension's schema in a log.
        self.assertLessEqual(
            large,
            small,
            f"the domain page scales with record count ({small} then {large})",
        )

    def test_the_catalogue_is_built_once_per_request(self):
        """Built to find one domain and again for the switcher, the page paid
        twice for two identical answers."""

        self.assertLessEqual(
            self._queries(10),
            DOMAIN_PAGE_QUERY_BUDGET,
            "the domain page exceeded its query budget",
        )


class ResourceDetailIsProviderDeclaredTests(TestCase):
    """Every kind gets a detail card, including ones added after this page.

    It carried a hand-written card per kind, reaching into ``spec.forward_host``
    and ``spec.answer`` -- the one thing nothing outside a provider may do. A
    provider added later got no card, because writing one was a step nobody was
    reminded to take.
    """

    def setUp(self):
        user = get_user_model().objects.create_user("op", password="x" * 20)
        self.client.force_login(user)

    def _detail(self, key, kind, spec, status=None):
        ManagedResource.objects.create(
            key=key, kind=kind, spec=spec, status=status or {}, enabled=True
        )
        return self.client.get(reverse("control_plane:detail", kwargs={"key": key}))

    def test_a_provider_added_later_still_describes_itself(self):
        response = self._detail(
            "a-record",
            RECORD_KIND,
            {
                "zone": "example.com", "name": "app.example.com",
                "record_type": "A", "content": "203.0.113.1",
                "proxied": False, "ttl": 1,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Public DNS record")
        self.assertContains(response, "203.0.113.1")

    def test_a_domain_describes_itself_too(self):
        response = self._detail(
            "a-domain", ZONE_KIND,
            {"zone": "example.com", "connection_ref": "cf-example"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "cf-example")

    def test_the_kinds_that_had_hand_written_cards_still_read_the_same(self):
        response = self._detail(
            "a-rewrite", "adguard.rewrite",
            {"domain": "app.example.com", "answer": "10.0.0.10"},
            status={"answer": "10.0.0.10"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "10.0.0.10")
        self.assertContains(response, "Internal DNS record")

    def test_drift_is_shown_against_what_was_declared(self):
        """The readout carries desired and observed together precisely so a
        page can say they disagree."""

        response = self._detail(
            "drifted", "adguard.rewrite",
            {"domain": "app.example.com", "answer": "10.0.0.10"},
            status={"answer": "10.0.0.99"},
        )

        self.assertContains(response, "10.0.0.99")
        self.assertContains(response, "declared 10.0.0.10")


class ResourcePageAfterWalkthroughTests(TestCase):
    """Fixes for what driving the UI actually surfaced.

    Every case here is something that only showed up by adding a service,
    looking at it, and deleting it — not by reading the code.
    """

    def setUp(self):
        user = get_user_model().objects.create_user("op", password="x" * 20)
        self.client.force_login(user)
        self.proxy = ManagedResource.objects.create(
            key="private-proxy",
            kind="npm.proxy_host",
            spec={
                "domain_names": ["private.example.com"],
                "forward_scheme": "http", "forward_host": "10.0.0.5",
                "forward_port": 8081, "ssl_forced": True, "http2_support": True,
                "allow_websocket_upgrade": False, "caching_enabled": False,
                "block_exploits": True, "access_list_id": 0,
                "advanced_config": "", "hsts_enabled": False,
                "hsts_subdomains": False, "trust_forwarded_proto": False,
                "certificate_resource": "", "enabled": True,
            },
            enabled=True,
        )

    def test_a_resource_names_the_hostname_it_serves(self):
        """It identified a record by the key HQ invented and nothing else, so
        the hostname appeared only inside a collapsed disclosure."""

        response = self.client.get(
            reverse("control_plane:detail", kwargs={"key": "private-proxy"})
        )

        self.assertContains(response, "private.example.com")
        self.assertContains(
            response,
            reverse(
                "control_plane:service",
                kwargs={"hostname": "private.example.com"},
            ),
        )

    def test_a_list_field_is_not_shown_as_a_python_repr(self):
        """The last thing before a destructive action read
        ``['private.jseverino.com']`` — brackets, quotes and all."""

        response = self.client.get(
            reverse("control_plane:remove", kwargs={"key": "private-proxy"})
        )

        self.assertContains(response, "private.example.com")
        self.assertNotContains(response, "[&#x27;private.example.com&#x27;]")

    def test_routine_settings_are_folded_away_before_a_deletion(self):
        """Fifteen rows of mostly defaults buried the four that say what this
        is. The provider already declares which of its fields are routine."""

        response = self.client.get(
            reverse("control_plane:remove", kwargs={"key": "private-proxy"})
        )

        self.assertContains(response, "Its other settings")

    def test_an_unset_optional_is_not_shown_as_none(self):
        ManagedResource.objects.create(
            key="a-cname", kind=RECORD_KIND,
            spec={
                "zone": "example.com", "name": "public.example.com",
                "record_type": "CNAME", "content": "example.pages.dev",
                "priority": None, "proxied": False, "ttl": 1,
            },
            enabled=True,
        )

        response = self.client.get(
            reverse("control_plane:remove", kwargs={"key": "a-cname"})
        )

        self.assertNotContains(response, "<code>None</code>")

    def test_every_provider_says_what_removing_it_costs(self):
        """Only DNS record types declared this, so deleting a proxy host —
        which takes a service offline — asked "are you sure" about a table of
        fields and said nothing about the consequence."""

        for kind in ("npm.proxy_host", "adguard.rewrite", RECORD_KIND):
            with self.subTest(kind=kind):
                self.assertIsNotNone(PROVIDERS[kind].removal_note)


class PublishAServiceTests(TestCase):
    """Starting from a name, which is the only thing HQ cannot work out.

    Publishing used to begin at the resource picker: choose a kind of thing,
    type a hostname, save, and only then does a page exist that knows what the
    name still needs -- so the second resource meant typing the name again.
    """

    def setUp(self):
        user = get_user_model().objects.create_user("op", password="x" * 20)
        self.client.force_login(user)

    def test_a_name_with_nothing_behind_it_has_a_page(self):
        response = self.client.get(
            reverse("control_plane:service", kwargs={"hostname": "new.example.com"})
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "new.example.com")

    def test_it_offers_every_facet_seeded_with_the_name(self):
        response = self.client.get(
            reverse("control_plane:service", kwargs={"hostname": "new.example.com"})
        )

        self.assertContains(response, "Add internal DNS record")
        self.assertContains(response, "Add proxy host")
        self.assertContains(response, "hostname=new.example.com")

    def test_nothing_declared_is_not_reported_as_healthy(self):
        """A service with no parts read "Wired" -- the most confident statement
        on a page about something that did not exist."""

        service = service_or_prospect("new.example.com")

        self.assertEqual(service.status, "unknown")
        self.assertEqual(service.status_label, "Nothing declared")

    def test_a_wildcard_covering_a_name_does_not_make_it_declared(self):
        """A certificate answers for a name without anyone having declared it.

        "This name has TLS" is true, and is not the same as "this name works".
        """

        ManagedResource.objects.create(
            key="wildcard", kind="tls.certificate",
            spec={
                "certificate_name": "wildcard",
                "domains": ["*.example.com"],
                "install_on": ["edge"],
            },
            enabled=True,
        )

        service = service_or_prospect("new.example.com")

        certificate = next(f for f in service.facets if f.id == "certificate")
        self.assertTrue(certificate.present)
        self.assertEqual(service.status_label, "Nothing declared")

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
        response = self.client.post(
            reverse("control_plane:service_start"), {"hostname": "not a hostname"}
        )

        self.assertRedirects(response, reverse("control_plane:service_start"))

    def test_saving_returns_to_the_service_being_built(self):
        """Landing on each resource's own page made the next step a navigation
        problem. The service page is the thing being assembled."""

        response = self.client.post(
            reverse("control_plane:create") + "?kind=adguard.rewrite&hostname=new.example.com",
            {
                "kind": "adguard.rewrite",
                "domain": "new.example.com",
                "answer": "10.0.0.7",
            },
        )

        self.assertRedirects(
            response,
            reverse("control_plane:service", kwargs={"hostname": "new.example.com"}),
        )


class ExternallyServedNameTests(TestCase):
    """A public record pointing straight at something *is* the routing.

    The page reported "Not routed. Nothing declares where requests for this name
    are served" about a name whose entire configuration was a statement of
    exactly that -- because only a proxy host declared an origin, and a name
    served by Cloudflare Pages has no proxy and never will.
    """

    def _record(self, key, name, rtype, content):
        ManagedResource.objects.create(
            key=key, kind=RECORD_KIND,
            spec={
                "zone": "example.com", "name": name, "record_type": rtype,
                "content": content, "proxied": False, "ttl": 1,
            },
            enabled=True,
        )

    def test_a_cname_declares_where_the_name_is_served(self):
        self._record("apex", "example.com", "CNAME", "example.pages.dev")

        origin = service_or_prospect("example.com").origin

        self.assertIsNotNone(origin)
        self.assertEqual(origin.address, "example.pages.dev")

    def test_it_is_named_as_outside_the_network_not_as_unknown(self):
        """Same missing lookup, very different meaning. An ingress pointing at
        an address no host claims is something HQ cannot describe and probably
        should; a CNAME to a Pages site is simply not HQ's to manage."""

        self._record("apex", "example.com", "CNAME", "example.pages.dev")

        origin = service_or_prospect("example.com").origin

        self.assertFalse(origin.known)
        self.assertTrue(origin.external)

    def test_a_record_that_routes_nothing_declares_no_origin(self):
        self._record("policy", "example.com", "TXT", '"v=spf1 -all"')

        self.assertIsNone(service_or_prospect("example.com").origin)

    def test_an_internal_proxy_origin_is_not_called_external(self):
        ManagedResource.objects.create(
            key="proxy", kind="npm.proxy_host",
            spec={
                "domain_names": ["app.example.com"],
                "forward_scheme": "http", "forward_host": "10.0.0.5",
                "forward_port": 8080, "ssl_forced": True, "http2_support": True,
                "allow_websocket_upgrade": False, "caching_enabled": False,
                "block_exploits": True, "access_list_id": 0,
                "advanced_config": "", "hsts_enabled": False,
                "hsts_subdomains": False, "trust_forwarded_proto": False,
                "certificate_resource": "", "enabled": True,
            },
            enabled=True,
        )

        origin = service_or_prospect("app.example.com").origin

        self.assertFalse(origin.external)


class CertificateEditFormTests(TestCase):
    """Editing a certificate showed an empty "define a new one" form.

    A certificate that exists only as a topology reference carries that
    reference in an advanced field, so the page opened on blank boxes with the
    one field describing it folded away -- it appeared to say the certificate
    had no configuration at all.
    """

    def _form(self, **initial):
        from .provider_forms import spec_form_class

        return spec_form_class("tls.certificate", lock_identity=True)(initial=initial)

    def test_editing_does_not_ask_which_certificate_this_is(self):
        """The question has an answer already, and no second answer is valid.

        Offered on the edit form, the selector let an existing certificate be
        told it was "a new certificate defined below" -- with the fields that
        would define one folded out of sight, so the option named inputs the
        page did not have. Which certificate this is belongs above the form,
        as a fact.
        """

        form = self._form(topology_ref="pki:wildcard", renewal_window_days=30)
        offered = [f.name for f in form.primary] + [f.name for f in form.advanced]

        self.assertNotIn("topology_ref", offered)
        self.assertIn("renewal_window_days", offered)

    def test_a_field_still_at_its_default_stays_folded_away(self):
        """Nobody arrives at a certificate to adjust how early it renews.

        HQ renews on its own, so the window is a knob rather than a question,
        and it holds the answer the model would have given anyway.
        """

        form = self._form(topology_ref="pki:wildcard", renewal_window_days=30)

        self.assertIn("renewal_window_days", [f.name for f in form.advanced])

    def test_a_default_somebody_changed_comes_out_of_the_disclosure(self):
        form = self._form(topology_ref="pki:wildcard", renewal_window_days=45)

        self.assertIn("renewal_window_days", [f.name for f in form.primary])

    def test_adding_one_does_not_offer_the_reference_at_all(self):
        """Creating asks for a new certificate, and only for that.

        The reference names a certificate that already exists, so it answers a
        different question than "add one". Behind a disclosure it was still
        offered -- an empty box for a thing that cannot be created by naming
        something already there -- so it is not among the fields at all.
        """

        form = self._form()
        offered = [f.name for f in form.primary] + [f.name for f in form.advanced]

        self.assertNotIn("topology_ref", offered)
        self.assertIn("certificate_name", offered)


class WhatCountsAsAServiceTests(TestCase):
    """A hostname is not a service just because a record type says so.

    Two ways the board filled up with rows that were not services, both of them
    modelling errors rather than bad data.
    """

    def _record(self, key, name, rtype, content):
        ManagedResource.objects.create(
            key=key, kind=RECORD_KIND,
            spec={
                "zone": "example.com", "name": name, "record_type": rtype,
                "content": content, "proxied": False, "ttl": 1,
            },
            enabled=True,
        )

    def test_an_underscore_label_is_never_a_service(self):
        """RFC 8552 reserves them for metadata about a domain, not hosts in it.

        The record type could not tell: TXT was excluded because it carries
        policy, which caught _dmarc and missed sig1._domainkey -- a DKIM
        delegation published as a CNAME, so the type said "an address, and
        therefore a service" while the name says it is a signing key.
        """

        self._record("dkim", "sig1._domainkey.example.com", "CNAME",
                     "sig1.dkim.example.com.at.icloudmailadmin.com")
        self._record("site", "example.com", "CNAME", "example.pages.dev")

        names = {service.hostname for service in service_catalog()}

        self.assertEqual(names, {"example.com"})

    def test_a_cname_to_another_service_is_an_alias_of_it(self):
        """One site, not two. Listed separately, www appeared on the board with
        its own health, its own certificate and its own "not routed"."""

        self._record("site", "example.com", "CNAME", "example.pages.dev")
        self._record("www", "www.example.com", "CNAME", "example.com")

        catalog = {service.hostname: service for service in service_catalog()}

        self.assertEqual(set(catalog), {"example.com"})
        self.assertEqual(catalog["example.com"].aliases, ("www.example.com",))

    def test_a_cname_to_somewhere_else_is_its_own_service(self):
        """A name HQ publishes and does not otherwise know about is a service
        by every definition that matters here."""

        self._record("site", "example.com", "CNAME", "example.pages.dev")
        self._record("docs", "docs.example.com", "CNAME", "elsewhere.example.net")

        names = {service.hostname for service in service_catalog()}

        self.assertEqual(names, {"example.com", "docs.example.com"})

    def test_an_alias_still_reaches_the_service_it_points_at(self):
        self._record("site", "example.com", "CNAME", "example.pages.dev")
        self._record("www", "www.example.com", "CNAME", "example.com")

        # Asked for by its alias, the page is still the service's.
        self.assertEqual(
            service_or_prospect("www.example.com").aliases, ()
        )
        self.assertEqual(
            service_or_prospect("example.com").aliases, ("www.example.com",)
        )


class KnownHostTests(TestCase):
    """Who runs a name, read off the name, in one table both cards share."""

    def test_a_recognised_operator_is_named_the_way_people_say_it(self):
        from .known_hosts import operator

        self.assertEqual(operator("jseverino.pages.dev"), "Cloudflare Pages")
        self.assertEqual(operator("mx01.mail.icloud.com"), "iCloud")
        self.assertEqual(operator("node.example.ts.net"), "Tailscale")

    def test_an_unknown_host_falls_back_to_its_domain(self):
        from .known_hosts import operator

        self.assertEqual(operator("srv1.someplace.example"), "someplace.example")

    def test_an_address_is_not_chopped_into_a_domain(self):
        """The last two labels of 198.51.100.72 is "100.72" -- not a domain,
        not an address, and nothing anyone could act on."""

        from .known_hosts import operator

        self.assertEqual(operator("198.51.100.72"), "198.51.100.72")


class AliasRecordPlacementTests(TestCase):
    """Where an alias's own declaration belongs.

    Twice wrong before it was right. Dropped, the CNAME that makes www work
    appeared on no service page at all -- a real resource, still reconciled,
    invisible. Merged into the target's facets, the two CNAMEs read as two
    records competing for one name: HQ raised "only one of them can be the
    answer" and called a working site Incomplete.
    """

    def _record(self, key, name, content):
        ManagedResource.objects.create(
            key=key, kind=RECORD_KIND,
            spec={
                "zone": "example.com", "name": name, "record_type": "CNAME",
                "content": content, "proxied": False, "ttl": 1,
            },
            enabled=True,
        )

    def setUp(self):
        self._record("site", "example.com", "example.pages.dev")
        self._record("www", "www.example.com", "example.com")
        self.service = service_or_prospect("example.com")

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

    def test_it_is_labelled_with_the_name_it_serves(self):
        alias, _ = self.service.alias_claims[0]

        self.assertEqual(alias, "www.example.com")

    def test_it_appears_on_the_page_it_belongs_to(self):
        user = get_user_model().objects.create_user("op", password="x" * 20)
        self.client.force_login(user)

        response = self.client.get(
            reverse("control_plane:service", kwargs={"hostname": "example.com"})
        )

        self.assertContains(response, "Records for www.example.com")
        self.assertContains(response, "www.example.com")


class ExternallyAnsweredFacetTests(TestCase):
    """A facet that cannot apply is not a facet that is missing.

    The page said "Nothing supplies this for jseverino.com" in one card while
    the card beside it named Cloudflare Pages as what serves it -- and offered
    to add an NPM proxy in front of a Pages site, which must not have one.
    """

    def setUp(self):
        ManagedResource.objects.create(
            key="site", kind=RECORD_KIND,
            spec={
                "zone": "example.com", "name": "example.com",
                "record_type": "CNAME", "content": "example.pages.dev",
                "proxied": False, "ttl": 1,
            },
            enabled=True,
        )
        user = get_user_model().objects.create_user("op", password="x" * 20)
        self.client.force_login(user)

    def test_a_routing_facet_is_not_reported_missing_when_the_name_leaves_the_network(self):
        response = self.client.get(
            reverse("control_plane:service", kwargs={"hostname": "example.com"})
        )
        ingress = next(
            f for f in service_or_prospect("example.com").facets if f.id == "proxy"
        )

        # A working arrangement, not a gap -- and said once. Every facet that
        # routes takes this branch, so a sentence in the card is printed once
        # per card: Runtime and Ingress sat side by side reading the same line,
        # with the origin note under them saying it a third time.
        self.assertContains(response, "Not needed")
        self.assertContains(response, "Served by")
        self.assertEqual(response.content.count(b"Served by"), 1)
        # Asserted on the facet rather than on the page, because the
        # certificate facet says the same sentence for its own good reason.
        self.assertFalse(ingress.present)
        self.assertTrue(ingress.routes)

    def test_it_does_not_offer_a_proxy_in_front_of_something_it_must_not(self):
        response = self.client.get(
            reverse("control_plane:service", kwargs={"hostname": "example.com"})
        )

        self.assertNotContains(response, "Add proxy host")

    def test_an_internally_served_name_still_asks_for_its_ingress(self):
        """The excuse is external routing, not any missing origin."""

        ManagedResource.objects.create(
            key="rewrite", kind="adguard.rewrite",
            spec={"domain": "app.example.com", "answer": "10.0.0.9"},
            enabled=True,
        )

        response = self.client.get(
            reverse("control_plane:service", kwargs={"hostname": "app.example.com"})
        )

        self.assertContains(response, "Add proxy host")


class NewVerbReadinessTests(TestCase):
    """What adding a controller verb costs, now that the credential will grow.

    Cache purge, key rotation and zone-setting writes are all verbs HQ does not
    have yet and will. Each used to mean another view class identical to the two
    that already existed but for one word.
    """

    def test_one_view_serves_every_verb(self):
        from control_plane import views

        self.assertFalse(hasattr(views, "ReconcileView"))
        self.assertFalse(hasattr(views, "RenewCertificateView"))
        self.assertTrue(hasattr(views, "OperationView"))

    def test_every_verb_the_model_allows_has_a_phrase(self):
        """A verb without one would be announced by its identifier."""

        from control_plane.views import OPERATION_PHRASE

        self.assertEqual(
            set(OPERATION_PHRASE), set(OperationRequest.Action.values)
        )

    def test_the_routes_carry_the_verb_rather_than_the_class(self):
        user = get_user_model().objects.create_user("op", password="x" * 20)
        self.client.force_login(user)
        ManagedResource.objects.create(
            key="a-rewrite", kind="adguard.rewrite",
            spec={"domain": "app.example.com", "answer": "10.0.0.1"},
            enabled=True, generation=1,
        )

        response = self.client.post(
            reverse("control_plane:reconcile", kwargs={"key": "a-rewrite"})
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            OperationRequest.objects.filter(
                action=OperationRequest.Action.RECONCILE
            ).exists()
        )


class PendingRemovalTests(TestCase):
    """A resource with a deletion in flight is on its way out.

    Shown unchanged, the page invited an operator to edit something about to
    stop existing, and to queue a convergence racing the deletion already
    waiting for the same controller.
    """

    def setUp(self):
        user = get_user_model().objects.create_user("op", password="x" * 20)
        self.client.force_login(user)
        self.resource = ManagedResource.objects.create(
            key="going", kind="adguard.rewrite",
            spec={"domain": "app.example.com", "answer": "10.0.0.1"},
            enabled=True, generation=1,
        )

    def _page(self):
        return self.client.get(
            reverse("control_plane:detail", kwargs={"key": "going"})
        )

    def test_it_offers_the_usual_actions_while_nothing_is_pending(self):
        response = self._page()

        self.assertContains(response, "Reconcile")
        self.assertNotContains(response, "Removal in progress")

    def test_a_queued_removal_withdraws_them(self):
        from .infrastructure import OperationCommand, request_removal

        request_removal(
            OperationCommand(idempotency_key="r1"),
            principal=cli_principal(),
            current_key="going",
        )

        response = self._page()

        self.assertContains(response, "Removal in progress")
        self.assertNotContains(response, ">Reconcile<")

    def test_the_report_is_still_reachable(self):
        """The one thing still worth doing: reading what it was."""

        from .infrastructure import OperationCommand, request_removal

        request_removal(
            OperationCommand(idempotency_key="r1"),
            principal=cli_principal(),
            current_key="going",
        )

        self.assertContains(self._page(), "Download report")


class ProxyDecisionTests(TestCase):
    """Whether Cloudflare answers for a name is a question, not a knob.

    Folded into Options with the TTL, the decision that determines whether the
    address is published at all was made silently by a default.
    """

    def test_it_is_asked_rather_than_folded_away(self):
        from .provider_forms import spec_form_class

        form = spec_form_class(RECORD_KIND)()

        self.assertIn("proxied", [f.name for f in form.primary])
        self.assertNotIn("proxied", [f.name for f in form.advanced])

    def test_it_still_defaults_to_off(self):
        from control_plane.providers import validate_spec

        spec = validate_spec(RECORD_KIND, {
            "zone": "example.com", "name": "app.example.com",
            "record_type": "A", "content": "203.0.113.1",
        })

        self.assertFalse(spec["proxied"])

    def test_routine_tuning_is_still_folded_away(self):
        from .provider_forms import spec_form_class

        folded = [f.name for f in spec_form_class(RECORD_KIND)().advanced]

        self.assertEqual(set(folded), {"priority", "ttl"})


class ResourceListReadabilityTests(TestCase):
    """The registry has to survive a zone's worth of records in it.

    Keys alone said "jseverino-com-caa-2" twenty times over -- names HQ
    invented, each describing nothing -- and a domain, which has no controller
    action at all, sat among them reporting "Pending" and "Never observed"
    forever.
    """

    def setUp(self):
        user = get_user_model().objects.create_user("op", password="x" * 20)
        self.client.force_login(user)
        ManagedResource.objects.create(
            key="example-com-caa", kind=RECORD_KIND,
            spec={
                "zone": "example.com", "name": "example.com",
                "record_type": "CAA", "content": '0 issue "letsencrypt.org"',
                "proxied": False, "ttl": 1,
            },
            enabled=True, generation=1,
        )
        ManagedResource.objects.create(
            key="example-com", kind=ZONE_KIND,
            spec={"zone": "example.com", "connection_ref": "cf-example"},
            enabled=True, generation=1,
        )

    def test_each_row_says_what_it_is(self):
        response = self.client.get(reverse("control_plane:list"))

        self.assertContains(response, 'CAA 0 issue &quot;letsencrypt.org&quot;')

    def test_a_declaration_only_resource_is_not_pending_forever(self):
        """It has no controller action, so nothing is ever coming."""

        response = self.client.get(reverse("control_plane:list"))
        rows = response.content.decode()
        row = rows[rows.index("example-com<"):]
        row = row[: row.index("</tr>")]

        self.assertIn("Declared", row)
        self.assertNotIn("Pending", row)
        self.assertNotIn("Not observed", row)

    def test_a_resource_with_a_controller_still_reports_its_sync(self):
        response = self.client.get(reverse("control_plane:list"))
        rows = response.content.decode()
        row = rows[rows.index("example-com-caa<"):]
        row = row[: row.index("</tr>")]

        self.assertIn("Pending", row)


class LabelAndDensityTests(TestCase):
    """Two things that only show up on a real page with real records in it."""

    def test_an_acronym_survives_being_put_mid_sentence(self):
        """"Add tLS certificate" -- the first letter lowered without looking at
        the word it belonged to."""

        from .services import Facet

        offers = dict(Facet(id="certificate", label="Certificate").declarable)
        dns = dict(Facet(id="dns", label="DNS").declarable)

        self.assertEqual(offers["tls.certificate"], "TLS certificate")
        self.assertEqual(dns["adguard.rewrite"], "internal DNS record")
        self.assertEqual(dns[RECORD_KIND], "public DNS record")

    def test_a_long_value_does_not_take_three_lines_in_a_list(self):
        user = get_user_model().objects.create_user("op", password="x" * 20)
        self.client.force_login(user)
        ManagedResource.objects.create(
            key="dmarc", kind=RECORD_KIND,
            spec={
                "zone": "example.com", "name": "_dmarc.example.com",
                "record_type": "TXT",
                "content": (
                    '"v=DMARC1; p=reject; sp=reject; '
                    'rua=mailto:872342119743452993e40ddb97bc20d0@example.net"'
                ),
                "proxied": False, "ttl": 1,
            },
            enabled=True,
        )

        response = self.client.get(reverse("control_plane:list"))

        self.assertContains(response, "…")
        self.assertNotContains(response, "872342119743452993e40ddb97bc20d0")


class CredentialCoverageTests(TestCase):
    """A domain HQ owns that its credential cannot read is a real gap."""

    def setUp(self):
        for zone in ("example.com", "example.net"):
            ManagedResource.objects.create(
                key=zone.replace(".", "-"), kind=ZONE_KIND, enabled=True,
                spec={"zone": zone, "connection_ref": "cf"},
            )

    def test_a_declared_domain_the_token_cannot_reach_is_named(self):
        self.assertEqual(unreachable_zones(["example.com"]), ("example.net",))

    def test_nothing_is_reported_when_the_token_reaches_them_all(self):
        self.assertEqual(unreachable_zones(["example.com", "example.net"]), ())

    def test_an_empty_report_is_not_read_as_everything_missing(self):
        """A controller that reported no zones has told us nothing, not that
        every domain is gone."""

        self.assertEqual(unreachable_zones([]), ())

    def test_extra_zones_the_credential_can_reach_are_not_a_problem(self):
        self.assertEqual(unreachable_zones(["example.com", "example.net", "spare.test"]), ())
