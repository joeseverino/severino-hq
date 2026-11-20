"""Every command is refused at the capability runner, before anything is
written, to a caller without a capability it requires, so a handler may leave
its authority to the runner."""

from django.test import TestCase

from hq.platform.core.models import AuditLog, Revision
from hq.platform.search_index.models import SearchDocument

from ..capabilities import capability_registry, execute_capability
from ..security import Principal, operator_capabilities


def _written() -> dict[str, int]:
    """How many times each table has been written, as the database counts it."""

    return dict(Revision.objects.values_list("name", "value"))


class RefusedAtTheRunnerTests(TestCase):
    def test_a_caller_without_a_required_capability_is_refused_and_nothing_is_written(self):
        held = operator_capabilities()
        for spec in capability_registry():
            for capability in spec.required_capabilities:
                with self.subTest(capability=spec.name, requires=str(capability)):
                    caller = Principal("example-operator", "web", held - {capability})
                    before = _written()
                    audited = AuditLog.objects.count()

                    result = execute_capability(
                        spec.name, {}, principal=caller, target="example-target" if spec.target_kind else None
                    )

                    self.assertEqual(result["error"]["code"], "forbidden", result)
                    self.assertIn(str(capability), result["error"]["message"])
                    after = _written()
                    moved = {table for table in after.keys() | before.keys() if after.get(table) != before.get(table)}
                    # The refusal itself is recorded, and indexed for search, and nothing else.
                    self.assertLessEqual(moved, {AuditLog._meta.db_table, SearchDocument._meta.db_table})
                    self.assertEqual(
                        set(AuditLog.objects.order_by("pk")[audited:].values_list("action", flat=True)),
                        {AuditLog.Action.DENIED},
                    )
