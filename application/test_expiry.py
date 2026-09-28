"""One days-left rule, and one derivation of a resource for every adapter."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from django.test import SimpleTestCase, TestCase
from django.utils import timezone as dj_timezone

from control_plane.models import ManagedResource
from control_plane.provider_adapters.tls import CERTIFICATE_KIND
from control_plane.provider_spec import expiry_phrase

from .expiry import days_until, renewal_opens_at, renewal_window
from .resource_context import resource_context
from .resources import get_resource
from .security import Capability, Principal

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
EVERYTHING = Principal("test", "operator", frozenset(Capability))


class DaysUntilTests(SimpleTestCase):
    def test_nearest_day(self):
        self.assertEqual(days_until(NOW + timedelta(days=40), NOW), 40)
        self.assertEqual(days_until(NOW + timedelta(days=40) - timedelta(seconds=1), NOW), 40)
        self.assertEqual(days_until(NOW + timedelta(days=87, hours=11), NOW), 87)
        self.assertEqual(days_until(NOW + timedelta(days=87, hours=13), NOW), 88)

    def test_never_zero_while_time_remains(self):
        self.assertEqual(days_until(NOW + timedelta(hours=2), NOW), 1)
        self.assertEqual(days_until(NOW, NOW), 0)

    def test_negative_once_past(self):
        self.assertEqual(days_until(NOW - timedelta(hours=2), NOW), -1)

    def test_a_naive_moment_is_utc(self):
        self.assertEqual(days_until(datetime(2026, 1, 11), NOW), 10)

    def test_renewal_window_and_opening(self):
        self.assertEqual(renewal_window({}), 30)
        self.assertEqual(renewal_window({"renewal_window_days": "14"}), 14)
        self.assertEqual(renewal_window({"renewal_window_days": "soon"}), 30)
        self.assertEqual(renewal_opens_at(NOW, 30), NOW - timedelta(days=30))

    def test_no_other_module_counts_days_itself(self):
        """The regression class: a second copy of the arithmetic disagrees by a day."""

        root = Path(__file__).resolve().parents[1]
        pattern = re.compile(r"(?<!/)/\s*86400|\)\.days\b")
        allowed = {"application/expiry.py", "application/ui.py", "application/analytics.py"}
        found = []
        for path in root.glob("*/**/*.py"):
            relative = path.relative_to(root).as_posix()
            if relative in allowed or "/test" in relative or relative.startswith(".venv"):
                continue
            text = path.read_text(encoding="utf-8")
            if "expir" not in text and "not_after" not in text:
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if pattern.search(line) and "MAX_QUERY_DAYS" not in line:
                    found.append(f"{relative}:{number}")
        self.assertEqual(found, [])


class OneAnswerTests(TestCase):
    def setUp(self):
        self.resource = ManagedResource.objects.create(
            key="example-wildcard",
            kind=CERTIFICATE_KIND,
            spec={"certificate_name": "example-wildcard", "domains": ["*.example.com"]},
            status={
                "not_after": (
                    dj_timezone.now() + timedelta(days=87, hours=12, minutes=30)
                ).isoformat()
            },
        )

    def test_every_surface_says_the_same_days_left(self):
        derived = resource_context(self.resource)
        phrase = expiry_phrase(self.resource.status["not_after"])
        from .estate import Expiry

        card = Expiry("example", "Certificate", derived.expiry.not_after)

        self.assertEqual(derived.expiry.days_left, 88)
        self.assertTrue(phrase.endswith("88 days"))
        self.assertEqual(card.days, 88)

    def test_the_api_and_mcp_return_the_derived_facts(self):
        found = get_resource("infrastructure.resources", "example-wildcard", principal=EVERYTHING)

        derived = found["derived"]
        self.assertEqual(derived["expiry"]["days_left"], 88)
        self.assertEqual(
            derived["expiry"]["renewal_at"],
            resource_context(self.resource).expiry.renewal_at.isoformat(),
        )
        self.assertIn("renew", derived["actions"])
        self.assertIn(derived["removal"]["mode"], {"delete", "forget", "unavailable"})
        self.assertEqual(derived["health"], found["resource"]["health"])

    def test_an_unreadable_expiry_is_none_not_an_error(self):
        self.resource.status = {"not_after": "not a date"}

        self.assertIsNone(resource_context(self.resource).expiry)
