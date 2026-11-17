from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from hq.platform.application.dashboard import work_queue
from hq.platform.application.inventory_testing import store
from hq.platform.application.projection import projection_scope
from hq.platform.application.routes import reverse
from hq.platform.core import seeds
from hq.platform.core.example_estate.names import CLOUDFLARE, ZONE
from hq.platform.core.example_estate.testing import throwaway_media


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
