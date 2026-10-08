"""What a read keeps for the audit diff changes no audit row.

`register_audit` keeps a cheap capture of each instance as it is read and makes
it readable only when the instance is saved. The reference here converts every
field to its readable form as the instance is read. Each script below runs
under both over the same real models, and the audit rows must match field by
field, with the same number of queries.
"""

import copy
import pickle
import uuid
from contextlib import ExitStack, contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest import mock

from django.db import connection, transaction
from django.db.models.signals import post_delete, post_init, post_save
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from hq.domains.control_plane.apps import _resource_connection
from hq.domains.control_plane.models import ManagedResource
from hq.domains.expenses.models import Expense
from hq.domains.projects.models import Project
from hq.domains.receipts.models import Receipt

from .. import audit
from ..audit import (
    AUDIT_DISPATCH_UID,
    REDACTED,
    _capture,
    _changes,
    _connection_of,
    _readable,
    _shown,
    _snapshot,
    _tracked,
    audited_labels,
    record_event,
    register_audit,
)
from ..models import AuditLog

COMPARED = ("action", "object_type", "object_id", "object_repr", "message", "connection", "metadata")
SIGNALS = (post_init, post_save, post_delete)

RESOURCE_ID = uuid.UUID(int=7)
OTHER_ID = uuid.UUID(int=8)
SEEN = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
SEEN_AGAIN = datetime(2026, 1, 3, 3, 4, 5, tzinfo=UTC)


def _eager_snapshot(instance) -> dict:
    """The reference: every loaded field, readable, taken on the spot."""
    loaded = instance.__dict__
    return {
        field.attname: _readable(loaded[field.attname])
        for field in instance._meta.concrete_fields
        if field.attname in loaded and not getattr(field, "auto_now", False)
    }


def register_eager(model, type_label, *, redact=(), observation=(), connection=None) -> None:
    """Audit a model with the snapshot taken, readable, at `post_init`."""
    secret = frozenset(redact)
    looked = frozenset(observation)

    def on_init(sender, instance, **kwargs):
        instance._audit_snapshot = _eager_snapshot(instance)

    def on_save(sender, instance, created, **kwargs):
        changes = {}
        if not created:
            changes = _changes(getattr(instance, "_audit_snapshot", None), _eager_snapshot(instance), secret)
            if looked and changes and set(changes) <= looked:
                instance._audit_snapshot = _eager_snapshot(instance)
                return
            if not changes:
                return
        record_event(
            action=AuditLog.Action.CREATED if created else AuditLog.Action.UPDATED,
            obj=instance,
            type_label=type_label,
            metadata={"changes": changes} if changes else None,
            connection=_connection_of(connection, instance),
        )
        instance._audit_snapshot = _eager_snapshot(instance)

    def on_delete(sender, instance, **kwargs):
        record_event(
            action=AuditLog.Action.DELETED,
            obj=instance,
            type_label=type_label,
            message=str(getattr(instance, "audit_gone", "") or ""),
            connection=_connection_of(connection, instance),
        )

    post_init.connect(on_init, sender=model, weak=False)
    post_save.connect(on_save, sender=model, weak=False)
    post_delete.connect(on_delete, sender=model, weak=False)


@contextmanager
def auditing(register, options: dict):
    """Audit each model in `options` through `register` alone, inside the block."""
    with ExitStack() as stack:
        stack.enter_context(mock.patch.dict(audit._AUDITED_MODELS))
        for signal in SIGNALS:
            # Registered first so it runs last: the cache is dropped once the
            # real receivers are back.
            stack.callback(signal.sender_receivers_cache.clear)
            stack.enter_context(mock.patch.object(signal, "receivers", list(signal.receivers)))
            for model in options:
                if not signal.disconnect(sender=model, dispatch_uid=AUDIT_DISPATCH_UID):
                    raise AssertionError(f"{model.__name__} has no audit receiver on a signal.")
        for model, extra in options.items():
            register(model, audit._AUDITED_MODELS.pop(model), **extra)
        yield


