"""A test the image ships that runs git carries the ``source`` tag.

No image ships git, so the composed suite, run as the image runs it, leaves
out ``source`` tests and ``lint:source`` runs them on the checkout. Running
git is a command line whose first item is ``"git"``, in the test, its class's
setup, or any function or value of this checkout it reaches by name.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from textwrap import dedent

from django.test import SimpleTestCase, tag

from .tree import ROOT, runs_git_untagged

HELPER = """
import subprocess


def head(root):
    return subprocess.run(["git", "-C", root, "rev-parse", "HEAD"], check=True)
"""


CASES = """
    import subprocess
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


    @tag("source")
    class Tagged(SimpleTestCase):
        def test_runs_git(self):
            helper.head(".")


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
    """A checkout whose image ships tests/ and leaves out tools/."""

    with TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "Dockerfile.dockerignore").write_text("*\n!tests/\n", encoding="utf-8")
        files = {
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
class GitTagTests(SimpleTestCase):
    def test_a_test_the_image_ships_that_runs_git_carries_the_source_tag(self):
        self.assertEqual(runs_git_untagged(ROOT), [], "add tag 'source': no image ships git")

    def test_git_is_found_through_a_helper_and_a_tagged_or_unshipped_test_is_not_named(self):
        with fixture() as root:
            untagged = runs_git_untagged(root)

        self.assertEqual(
            sorted(untagged),
            ["tests.pkg.test_case.Direct.test_runs_git", "tests.pkg.test_case.Through.test_anything"],
        )
