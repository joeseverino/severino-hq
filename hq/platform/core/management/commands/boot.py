"""The container's boot before it serves, in one Django setup: migrate, then ask for what a new image reads.

Migrating keeps the database first when the schema is about to change. The
host names where (``SEVERINO_SCHEMA_SNAPSHOT``), and a release that fails puts
that copy back before the previous image starts: an image only knows the
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

from hq.platform.application.cadence import request_boot_reads


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
    help = "Apply migrations, keeping the database first where the host asks, then ask for what a new image reads."

    @override
    def handle(self, *args: Any, **options: Any) -> None:
        named = os.environ.get("SEVERINO_SCHEMA_SNAPSHOT", "")
        if named and not Path(named).exists() and pending():
            keep(Path(named))
            self.stdout.write(f"Kept the database before migrating, at {named}.")
        call_command("migrate", interactive=False, verbosity=options["verbosity"])
        # A new image is the moment production changes what it runs and what
        # a stored reading may hold, so those are read now rather than found
        # later. Best effort: serving never waits on the controller, and a
        # migrated database serves.
        try:
            asked = request_boot_reads()
        except Exception as error:  # noqa: BLE001 - any failure here leaves a boot that serves.
            self.stderr.write(f"Boot reads not requested: {error!r}")
            return
        self.stdout.write(f"Read requested: {', '.join(asked)}." if asked else "Nothing to read at boot.")
