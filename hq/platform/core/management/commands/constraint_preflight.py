"""Count the rows each pending check constraint would refuse when it is added.

Adding a check constraint rebuilds the table on SQLite, and the rebuild fails
if one stored row breaks the rule. A migration may delete or rewrite those
rows first, so a row is only a refusal if it is still there when the
constraint is added.

The database is copied through SQLite's backup API from a read-only handle,
and the copy is migrated as ``migrate`` would migrate it: the same plan, every
operation run in order. Just before each ``AddConstraint`` of a check, the rows
its ``CHECK`` refuses are counted; a constraint with refused rows is not added
to the copy, so the run goes on and counts the rest. The original file is never
written, and the copy is deleted.

Exit status 1 when a pending constraint would be refused by a row.
"""

import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, override

from django.apps import apps
from django.core.management.base import BaseCommand, CommandError
from django.db import DEFAULT_DB_ALIAS, connection, connections, models
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.operations import AddConstraint
from django.db.utils import load_backend

from hq.platform.application.ui import counted

# Amounts of money, counted for the owner and held by no constraint: whether a
# negative amount means something (a refund) is not this command's to decide.
INFORMATIONAL = (
    ("assets.Asset", "total_cost"),
    ("expenses.Expense", "total_cost"),
    ("receipts.Receipt", "amount"),
)


@dataclass(frozen=True, slots=True)
class ConstraintCheck:
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


def _check(app: str, migration: str, position: int, operation: AddConstraint, model, editor) -> ConstraintCheck:
    return ConstraintCheck(
        app=app,
        migration=migration,
        position=position,
        name=operation.constraint.name,
        table=model._meta.db_table,
        key=model._meta.pk.column,
        # The SQL of the CHECK itself; Django has no public name for it.
        condition=operation.constraint._get_check_sql(model, editor),
    )


def _adds_a_check(operation) -> bool:
    return isinstance(operation, AddConstraint) and isinstance(operation.constraint, models.CheckConstraint)


def checks() -> Iterator[ConstraintCheck]:
    """Every check constraint a migration on disk adds, in migration order,
    stated against the models as they are now."""

    loader = MigrationLoader(None, ignore_no_migrations=True)
    editor = connection.SchemaEditorClass(connection, collect_sql=True)
    for app, name in sorted(loader.disk_migrations):
        migration = loader.disk_migrations[app, name]
        for position, operation in enumerate(migration.operations, start=1):
            if not _adds_a_check(operation):
                continue
            try:
                model = apps.get_model(app, operation.model_name)
            except LookupError:
                # A model a later migration removed has no table left to count.
                continue
            yield _check(app, name, position, operation, model, editor)


@dataclass(frozen=True, slots=True)
class Counted:
    check: ConstraintCheck
    count: int
    sample: list[object]


class _CountFirst(AddConstraint):
    """An ``AddConstraint`` that counts the rows its check refuses at the point
    in the plan where it runs, and adds the constraint only when there are none."""

    def __init__(self, operation: AddConstraint, migration: str, position: int, found: list[Counted]):
        super().__init__(operation.model_name, operation.constraint)
        self.migration = migration
        self.position = position
        self.found = found

    @override
    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        model = to_state.apps.get_model(app_label, self.model_name)
        check = _check(app_label, self.migration, self.position, self, model, schema_editor)
        with schema_editor.connection.cursor() as cursor:
            cursor.execute(check.counting)
            (count,) = cursor.fetchone()
            cursor.execute(check.sampling)
            sample = [row[0] for row in cursor.fetchall()]
        self.found.append(Counted(check, count, sample))
        if not count:
            super().database_forwards(app_label, schema_editor, from_state, to_state)


def _copy(path: Path, to: Path) -> None:
    source = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    target = sqlite3.connect(to)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


@contextmanager
def connected(path: Path) -> Iterator[None]:
    """The default database is the file at ``path`` while the block runs.

    A migration's ``RunPython`` writes through the default database, as at boot,
    so the copy has to be that database, not a second alias beside it."""

    settings = {**connections[DEFAULT_DB_ALIAS].settings_dict, "NAME": str(path)}
    replacement = load_backend(settings["ENGINE"]).DatabaseWrapper(settings, DEFAULT_DB_ALIAS)
    original = connections[DEFAULT_DB_ALIAS]
    connections[DEFAULT_DB_ALIAS] = replacement
    try:
        yield
    finally:
        replacement.close()
        connections[DEFAULT_DB_ALIAS] = original


