"""A dev:look run whose checkout changed while it ran is refused: any file git
does not ignore, tracked or not, changes the tree it began on, and nothing
the run itself writes under an ignored folder does."""

import subprocess
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