# Every audited model the scripts touch, as the host registers it.
AS_REGISTERED: dict = {
    Expense: {},
    Project: {},
    Receipt: {},
    ManagedResource: {"observation": ("last_observed_at",), "connection": _resource_connection},
}
# The same models with fields declared secret.
WITH_SECRETS: dict = {
    **AS_REGISTERED,
    Expense: {"redact": ("vendor", "total_cost", "notes")},
    ManagedResource: {
        "redact": ("spec", "desired_fingerprint"),
        "observation": ("last_observed_at",),
        "connection": _resource_connection,
    },
}


def a_resource(**fields) -> ManagedResource:
    values = {
        "id": RESOURCE_ID,
        "key": "example-rewrite",
        "kind": "example.rewrite",
        "spec": {"connection_ref": "example-dns", "domain": "app.example.com", "answer": "192.0.2.10"},
        "conditions": [{"type": "Ready", "status": "True"}],
    }
    return ManagedResource.objects.create(**{**values, **fields})


def an_expense(**fields) -> Expense:
    values = {
        "date": date(2026, 1, 2),
        "vendor": "Example Hosting",
        "item": "Server",
        "total_cost": Decimal("10.00"),
    }
    return Expense.objects.create(**{**values, **fields})


# The scripts. Each is a sequence of operations a request could perform.


def create_update_delete():
    expense = an_expense()
    expense.vendor = "Example Cloud"
    expense.total_cost = Decimal("12.50")
    expense.date = date(2026, 2, 3)
    expense.business_use_percentage = 50
    expense.notes = "n" * 300
    expense.save()
    expense.audit_gone = "Entered twice"
    expense.delete()
    resource = a_resource()
    resource.enabled = False
    resource.save()
    resource.delete()


def loaded_then_saved():
    pk = an_expense().pk
    expense = Expense.objects.get(pk=pk)
    expense.item = "Rack"
    expense.payment_method = "card"
    expense.save()
    a_resource()
    resource = ManagedResource.objects.get(pk=RESOURCE_ID)
    resource.kind = "example.record"
    resource.generation = 2
    resource.save()


def unchanged_saves():
    expense = Expense.objects.get(pk=an_expense().pk)
    expense.save()
    expense.vendor = expense.vendor + ""
    expense.save()
    a_resource()
    resource = ManagedResource.objects.get(pk=RESOURCE_ID)
    resource.spec = dict(resource.spec)
    resource.save()


def second_save_reports_only_its_own():
    expense = Expense.objects.get(pk=an_expense().pk)
    expense.vendor = "Second"
    expense.save()
    expense.item = "Third"
    expense.save()
    expense.save()
    expense.vendor = "Example Hosting"
    expense.save()


def json_mutated_in_place():
    a_resource()
    resource = ManagedResource.objects.get(pk=RESOURCE_ID)
    resource.spec["answer"] = "192.0.2.99"
    resource.conditions.append({"type": "Synced", "status": "False"})
    resource.status["phase"] = "pending"
    resource.save()
    resource.spec["nested"] = {"deep": [1, 2, 3]}
    resource.save()
    resource.spec["nested"]["deep"].append(4)
    resource.save()


def json_mutated_past_what_the_log_shows():
    a_resource(spec={"connection_ref": "example-dns", "padding": "p" * 400, "tail": "before"})
    resource = ManagedResource.objects.get(pk=RESOURCE_ID)
    resource.spec["tail"] = "after"
    resource.save()


def json_replaced_and_restored():
    a_resource()
    resource = ManagedResource.objects.get(pk=RESOURCE_ID)
    kept = copy.deepcopy(resource.spec)
    resource.spec = {"connection_ref": "example-other"}
    resource.save()
    resource.spec = kept
    resource.save()


def observation_only_then_real():
    a_resource()
    resource = ManagedResource.objects.get(pk=RESOURCE_ID)
    resource.last_observed_at = SEEN
    resource.save()
    resource.kind = "example.record"
    resource.save()
    resource.last_observed_at = SEEN_AGAIN
    resource.status = {"phase": "ready"}
    resource.save()
    resource.last_observed_at = SEEN
    resource.save()
    resource.save()


