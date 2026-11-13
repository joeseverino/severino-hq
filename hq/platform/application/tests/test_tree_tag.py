"""A source test that reads a whole tree carries the ``tree`` tag, and only one.

No import leads from a changed module to a test that finds it by walking a
directory, so ``mise run fast`` runs every ``tree`` test on any change, or,
where its class declares what it reads (``reads.py``), on a change to those
files. A walk is ``rglob``, ``os.walk``, ``git ls-files`` or an ``ast-grep``
command line that does not read ``--stdin``, in the test, its class's setup,
or any function or value of this checkout it reaches by name. A test that
patches the ROOT of the code it drives, itself or in its class's setup, walks
a fixture rather than this checkout, so it carries no ``tree`` tag.

A walk that parses the tree runs apart (``apart.py``), in a process that
exits, since a test worker never gives back what a parse took.
"""

import filecmp
import os
import subprocess
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from functools import partial
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from textwrap import dedent

from django.test import SimpleTestCase, tag

from hq.platform.core.tests.task import names

from . import tree
from .apart import apart
from .tree import ROOT, misplaced

HELPER = """
from pathlib import Path


def every_file(root):
    return sorted(Path(root).rglob("*.py"))


def listed():
    return ["git", "ls-files", "-z"]


def scanned():
    return ["ast-grep", "scan", "--json=stream", "."]


def piped():
    return ["ast-grep", "scan", "--stdin", "--json=compact"]
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
    class Scans(SimpleTestCase):
        def test_scans(self):
            helper.scanned()


    @tag("source")
    class ScansWhatItIsHanded(SimpleTestCase):
        def test_scans_its_input(self):
            helper.piped()


    @tag("source", "tree")
    class ScansTagged(SimpleTestCase):
        def test_scans(self):
            helper.scanned()


    @tag("source")
    class OnAFixtureByName(SimpleTestCase):
        @patch("tests.pkg.helper.ROOT", "fixture")
        def test_walks_the_fixture(self):
            listed()
"""


UNREAD_CASES = """
    import os
    from django.test import SimpleTestCase, tag
    from hq.platform.application.tests.reads import reads

    GLOB = "docs/**"


    @tag("source", "tree")
    @reads("templates/**", lists=("**",))
    class Declared(SimpleTestCase):
        def test_walks(self):
            os.walk(".")


    @tag("source", "tree")
    @reads(GLOB)
    class Built(SimpleTestCase):
        def test_walks(self):
            os.walk(".")


    @reads("templates/**")
    class Untagged(SimpleTestCase):
        def test_reads(self):
            open("a")


    @tag("source", "tree")
    class OnAMethod(SimpleTestCase):
        @reads("templates/**")
        def test_walks(self):
            os.walk(".")
"""


@contextmanager
def fixture(cases: str = CASES) -> Iterator[Path]:
    """A checkout holding ``cases`` and the helper they walk through."""

    with TemporaryDirectory() as directory:
        root = Path(directory)
        package = root / "tests" / "pkg"
        package.mkdir(parents=True)
        for name, text in (("__init__.py", ""), ("helper.py", HELPER), ("test_case.py", dedent(cases))):
            (package / name).write_text(text, encoding="utf-8")
        (root / "tests" / "__init__.py").write_text("")
        yield root


@tag("source", "tree")
class TreeTagTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # One walk of this checkout answers its tests.
        cls.untagged, cls.idle, cls.unread = misplaced(ROOT)

    def test_a_source_test_that_walks_a_tree_carries_the_tree_tag(self):
        self.assertEqual(self.untagged, [], "add tag 'tree': `mise run fast` runs these on every change")

    def test_a_tree_tag_marks_a_test_that_walks(self):
        self.assertEqual(self.idle, [], "drop tag 'tree': nothing here walks a tree")

    def test_fast_reads_every_declaration_of_what_a_tree_test_reads(self):
        self.assertEqual(self.unread, {}, "fast runs a tree test by its class's @reads, of string literals alone")

    def test_each_file_a_tree_test_declares_it_opens_is_here(self):
        from hq.platform.application.tests.tree import tree_tests

        files = [PurePosixPath(path) for path in names("ls-files", root=ROOT)]
        declared = {glob for _, held in tree_tests(ROOT) if held for glob in held[0]}
        self.assertTrue(declared, "no tree test declares what it reads, so this holds nothing")
        self.assertEqual(
            sorted(glob for glob in declared if not any(path.full_match(glob) for path in files)),
            [],
            "an opened glob that names no file reads nothing: correct it, or list it where the test waits for an add",
        )

    def test_a_declaration_fast_cannot_read_is_named(self):
        with fixture(UNREAD_CASES) as root:
            _, _, unread = misplaced(root)
            held = dict(tree.tree_tests(root))

        self.assertEqual(held["tests.pkg.test_case.Declared"], (("templates/**",), ("**",)))
        self.assertIsNone(held["tests.pkg.test_case.Built"])
        self.assertIsNone(held["tests.pkg.test_case.OnAMethod"])
        self.assertEqual(
            sorted(unread),
            [
                "tests.pkg.test_case.Built",
                "tests.pkg.test_case.OnAMethod.test_walks",
                "tests.pkg.test_case.Untagged",
            ],
        )

    def test_a_walk_is_found_through_a_helper_of_another_module_and_a_fixtures_walk_is_not_one(self):
        with fixture() as root:
            untagged, idle, _ = misplaced(root)

        self.assertEqual(
            sorted(untagged),
            [
                "tests.pkg.test_case.Direct.test_walks",
                "tests.pkg.test_case.Methods.test_by_import",
                "tests.pkg.test_case.Scans.test_scans",
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
                "tests.pkg.test_case.ScansTagged",
                "tests.pkg.test_case.Tagged",
            ],
        )


