from django.apps import apps
from django.db import connection
from django.db.models import JSONField, UniqueConstraint
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext

from hq.platform.application import readings
from hq.platform.application.projection import projection_scope
from hq.platform.core.models import UpstreamReading


class StoredManyTests(TestCase):
    def test_a_projection_asks_for_each_reading_once(self):
        readings.record("example:a", 1)
        with projection_scope(), CaptureQueriesContext(connection) as captured:
            first = readings.stored_many(["example:a", "example:b"])
            again = readings.stored_many(["example:b", "example:a"])

        self.assertEqual(len(captured), 1)
        self.assertEqual({key: found.value for key, found in first.items()}, {"example:a": 1})
        self.assertEqual(again, first)

    def test_a_reading_written_within_a_projection_is_read_as_written(self):
        readings.record("example:a", 1)
        with projection_scope():
            readings.stored_many(["example:a", "example:b"])
            readings.record("example:a", 2)
            readings.record("example:b", 3)
            found = readings.stored_many(["example:a", "example:b"])

        self.assertEqual({key: reading.value for key, reading in found.items()}, {"example:a": 2, "example:b": 3})

    def test_outside_a_projection_each_call_reads_the_table(self):
        readings.record("example:a", 1)
        readings.stored_many(["example:a"])
        readings.record("example:a", 2)

        self.assertEqual(readings.stored_many(["example:a"])["example:a"].value, 2)


class OneReadingStoreTests(SimpleTestCase):
    """A value read from outside HQ is kept in one store, keyed by what it is a reading of."""

    def test_no_model_but_upstream_reading_holds_a_reading(self):
        def keyed(model) -> set[str]:
            names = {field.name for field in model._meta.concrete_fields if field.unique}
            for together in model._meta.unique_together:
                names.update(together)
            for constraint in model._meta.constraints:
                if isinstance(constraint, UniqueConstraint):
                    names.update(constraint.fields)
            return names

        def holds_a_reading(model) -> bool:
            fields = [
                field
                for field in model._meta.concrete_fields
                if not field.auto_created and field.name not in ("created_at", "updated_at")
            ]
            observed = [field for field in fields if field.name.endswith("observed_at")]
            values = [field for field in fields if isinstance(field, JSONField)]
            key = keyed(model)
            # A reading is a value, when it was read, and what it is a reading of:
            # nothing else a row could keep of its own.
            rest = [
                field
                for field in fields
                if field not in observed and field not in values and field.name not in key and not field.is_relation
            ]
            return bool(observed and values and not rest)

        found = [
            model._meta.label
            for model in apps.get_models()
            if model.__module__.startswith("hq.") and model is not UpstreamReading and holds_a_reading(model)
        ]

        self.assertEqual(
            found,
            [],
            "A value read from outside HQ, and when it was read, is an UpstreamReading under a key"
            " hq.platform.application.readings names (readings.record, readings.stored), never a model of its own.",
        )