def deferred_fields():
    a_resource()
    narrow = ManagedResource.objects.only("key", "kind").get(pk=RESOURCE_ID)
    narrow.kind = "example.record"
    narrow.save()
    narrow.enabled = False
    narrow.save()
    without = ManagedResource.objects.defer("spec", "status").get(pk=RESOURCE_ID)
    without.generation = 5
    without.save()
    without.spec["answer"] = "192.0.2.50"
    without.save()
    pk = an_expense().pk
    for expense in Expense.objects.only("vendor"):
        expense.vendor = "Narrow"
        expense.save()
    expense = Expense.objects.defer("notes").get(pk=pk)
    expense.notes = "set while deferred"
    expense.save()


def constructed_not_read():
    pk = an_expense().pk
    replacement = Expense(pk=pk, date=date(2026, 3, 4), vendor="Replaced", item="By hand")
    replacement.save()
    replacement.vendor = "Replaced again"
    replacement.save()
    fresh = ManagedResource(id=OTHER_ID, key="example-second", kind="example.rewrite")
    fresh.spec["connection_ref"] = "example-late"
    fresh.save()
    fresh.spec["answer"] = "192.0.2.77"
    fresh.save()
    expense = Expense(date=date(2026, 3, 4), vendor="Built", item="By hand")
    expense.vendor = "Renamed before saving"
    expense.save()


def without_a_capture():
    pk = an_expense().pk
    expense = Expense.objects.get(pk=pk)
    del expense.__dict__["_audit_snapshot"]
    expense.vendor = "Unknown before"
    expense.save()
    expense.vendor = "Known before"
    expense.save()


def refreshed():
    pk = an_expense().pk
    expense = Expense.objects.get(pk=pk)
    Expense.objects.filter(pk=pk).update(vendor="Changed elsewhere", item="Elsewhere")
    expense.refresh_from_db()
    expense.save()
    Expense.objects.filter(pk=pk).update(notes="noted elsewhere")
    expense.refresh_from_db(fields=["notes"])
    expense.save()
    a_resource()
    resource = ManagedResource.objects.only("key").get(pk=RESOURCE_ID)
    ManagedResource.objects.filter(pk=RESOURCE_ID).update(spec={"connection_ref": "example-moved"})
    resource.refresh_from_db()
    resource.save()
    resource.refresh_from_db(fields=["spec", "kind"])
    resource.spec["answer"] = "192.0.2.3"
    resource.save()


def update_fields():
    expense = Expense.objects.get(pk=an_expense().pk)
    expense.vendor = "Written"
    expense.item = "Only in memory"
    expense.save(update_fields=["vendor"])
    expense.save(update_fields=["item"])
    expense.save(update_fields=[])
    a_resource()
    resource = ManagedResource.objects.get(pk=RESOURCE_ID)
    resource.last_observed_at = SEEN
    resource.spec["answer"] = "192.0.2.4"
    resource.save(update_fields=["last_observed_at", "updated_at"])
    resource.save(update_fields=["spec"])


def no_signal_paths():
    Expense.objects.bulk_create([Expense(date=date(2026, 1, day), vendor="Bulk", item=str(day)) for day in (1, 2, 3)])
    rows = list(Expense.objects.order_by("pk"))
    for row in rows:
        row.vendor = "Bulk updated"
    Expense.objects.bulk_update(rows, ["vendor"])
    Expense.objects.filter(pk=rows[0].pk).update(item="Updated in place")
    rows[1].save()
    Expense.objects.filter(pk=rows[2].pk).delete()
    Expense.objects.filter(pk=rows[0].pk)._raw_delete(Expense.objects.db)


def copied_and_pickled():
    expense = Expense.objects.get(pk=an_expense().pk)
    shallow = copy.copy(expense)
    shallow.vendor = "Shallow"
    shallow.save()
    deep = copy.deepcopy(expense)
    deep.item = "Deep"
    deep.save()
    a_resource()
    resource = ManagedResource.objects.get(pk=RESOURCE_ID)
    revived = pickle.loads(pickle.dumps(resource))
    revived.spec["answer"] = "192.0.2.5"
    revived.save()
    twin = copy.deepcopy(resource)
    twin.conditions.clear()
    twin.save()
    resource.save()


