"""What the internet reaches, derived from the service paths HQ already walks."""

from django.test import TestCase

from ..exposure import GATED, OPEN, PRIVATE, UNKNOWN, UNROUTED, exposure_of_name, status_at
from ..projection import projection_scope
from .test_paths import CLOUDFLARE, PUBLIC_RANGE, estate, store


@PUBLIC_RANGE
class ExposureTests(TestCase):
    def setUp(self):
        estate()

    def level(self, hostname):
        with projection_scope():
            return exposure_of_name(hostname).level

    def test_a_gate_on_one_path_leaves_the_name_open_and_says_where(self):
        store(
            "cloudflare.access_app",
            {
                "connection_ref": CLOUDFLARE,
                "id": "a1",
                "name": "Shop admin",
                "domain": "shop.example.com/admin*",
                "type": "self_hosted",
            },
        )

        with projection_scope():
            exposure = exposure_of_name("shop.example.com")

        self.assertEqual(exposure.level, OPEN)
        self.assertEqual(exposure.worst.gates, ())
        self.assertEqual(exposure.worst.path_gates, ("/admin*",))


class RankingTests(TestCase):
    def test_exposure_only_ever_lowers_urgency(self):
        self.assertEqual(
            [status_at("serious", level) for level in (OPEN, UNKNOWN, GATED, PRIVATE, UNROUTED)],
            ["serious", "serious", "attention", "attention", "neutral"],
        )
        self.assertEqual(
            [status_at("attention", level) for level in (OPEN, GATED, PRIVATE, UNROUTED)],
            ["attention", "attention", "attention", "neutral"],
        )
        self.assertEqual(status_at("good", OPEN), "good")
