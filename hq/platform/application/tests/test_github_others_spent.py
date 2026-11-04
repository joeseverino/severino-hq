"""What else spends the GitHub App's hourly allowance, from consecutive sweeps'
records of it, and the finding once that passes what the pipeline takes."""

from datetime import UTC, datetime, timedelta

from django.test import TestCase
from django.utils import timezone

from hq.domains.control_plane.models import ProviderConnection, ProviderInventory
from hq.domains.control_plane.observations import OBSERVATIONS

from ..findings import derive_findings
from ..inventory import record_inventory
from ..security import cli_principal
from ..topology import derive_topology

KIND = "github.allowance"
RULE = "github-others-spent"
# The usual most that the deploy pipeline and the dependency update run spend
# in an hour, written out so the test holds the number it promises.
USUAL = 1000
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
    record_inventory({KIND: {"ok": True, "records": [record]}}, principal=cli_principal())


def stored():
    return ProviderInventory.objects.get(kind=KIND).records[0]


def raised():
    principal = cli_principal()
    return [
        finding
        for finding in derive_findings(derive_topology(principal=principal), principal=principal)
        if finding.rule == RULE
    ]


class OthersSpendTests(TestCase):
    def setUp(self):
        ProviderConnection.objects.create(
            connection_ref="example-app",
            controller_id="example-controller",
            provider="github_app",
            observed_at=timezone.now(),
        )

    def test_others_spend_is_what_github_counted_beyond_the_last_sweeps_own(self):
        sweep(allowance(used=100, spent=40))
        sweep(allowance(at=timedelta(minutes=5), used=100 + 40 + 25))

        self.assertEqual(stored()["others_spent"], 25)
        self.assertEqual(stored()["others_since"], SWEPT.isoformat())
        self.assertEqual(raised(), [])

    def test_a_first_sweep_has_nothing_to_judge_against(self):
        sweep(allowance(used=3000))

        self.assertIsNone(stored()["others_spent"])
        self.assertEqual(raised(), [])

    def test_a_reset_between_two_sweeps_leaves_the_pair_unjudged(self):
        sweep(allowance(used=100))
        sweep(allowance(at=timedelta(hours=1), used=4000, resets=RESETS + timedelta(hours=1)))

        self.assertIsNone(stored()["others_spent"])
        self.assertEqual(raised(), [])

    def test_spend_past_the_usual_within_one_hour_is_a_finding(self):
        sweep(allowance(used=100, spent=40))
        sweep(allowance(at=timedelta(minutes=5), used=140 + 700, spent=40))
        sweep(allowance(at=timedelta(minutes=10), used=880 + 400, spent=40))

        self.assertEqual(stored()["others_spent"], 1100)
        (finding,) = raised()
        self.assertEqual(finding.severity, "attention")
        connections = [
            node.id
            for node in derive_topology(principal=cli_principal()).nodes
            if node.kind == "connection" and node.connection_ref == "example-app"
        ]
        self.assertEqual([finding.subject], connections)
        self.assertIn(("Requests", "1,100"), finding.evidence)
        commands = [step.command for step in finding.steps if step.command]
        self.assertIn(
            "openssl rsa -in PATH_TO_PEM_FILE -pubout -outform DER | openssl sha256 -binary | openssl base64", commands
        )

    def test_spend_up_to_the_usual_is_no_finding(self):
        sweep(allowance(used=100, spent=40))
        sweep(allowance(at=timedelta(minutes=5), used=140 + USUAL - 1))

        self.assertEqual(stored()["others_spent"], USUAL - 1)
        self.assertEqual(raised(), [])

    def test_a_count_that_went_back_is_no_spend(self):
        sweep(allowance(used=500, spent=40))
        sweep(allowance(at=timedelta(minutes=5), used=300))

        self.assertEqual(stored()["others_spent"], 0)

    def test_a_controller_cannot_send_others_spend_of_its_own(self):
        sweep(allowance(used=100, others_spent=5000, others_since=SWEPT.isoformat()))

        self.assertIsNone(stored()["others_spent"])
        self.assertEqual(raised(), [])

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
