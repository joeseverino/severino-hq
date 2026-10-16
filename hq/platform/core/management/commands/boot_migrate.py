"""Migrate on boot, keeping the database first when the schema is about to change.

The host names where (``SEVERINO_SCHEMA_SNAPSHOT``), and a release that fails
puts that copy back before the previous image starts: an image only knows the
schema it was built with. A copy already there is the database as the release
found it, from an earlier attempt at this boot, so it is never replaced.
"""

import os
from pathlib import Path
from typing import Any, override

from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


def pending() -> bool:
    """Whether this boot changes a schema that exists: a database with nothing
    applied yet has nothing a previous release could run on."""

    executor = MigrationExecutor(connection)
    return bool(executor.loader.applied_migrations) and bool(
        executor.migration_plan(executor.loader.graph.leaf_nodes())
    )


def keep(target: Path) -> None:
    """A consistent copy of the database at ``target``, whole or not at all."""

    partial = target.with_name(target.name + ".partial")
    partial.unlink(missing_ok=True)
    # As the backup copies it: one statement, which refuses inside a
    # transaction rather than waiting on one.
    with connection.cursor() as cursor:
        cursor.execute("VACUUM INTO %s", [str(partial)])
    partial.replace(target)


class Command(BaseCommand):
    help = "Apply migrations, first keeping the database where the host asks when any are pending."

    @override
    def handle(self, *args: Any, **options: Any) -> None:
        named = os.environ.get("SEVERINO_SCHEMA_SNAPSHOT", "")
        if named and not Path(named).exists() and pending():
            keep(Path(named))
            self.stdout.write(f"Kept the database before migrating, at {named}.")
        call_command("migrate", interactive=False, verbosity=options["verbosity"])
