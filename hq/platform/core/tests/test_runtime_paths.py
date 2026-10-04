"""Changing local defaults must never make an existing database disappear."""

from pathlib import Path
from tempfile import TemporaryDirectory

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

from hq.config.paths import database_path


class RuntimePathTests(SimpleTestCase):
    def test_a_fresh_checkout_uses_var(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(database_path(root, None), root / "var/db/severino.sqlite3")

    def test_the_old_default_requires_an_explicit_choice(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / "data/severino.sqlite3"
            old.parent.mkdir()
            old.write_bytes(b"existing database")
            with self.assertRaisesMessage(ImproperlyConfigured, "SEVERINO_DATABASE_PATH"):
                database_path(root, None)
            self.assertEqual(old.read_bytes(), b"existing database")
            self.assertEqual(database_path(root, str(old)), old)

    def test_a_deployment_override_keeps_its_volume_path(self):
        with TemporaryDirectory() as directory:
            self.assertEqual(database_path(Path(directory), "/data/severino.sqlite3"), Path("/data/severino.sqlite3"))