def manager_shortcuts():
    an_expense()
    Expense.objects.update_or_create(vendor="Example Hosting", defaults={"item": "Upserted"})
    Expense.objects.update_or_create(vendor="Nobody yet", defaults={"item": "New", "date": date(2026, 5, 6)})
    Expense.objects.get_or_create(vendor="Example Hosting", defaults={"item": "x", "date": date(2026, 5, 6)})
    ManagedResource.objects.update_or_create(
        key="example-rewrite", defaults={"kind": "example.rewrite", "id": RESOURCE_ID}
    )
    ManagedResource.objects.update_or_create(key="example-rewrite", defaults={"spec": {"answer": "192.0.2.6"}})


def related_rows():
    project = Project.objects.create(name="Example", slug="example")
    other = Project.objects.create(name="Other", slug="other")
    pk = an_expense(related_project=project).pk
    expense = Expense.objects.select_related("related_project").get(pk=pk)
    expense.related_project.name = "Renamed through the join"
    expense.related_project.save()
    expense.related_project = other
    expense.save()
    expense.related_project = None
    expense.save()
    project.delete()


def every_kind_of_value():
    expense = Expense.objects.get(pk=an_expense(notes="").pk)
    expense.total_cost = Decimal("10.0")
    expense.save()
    expense.total_cost = 10
    expense.save()
    expense.date = "2026-01-02"
    expense.save()
    expense.business_use_percentage = 100.0
    expense.save()
    expense.business_use_percentage = True
    expense.save()
    expense.vendor = "v" * 250
    expense.save()
    expense.vendor = "v" * 200 + "w" * 50
    expense.save()
    a_resource(last_observed_at=SEEN)
    resource = ManagedResource.objects.get(pk=RESOURCE_ID)
    resource.id = str(RESOURCE_ID)
    resource.enabled = 1
    resource.conditions = ()
    resource.spec = ["a", "list"]
    resource.last_observed_at = None
    resource.kind = "example.kind"
    resource.save()


def a_file():
    receipt = Receipt.objects.create(file="receipts/2026/a.pdf", vendor="Example", amount=Decimal("3.00"))
    loaded = Receipt.objects.get(pk=receipt.pk)
    loaded.file.name = "receipts/2026/b.pdf"
    loaded.save()
    loaded.file = "receipts/2026/c.pdf"
    loaded.amount = Decimal("4.00")
    loaded.save()
    str(loaded)
    loaded.save()
    again = Receipt.objects.get(pk=receipt.pk)
    str(again)
    again.file.name = "receipts/2026/d.pdf"
    again.save()
    again.delete()


def many_read_one_saved():
    for day in range(1, 21):
        an_expense(date=date(2026, 1, day), item=f"item {day}")
    rows = list(Expense.objects.order_by("pk"))
    narrow = list(Expense.objects.only("vendor").order_by("pk"))
    rows[3].vendor = "The one that moved"
    rows[3].save()
    narrow[5].vendor = "The narrow one"
    narrow[5].save()


SCRIPTS = (
    create_update_delete,
    loaded_then_saved,
    unchanged_saves,
    second_save_reports_only_its_own,
    json_mutated_in_place,
    json_mutated_past_what_the_log_shows,
    json_replaced_and_restored,
    observation_only_then_real,
    deferred_fields,
    constructed_not_read,
    without_a_capture,
    refreshed,
    update_fields,
    no_signal_paths,
    copied_and_pickled,
    manager_shortcuts,
    related_rows,
    every_kind_of_value,
    a_file,
    many_read_one_saved,
)


def run(register, options: dict, script) -> tuple[list[tuple], int]:
    """The audit rows a script leaves under `register`, and its query count."""
    savepoint = transaction.savepoint_create()
    try:
        with auditing(register, options), CaptureQueriesContext(connection) as queries:
            script()
        rows = list(AuditLog.objects.order_by("pk").values_list(*COMPARED))
        return rows, len(queries)
    finally:
        transaction.savepoint_rollback(savepoint)


