"""A dev:look run whose checkout changed while it ran is refused: any file git
does not ignore, tracked or not, changes the tree it began on, and nothing
the run itself writes under an ignored folder does. A look against a base
draws there with the base's own copy of a test file this tree's does not
import against."""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from django.test import SimpleTestCase, tag

from hq.platform.core.tests import look


# git runs here, and no image ships it.
@tag("source")
class TreeChangedTests(SimpleTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        subprocess.run(["git", "init", "--quiet", str(self.root)], check=True)
        (self.root / ".gitignore").write_text("var/\n", encoding="utf-8")
        (self.root / "page.html").write_text("<p>Example</p>\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "--all"], check=True)
        self.began = look.tree_of(self.root)

    def test_a_file_changed_or_added_while_the_run_ran_refuses_it(self):
        for change in ("page.html", "untracked.css"):
            with self.subTest(change=change):
                path = self.root / change
                was = path.read_text(encoding="utf-8") if path.exists() else None
                path.write_text("p { color: red; }\n", encoding="utf-8")
                self.assertTrue(look.changed_since(self.began, self.root))
                if was is None:
                    path.unlink()
                else:
                    path.write_text(was, encoding="utf-8")
                self.assertFalse(look.changed_since(self.began, self.root))

    def test_what_the_run_writes_where_git_ignores_it_changes_nothing(self):
        (self.root / "var" / "look").mkdir(parents=True)
        (self.root / "var" / "look" / "page-820.png").write_bytes(b"example")

        self.assertFalse(look.changed_since(self.began, self.root))

    def test_the_checkouts_own_index_is_left_as_it_was(self):
        index = self.root / ".git" / "index"
        before = index.read_bytes()
        (self.root / "untracked.css").write_text("p {}\n", encoding="utf-8")

        look.tree_of(self.root)

        self.assertEqual(index.read_bytes(), before)


# git runs here, and no image ships it.
@tag("source")
class KeptOwnTests(SimpleTestCase):
    """A file laid over a base that names what the base lacks is put back as
    the base has it, so the base still draws."""

    def write(self, path, text):
        (self.root / path).write_text(text, encoding="utf-8")

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        subprocess.run(["git", "init", "--quiet", str(self.root)], check=True)
        self.write("seeds.py", "SERVER = 1\n")
        self.write("walk.py", "lazy from seeds import SERVER\n")
        self.write("chain.py", "from walk import SERVER\n")
        self.write("fine.py", "lazy from seeds import SERVER\n")
        subprocess.run(["git", "-C", str(self.root), "add", "--all"], check=True)
        identity = ("user.name=Example", "user.email=example@example.com", "commit.gpgsign=false")
        configured = [part for setting in identity for part in ("-c", setting)]
        subprocess.run(
            ["git", "-C", str(self.root), *configured, "commit", "--quiet", "--no-verify", "-m", "base"], check=True
        )

    def kept(self, laid):
        env = {name: value for name, value in os.environ.items() if name != "PYTHONPATH"}
        return look.kept_own(self.root, "HEAD", laid, [sys.executable], env)

    def test_a_file_naming_a_seed_the_base_lacks_is_the_bases_own_and_what_imports_it_follows(self):
        # This tree's walk names a seed the base lacks, lazily as the estate does,
        # and its chain names what only this tree's walk holds.
        self.write("walk.py", "lazy from seeds import SERVER, SWITCH\nWALKED = 1\n")
        self.write("chain.py", "from walk import WALKED\n")
        self.write("fine.py", "lazy from seeds import SERVER\nFINE = 1\n")
        self.write("new.py", "lazy from seeds import SWITCH\n")

        self.assertEqual(self.kept(["chain.py", "fine.py", "new.py", "walk.py"]), ["chain.py", "walk.py"])
        self.assertEqual((self.root / "walk.py").read_text(encoding="utf-8"), "lazy from seeds import SERVER\n")
        self.assertIn("FINE", (self.root / "fine.py").read_text(encoding="utf-8"))

    def test_files_that_import_at_the_base_stay_as_this_tree_has_them(self):
        self.write("walk.py", "lazy from seeds import SERVER\nWALKED = 1\n")

        self.assertEqual(self.kept(["walk.py", "seeds.py"]), [])
        self.assertIn("WALKED", (self.root / "walk.py").read_text(encoding="utf-8"))
