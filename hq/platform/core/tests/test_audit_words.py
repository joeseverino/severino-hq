"""Every audit row says what happened in words: an update its changes, and
any row the operation it was written in, older rows as well as new ones."""

from datetime import date
from decimal import Decimal

from django.test import SimpleTestCase, TestCase

from hq.domains.control_plane.models import ApprovalRequest, OperationRequest
from hq.domains.expenses.models import Expense
from hq.platform.application.adoption_testing import managing_everything
from hq.platform.application.capabilities import execute_capability
from hq.platform.application.security import cli_principal
from hq.platform.application.tests.test_record_timeline import KEY, REWRITE, claim_and_report
from hq.platform.core import audit
from hq.platform.core.attribution import operation_context
from hq.platform.core.audit import audited_labels, register_audit
from hq.platform.core.models import AuditLog

# What ``audit._changes`` stores, in each shape it stores: a plain pair, a
# value now set or now cleared, a pair of nothings, a secret, and a record
# whose inner values moved.
STORED_SHAPES = (
    {"state": ["queued", "claimed"]},
    {"notes": [None, "example"]},
    {"notes": ["example", ""]},
    {"notes": [None, ""]},
    {"token": ["«redacted»", "«redacted»"]},
    {"spec": {"Answer": ["192.0.2.10", "192.0.2.11"], "": ["", "3 fields"]}},
    {"a": [1, 2], "b": [True, False], "c": ["x", "y"], "d": ["y", "z"]},
)


def changes_said(changes):
    # Imported where it runs, so this module imports at a base without it.
    from hq.platform.core.audit_words import changes_said as said

    return said(changes)


class ChangesSaidTests(SimpleTestCase):
    def test_every_shape_a_change_is_stored_in_is_said_in_one_short_line(self):
        for changes in STORED_SHAPES:
            with self.subTest(changes=changes):
                said = changes_said(changes)
                self.assertTrue(said)
                self.assertNotIn("\n", said)
                self.assertLessEqual(len(said), 160)

    def test_a_secret_is_said_to_have_changed_and_nothing_more(self):
        said = changes_said({"token": ["«redacted»", "«redacted»"]})

        self.assertIn("Token", said)
        self.assertNotIn("redacted", said)

    def test_a_plain_change_says_its_field_and_both_values(self):
        said = changes_said({"state": ["queued", "claimed"]})

        for word in ("State", "queued", "claimed"):
            self.assertIn(word, said)


class UpdateMessageTests(TestCase):
    def an_expense(self) -> Expense:
        return Expense.objects.create(
            date=date(2026, 1, 2), vendor="Example Hosting", item="Server", total_cost=Decimal("10.00")
        )

    def updated(self, expense: Expense) -> AuditLog:
        return AuditLog.objects.filter(
            action=AuditLog.Action.UPDATED, object_type=audited_labels()[Expense], object_id=str(expense.pk)
        ).latest("pk")

    def test_an_update_nobody_described_says_what_it_changed(self):
        expense = Expense.objects.get(pk=self.an_expense().pk)
        expense.item = "Example server"
        expense.save()

        row = self.updated(expense)
        self.assertTrue(row.metadata["changes"])
        self.assertIn("Example server", row.message)

    def test_a_redacted_field_s_update_says_only_that_it_changed(self):
        with audit.withdrawn(Expense) as labels:
            register_audit(Expense, labels[Expense], redact=("vendor",))
            expense = Expense.objects.get(pk=self.an_expense().pk)
            expense.vendor = "Example Secret Vendor"
            expense.save()

            row = self.updated(expense)
        self.assertIn("Vendor", row.message)
        self.assertNotIn("Example", row.message)

    def test_what_the_saver_said_is_the_message(self):
        expense = Expense.objects.get(pk=self.an_expense().pk)
        expense.item = "Example server"
        expense.audit_said = "Renamed for the example."
        expense.save()

        self.assertEqual(self.updated(expense).message, "Renamed for the example.")

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


def title_of(operation):
    # Imported where it runs, so this module imports at a base without it.
    from hq.platform.core.audit_words import title_of as titled

    return titled(operation)


def words_of(row):
    # Imported where it runs, so this module imports at a base without it.
    from hq.platform.core.audit_words import words_of as said

    return said(row)


class StoredRowTests(TestCase):
    def older(self, *, operation: str, message: str = "", changes=None, object_repr: str = "") -> AuditLog:
        """A row as HQ stored it before rows said anything, written past the audit signals."""

        with operation_context(interface="controller", actor="controller", operation=operation):
            return AuditLog.objects.create(
                action=AuditLog.Action.UPDATED,
                object_type=audited_labels()[OperationRequest],
                object_id="example-operation",
                object_repr=object_repr,
                message=message,
                metadata={"operation": operation, **({"changes": changes} if changes else {})},
            )

    def test_a_row_written_before_updates_said_anything_reads_as_a_new_one(self):
        row = self.older(
            operation="bridge.claim",
            changes={"state": ["queued", "claimed"]},
            object_repr="example-rewrite: reconcile (claimed)",
        )

        said = words_of(row)
        self.assertEqual(said.title, title_of("bridge.claim"))
        self.assertNotIn("claimed)", said.title)
        self.assertIn("claimed", said.message)

    def test_a_row_s_own_message_is_kept(self):
        row = self.older(operation="bridge.report", message="Rewrite applied.", changes={"state": ["a", "b"]})

        self.assertEqual(words_of(row).message, "Rewrite applied.")

    def test_a_held_change_is_titled_by_what_it_does_whoever_asked(self):
        row = self.older(
            operation="infrastructure.resource.remove", object_repr="Remove a record on example-dns by example-agent"
        )

        title = words_of(row).title
        self.assertTrue(title)
        self.assertNotIn("example-agent", title)


class ApprovalTitleTests(TestCase):
    def test_a_held_change_is_named_by_its_record_s_name_never_its_key_or_who_asked(self):
        held = ApprovalRequest(
            capability="infrastructure.resource.remove",
            target="17",
            resource_key="example-dns-17",
            baseline={"name": "Example DNS record"},
            requested_actor="example-agent",
            requested_interface="mcp",
        )

        self.assertIn("Example DNS record", str(held))
        self.assertNotIn("example-dns-17", str(held))
        self.assertNotIn("example-agent", str(held))

    def test_a_held_run_is_named_by_the_machines_it_runs_on_never_its_record_key(self):
        held = ApprovalRequest(
            capability="automation.example.check.run",
            resource_kind="automation.operation",
            resource_key="automation-example-check",
            baseline={"spec": {"operation": "example.check"}, "hosts": ["app.example.com"]},
        )

        self.assertIn("app.example.com", str(held))
        self.assertNotIn("automation-example-check", str(held))

    def test_a_held_change_with_no_target_names_no_key(self):
        held = ApprovalRequest(capability="automation.source.adopt", resource_key="automation-source")

        self.assertTrue(str(held))
        self.assertNotIn("automation-source", str(held))