class SameRecordTests(TestCase):
    """The cheap capture and the eager snapshot leave the same audit log."""

    maxDiff = None

    def assert_same(self, options: dict, script) -> list[tuple]:
        expected, expected_queries = run(register_eager, options, script)
        actual, actual_queries = run(register_audit, options, script)
        self.assertEqual(actual, expected)
        self.assertEqual(actual_queries, expected_queries)
        return actual

    def test_every_script_leaves_the_same_rows_as_registered(self):
        for script in SCRIPTS:
            with self.subTest(script=script.__name__):
                self.assert_same(AS_REGISTERED, script)

    def test_every_script_leaves_the_same_rows_with_secret_fields(self):
        for script in SCRIPTS:
            with self.subTest(script=script.__name__):
                self.assert_same(WITH_SECRETS, script)

    def test_the_scripts_use_the_models_the_host_audits(self):
        labels = audited_labels()
        for model in AS_REGISTERED:
            self.assertIn(model, labels)

    def test_the_harness_puts_the_real_receivers_back(self):
        before = [list(signal.receivers) for signal in SIGNALS]
        run(register_eager, AS_REGISTERED, create_update_delete)
        self.assertEqual([list(signal.receivers) for signal in SIGNALS], before)
        self.assertEqual(audited_labels()[Expense], "Expense")
        expense = an_expense()
        self.assertEqual(AuditLog.objects.filter(object_type="Expense").count(), 1)
        self.assertIn("_audit_snapshot", expense.__dict__)


class RecordedChangeTests(TestCase):
    """What the rows say, so two wrong answers cannot agree."""

    def changes(self, options: dict, script) -> list[dict]:
        rows, _ = run(register_audit, options, script)
        return [row[6].get("changes", {}) for row in rows if row[0] == AuditLog.Action.UPDATED]

    def test_an_in_place_json_change_is_reported(self):
        first, second, third = self.changes(AS_REGISTERED, json_mutated_in_place)
        self.assertEqual(set(first), {"spec", "conditions", "status"})
        self.assertIn("192.0.2.10", first["spec"][0])
        self.assertIn("192.0.2.99", first["spec"][1])
        self.assertEqual(set(second), {"spec"})
        self.assertEqual(set(third), {"spec"})
        self.assertIn("[1, 2, 3]", third["spec"][0])
        self.assertIn("[1, 2, 3, 4]", third["spec"][1])

    def test_a_json_change_past_the_shown_length_is_not_an_event(self):
        self.assertEqual(self.changes(AS_REGISTERED, json_mutated_past_what_the_log_shows), [])

    def test_a_second_save_reports_only_its_own_changes(self):
        self.assertEqual(
            self.changes(AS_REGISTERED, second_save_reports_only_its_own),
            [
                {"vendor": ["Example Hosting", "Second"]},
                {"item": ["Server", "Third"]},
                {"vendor": ["Second", "Example Hosting"]},
            ],
        )

    def test_values_are_shown_as_text_the_log_can_store(self):
        changed = self.changes(AS_REGISTERED, create_update_delete)[0]
        self.assertEqual(changed["total_cost"], ["10.00", "12.50"])
        self.assertEqual(changed["date"], ["2026-01-02", "2026-02-03"])
        self.assertEqual(changed["business_use_percentage"], [100, 50])
        self.assertEqual(changed["notes"], ["", "n" * 200])

    def test_a_secret_field_is_named_and_its_values_withheld(self):
        changed = self.changes(WITH_SECRETS, create_update_delete)[0]
        self.assertEqual(changed["vendor"], [REDACTED, REDACTED])
        self.assertEqual(changed["total_cost"], [REDACTED, REDACTED])
        self.assertEqual(changed["date"], ["2026-01-02", "2026-02-03"])
        secret_json = self.changes(WITH_SECRETS, json_mutated_in_place)[0]
        self.assertEqual(secret_json["spec"], [REDACTED, REDACTED])

    def test_a_save_that_only_looked_is_not_an_event(self):
        self.assertEqual(
            self.changes(AS_REGISTERED, observation_only_then_real),
            [
                {"kind": ["example.rewrite", "example.record"]},
                {
                    "status": ["{}", "{'phase': 'ready'}"],
                    "last_observed_at": ["2026-01-02 03:04:05+00:00", "2026-01-03 03:04:05+00:00"],
                },
            ],
        )

    def test_a_deferred_field_is_never_claimed(self):
        changed = self.changes(AS_REGISTERED, deferred_fields)
        self.assertEqual(changed[0], {"kind": ["example.rewrite", "example.record"]})
        # `enabled` and `notes` were set while deferred: what they were before
        # is not known, so neither is reported.
        self.assertEqual(changed[1], {"generation": [1, 5]})
        self.assertEqual(changed[3], {"vendor": ["Example Hosting", "Narrow"]})
        self.assertEqual(len(changed), 4)
        self.assertFalse({"enabled", "notes"} & {name for change in changed for name in change})

    def test_a_constructed_instance_is_compared_with_how_it_was_built(self):
        changed = self.changes(AS_REGISTERED, constructed_not_read)
        self.assertEqual(changed[0], {"vendor": ["Replaced", "Replaced again"]})
        self.assertEqual(set(changed[1]), {"spec"})
        self.assertEqual(len(changed), 2)

    def test_an_instance_with_no_capture_claims_nothing(self):
        rows, _ = run(register_audit, AS_REGISTERED, without_a_capture)
        self.assertEqual([row[0] for row in rows], [AuditLog.Action.CREATED])

    def test_paths_that_send_no_signal_leave_no_row(self):
        rows, _ = run(register_audit, AS_REGISTERED, no_signal_paths)
        self.assertEqual(
            [(row[0], row[6].get("changes")) for row in rows],
            [
                # The one row saved through the model, against what it read.
                (AuditLog.Action.UPDATED, {"vendor": ["Bulk", "Bulk updated"]}),
                # A queryset delete loads its rows and signals each.
                (AuditLog.Action.DELETED, None),
            ],
        )

    def test_the_connection_and_the_reason_are_recorded(self):
        rows, _ = run(register_audit, AS_REGISTERED, create_update_delete)
        by_type = {(row[1], row[0]): row for row in rows}
        self.assertEqual(by_type[("Expense", AuditLog.Action.DELETED)][4], "Entered twice")
        self.assertEqual(by_type[("Managed resource", AuditLog.Action.UPDATED)][5], "example-dns")


