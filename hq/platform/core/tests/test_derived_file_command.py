"""A derived file's command writes it, and with --check fails while the
committed copy is behind, writing nothing (``DerivedFileCommand``)."""

import tempfile
from io import StringIO
from pathlib import Path
from typing import override

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase

from hq.platform.core.management.derived import DerivedFileCommand

DERIVED = "# Example\n\nWhat the declarations say now.\n"


class Example(DerivedFileCommand):
    source = "the example declarations"

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path

    @override
    def target(self) -> Path:
        return self.path

    @override
    def derive(self) -> str:
        return DERIVED


class DerivedFileCommandTests(SimpleTestCase):
    def test_check_fails_while_the_file_is_behind_and_passes_once_it_is_written(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name, "Example.md")
        # The command is named for its module, as manage.py runs it.
        run = f"mise run manage -- {__name__.rpartition('.')[2]}"

        for state, committed in (("missing", None), ("stale", DERIVED.replace("now", "once"))):
            with self.subTest(state):
                if committed is not None:
                    path.write_text(committed, encoding="utf-8")

                with self.assertRaises(CommandError) as raised:
                    call_command(Example(path), "--check", stdout=StringIO())

                self.assertIn(run, str(raised.exception))
                self.assertIn("the example declarations", str(raised.exception))
                self.assertEqual(path.read_text(encoding="utf-8") if path.exists() else None, committed)

        call_command(Example(path), stdout=StringIO())
        self.assertEqual(path.read_text(encoding="utf-8"), DERIVED)
        call_command(Example(path), "--check", stdout=StringIO())
