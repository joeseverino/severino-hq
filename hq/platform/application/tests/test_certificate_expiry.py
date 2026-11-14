from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from hq.platform.application.certificate_expiry import CERTIFICATE_EXPIRES, expiring
from hq.platform.application.dashboard import work_queue
from hq.platform.application.findings import findings
from hq.platform.application.inventory_testing import store
from hq.platform.application.projection import projection_scope
from hq.platform.application.routes import reverse
from hq.platform.application.security import web_principal
from hq.platform.core import seeds
from hq.platform.core.example_estate.names import CLOUDFLARE, ZONE
from hq.platform.core.example_estate.testing import throwaway_media

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def holding(*facts: tuple[str, str], holders: tuple[str, ...] = ("example-edge",)) -> SimpleNamespace:
    """An estate with a connection node for each of ``holders``, each carrying ``facts``."""

    nodes = tuple(SimpleNamespace(id=f"connection:{label}", label=label, facts=facts) for label in holders)
    return SimpleNamespace(now=NOW, nodes=lambda: nodes)


def certificate(
    name: str, days: int, names: str = "app.example.com", issuer: str = "", hours: int = 0
) -> tuple[str, str]:
    expires = (NOW + timedelta(days=days, hours=hours)).isoformat()
    return CERTIFICATE_EXPIRES, "|".join(("cloudflare.edge_certificate", name, expires, names, issuer))


class ExpiringTests(SimpleTestCase):
    def test_a_serving_certificate_two_weeks_out_needs_attention_not_alarm(self):
        (finding,) = expiring(holding(certificate("example.com", 14)))

        self.assertEqual(finding.severity, "attention")
        self.assertIn("certificate error", finding.explanation)

    def test_an_expired_certificate_folds_under_a_family_named_for_it_too(self):
        from hq.platform.application.findings import rule_for

        (expired,) = expiring(holding(certificate("example.com", -3)))

        self.assertIn("past expiry", rule_for(expired.rule).title)

    def test_findings_come_in_title_order_and_other_facts_are_passed_over(self):
        found = expiring(
            holding(
                certificate("b.example.com", 3, names="b.example.com"),
                ("other", "x"),
                certificate("a.example.com", 3, names="a.example.com"),
            )
        )

        self.assertEqual([item.evidence[0][1] for item in found], ["a.example.com", "b.example.com"])


class ServingTests(SimpleTestCase):
    """A name held by two certificates is judged by the one that serves it."""

    def test_a_replaced_certificate_that_lapsed_raises_nothing_while_its_successor_is_far_off(self):
        self.assertEqual(expiring(holding(certificate("example.com", -3), certificate("example.com", 60))), ())

    def test_a_name_held_twice_is_raised_once_by_the_certificate_that_runs_out_last(self):
        (finding,) = expiring(holding(certificate("example.com", -3), certificate("example.com", 5)))

        self.assertIn("expires in 5 days", finding.title)

    def test_certificates_that_serve_no_name_are_each_their_own(self):
        found = expiring(holding(certificate("example.com", -3, names=""), certificate("example.com", 5, names="")))

        self.assertEqual(len(found), 2)


