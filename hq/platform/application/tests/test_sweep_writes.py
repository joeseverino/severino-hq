"""What a sweep writes when it finds the estate as it left it.

A sweep runs every couple of minutes. It stores the inventory it read and the
moment it saw each declaration, and nothing else: no declaration is saved one
by one, nothing is audited and nothing is indexed, so its statements do not
grow with the estate and the only revisions it moves are those of the two
tables whose rows it changed.
"""

from collections import Counter

from django.apps import apps
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from hq.domains.control_plane.models import ManagedResource, ProviderInventory
from hq.platform.core import revisions
from hq.platform.core.models import AuditLog
from hq.platform.search_index.models import SearchDocument

from ..derived_inputs import ESTATE_READS
from ..security import cli_principal
from ..sweep import record_sweep

KIND = "adguard.rewrite"
INVENTORY = ProviderInventory._meta.db_table
DECLARATIONS = ManagedResource._meta.db_table


def _records(count: int) -> list[dict]:
    return [{"domain": f"app{index}.example.com", "answer": "192.0.2.10", "enabled": True} for index in range(count)]


def _sweep(records):
    return record_sweep({KIND: {"ok": True, "records": records}}, principal=cli_principal())


def _measured(records):
    """One sweep: its statements by verb, and how far each revision moved."""

    before = revisions.read().counts
    with CaptureQueriesContext(connection) as captured:
        result = _sweep(records)
    after = revisions.read().counts
    moved = {name: after[name] - before.get(name, 0) for name in after if after[name] != before.get(name, 0)}
    verbs = Counter(query["sql"].split(None, 1)[0].upper() for query in captured.captured_queries)
    return result, verbs, moved


class NoChangeSweepTests(TestCase):
    def declare(self, count: int) -> list[dict]:
        records = _records(count)
        for index, record in enumerate(records):
            ManagedResource.objects.create(key=f"app{index}", kind=KIND, spec=dict(record))
        # The first sweep observes every declaration, which is a real change.
        _sweep(records)
        return records

    def test_it_writes_the_inventory_and_the_moment_and_nothing_else(self):
        records = self.declare(20)

        result, verbs, moved = _measured(records)

        self.assertEqual(result["confirmed"], 20)
        # One row per kind read, and one per declaration seen.
        self.assertEqual(moved, {INVENTORY: 1, DECLARATIONS: 20})
        self.assertEqual(verbs["UPDATE"], 2)
        self.assertNotIn("INSERT", verbs)
        self.assertNotIn("DELETE", verbs)

    def test_no_other_table_an_estate_derivation_reads_moves(self):
        records = self.declare(5)
        estate = {apps.get_model(label)._meta.db_table for label in ESTATE_READS}

        _result, _verbs, moved = _measured(records)

        self.assertEqual(set(moved) & estate, {INVENTORY, DECLARATIONS})
        self.assertEqual(set(moved) - estate, set())

    def test_its_statements_do_not_grow_with_the_declarations(self):
        few = sum(_measured(self.declare(5))[1].values())
        ManagedResource.objects.all().delete()
        many = sum(_measured(self.declare(40))[1].values())

        self.assertEqual(few, many)
        self.assertLessEqual(many, 25)

    def test_the_moment_still_advances(self):
        records = self.declare(1)
        first = ManagedResource.objects.get().last_observed_at

        _sweep(records)

        self.assertGreater(ManagedResource.objects.get().last_observed_at, first)


class ChangedSweepTests(TestCase):
    def setUp(self):
        self.records = _records(2)
        for index, record in enumerate(self.records):
            ManagedResource.objects.create(key=f"app{index}", kind=KIND, spec=dict(record))
        _sweep(self.records)
        self.first = ManagedResource.objects.get(key="app0")

    def test_a_record_that_is_gone_leaves_its_declaration_last_seen_before(self):
        seen = self.first.last_observed_at

        _sweep(self.records[1:])

        self.first.refresh_from_db()
        self.assertEqual(self.first.last_observed_at, seen)
        self.assertGreater(ManagedResource.objects.get(key="app1").last_observed_at, seen)

    def test_a_drift_already_described_is_not_written_again(self):
        changed = [{**self.records[0], "answer": "192.0.2.99"}, self.records[1]]
        seen = self.first.last_observed_at
        _sweep(changed)
        self.first.refresh_from_db()
        self.assertEqual(self.first.conditions[0]["reason"], "Drifted")
        self.assertEqual(self.first.last_observed_at, seen)

        _result, _verbs, moved = _measured(changed)

        self.assertEqual(moved, {INVENTORY: 1, DECLARATIONS: 1})

    def test_an_observation_that_changed_is_saved_audited_and_indexed(self):
        ManagedResource.objects.filter(pk=self.first.pk).update(spec={**self.records[0], "answer": "192.0.2.99"})
        AuditLog.objects.all().delete()

        _sweep([{**self.records[0], "answer": "192.0.2.99"}, self.records[1]])

        self.first.refresh_from_db()
        self.assertEqual(self.first.status["answer"], "192.0.2.99")
        updates = AuditLog.objects.filter(object_type="Managed resource", action=AuditLog.Action.UPDATED)
        self.assertEqual(updates.count(), 1)
        self.assertIn("status", updates.get().metadata["changes"])
        body = SearchDocument.objects.get(scope="infrastructure.resources", object_id="app0").body
        self.assertEqual(body.count("192.0.2.99"), 2)
