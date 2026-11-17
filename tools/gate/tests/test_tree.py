"""The tree a checkout's files would commit, read from a copy of its index."""

import os
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from tools.gate.tests.base import fixture_git
from tools.gate.tree import working_tree

# A moment long past, stamped on the file and the index alike, so the index's
# entry for the file is racily clean: written in the same instant as the index.
STAMP = 1_700_000_000


class WorkingTreeTests(SimpleTestCase):
    def test_a_same_size_rewrite_racily_clean_in_the_index_is_in_the_tree(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            git = fixture_git(root)
            git("init", "--quiet")
            # git then judges a file by its size and times alone, and the stamp fixes those.
            git("config", "core.trustctime", "false")
            source = root / "source.py"
            source.write_text("EXAMPLE = 1\n")
            os.utime(source, (STAMP, STAMP))
            git("add", "--all")
            git("commit", "--quiet", "-m", "base")
            index = Path(root, git("rev-parse", "--git-path", "index"))
            os.utime(index, (STAMP, STAMP))
            source.write_text("EXAMPLE = 2\n")
            os.utime(source, (STAMP, STAMP))

            seen = working_tree(root)

            git("add", "--all")
            self.assertEqual(seen, git("write-tree"))
