"""The web spells a Cloudflare record the way the controller does.

Both read ``controller/providers/testdata/cloudflare-normalization.json``; the
controller's half is ``TestNormalizationMatchesTheSharedCases``. A record the
two spell differently is matched by one and reported as drifted by the other.
"""

import json
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase, tag

from hq.domains.control_plane.names import normalized_hostname
from hq.domains.control_plane.provider_adapters.cloudflare import normalized_record_content
from hq.domains.control_plane.providers import PROVIDERS

CASES = Path(settings.BASE_DIR) / "controller/providers/testdata/cloudflare-normalization.json"
RECORD_KIND = "cloudflare.dns_record"


@tag("source")
class SharedNormalizationTests(SimpleTestCase):
    def test_each_case_normalizes_as_the_controller_does(self):
        cases = json.loads(CASES.read_text(encoding="utf-8"))
        self.assertTrue(cases, f"{CASES} holds no cases")
        provider = PROVIDERS[RECORD_KIND]
        for case in cases:
            with self.subTest(case["case"]):
                self.assertEqual(normalized_hostname(case["name"]), case["want_name"])
                self.assertEqual(normalized_record_content(case["record_type"], case["content"]), case["want_content"])
                # Identity is what adoption and the zone view match a live record by.
                spec = {"zone": "Example.COM.", **{key: case[key] for key in ("record_type", "name", "content")}}
                self.assertEqual(
                    provider.identity(spec),
                    ("example.com", case["want_name"], case["record_type"], case["want_content"]),
                )
