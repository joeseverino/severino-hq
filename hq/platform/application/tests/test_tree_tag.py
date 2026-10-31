"""A source test that reads a whole tree carries the ``tree`` tag, and only one.

No import leads from a changed module to a test that finds it by walking a
directory, so ``mise run fast`` runs every ``tree`` test on any change to a
module under hq/ or hq_sdk/. A walk is ``rglob``, ``os.walk`` or ``git
ls-files``, in the test, its class's setup, or any function or value of this
checkout it reaches by name. A test that patches the ROOT of the code it drives,
itself or in its class's setup, walks a fixture rather than this checkout, so it
carries no ``tree`` tag.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from textwrap import dedent

from django.test import SimpleTestCase, tag

from . import tree
from .tree import ROOT, misplaced

HELPER = """
from pathlib import Path


def every_file(root):
    return sorted(Path(root).rglob("*.py"))


def listed():
    return ["git", "ls-files"]
"""


CASES = """
    import os
    from django.test import SimpleTestCase, tag
    from tests.pkg import helper
    from tests.pkg.helper import listed
    from unittest.mock import patch


    @tag("source")
    class Direct(SimpleTestCase):
        def test_walks(self):
            os.walk(".")

        def test_reads_one_file(self):
            open("a")


    @tag("source")
    class Through(SimpleTestCase):
        def setUp(self):
            self.files = helper.every_file(".")

        def test_anything(self):
            pass


    class Methods(SimpleTestCase):
        @tag("source")
        def test_by_import(self):
            listed()

        @tag("source", "tree")
        def test_tagged_without_a_walk(self):
            pass


    @tag("source", "tree")
    class Tagged(SimpleTestCase):
        def test_walks(self):
            self.walker()

        def walker(self):
            return list(Path(".").rglob("*"))


    @tag("source", "tree")
    class OnAFixture(SimpleTestCase):
        def setUp(self):
            patcher = patch.object(helper, "ROOT", "fixture")
            patcher.start()

        def test_walks_the_fixture(self):
            helper.every_file(helper.ROOT)


    @tag("source")
    class OnAFixtureByName(SimpleTestCase):
        @patch("tests.pkg.helper.ROOT", "fixture")
        def test_walks_the_fixture(self):
            listed()
"""


@contextmanager
def fixture() -> Iterator[Path]:
    """A checkout holding CASES and the helper they walk through."""

    with TemporaryDirectory() as directory:
        root = Path(directory)
        package = root / "tests" / "pkg"
        package.mkdir(parents=True)
        for name, text in (("__init__.py", ""), ("helper.py", HELPER), ("test_case.py", dedent(CASES))):
            (package / name).write_text(text, encoding="utf-8")
        (root / "tests" / "__init__.py").write_text("")
        yield root


@tag("source", "tree")
class TreeTagTests(SimpleTestCase):
    def test_a_source_test_that_walks_a_tree_carries_the_tree_tag(self):
        self.assertEqual(misplaced(ROOT)[0], [], "add tag 'tree': `mise run fast` runs these on every module change")

    def test_a_tree_tag_marks_a_test_that_walks(self):
        self.assertEqual(misplaced(ROOT)[1], [], "drop tag 'tree': nothing here walks a tree")

    def test_a_walk_is_found_through_a_helper_of_another_module_and_a_fixtures_walk_is_not_one(self):
        with fixture() as root:
            untagged, idle = misplaced(root)

        self.assertEqual(
            sorted(untagged),
            [
                "tests.pkg.test_case.Direct.test_walks",
                "tests.pkg.test_case.Methods.test_by_import",
                "tests.pkg.test_case.Through.test_anything",
            ],
        )
        self.assertEqual(
            idle, ["tests.pkg.test_case.Methods.test_tagged_without_a_walk", "tests.pkg.test_case.OnAFixture"]
        )

    def test_the_tagged_tests_are_named_by_class_when_the_whole_class_carries_the_tag(self):
        with fixture() as root:
            labels = tree.tagged(root, "tree")

        self.assertEqual(
            sorted(labels),
            [
                "tests.pkg.test_case.Methods.test_tagged_without_a_walk",
                "tests.pkg.test_case.OnAFixture",
                "tests.pkg.test_case.Tagged",
            ],
        )
