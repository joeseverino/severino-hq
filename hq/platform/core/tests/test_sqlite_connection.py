"""Every HQ connection is in WAL mode, from the settings' init_command."""

import re
import sqlite3
import tempfile
from pathlib import Path

from django.conf import settings
from django.db import connection
from django.db.utils import load_backend
from django.test import SimpleTestCase, tag


def opened(path: Path, **options: object):
    """A new connection to ``path`` through HQ's own database settings."""

    declared = connection.settings_dict
    wrapper = load_backend(declared["ENGINE"]).DatabaseWrapper
    return wrapper({**declared, "NAME": str(path), "OPTIONS": {**declared["OPTIONS"], **options}}, "opened")


class JournalModeTests(SimpleTestCase):
    def test_a_new_file_is_wal_from_its_first_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            fresh = opened(Path(directory) / "hq.sqlite3")
            try:
                with fresh.cursor() as cursor:
                    cursor.execute("PRAGMA journal_mode")
                    self.assertEqual(cursor.fetchone()[0], "wal")
            finally:
                fresh.close()

    def test_opening_a_wal_database_takes_no_lock(self):
        # A running host: the file is WAL and a writer holds the lock. A new
        # connection that will not wait at all still opens it.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hq.sqlite3"
            writer = sqlite3.connect(path, isolation_level=None)
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("BEGIN IMMEDIATE")
            reader = opened(path, timeout=0)
            try:
                with reader.cursor() as cursor:
                    cursor.execute("PRAGMA journal_mode")
                    self.assertEqual(cursor.fetchone()[0], "wal")
            finally:
                reader.close()
                writer.close()


@tag("source")
class CreatorTests(SimpleTestCase):
    """One process creates the database file before any other opens it.

    The first ``journal_mode=WAL`` on a file writes its header, and SQLite
    never waits for that lock upgrade (``sqlite3_busy_handler``), so processes
    opening one new file together can fail with "database is locked". Each
    launcher migrates in the foreground and stops on a failure before it
    starts the server, which finds the file already WAL.
    """

    def assert_migrates_before_serving(self, script: str, migrates: str, serves: str) -> None:
        lines = (Path(settings.BASE_DIR) / script).read_text().splitlines()

        def first(pattern: str) -> int:
            at = next((i for i, line in enumerate(lines) if re.match(pattern, line)), None)
            self.assertIsNotNone(at, f"{script} has no line matching {pattern}")
            return at

        self.assertLess(first(r"set -e"), first(migrates))
        self.assertLess(first(migrates), first(serves))

    def test_the_image_migrates_before_it_serves(self):
        self.assert_migrates_before_serving("entrypoint.sh", r"python manage\.py boot_migrate$", r'exec "\$@"$')
        self.assertIn('ENTRYPOINT ["/entrypoint.sh"]', (Path(settings.BASE_DIR) / "Dockerfile").read_text())

    def test_the_dev_server_migrates_before_it_serves(self):
        self.assert_migrates_before_serving(
            # A new scratch database is migrated by the seed that fills it.
            "tools/dev/dev.sh",
            r"\s*(\S+=\S+ )?uv run python manage\.py (migrate|seed)\b",
            r'exec "\$python" -m uvicorn\b',
        )