class ReadCostTests(TestCase):
    """Reading rows pays for a capture, never for a query or a conversion."""

    def test_reading_rows_is_one_query_and_the_diff_is_none(self):
        for day in range(1, 21):
            an_expense(date=date(2026, 1, day))
        with self.assertNumQueries(1):
            rows = list(Expense.objects.order_by("pk"))
        with self.assertNumQueries(1):
            narrow = list(Expense.objects.only("vendor").order_by("pk"))
        names = _tracked(Expense)
        narrow[1].vendor = "Moved"
        # Everything the save receiver computes, on a full and a narrow row.
        with self.assertNumQueries(0):
            for row in (rows[0], narrow[1]):
                changed = _changes(_shown(row._audit_snapshot), _snapshot(row, names), frozenset())
                _capture(row, names)
        self.assertEqual(changed, {"vendor": ["Example Hosting", "Moved"]})
        self.assertEqual(len(narrow[1].get_deferred_fields()), len(names) - 1)

    def test_a_capture_holds_only_loaded_fields(self):
        an_expense()
        narrow = Expense.objects.only("vendor").get()
        with self.assertNumQueries(0):
            held = _capture(narrow, _tracked(Expense))
        self.assertEqual(set(held), {"id", "vendor"})
        self.assertEqual(set(narrow._audit_snapshot), {"id", "vendor"})

    def test_a_value_that_can_change_in_place_is_made_readable_on_read(self):
        a_resource()
        resource = ManagedResource.objects.get(pk=RESOURCE_ID)
        held = resource._audit_snapshot
        for name in ("spec", "status", "conditions"):
            self.assertIsInstance(held[name], str, name)
        # A value that cannot is held as it is, and converted on a save.
        self.assertIs(held["id"], resource.id)
        self.assertIs(held["created_at"], resource.created_at)

    def test_only_exact_immutable_types_are_held_unconverted(self):
        class Text(str):
            pass

        resource = ManagedResource(key="k", kind=Text("example.kind"), conditions=("a",))
        held = resource._audit_snapshot
        self.assertIs(type(held["kind"]), str)
        self.assertEqual(held["conditions"], "('a',)")

    def test_the_clock_field_is_not_tracked(self):
        self.assertNotIn("updated_at", _tracked(Expense))
        self.assertIn("created_at", _tracked(Expense))
        self.assertEqual(
            _tracked(Expense),
            tuple(f.attname for f in Expense._meta.concrete_fields if f.attname != "updated_at"),
        )
