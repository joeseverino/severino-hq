"""A test the image ships that runs a program the image lacks carries the ``source`` tag.

The image ships Python and the base system, never git or a tool mise pins
(ast-grep, uv, jq), so the composed suite, run as the image runs it, leaves
out ``source`` tests and ``lint:source`` runs them on the checkout. Running
one is a command line whose first item names it, in the test, its class's
setup, or any function or value of this checkout it reaches by name. A test
skipped by a ``skipIf`` or ``skipUnless`` that asks ``shutil.which`` for the
program skips in the image, so it needs no tag.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from textwrap import dedent

from django.test import SimpleTestCase, tag

from .tree import ROOT, runs_unshipped_untagged

HELPER = """
import shutil
import subprocess


def head(root):
    return subprocess.run(["git", "-C", root, "rev-parse", "HEAD"], check=True)


def scan(root):
    return subprocess.run(["ast-grep", "scan", "--stdin"], cwd=root, check=True)


def search(root):
    return subprocess.run(["rg", "x"], cwd=root, check=True)


def can_scan():
    return shutil.which("ast-grep") is not None
"""


CASES = """
    import subprocess
    from unittest import skipUnless
    from django.test import SimpleTestCase, tag
    from tests.pkg import helper


    class Direct(SimpleTestCase):
        def test_runs_git(self):
            subprocess.run(("git", "init"), check=True)

        def test_runs_something_else(self):
            subprocess.run(["true"], check=True)


    class Through(SimpleTestCase):
        def setUp(self):
            helper.head(".")

        def test_anything(self):
            pass


    class Pinned(SimpleTestCase):
        def test_scans(self):
            helper.scan(".")

        def test_runs_a_program_mise_does_not_pin(self):
            helper.search(".")


    @skipUnless(helper.can_scan(), "no ast-grep")
    class SkippedWithoutIt(SimpleTestCase):
        def test_scans(self):
            helper.scan(".")


    class SkippedForAnotherReason(SimpleTestCase):
        @skipUnless(HYPOTHESIS, "hypothesis is a development tool")
        def test_scans(self):
            helper.scan(".")


    @tag("source")
    class Tagged(SimpleTestCase):
        def test_runs_git(self):
            helper.head(".")

        def test_scans(self):
            helper.scan(".")


    class Methods(SimpleTestCase):
        @tag("source")
        def test_tagged(self):
            helper.head(".")
"""

UNSHIPPED = """
    import subprocess
    from django.test import SimpleTestCase


    class Unshipped(SimpleTestCase):
        def test_runs_git(self):
            subprocess.run(["git", "init"], check=True)
"""


@contextmanager
def fixture() -> Iterator[Path]:
    """A checkout whose image ships tests/ and leaves out tools/, with mise pinning ast-grep."""

    with TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "Dockerfile.dockerignore").write_text("*\n!tests/\n", encoding="utf-8")
        files = {
            "mise.toml": '[tools]\nast-grep = "1"\n"aqua:example/lint" = "1"\n',
            "tests/__init__.py": "",
            "tests/pkg/__init__.py": "",
            "tests/pkg/helper.py": HELPER,
            "tests/pkg/test_case.py": dedent(CASES),
            "tools/__init__.py": "",
            "tools/test_unshipped.py": dedent(UNSHIPPED),
        }
        for name, text in files.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text(text, encoding="utf-8")
        yield root


@tag("source", "tree")
class UnshippedTagTests(SimpleTestCase):
    def test_a_test_the_image_ships_that_runs_a_program_the_image_lacks_carries_the_source_tag(self):
        self.assertEqual(
            runs_unshipped_untagged(ROOT), [], "add tag 'source': the image ships neither git nor a tool mise pins"
        )

    def test_a_program_is_found_through_a_helper_and_a_tagged_unshipped_or_skipped_test_is_not_named(self):
        with fixture() as root:
            untagged = runs_unshipped_untagged(root)

        self.assertEqual(
            sorted(untagged),
            [
                "tests.pkg.test_case.Direct.test_runs_git",
                "tests.pkg.test_case.Pinned.test_scans",
                "tests.pkg.test_case.SkippedForAnotherReason.test_scans",
                "tests.pkg.test_case.Through.test_anything",
            ],
        )
