"""Every audit update says what changed, and a secret field only that it did."""

from datetime import date
from decimal import Decimal

from django.test import TestCase

from hq.domains.control_plane.models import OperationRequest
from hq.domains.expenses.models import Expense
from hq.platform.application.capabilities import execute_capability
from hq.platform.application.security import cli_principal
from hq.platform.application.tests.test_record_timeline import KEY, REWRITE, claim_and_report
from hq.platform.core import audit
from hq.platform.core.audit import audited_labels, register_audit
from hq.platform.core.models import AuditLog
from hq.platform.core.testing import managing_everything


class UpdateMessageTests(TestCase):
    def an_expense(self) -> Expense:
        return Expense.objects.create(
            date=date(2026, 1, 2), vendor="Example Hosting", item="Server", total_cost=Decimal("10.00")
        )

    def updated(self, expense: Expense) -> AuditLog:
        return AuditLog.objects.filter(
            action=AuditLog.Action.UPDATED, object_type=audited_labels()[Expense], object_id=str(expense.pk)
        ).latest("pk")

    def test_a_redacted_field_s_update_says_only_that_it_changed(self):
        with audit.withdrawn(Expense) as labels:
            register_audit(Expense, labels[Expense], redact=("vendor",))
            expense = Expense.objects.get(pk=self.an_expense().pk)
            expense.vendor = "Example Secret Vendor"
            expense.save()

            row = self.updated(expense)
        self.assertTrue(row.message)
        self.assertNotIn("Example", row.message)

    def test_no_update_in_a_record_s_life_through_the_controller_is_blank(self):
        managing_everything()
        first = AuditLog.objects.order_by("-pk").values_list("pk", flat=True).first() or 0
        created = execute_capability(
            "infrastructure.resource.create",
            {"key": KEY, "kind": "adguard.rewrite", "spec": dict(REWRITE)},
            principal=cli_principal(),
        )
        self.assertTrue(created["ok"], created)
        claim_and_report()
        removed = execute_capability(
            "infrastructure.resource.remove", {"idempotency_key": "remove-once"}, principal=cli_principal(), target=KEY
        )
        self.assertTrue(removed["ok"], removed)
        claim_and_report()

        updates = AuditLog.objects.filter(pk__gt=first, action=AuditLog.Action.UPDATED)
        self.assertTrue(updates.filter(object_type=audited_labels()[OperationRequest]).exists())
        for row in updates:
            with self.subTest(row=f"{row.object_type} {row.object_repr}"):
                self.assertTrue(row.message, row.metadata.get("changes"))
