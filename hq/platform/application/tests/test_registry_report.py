"""One image the registry refuses must never cost HQ the readings it holds."""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from hq.domains.control_plane.models import ProviderInventory

from .. import public_registry
from ..public_registry import LookupNotFound, LookupUnavailable

KIND = "registry.image"


class OneRefusedImageTests(TestCase):
    def setUp(self):
        now = timezone.now()
        ProviderInventory.objects.create(
            kind=KIND,
            reachable=True,
            connected=True,
            observed_at=now,
            records=[
                {"image": "docker.io/example/app", "tags": ["1.0.0"], "read_at": now.isoformat()},
                {"image": "ghcr.io/example/private", "unread": "refused",
                 "read_at": (now - timedelta(days=2)).isoformat()},
            ],
        )

    def report(self, error):
        def read(subject):
            raise error("the registry refused an anonymous read (HTTP 401)")

        return public_registry._report(
            KIND, "image", ["docker.io/example/app", "ghcr.io/example/private"], read, timezone.now()
        )

    def test_a_failure_on_the_only_due_image_keeps_every_other_reading(self):
        found = self.report(LookupUnavailable)

        self.assertTrue(found["ok"])
        self.assertIn("docker.io/example/app", [record["image"] for record in found["records"]])

    def test_a_private_image_is_its_own_answer(self):
        found = self.report(LookupNotFound)

        self.assertTrue(found["ok"])
        private = next(record for record in found["records"] if record["image"] == "ghcr.io/example/private")
        self.assertIn("401", private["unread"])

    def test_nothing_readable_at_all_is_still_unreadable(self):
        ProviderInventory.objects.filter(kind=KIND).delete()

        found = self.report(LookupUnavailable)

        self.assertFalse(found["ok"])
