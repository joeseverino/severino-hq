"""HQ's plan for the GitHub repository reading: the fingerprint each held
record was read under, and which repositories are due a full read."""

from datetime import UTC, datetime, timedelta

from django.test import SimpleTestCase, TestCase

from hq.domains.control_plane.models import ProviderInventory
from hq.domains.control_plane.observations.github import FULL_READ_EVERY, REPOSITORY_KIND

from ..github_repositories import due, plan
from ..inventory import record_inventory
from ..security import cli_principal

READ_AT = datetime(2030, 1, 2, 12, 0, tzinfo=UTC)


def read(name, *, fingerprint="f1", sha="abc", read_at=READ_AT):
    return {
        "connection_ref": "github",
        "repository": name,
        "default_branch": "main",
        "head": {"sha": sha},
        "fingerprint": fingerprint,
        "read_at": read_at.isoformat(),
        "carried": False,
    }


def sweep(*records):
    record_inventory({REPOSITORY_KIND: {"ok": True, "records": list(records)}}, principal=cli_principal())
    return {record["repository"]: record for record in ProviderInventory.objects.get(kind=REPOSITORY_KIND).records}


class PlanTests(TestCase):
    def test_the_plan_sends_each_held_fingerprint(self):
        sweep(read("example/alpha", fingerprint="f1"), read("example/beta", fingerprint=""))

        held = {entry["repository"]: entry for entry in plan(READ_AT + timedelta(minutes=1))["held"]}

        self.assertEqual((held["example/alpha"]["fingerprint"], held["example/beta"]["fingerprint"]), ("f1", ""))

    def test_the_plan_is_empty_before_the_first_read(self):
        self.assertEqual(plan(), {"held": []})

    def test_a_held_record_the_schema_now_refuses_is_read_in_full(self):
        # Stored under an older contract, as a record the bridge struck to text.
        ProviderInventory.objects.create(
            kind=REPOSITORY_KIND,
            records=[
                {**read("example/alpha"), "alerts": {"leaked_credentials": {"leaked": "[redacted]"}}},
                {**read("example/beta"), "alerts": {"leaked_credentials": {"leaked": 1}}},
            ],
            reachable=True,
            observed_at=READ_AT,
        )

        held = {entry["repository"]: entry["due"] for entry in plan(READ_AT + timedelta(seconds=1))["held"]}

        self.assertEqual(held, {"example/alpha": True, "example/beta": False})


class FullReadClockTests(SimpleTestCase):
    def test_a_repository_never_read_is_due(self):
        self.assertTrue(due("example/alpha", None, READ_AT))

    def test_a_repository_is_due_once_a_clock_period_after_its_read(self):
        self.assertFalse(due("example/alpha", READ_AT, READ_AT))
        self.assertTrue(due("example/alpha", READ_AT, READ_AT + FULL_READ_EVERY))
        self.assertTrue(due("example/alpha", READ_AT, READ_AT + 30 * FULL_READ_EVERY))

    def test_repositories_read_together_fall_due_in_different_sweeps(self):
        names = [f"example/repo-{index}" for index in range(12)]
        sweep_every = timedelta(minutes=5)

        def first_due(name):
            moment = READ_AT
            while not due(name, READ_AT, moment):
                moment += sweep_every
            return moment

        moments = {first_due(name) for name in names}

        self.assertGreater(len(moments), len(names) // 2)
        self.assertTrue(all(moment - READ_AT <= FULL_READ_EVERY for moment in moments))
