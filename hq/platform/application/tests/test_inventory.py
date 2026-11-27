"""Seeing what the providers hold: what a sweep records, what a reading stands for, and what an observation is."""

from django.test import TestCase

from ..report_testing import bridged
from .inventory_records import A_REWRITE


class RecordingTests(TestCase):
    def test_a_kind_this_hq_does_not_know_is_ignored_not_rejected(self):
        """A controller ahead of this HQ must not take the whole sweep down."""
        result = bridged(
            "inventory",
            {
                "invented.kind": {"ok": True, "records": [{}]},
                "adguard.rewrite": {"ok": True, "records": [A_REWRITE]},
            },
        )

        self.assertEqual(result["recorded"], ["adguard.rewrite"])
