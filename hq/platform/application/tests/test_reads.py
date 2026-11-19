"""A step held to its @reads fails on a read outside it, by every way a test reads the checkout."""

import inspect
import io
import os
from pathlib import Path

from django.template.utils import get_app_template_dirs
from django.test import SimpleTestCase, tag

from hq.platform.application.tests import program_reads
from hq.platform.application.tests.reads import Reads, watching

# A file of the checkout, read by each way below.
FILE = Path(program_reads.__file__)
EVERYTHING_LISTED = Reads((), ("**",))


def _low_level_read() -> None:
    os.close(os.open(FILE, os.O_RDONLY))


# Each way to read the file's content, which a declaration that only lists it must not admit.
READS_CONTENT = {
    "Path.open": lambda: FILE.open(encoding="utf-8").close(),
    "Path.read_text": FILE.read_text,
    "io.open_code": lambda: io.open_code(str(FILE)).close(),
    "os.open": _low_level_read,
    "inspect.getsource": lambda: inspect.getsource(program_reads),
}


@tag("source")
class ReadsTests(SimpleTestCase):
    def assertNamed(self, declared: Reads, read, said: str) -> None:
        with self.assertRaisesRegex(AssertionError, said), watching(declared):
            read()

    def test_each_way_of_reading_a_file_outside_the_declaration_is_named(self):
        for name, read in READS_CONTENT.items():
            with self.subTest(name):
                self.assertNamed(EVERYTHING_LISTED, read, "opens hq/platform/application/tests/program_reads.py")

    def test_each_way_of_reading_a_file_the_declaration_opens_passes(self):
        for name, read in READS_CONTENT.items():
            with self.subTest(name), watching(Reads(("hq/platform/application/tests/*.py",))):
                read()

    def test_a_directory_opened_is_no_read_of_a_file(self):
        with watching(EVERYTHING_LISTED):
            os.close(os.open(FILE.parent, os.O_RDONLY))

    def test_what_django_finds_once_for_the_process_is_no_read_of_the_step_that_first_asks(self):
        get_app_template_dirs.cache_clear()
        with watching(Reads(())):
            get_app_template_dirs("templates")

    def test_a_program_started_outside_subprocess_is_named(self):
        for name, start in {
            "os.system": lambda: os.system("true"),  # noqa: S605 - the shell is the way of starting under test
            "os.posix_spawnp": lambda: os.waitpid(os.posix_spawnp("true", ["true"], os.environ), 0),
        }.items():
            with self.subTest(name):
                self.assertNamed(EVERYTHING_LISTED, start, "runs .*true.* here")
