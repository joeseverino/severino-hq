from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from django.test import SimpleTestCase

from hq.platform.application.certificate_expiry import CERTIFICATE_EXPIRES, expiring

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def holding(*facts: tuple[str, str]) -> SimpleNamespace:
    """An estate with one connection node carrying ``facts``."""

    node = SimpleNamespace(id="connection:example-edge", label="example-edge", facts=facts)
    return SimpleNamespace(now=NOW, nodes=lambda: (node,))


def certificate(name: str, days: int, names: str = "app.example.com") -> tuple[str, str]:
    return CERTIFICATE_EXPIRES, "|".join(("", name, (NOW + timedelta(days=days)).isoformat(), names))


class ExpiringTests(SimpleTestCase):
    def test_a_serving_certificate_two_weeks_out_needs_attention_not_alarm(self):
        (finding,) = expiring(holding(certificate("example.com", 14)))

        self.assertEqual(finding["severity"], "attention")
        self.assertIn("certificate error", finding["explanation"])

    def test_findings_come_in_title_order_and_other_facts_are_passed_over(self):
        found = expiring(holding(certificate("b.example.com", 3), ("other", "x"), certificate("a.example.com", 3)))

        self.assertEqual([item["evidence"][0][1] for item in found], ["a.example.com", "b.example.com"])