@tag("source")
class ReadsTests(SimpleTestCase):
    """A declared tree test fails on a read outside its declaration, however it reads."""

    def outside(self, declared, work) -> list[str]:
        """What a watch of ``declared`` names as outside it after ``work``, or [] where it held."""

        from hq.platform.application.tests.reads import Reads, watching

        try:
            with watching(Reads(*declared)):
                work()
        except AssertionError as failed:
            return [line.strip() for line in str(failed).splitlines()[1:]]
        return []

    def test_a_file_opened_or_listed_outside_the_declaration_is_named(self):
        docs = ROOT / "docs"
        cases = {
            "an open of a declared file": ((("docs/*.md",),), (docs / "Development.md").read_bytes, False),
            # The builtin, which Django's template loader calls, as well as the Path method.
            "an open of a listed one": (((), ("docs/*.md",)), lambda: open(docs / "Development.md").close(), True),  # noqa: PTH123
            "a listing of declared names": (((), ("docs/*",)), lambda: list(docs.iterdir()), False),
            "a listing past them": (((), ("docs/Development.md",)), lambda: list(docs.iterdir()), True),
            "a scandir past them": (((), ("docs/Development.md",)), lambda: list(os.scandir(docs)), True),
            "a stat past them": (((), ("docs/Development.md",)), (docs / "Architecture.md").exists, True),
            "a stat of a directory": (((),), docs.is_dir, False),
            "a look for a path an add would hold": (((), ("docs/*.md",)), (docs / "none").exists, True),
            "a look for one declared": (((), ("docs/none/**",)), (docs / "none" / "a").exists, False),
            "an open git ignores": (((),), (ROOT / ".venv" / "pyvenv.cfg").read_bytes, False),
        }
        for case, (declared, work, refused) in cases.items():
            with self.subTest(case):
                found = self.outside(declared, work)
                self.assertEqual(bool(found), refused, found)

    @tag("tree")
    def test_a_program_is_held_to_what_it_can_read(self):
        config = str(ROOT / "mise.toml")
        with TemporaryDirectory() as directory:
            cases = {
                "a list of every tracked path, declared so": (((), ("**",)), ["git", "ls-files", "-z"], ROOT, False),
                "a list of every tracked path, declared less": ((("docs/**",),), ["git", "ls-files", "-z"], ROOT, True),
                "a program in the checkout": ((("**",),), ["git", "status"], ROOT, True),
                "a program pointed at the checkout": ((("**",),), ["git", "-C", str(ROOT), "status"], directory, True),
                "a program in a fixture": (((),), ["git", "--version"], directory, False),
                "a program handed a path here": (((),), ["cat", config], directory, True),
                "a program handed a declared one": ((("mise.toml",),), ["cat", config], directory, False),
            }
            for case, (declared, argv, cwd, refused) in cases.items():
                with self.subTest(case):
                    run = partial(subprocess.run, argv, cwd=cwd, capture_output=True, check=False)
                    found = self.outside(declared, run)
                    self.assertEqual(bool(found), refused, found)

    def test_a_walk_run_apart_is_held_to_the_declaration_of_the_test_that_ran_it(self):
        from hq.platform.application.tests.reads import Reads, watching

        # filecmp reads both files through open, in the walk's own process.
        same = (ROOT / "mise.toml", ROOT / "mise.toml", False)
        with self.assertRaisesRegex(RuntimeError, "opens mise.toml"), watching(Reads(("docs/**",))):
            apart(filecmp.cmp, *same)
        with watching(Reads(("mise.toml",))):
            self.assertTrue(apart(filecmp.cmp, *same))

    def test_the_declaration_holds_the_class_fixtures_and_each_test(self):
        from hq.platform.application.tests.reads import reads

        @reads("docs/**")
        class Declared(SimpleTestCase):
            @classmethod
            def setUpClass(cls):
                super().setUpClass()
                (ROOT / "mise.toml").read_text()

            def test_reads_outside(self):
                (ROOT / "pyproject.toml").read_text()

        result = unittest.TestResult()
        unittest.defaultTestLoader.loadTestsFromTestCase(Declared).run(result)
        failed = "\n".join(text for _, text in result.errors + result.failures)
        self.assertIn("opens mise.toml", failed)
