"""A boot that is about to change the schema keeps the database first, where the host says,
and a migrated boot asks for what a new image reads without depending on it."""

import sqlite3
import tempfile
from contextlib import closing
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.db import OperationalError
from django.test import TransactionTestCase

from hq.platform.core.management.commands import boot


class BootTests(TransactionTestCase):
    # A copy is made outside a transaction, as a boot makes it.
    def setUp(self):
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.kept = self.dir / "severino.sqlite3.before-migrate"
        self.migrate = self.enterContext(patch.object(boot, "call_command"))
        self.read = self.enterContext(patch.object(boot, "request_boot_reads", return_value=("github.delivery",)))

    def boot(self, *, pending: bool, named: str | None = None):
        environ = {"SEVERINO_SCHEMA_SNAPSHOT": str(self.kept) if named is None else named}
        with patch.dict("os.environ", environ), patch.object(boot, "pending", return_value=pending):
            call_command("boot", stdout=StringIO(), stderr=StringIO())

    def tables(self, path: Path) -> set[str]:
        with closing(sqlite3.connect(path)) as copy:
            return {name for (name,) in copy.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}

    def test_a_pending_migration_keeps_the_whole_database_first(self):
        self.boot(pending=True)

        self.assertIn("django_migrations", self.tables(self.kept))
        self.assertIn("jobs_job", self.tables(self.kept))
        self.migrate.assert_called_once_with("migrate", interactive=False, verbosity=1)
        self.assertFalse(self.kept.with_name(self.kept.name + ".partial").exists())

    def test_nothing_pending_keeps_nothing(self):
        self.boot(pending=False)

        self.assertFalse(self.kept.exists())
        self.migrate.assert_called_once()

    def test_a_copy_already_kept_is_the_one_the_release_found(self):
        self.kept.write_bytes(b"the database before the first attempt")

        self.boot(pending=True)

        self.assertEqual(self.kept.read_bytes(), b"the database before the first attempt")

    def test_a_host_that_names_no_path_is_only_migrated(self):
        self.boot(pending=True, named="")

        self.assertEqual(list(self.dir.iterdir()), [])
        self.migrate.assert_called_once()

    def test_a_copy_that_fails_migrates_nothing(self):
        with (
            patch.object(boot, "keep", side_effect=OperationalError("disk full")),
            self.assertRaises(OperationalError),
        ):
            self.boot(pending=True)

        self.migrate.assert_not_called()
        self.read.assert_not_called()

    def test_a_migrated_boot_asks_for_what_a_new_image_reads(self):
        self.read.side_effect = lambda: self.migrate.assert_called_once() or ("github.delivery",)

        self.boot(pending=False)

        self.read.assert_called_once_with()

    def test_a_failed_migration_asks_for_no_read_and_stops_the_boot(self):
        self.migrate.side_effect = OperationalError("database is locked")

        with self.assertRaises(OperationalError):
            self.boot(pending=False)

        self.read.assert_not_called()

    def test_a_read_that_fails_still_boots(self):
        self.read.side_effect = OperationalError("no such table")

        self.boot(pending=False)

        self.migrate.assert_called_once()

    def test_what_is_pending_is_read_from_the_migration_graph(self):
        self.assertFalse(boot.pending())

    def test_a_database_with_nothing_applied_has_nothing_to_keep(self):
        with patch("django.db.migrations.recorder.MigrationRecorder.applied_migrations", return_value={}):
            self.assertFalse(boot.pending())

    def test_an_unapplied_migration_on_a_standing_schema_is_pending(self):
        applied = boot.MigrationExecutor(boot.connection).loader.applied_migrations
        latest = max(key for key in applied if key[0] == "jobs")
        rest = {key: value for key, value in applied.items() if key != latest}
        with patch("django.db.migrations.recorder.MigrationRecorder.applied_migrations", return_value=rest):
            self.assertTrue(boot.pending())
