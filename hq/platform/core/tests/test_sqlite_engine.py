"""Every HQ connection is in WAL mode, however many processes open the file at once."""

import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

from django.conf import settings
from django.db import connection
from django.test import SimpleTestCase

from hq.platform.core.sqlite.base import DatabaseWrapper

PROCESSES = 6
# A new file per round; without the wait nearly every round fails.
ROUNDS = 30

# One process: for each path it reads, a write through HQ's own settings, then
# the journal mode the file ended up in, or the error.
OPENER = """
import sys
import django
from django.db import OperationalError, connection, transaction

django.setup()
print("ready", flush=True)
for line in sys.stdin:
    connection.settings_dict["NAME"] = line.strip()
    try:
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("CREATE TABLE IF NOT EXISTS opened (n)")
            cursor.execute("INSERT INTO opened VALUES (1)")
            cursor.execute("PRAGMA journal_mode")
            print(cursor.fetchone()[0], flush=True)
    except OperationalError as error:
        print(error, flush=True)
    finally:
        connection.close()
"""


class NewFileTests(SimpleTestCase):
    def test_processes_opening_one_new_file_together_all_write_to_it_in_wal(self):
        openers = [
            subprocess.Popen(
                [sys.executable, "-c", OPENER],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                text=True,
                cwd=settings.BASE_DIR,
            )
            for _ in range(PROCESSES)
        ]
        try:
            for opener in openers:
                self.assertEqual(opener.stdout.readline().strip(), "ready")
            with tempfile.TemporaryDirectory() as directory:
                for round_ in range(ROUNDS):
                    for opener in openers:
                        opener.stdin.write(f"{Path(directory) / f'{round_}.sqlite3'}\n")
                        opener.stdin.flush()
                    answers = [opener.stdout.readline().strip() for opener in openers]
                    self.assertEqual(answers, ["wal"] * PROCESSES, f"round {round_}")
        finally:
            for opener in openers:
                opener.stdin.close()
                opener.wait()
                opener.stdout.close()


class ExistingFileTests(SimpleTestCase):
    def test_opening_a_wal_database_takes_no_lock(self):
        # A running host: the file is WAL and a writer holds the lock. A new
        # connection that will not wait at all still opens it.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hq.sqlite3"
            writer = sqlite3.connect(path, isolation_level=None)
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("BEGIN IMMEDIATE")
            options = {**connection.settings_dict["OPTIONS"], "timeout": 0}
            opened = DatabaseWrapper({**connection.settings_dict, "NAME": str(path), "OPTIONS": options}, "opened")
            try:
                with opened.cursor() as cursor:
                    cursor.execute("PRAGMA journal_mode")
                    self.assertEqual(cursor.fetchone()[0], "wal")
            finally:
                opened.close()
                writer.close()
