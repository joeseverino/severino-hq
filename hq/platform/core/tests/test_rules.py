"""A row-level rule is declared once and held twice: by ``full_clean`` beside
its field, and by the database against every writer."""

import hashlib
import shutil
import tempfile
from datetime import date, time
from io import StringIO
from pathlib import Path

from django.apps import apps
from django.core.exceptions import ValidationError
from django.core.management import CommandError, call_command
from django.db import connection, models
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.operations import AddConstraint
from django.test import TestCase, TransactionTestCase

from hq.domains.calendars.models import Entry
from hq.domains.docs_index.models import DocumentationRecord
from hq.domains.jobs.models import Job
from hq.platform.core import revisions
from hq.platform.core.management.commands import constraint_preflight
from hq.platform.core.rules import Rule, one_of

DAY = date(2026, 10, 1)


class RuleTests(TestCase):
    def test_full_clean_reports_a_broken_rule_beside_its_field(self):
        cases = (
            ({"title": "  "}, "title", "Give the entry a title."),
            ({"ends_on": date(2026, 9, 1)}, "ends_on", "It cannot end before it starts."),
            ({"ends_at": time(10)}, "ends_at", "An end time needs a start time."),
            ({"starts_at": time(10), "ends_at": time(9)}, "ends_at", "It cannot end before it starts."),
            ({"repeat": "daily", "interval": 0}, "interval", "Repeat every 1 to 366."),
            ({"weekdays": "1"}, "weekdays", "Only a weekly entry names its days."),
            ({"repeat_until": date(2026, 12, 1)}, "repeat_until", "Only a repeating entry has an end."),
            (
                {"repeat": "daily", "repeat_until": date(2026, 9, 1)},
                "repeat_until",
                "It cannot stop repeating before it starts.",
            ),
        )
        for fields, field, message in cases:
            with self.subTest(fields=fields):
                with self.assertRaises(ValidationError) as raised:
                    Entry(**{"title": "Entry", "starts_on": DAY, **fields}).full_clean()
                self.assertEqual(raised.exception.message_dict, {field: [message]})

    def test_an_entry_that_keeps_every_rule_is_clean(self):
        Entry(
            title="Entry",
            starts_on=DAY,
            ends_on=date(2026, 10, 2),
            starts_at=time(22),
            ends_at=time(1),
            repeat="weekly",
            weekdays="0,3",
            repeat_until=date(2026, 12, 1),
        ).full_clean()

    def test_a_rule_survives_its_migration(self):
        rule = Rule(
            condition=models.Q(interval__gte=1),
            name="example_rule",
            violation_error_message="At least one.",
            field="interval",
        )
        path, args, kwargs = rule.deconstruct()

        self.assertEqual(path, "hq.platform.core.rules.Rule")
        self.assertEqual(Rule(*args, **kwargs), rule)
        self.assertNotEqual(Rule(*args, **{**kwargs, "field": "title"}), rule)

    def test_a_rule_says_what_it_requires(self):
        with self.assertRaisesRegex(ValueError, "says what it requires"):
            Rule(condition=models.Q(interval__gte=1), name="example_rule")

    def test_a_choice_rule_is_made_of_the_declared_values(self):
        rule = one_of("state", Job.State, "example_states")

        self.assertEqual(rule.condition, models.Q(state__in=Job.State.values))

    def test_every_check_constraint_says_what_it_requires(self):
        silent = [
            f"{model._meta.label}.{constraint.name}"
            for model in apps.get_models()
            if model.__module__.startswith("hq.")
            for constraint in model._meta.constraints
            if isinstance(constraint, models.CheckConstraint)
            and constraint.violation_error_message == constraint.default_violation_error_message
        ]

        self.assertEqual(silent, [])


def _rules() -> set[str]:
    """Every rule the host's models declare, by name."""

    return {
        constraint.name
        for model in apps.get_models()
        if model.__module__.startswith("hq.")
        for constraint in model._meta.constraints
        if isinstance(constraint, Rule)
    }