class ToldApartTests(SimpleTestCase):
    """Two certificates that would read alike are each named by what differs."""

    def assertToldApart(self, estate, *said: str) -> None:
        found = expiring(estate)
        titles = [item.title for item in found]
        self.assertEqual(len(set(titles)), len(titles), titles)
        self.assertEqual(len({(item.subject, item.part) for item in found}), len(found), "one queue item each")
        for words in said:
            self.assertTrue(any(words in title for title in titles), (words, titles))

    def test_two_left_over_under_one_name_by_issuer(self):
        self.assertToldApart(
            holding(
                certificate("example.com", -3, names="", issuer="Google Trust Services"),
                certificate("example.com", -3, names="", issuer="Let's Encrypt", hours=5),
            ),
            "Google Trust Services",
            "Let's Encrypt",
        )

    def test_two_from_one_issuer_by_the_names_they_serve(self):
        self.assertToldApart(
            holding(
                certificate("example.com", 4, names="a.example.com", issuer="Let's Encrypt"),
                certificate("example.com", 4, names="b.example.com", issuer="Let's Encrypt", hours=5),
            ),
            "a.example.com",
            "b.example.com",
        )

    def test_one_held_in_two_places_by_where(self):
        self.assertToldApart(holding(certificate("example.com", 4), holders=("edge-a", "edge-b")), "edge-a", "edge-b")

    def test_one_issued_and_one_uploaded_by_how_each_came(self):
        self.assertToldApart(
            holding(
                certificate("example.com", -3, names="", issuer="Let's Encrypt"),
                certificate("example.com", -3, names="", issuer="uploaded"),
            ),
            "from Let's Encrypt",
            "Uploaded edge certificate example.com expired",
        )

    def test_a_certificate_no_other_reads_like_names_only_itself(self):
        (finding,) = expiring(holding(certificate("example.com", 4, issuer="Let's Encrypt")))

        self.assertNotIn("Let's Encrypt", finding.title)


def _certificate_named(item) -> str:
    """The name a Needs you item says a certificate serves: the first a
    finding's evidence names, an estate item's subject. A certificate left over
    serves none, so it is its own item and names nothing here."""

    facts = dict(map(tuple, item["facts"]))
    if "Certificate" in facts:
        return facts.get("Serves", "").split(", ")[0]
    return (item["subject"] or {}).get("label", "") if ":estate-certificate:" in item["key"] else ""


class ExampleEstateTests(TestCase):
    """Over the example estate, no two open findings read the same."""

    @classmethod
    def setUpTestData(cls):
        with throwaway_media(), seeds.built("standard") as owner:
            cls.user = owner

    def test_no_certificate_subject_is_on_needs_you_twice(self):
        # A zone's edge certificate beside the older one it replaced, which
        # lapsed: the zone is served by the newer, inside its warning window.
        store(
            "cloudflare.edge_certificate",
            *(
                {
                    "connection_ref": CLOUDFLARE,
                    "zone": ZONE,
                    "id": f"example-edge-{status}",
                    "type": "universal",
                    "hosts": [ZONE, f"*.{ZONE}"],
                    "status": status,
                    "certificate_authority": authority,
                    "expires_on": (timezone.now() + timedelta(days=days, hours=12)).isoformat(),
                }
                for status, authority, days in (("expired", "google", -4), ("active", "lets_encrypt", 5))
            ),
        )

        with projection_scope():
            queue = work_queue()
        named = [name for item in queue if (name := _certificate_named(item))]

        self.assertEqual(len(named), len(set(named)), named)
        (zone,) = [item for item in queue if _certificate_named(item) == ZONE]
        self.assertIn("5 days", zone["label"])
        # The finding draws on the findings page, whose queue keys hold no stamp.
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse("control_plane:findings")).status_code, 200)

    def test_a_certificate_uploaded_to_the_proxy_reads_as_uploaded(self):
        # NPM holds one name twice, as issued through it and as uploaded to it.
        lapsed = timezone.now() - timedelta(days=3, hours=12)
        store(
            "npm.certificate",
            *(
                {
                    "connection_ref": "example-npm",
                    "id": number,
                    "name": "lapsed.example.com",
                    "provider": provider,
                    "domains": ["lapsed.example.com"],
                    "expires_on": (lapsed + timedelta(hours=number)).isoformat(),
                    "serves": [],
                }
                for number, provider in ((1, "letsencrypt"), (2, "other"))
            ),
        )

        with projection_scope():
            found = findings(principal=web_principal(self.user), rule="certificate-expiring")["findings"]
        titles = sorted(finding["title"] for finding in found if "lapsed.example.com" in finding["title"].split())

        self.assertEqual(len(titles), 2, titles)
        self.assertIn("NPM certificate lapsed.example.com from Let's Encrypt", titles[0])
        self.assertIn("Uploaded NPM certificate lapsed.example.com expired", titles[1])
        # The page draws them.
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse("control_plane:findings")).status_code, 200)
