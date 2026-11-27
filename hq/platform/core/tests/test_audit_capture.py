"""What a read keeps for the audit diff: an instance read with no capture
claims no change, and only an audited model can be withdrawn from auditing."""

from contextlib import contextmanager
from datetime import date
from decimal import Decimal

from django.db import transaction
from django.test import TestCase

from hq.domains.expenses.models import Expense

from .. import audit
from ..audit import audited_labels, register_audit
from ..models import AuditLog


@contextmanager
def auditing(register, options: dict):
    """Audit each model in `options` through `register` alone, inside the block."""
    with audit.withdrawn(*options) as labels:
        for model, extra in options.items():
            register(model, labels[model], **extra)
        yield


def an_expense(**fields) -> Expense:
    values = {
        "date": date(2026, 1, 2),
        "vendor": "Example Hosting",
        "item": "Server",
        "total_cost": Decimal("10.00"),
    }
    return Expense.objects.create(**{**values, **fields})


def without_a_capture():
    pk = an_expense().pk
    expense = Expense.objects.get(pk=pk)
    del expense.__dict__["_audit_snapshot"]
    expense.vendor = "Unknown before"
    expense.save()
    expense.vendor = "Known before"
    expense.save()


def run(script) -> list[str]:
    """The actions of the audit rows a script leaves with expenses audited as the host registers them."""
    savepoint = transaction.savepoint_create()
    try:
        with auditing(register_audit, {Expense: {}}):
            script()
        return list(AuditLog.objects.order_by("pk").values_list("action", flat=True))
    finally:
        transaction.savepoint_rollback(savepoint)


class WithdrawnTests(TestCase):
    def test_only_an_audited_model_can_be_withdrawn(self):
        before = audited_labels()
        with self.assertRaises(LookupError), audit.withdrawn(Expense, AuditLog):
            pass
        self.assertEqual(audited_labels(), before)


class RecordedChangeTests(TestCase):
    def test_an_instance_with_no_capture_claims_nothing(self):
        self.assertEqual(run(without_a_capture), [AuditLog.Action.CREATED])
