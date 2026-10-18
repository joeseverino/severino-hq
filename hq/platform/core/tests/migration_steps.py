"""Run a data migration the way a deploy does: from the state before it, over rows that state holds."""

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

from hq.platform.core import revisions


class MigrationStepTestCase(TransactionTestCase):
    """Each ``migrate`` moves the test database to ``targets`` and returns that state's models.

    The revision triggers name a table an earlier state does not have, so they
    are lifted for the trip and put back with the schema.
    """

    def migrate(self, *targets: tuple[str, str]):
        with connection.cursor() as cursor:
            cursor.execute("SELECT name FROM sqlite_master WHERE type = 'trigger' AND name GLOB 'hq_revision_*'")
            for (name,) in cursor.fetchall():
                cursor.execute(f'DROP TRIGGER "{name}"')
        MigrationExecutor(connection).migrate(list(targets))
        return MigrationExecutor(connection).loader.project_state(list(targets)).apps

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        revisions.install()
