"""Count the rows each check constraint a migration adds would refuse.

Adding a check constraint rebuilds the table on SQLite, and the rebuild fails
if one stored row breaks the rule. This counts those rows first.

It reads only. The database file is opened read-only through SQLite's own URI
mode, apart from Django's connection, so no pragma is set, no migration runs
and nothing is written; a database that has not been migrated to this code is
read as it stands. The constraints come from the migration files on disk, one
line per ``AddConstraint`` operation in the order the migration runs them, and
each is counted with the SQL the migration would put in the ``CHECK``.

Exit status 1 when a migration not yet applied would be refused by a row.
"""

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from django.apps import apps
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, models
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.operations import AddConstraint

from hq.platform.application.ui import counted

# Amounts of money, counted for the owner and held by no constraint: whether a
# negative amount means something (a refund) is not this command's to decide.
INFORMATIONAL = (
    ("assets.Asset", "total_cost"),
    ("expenses.Expense", "total_cost"),
    ("receipts.Receipt", "amount"),
)


@dataclass(frozen=True)
class Check:
    """One ``AddConstraint`` of a check constraint, as its migration states it."""

    app: str
    migration: str
    position: int
    name: str
    table: str
    key: str
    condition: str

    @property
    def counting(self) -> str:
        # A check passes when it is true or unknown, so a row is refused only
        # where the condition is false.
        return f'SELECT COUNT(*) FROM "{self.table}" WHERE NOT ({self.condition})'

    @property
    def sampling(self) -> str:
        return f'SELECT "{self.key}" FROM "{self.table}" WHERE NOT ({self.condition}) LIMIT 5'


def checks() -> Iterator[Check]:
    """Every check constraint a migration on disk adds, in migration order."""

    loader = MigrationLoader(None, ignore_no_migrations=True)
    editor = connection.SchemaEditorClass(connection, collect_sql=True)
    for app, name in sorted(loader.disk_migrations):
        migration = loader.disk_migrations[app, name]
        for position, operation in enumerate(migration.operations, start=1):
            if not isinstance(operation, AddConstraint):
                continue
            if not isinstance(operation.constraint, models.CheckConstraint):
                continue
            try:
                model = apps.get_model(app, operation.model_name)
            except LookupError:
                # A model a later migration removed has no table left to count.
                continue
            yield Check(
                app=app,
                migration=name,
                position=position,
                name=operation.constraint.name,
                table=model._meta.db_table,
                key=model._meta.pk.column,
                # The SQL of the CHECK itself; Django has no public name for it.
                condition=operation.constraint._get_check_sql(model, editor),
            )


def _informational() -> Iterator[tuple[str, str]]:
    for label, column in INFORMATIONAL:
        table = apps.get_model(label)._meta.db_table
        yield (
            f"{table}.{column} below zero",
            f'SELECT COUNT(*) FROM "{table}" WHERE "{column}" < 0',
        )


class Command(BaseCommand):
    help = "Count, reading only, the rows each added check constraint would refuse."
    requires_system_checks: ClassVar[list[str]] = []

    def add_arguments(self, parser):
        parser.add_argument(
            "--path",
            help="The SQLite database file to read. Defaults to the configured database.",
        )
        parser.add_argument(
            "--sql",
            action="store_true",
            help="Print the counting statements and read nothing.",
        )

    def handle(self, *args, **options):
        found = list(checks())
        if options["sql"]:
            for check in found:
                self.stdout.write(f"-- {check.app}.{check.migration} #{check.position} {check.name}")
                self.stdout.write(f"{check.counting};")
            for label, statement in _informational():
                self.stdout.write(f"-- informational: {label}")
                self.stdout.write(f"{statement};")
            return
        path = Path(options["path"] or connection.settings_dict["NAME"])
        if not path.is_file():
            raise CommandError(f"{path} is not a database file.")
        database = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            refused = self._report(database, found)
        finally:
            database.close()
        if refused:
            raise CommandError(
                f"{counted(refused, 'pending constraint', 'pending constraints')} would be refused by a stored row."
            )

    def _report(self, database: sqlite3.Connection, found: list[Check]) -> int:
        applied = _applied(database)
        refused = 0
        heading = None
        for check in found:
            pending = (check.app, check.migration) not in applied
            if heading != (check.app, check.migration):
                heading = (check.app, check.migration)
                self.stdout.write(
                    f"{check.app}.{check.migration} ({'pending' if pending else 'applied'})"
                )
            count, sample = _count(database, check)
            if count is None:
                self.stdout.write(f"  #{check.position} {check.name}: no table {check.table}")
                continue
            line = f"  #{check.position} {check.name}: {count} of {check.table} refused"
            if count:
                line += f" (for example {check.key} {', '.join(map(str, sample))})"
                refused += pending
            self.stdout.write(line)
        self.stdout.write("informational (no constraint is added)")
        for label, statement in _informational():
            try:
                (count,) = database.execute(statement).fetchone()
            except sqlite3.OperationalError:
                count = "no table"
            self.stdout.write(f"  {label}: {count}")
        return refused


def _applied(database: sqlite3.Connection) -> set[tuple[str, str]]:
    try:
        return set(database.execute("SELECT app, name FROM django_migrations").fetchall())
    except sqlite3.OperationalError:
        return set()


def _count(database: sqlite3.Connection, check: Check) -> tuple[int | None, list[object]]:
    try:
        (count,) = database.execute(check.counting).fetchone()
        sample = [row[0] for row in database.execute(check.sampling)] if count else []
    except sqlite3.OperationalError:
        return None, []
    return count, sample
