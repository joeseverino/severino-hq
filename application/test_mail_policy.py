"""The policies a domain publishes about its own mail, read and written back.

Every record here is a real shape -- the quoting DNS carries, the split strings
a long TXT arrives in, the tags nobody models -- because the one thing an editor
must never do is publish something other than what the operator chose.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from control_plane.models import ManagedResource

from .mail_policy import (
    SPF_LOOKUP_LIMIT,
    compose_dmarc,
    compose_spf,
    describe_dmarc,
    parse_dmarc,
    parse_spf,
)


class DmarcReadingTests(SimpleTestCase):
    def test_a_record_is_read_as_its_tags(self):
        tags = parse_dmarc('"v=DMARC1; p=reject; sp=reject; rua=mailto:box@example.com"')

        self.assertEqual(tags["p"], "reject")
        self.assertEqual(tags["sp"], "reject")
        self.assertEqual(tags["rua"], "mailto:box@example.com")

    def test_a_long_record_arrives_as_adjacent_strings(self):
        """DNS splits a long TXT into quoted chunks; joined they are one policy."""

        tags = parse_dmarc('"v=DMARC1; p=quarantine; " "rua=mailto:box@example.com"')

        self.assertEqual(tags["p"], "quarantine")
        self.assertEqual(tags["rua"], "mailto:box@example.com")

    def test_spacing_and_case_do_not_change_the_policy(self):
        self.assertEqual(parse_dmarc("V=DMARC1;P=reject")["p"], "reject")


class DmarcWritingTests(SimpleTestCase):
    def test_a_round_trip_publishes_what_was_read(self):
        original = "v=DMARC1; p=reject; sp=reject; rua=mailto:box@example.com"

        self.assertEqual(compose_dmarc(parse_dmarc(original)), original)

    def test_the_version_leads_whatever_order_the_tags_arrived_in(self):
        composed = compose_dmarc({"p": "none", "v": "DMARC1"})

        self.assertTrue(composed.startswith("v=DMARC1;"))

    def test_a_tag_this_module_does_not_model_survives_the_edit(self):
        """An editor that drops what it does not understand deletes policy."""

        composed = compose_dmarc(parse_dmarc("v=DMARC1; p=reject; fo=1; ri=3600"))

        self.assertIn("fo=1", composed)
        self.assertIn("ri=3600", composed)

    def test_a_default_is_not_written_out(self):
        self.assertNotIn("pct=", compose_dmarc({"p": "reject", "pct": "100"}))

    def test_a_changed_default_is_written_out(self):
        self.assertIn("pct=25", compose_dmarc({"p": "reject", "pct": "25"}))


class DmarcExplanationTests(SimpleTestCase):
    def test_the_policy_reads_as_what_happens_to_forged_mail(self):
        said = describe_dmarc("v=DMARC1; p=reject; rua=mailto:box@example.com")

        self.assertIn("Mail that fails checks is rejected outright.", said)
        self.assertIn("Subdomains follow the same rule.", said)

    def test_monitoring_only_is_not_described_as_protection(self):
        said = describe_dmarc("v=DMARC1; p=none")

        self.assertIn("delivered anyway", " ".join(said))

    def test_a_policy_with_nowhere_to_report_says_so(self):
        """Without reports there is no way to tighten safely, so it is named."""

        said = describe_dmarc("v=DMARC1; p=none")

        self.assertIn("nothing shows who sends as this domain", " ".join(said))

    def test_a_partial_rollout_is_not_silently_read_as_full_enforcement(self):
        said = describe_dmarc("v=DMARC1; p=reject; pct=25")

        self.assertIn("25%", " ".join(said))

    def test_something_that_is_not_dmarc_describes_nothing(self):
        self.assertEqual(describe_dmarc("v=spf1 include:example.com -all"), ())


class SpfTests(SimpleTestCase):
    def test_a_policy_is_read_as_its_terms(self):
        policy = parse_spf('"v=spf1 include:icloud.com -all"')

        self.assertTrue(policy.valid)
        self.assertEqual(policy.terms[0].mechanism, "include")
        self.assertEqual(policy.terms[0].argument, "icloud.com")

    def test_the_last_word_decides_everyone_else(self):
        self.assertIn("rejected", parse_spf("v=spf1 include:a.example -all").default_result)
        self.assertIn("soft", parse_spf("v=spf1 include:a.example ~all").default_result)

    def test_a_policy_protecting_nothing_says_so(self):
        self.assertIn("protects nothing", parse_spf("v=spf1 +all").default_result)

    def test_a_policy_with_no_final_term_is_named_as_unhandled(self):
        self.assertIn("unhandled", parse_spf("v=spf1 include:a.example").default_result)

    def test_lookups_are_counted_because_eleven_of_them_fail_silently(self):
        policy = parse_spf("v=spf1 " + " ".join(f"include:h{i}.example" for i in range(11)) + " -all")

        self.assertEqual(policy.lookups, 11)
        self.assertTrue(policy.over_limit)

    def test_a_policy_inside_the_limit_is_not_flagged(self):
        policy = parse_spf("v=spf1 include:icloud.com -all")

        self.assertLessEqual(policy.lookups, SPF_LOOKUP_LIMIT)
        self.assertFalse(policy.over_limit)

    def test_terms_that_cost_nothing_are_not_counted(self):
        """ip4 and ip6 need no lookup, so a long list of them is fine."""

        policy = parse_spf("v=spf1 " + " ".join(f"ip4:192.0.2.{i}" for i in range(20)) + " -all")

        self.assertEqual(policy.lookups, 0)

    def test_a_round_trip_publishes_what_was_read(self):
        original = "v=spf1 include:icloud.com -all"

        self.assertEqual(compose_spf(parse_spf(original).terms), original)

    def test_something_that_is_not_spf_is_not_read_as_one(self):
        self.assertFalse(parse_spf("v=DMARC1; p=reject").valid)


@override_settings(SEVERINO_INFRASTRUCTURE_ENABLE_PUBLIC_DNS=True)
class MailPageTests(TestCase):
    """The page that publishes a policy, and what it must never publish.

    Publishing needs the deployment's public-DNS switch, the same gate every
    other record change goes through. Enabled here deliberately: without it
    these tests would pass by never writing anything.
    """

    def setUp(self):
        for key, name, content, priority in (
            ("mx1", "example.com", "mx01.mail.example.net", 10),
            ("spf", "example.com", '"v=spf1 include:example.net -all"', None),
            ("dmarc", "_dmarc.example.com",
             '"v=DMARC1; p=none; rua=mailto:box@example.com; fo=1"', None),
        ):
            ManagedResource.objects.create(
                key=key, kind="cloudflare.dns_record", enabled=True,
                spec={
                    "zone": "example.com", "name": name,
                    "record_type": "MX" if priority else "TXT",
                    "content": content, "priority": priority,
                    "proxied": False, "ttl": 1,
                },
            )
        ManagedResource.objects.create(
            key="zone", kind="cloudflare.zone", enabled=True,
            spec={"zone": "example.com", "connection_ref": "cf"},
        )
        self.user = get_user_model().objects.create_user("op", password="x" * 12)
        self.client.force_login(self.user)
        self.url = reverse("zones:mail", kwargs={"zone": "example.com"})

    def test_the_page_reads_the_policy_in_english(self):
        response = self.client.get(self.url)

        self.assertContains(response, "delivered anyway")
        self.assertContains(response, "Who may send as this domain")

    def test_tightening_the_policy_publishes_it(self):
        self.client.post(self.url, {"section": "dmarc", "p": "reject", "sp": "",
                                    "rua": "mailto:box@example.com", "pct": "100"})
        content = ManagedResource.objects.get(key="dmarc").spec["content"]

        self.assertIn("p=reject", content)

    def test_a_tag_the_editor_does_not_model_survives_publishing(self):
        """`fo` is not on the form. Saving must not delete it."""

        self.client.post(self.url, {"section": "dmarc", "p": "reject", "sp": "",
                                    "rua": "mailto:box@example.com", "pct": "100"})

        self.assertIn("fo=1", ManagedResource.objects.get(key="dmarc").spec["content"])

    def test_senders_are_composed_rather_than_typed(self):
        self.client.post(self.url, {"section": "spf",
                                    "rule": ["include:example.net", "include:other.example"],
                                    "default": "~"})
        content = ManagedResource.objects.get(key="spf").spec["content"]

        self.assertIn("include:other.example", content)
        self.assertTrue(content.rstrip('"').endswith("~all"))

    def test_the_sender_policy_always_ends_with_a_decision(self):
        """Without a final `all`, unlisted senders are simply unhandled."""

        self.client.post(self.url, {"section": "spf", "rule": ["include:example.net"],
                                    "default": "-"})

        self.assertIn("-all", ManagedResource.objects.get(key="spf").spec["content"])
