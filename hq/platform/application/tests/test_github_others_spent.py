"""What else spends the GitHub App's hourly allowance, from consecutive sweeps'
records of it, and the finding once that passes what the pipeline takes."""

from datetime import UTC, datetime, timedelta

from django.test import TestCase

from hq.domains.control_plane.models import ProviderInventory
from hq.domains.control_plane.observations import OBSERVATIONS
from hq.platform.core.testing import connection

from ..inventory import record_inventory

KIND = "github.allowance"
SWEPT = datetime(2030, 1, 2, 17, 0, tzinfo=UTC)
RESETS = SWEPT + timedelta(minutes=50)


def allowance(*, at=timedelta(0), used, spent=40, resets=RESETS, **fields):
    """The record a controller sends for one sweep of installation 7."""

    return {
        "connection_ref": "example-app",
        "installation": 7,
        "account": "example",
        "limit": 5000,
        "used": used,
        "remaining": 5000 - used,
        "resets_at": resets.isoformat(),
        "started_at": (SWEPT + at).isoformat(),
        "spent": spent,
        **fields,
    }


def sweep(record):
    record_inventory({KIND: {"ok": True, "records": [record]}})


def stored():
    return ProviderInventory.objects.get(kind=KIND).records[0]


class OthersSpendTests(TestCase):
    def setUp(self):
        connection("github_app", "example-app")

    def test_others_spend_is_what_github_counted_beyond_the_last_sweeps_own(self):
        sweep(allowance(used=100, spent=40))
        sweep(allowance(at=timedelta(minutes=5), used=100 + 40 + 25))

        self.assertEqual(stored()["others_spent"], 25)
        self.assertEqual(stored()["others_since"], SWEPT.isoformat())

    def test_a_first_sweep_has_nothing_to_judge_against(self):
        sweep(allowance(used=3000))

        self.assertIsNone(stored()["others_spent"])

    def test_a_reset_between_two_sweeps_leaves_the_pair_unjudged(self):
        sweep(allowance(used=100))
        sweep(allowance(at=timedelta(hours=1), used=4000, resets=RESETS + timedelta(hours=1)))

        self.assertIsNone(stored()["others_spent"])

    def test_a_count_that_went_back_is_no_spend(self):
        sweep(allowance(used=500, spent=40))
        sweep(allowance(at=timedelta(minutes=5), used=300))

        self.assertEqual(stored()["others_spent"], 0)

    def test_a_controller_cannot_send_others_spend_of_its_own(self):
        sweep(allowance(used=100, others_spent=5000, others_since=SWEPT.isoformat()))

        self.assertIsNone(stored()["others_spent"])

    def test_a_record_without_github_s_count_is_refused(self):
        record = allowance(used=100)
        del record["used"]
        sweep(record)

        row = ProviderInventory.objects.get(kind=KIND)
        self.assertEqual(row.records, [])
        self.assertTrue(row.error)

    def test_the_reading_says_the_hour_in_its_line(self):
        sweep(allowance(used=100, spent=40))
        sweep(allowance(at=timedelta(minutes=5), used=140 + 25, spent=12))

        line = OBSERVATIONS[KIND].describe(stored())

        for number in ("4,835", "5,000", "12", "25"):
            self.assertIn(number, line)
