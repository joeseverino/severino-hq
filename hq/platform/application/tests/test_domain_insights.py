"""What HQ notices about a domain: its zone, its services, its TLS posture and its registration."""

from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse

from hq.domains.control_plane.models import ManagedResource

from ..zones import find_zone
from .zone_records import APEX, record, sweep


@override_settings(SEVERINO_INFRASTRUCTURE_ENABLE_PUBLIC_DNS=True)
class ZoneInsightTests(TestCase):
    """What the domain page says, and why any of it is worth saying.

    The cards it replaced restated DNS records back at the operator: true,
    already visible in Cloudflare's own dashboard, and no reason to have built
    this. These join the zone to what HQ holds elsewhere.
    """

    def _certificate(
        self,
        key="wildcard",
        domains=("example.com", "*.example.com"),
        kind="tls.certificate",
        not_after="2099-01-01T00:00:00+00:00",
    ):
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
        from ..zone_insights import certificates

        zone = find_zone(zone_name)
        # The certificate half of the Security card, asked directly.
        if label == "Certificates":
            return certificates(zone)
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

        sweep(
            records=[
                record("example.com", "CAA", '0 issue "digicert.com"', rid="c1"),
                record("example.com", "A", "203.0.113.1", rid="a1"),
            ]
        )
        self._certificate()

        card = self._insight("example.com", "Certificates")

        self.assertTrue(card.concern)
        self.assertIn("digicert.com", card.detail)

    def test_caa_that_permits_the_renewing_authority_is_not_flagged_and_iodef_names_no_issuer(self):
        """iodef names where to report a violation, not who may issue: counted
        as an issuer, a certificate HQ renews would be flagged as doomed."""
        sweep(
            records=[
                record("example.com", "CAA", '0 issue "letsencrypt.org"', rid="c1"),
                record("example.com", "CAA", '0 iodef "mailto:sec@example.com"', rid="c2"),
                record("example.com", "A", "203.0.113.1", rid="a1"),
            ]
        )
        self._certificate()

        card = self._insight("example.com", "Certificates")

        self.assertFalse(card.concern)

    def test_an_uploaded_certificate_is_not_cross_checked_against_caa(self):
        """HQ did not choose who signed it, so it cannot predict a renewal.

        Flagging one would assert a fact HQ does not have, and the operator
        who uploaded it renews it themselves, by hand, from wherever it came.
        """

        sweep(
            records=[
                record("example.com", "CAA", '0 issue "digicert.com"', rid="c1"),
                record("example.com", "A", "203.0.113.1", rid="a1"),
            ]
        )
        self._certificate(kind="tls.uploaded_certificate")

        card = self._insight("example.com", "Certificates")

        self.assertFalse(card.concern)

    def test_a_certificate_card_does_not_recite_other_domains(self):
        """A wildcard covering four domains explained itself with another one.

        Listing coverage at all made the card the tallest thing on the page,
        and on example.test it opened with three example.com names. What a
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
        """ "2 mail servers" is true and useless. The count of MX records is a
        redundancy detail; the question is who has the mailbox, and the records
        already say: both point at example.net.
        """

        sweep(
            records=[
                *APEX,
                record("_dmarc.example.com", "TXT", '"v=DMARC1; p=reject"', rid="d1"),
            ]
        )
        card = self._insight("example.com", "Email")

        self.assertEqual(card.value, "example.net")
        self.assertIn("SPF", card.detail)
        self.assertIn("DMARC", card.detail)

    def test_a_recognized_mail_host_is_named_the_way_people_say_it(self):
        sweep(
            records=[
                record("example.com", "MX", "mx01.mail.icloud.com", rid="m1", priority=10),
                record("example.com", "MX", "mx02.mail.icloud.com", rid="m2", priority=10),
            ]
        )

        self.assertEqual(self._insight("example.com", "Email").value, "iCloud")

    def test_mail_split_across_providers_is_not_summarized_into_one(self):
        sweep(
            records=[
                record("example.com", "MX", "mx01.mail.icloud.com", rid="m1", priority=10),
                record("example.com", "MX", "mx.other.example", rid="m2", priority=20),
            ]
        )

        self.assertEqual(self._insight("example.com", "Email").value, "2 mail servers")

    def test_a_domain_with_no_mail_records_says_it_is_forgeable(self):
        sweep(records=[record("example.com", "A", "203.0.113.1", rid="a1")])

        card = self._insight("example.com", "Email")

        self.assertEqual(card.value, "Not configured")

    def test_one_failing_contributor_does_not_lose_the_page(self):
        """This is the screen opened to find out what is wrong."""

        sweep()
        with mock.patch("hq.platform.application.zone_insights.certificates", side_effect=RuntimeError("boom")):
            insights = find_zone("example.com").insights

        labels = {i.label for i in insights}
        self.assertNotIn("Certificates", labels)
        self.assertIn("Email", labels)


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
        return next(i for i in find_zone("example.com").insights if i.label == "Services")

    def test_a_count_carrying_every_service_and_a_link_to_the_same_list(self):
        """The dialog is an enhancement: without a real href behind it, a card
        that summarizes thirty services into a number loses them when it does not open."""
        card = self._card()

        self.assertIn("3", card.value)
        self.assertEqual({row.title for row in card.rows}, {"h0.example.com", "h1.example.com", "h2.example.com"})
        self.assertEqual(card.url, reverse("control_plane:services"))

    def test_services_in_another_domain_are_not_counted(self):
        ManagedResource.objects.create(
            key="elsewhere",
            kind="adguard.rewrite",
            spec={"domain": "app.other.example", "answer": "10.0.0.2"},
            enabled=True,
        )

        self.assertEqual(len(self._card().rows), 3)


class RecordedResponsibilityTests(TestCase):
    """A declaration nothing converges is not a resource awaiting convergence.

    Reported as one, every domain and every watched container sat in the
    attention queue forever, each a line an operator can do nothing about.
    """

    def test_a_domain_is_recorded_rather_than_unobserved(self):
        from hq.platform.application.infrastructure import resource_health

        resource = ManagedResource.objects.create(
            key="a-domain",
            kind="cloudflare.zone",
            spec={"zone": "example.com", "connection_ref": "a-dns-account"},
        )

        health = resource_health(resource)
        self.assertEqual(health["state"], "declared")
        self.assertEqual(health["message"], "")

    def test_it_does_not_reach_the_attention_queue(self):
        from hq.platform.application.attention import infrastructure

        ManagedResource.objects.create(
            key="a-domain",
            kind="cloudflare.zone",
            spec={"zone": "example.com", "connection_ref": "a-dns-account"},
        )

        self.assertEqual(infrastructure(), ())


class PendingIsNotAFaultTests(TestCase):
    """A resource HQ has already asked about is not one to ask an operator
    about. Adopting several names queues their declarations at once, and none
    should read as a failure while the controller is on its way to them.
    """

    def _resource(self, **fields):
        return ManagedResource.objects.create(
            key="a-rewrite",
            kind="adguard.rewrite",
            spec={"domain": "app.example.com", "answer": "10.0.0.1"},
            **fields,
        )

    def test_a_declaration_the_controller_has_not_reached_or_reapplied_is_pending(self):
        from hq.platform.application.infrastructure import resource_health

        for generation, observed in ((1, 0), (3, 2)):
            with self.subTest(generation=generation, observed=observed):
                ManagedResource.objects.all().delete()
                health = resource_health(self._resource(generation=generation, observed_generation=observed))

                self.assertEqual((health["state"], health["message"]), ("pending", ""))

    def test_pending_does_not_reach_the_attention_queue(self):
        from hq.platform.application.attention import infrastructure

        self._resource(generation=1, observed_generation=0)

        self.assertEqual(infrastructure(), ())

    def test_one_the_controller_reached_and_said_nothing_about_still_does(self):
        """The rule must not swallow a resource that was applied and reported
        nothing, which is the case the message is for.
        """

        from hq.platform.application.attention import infrastructure

        self._resource(generation=1, observed_generation=1)

        self.assertEqual(len(infrastructure()), 1)


class TLSPostureInsightTests(TestCase):
    """A domain's TLS posture, read through the credential that can see it.

    The DNS token holds records and nothing else; `cloudflare_api` carries the
    account surface.
    """

    def _zone(self, posture=None, refused_parts=()):
        from django.utils import timezone

        from hq.domains.control_plane.models import ProviderInventory

        from ..zones import Zone

        record = {"zone": "example.com", "connection_ref": "a-dns"}
        if posture is not None:
            record["posture"] = posture
        ProviderInventory.objects.update_or_create(
            kind="cloudflare.zone",
            defaults={
                "records": [record],
                "observed_at": timezone.now(),
                "refused_parts": list(refused_parts),
            },
        )
        return Zone(zone="example.com", connection_ref="a-dns")

    def test_it_says_what_the_mode_means_rather_than_repeating_its_name(self):
        """ "Flexible" tells you nothing unless you already know what it does."""

        from ..zone_insights import posture

        found = posture(self._zone({"ssl": "flexible"}))

        self.assertEqual(found.value, "Flexible")
        self.assertIn("plain HTTP onward to the origin", found.detail)

    def test_it_carries_the_minimum_version_and_the_redirect(self):
        from ..zone_insights import posture

        found = posture(self._zone({"ssl": "strict", "min_tls_version": "1.2", "always_use_https": "on"}))

        self.assertEqual(found.value, "Full (strict)")
        self.assertIn("TLS 1.2", found.detail)
        self.assertIn("HTTPS", found.detail)

    def test_a_posture_hq_could_not_read_is_absent_rather_than_off(self):
        """The account token is separate and may refuse.

        A card reading "Off" because a permission was missing is worse than no
        card: it describes a domain served over plain HTTP that is not.
        """

        from ..zone_insights import posture

        self.assertIsNone(posture(self._zone()))

    def test_a_refused_read_says_so(self):
        """A read that failed is not a read that found nothing."""

        from ..zone_insights import posture

        found = posture(
            self._zone(
                {},
                [
                    {"part": "posture", "refusal": "", "reason": "Cloudflare refused: 403", "scope": "example.com"},
                ],
            )
        )

        self.assertEqual(found.value, "Not readable")
        self.assertIn("403", found.detail)
        self.assertTrue(found.concern)

    def test_a_missing_permission_names_it(self):
        from ..zone_insights import posture

        found = posture(
            self._zone(
                {},
                [
                    {
                        "part": "posture",
                        "refusal": "permission",
                        "reason": "Authentication error",
                        "scope": "example.com",
                    },
                ],
            )
        )

        # The permission to grant, by the name the provider's console uses.
        self.assertIn("Zone Settings Read", found.detail)

    def test_a_refusal_on_another_zone_is_not_this_ones(self):
        from ..zone_insights import posture

        found = posture(
            self._zone(
                {"ssl": "strict"},
                [
                    {"part": "posture", "refusal": "", "reason": "Cloudflare refused: 403", "scope": "example.net"},
                ],
            )
        )

        self.assertEqual(found.value, "Full (strict)")

    def test_a_refused_registration_read_says_so(self):
        from django.utils import timezone

        from hq.domains.control_plane.models import ProviderInventory

        from ..zone_insights import registration
        from ..zones import Zone

        ProviderInventory.objects.update_or_create(
            kind="cloudflare.zone",
            defaults={
                "records": [{"zone": "example.com", "registration": {}}],
                "refused_parts": [
                    {"part": "registration", "refusal": "", "reason": "Cloudflare refused: 403", "scope": ""}
                ],
                "observed_at": timezone.now(),
            },
        )

        found = registration(Zone(zone="example.com"))

        # A refusal that names no permission is said with the reason kept.
        self.assertEqual(found.value, "Not read")
        self.assertFalse(found.concern)
        self.assertIn("Cloudflare refused: 403", found.note)


class DomainRegistrationInsightTests(TestCase):
    """When a domain stops being yours, and whether it renews itself.

    The one fact about a domain no other credential here can supply. HQ renews
    the certificate, reconciles the records and serves the names inside it, and
    none of that survives the registration lapsing: Cloudflare will serve a
    zone perfectly for a domain about to stop being yours.
    """

    def _zone(self, registration=None):
        from django.utils import timezone

        from hq.domains.control_plane.models import ProviderInventory

        from ..zones import Zone

        record = {"zone": "example.com", "connection_ref": "a-dns"}
        if registration is not None:
            record["registration"] = registration
        ProviderInventory.objects.update_or_create(
            kind="cloudflare.zone",
            defaults={"records": [record], "observed_at": timezone.now()},
        )
        return Zone(zone="example.com", connection_ref="a-dns")

    def _soon(self, days):
        from datetime import timedelta

        from django.utils import timezone

        return (timezone.now() + timedelta(days=days)).date().isoformat()

    def test_a_domain_that_will_not_renew_itself_is_a_concern(self):
        from ..zone_insights import registration

        found = registration(self._zone({"expires_at": self._soon(40), "auto_renew": False}))

        self.assertTrue(found.concern)

    def test_the_same_date_with_auto_renew_on_is_not(self):
        """A date on its own is a calendar entry, not something to act on.

        Every domain expires every year. Flagging that would put four findings
        in the queue permanently and teach the operator to ignore the card.
        """

        from ..zone_insights import registration

        found = registration(self._zone({"expires_at": self._soon(40), "auto_renew": True}))

        self.assertFalse(found.concern)

    def test_it_names_the_registrar_that_reported_it(self):
        from ..zone_insights import registration

        renews = registration(
            self._zone({"expires_at": self._soon(200), "auto_renew": True, "registrar": "Example Registrar"})
        )
        manual = registration(
            self._zone({"expires_at": self._soon(200), "auto_renew": False, "registrar": "Example Registrar"})
        )

        self.assertIn("Example Registrar", renews.detail)
        self.assertIn("Example Registrar", manual.detail)
        self.assertNotEqual(renews.detail, manual.detail)

    def test_a_domain_nothing_has_read_says_not_read(self):
        from ..zone_insights import registration

        found = registration(self._zone())

        # The card stays, so the domain page keeps its four questions.
        self.assertEqual(found.value, "Not read")
        self.assertFalse(found.concern)
