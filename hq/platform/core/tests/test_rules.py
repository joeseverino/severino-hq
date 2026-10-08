"""A row-level rule is declared once and held twice: by ``full_clean`` beside
its field, and by the database against every writer."""

import hashlib
import sqlite3
import tempfile
from datetime import date, time
from io import StringIO
from pathlib import Path

from django.apps import apps
from django.core.exceptions import ValidationError
from django.core.management import CommandError, call_command
from django.db import IntegrityError, connection, models, transaction
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.operations import AddConstraint
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from hq.domains.analytics.models import AnalyticsSite, RumDaily
from hq.domains.calendars.models import Entry
from hq.domains.control_plane.models import (
    ApprovalRequest,
    DashboardConfiguration,
    ManagedResource,
    OperationRequest,
)
from hq.domains.docs_index.models import DocumentationRecord
from hq.domains.jobs.models import Job
from hq.platform.core import revisions
from hq.platform.core.management.commands import constraint_preflight
from hq.platform.core.models import AgentAccess
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

    def test_the_database_refuses_a_row_written_around_full_clean(self):
        now = timezone.now()
        entry = Entry.objects.create(title="Entry", starts_on=DAY)
        resource = ManagedResource.objects.create(key="example", kind="adguard.rewrite", spec={})
        asked = {"resource": resource, "requested_actor": "someone", "requested_interface": "web"}
        queued = OperationRequest.objects.create(**asked, action="reconcile", idempotency_key="queued")
        pending = ApprovalRequest.objects.create(
            capability="example.change",
            content_fingerprint="0" * 64,
            requested_actor="example-agent",
            requested_interface="api",
            expires_at=now,
        )
        job = Job.objects.create(kind="example.work", label="Work")
        site = AnalyticsSite.objects.create(site_tag="tag", host="example.com")
        row = RumDaily.objects.create(site=site, date=DAY, dimension="path", value="/")
        runbook = DocumentationRecord.objects.create(doc_id="example-doc", title="A runbook")

        def changed(instance, **values):
            return lambda: type(instance).objects.filter(pk=instance.pk).update(**values)

        refused = {
            "calendar_entry_has_a_title": changed(entry, title=" "),
            "calendar_entry_ends_after_it_starts": changed(entry, ends_on=date(2026, 9, 1)),
            "calendar_entry_end_time_has_a_start": changed(entry, ends_at=time(9)),
            "calendar_entry_end_time_follows_start": changed(entry, starts_at=time(10), ends_at=time(9)),
            "calendar_entry_interval_in_range": changed(entry, interval=367),
            "calendar_entry_weekdays_only_weekly": changed(entry, weekdays="1"),
            "calendar_entry_until_only_repeating": changed(entry, repeat_until=date(2026, 12, 1)),
            "calendar_entry_until_after_it_starts": changed(entry, repeat="daily", repeat_until=date(2026, 9, 1)),
            "resource_generation_starts_at_one": changed(resource, generation=0, observed_generation=0),
            "resource_observed_no_later_than_declared": changed(resource, observed_generation=2),
            "operation_state_is_declared": changed(queued, state="paused"),
            "operation_claimed_names_its_claim": changed(queued, state="claimed"),
            "operation_queued_has_no_claim": changed(queued, claimed_by="example-controller"),
            "operation_finished_says_when": changed(queued, state="succeeded"),
            "approval_state_is_declared": changed(pending, state="maybe"),
            "approval_settled_says_when": changed(pending, state="expired"),
            "approval_decision_names_who_decided": changed(pending, state="approved", decided_at=now),
            "job_percent_at_most_100": changed(job, percent=101),
            "job_state_is_declared": changed(job, state="paused"),
            "analytics_rumdaily_sample_interval": changed(row, sample_interval=0),
            "documentation_status_fits_its_type": changed(runbook, status="open"),
            "agent_access_is_one_row": lambda: AgentAccess.objects.create(pk=2),
            "dashboard_configuration_is_one_row": lambda: DashboardConfiguration.objects.create(pk=2),
            "analytics_vitalsdaily_sample_interval": lambda: site.vitals.create(date=DAY, sample_interval=0),
        }

        for name, write in refused.items():
            with self.subTest(rule=name), self.assertRaisesRegex(IntegrityError, name), transaction.atomic():
                write()
        self.assertEqual(set(refused), _rules())

    def test_a_bulk_write_is_refused_too(self):
        with self.assertRaisesRegex(IntegrityError, "calendar_entry_interval_in_range"), transaction.atomic():
            Entry.objects.bulk_create([Entry(title="Entry", starts_on=DAY, interval=0)])

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
    """The count that is read before a constraint is added."""

    TABLES = (
        "CREATE TABLE calendars_entry (id integer PRIMARY KEY, title text, starts_on date, ends_on date,"
        " starts_at time, ends_at time, interval integer, weekdays text, repeat text, repeat_until date)",
        "CREATE TABLE jobs_job (id text PRIMARY KEY, percent integer, state text)",
        "CREATE TABLE assets_asset (id integer PRIMARY KEY, total_cost decimal, business_use_percentage integer)",
        "CREATE TABLE django_migrations (app text, name text)",
        "INSERT INTO calendars_entry VALUES (1, 'Kept', '2026-10-01', NULL, NULL, NULL, 1, '', '', NULL)",
        "INSERT INTO calendars_entry VALUES (2, 'Backwards', '2026-10-01', '2026-09-01', NULL, NULL, 1, '', '', NULL)",
        "INSERT INTO calendars_entry VALUES (3, 'Never', '2026-10-01', NULL, NULL, NULL, 0, '', '', NULL)",
        "INSERT INTO jobs_job VALUES ('a', 250, 'running')",
        "INSERT INTO assets_asset VALUES (1, -5, 50)",
    )

    def database(self, *more: str) -> Path:
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: [item.unlink() for item in folder.iterdir()] and folder.rmdir())
        path = folder / "copy.sqlite3"
        scratch = sqlite3.connect(path)
        for statement in (*self.TABLES, *more):
            scratch.execute(statement)
        scratch.commit()
        scratch.close()
        return path

    def run_on(self, path: Path) -> str:
        out = StringIO()
        try:
            call_command("constraint_preflight", path=str(path), stdout=out)
        except CommandError as refused:
            return f"{out.getvalue()}{refused}"
        return out.getvalue()

    def test_it_counts_the_rows_each_pending_constraint_would_refuse(self):
        report = self.run_on(self.database())

        self.assertIn("calendars.0002_rules (pending)", report)
        self.assertIn("  #1 calendar_entry_has_a_title: 0 of calendars_entry refused", report)
        self.assertIn(
            "  #2 calendar_entry_ends_after_it_starts: 1 of calendars_entry refused (for example id 2)",
            report,
        )
        self.assertIn("  #5 calendar_entry_interval_in_range: 1 of calendars_entry refused (for example id 3)", report)
        self.assertIn("  #1 job_percent_at_most_100: 1 of jobs_job refused (for example id a)", report)
        self.assertIn("  #7 operation_state_is_declared: no table control_plane_operationrequest", report)
        self.assertIn("3 pending constraints would be refused by a stored row.", report)

    def test_money_below_zero_is_counted_and_refuses_nothing(self):
        report = self.run_on(self.database("DELETE FROM calendars_entry WHERE id > 1", "DELETE FROM jobs_job"))

        self.assertIn("informational (no constraint is added)", report)
        self.assertIn("  assets_asset.total_cost below zero: 1", report)
        self.assertIn("  receipts_receipt.amount below zero: no table", report)
        self.assertNotIn("would be refused", report)

    def test_a_constraint_already_applied_does_not_fail_the_run(self):
        applied = "INSERT INTO django_migrations VALUES ('calendars', '0002_rules'), ('jobs', '0002_rules')"

        report = self.run_on(self.database(applied))

        self.assertIn("calendars.0002_rules (applied)", report)
        self.assertNotIn("would be refused", report)

    def test_it_changes_nothing_in_the_database_it_reads(self):
        path = self.database()
        before = hashlib.sha256(path.read_bytes()).hexdigest()

        self.run_on(path)

        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)
        self.assertEqual(sorted(item.name for item in path.parent.iterdir()), ["copy.sqlite3"])

    def test_it_lists_one_line_for_each_added_check_in_migration_order(self):
        loader = MigrationLoader(None, ignore_no_migrations=True)
        added = [
            (app, name, position, operation.constraint.name)
            for (app, name), migration in sorted(loader.disk_migrations.items())
            for position, operation in enumerate(migration.operations, start=1)
            if isinstance(operation, AddConstraint) and isinstance(operation.constraint, models.CheckConstraint)
        ]

        listed = [(check.app, check.migration, check.position, check.name) for check in constraint_preflight.checks()]

        self.assertEqual(listed, added)
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
