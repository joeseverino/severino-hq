from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from hq.platform.application.certificate_expiry import CERTIFICATE_EXPIRES, expiring
from hq.platform.application.findings import findings
from hq.platform.application.inventory_testing import store
from hq.platform.application.projection import projection_scope
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

        self.assertEqual(finding["severity"], "attention")
        self.assertIn("certificate error", finding["explanation"])

    def test_findings_come_in_title_order_and_other_facts_are_passed_over(self):
        found = expiring(holding(certificate("b.example.com", 3), ("other", "x"), certificate("a.example.com", 3)))

        self.assertEqual([item["evidence"][0][1] for item in found], ["a.example.com", "b.example.com"])


class ToldApartTests(SimpleTestCase):
    """Two certificates that would read alike are each named by what differs."""

    def assertToldApart(self, estate, *said: str) -> None:
        found = expiring(estate)
        titles = [item["title"] for item in found]
        self.assertEqual(len(set(titles)), len(titles), titles)
        self.assertEqual(len({(item["subject"], item["part"]) for item in found}), len(found), "one queue item each")
        for words in said:
            self.assertTrue(any(words in title for title in titles), (words, titles))

    def test_a_zone_s_certificate_and_its_backup_by_issuer(self):
        self.assertToldApart(
            holding(
                certificate("example.com", -3, issuer="Google Trust Services"),
                certificate("example.com", -3, issuer="Let's Encrypt", hours=5),
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

    def test_a_certificate_no_other_reads_like_names_only_itself(self):
        (finding,) = expiring(holding(certificate("example.com", 4, issuer="Let's Encrypt")))

        self.assertNotIn("Let's Encrypt", finding["title"])


class ExampleEstateTests(TestCase):
    """Over the example estate, no two open findings read the same."""

    @classmethod
    def setUpTestData(cls):
        with throwaway_media(), seeds.built("standard") as owner:
            cls.user = owner

    def test_a_zone_s_edge_certificate_and_its_backup_each_read_as_their_own(self):
        # Cloudflare holds a backup beside a zone's edge certificate; here
        # both lapsed on one day, two certificates under one name.
        lapsed = timezone.now() - timedelta(days=3, hours=12)
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
                    "expires_on": (lapsed + timedelta(hours=hours)).isoformat(),
                }
                for status, authority, hours in (("active", "google", 0), ("backup_issued", "lets_encrypt", 5))
            ),
        )

        with projection_scope():
            found = findings(principal=web_principal(self.user))["findings"]
        titles = [finding["title"] for finding in found]

        self.assertEqual(sum(finding["rule"] == "certificate-expiring" for finding in found), 2, titles)
        self.assertEqual(len(set(titles)), len(titles), titles)
