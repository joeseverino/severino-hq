"""A dev:look run whose checkout changed while it ran says so last, naming
the files and the tree it began on, and reports what it found without failing
on it: any file a page can read that git does not ignore, tracked or not,
changes the tree it began on, and neither a doc nor what the run itself writes
under an ignored folder does. A look against a base draws there with the
base's own copy of a lens file this tree's does not run against, and a base
that cannot run even so is unfit, so the look goes on without it."""

import io
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase, override_settings, tag

from hq.platform.core.tests import look
from hq.platform.core.tests.test_look_survey_record import ASKED, _Surveyed


# git runs here, and no image ships it.
@tag("source")
class TreeChangedTests(SimpleTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root, self.store = Path(folder.name, "checkout"), Path(folder.name, "store")
        self.root.mkdir()
        subprocess.run(["git", "init", "--quiet", str(self.root)], check=True)
        (self.root / ".gitignore").write_text("var/\n", encoding="utf-8")
        (self.root / "page.html").write_text("<p>Example</p>\n", encoding="utf-8")
        (self.root / "docs").mkdir()
        (self.root / "docs" / "Example.md").write_text("Example\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "--all"], check=True)
        self.began = look.tree_of(self.root, self.store)

    def test_a_file_changed_or_added_while_the_run_ran_is_named(self):
        for change in ("page.html", "untracked.css"):
            with self.subTest(change=change):
                path = self.root / change
                was = path.read_text(encoding="utf-8") if path.exists() else None
                path.write_text("p { color: red; }\n", encoding="utf-8")
                self.assertEqual(look.changed_since(self.began, self.root, self.store), [change])
                if was is None:
                    path.unlink()
                else:
                    path.write_text(was, encoding="utf-8")
                self.assertEqual(look.changed_since(self.began, self.root, self.store), [])

    def test_a_file_no_page_reads_changes_nothing(self):
        (self.root / "docs" / "Example.md").write_text("Changed\n", encoding="utf-8")

        self.assertEqual(look.changed_since(self.began, self.root, self.store), [])

    def test_what_the_run_writes_where_git_ignores_it_changes_nothing(self):
        (self.root / "var" / "look").mkdir(parents=True)
        (self.root / "var" / "look" / "page-820.png").write_bytes(b"example")

        self.assertEqual(look.changed_since(self.began, self.root, self.store), [])

    def test_the_checkouts_own_index_is_left_as_it_was(self):
        index = self.root / ".git" / "index"
        before = index.read_bytes()
        (self.root / "untracked.css").write_text("p {}\n", encoding="utf-8")

        look.tree_of(self.root)
        look.changed_since(self.began, self.root, self.store)

        self.assertEqual(index.read_bytes(), before)


@tag("source")
class ChangedAtCloseTests(SimpleTestCase):
    """A look whose tree changed reports what it found as found and fails
    only on that, with a last line naming the files and the tree it began on."""

    def close(self, found, moved):
        with (
            tempfile.TemporaryDirectory() as folder,
            override_settings(BASE_DIR=folder),
            mock.patch.object(look, "this_run", lambda: Path(folder)),
            mock.patch.object(look, "changed_since", lambda *_: moved),
            mock.patch.dict(os.environ, ASKED, clear=True),
            mock.patch("sys.stdout", new_callable=io.StringIO) as said,
        ):
            failed = _Surveyed(found).close("survey", found)
        return failed, said.getvalue().splitlines()

    def test_a_change_mid_run_is_said_last_and_fails_nothing_that_held(self):
        held = {("example/one", 402): []}
        failed, lines = self.close(held, ["static/css/example.css"])

        self.assertEqual(failed, [], "what held still holds; the gate keeps no pass of a tree that moved")
        self.assertIn("static/css/example.css", lines[-1])
        self.assertIn("example-tree", lines[-1], "the tree the findings read")

    def test_what_a_run_found_still_fails_it_when_the_tree_changed(self):
        found = {("example/one", 402): ["overflow: a box past the edge"]}
        failed, _ = self.close(found, ["static/css/example.css"])

        self.assertTrue(any("overflow: a box past the edge" in line for line in failed), failed)
        self.assertFalse(any("static/css/example.css" in line for line in failed), "the change is said, not failed")

    def test_a_long_list_of_changes_is_named_in_part(self):
        moved = [f"static/css/example-{n}.css" for n in range(12)]

        said = look.tree_changed("example-tree", moved)

        self.assertIn("static/css/example-0.css", said)
        self.assertNotIn("static/css/example-11.css", said)
        self.assertIn("7 more", said)


# git runs here, and no image ships it.
@tag("source")
class KeptOwnTests(SimpleTestCase):
    """A file laid over a base that names what the base lacks is put back as
    the base has it, or taken out where it has none, so the base still draws;
    a base whose own modules do not import even so is unfit."""

    def write(self, path, text):
        (self.root / path).write_text(text, encoding="utf-8")

    def commit(self, message):
        subprocess.run(["git", "-C", str(self.root), "add", "--all"], check=True)
        identity = ("user.name=Example", "user.email=example@example.com", "commit.gpgsign=false")
        configured = [part for setting in identity for part in ("-c", setting)]
        subprocess.run(
            ["git", "-C", str(self.root), *configured, "commit", "--quiet", "--no-verify", "-m", message], check=True
        )

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        subprocess.run(["git", "init", "--quiet", str(self.root)], check=True)
        self.write("seeds.py", "SERVER = 1\n")
        self.write("walk.py", "lazy from seeds import SERVER\n")
        self.write("chain.py", "from walk import SERVER\n")
        self.write("fine.py", "lazy from seeds import SERVER\n")
        self.commit("base")

    def kept(self, laid, **options):
        env = {name: value for name, value in os.environ.items() if name != "PYTHONPATH"}
        return look.kept_own(self.root, "HEAD", laid, [sys.executable], env, **{"loads": (), **options})

    def test_a_file_naming_a_seed_the_base_lacks_is_the_bases_own_and_what_imports_it_follows(self):
        # This tree's walk names a seed the base lacks, lazily as the estate does,
        # and its chain names what only this tree's walk holds.
        self.write("walk.py", "lazy from seeds import SERVER, SWITCH\nWALKED = 1\n")
        self.write("chain.py", "from walk import WALKED\n")
        self.write("fine.py", "lazy from seeds import SERVER\nFINE = 1\n")
        self.write("new.py", "lazy from seeds import SWITCH\n")

        self.assertEqual(self.kept(["chain.py", "fine.py", "new.py", "walk.py"]), ["chain.py", "new.py", "walk.py"])
        self.assertEqual((self.root / "walk.py").read_text(encoding="utf-8"), "lazy from seeds import SERVER\n")
        self.assertIn("FINE", (self.root / "fine.py").read_text(encoding="utf-8"))
        # A new file only this tree has, which does not import there, is not left for the base to load.
        self.assertFalse((self.root / "new.py").exists())

    def test_a_probe_reading_a_selector_the_base_lacks_as_it_loads_is_the_bases_own(self):
        self.write("parts.py", "SELECTORS = {'frame': '.frame'}\n")
        self.write("probe.py", "from parts import SELECTORS\nREAD = SELECTORS['frame']\n")
        self.commit("probe")
        self.write("probe.py", "from parts import SELECTORS\nREAD = SELECTORS['facts_list']\n")

        self.assertEqual(self.kept(["probe.py"]), ["probe.py"])
        self.assertIn("'frame'", (self.root / "probe.py").read_text(encoding="utf-8"))

    def test_files_that_import_at_the_base_stay_as_this_tree_has_them(self):
        self.write("walk.py", "lazy from seeds import SERVER\nWALKED = 1\n")

        self.assertEqual(self.kept(["walk.py", "seeds.py"], loads=("chain",)), [])
        self.assertIn("WALKED", (self.root / "walk.py").read_text(encoding="utf-8"))

    def test_a_base_module_that_reads_what_the_laid_lens_took_away_draws_with_its_own_lens(self):
        # The laid probe imports at the base, but the base's own survey reads a name it no longer holds,
        # and a file this tree took out.
        self.write("probe.py", "ONLY_ON = 1\n")
        self.write("gone.py", "GONE = 1\n")
        self.write("survey.py", "from probe import ONLY_ON\nfrom gone import GONE\n")
        self.commit("survey")
        self.write("probe.py", "EVERY = 1\n")
        (self.root / "gone.py").unlink()

        kept = self.kept(["probe.py"], taken=("gone.py",), loads=("survey",))

        self.assertEqual(kept, ["gone.py", "probe.py"])
        self.assertIn("ONLY_ON", (self.root / "probe.py").read_text(encoding="utf-8"))
        self.assertTrue((self.root / "gone.py").is_file())

    def test_a_base_that_does_not_import_its_own_modules_is_unfit_saying_why(self):
        self.write("survey.py", "from seeds import SWITCH\n")
        self.commit("broken")
        self.write("walk.py", "lazy from seeds import SERVER\nWALKED = 1\n")

        with self.assertRaises(look.BaseUnfit) as unfit:
            self.kept(["walk.py"], loads=("survey",))
        self.assertIn("survey", str(unfit.exception))
        self.assertIn("SWITCH", str(unfit.exception))