class PreflightTests(TestCase):
    """The count, on a migrated copy, of the rows each pending check refuses
    at the point in the plan where it is added."""

    BEFORE = ("docs_index", "0009_doc_type_queue_report")

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        folder = Path(tempfile.mkdtemp())
        cls.addClassCleanup(shutil.rmtree, folder)
        cls.base = folder / "base.sqlite3"
        with constraint_preflight.connected(cls.base):
            call_command("migrate", verbosity=0)
            call_command("migrate", *cls.BEFORE, verbosity=0)

    def database(self, *rows: dict[str, str]) -> Path:
        """A copy of the base, migrated to ``BEFORE``, holding ``rows``, which
        are written past the checks the table already has."""

        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder)
        path = folder / "copy.sqlite3"
        shutil.copyfile(self.base, path)
        with constraint_preflight.connected(path):
            with connection.cursor() as cursor:
                cursor.execute("PRAGMA ignore_check_constraints = ON")
            for index, row in enumerate(rows):
                DocumentationRecord.objects.create(doc_id=f"rb-{index}", title=f"Doc {index}", **row)
        return path

    def run_on(self, path: Path) -> str:
        out = StringIO()
        try:
            call_command("constraint_preflight", path=str(path), stdout=out)
        except CommandError as refused:
            return f"{out.getvalue()}{refused}"
        return out.getvalue()

    def test_a_row_an_earlier_operation_deletes_is_not_counted(self):
        path = self.database({"doc_type": "task", "status": "open"})
        before = hashlib.sha256(path.read_bytes()).hexdigest()

        report = self.run_on(path)

        self.assertIn("docs_index.0011_task_retired (pending)", report)
        self.assertIn("documentation_status_is_a_doc_status: 0 of docs_index_documentationrecord refused", report)
        self.assertNotIn("would be refused", report)
        # Reading a WAL database opens its shared-memory index; the data is the file.
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)
        with constraint_preflight.connected(path):
            self.assertTrue(DocumentationRecord.objects.filter(doc_type="task").exists())

    def test_a_row_nothing_deletes_is_counted_and_fails_the_run(self):
        report = self.run_on(self.database({"doc_type": "runbook", "status": "open"}))

        self.assertIn(
            "documentation_status_is_a_doc_status: 1 of docs_index_documentationrecord refused (for example id ",
            report,
        )
        self.assertIn("1 pending constraint would be refused by a stored row.", report)

    def test_a_database_with_nothing_pending_passes_and_counts_money(self):
        path = self.database()
        with constraint_preflight.connected(path):
            call_command("migrate", verbosity=0)

        report = self.run_on(path)

        self.assertIn("0 pending migrations run on a copy", report)
        self.assertIn("informational (no constraint is added)", report)
        self.assertIn("  assets_asset.total_cost below zero: 0", report)
        self.assertNotIn("would be refused", report)

    def test_it_lists_one_line_for_each_added_check_in_migration_order(self):
        loader = MigrationLoader(None, ignore_no_migrations=True)
        added = [
            (app, name, position, operation.constraint.name)
            for (app, name), migration in sorted(loader.disk_migrations.items())
            for position, operation in enumerate(migration.operations, start=1)
            if isinstance(operation, AddConstraint) and isinstance(operation.constraint, models.CheckConstraint)
        ]

        listed = [(check.app, check.migration, check.position, check.name) for check in constraint_preflight.checks()]

        # Every rule the models hold now, in migration order; a check whose
        # field a later migration removed has nothing left to count.
        self.assertEqual(listed, [check for check in added if check in listed])
        self.assertLessEqual(_rules(), {name for *_rest, name in listed})

    def test_the_statements_can_be_printed_and_run_by_hand(self):
        out = StringIO()
        call_command("constraint_preflight", sql=True, stdout=out)
        statements = [line for line in out.getvalue().splitlines() if not line.startswith("--")]

        self.assertEqual(len(statements), len(list(constraint_preflight.checks())) + 3)
        with connection.cursor() as cursor:
            for statement in statements:
                cursor.execute(statement)
                self.assertEqual(cursor.fetchone(), (0,), statement)


class RebuiltTableTests(TransactionTestCase):
    """Adding a constraint rebuilds the table, which drops its triggers; the
    hook that runs after every migrate puts them back."""

    def test_a_rebuilt_table_is_counted_again_after_migrate(self):
        table = Entry._meta.db_table
        with connection.schema_editor() as editor:
            editor._remake_table(Entry)
        with self.assertLogs("severino.revisions", "ERROR"):
            self.assertIsNone(revisions.read().of([table]))

        revisions.after_migrate(sender=None)

        before = revisions.read().of([table])
        Entry.objects.create(title="Entry", starts_on=DAY)
        self.assertEqual(revisions.read().of([table]), (before[0] + 1,))
