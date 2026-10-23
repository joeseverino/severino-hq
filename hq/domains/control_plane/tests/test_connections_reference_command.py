"""`manage.py connections_reference --check` fails on a stale or missing reference, and writes nothing."""

import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase

from hq.domains.control_plane.management.commands import connections_reference as command
from hq.platform.application.connection_setup import reference_markdown


class CheckTests(SimpleTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name, "Connections.md")
        patcher = patch.object(command, "reference_path", lambda: self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def check(self) -> None:
        call_command("connections_reference", "--check", stdout=StringIO())

    def test_the_current_reference_passes(self):
        self.path.write_text(reference_markdown(), encoding="utf-8")
        self.check()

    def test_a_stale_reference_fails_and_is_left_as_it_was(self):
        current = reference_markdown()
        # The reference before its last section was declared.
        stale = current[: current.rindex("\n## ")] + "\n"
        self.path.write_text(stale, encoding="utf-8")

        with self.assertRaises(CommandError):
            self.check()
        self.assertEqual(self.path.read_text(encoding="utf-8"), stale)

    def test_a_missing_reference_fails(self):
        with self.assertRaises(CommandError):
            self.check()
        self.assertFalse(self.path.exists())
