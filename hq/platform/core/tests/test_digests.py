"""The fingerprints HQ stores keep their bytes on the shared canonical form.

Each formula below is one a caller wrote for itself before it moved onto
``digests``; a stored approval, idempotency key, record contract or adopted
catalog compares against what it produced.
"""

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal

from django.test import SimpleTestCase

VALUES = (
    {},
    [],
    {"b": 1, "a": {"z": [3, 1, {"y": "é", "x": None}], "w": True}},
    {"ключ": "значение", "emoji": "\U0001f9ed", "tab": "a\tb", "quote": '"'},
    {"nested": {"deeper": {"deepest": [1.5, -0, 10**20, False]}}},
    ["naïve ☃", {"z": 1, "Z": 2, "_": 3}],
    "plain text",
    12,
)
# A value JSON has no form for, which approvals and run specs wrote as str.
UNWRITTEN = ({"at": datetime(2026, 1, 2, 3, 4, tzinfo=UTC), "amount": Decimal("1.10")},)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class StoredFingerprintTests(SimpleTestCase):
    def test_each_moved_digest_keeps_its_bytes(self):
        from hq.platform.core.digests import digest

        formulas = {
            # approvals, automation_runs and automation_adoption
            "with str": lambda value: _sha(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)),
            # observations.contract
            "plain": lambda value: _sha(json.dumps(value, sort_keys=True, separators=(",", ":"))),
            # idempotency's request fingerprint
            "no NaN": lambda value: _sha(json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True)),
        }
        for name, formula in formulas.items():
            for value in VALUES:
                with self.subTest(name, value=value):
                    self.assertEqual(digest(value), formula(value))
        for value in UNWRITTEN:
            self.assertEqual(digest(value), formulas["with str"](value))

    def test_a_document_compared_by_what_it_says_keeps_its_text(self):
        from hq.platform.application.inventory import _canonical_document

        for value in VALUES[:6]:
            with self.subTest(value=value):
                written = json.dumps(value, indent=2)
                self.assertEqual(
                    _canonical_document(written), json.dumps(json.loads(written), sort_keys=True, separators=(",", ":"))
                )

    def test_an_approval_keeps_its_fingerprint(self):
        from hq.platform.application.approvals import fingerprint

        payload, baseline = VALUES[3], VALUES[2]
        self.assertEqual(
            fingerprint("example.change", "example-target", payload, baseline),
            _sha(
                json.dumps(
                    {
                        "capability": "example.change",
                        "target": "example-target",
                        "payload": payload,
                        "baseline": baseline,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                )
            ),
        )