def _migrate(found: list[Counted]) -> list[tuple[str, str]]:
    """Run the plan ``migrate`` runs with no arguments, counting before each
    added check, and name the migrations it applied."""

    copy = connections[DEFAULT_DB_ALIAS]
    copy.prepare_database()
    executor = MigrationExecutor(copy)
    executor.loader.check_consistent_history(copy)
    if conflicts := executor.loader.detect_conflicts():
        raise CommandError(f"Conflicting migrations: {conflicts}.")
    targets = executor.loader.graph.leaf_nodes()
    plan = executor.migration_plan(targets)
    for migration, _backwards in plan:
        # The loader gives each migration its own list of shared operations, so
        # swapping one in that list leaves the migration module as it was.
        for index, operation in enumerate(migration.operations):
            if _adds_a_check(operation):
                migration.operations[index] = _CountFirst(operation, migration.name, index + 1, found)
    state = executor._create_project_state(with_applied_migrations=True)
    try:
        executor.migrate(targets, plan=plan, state=state)
    except Exception as failed:
        raise CommandError(f"The pending migrations fail on this database: {failed}") from failed
    return [(migration.app_label, migration.name) for migration, _backwards in plan]


def _informational() -> Iterator[tuple[str, str]]:
    for label, column in INFORMATIONAL:
        table = apps.get_model(label)._meta.db_table
        yield (
            f"{table}.{column} below zero",
            f'SELECT COUNT(*) FROM "{table}" WHERE "{column}" < 0',
        )


class Command(BaseCommand):
    help = "Migrate a copy of the database and count the rows each added check constraint would refuse."
    requires_system_checks: ClassVar[list[str]] = []

    @override
    def add_arguments(self, parser):
        parser.add_argument(
            "--path",
            help="The SQLite database file to copy. Defaults to the configured database.",
        )
        parser.add_argument(
            "--sql",
            action="store_true",
            help="Print the counting statements and read nothing.",
        )

    @override
    def handle(self, *args, **options):
        if options["sql"]:
            self._print_sql()
            return
        path = Path(options["path"] or connection.settings_dict["NAME"])
        if not path.is_file():
            raise CommandError(f"{path} is not a database file.")
        found: list[Counted] = []
        with tempfile.TemporaryDirectory() as folder:
            copy = Path(folder) / path.name
            _copy(path, copy)
            with connected(copy):
                applied = _migrate(found)
                refused = self._report(applied, found)
        if refused:
            raise CommandError(
                f"{counted(refused, 'pending constraint', 'pending constraints')} would be refused by a stored row."
            )

    def _print_sql(self) -> None:
        for check in checks():
            self.stdout.write(f"-- {check.app}.{check.migration} #{check.position} {check.name}")
            self.stdout.write(f"{check.counting};")
        for label, statement in _informational():
            self.stdout.write(f"-- informational: {label}")
            self.stdout.write(f"{statement};")

    def _report(self, applied: list[tuple[str, str]], found: list[Counted]) -> int:
        self.stdout.write(f"{counted(len(applied), 'pending migration', 'pending migrations')} run on a copy")
        heading = None
        for item in found:
            check = item.check
            if heading != (check.app, check.migration):
                heading = (check.app, check.migration)
                self.stdout.write(f"{check.app}.{check.migration} (pending)")
            line = f"  #{check.position} {check.name}: {item.count} of {check.table} refused"
            if item.count:
                line += f" (for example {check.key} {', '.join(map(str, item.sample))})"
            self.stdout.write(line)
        self.stdout.write("informational (no constraint is added)")
        with connection.cursor() as cursor:
            for label, statement in _informational():
                cursor.execute(statement)
                (count,) = cursor.fetchone()
                self.stdout.write(f"  {label}: {count}")
        return sum(1 for item in found if item.count)
